"""Chat-domain dispatch of a live run through the QAgent Runtime.

This is the minimal, reversible seam between the existing chat entrypoints and
:class:`app.qagent_runtime.service.RuntimeService`.

Contract:

* **Opt-in.** Dispatch only happens when ``QAGENT_CHAT_RUNTIME_OPT_IN`` is set
  to a truthy value. When unset (the default) :func:`dispatch_chat_run` reports
  ``enabled=False`` and performs no work, so the existing LangGraph path is
  byte-for-byte unaffected.
* **Reuse, do not re-implement.** Run creation, approval semantics, event
  sequencing and AgentScope execution all stay inside the Runtime. This module
  only translates a Chat request into the Runtime call and reports what it got
  back. It deliberately imports the Runtime lazily so importing the Chat gateway
  never requires Runtime (or its PostgreSQL) to be present.
* **No new user-visible API.** The seam is server-side; the frontend keeps using
  the same endpoints it already uses.

Nothing here modifies ``backend/app/qagent_runtime/**``, migrations, the router
registry, background startup, or the global Gateway.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from app.gateway.chat_runtime_identity import build_chat_run_identity

logger = logging.getLogger(__name__)

#: Truthy values accepted for the server-side opt-in switch.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

CHAT_RUNTIME_OPT_IN_ENV = "QAGENT_CHAT_RUNTIME_OPT_IN"


def chat_runtime_opt_in_enabled() -> bool:
    """Whether chat runs should be dispatched through the QAgent Runtime.

    Server-side configuration only — deliberately not derivable from any
    request field, so a client cannot switch execution backends.
    """
    return str(os.getenv(CHAT_RUNTIME_OPT_IN_ENV, "")).strip().lower() in _TRUTHY


@dataclass(frozen=True)
class ChatRunDispatch:
    """Outcome of attempting to dispatch one chat run through the Runtime."""

    enabled: bool
    dispatched: bool
    run_id: str | None = None
    status: str | None = None
    org_id: str | None = None
    reason: str | None = None


def _get_runtime_service(request: Any) -> Any:
    """Resolve the shared Runtime service from app state.

    Mirrors ``app.qagent_runtime.router._service`` so the Chat seam and the
    Runtime HTTP surface share one service instance instead of racing to build
    separate ones.
    """
    service = getattr(request.app.state, "qagent_runtime_service", None)
    if service is not None:
        return service

    from app.qagent_runtime.repository import RuntimeRepository
    from app.qagent_runtime.service import RuntimeService

    service = RuntimeService(RuntimeRepository.from_config())
    request.app.state.qagent_runtime_service = service
    return service


async def dispatch_chat_run(
    request: Any,
    *,
    identity: dict[str, Any] | None,
    session_key: str,
    message_id: str | None = None,
    run_id: str | None = None,
    principal_id: str | None = None,
    input_payload: dict[str, Any] | None = None,
) -> ChatRunDispatch:
    """Create (or reuse) a Runtime run for a chat request, when opted in.

    Returns a :class:`ChatRunDispatch` describing the outcome and never raises
    for Runtime unavailability: a disabled or failed dispatch is reported so the
    caller can fall back to the existing chat path instead of failing the user's
    message.
    """
    if not chat_runtime_opt_in_enabled():
        return ChatRunDispatch(enabled=False, dispatched=False, reason="opt_in_disabled")

    mapping = build_chat_run_identity(
        identity=identity,
        session_key=session_key,
        message_id=message_id,
        run_id=run_id,
        principal_id=principal_id,
        extra_payload=input_payload,
    )

    try:
        service = _get_runtime_service(request)
        run = await service.create_run(**mapping.as_create_run_kwargs())
    except Exception:
        logger.exception("chat runtime dispatch failed session=%s", mapping.session_key)
        return ChatRunDispatch(
            enabled=True,
            dispatched=False,
            org_id=mapping.org_id,
            reason="runtime_unavailable",
        )

    return ChatRunDispatch(
        enabled=True,
        dispatched=True,
        run_id=str(run.get("run_id") or "") or None,
        status=str(run.get("status") or "") or None,
        org_id=mapping.org_id,
    )
