"""JWT-attached requests must not resolve to local admin (switch-user 串台)."""

from __future__ import annotations

import gc
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from evoflow.persistence.db import get_db, reset_db_for_tests
from evoflow.persistence.schema import ensure_app_schema

_EVOFLOW_ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def sqlite_tmp(monkeypatch: pytest.MonkeyPatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("EVOFLOW_HOME", tmp)
        monkeypatch.setenv("EVOFLOW_CONFIG_PATH", str(_EVOFLOW_ROOT / "config.yaml"))
        reset_db_for_tests()
        yield tmp
        reset_db_for_tests()
        gc.collect()


def test_jwt_payload_does_not_fall_back_to_local_admin(sqlite_tmp: str) -> None:
    del sqlite_tmp
    from evoflow.authz import principals as principals_mod
    from evoflow.authz.context import me_payload, resolve_request_authz, resolve_request_principal

    ensure_app_schema(get_db())
    admin = principals_mod.get_or_create_local_admin()
    alice = principals_mod.ensure_webui_principal(
        webui_user_id=42,
        username="alice",
        is_primary=False,
    )
    assert str(alice["principal_id"]) != str(admin["principal_id"])

    req = MagicMock()
    req.state = SimpleNamespace(
        webui_user={
            "sub": str(alice["principal_id"]),
            "principal_id": str(alice["principal_id"]),
            "username": "alice",
            "webui_user_id": 42,
            "auth_method": "password",
        }
    )
    req.headers = {}

    p = resolve_request_principal(req)
    assert p["principal_id"] == alice["principal_id"]
    assert p["principal_id"] != admin["principal_id"]

    ctx = resolve_request_authz(req)
    payload = me_payload(ctx, auth_source="jwt", username="alice")
    assert payload["principalId"] == alice["principal_id"]
    assert payload["displayName"]
    assert payload["displayName"].lower() != "local admin"
    assert payload["username"] == "alice"
    assert payload["authSource"] == "jwt"


def test_jwt_recovers_via_ensure_when_principal_missing(sqlite_tmp: str) -> None:
    del sqlite_tmp
    from evoflow.authz import principals as principals_mod
    from evoflow.authz.context import resolve_request_principal

    ensure_app_schema(get_db())
    admin = principals_mod.get_or_create_local_admin()

    req = MagicMock()
    req.state = SimpleNamespace(
        webui_user={
            "sub": "99",
            "username": "bob",
            "webui_user_id": 99,
            "auth_method": "password",
        }
    )
    req.headers = {}

    p = resolve_request_principal(req)
    assert p["principal_id"] != admin["principal_id"]
    assert "bob" in str(p.get("display_name") or "").lower() or p["principal_id"].startswith("webui:")


def test_local_admin_repairs_orphaned_bootstrap_principal(sqlite_tmp: str) -> None:
    """A stale DB may contain local-admin before its identity/grant tables existed."""
    del sqlite_tmp
    from evoflow.authz import admin_grants as admin_mod
    from evoflow.authz import principals as principals_mod

    ensure_app_schema(get_db())
    principals_mod.create_principal(
        display_name="Local Admin",
        principal_id="local-admin",
        attrs={"bootstrap": True},
    )

    assert principals_mod.resolve_principal_by_identity("local", "admin") is None
    assert not admin_mod.is_org_admin("local-admin")

    recovered = principals_mod.get_or_create_local_admin()
    assert recovered["principal_id"] == "local-admin"
    assert principals_mod.resolve_principal_by_identity("local", "admin") == recovered
    assert admin_mod.is_org_admin("local-admin")

    # The repair is idempotent and never retries the fixed principal insert.
    assert principals_mod.get_or_create_local_admin()["principal_id"] == "local-admin"
    assert (
        get_db()
        .execute(
            """
            SELECT COUNT(*) FROM evoflow_principal_identities
            WHERE org_id = 'local' AND provider = 'local' AND external_id = 'admin'
            """
        )
        .fetchone()[0]
        == 1
    )
