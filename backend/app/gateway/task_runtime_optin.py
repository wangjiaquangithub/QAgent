"""Server-config-controlled Runtime opt-in for unattended Task Center tasks.

AG-G2-AUTO-003-B01.

This module is the only place that decides whether an unattended task should be
driven through the existing QAgent Runtime. It is deliberately inert by default:
with the switch off nothing here reads, creates or cancels a Runtime run, and the
existing LangGraph / legacy execution path stays exactly as it was.

Trusted inputs only
-------------------
A background queue tick has no HTTP request, so it cannot use
``resolve_request_authz``. The organization and subject therefore come from the
task's own persisted ACL columns through the existing accessor
``evoflow.persistence.task_repositories.get_root_task_owner_scope``. Nothing is
taken from a client payload.

Routing rules
-------------
- switch off -> legacy, no Runtime interaction at all;
- an explicit per-task ``execution_mode`` opts out (``direct_langgraph`` and the
  values the automation runner already treats as direct) -> legacy, even with the
  switch on; an unrecognised value does not opt in either, so a typo cannot change
  which engine runs the work (AG-G2-AUTO-022);
- trusted identity incomplete, or the task is not authorized for execution ->
  legacy with a reason code (the preconditions simply do not hold yet);
- otherwise the run is established through the Runtime **public** contract only:
  an existing linkage is reused (read), otherwise exactly one run is created, and
  the linkage is persisted into the task's existing extras slot;
- the trusted organization is always forwarded to the Runtime on the creation
  call, so the run belongs to the owning organization and never falls back to
  the Runtime's own default org (AG-G2-AUTO-008);
- a Runtime call failure is raised, never converted into a second legacy
  execution — that is what would duplicate side effects.

Idempotence of a repeated trigger (AG-G2-AUTO-020)
-------------------------------------------------
A manual run-now can be delivered twice: a double click, a retried HTTP call, a
second scheduler tick. No second run may be created for the same trigger, and the
protection must not live in this process:

- the **persisted linkage** already pins one trigger (org scope + task +
  attempt) to exactly one run, and it survives a restart because it travels in
  the task row's existing extras slot;
- the **Runtime's own idempotency key**, derived from that same trigger, is
  unique per ``(org_id, idempotency_key)`` in the Runtime's own store, so even
  two callers that race past the read both end up with the same run. The
  uniqueness is enforced by the Runtime, not by this module;
- consequently nothing here needs — or uses — an in-process lock as its
  guarantee. A second call is answered from persisted state, not from memory.

A **terminal task** is not "an already running task". Once a task has settled,
what happens next — a retry, a requeue, a failure report — is the existing
business semantics' decision, so this branch steps aside and lets it decide
instead of answering "a run is already in place" and swallowing the retry.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from app.gateway.task_runtime_context import (
    TaskRuntimeContext,
    TaskRuntimeContextError,
    build_task_runtime_context,
)
from app.gateway.task_runtime_linkage import (
    RuntimeRunLinkageError,
    link_runtime_run,
    read_linked_runtime_run,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ExecutionModeDecision",
    "RuntimeOptInDecision",
    "RuntimeOptInResult",
    "assert_no_runtime_side_effects",
    "classify_execution_mode",
    "decide_runtime_opt_in",
    "default_runtime_contract",
    "establish_runtime_run",
    "resolve_server_task_runtime_identity",
    "runtime_unattended_enabled",
]

# Server-side switch. Default off; no client input can influence it.
_ENV_SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

RouteDecision = Literal["runtime", "legacy"]

# A settled task. The existing pipeline owns what happens to it (retry, requeue,
# report), so the Runtime branch must not claim it.
_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})

# A settled run. The Runtime's own vocabulary, matching the projection's.
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "timed_out"})

# The explicit per-task execution mode. These are exactly the values the existing
# automation runner already recognises (``_automation_via_task_center``), kept in
# the same normalised form so an operator's explicit choice cannot mean one thing
# to the Task Center routing and another to the Runtime (AG-G2-AUTO-022).
_EXPLICIT_OPT_IN_MODES = ("task_center", "plan", "unattended", "task_center_plan")
_EXPLICIT_OPT_OUT_MODES = ("direct", "direct_langgraph", "langgraph", "execute", "runs_wait")

ExecutionModeClass = Literal["opt_in", "opt_out", "default", "invalid"]


class RuntimeRunContract(Protocol):
    """The subset of the Runtime public contract used here.

    Structural on purpose: the caller passes the real service, this module
    imports no Runtime internals, and tests can pass a recorder.

    ``org_id`` mirrors the keyword the Runtime service's ``create_run`` already
    accepts. Declaring it here means the trusted Task Center organization is
    always passed on the creation call and can never fall back to the Runtime's
    own default (AG-G2-AUTO-008).
    """

    async def create_run(
        self,
        *,
        org_id: str,
        task_id: str,
        input_payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]: ...

    async def get_run_status(self, run_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class RuntimeOptInResult:
    """What the opt-in branch did."""

    decision: RouteDecision
    reason: str
    context: TaskRuntimeContext | None = None
    run_id: str | None = None
    run_status: dict[str, Any] | None = None
    action: str | None = None
    updated_task: dict[str, Any] | None = None

    @property
    def use_runtime(self) -> bool:
        return self.decision == "runtime"


def runtime_unattended_enabled() -> bool:
    """Whether the server has opted unattended tasks into the Runtime."""
    return (os.getenv(_ENV_SWITCH) or "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class ExecutionModeDecision:
    """What a task's explicit execution mode says, if it says anything."""

    kind: ExecutionModeClass
    value: str = ""

    @property
    def was_explicit(self) -> bool:
        return self.kind in {"opt_in", "opt_out"}


