"""Project QAgent Runtime run status onto an existing Task Center task.

AG-G2-AUTO-003-B02.

The Runtime remains the single source of truth for run state; it lives in
PostgreSQL and nothing here copies runtime state, events or results back into
SQLite. This module only *projects* a Runtime status onto fields the Task Center
already has: the existing ``TaskStatus`` values and the existing
``execution_history`` list on the task row.

Frozen semantics (implemented as specified, not re-litigated here):

- ``waiting_approval``: the task status is **not** changed. It is neither
  ``paused`` nor ``planned``. An ``execution_history`` record carries
  ``runtime_status=waiting_approval``, ``approval_required=true`` and a
  sanitised hint.
- ``timed_out``: the task status becomes ``failed``, and the history record
  keeps ``runtime_status=timed_out`` plus a sanitised error code and reason.
- ``completed`` / ``failed`` / ``cancelled``: projected onto the existing values
  only, never regressing a terminal state, never overwritten by an older event.

No ``TaskStatus`` value is added, no frontend change, no table and no migration.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app.gateway.task_runtime_context import TaskRuntimeContext
from app.gateway.task_runtime_linkage import RuntimeRunLinkageError, read_runtime_run_linkage

__all__ = [
    "ProjectionOutcome",
    "RuntimeProjectionError",
    "project_runtime_status",
]

HISTORY_FIELD = "execution_history"

# Runtime status -> an existing TaskStatus value. ``waiting_approval`` is
# deliberately absent: it must not change the task status at all.
_RUNTIME_TO_TASK_STATUS = {
    "created": "pending",
    "queued": "pending",
    "planning": "planning",
    "running": "executing",
    "executing": "executing",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "timed_out": "failed",
}

_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})
_TERMINAL_RUNTIME_STATUSES = frozenset({"completed", "failed", "cancelled", "timed_out"})
_AWAITING_APPROVAL = "waiting_approval"

_STATUS_PATTERN = re.compile(r"[^A-Za-z0-9._:-]+")
_CODE_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")

ProjectionAction = Literal["status_updated", "history_only", "noop", "stale"]


class RuntimeProjectionError(RuntimeError):
    """Raised when a Runtime status must not be projected onto this task."""


@dataclass(frozen=True)
class ProjectionOutcome:
    """What the projection attempt did."""

    action: ProjectionAction
    reason: str
    status_before: str
    status_after: str
    record: dict[str, Any] | None = None


def _sanitize_text(value: Any, *, limit: int = 240) -> str | None:
    """Collapse a free-form message to a short, safe, single-line string."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    return text[:limit]


def _sanitize_code(value: Any) -> str | None:
    if value is None:
        return None
    code = _CODE_PATTERN.sub("", str(value).strip())
    return code[:64] or None


def _normalize_status(value: Any) -> str:
    status = _STATUS_PATTERN.sub("", str(value or "").strip().lower())
    if not status:
        raise RuntimeProjectionError("runtime status is required")
    return status


