"""Bridge QAgent Runtime Event v1 frames onto the existing Task history.

AG-G2-AUTO-004-B02.

Scope, deliberately narrow:

- an existing Runtime Event v1 frame (the shape produced by
  ``qagent_runtime.events.event_dict``) is translated into the Runtime status it
  represents and handed to the local projection, which appends to the task row's
  **existing** ``execution_history`` list;
- that list is already exposed to the frontend by the existing endpoint
  ``GET /api/tasks/{task_id}/execution-history`` (it returns the list verbatim),
  so no global SSE registry, cursor protocol, event schema or frontend change is
  needed or made here.

Replay and ordering follow the projection's own rules, because the bridge passes
``event_id`` and ``sequence`` straight through: the same event never appends
twice, and an older sequence never moves the task.

The frame's ``run_id`` is validated against the task's stored linkage before
anything is written, so a frame addressed to another run — or to another
organization's run — is refused instead of being attributed to this task's run
(AG-G2-AUTO-009).

A frame's ``payload["result"]`` is handed to the projection, which reduces it to
the allowlisted display summary and records whether a result was available. The
raw result never reaches the task (AG-G2-AUTO-013).

An ``asset.available`` frame is a side record: it adds the asset's allowlisted
metadata to the history at most once per asset per run and never moves the task
status. The asset's bytes and its storage location are not projected
(AG-G2-AUTO-015).

An ``approval.granted`` / ``approval.rejected`` frame is also a side record: the
decision becomes visible in the history, once, without moving the task status.
The task converges from the run status the Runtime reports next, so the Task
Center never keeps an approval state of its own and nothing a client writes can
make a task look approved (AG-G2-AUTO-016).

Runtime Event v1 has no ``run.timed_out`` frame, so a ``timed_out`` outcome is
applied by passing ``runtime_status`` explicitly; it is the only status that
cannot be derived from a frame type.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.gateway.task_runtime_asset import project_runtime_asset
from app.gateway.task_runtime_context import TaskRuntimeContext
from app.gateway.task_runtime_projection import (
    APPROVAL_GRANTED,
    APPROVAL_REJECTED,
    ProjectionOutcome,
    RuntimeProjectionError,
    project_runtime_status,
)

__all__ = [
    "ASSET_EVENT_TYPE",
    "EVENT_TYPE_TO_RUNTIME_STATUS",
    "apply_runtime_event",
    "runtime_status_for_event_type",
]

# Runtime Event v1 type -> the Runtime status it represents.
EVENT_TYPE_TO_RUNTIME_STATUS = {
    "run.created": "created",
    "run.queued": "queued",
    "run.planning": "planning",
    "run.waiting_approval": "waiting_approval",
    "run.running": "running",
    "run.executing": "executing",
    "run.completed": "completed",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
    # An approval decision is a marker: it is made visible in the history and
    # never moves the task, whose status converges from the run status the Runtime
    # reports next. The Task Center keeps no approval state of its own.
    "approval.granted": APPROVAL_GRANTED,
    "approval.rejected": APPROVAL_REJECTED,
}

# A frame that announces an artifact. It carries no run status either, but it does
# add a displayable record (the asset's metadata, never its bytes).
ASSET_EVENT_TYPE = "asset.available"


class RuntimeEventBridgeError(RuntimeError):
    """Raised when a Runtime frame cannot be bridged onto the task."""


def runtime_status_for_event_type(event_type: Any) -> str | None:
    """The Runtime status a frame type represents, or ``None`` for non-status frames."""
    name = str(event_type or "").strip().lower()
    if not name:
        return None
    return EVENT_TYPE_TO_RUNTIME_STATUS.get(name)


def _sequence_of(event: Mapping[str, Any]) -> int | None:
    raw = event.get("sequence")
    if raw is None:
        return None
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _error_fields(payload: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Extract a sanitised-by-the-projection error code and message."""
    raw_error = payload.get("error")
    if isinstance(raw_error, Mapping):
        code = raw_error.get("code")
        message = raw_error.get("message") or raw_error.get("reason")
    else:
        code = payload.get("error_code")
        message = raw_error if raw_error is not None else payload.get("reason")
    return (
        None if code is None else str(code),
        None if message is None else str(message),
    )


def apply_runtime_event(
    task: Mapping[str, Any] | None,
    *,
    context: TaskRuntimeContext,
    event: Mapping[str, Any] | None,
    runtime_status: str | None = None,
) -> tuple[dict[str, Any], ProjectionOutcome]:
    """Project one Runtime Event v1 frame onto an existing Task Center task row.

    ``runtime_status`` is only needed for outcomes Event v1 cannot express as a
    frame type (``timed_out``); when given it takes precedence.
    """
    if not isinstance(event, Mapping):
        raise RuntimeEventBridgeError("no runtime event supplied")

    run_id = str(event.get("run_id") or "").strip()
    if not run_id:
        raise RuntimeEventBridgeError("runtime event has no run_id")

    payload = event.get("payload")
    payload_map: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}

    status = str(runtime_status or "").strip().lower()
    if not status:
        event_type = str(event.get("type") or "").strip().lower()
        if not event_type:
            raise RuntimeEventBridgeError("runtime event has no type")
        if event_type == ASSET_EVENT_TYPE:
            # An asset is a side record: it never moves the task status, and its
            # metadata is projected at most once per asset per run.
            try:
                return project_runtime_asset(
                    task,
                    context=context,
                    asset=payload_map.get("asset"),
                    event_id=str(event.get("event_id") or "").strip() or None,
                    sequence=_sequence_of(event),
                    expected_run_id=str(event.get("run_id") or "").strip() or None,
                )
            except RuntimeProjectionError as exc:
                raise RuntimeEventBridgeError(str(exc)) from exc
        derived = runtime_status_for_event_type(event_type)
        if derived is None:
            raise RuntimeEventBridgeError(f"unsupported runtime event type: {event_type}")
        status = derived

    error_code, reason = _error_fields(payload_map)

    # The trusted run id still comes from the stored linkage, but the frame's own
    # run id is now enforced against it: a frame for another run — including one
    # owned by another organization — is refused instead of being written into
    # this task's history under the linked run's identity.
    try:
        return project_runtime_status(
            task,
            context=context,
            runtime_status=status,
            event_id=str(event.get("event_id") or "").strip() or None,
            sequence=_sequence_of(event),
            error_code=error_code,
            reason=reason,
            expected_run_id=run_id,
            result_summary=payload_map.get("result"),
        )
    except RuntimeProjectionError as exc:
        raise RuntimeEventBridgeError(str(exc)) from exc
