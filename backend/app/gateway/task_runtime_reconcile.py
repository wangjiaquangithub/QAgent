"""Idempotent Runtime -> Task status re-projection for already-linked tasks.

AG-G2-AUTO-012.

Why this exists
---------------
The projection runs while a Runtime frame flows past. If the process stops in
between — a restart, a dropped connection, a task whose projection simply lagged
— the Task Center can hold a task that is linked to a Runtime run whose state has
since moved on. Nothing repairs that by itself, so the user sees a stale status.

``reconcile_linked_task_runtime`` reads the Runtime's own state for an existing
linkage and re-projects it. It is deliberately the *only* thing it does:

- the Runtime stays the single source of truth: reconciliation **reads**
  (``get_run_status``) and never writes, so nothing here can alter a run;
- no provider is executed and no run is created — a linked task is never
  re-triggered, and the reconciler has no ``create_run`` path at all;
- the write is the existing projection, so a repeat is guaranteed to be
  idempotent rather than a second history record: the projection already refuses
  a duplicate event, a stage behind what the run reported, and a regression of a
  terminal task;
- an unusable Runtime answer is fail-safe: the task is left exactly as it is
  rather than being marked with a state nobody reported. Degrading a broken
  linkage into a user-visible message is a separate concern;
- with the server opt-in switch off the whole path is inert, so a deployment that
  has not opted in keeps byte-identical legacy behaviour.

Where it is called from
-----------------------
Explicitly, by whoever wants a repaired view: a startup sweep or a task read
path, both of which live in modules this domain must not modify (Gateway global
registration / background startup). So this module exposes the operation and its
preconditions instead of installing a hook of its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from app.gateway.task_runtime_context import TaskRuntimeContext
from app.gateway.task_runtime_linkage import read_linked_runtime_run
from app.gateway.task_runtime_optin import runtime_unattended_enabled
from app.gateway.task_runtime_projection import (
    ProjectionOutcome,
    RuntimeProjectionError,
    project_runtime_status,
)

__all__ = [
    "ReconcileOutcome",
    "RuntimeReconcileContract",
    "reconcile_linked_task_runtime",
]

ReconcileAction = Literal[
    "reconciled",
    "unchanged",
    "not_linked",
    "terminal",
    "disabled",
    "runtime_unavailable",
]

_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})


class RuntimeReconcileContract(Protocol):
    """The read-only slice of the Runtime public contract used here.

    Read-only on purpose: reconciliation must never be able to create, start or
    cancel anything, so those methods are deliberately absent from the protocol.
    """

    async def get_run_status(self, run_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ReconcileOutcome:
    """What the reconciliation attempt did."""

    action: ReconcileAction
    reason: str
    runtime_run_id: str | None = None
    projected_status: str | None = None
    updated_task: dict[str, Any] | None = None
    projection: ProjectionOutcome | None = None

    @property
    def changed(self) -> bool:
        return self.action == "reconciled"


def _outcome(
    action: ReconcileAction, reason: str, *, run_id: str | None = None
) -> ReconcileOutcome:
    return ReconcileOutcome(action=action, reason=reason, runtime_run_id=run_id)


async def reconcile_linked_task_runtime(
    task: Mapping[str, Any] | None,
    *,
    context: TaskRuntimeContext,
    contract: RuntimeReconcileContract | None,
) -> ReconcileOutcome:
    """Re-project a linked task's Runtime state, idempotently.

    Never raises for a task that is simply not linked, or whose Runtime answer is
    unusable — those are reported as outcomes. An unsafe or foreign linkage still
    raises the linkage error every other Task <-> Runtime path raises, so a caller
    can log it rather than silence it.
    """
    if not runtime_unattended_enabled():
        return _outcome("disabled", "runtime_disabled")

    # The shared org-consistency gate: a foreign organization's linkage is
    # refused before any Runtime call is considered.
    linkage = read_linked_runtime_run(task, context=context)
    if linkage is None:
        return _outcome("not_linked", "no_runtime_linkage")

    run_id = linkage.runtime_run_id

    current = str((task or {}).get("status") or "").strip().lower()
    if current in _TERMINAL_TASK_STATUSES:
        # A terminal task is already settled and the projection refuses to move
        # it; there is nothing to reconcile and no reason to spend a read.
        return _outcome("terminal", "task_already_terminal", run_id=run_id)

    if contract is None:
        return _outcome("runtime_unavailable", "runtime_contract_unavailable", run_id=run_id)

    status = await contract.get_run_status(run_id)
    if not isinstance(status, Mapping):
        return _outcome("runtime_unavailable", "runtime_status_unusable", run_id=run_id)

    reported = str(status.get("status") or "").strip().lower()
    if not reported:
        return _outcome("runtime_unavailable", "runtime_status_missing", run_id=run_id)

    # If the Runtime names the run its answer belongs to, it must be the run this
    # task is linked to.
    echoed_run_id = str(status.get("run_id") or "").strip() or run_id

    try:
        updated, projection = project_runtime_status(
            task,
            context=context,
            runtime_status=reported,
            expected_run_id=echoed_run_id,
        )
    except RuntimeProjectionError as exc:
        # An unsafe answer (for example a mismatching run) leaves the task alone.
        return ReconcileOutcome(
            action="runtime_unavailable",
            reason=f"projection_refused:{exc}",
            runtime_run_id=run_id,
        )

    if projection.action in {"noop", "stale"}:
        return ReconcileOutcome(
            action="unchanged",
            reason=projection.reason,
            runtime_run_id=run_id,
            projected_status=reported,
            projection=projection,
        )

    return ReconcileOutcome(
        action="reconciled",
        reason=projection.reason,
        runtime_run_id=run_id,
        projected_status=reported,
        updated_task=updated,
        projection=projection,
    )
