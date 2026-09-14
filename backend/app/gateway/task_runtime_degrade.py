"""Fail-safe display for a broken Task Center <-> Runtime linkage.

AG-G2-AUTO-026.

The problem
-----------
A stored linkage is a pointer, and a pointer can go bad in ways nobody
controls:

- the Runtime run it names was **deleted**, or lives in another store that has
  since been reset, so reading its status raises rather than returning;
- the linkage itself is **malformed** — a half-written extras slot, a field
  dropped by an older writer, a value a client got into the row;
- it belongs to **another organization** or another task, because the extras
  slot is client-writable and nothing about a string makes it trustworthy;
- the task and the run disagree about a **terminal** outcome — the task was
  cancelled locally while the run reports it completed, or the reverse.

None of those is a reason for a Task Center read to fail, and none of them is a
reason to tell the user something that did not happen. So this module is the one
place that answers "what may be shown when the link is broken", and it answers it
with the vocabulary the rest of the domain already uses.

What it deliberately does not do
--------------------------------
- **It never creates, starts, resumes or cancels a run.** There is no such call
  anywhere in this module, so a broken link can never be "repaired" by silently
  standing up a replacement run and billing the work twice.
- **It never reports success.** A broken link degrades to "no Runtime view
  available" — never to a fabricated ``completed``.
- **It never invents a TaskStatus or a new state machine.** The degraded answer
  is an existing shape: the task is still returned from persisted state, and the
  failure is reported through the existing ``runtime_unavailable`` reconcile
  action plus a fixed reason code. A reader that does not understand the reason
  still understands the action.
- **It never reveals a foreign run.** An unusable linkage is reported with **no**
  run id at all, so a broken link is never a way to read another organization's
  run identity. Refusing the link *and* keeping the task readable are the two
  halves of the same requirement, not a contradiction.

Retryability is a separate question and is answered separately
--------------------------------------------------------------
"Unavailable" is worth another attempt; "this run is gone" and "this link is
malformed" are not — they need a person, not a retry. :func:`is_retryable_reason`
states which is which instead of leaving a caller to guess from prose.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app.gateway.task_runtime_context import TaskRuntimeContext
from app.gateway.task_runtime_linkage import (
    RuntimeRunLinkageError,
    read_linked_runtime_run,
    read_stored_linkage,
)
from app.gateway.task_runtime_optin import runtime_unattended_enabled
from app.gateway.task_runtime_projection import HISTORY_FIELD
from app.gateway.task_runtime_reconcile import (
    ReconcileOutcome,
    RuntimeReconcileContract,
    reconcile_linked_task_runtime,
)
from app.gateway.task_runtime_recovery import TaskStreamView, read_task_stream_view

__all__ = [
    "DegradedTaskView",
    "RuntimeDegradeError",
    "RuntimeLinkHealth",
    "assess_runtime_link",
    "is_retryable_reason",
    "read_degraded_task_stream_view",
    "reconcile_task_runtime_safely",
    "safe_runtime_read_reason",
]

LinkState = Literal["linked", "unlinked", "unusable"]

# Reasons a degraded answer can carry. Fixed strings, never a storage message, so
# a tampered extras slot cannot become a channel: whatever it contains is
# classified, not echoed.
REASON_LINKAGE_MALFORMED = "linkage_malformed"
REASON_LINKAGE_FOREIGN_ORG = "linkage_foreign_org"
REASON_LINKAGE_OTHER_TASK = "linkage_other_task"
REASON_LINKAGE_UNUSABLE = "linkage_unusable"
REASON_NO_LINKAGE = "no_runtime_linkage"
REASON_RUN_MISSING = "run_missing"
REASON_RUNTIME_UNAVAILABLE = "runtime_unavailable"

# Worth another attempt later. A missing run and a malformed link are not: a
# retry reproduces them exactly.
_RETRYABLE_REASONS = frozenset({REASON_RUNTIME_UNAVAILABLE})


class RuntimeDegradeError(RuntimeError):
    """Raised for a caller error, never for a broken link.

    A broken link is data, and is reported as a degraded answer. This is only for
    the cases where there is nothing to degrade *from*: no task row, no trusted
    context, or a context that belongs to a different task.
    """


@dataclass(frozen=True)
class RuntimeLinkHealth:
    """What can be said about a task's stored Runtime linkage."""

    state: LinkState
    reason: str
    run_id: str | None = None

    @property
    def usable(self) -> bool:
        """Whether the linkage may be used to reach the Runtime at all."""
        return self.state != "unusable"

    @property
    def displayable_run_id(self) -> str | None:
        """The run id a reader may be shown, or ``None`` when it must stay hidden.

        An unusable linkage answers ``None`` even when its stored run id is
        syntactically fine: a run this caller's organization does not own must
        not become visible through a refusal path.
        """
        return self.run_id if self.state == "linked" else None


