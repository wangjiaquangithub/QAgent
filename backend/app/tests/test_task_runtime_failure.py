"""Protective tests for redacted Runtime failure projection.

Covers AG-G2-AUTO-014: a failed run's public code and message reach the task
history, cancelled and failed stay distinguishable, a terminal error is never
rewritten by a later stale event, and none of the leak shapes a provider
exception can carry - traceback, endpoint, path, credential, prompt - survives.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.gateway.task_runtime_context import build_task_runtime_context
from app.gateway.task_runtime_event_bridge import apply_runtime_event
from app.gateway.task_runtime_failure import (
    MAX_FAILURE_MESSAGE,
    sanitize_error_code,
    sanitize_failure_detail,
    sanitize_failure_message,
)
from app.gateway.task_runtime_linkage import link_runtime_run
from app.gateway.task_runtime_projection import HISTORY_FIELD, project_runtime_status

ORG_A = "org-a"
TASK_ID = "e4c1a2b0"
RUN_ID = "run-1"

LEAKY_MESSAGES = [
    'Traceback (most recent call last): File "/app/x.py", line 42, in go',
    "connection to https://internal.example/v1 failed",
    "cannot read /var/private/creds.json",
    "open C:\\Users\\ops\\secret.txt failed",
    "provider rejected api_key=sk-live-DO-NOT-LEAK",
    "authorization: Bearer DO-NOT-LEAK",
    "password prompt DO-NOT-LEAK",
    "token refresh failed: access_token=DO-NOT-LEAK",
]

SAFE_MESSAGES = [
    "provider returned an unusable response",
    "exceeded 900s budget second line",
    "structured output did not match the expected schema",
]


def _authz() -> dict[str, Any]:
    return {
        "org_id": ORG_A,
        "principal": {"principal_id": "webui:1", "principal_type": "internal"},
        "scope_id": "personal:webui:1",
        "is_org_admin": False,
    }


def _task(*, status: str = "executing") -> dict[str, Any]:
    return {
        "id": TASK_ID,
        "name": "每周经营简报",
        "run_mode": "unattended",
        "status": status,
        "unattended_attempts": 0,
        "execution_history": [],
    }


def _linked(*, status: str = "pending") -> tuple[dict[str, Any], Any]:
    ctx = build_task_runtime_context(task=_task(status=status), authz=_authz(), authorized=True)
    task, _ = link_runtime_run(_task(status=status), context=ctx, runtime_run_id=RUN_ID)
    return task, ctx


def _history(task: dict[str, Any]) -> list[dict[str, Any]]:
    return list(task.get(HISTORY_FIELD) or [])


def _frame(event_type: str, *, payload: Any = None, sequence: int = 1, event_id: str = "ev-1"):
    return {
        "event_id": event_id,
        "run_id": RUN_ID,
        "sequence": sequence,
        "type": event_type,
        "payload": {} if payload is None else payload,
    }


# --- the code sanitiser ----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("run.timeout", "run.timeout"),
        ("run.timeout!!", "run.timeout"),
        ("provider.error", "provider.error"),
        ("schema.invalid_output", "schema.invalid_output"),
        ("  run.failed  ", "run.failed"),
    ],
)
def test_error_codes_keep_only_their_own_characters(raw: str, expected: str) -> None:
    assert sanitize_error_code(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "!!!", {"code": "x"}])
def test_unusable_error_codes_are_dropped(raw: Any) -> None:
    assert sanitize_error_code(raw) is None


# --- the message sanitiser -------------------------------------------------


@pytest.mark.parametrize("message", SAFE_MESSAGES)
def test_plain_sentences_survive(message: str) -> None:
    assert sanitize_failure_message(message) == message


@pytest.mark.parametrize("message", LEAKY_MESSAGES)
def test_leak_shapes_are_withheld_whole(message: str) -> None:
    assert sanitize_failure_message(message) is None
    text, withheld = sanitize_failure_detail(message)
    assert text is None and withheld is True


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_absent_messages_are_not_reported_as_withheld(raw: Any) -> None:
    assert sanitize_failure_detail(raw) == (None, False)


def test_a_long_but_plain_message_is_truncated_not_dropped() -> None:
    text = "x" * (MAX_FAILURE_MESSAGE + 200)
    sanitized = sanitize_failure_message(text)
    assert sanitized is not None
    assert len(sanitized) == MAX_FAILURE_MESSAGE


# --- projection ------------------------------------------------------------


@pytest.mark.parametrize("message", LEAKY_MESSAGES)
def test_a_leaky_failure_message_never_reaches_the_history(message: str) -> None:
    task, ctx = _linked()

    updated, outcome = project_runtime_status(
        task, context=ctx, runtime_status="failed", error_code="provider.error", reason=message
    )

    assert outcome.status_after == "failed"
    record = _history(updated)[-1]
    assert record["error_code"] == "provider.error"
    assert "reason" not in record, "an unsafe detail must be withheld, not truncated"
    assert record["error_detail_redacted"] is True

    serialized = json.dumps(_history(updated), ensure_ascii=False)
    assert "DO-NOT-LEAK" not in serialized
    assert "Traceback" not in serialized


def test_a_plain_failure_message_is_kept_verbatim() -> None:
    task, ctx = _linked()

    updated, _ = project_runtime_status(
        task,
        context=ctx,
        runtime_status="failed",
        error_code="provider.error",
        reason="provider returned an unusable response",
    )

    record = _history(updated)[-1]
    assert record["reason"] == "provider returned an unusable response"
    assert "error_detail_redacted" not in record


def test_cancelled_and_failed_are_distinguishable() -> None:
    failed_task, ctx = _linked()
    failed, _ = project_runtime_status(
        failed_task, context=ctx, runtime_status="failed", error_code="provider.error"
    )

    cancelled_task, ctx2 = _linked()
    cancelled, _ = project_runtime_status(cancelled_task, context=ctx2, runtime_status="cancelled")

    assert failed["status"] == "failed"
    assert cancelled["status"] == "cancelled"
    assert _history(failed)[-1]["runtime_status"] == "failed"
    assert _history(cancelled)[-1]["runtime_status"] == "cancelled"
    assert "error_code" not in _history(cancelled)[-1], "a cancel is not an error"


def test_a_terminal_error_is_never_rewritten_by_a_later_stale_event() -> None:
    task, ctx = _linked()
    failed, _ = project_runtime_status(
        task,
        context=ctx,
        runtime_status="failed",
        error_code="provider.error",
        reason="provider returned an unusable response",
        event_id="ev-fail",
        sequence=5,
    )

    after, outcome = project_runtime_status(
        failed,
        context=ctx,
        runtime_status="completed",
        event_id="ev-done",
        sequence=6,
        result_summary={"summary": "late success"},
    )

    assert outcome.action == "noop"
    assert after["status"] == "failed"
    assert _history(after)[-1]["runtime_status"] == "failed"
    assert len(_history(after)) == 1


def test_repeated_failure_projection_is_idempotent() -> None:
    task, ctx = _linked()
    first, _ = project_runtime_status(
        task,
        context=ctx,
        runtime_status="failed",
        error_code="provider.error",
        reason="provider returned an unusable response",
        event_id="ev-fail",
        sequence=5,
    )
    before = len(_history(first))

    second, outcome = project_runtime_status(
        first,
        context=ctx,
        runtime_status="failed",
        error_code="provider.error",
        reason="provider returned an unusable response",
        event_id="ev-fail",
        sequence=5,
    )

    assert outcome.action == "noop"
    assert len(_history(second)) == before


# --- through the event bridge, which is where provider text actually arrives


def test_a_provider_failure_frame_is_projected_redacted() -> None:
    task, ctx = _linked()

    updated, outcome = apply_runtime_event(
        task,
        context=ctx,
        event=_frame(
            "run.failed",
            payload={
                "error": {
                    "code": "provider.error",
                    "message": 'Traceback (most recent call last): File "/app/x.py", line 3, '
                    "in call -> https://internal.example api_key=sk-live-DO-NOT-LEAK",
                }
            },
        ),
    )

    assert outcome.status_after == "failed"
    record = _history(updated)[-1]
    assert record["error_code"] == "provider.error"
    assert record["error_detail_redacted"] is True
    assert "reason" not in record

    serialized = json.dumps(_history(updated), ensure_ascii=False)
    for marker in ("DO-NOT-LEAK", "Traceback", "internal.example", "/app/x.py"):
        assert marker not in serialized, f"leaked: {marker}"


def test_a_structured_output_failure_frame_keeps_a_public_code() -> None:
    task, ctx = _linked()

    updated, _ = apply_runtime_event(
        task,
        context=ctx,
        event=_frame(
            "run.failed",
            payload={"error": {"code": "schema.invalid_output", "message": "output did not match"}},
        ),
    )

    record = _history(updated)[-1]
    assert record["error_code"] == "schema.invalid_output"
    assert record["reason"] == "output did not match"
