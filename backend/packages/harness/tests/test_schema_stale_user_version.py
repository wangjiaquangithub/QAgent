"""Legacy pre-1.0 user_version snap onto the public schema epoch."""

from __future__ import annotations

import sqlite3

from evoflow.persistence.schema import (
    APP_SCHEMA_VERSION,
    _snap_legacy_user_version,
    ensure_app_schema,
)


def test_snap_legacy_user_version_when_markers_present() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA user_version = 91")
    for table in (
        "evoflow_proactive_initiatives",
        "evoflow_chat_messages",
        "evoflow_usage_events",
        "evoflow_principals",
    ):
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
    conn.commit()

    snapped = _snap_legacy_user_version(conn, 91)
    assert snapped == APP_SCHEMA_VERSION
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == APP_SCHEMA_VERSION
    conn.close()


def test_ensure_app_schema_preserves_rows_when_snapping_legacy() -> None:
    """Legacy ladder version + marker tables: snap epoch, keep existing rows."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA user_version = 8")
    conn.executescript(
        """
        CREATE TABLE evoflow_chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_key TEXT NOT NULL,
            seq INTEGER NOT NULL,
            role TEXT NOT NULL,
            message_id TEXT,
            content_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            round_id TEXT,
            UNIQUE(session_key, seq)
        );
        INSERT INTO evoflow_chat_messages(
            session_key, seq, role, message_id, content_json, created_at, updated_at
        ) VALUES ('sk', 1, 'user', 'm1', '{}', 't', 't');

        CREATE TABLE evoflow_proactive_initiatives (id TEXT PRIMARY KEY);
        CREATE TABLE evoflow_usage_events (id INTEGER PRIMARY KEY);
        CREATE TABLE evoflow_principals (id TEXT PRIMARY KEY);
        """
    )
    conn.commit()

    ensure_app_schema(conn)

    row = conn.execute(
        "SELECT message_id FROM evoflow_chat_messages WHERE session_key='sk'"
    ).fetchone()
    assert row is not None
    assert row[0] == "m1"
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == APP_SCHEMA_VERSION
    conn.close()


def test_fresh_db_applies_baseline_to_version_1() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_app_schema(conn)
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == APP_SCHEMA_VERSION
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evoflow_apps'"
    ).fetchone()
    conn.close()


def test_ensure_app_schema_backfills_missing_authz_tables_at_current_epoch() -> None:
    """A snapped legacy database can have epoch 1 but lack authz tables."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        f"""
        CREATE TABLE evoflow_chat_messages (id INTEGER PRIMARY KEY);
        PRAGMA user_version = {APP_SCHEMA_VERSION};
        """
    )

    ensure_app_schema(conn)

    expected_tables = (
        "evoflow_acl_grants",
        "evoflow_admin_grants",
        "evoflow_principal_identities",
        "evoflow_principals",
    )
    for table in expected_tables:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == APP_SCHEMA_VERSION
    conn.close()


def test_ensure_app_schema_backfills_hidden_session_list_flag_at_current_epoch() -> None:
    """A snapped database can lack the column used by chat session queries."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        f"""
        CREATE TABLE evoflow_chat_sessions (
            session_key TEXT PRIMARY KEY,
            updated_at TEXT NOT NULL DEFAULT '',
            is_deleted INTEGER NOT NULL DEFAULT 0
        );
        PRAGMA user_version = {APP_SCHEMA_VERSION};
        """
    )

    ensure_app_schema(conn)

    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(evoflow_chat_sessions)")
    }
    assert {
        "hidden_from_list",
        "permission_preset",
        "org_id",
        "scope_id",
        "created_by",
    } <= columns
    conn.execute(
        "INSERT INTO evoflow_chat_sessions(session_key) VALUES ('session-1')"
    )
    assert conn.execute(
        "SELECT hidden_from_list FROM evoflow_chat_sessions WHERE session_key='session-1'"
    ).fetchone() == (0,)
    for index_name in (
        "idx_evo_chat_sessions_hidden_from_list",
        "idx_evo_chat_sessions_created_by",
        "idx_evo_chat_sessions_scope",
    ):
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (index_name,)
        ).fetchone()
    conn.close()