@dataclass(frozen=True)
class RuntimeOptInDecision:
    """Whether the Runtime drives this task, and the reason a reader can act on."""

    use_runtime: bool
    reason: str
    execution_mode: str = ""


def classify_execution_mode(task: Mapping[str, Any] | None) -> ExecutionModeDecision:
    """Classify the explicit execution mode carried by a task row.

    Recognises the same opt-in and opt-out values the existing automation runner
    does, normalised the same way. An unrecognised value is reported as
    ``invalid`` rather than silently treated as absent: a typo must not change
    which engine runs the work, and it must be visible rather than guessed at.
    """
    if not isinstance(task, Mapping):
        return ExecutionModeDecision("default")
    raw = task.get("execution_mode") or task.get("prompt_execution_mode") or ""
    value = str(raw).strip().lower().replace("-", "_")
    if not value:
        return ExecutionModeDecision("default")
    if value in _EXPLICIT_OPT_IN_MODES:
        return ExecutionModeDecision("opt_in", value)
    if value in _EXPLICIT_OPT_OUT_MODES:
        return ExecutionModeDecision("opt_out", value)
    return ExecutionModeDecision("invalid", value)


def decide_runtime_opt_in(task: Mapping[str, Any] | None) -> RuntimeOptInDecision:
    """The single decision for whether the Runtime drives this task.

    Rules, in order:

    1. the server switch is the master gate — off means never the Runtime, so an
       unconfigured deployment keeps its existing behaviour byte for byte;
    2. an explicit per-task opt-out wins — a task an operator marked as direct
       LangGraph is not run by the Runtime even when the switch is on, so the
       Runtime path can never quietly override an explicit instruction;
    3. an unrecognised value does **not** opt in. It falls back to the existing
       routing and says so, rather than turning a typo into a change of engine;
    4. otherwise the existing semantics apply: absent means the server switch
       decides, exactly as before. Nothing is flipped on by default.
    """
    if not runtime_unattended_enabled():
        return RuntimeOptInDecision(False, "runtime_disabled")

    mode = classify_execution_mode(task)
    if mode.kind == "opt_out":
        return RuntimeOptInDecision(False, "execution_mode_opt_out", mode.value)
    if mode.kind == "invalid":
        return RuntimeOptInDecision(False, "invalid_execution_mode", mode.value)
    if mode.kind == "opt_in":
        return RuntimeOptInDecision(True, "execution_mode_opt_in", mode.value)
    return RuntimeOptInDecision(True, "runtime_enabled_by_server")


_contract_cache: RuntimeRunContract | None = None
_contract_resolved = False


def default_runtime_contract() -> RuntimeRunContract | None:
    """The existing Runtime public service, or ``None`` when it is unavailable.

    Uses the Runtime package's own public constructors (``RuntimeService`` as the
    implementation of ``RuntimeContract``, plus ``RuntimeRepository.from_config``)
    exactly as the Runtime router does. No private internals are touched.
    """
    global _contract_cache, _contract_resolved
    if _contract_resolved:
        return _contract_cache
    _contract_resolved = True
    try:
        from app.qagent_runtime.repository import RuntimeRepository
        from app.qagent_runtime.service import RuntimeService

        _contract_cache = RuntimeService(RuntimeRepository.from_config())
    except Exception:
        logger.warning("runtime service unavailable for unattended opt-in", exc_info=True)
        _contract_cache = None
    return _contract_cache


def resolve_server_task_runtime_identity(task_id: str) -> dict[str, Any] | None:
    """Build a trusted identity mapping from the task's persisted ACL columns.

    Returns ``None`` when the server has no trustworthy identity for the task,
    which the caller must treat as "preconditions not met" rather than guessing.
    """
    from evoflow.persistence.task_repositories import get_root_task_owner_scope

    org_id, owner_scope_id, created_by = get_root_task_owner_scope(task_id)
    org_id = str(org_id or "").strip()
    owner_scope_id = str(owner_scope_id or "").strip()
    created_by = str(created_by or "").strip()
    if not org_id or not owner_scope_id or not created_by:
        return None
    return {
        "org_id": org_id,
        "scope_id": owner_scope_id,
        "principal": {"principal_id": created_by, "principal_type": "internal"},
    }