@dataclass(frozen=True)
class DegradedTaskView:
    """A stream view that can always be produced, plus why it looks like it does."""

    health: RuntimeLinkHealth
    view: TaskStreamView


def _task_id_or_raise(task: Mapping[str, Any] | None, *, context: Any) -> str:
    if not isinstance(task, Mapping):
        raise RuntimeDegradeError("no server-loaded task row supplied")
    if not isinstance(context, TaskRuntimeContext):
        raise RuntimeDegradeError("no trusted task runtime context supplied")
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        raise RuntimeDegradeError("task row has no id")
    if task_id != context.task_id:
        raise RuntimeDegradeError("task runtime context does not belong to this task")
    return task_id


def _classify_unusable_linkage(task: Mapping[str, Any], *, context: TaskRuntimeContext) -> str:
    """Name *why* the shared gate refused, from the stored value alone.

    The gate reports every storage-level refusal the same way; re-reading the
    stored linkage without the organization check is what separates a malformed
    value from a foreign one, without parsing anybody's error message.
    """
    try:
        stored = read_stored_linkage(task)
    except RuntimeRunLinkageError:
        return REASON_LINKAGE_MALFORMED
    if stored is None:
        # Nothing there any more: the row changed underneath the read. Still safe.
        return REASON_LINKAGE_UNUSABLE
    if stored.org_scope_key != context.org_scope_key:
        return REASON_LINKAGE_FOREIGN_ORG
    if stored.task_id != context.task_id:
        return REASON_LINKAGE_OTHER_TASK
    return REASON_LINKAGE_UNUSABLE


def assess_runtime_link(
    task: Mapping[str, Any] | None,
    *,
    context: TaskRuntimeContext,
) -> RuntimeLinkHealth:
    """Classify a task's stored linkage without raising for a broken one.

    Pure and persisted-state only: no Runtime call and no write. A refusal from
    the shared organization gate is turned into an ``unusable`` health instead of
    an exception, so a Task Center read survives it.
    """
    _task_id_or_raise(task, context=context)
    assert isinstance(task, Mapping)

    try:
        linkage = read_linked_runtime_run(task, context=context)
    except RuntimeRunLinkageError:
        return RuntimeLinkHealth(
            state="unusable", reason=_classify_unusable_linkage(task, context=context)
        )
    if linkage is None:
        return RuntimeLinkHealth(state="unlinked", reason=REASON_NO_LINKAGE)
    return RuntimeLinkHealth(state="linked", reason="linked", run_id=linkage.runtime_run_id)


