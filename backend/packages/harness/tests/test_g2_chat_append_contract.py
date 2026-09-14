"""Contract protection for the live chat transcript append entrypoint.

Guards the *existing* user-visible path used by the frontend:

    POST /api/chat/sessions/{session_key}/messages
        -> chat_sessions.append_session_message
        -> chat_session_service.append_message_and_touch_session

The test intentionally drives the real service/persistence layer (no fake API,
no module-wide mocks) so that a regression in the wire contract fails here.
LangGraph and the QAgent Runtime are *not* subjects of this test.
"""

from __future__ import annotations

import importlib.util

import pytest
from fastapi import HTTPException

from app.gateway.routers import chat_sessions as cs
from evoflow.persistence import chat_message_repositories as msg_repo
from evoflow.persistence import chat_session_service as chat_svc
from evoflow.persistence import session_repositories as sess_repo
from evoflow.persistence.db import reset_db_for_tests

SESSION_KEY = "agent:main:g2-contract"
THREAD_ID = "thread-g2-contract"

# The transcript write path transitively imports the agent runtime stack
# (evoflow.agents -> langgraph/langchain). A light environment without those
# deps can still run the request-shape contract tests, so only the write-path
# tests are gated rather than erroring out at collection time.
_HAS_AGENT_RUNTIME = importlib.util.find_spec("langgraph") is not None

requires_agent_runtime = pytest.mark.skipif(
    not _HAS_AGENT_RUNTIME,
    reason="transcript write path requires langgraph/langchain (agent runtime deps)",
)


@pytest.fixture
def chat_db(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Isolated SQLite transcript DB (no external services required)."""
    monkeypatch.setenv("EVOFLOW_DB_PATH", str(tmp_path / "g2-contract.db"))
    reset_db_for_tests()
    sess_repo.upsert_session_row(
        SESSION_KEY,
        thread_id=THREAD_ID,
        created_at_ms=1,
        updated_at_ms=1,
        message_count=0,
        context={},
        title="t",
    )
    yield
    reset_db_for_tests()


# --------------------------------------------------------------------------
# Request-shape contract: the fields the frontend actually sends
# --------------------------------------------------------------------------


def test_append_message_body_accepts_frontend_wire_fields() -> None:
    """The request model must keep accepting the fields the UI sends."""
    body = cs.AppendMessageBody.model_validate(
        {
            "role": "user",
            "content": "hello",
            "messageId": "u-1",
            "runId": "run-1",
            "threadId": THREAD_ID,
            "parentThreadId": "parent-1",
        }
    )
    assert body.role == "user"
    assert body.content == "hello"
    assert body.messageId == "u-1"
    assert body.runId == "run-1"
    assert body.threadId == THREAD_ID
    assert body.parentThreadId == "parent-1"


def test_client_may_only_append_user_role() -> None:
    """Client HTTP append is restricted to `user`; assistant/tool are server-written."""
    cs._validate_client_http_append_role("user")

    for role in ("assistant", "tool", "system"):
        with pytest.raises(HTTPException) as exc:
            cs._validate_client_http_append_role(role)
        assert exc.value.status_code == 422


def test_append_body_rejects_unknown_role() -> None:
    with pytest.raises(HTTPException) as exc:
        cs._validate_append_body("wizard", "x")
    assert exc.value.status_code == 422


def test_append_body_rejects_oversized_content() -> None:
    oversized = "x" * (cs._MAX_APPEND_CONTENT_BYTES + 1)
    with pytest.raises(HTTPException) as exc:
        cs._validate_append_body("user", oversized)
    assert exc.value.status_code == 413


# --------------------------------------------------------------------------
# Write-path contract: session/message association + response shape
# --------------------------------------------------------------------------


@requires_agent_runtime
def test_append_message_associates_message_with_session(chat_db: None) -> None:
    """A user append lands in the transcript and is bound to the session/run."""
    del chat_db

    result = chat_svc.append_message_and_touch_session(
        SESSION_KEY,
        role="user",
        content="ping",
        run_id="run-1",
        thread_id=THREAD_ID,
        message_id="u-1",
    )

    assert result is not None
    # Response shape consumed by the UI: append fields + messageCount
    assert "messageCount" in result
    assert result["messageCount"] == 1

    rows = msg_repo.list_messages(SESSION_KEY, limit=10)
    assert len(rows) == 1
    assert rows[0]["role"] == "user"
    assert rows[0].get("run_id") == "run-1"


@requires_agent_runtime
def test_append_message_counts_and_touches_session(chat_db: None) -> None:
    """Each append increments the session message count (dedupe is by message_id)."""
    del chat_db

    for idx in range(3):
        chat_svc.append_message_and_touch_session(
            SESSION_KEY,
            role="user",
            content=f"m{idx}",
            run_id="run-1",
            thread_id=THREAD_ID,
            message_id=f"u-{idx}",
        )

    assert msg_repo.count_messages(SESSION_KEY) == 3

    session_map = sess_repo.load_session_map()
    assert SESSION_KEY in session_map
    assert int(session_map[SESSION_KEY].get("messageCount") or 0) == 3


@requires_agent_runtime
def test_append_message_idempotent_by_message_id(chat_db: None) -> None:
    """Re-sending the same messageId must not create a duplicate transcript row."""
    del chat_db

    payload = {
        "role": "user",
        "content": "retry me",
        "run_id": "run-1",
        "thread_id": THREAD_ID,
        "message_id": "u-dup",
    }
    first = chat_svc.append_message_and_touch_session(SESSION_KEY, **payload)
    second = chat_svc.append_message_and_touch_session(SESSION_KEY, **payload)

    assert first is not None
    assert second is not None
    assert msg_repo.count_messages(SESSION_KEY) == 1
    assert second["messageCount"] == 1


@requires_agent_runtime
def test_append_message_retry_returns_the_persisted_row(chat_db: None) -> None:
    """The retry result is the existing row, and dedupe stays session-scoped."""
    del chat_db

    other_session = "agent:main:g2-contract-other"
    sess_repo.upsert_session_row(
        other_session,
        thread_id=THREAD_ID,
        created_at_ms=1,
        updated_at_ms=1,
        message_count=0,
        context={},
        title="t",
    )

    payload = {
        "role": "user",
        "content": "retry me twice",
        "run_id": "run-1",
        "thread_id": THREAD_ID,
        "message_id": "u-ident",
    }
    first = chat_svc.append_message_and_touch_session(SESSION_KEY, **payload)
    second = chat_svc.append_message_and_touch_session(SESSION_KEY, **payload)

    assert first is not None and second is not None
    # Same persisted row: same seq, same id, same session.
    assert second["seq"] == first["seq"]
    assert second["message_id"] == "u-ident"
    assert second["session_key"] == SESSION_KEY
    assert second["role"] == "user"

    # The same messageId in another session must not be conflated with that row.
    other = chat_svc.append_message_and_touch_session(other_session, **payload)
    assert other is not None
    assert other["session_key"] == other_session
    assert msg_repo.count_messages(other_session) == 1
    assert msg_repo.count_messages(SESSION_KEY) == 1

    # A different messageId in the same session still appends a new row.
    third = chat_svc.append_message_and_touch_session(
        SESSION_KEY, **{**payload, "message_id": "u-ident-2", "content": "not a retry"}
    )
    assert third is not None
    assert third["message_id"] == "u-ident-2"
    assert third["seq"] != first["seq"]
    assert msg_repo.count_messages(SESSION_KEY) == 2
