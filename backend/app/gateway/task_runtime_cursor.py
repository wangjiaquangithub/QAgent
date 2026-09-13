"""Minimal per-run stream cursor for the Task Center.

AG-G2-AUTO-018.

The history list is the record of what a task has been told; the cursor is a
watermark of *how far* along the run's event stream that record reaches. Keeping
one means a consumer that reconnects — a new process, a reopened stream — can tell
in constant time whether a frame it just received is already applied, instead of
scanning the whole history.

Where it lives, and what it is not
----------------------------------
The cursor is stored in the task row's **existing** extras slot (the same slot the
run linkage uses), so no table, no migration and no schema change is introduced.
It is a watermark, never a second source of truth:

- it is only advanced together with a history record, so it cannot get ahead of
  the record it describes;
- the projection never consults it to decide whether a frame is applied — the
  history does that — so a tampered cursor can neither fabricate task state nor
  hide a frame that was actually written. The worst a forged value can do is make
  a *consumer* of :func:`applied_watermark` resume from a later position in the
  same run of the same task of the same organization;
- a cursor belonging to another organization or another task is refused, not
  treated as absent, exactly like every other Task <-> Runtime value;
- it only ever describes the run this task *currently follows*. The linkage is
  the single source of truth for which run that is, so a late frame from a
  superseded run cannot take the watermark back off the live one. A retry that
  supersedes the linkage starts a new stream, and the new run replaces the
  watermark rather than merging with it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.gateway.task_runtime_context import TaskRuntimeContext
from app.gateway.task_runtime_linkage import (
    RuntimeRunLinkageError,
    read_linked_runtime_run,
)

__all__ = [
    "CURSOR_TASK_KEY",
    "RuntimeCursorError",
    "RuntimeRunCursor",
    "advance_runtime_cursor",
    "applied_watermark",
    "read_runtime_cursor",
]

# The key that lands in the task row's existing extras / extra_json slot.
CURSOR_TASK_KEY = "runtime_run_cursor"


class RuntimeCursorError(RuntimeError):
    """Raised when a stored stream cursor is malformed or unsafe."""


@dataclass(frozen=True)
class RuntimeRunCursor:
    """How far along one Runtime run's event stream this task has been told."""

    run_id: str
    org_scope_key: str
    task_id: str
    last_sequence: int
    last_event_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "org_scope_key": self.org_scope_key,
            "task_id": self.task_id,
            "last_sequence": self.last_sequence,
        }
        if self.last_event_id is not None:
            payload["last_event_id"] = self.last_event_id
        return payload


def _decode(raw: Any) -> RuntimeRunCursor:
    if not isinstance(raw, Mapping):
        raise RuntimeCursorError("stored stream cursor is not a mapping")
    run_id = str(raw.get("run_id") or "").strip()
    org_scope_key = str(raw.get("org_scope_key") or "").strip()
    task_id = str(raw.get("task_id") or "").strip()
    if not run_id or not org_scope_key or not task_id:
        raise RuntimeCursorError("stored stream cursor is incomplete")
    try:
        last_sequence = int(raw.get("last_sequence"))  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RuntimeCursorError("stored stream cursor has no usable sequence") from exc
    if last_sequence < 0:
        raise RuntimeCursorError("stored stream cursor has a negative sequence")
    event_id = str(raw.get("last_event_id") or "").strip() or None
    return RuntimeRunCursor(
        run_id=run_id,
        org_scope_key=org_scope_key,
        task_id=task_id,
        last_sequence=last_sequence,
        last_event_id=event_id,
    )


def _trusted_scope(task: Mapping[str, Any], context: TaskRuntimeContext) -> str:
    if not isinstance(context, TaskRuntimeContext):
        raise RuntimeCursorError("no trusted task runtime context supplied")
    if not isinstance(task, Mapping):
        raise RuntimeCursorError("no server-loaded task row supplied")
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        raise RuntimeCursorError("task row has no id")
    if task_id != context.task_id:
        raise RuntimeCursorError("task runtime context does not belong to this task")
    return task_id


def _linked_run_id(
    task: Mapping[str, Any] | None, *, context: TaskRuntimeContext
) -> str | None:
    """The run this task currently follows, through the shared linkage gate.

    Uses ``read_linked_runtime_run`` rather than reading the extras slot directly,
    so "which run is live" has exactly one definition and a linkage owned by
    another organization is refused here too. The linkage's own error surface is
    translated into this module's, so callers of the cursor see one exception
    type for every unsafe cursor.
    """
    try:
        linkage = read_linked_runtime_run(task, context=context)
    except RuntimeRunLinkageError as exc:
        raise RuntimeCursorError(str(exc)) from exc
    return None if linkage is None else linkage.runtime_run_id


