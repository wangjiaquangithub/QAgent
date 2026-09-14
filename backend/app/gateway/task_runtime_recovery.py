"""Recover the Task Center's view of a linked Runtime run after a disconnect.

AG-G2-AUTO-019.

The problem
-----------
A Task Center client can lose its connection to the server while a Runtime-backed
task is running: the browser tab is closed, the server process is restarted, an
SSE stream is dropped. Whatever the client had on screen is gone, and whatever is
in the server's memory is gone with it. What survives is the task row and the run
in the Runtime — nothing else may be relied on.

So recovery has to be assembled from **already persisted state only**:

- the task row the server just loaded (the existing status, and the existing
  ``execution_history`` list the existing endpoint already returns), and
- the Runtime's own state, read through the existing read-only reconciliation.

Nothing in-process is consulted, and nothing in-process is needed: a brand new
process that has never seen this task produces the same view as the one that
dropped the connection. That is what makes a recovered view trustworthy rather
than a cache that happens to be warm.

What it deliberately does not do
--------------------------------
- **It never re-executes the run.** The only Runtime call it can reach is the
  read-only ``get_run_status`` that reconciliation already uses; there is no
  ``create_run``, ``start_run``, ``resume_run`` or ``request_cancel`` path here,
  so a reconnect can never restart or duplicate work.
- **It never writes a history record of its own.** The only write it can cause is
  the existing idempotent projection, reached through reconciliation, which
  already refuses a duplicate event, a repeated stage and a terminal regression.
  Recovering twice therefore leaves the history exactly as long as recovering
  once.
- **It never invents state.** When the Runtime cannot be read, the view is still
  returned from what is persisted, and the outcome says the catch-up did not
  happen rather than guessing.

Fail-closed, not fail-silent
----------------------------
A linkage owned by another organization or another task is refused exactly as
every other Task <-> Runtime path refuses it, so recovery cannot become a way to
read a foreign run. A *stored stream cursor* that is malformed, however, is a
different case: it is display bookkeeping, and letting it block the view would
hide a task the user is entitled to see. Such a cursor is reported as rejected —
``cursor_rejected=True`` — and the view falls back to the history it still has.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from app.gateway.task_runtime_context import TaskRuntimeContext
from app.gateway.task_runtime_cursor import RuntimeCursorError, applied_watermark
from app.gateway.task_runtime_linkage import read_linked_runtime_run
from app.gateway.task_runtime_projection import HISTORY_FIELD
from app.gateway.task_runtime_reconcile import (
    ReconcileOutcome,
    RuntimeReconcileContract,
    reconcile_linked_task_runtime,
)

__all__ = [
    "RecoveryOutcome",
    "RuntimeRecoveryError",
    "TaskStreamView",
    "read_task_stream_view",
    "recover_task_stream",
]

_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})

RecoveryAction = Literal[
    "read",
    "caught_up",
    "unchanged",
    "not_linked",
    "terminal",
    "disabled",
    "runtime_unavailable",
]

# A reconciliation answer and a recovery answer are the same vocabulary except
# for the successful write, which recovery reports as a catch-up.
_RECOVERY_FOR_RECONCILE: dict[str, RecoveryAction] = {
    "unchanged": "unchanged",
    "not_linked": "not_linked",
    "terminal": "terminal",
    "disabled": "disabled",
    "runtime_unavailable": "runtime_unavailable",
}


class RuntimeRecoveryError(RuntimeError):
    """Raised when a task stream view cannot be assembled at all."""


@dataclass(frozen=True)
class TaskStreamView:
    """What a reconnected client can be told, built only from persisted state."""

    task_id: str
    org_scope_key: str
    status: str
    runtime_run_id: str | None = None
    history: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    watermark: int | None = None
    cursor_rejected: bool = False

    @property
    def linked(self) -> bool:
        return self.runtime_run_id is not None

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL_TASK_STATUSES

    @property
    def approx_history_length(self) -> int:
        return len(self.history)


@dataclass(frozen=True)
class RecoveryOutcome:
    """What a recovery attempt produced."""

    action: RecoveryAction
    reason: str
    view: TaskStreamView
    updated_task: dict[str, Any] | None = None
    reconcile: ReconcileOutcome | None = None

    @property
    def changed(self) -> bool:
        return self.action == "caught_up"


def _stored_history(task: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    raw = task.get(HISTORY_FIELD)
    if not isinstance(raw, list):
        return ()
    return tuple(entry for entry in raw if isinstance(entry, Mapping))


def read_task_stream_view(
    task: Mapping[str, Any] | None,
    *,
    context: TaskRuntimeContext,
) -> TaskStreamView:
    """Assemble the current view of a linked task from persisted state alone.

    Pure: it performs no I/O and writes nothing, so it is safe to call on every
    reconnect. The linkage is resolved through the shared organization gate, so a
    foreign organization's linkage is refused rather than displayed.
    """
    if not isinstance(task, Mapping):
        raise RuntimeRecoveryError("no server-loaded task row supplied")
    if not isinstance(context, TaskRuntimeContext):
        raise RuntimeRecoveryError("no trusted task runtime context supplied")

    linkage = read_linked_runtime_run(task, context=context)

    history = _stored_history(task)
    status = str(task.get("status") or "").strip().lower()
    task_id = str(task.get("id") or "").strip()

    if linkage is None:
        return TaskStreamView(
            task_id=task_id,
            org_scope_key=context.org_scope_key,
            status=status,
            history=history,
        )

    run_id = linkage.runtime_run_id
    try:
        watermark: int | None = applied_watermark(
            task, context=context, run_id=run_id, history=history
        )
        cursor_rejected = False
    except RuntimeCursorError:
        # Bookkeeping, not state: a cursor nobody can decode must not hide the
        # task. The history it would have summarised is still returned.
        watermark = None
        cursor_rejected = True

    return TaskStreamView(
        task_id=task_id,
        org_scope_key=context.org_scope_key,
        status=status,
        runtime_run_id=run_id,
        history=history,
        watermark=watermark,
        cursor_rejected=cursor_rejected,
    )


async def recover_task_stream(
    task: Mapping[str, Any] | None,
    *,
    context: TaskRuntimeContext,
    contract: RuntimeReconcileContract | None = None,
    catch_up: bool = True,
) -> RecoveryOutcome:
    """Rebuild the user-visible view of a linked task, optionally catching up.

    Always returns a view read from persisted state, even when the catch-up
    cannot happen. ``catch_up=False`` gives the pure read path, which is what a
    reconnect that must not touch the Runtime at all should use.

    A catch-up is delegated to the existing reconciliation, so it reads the
    Runtime without ever creating, starting, resuming or cancelling a run, and it
    writes at most the one idempotent projection the projection layer already
    refuses to duplicate.
    """
    view = read_task_stream_view(task, context=context)

    if not view.linked:
        return RecoveryOutcome(action="not_linked", reason="no_runtime_linkage", view=view)

    if not catch_up:
        return RecoveryOutcome(action="read", reason="read_from_stored_state", view=view)

    reconciled = await reconcile_linked_task_runtime(task, context=context, contract=contract)

    if reconciled.action == "reconciled":
        updated = reconciled.updated_task
        if updated is None:  # pragma: no cover - reconciliation always carries one
            return RecoveryOutcome(
                action="unchanged",
                reason="reconciled_without_update",
                view=view,
                reconcile=reconciled,
            )
        refreshed = read_task_stream_view(updated, context=context)
        return RecoveryOutcome(
            action="caught_up",
            reason=reconciled.reason,
            view=refreshed,
            updated_task=updated,
            reconcile=reconciled,
        )

    # Everything else — unchanged, terminal, switch off, Runtime unreadable — is
    # a legitimate answer, and the persisted view is what the user gets.
    return RecoveryOutcome(
        action=_RECOVERY_FOR_RECONCILE.get(reconciled.action, "unchanged"),
        reason=reconciled.reason,
        view=view,
        reconcile=reconciled,
    )