def _history_of(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = task.get(HISTORY_FIELD)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RuntimeProjectionError(f"task {HISTORY_FIELD} is not a list")
    return [entry for entry in raw if isinstance(entry, dict)]


def _is_duplicate(
    history: list[dict[str, Any]], *, run_id: str, runtime_status: str, event_id: str | None
) -> bool:
    for entry in history:
        if str(entry.get("runtime_run_id") or "") != run_id:
            continue
        if event_id is not None:
            if str(entry.get("event_id") or "") == event_id:
                return True
            continue
        if str(entry.get("runtime_status") or "") == runtime_status:
            return True
    return False


def _max_sequence(history: list[dict[str, Any]], *, run_id: str) -> int | None:
    seen: int | None = None
    for entry in history:
        if str(entry.get("runtime_run_id") or "") != run_id:
            continue
        try:
            value = int(entry.get("sequence"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if seen is None or value > seen:
            seen = value
    return seen


def _build_record(
    *,
    runtime_status: str,
    run_id: str,
    org_scope_key: str,
    approval_required: bool,
    event_id: str | None,
    sequence: int | None,
    error_code: str | None,
    reason: str | None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "source": "qagent_runtime",
        "runtime_run_id": run_id,
        "runtime_status": runtime_status,
        "org_scope_key": org_scope_key,
        "approval_required": approval_required,
    }
    if event_id is not None:
        record["event_id"] = event_id
    if sequence is not None:
        record["sequence"] = sequence
    if error_code is not None:
        record["error_code"] = error_code
    if reason is not None:
        record["reason"] = reason
    if runtime_status == _AWAITING_APPROVAL:
        record["hint"] = "Runtime run is waiting for an approval decision."
    return record


def project_runtime_status(
    task: Mapping[str, Any] | None,
    *,
    context: TaskRuntimeContext,
    runtime_status: str,
    event_id: str | None = None,
    sequence: int | None = None,
    error_code: str | None = None,
    reason: str | None = None,
) -> tuple[dict[str, Any], ProjectionOutcome]:
    """Project one Runtime status onto a linked Task Center task row.

    Returns the updated task dict (persist through the existing task save path)
    and what happened. Raises :class:`RuntimeProjectionError` when the projection
    is not allowed: unlinked task, another run, another organization, or a
    regression attempt.
    """
    if not isinstance(task, Mapping):
        raise RuntimeProjectionError("no server-loaded task row supplied")
    if not isinstance(context, TaskRuntimeContext):
        raise RuntimeProjectionError("no trusted task runtime context supplied")

    task_id = str(task.get("id") or "").strip()
    if not task_id:
        raise RuntimeProjectionError("task row has no id")
    if task_id != context.task_id:
        raise RuntimeProjectionError("task runtime context does not belong to this task")

    status = _normalize_status(runtime_status)

    # Cross-organization protection: the linkage reader refuses a linkage owned
    # by another organization, and one error surface is exposed to callers.
    try:
        linkage = read_runtime_run_linkage(task, org_scope_key=context.org_scope_key)
    except RuntimeRunLinkageError as exc:
        raise RuntimeProjectionError(str(exc)) from exc
    if linkage is None:
        raise RuntimeProjectionError("task is not linked to a runtime run")

    run_id = linkage.runtime_run_id
    current = str(task.get("status") or "").strip().lower()
    history = _history_of(task)

    if sequence is not None and _is_duplicate(
        history, run_id=run_id, runtime_status=status, event_id=event_id
    ):
        return dict(task), ProjectionOutcome("noop", "duplicate_event", current, current)

    # An event older than what this run already reported must never move the task.
    newest = _max_sequence(history, run_id=run_id)
    if sequence is not None and newest is not None and sequence < newest:
        return dict(task), ProjectionOutcome("stale", "older_sequence", current, current)

    if current in _TERMINAL_TASK_STATUSES:
        if status in _TERMINAL_RUNTIME_STATUSES:
            return dict(task), ProjectionOutcome("noop", "terminal_already_reached", current, current)
        # A non-terminal runtime event must not un-terminate the task.
        return dict(task), ProjectionOutcome("stale", "task_already_terminal", current, current)

    record = _build_record(
        runtime_status=status,
        run_id=run_id,
        org_scope_key=context.org_scope_key,
        approval_required=status == _AWAITING_APPROVAL,
        event_id=event_id,
        sequence=sequence,
        error_code=_sanitize_code(error_code),
        reason=_sanitize_text(reason),
    )

    updated = dict(task)
    updated_history = [*history, record]
    updated[HISTORY_FIELD] = updated_history

    target = _RUNTIME_TO_TASK_STATUS.get(status)
    if target is None:
        # waiting_approval, or any status the Task Centre cannot express: the
        # task status stays exactly as it was and only the history grows.
        return updated, ProjectionOutcome("history_only", status, current, current, record)

    if target == current:
        return updated, ProjectionOutcome("history_only", "status_unchanged", current, current, record)

    updated["status"] = target
    return updated, ProjectionOutcome("status_updated", status, current, target, record)
