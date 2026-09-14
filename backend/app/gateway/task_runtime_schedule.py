"""Persisted scheduling guard for Runtime opt-in tasks (scheduler / queue ticks).

AG-G2-AUTO-021.

The problem
-----------
The unattended queue picks candidates by *task status* alone. A task whose run
lives in the Runtime does not change its own status when the run is created — it
converges later, from the frames the Runtime sends — so between the trigger and
the convergence the task still looks like an untouched candidate. Every tick can
therefore pick it again, and a duplicate enqueue can hand it to the tick twice.
The only thing stopping a duplicate run today is the per-task asyncio lock in the
existing single-instance pipeline, and that lock does not survive a restart.

What this module adds
---------------------
A decision that can be answered from the **persisted task row alone**: has this
attempt already been handed to the Runtime?

It is answered by comparing the idempotency key the trigger *would* use — derived
from the linkage's own organization scope, the task id and the task's current
attempt — with the key the stored linkage actually carries. Same attempt, same
key: the run already exists, so a tick can skip the task instead of spending a
Runtime round-trip to rediscover it. A new attempt changes the key, so a retry is
never mistaken for a repeat.

Deliberately out of scope, per the card
---------------------------------------
No leader election, lease, fencing token or multi-instance coordination is added.
Under the existing single-instance semantics the persisted linkage is the dedup,
and this module only reads it.

Advisory, never authoritative
-----------------------------
This is a *selector*. It reads the linkage without an organization check, so a
tampered extras slot can make it say "already scheduled" and cost a task a tick —
it can never make it say "go ahead" on something the authoritative path would
refuse, and it never creates, cancels or projects anything. Every creation still
goes through the opt-in path, which resolves the organization from the trusted
context and fails closed. That is why the guard can be cheap: its failure mode is
deferral, not authorization.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app.gateway.task_runtime_context import TaskRuntimeContextError, idempotency_key_for
from app.gateway.task_runtime_linkage import RuntimeRunLinkageError, read_stored_linkage
from app.gateway.task_runtime_optin import runtime_unattended_enabled

__all__ = [
    "PickupAction",
    "PickupDecision",
    "RuntimeScheduleError",
    "decide_runtime_pickup",
]

_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})
_UNATTENDED_RUN_MODE = "unattended"

PickupAction = Literal[
    "disabled",
    "not_unattended",
    "task_settled",
    "schedule",
    "already_scheduled",
    "attempt_changed",
    "linkage_unusable",
]

# The actions that mean "this tick must not advance the task". A settled task is
# deliberately absent: the pipeline owns retry / rerun for it, so the tick must
# still hand it over.
_SKIP_ACTIONS = frozenset({"already_scheduled", "linkage_unusable"})


class RuntimeScheduleError(RuntimeError):
    """Raised when no task row was supplied to the pickup decision."""


@dataclass(frozen=True)
class PickupDecision:
    """Whether a tick should advance a task, and why."""

    action: PickupAction
    reason: str
    task_id: str | None = None
    runtime_run_id: str | None = None

    @property
    def already_scheduled(self) -> bool:
        return self.action == "already_scheduled"

    @property
    def should_skip(self) -> bool:
        """Whether the tick must leave the task alone this time."""
        return self.action in _SKIP_ACTIONS


def decide_runtime_pickup(task: Mapping[str, Any] | None) -> PickupDecision:
    """Decide whether a queue tick should advance ``task``.

    Answers from the persisted row only: no Runtime call, no identity resolution
    and no in-process state, so consecutive ticks and a restart produce the same
    answer.
    """
    if not isinstance(task, Mapping):
        raise RuntimeScheduleError("no server-loaded task row supplied")

    task_id = str(task.get("id") or "").strip() or None

    if not runtime_unattended_enabled():
        # Nothing is driven by the Runtime, so scheduling is byte-identically the
        # legacy behaviour and the tick must not change what it picks.
        return PickupDecision("disabled", "runtime_disabled", task_id=task_id)

    if str(task.get("run_mode") or "").strip().lower() != _UNATTENDED_RUN_MODE:
        return PickupDecision("not_unattended", "run_mode_is_not_unattended", task_id=task_id)

    if str(task.get("status") or "").strip().lower() in _TERMINAL_TASK_STATUSES:
        # Reported, not skipped: the pipeline's own retry / rerun handling runs
        # when the tick advances a settled task.
        return PickupDecision("task_settled", "task_already_terminal", task_id=task_id)

    try:
        linkage = read_stored_linkage(task)
    except RuntimeRunLinkageError:
        # A linkage nobody can decode cannot be compared, and must certainly not
        # be treated as "no run yet" — that is how a second run gets created.
        return PickupDecision("linkage_unusable", "stored_linkage_is_unusable", task_id=task_id)

    if linkage is None:
        return PickupDecision("schedule", "no_runtime_linkage", task_id=task_id)

    try:
        current_key = idempotency_key_for(
            linkage.org_scope_key, linkage.task_id, task.get("unattended_attempts")
        )
    except TaskRuntimeContextError:  # pragma: no cover - a decoded linkage always has both
        return PickupDecision("linkage_unusable", "stored_linkage_is_unusable", task_id=task_id)

    if linkage.idempotency_key == current_key:
        return PickupDecision(
            "already_scheduled",
            "attempt_already_handed_to_runtime",
            task_id=task_id,
            runtime_run_id=linkage.runtime_run_id,
        )

    # The linkage belongs to a different attempt: this is the retry path, whose
    # re-linking is owned by the retry linkage card. The tick proceeds exactly as
    # it did before, and the creation path's own guard decides.
    return PickupDecision(
        "attempt_changed",
        "linkage_belongs_to_another_attempt",
        task_id=task_id,
        runtime_run_id=linkage.runtime_run_id,
    )