def _stored_history(task: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    raw = task.get(HISTORY_FIELD)
    if not isinstance(raw, list):
        return ()
    return tuple(entry for entry in raw if isinstance(entry, Mapping))


def _delinked_view(
    task: Mapping[str, Any], *, context: TaskRuntimeContext
) -> TaskStreamView:
    """The view of a task whose link cannot be used: the task, minus the link.

    The history the existing endpoint already stores is returned unchanged, so
    nothing the user legitimately saw is hidden by a broken pointer. No run id and
    no watermark are reported, which is what keeps this honest rather than
    decorated.
    """
    return TaskStreamView(
        task_id=str(task.get("id") or "").strip(),
        org_scope_key=context.org_scope_key,
        status=str(task.get("status") or "").strip().lower(),
        runtime_run_id=None,
        history=_stored_history(task),
        watermark=None,
    )


def read_degraded_task_stream_view(
    task: Mapping[str, Any] | None,
    *,
    context: TaskRuntimeContext,
) -> DegradedTaskView:
    """The stream view a reconnect should be given, broken link or not.

    A usable link is read through the normal recovery view, so the healthy path is
    unchanged. An unusable one yields the same task without the link, and says so
    through ``health`` — the caller decides how to present it, and no caller has
    to catch an exception to keep a task visible.
    """
    _task_id_or_raise(task, context=context)
    assert isinstance(task, Mapping)

    health = assess_runtime_link(task, context=context)
    if health.state == "linked":
        return DegradedTaskView(health=health, view=read_task_stream_view(task, context=context))
    return DegradedTaskView(health=health, view=_delinked_view(task, context=context))


def safe_runtime_read_reason(exc: BaseException) -> str:
    """Map a Runtime read failure onto the existing degraded vocabulary.

    A ``KeyError`` is the Runtime's own answer for a run that does not exist —
    deleted, or in a store that was reset — so it says ``run_missing`` rather than
    being flattened into "unavailable". Everything else is reported as
    unavailable, which is the existing word for "not readable right now".
    """
    if isinstance(exc, KeyError):
        return REASON_RUN_MISSING
    return REASON_RUNTIME_UNAVAILABLE


def is_retryable_reason(reason: str) -> bool:
    """Whether a degraded answer is worth attempting again later."""
    return str(reason or "").strip() in _RETRYABLE_REASONS


async def reconcile_task_runtime_safely(
    task: Mapping[str, Any] | None,
    *,
    context: TaskRuntimeContext,
    contract: RuntimeReconcileContract | None,
) -> ReconcileOutcome:
    """Reconcile a linked task, degrading instead of raising when the link is bad.

    Wraps the existing read-only reconciliation, which is still the only thing
    that reads the Runtime here, so this cannot create or cancel anything. The
    server opt-in switch stays the master gate: with it off this is as inert as
    the reconciler it wraps, and a deployment that never opted in keeps its
    behaviour unchanged.

    Two failures are absorbed:

    - the linkage is unusable — refused up front, without spending a Runtime call
      on a run that must not be read;
    - the Runtime read fails — a deleted run (``KeyError``) or an unreadable store.
      The task is left exactly as it is, which is the same fail-safe answer the
      reconciler already gives for an unusable status.

    Both are reported through the existing ``runtime_unavailable`` action, so no
    consumer needs to learn a new action to stay correct.
    """
    if not runtime_unattended_enabled():
        return ReconcileOutcome(action="disabled", reason="runtime_disabled")

    health = assess_runtime_link(task, context=context)
    if health.state == "unusable":
        return ReconcileOutcome(
            action="runtime_unavailable", reason=health.reason, runtime_run_id=None
        )

    try:
        return await reconcile_linked_task_runtime(task, context=context, contract=contract)
    except RuntimeRunLinkageError:
        assert isinstance(task, Mapping)
        return ReconcileOutcome(
            action="runtime_unavailable",
            reason=_classify_unusable_linkage(task, context=context),
            runtime_run_id=None,
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable Runtime must not crash a task read
        # CancelledError is a BaseException and is deliberately not absorbed here.
        return ReconcileOutcome(
            action="runtime_unavailable",
            reason=safe_runtime_read_reason(exc),
            runtime_run_id=health.run_id,
        )
