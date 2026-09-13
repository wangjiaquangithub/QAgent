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
- trusted identity incomplete, or the task is not authorized for execution ->
  legacy with a reason code (the preconditions simply do not hold yet);
- otherwise the run is established through the Runtime **public** contract only:
  an existing linkage is reused (read), otherwise exactly one run is created, and
  the linkage is persisted into the task's existing extras slot;
- a Runtime call failure is raised, never converted into a second legacy
  execution — that is what would duplicate side effects.
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
    read_runtime_run_linkage,
)

logger = logging.getLogger(__name__)

__all__ = [
    "RuntimeOptInResult",
    "assert_no_runtime_side_effects",
    "default_runtime_contract",
    "establish_runtime_run",
    "resolve_server_task_runtime_identity",
    "runtime_unattended_enabled",
]

# Server-side switch. Default off; no client input can influence it.
_ENV_SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

RouteDecision = Literal["runtime", "legacy"]


class RuntimeRunContract(Protocol):
    """The subset of the Runtime public contract used here.

    Structural on purpose: the caller passes the real service, this module
    imports no Runtime internals, and tests can pass a recorder.
    """

    async def create_run(
        self, *, task_id: str, input_payload: dict[str, Any], idempotency_key: str | None = None
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


def _legacy(reason: str, context: TaskRuntimeContext | None = None) -> RuntimeOptInResult:
    return RuntimeOptInResult(decision="legacy", reason=reason, context=context)


async def establish_runtime_run(
    task: Mapping[str, Any] | None,
    *,
    identity: Mapping[str, Any] | None,
    authorized: bool,
    contract: RuntimeRunContract | None,
) -> RuntimeOptInResult:
    """Decide the route and, when opted in, establish/reuse exactly one run."""
    if not runtime_unattended_enabled():
        return _legacy("runtime_disabled")

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

    try:
        existing = read_runtime_run_linkage(task, org_scope_key=context.org_scope_key)
    except RuntimeRunLinkageError:
        # An unsafe or foreign linkage must never be adopted or overwritten.
        raise

    if existing is not None:
        status = await contract.get_run_status(existing.runtime_run_id)
        return RuntimeOptInResult(
            decision="runtime",
            reason="reused",
            context=context,
            run_id=existing.runtime_run_id,
            run_status=status,
            action="reused",
        )

    # First trigger for this attempt. A failure here propagates: it must never be
    # swallowed into a legacy execution that would duplicate the side effects.
    created = await contract.create_run(**context.to_request_kwargs())
    run_id = str(created.get("run_id") or "").strip()
    if not run_id:
        raise RuntimeError("runtime create_run returned no run_id")

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