def read_runtime_cursor(
    task: Mapping[str, Any] | None,
    *,
    context: TaskRuntimeContext,
) -> RuntimeRunCursor | None:
    """The stored cursor, scoped to the caller's trusted organization and task.

    Returns ``None`` when no cursor exists. A cursor that belongs to another
    organization or another task is refused rather than reported as absent.
    """
    task_id = _trusted_scope(task, context)  # type: ignore[arg-type]
    assert isinstance(task, Mapping)

    raw = task.get(CURSOR_TASK_KEY)
    if raw is None:
        return None

    cursor = _decode(raw)
    if cursor.org_scope_key != context.org_scope_key:
        raise RuntimeCursorError("stream cursor belongs to another organization")
    if cursor.task_id != task_id:
        raise RuntimeCursorError("stream cursor belongs to another task")
    return cursor


def advance_runtime_cursor(
    task: Mapping[str, Any] | None,
    *,
    context: TaskRuntimeContext,
    run_id: str,
    sequence: int | None = None,
    event_id: str | None = None,
) -> tuple[dict[str, Any], RuntimeRunCursor | None]:
    """Advance the watermark for ``run_id`` and return the updated task row.

    The watermark never moves backwards, and a cursor for a *different* run is
    replaced rather than merged: a new attempt is a new stream. Callers hold the
    returned task only when they are going to persist it, so an absent history
    record is never accompanied by an advanced cursor.

    ``run_id`` must be the run the task is currently linked to. The linkage is
    what decides which run is live, so a frame from a superseded run — an event
    that arrived after a retry re-pointed the task — is refused instead of
    silently taking the watermark over. This is what stops a late old-run frame
    from rewinding what a consumer believes has been applied.
    """
    task_id = _trusted_scope(task, context)  # type: ignore[arg-type]
    assert isinstance(task, Mapping)
    target = str(run_id or "").strip()
    if not target:
        raise RuntimeCursorError("runtime_run_id is required")

    if _linked_run_id(task, context=context) != target:
        raise RuntimeCursorError(
            "stream frame belongs to a run this task does not follow"
        )

    current = read_runtime_cursor(task, context=context)
    position = None if sequence is None else int(sequence)
    if position is not None and position < 0:
        raise RuntimeCursorError("stream cursor sequence must not be negative")

    if position is None and event_id is None:
        # Nothing positional to record, so there is no watermark to move.
        return dict(task), current

    if current is not None and current.run_id == target:
        if position is not None and position <= current.last_sequence:
            # Already at or past this position: the watermark does not move.
            return dict(task), current
        new_sequence = current.last_sequence if position is None else position
        new_event_id = current.last_event_id if event_id is None else str(event_id).strip() or None
    else:
        if position is None:
            # Nothing positional to record, so there is no watermark to start.
            return dict(task), current
        new_sequence = position
        new_event_id = None if event_id is None else str(event_id).strip() or None

    cursor = RuntimeRunCursor(
        run_id=target,
        org_scope_key=context.org_scope_key,
        task_id=task_id,
        last_sequence=new_sequence,
        last_event_id=new_event_id,
    )
    updated = dict(task)
    updated[CURSOR_TASK_KEY] = cursor.to_dict()
    return updated, cursor


def applied_watermark(
    task: Mapping[str, Any] | None,
    *,
    context: TaskRuntimeContext,
    run_id: str,
    history: Sequence[Mapping[str, Any]] | None = None,
) -> int | None:
    """The furthest stream position this task can be said to have applied.

    Takes the larger of the stored cursor and the highest sequence the history
    actually holds for this run, so a cursor that was raised without a matching
    record cannot make a consumer believe a frame was applied when it was not, and
    a missing cursor cannot make it re-read frames it already has.
    """
    target = str(run_id or "").strip()
    if not target:
        return None

    cursor = read_runtime_cursor(task, context=context)
    from_cursor = cursor.last_sequence if cursor is not None and cursor.run_id == target else None

    from_history: int | None = None
    for entry in history or ():
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("runtime_run_id") or "") != target:
            continue
        try:
            value = int(entry.get("sequence"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if from_history is None or value > from_history:
            from_history = value

    if from_cursor is None:
        return from_history
    if from_history is None:
        return from_cursor
    return max(from_cursor, from_history)
