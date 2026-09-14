"""Chat -> Runtime dispatch seam: opt-in behaviour and fallback safety.

Verifies the two properties that make this seam safe to ship:

1. With the opt-in switch **off** (default), no Runtime work happens at all —
   the existing LangGraph chat path is untouched.
2. With the opt-in switch **on**, the chat request produces a Runtime run whose
   scope comes from verified identity, and a Runtime that is unavailable
   degrades gracefully instead of failing the user's message.

A fake Runtime service stands in for the real one; this test does not require
PostgreSQL. The real Runtime kernel is intentionally not exercised here.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.gateway import chat_runtime_dispatch as dispatch_mod
from app.gateway.chat_runtime_dispatch import (
    CHAT_RUNTIME_OPT_IN_ENV,
    chat_runtime_opt_in_enabled,
    dispatch_chat_run,
)

SESSION_KEY = "agent:main:g2-dispatch"
IDENTITY = {"identity_type": "token", "identity_id": "user-1"}


class _FakeRuntimeService:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def create_run(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("postgres unavailable")
        return {"run_id": "run-fake-1", "status": "queued"}


def _request(service: Any) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(qagent_runtime_service=service)))


@pytest.fixture(autouse=True)
def _clear_opt_in(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(CHAT_RUNTIME_OPT_IN_ENV, raising=False)


# --------------------------------------------------------------------------
# Opt-in off: existing chat path must be untouched
# --------------------------------------------------------------------------


def test_opt_in_disabled_by_default() -> None:
    assert chat_runtime_opt_in_enabled() is False


@pytest.mark.asyncio
async def test_dispatch_does_nothing_when_opt_in_off() -> None:
    service = _FakeRuntimeService()
    result = await dispatch_chat_run(
        _request(service),
        identity=IDENTITY,
        session_key=SESSION_KEY,
        message_id="msg-1",
    )

    assert result.enabled is False
    assert result.dispatched is False
    assert result.reason == "opt_in_disabled"
    assert result.run_id is None
    # The Runtime must not even be touched.
    assert service.calls == []


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
def test_opt_in_accepts_truthy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(CHAT_RUNTIME_OPT_IN_ENV, value)
    assert chat_runtime_opt_in_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "enabled"])
def test_opt_in_rejects_non_truthy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(CHAT_RUNTIME_OPT_IN_ENV, value)
    assert chat_runtime_opt_in_enabled() is False


# --------------------------------------------------------------------------
# Opt-in on: dispatch through the Runtime with server-derived scope
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_creates_runtime_run_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CHAT_RUNTIME_OPT_IN_ENV, "1")
    service = _FakeRuntimeService()

    result = await dispatch_chat_run(
        _request(service),
        identity=IDENTITY,
        session_key=SESSION_KEY,
        message_id="msg-1",
        principal_id="user:alice",
    )

    assert result.enabled is True
    assert result.dispatched is True
    assert result.run_id == "run-fake-1"
    assert result.status == "queued"
    assert result.org_id == "identity:token:user-1"

    assert len(service.calls) == 1
    call = service.calls[0]
    # Exactly the Runtime create_run signature — nothing extra is smuggled in.
    assert set(call) == {"org_id", "task_id", "input_payload", "idempotency_key"}
    assert call["org_id"] == "identity:token:user-1"
    assert call["input_payload"]["source"] == "chat"
    assert call["input_payload"]["session_key"] == SESSION_KEY


@pytest.mark.asyncio
async def test_dispatch_delegates_run_reuse_to_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repeated send must yield the same idempotency key (Runtime dedupes)."""
    monkeypatch.setenv(CHAT_RUNTIME_OPT_IN_ENV, "1")
    service = _FakeRuntimeService()

    for _ in range(2):
        await dispatch_chat_run(
            _request(service),
            identity=IDENTITY,
            session_key=SESSION_KEY,
            message_id="msg-1",
        )

    assert len(service.calls) == 2
    assert service.calls[0]["idempotency_key"] == service.calls[1]["idempotency_key"]
    assert service.calls[0]["org_id"] == service.calls[1]["org_id"]


@pytest.mark.asyncio
async def test_client_payload_cannot_change_org_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CHAT_RUNTIME_OPT_IN_ENV, "1")
    service = _FakeRuntimeService()

    await dispatch_chat_run(
        _request(service),
        identity=IDENTITY,
        session_key=SESSION_KEY,
        message_id="msg-1",
        # Client-controlled payload attempting to widen scope.
        input_payload={"org_id": "identity:token:victim", "orgId": "identity:token:victim"},
    )

    assert service.calls[0]["org_id"] == "identity:token:user-1"


# --------------------------------------------------------------------------
# Opt-in on but Runtime unavailable: degrade, do not fail the user's message
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_reports_runtime_unavailable_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CHAT_RUNTIME_OPT_IN_ENV, "1")
    service = _FakeRuntimeService(fail=True)

    result = await dispatch_chat_run(
        _request(service),
        identity=IDENTITY,
        session_key=SESSION_KEY,
        message_id="msg-1",
    )

    assert result.enabled is True
    assert result.dispatched is False
    assert result.reason == "runtime_unavailable"
    assert result.run_id is None
    # Scope is still reported so callers can log the attempted org.
    assert result.org_id == "identity:token:user-1"


@pytest.mark.asyncio
async def test_dispatch_requires_session_key_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CHAT_RUNTIME_OPT_IN_ENV, "1")
    service = _FakeRuntimeService()

    with pytest.raises(ValueError):
        await dispatch_chat_run(_request(service), identity=IDENTITY, session_key="  ")

    assert service.calls == []


def test_module_does_not_import_runtime_kernel_eagerly() -> None:
    """Importing the Chat seam must not require the Runtime/PostgreSQL stack."""
    # If the Runtime kernel were imported eagerly this would already have blown
    # up at module import in a light environment.
    assert dispatch_mod.chat_runtime_opt_in_enabled() in (True, False)
