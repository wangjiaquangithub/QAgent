"""Chat -> Runtime identity mapping contract.

Protects the two invariants the Chat domain must guarantee when handing a
request to the QAgent Runtime:

1. Repeated requests for the same session/message map to a **stable**
   ``(org_id, idempotency_key)`` pair, so the Runtime reuses one run instead of
   spawning uncontrolled duplicates.
2. ``org_id`` is derived from verified server-side identity only. There is no
   input through which a caller can claim another organization.

No Runtime kernel code, database schema, or migration is exercised here — the
mapping is a pure Chat-domain function.
"""

from __future__ import annotations

import pytest

from app.gateway.chat_runtime_identity import (
    build_chat_run_identity,
    runtime_org_id_for_identity,
)

SESSION_KEY = "agent:main:g2-identity"


def _identity(identity_type: str, identity_id: str) -> dict[str, str]:
    return {"identity_type": identity_type, "identity_id": identity_id}


# --------------------------------------------------------------------------
# org_id derivation: server-side only, cannot be forged by the client
# --------------------------------------------------------------------------


def test_org_id_derived_from_verified_identity() -> None:
    org = runtime_org_id_for_identity(_identity("token", "user-1"))
    assert org == "identity:token:user-1"


def test_org_id_matches_runtime_principal_format() -> None:
    """Must stay aligned with app.qagent_runtime.auth.get_runtime_principal."""
    ident = _identity("webui", "42")
    mapped = build_chat_run_identity(identity=ident, session_key=SESSION_KEY)
    assert mapped.org_id == runtime_org_id_for_identity(ident)
    assert mapped.org_id == "identity:webui:42"


def test_org_id_falls_back_to_local_when_unauthenticated() -> None:
    """Unauthenticated requests use the Runtime default, not another org."""
    assert runtime_org_id_for_identity(None) == "local"
    assert runtime_org_id_for_identity({}) == "local"
    assert runtime_org_id_for_identity({"identity_type": "token"}) == "local"
    assert runtime_org_id_for_identity({"identity_id": "user-1"}) == "local"


def test_client_cannot_forge_organization() -> None:
    """extra_payload (client-controlled) must never change org_id."""
    mapped = build_chat_run_identity(
        identity=_identity("token", "user-1"),
        session_key=SESSION_KEY,
        extra_payload={"org_id": "identity:token:someone-else", "orgId": "evil"},
    )
    assert mapped.org_id == "identity:token:user-1"

    # The forged value may ride along as opaque payload, but never as scope.
    assert "org_id" not in mapped.as_create_run_kwargs() or mapped.as_create_run_kwargs()["org_id"] == mapped.org_id


def test_two_identities_do_not_share_org_scope() -> None:
    a = build_chat_run_identity(identity=_identity("token", "alice"), session_key=SESSION_KEY)
    b = build_chat_run_identity(identity=_identity("token", "bob"), session_key=SESSION_KEY)
    assert a.org_id != b.org_id
    # Same session key, different orgs -> different idempotency scopes
    assert (a.org_id, a.idempotency_key) != (b.org_id, b.idempotency_key)


# --------------------------------------------------------------------------
# Idempotency: repeated sends must not produce uncontrolled duplicate runs
# --------------------------------------------------------------------------


def test_repeated_request_yields_stable_idempotency_key() -> None:
    ident = _identity("token", "user-1")
    first = build_chat_run_identity(identity=ident, session_key=SESSION_KEY, message_id="msg-1")
    second = build_chat_run_identity(identity=ident, session_key=SESSION_KEY, message_id="msg-1")

    assert first.idempotency_key == second.idempotency_key
    assert first.task_id == second.task_id
    assert first.as_create_run_kwargs() == second.as_create_run_kwargs()


def test_distinct_messages_get_distinct_idempotency_keys() -> None:
    ident = _identity("token", "user-1")
    a = build_chat_run_identity(identity=ident, session_key=SESSION_KEY, message_id="msg-1")
    b = build_chat_run_identity(identity=ident, session_key=SESSION_KEY, message_id="msg-2")
    assert a.idempotency_key != b.idempotency_key


def test_idempotency_key_within_runtime_column_width() -> None:
    """qagent_runs.idempotency_key is String(255) — the key must always fit."""
    long_key = "x" * 400
    mapped = build_chat_run_identity(
        identity=_identity("token", "user-1"),
        session_key=long_key,
        message_id=long_key,
    )
    assert len(mapped.idempotency_key) <= 255
    assert len(mapped.task_id) <= 96
    # Truncation must remain deterministic so retries still dedupe.
    again = build_chat_run_identity(
        identity=_identity("token", "user-1"),
        session_key=long_key,
        message_id=long_key,
    )
    assert mapped.idempotency_key == again.idempotency_key


def test_existing_run_id_binds_idempotency() -> None:
    """When a chat run id already exists, it drives the idempotency basis."""
    ident = _identity("token", "user-1")
    mapped = build_chat_run_identity(
        identity=ident,
        session_key=SESSION_KEY,
        run_id="run-existing",
        message_id="msg-1",
    )
    assert "run-existing" in mapped.idempotency_key
    assert mapped.input_payload["chat_run_id"] == "run-existing"


def test_session_key_required() -> None:
    with pytest.raises(ValueError):
        build_chat_run_identity(identity=_identity("token", "user-1"), session_key="   ")


def test_payload_carries_chat_provenance() -> None:
    mapped = build_chat_run_identity(
        identity=_identity("token", "user-1"),
        session_key=SESSION_KEY,
        message_id="msg-1",
        principal_id="user:alice",
    )
    assert mapped.input_payload["source"] == "chat"
    assert mapped.input_payload["session_key"] == SESSION_KEY
    assert mapped.input_payload["message_id"] == "msg-1"
    assert mapped.input_payload["principal_id"] == "user:alice"


def test_create_run_kwargs_only_expose_supported_fields() -> None:
    """The mapping must produce exactly the Runtime create_run signature."""
    mapped = build_chat_run_identity(identity=_identity("token", "user-1"), session_key=SESSION_KEY)
    assert set(mapped.as_create_run_kwargs()) == {
        "org_id",
        "task_id",
        "input_payload",
        "idempotency_key",
    }
