"""Sensitive-field filtering tests for the Runtime input-snapshot display boundary.

Covers AG-G2-AUTO-027: a Runtime input snapshot is execution input, not a display
artefact. Only the fields the user already sees may be summarised from it, and the
full prompt, credentials, cookies, tokens, internal paths and provider settings
must not reach the task stream or ``execution_history``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.gateway import task_runtime_input_snapshot as snap
from app.gateway.task_runtime_context import build_task_runtime_context
from app.gateway.task_runtime_linkage import LINKAGE_TASK_KEY
from app.gateway.task_runtime_projection import HISTORY_FIELD, project_runtime_status

ORG_A = "org-a"
TASK_ID = "e4c1a2b0"
ORG_SCOPE_KEY = f"tc:org:{ORG_A}"

PROMPT_TEXT = "请把 /var/etl/daily.csv 汇总后发到 https://internal.corp/api/v1/report"
TOKEN_TEXT = "ghp_ABCdef0123456789ghp_ABCdef0123456789"


def _identity() -> dict[str, Any]:
    return {
        "org_id": ORG_A,
        "scope_id": "personal:webui:1",
        "principal": {"principal_id": "webui:1", "principal_type": "internal"},
    }


def _task(**extra: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": TASK_ID,
        "name": "每周经营简报",
        "run_mode": "unattended",
        "status": "inbox",
        "unattended_attempts": 0,
    }
    task.update(extra)
    return task


# --- key-name screening ----------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "access_token",
        "auth_token",
        "refreshToken",
        "session_cookie",
        "cookie",
        "password",
        "api_key",
        "apiKey",
        "client_secret",
        "authorization",
        "provider_config",
        "llm_provider",
        "base_url",
        "endpoint",
        "webhook_url",
        "attachment_path",
        "file_path",
        "internal_dir",
        "asset_uri",
        "bucket",
        "dsn",
        "private_key",
    ],
)
def test_a_secret_or_internal_key_is_sensitive(key: str) -> None:
    assert snap.is_sensitive_input_key(key)


@pytest.mark.parametrize("key", ["name", "description", "title", "author", "ghost", "status"])
def test_an_ordinary_key_is_not_sensitive(key: str) -> None:
    assert not snap.is_sensitive_input_key(key)


# --- the projected summary -------------------------------------------------------


def test_only_displayable_fields_survive() -> None:
    payload = {
        "name": "每周经营简报",
        "description": "给管理层的周报",
        "prompt": PROMPT_TEXT,
        "message": "hello",
        "input": {"a": 1},
        "inputs": [1, 2, 3],
    }

    summary = snap.sanitize_input_snapshot(payload)

    assert summary is not None
    assert summary["name"] == "每周经营简报"
    assert summary["description"] == "给管理层的周报"
    assert summary[snap.WITHHELD_COUNT_KEY] == 4
    assert "prompt" not in summary
    assert "input" not in summary
    assert "inputs" not in summary


def test_the_full_prompt_never_appears() -> None:
    summary = snap.sanitize_input_snapshot({"prompt": PROMPT_TEXT})

    assert summary is None or PROMPT_TEXT not in json.dumps(summary, ensure_ascii=False)


def test_a_secret_looking_value_is_dropped_even_under_a_displayable_key() -> None:
    summary = snap.sanitize_input_snapshot({"description": f"see {PROMPT_TEXT}"})

    assert summary is not None
    assert "description" not in summary
    assert summary[snap.WITHHELD_COUNT_KEY] == 1


def test_a_token_looking_value_is_dropped() -> None:
    summary = snap.sanitize_input_snapshot({"name": TOKEN_TEXT})

    assert summary is not None
    assert "name" not in summary


def test_a_non_primitive_value_is_dropped() -> None:
    summary = snap.sanitize_input_snapshot({"name": {"nested": ["structure"]}})

    assert summary is not None
    assert "name" not in summary


def test_an_empty_snapshot_is_reported_as_nothing_displayable() -> None:
    assert snap.sanitize_input_snapshot({}) is None
    assert snap.sanitize_input_snapshot(None) is None
    assert snap.sanitize_input_snapshot("not-a-mapping") is None


def test_a_withheld_count_is_stated_rather_than_hidden() -> None:
    summary = snap.sanitize_input_snapshot({"prompt": "x", "inputs": {}})

    assert summary == {snap.WITHHELD_COUNT_KEY: 2}


def test_never_projected_keys_cover_the_execution_input() -> None:
    assert set(snap.NEVER_PROJECTED_INPUT_KEYS) == {"prompt", "message", "input", "inputs"}
    for key in snap.NEVER_PROJECTED_INPUT_KEYS:
        assert key not in snap.DISPLAYABLE_INPUT_KEYS


# --- the boundary against the real snapshot --------------------------------------


def test_the_real_snapshot_projects_only_the_visible_fields() -> None:
    """The context's snapshot is execution input plus the task's own visible name."""
    task = _task(prompt=PROMPT_TEXT, message="hello")
    context = build_task_runtime_context(task=task, authz=_identity(), authorized=True)

    assert "prompt" in context.input_payload
    summary = snap.sanitize_input_snapshot(context.input_payload)

    assert summary is not None
    assert summary["name"] == "每周经营简报"
    assert summary[snap.WITHHELD_COUNT_KEY] == 2
    assert "prompt" not in summary
    assert "message" not in summary
    assert PROMPT_TEXT not in json.dumps(summary, ensure_ascii=False)


def test_the_snapshot_never_reaches_the_history() -> None:
    """The projection writes no snapshot text, whatever the task row carries."""
    task = _task(
        status="pending",
        prompt=PROMPT_TEXT,
        message=TOKEN_TEXT,
        description="ok",
        **{
            LINKAGE_TASK_KEY: {
                "runtime_run_id": "run-old",
                "org_scope_key": ORG_SCOPE_KEY,
                "task_id": TASK_ID,
                "idempotency_key": "tc:" + "0" * 32,
            }
        },
    )
    context = build_task_runtime_context(task=task, authz=_identity(), authorized=True)

    updated, outcome = project_runtime_status(task, context=context, runtime_status="running")

    assert outcome.action == "status_updated"
    rendered = json.dumps(updated[HISTORY_FIELD], ensure_ascii=False)
    assert PROMPT_TEXT not in rendered
    assert TOKEN_TEXT not in rendered
    assert snap.input_snapshot_leaks(context.input_payload, updated[HISTORY_FIELD]) == ()


# --- the leak probe itself -------------------------------------------------------


def test_the_leak_probe_finds_a_projected_snapshot_value() -> None:
    payload = {"prompt": PROMPT_TEXT}

    assert snap.input_snapshot_leaks(payload, {"text": f"ran: {PROMPT_TEXT}"}) == (PROMPT_TEXT,)


def test_the_leak_probe_ignores_short_and_absent_values() -> None:
    payload = {"prompt": "abc", "inputs": {"nested": "a-long-enough-value"}}

    assert snap.input_snapshot_leaks(payload, {"text": "nothing to see"}) == ()


def test_the_leak_probe_sees_a_nested_display_value() -> None:
    payload = {"inputs": {"detail": "a-long-enough-value"}}

    leaked = snap.input_snapshot_leaks(payload, {"outer": [{"inner": "a-long-enough-value"}]})

    assert leaked == ("a-long-enough-value",)
