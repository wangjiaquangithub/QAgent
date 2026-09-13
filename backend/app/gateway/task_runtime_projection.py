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

Monotonicity (AG-G2-AUTO-011)
-----------------------------
The projection only ever moves a task forward, and it does so per run:

- a terminal task is never overwritten by a later terminal, so ``cancelled``,
  ``failed`` and ``completed`` cannot overwrite each other;
- a non-terminal status that is behind what the same run already reported is
  refused, whether it arrives out of order or as a later status re-read with no
  sequence;
- an exact repeat of the stage this run already reported changes nothing;
- the check is scoped to one ``run_id``, so a retry that links a new run is a new
  monotonic sequence rather than a regression of the previous one;
- projection is a pure function over the task row: it never calls the Runtime and
  therefore can never alter the Runtime's own state.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app.gateway.task_runtime_context import TaskRuntimeContext
from app.gateway.task_runtime_failure import sanitize_error_code, sanitize_failure_detail
from app.gateway.task_runtime_linkage import RuntimeRunLinkageError, read_linked_runtime_run
from app.gateway.task_runtime_result import sanitize_result_summary

__all__ = [
    "ProjectionOutcome",
    "RuntimeProjectionError",
    "build_runtime_history_record",
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

# Per-run monotonic stage order (AG-G2-AUTO-011). Terminal statuses are handled
# by the terminal branches and are deliberately absent: the order only exists to
# decide whether an incoming non-terminal status is *behind* what the same run
# has already reported, so an out-of-order frame or a later status re-read can
# never move the task backwards. The order follows the Runtime's own state
# machine; a run never legitimately returns to an earlier stage.
_RUNTIME_STAGE_RANK = {
    "created": 0,
    "queued": 1,
    "planning": 2,
    "waiting_approval": 3,
    "running": 4,
    "executing": 5,
}

_STATUS_PATTERN = re.compile(r"[^A-Za-z0-9._:-]+")

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


def _latest_stage(history: list[dict[str, Any]], *, run_id: str) -> str | None:
    """The most advanced non-terminal stage this run already reported.

    Scoped to one ``run_id`` on purpose: a retry that links a *new* run starts
    from an empty stage history, so it is never mistaken for a regression of the
    previous run.
    """
    best: str | None = None
    best_rank = -1
    for entry in history:
        if str(entry.get("runtime_run_id") or "") != run_id:
            continue
        rank = _RUNTIME_STAGE_RANK.get(str(entry.get("runtime_status") or ""))
        if rank is not None and rank > best_rank:
            best_rank = rank
            best = str(entry.get("runtime_status") or "")
    return best


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
    result_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one history record, redacting the failure detail.

    The failure fields are sanitised here rather than by the caller, so every
    writer — the event bridge, the shared history builder, and any future call
    site — inherits the same redaction and none can bypass it by accident.
    """
    clean_code = sanitize_error_code(error_code)
    clean_reason, withheld = sanitize_failure_detail(reason)

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
    if clean_code is not None:
        record["error_code"] = clean_code
    if clean_reason is not None:
        record["reason"] = clean_reason
    if withheld:
        # A withheld detail is stated, so "no detail" and "detail hidden" stay
        # distinguishable to a reader.
        record["error_detail_redacted"] = True
    if runtime_status == _AWAITING_APPROVAL:
        record["hint"] = "Runtime run is waiting for an approval decision."
    if runtime_status == "completed":
        # A completed run always states whether a displayable result exists, so
        # "no result" is explicit rather than an ambiguous absence.
        record["result_available"] = result_summary is not None
        if result_summary is not None:
            record["result"] = dict(result_summary)
    return record


def build_runtime_history_record(
    *,
    runtime_status: str,
    run_id: str,
    org_scope_key: str,
    approval_required: bool = False,
    event_id: str | None = None,
    sequence: int | None = None,
    error_code: str | None = None,
    reason: str | None = None,
    result_summary: Any = None,
) -> dict[str, Any]:
    """Build an ``execution_history`` record in the shared runtime vocabulary.

    Exposed so call sites that must record a Runtime outcome without touching the
    task status (for example a cancellation request) produce the same shape as the
    projection does. A ``result_summary`` is sanitised here rather than by the
    caller, so no call site can write an unredacted result into history.
    """
    return _build_record(
        runtime_status=_normalize_status(runtime_status),
        run_id=str(run_id or "").strip(),
        org_scope_key=str(org_scope_key or "").strip(),
        approval_required=approval_required,
        event_id=event_id,
        sequence=sequence,
        error_code=error_code,
        reason=reason,
        result_summary=sanitize_result_summary(result_summary),
    )


def project_runtime_status(
    task: Mapping[str, Any] | None,
    *,
    context: TaskRuntimeContext,
    runtime_status: str,
    event_id: str | None = None,
    sequence: int | None = None,
    error_code: str | None = None,
    reason: str | None = None,
    expected_run_id: str | None = None,
    result_summary: Any = None,
) -> tuple[dict[str, Any], ProjectionOutcome]:
    """Project one Runtime status onto a linked Task Center task row.

    Returns the updated task dict (persist through the existing task save path)
    and what happened. Raises :class:`RuntimeProjectionError` when the projection
    is not allowed: unlinked task, another run, another organization, or a
    regression attempt.

    ``expected_run_id`` is for callers that carry a run id from an incoming
    Runtime frame. When given it must match the stored linkage, so a frame for
    another run (or another organization's run) can never be written into this
    task's history under the linked run's identity.

    ``result_summary`` is the raw Runtime result, if the caller has one. It is
    sanitised here through the allowlisted summary, so a caller cannot write an
    unredacted result into history. It is only recorded for ``completed``, which
    always states whether a displayable result exists.
    """
    if not isinstance(task, Mapping):
        raise RuntimeProjectionError("no server-loaded task row supplied")
    if not isinstance(context, TaskRuntimeContext):
        raise RuntimeProjectionError("no trusted task runtime context supplied")

    task_id = str(task.get("id") or "").strip()
    if not task_id:
        raise RuntimeProjectionError("task row has no id")

    status = _normalize_status(runtime_status)

    # Cross-organization protection: the shared linkage gate refuses a linkage
    # owned by another organization or another task, and one error surface is
    # exposed to callers.
    try:
        linkage = read_linked_runtime_run(task, context=context)
    except RuntimeRunLinkageError as exc:
        raise RuntimeProjectionError(str(exc)) from exc
    if linkage is None:
        raise RuntimeProjectionError("task is not linked to a runtime run")

    run_id = linkage.runtime_run_id

    if expected_run_id is not None and str(expected_run_id).strip() != run_id:
        raise RuntimeProjectionError("runtime event belongs to a different runtime run")

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

    # Monotonicity within one run: a status that is behind what this run has
    # already reported never moves the task, whether it arrived out of order or
    # as a later status re-read that carries no sequence at all.
    previous_stage = _latest_stage(history, run_id=run_id)
    if previous_stage is not None:
        incoming_rank = _RUNTIME_STAGE_RANK.get(status)
        previous_rank = _RUNTIME_STAGE_RANK[previous_stage]
        if incoming_rank is not None and incoming_rank < previous_rank:
            return dict(task), ProjectionOutcome(
                "stale", "runtime_status_regression", current, current
            )
        if incoming_rank == previous_rank:
            # This run already reported exactly this stage: idempotent repeat.
            return dict(task), ProjectionOutcome("noop", "duplicate_status", current, current)

    record = _build_record(
        runtime_status=status,
        run_id=run_id,
        org_scope_key=context.org_scope_key,
        approval_required=status == _AWAITING_APPROVAL,
        event_id=event_id,
        sequence=sequence,
        error_code=error_code,
        reason=reason,
        result_summary=sanitize_result_summary(result_summary),
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