def _legacy(
    reason: str,
    context: TaskRuntimeContext | None = None,
    *,
    action: str | None = None,
) -> RuntimeOptInResult:
    return RuntimeOptInResult(decision="legacy", reason=reason, context=context, action=action)


async def establish_runtime_run(
    task: Mapping[str, Any] | None,
    *,
    identity: Mapping[str, Any] | None,
    authorized: bool,
    contract: RuntimeRunContract | None,
) -> RuntimeOptInResult:
    """Decide the route and, when opted in, establish/reuse exactly one run.

    A repeated trigger never creates a second run: an existing linkage is reused,
    and the read is answered from the persisted task row rather than from
    anything this process remembers (AG-G2-AUTO-020).
    """
    decision = decide_runtime_opt_in(task)
    if not decision.use_runtime:
        return _legacy(decision.reason)

    if contract is None:
        return _legacy("runtime_contract_unavailable")

    if identity is None:
        return _legacy("trusted_identity_unavailable")

    try:
        context = build_task_runtime_context(task=task, authz=identity, authorized=authorized)
    except TaskRuntimeContextError as exc:
        # Safety preconditions do not hold yet: keep the existing behaviour.
        logger.info("unattended runtime opt-in skipped: %s", exc)
        return _legacy("preconditions_not_met")

    assert isinstance(task, Mapping)

    # A settled task is not an already-running one. Step aside so the existing
    # retry / rerun / report semantics decide, instead of answering "a run is
    # already in place" and swallowing them. Nothing is created or read here.
    if str(task.get("status") or "").strip().lower() in _TERMINAL_TASK_STATUSES:
        return _legacy("task_already_terminal", action="deferred_to_existing_semantics")

    try:
        existing = read_linked_runtime_run(task, context=context)
    except RuntimeRunLinkageError:
        # An unsafe or foreign linkage must never be adopted or overwritten.
        raise

    if existing is not None:
        # Reuse, never re-create: this is what makes a double click, a retried
        # HTTP call and a second tick converge on the same run. The answer comes
        # from the persisted linkage, so a restart answers identically.
        status = await contract.get_run_status(existing.runtime_run_id)
        reported = ""
        if isinstance(status, Mapping):
            reported = str(status.get("status") or "").strip().lower()
        if reported in _TERMINAL_RUN_STATUSES:
            # The run is over; there is nothing to reuse, but a second run is
            # still not this branch's to create.
            return RuntimeOptInResult(
                decision="runtime",
                reason="linked_run_terminal",
                context=context,
                run_id=existing.runtime_run_id,
                run_status=status,
                action="reused_terminal_run",
            )
        # An unreadable status must not be read as "nothing is running" either:
        # that would create a duplicate run, and falling through would duplicate
        # the side effects in a legacy execution. Reuse, and say it is unverified.
        return RuntimeOptInResult(
            decision="runtime",
            reason="reused",
            context=context,
            run_id=existing.runtime_run_id,
            run_status=status,
            action="reused" if reported else "reused_unverified",
        )

    # First trigger for this attempt. A failure here propagates: it must never be
    # swallowed into a legacy execution that would duplicate the side effects.
    # The trusted organization is passed explicitly so the run can never be
    # attributed to the Runtime's own default org.
    # The idempotency key makes even two callers that raced past the read above
    # converge on the same run: the Runtime enforces it per (org, key) in its own
    # store, so no in-process lock is load-bearing here.
    created = await contract.create_run(
        org_id=context.org_id, **context.to_request_kwargs()
    )
    run_id = str(created.get("run_id") or "").strip()
    if not run_id:
        raise RuntimeError("runtime create_run returned no run_id")

    # When the Runtime echoes the owning organization, it must be the trusted
    # one. A mismatching echo means the run we are about to link does not belong
    # to this task's organization, so refuse rather than link it (AG-G2-AUTO-010).
    echoed_org = str(created.get("org_id") or "").strip()
    if echoed_org and echoed_org != context.org_id:
        raise RuntimeError(
            "runtime created a run for a different organization; refusing to link it"
        )

    updated_task, outcome = link_runtime_run(task, context=context, runtime_run_id=run_id)
    return RuntimeOptInResult(
        decision="runtime",
        reason="created",
        context=context,
        run_id=run_id,
        run_status=created,
        action=outcome.action,
        updated_task=updated_task,
    )


def assert_no_runtime_side_effects(result: RuntimeOptInResult) -> None:
    """Test/debug helper: a legacy decision must carry no Runtime artefacts."""
    if result.decision == "legacy":
        assert result.run_id is None and result.run_status is None and result.updated_task is None
