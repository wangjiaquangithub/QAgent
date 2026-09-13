"""Cancel an already-linked Runtime run through the Runtime public boundary.

AG-G2-AUTO-005-B02.

Cancel is the one place where the Task Center must reach the Runtime while
tearing state down, so the rules here are deliberately asymmetric with the
opt-in path:

- the task's existing ``execution_authorized`` flag is **not** a precondition:
  cancelling is what revokes it, so requiring it would make cancellation
  impossible exactly when it is needed;
- nothing writes to the Runtime's own tables. Cancellation goes through the
  existing public contract call ``request_cancel(run_id)`` and nothing else;
- a task without a linkage keeps the untouched legacy cancel path;
- cancelling twice is a no-op, decided from the history already on the task, so
  the Runtime is not asked a second time;
- the recorded terminal state is ``cancelled``, and the projection refuses to
  move a terminal task, so an old executor cannot later overwrite it with
  ``completed``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from app.gateway.task_runtime_context import TaskRuntimeContext
from app.gateway.task_runtime_linkage import RuntimeRunLinkageError, read_linked_runtime_run
from app.gateway.task_runtime_projection import HISTORY_FIELD, build_runtime_history_record

__all__ = [
    "RuntimeCancelOutcome",
    "RuntimeCancelError",
    "RuntimeCancelContract",
    "cancel_linked_runtime_run",
]

CancelAction = Literal[
    "runtime_cancelled",
    "legacy_not_linked",
    "already_cancelled",
    "runtime_unavailable",
]


class RuntimeCancelContract(Protocol):
    """The subset of the Runtime public contract used for cancellation."""

    async def request_cancel(self, run_id: str) -> dict[str, Any]: ...


class RuntimeCancelError(RuntimeError):
    """Raised when a Runtime cancellation must not be attempted for this task."""


@dataclass(frozen=True)
class RuntimeCancelOutcome:
    """What the cancellation attempt did."""

    action: CancelAction
    reason: str
    runtime_run_id: str | None = None
    runtime_response: dict[str, Any] | None = None
    updated_task: dict[str, Any] | None = None

    @property
    def used_runtime(self) -> bool:
        return self.action == "runtime_cancelled"


def _already_cancelled(task: Mapping[str, Any], *, run_id: str) -> bool:
    history = task.get(HISTORY_FIELD)
    if not isinstance(history, list):
        return False
    for entry in history:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("runtime_run_id") or "") != run_id:
            continue
        if str(entry.get("runtime_status") or "") == "cancelled":
            return True
    return False


async def cancel_linked_runtime_run(
    task: Mapping[str, Any] | None,
    *,
    context: TaskRuntimeContext,
    contract: RuntimeCancelContract | None,
) -> RuntimeCancelOutcome:
    """Cancel the Runtime run linked to ``task``, if there is one.

    Never raises for a task that is simply not linked — that is the legacy path.
    Raises :class:`RuntimeCancelError` when the linkage is foreign, belongs to
    another organization, or the cancellation itself fails, so the caller can log
    it instead of silently leaving a run alive.
    """
    if not isinstance(task, Mapping):
        raise RuntimeCancelError("no server-loaded task row supplied")
    if not isinstance(context, TaskRuntimeContext):
        raise RuntimeCancelError("no trusted task runtime context supplied")

    task_id = str(task.get("id") or "").strip()
    if not task_id:
        raise RuntimeCancelError("task row has no id")

    # The shared org-consistency gate: a foreign organization's linkage is
    # refused here, so this path can never cancel another org's run.
    try:
        linkage = read_linked_runtime_run(task, context=context)
    except RuntimeRunLinkageError as exc:
        raise RuntimeCancelError(str(exc)) from exc

    if linkage is None:
        return RuntimeCancelOutcome("legacy_not_linked", "no_runtime_linkage")

    run_id = linkage.runtime_run_id

    if _already_cancelled(task, run_id=run_id):
        return RuntimeCancelOutcome("already_cancelled", "cancelled_already_recorded", run_id)

    if contract is None:
        return RuntimeCancelOutcome("runtime_unavailable", "runtime_contract_unavailable", run_id)

    # The only way this module touches the Runtime: its existing public call.
    response = await contract.request_cancel(run_id)
    if not isinstance(response, Mapping):
        response = {"raw": response}

    # Record the request in the shared runtime history vocabulary without
    # touching the task status: the existing cancel handler already owns that,
    # and the projection must stay free to refuse terminal regressions.
    updated_task = dict(task)
    history = updated_task.get(HISTORY_FIELD)
    history_list = list(history) if isinstance(history, list) else []
    history_list.append(
        build_runtime_history_record(
            runtime_status="cancelled",
            run_id=run_id,
            org_scope_key=context.org_scope_key,
            reason="cancellation requested through the runtime public boundary",
        )
    )
    updated_task[HISTORY_FIELD] = history_list

    return RuntimeCancelOutcome(
        "runtime_cancelled",
        "request_cancel_accepted",
        run_id,
        dict(response),
        updated_task,
    )
