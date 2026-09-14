"""Protective tests for sanitised Runtime result summaries on tasks.

Covers AG-G2-AUTO-013: a completed run's displayable summary reaches the existing
execution history, a completed run with no result is still unambiguous, repeats
are idempotent, and nothing that a user must not see can leak through - no
provider object, credential, endpoint, prompt or traceback.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.gateway.task_runtime_context import build_task_runtime_context
from app.gateway.task_runtime_event_bridge import apply_runtime_event
from app.gateway.task_runtime_linkage import link_runtime_run
from app.gateway.task_runtime_projection import (
    HISTORY_FIELD,
    build_runtime_history_record,
    project_runtime_status,
)
from app.gateway.task_runtime_result import (
    MAX_SUMMARY_TEXT,
    RESULT_SUMMARY_KEYS,
    sanitize_result_summary,
)

ORG_A = "org-a"
TASK_ID = "e4c1a2b0"
RUN_ID = "run-1"

# Strings that must never survive into the task, each attached to a realistic
# carrier key.
SECRETS = {
    "api_key": "sk-live-DO-NOT-LEAK",
    "token": "Bearer DO-NOT-LEAK",
    "provider_raw": "provider-object-DO-NOT-LEAK",
    "traceback": "Traceback-DO-NOT-LEAK",
    "prompt": "original-prompt-DO-NOT-LEAK",
    "endpoint": "https://internal-DO-NOT-LEAK.example/api",
    "path": "/var/private/DO-NOT-LEAK",
}


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


def _linked(*, status: str = "executing") -> tuple[dict[str, Any], Any]:
    ctx = build_task_runtime_context(task=_task(status=status), authz=_authz(), authorized=True)
    task, _ = link_runtime_run(_task(status=status), context=ctx, runtime_run_id=RUN_ID)
    return task, ctx


def _history(task: dict[str, Any]) -> list[dict[str, Any]]:
    return list(task.get(HISTORY_FIELD) or [])


def _hostile_result() -> dict[str, Any]:
    return {
        "summary": "区域销量已汇总。\n\n  异常清单 3 条。",
        "count": 3,
        "items": ["华东", {"nested": "DO-NOT-LEAK"}, "华南"],
        **SECRETS,
        "provider_obj": object(),
        "deep": {"nested": {"deeper": "DO-NOT-LEAK"}},
    }


# --- the sanitiser ---------------------------------------------------------


def test_only_allowlisted_keys_ever_survive() -> None:
    summary = sanitize_result_summary(_hostile_result())

    assert summary is not None
    assert set(summary) <= set(RESULT_SUMMARY_KEYS)
    assert summary["summary"] == "区域销量已汇总。 异常清单 3 条。"
    assert summary["count"] == 3
    assert summary["items"] == ["华东", "华南"]


def test_free_text_is_collapsed_and_truncated() -> None:
    summary = sanitize_result_summary({"summary": "x" * (MAX_SUMMARY_TEXT + 500)})

    assert summary is not None
    assert len(summary["summary"]) == MAX_SUMMARY_TEXT
    assert "\n" not in summary["summary"]


@pytest.mark.parametrize(
    "result",
    [None, "a string", 42, [], {}, {"unknown_only": "value"}, {"summary": "   "}],
)
def test_nothing_displayable_yields_no_summary(result: Any) -> None:
    assert sanitize_result_summary(result) is None


def test_no_secret_or_internal_ever_survives_sanitisation() -> None:
    summary = sanitize_result_summary(_hostile_result())
    serialized = json.dumps(summary, ensure_ascii=False, sort_keys=True)

    for value in SECRETS.values():
        assert value not in serialized, f"leaked: {value}"
    assert "DO-NOT-LEAK" not in serialized


# --- projection ------------------------------------------------------------


def test_a_completed_run_records_the_displayable_summary() -> None:
    task, ctx = _linked()

    updated, outcome = project_runtime_status(
        task, context=ctx, runtime_status="completed", result_summary=_hostile_result()
    )

    assert outcome.status_after == "completed"
    record = _history(updated)[-1]
    assert record["result_available"] is True
    assert record["result"]["summary"] == "区域销量已汇总。 异常清单 3 条。"
    assert set(record["result"]) <= set(RESULT_SUMMARY_KEYS)


def test_a_completed_run_without_a_result_is_explicitly_marked() -> None:
    task, ctx = _linked()

    updated, _ = project_runtime_status(
        task, context=ctx, runtime_status="completed", result_summary=None
    )

    record = _history(updated)[-1]
    assert record["result_available"] is False
    assert "result" not in record, "no result must not look like an empty result"


def test_a_completed_run_whose_result_is_unusable_is_marked_empty() -> None:
    task, ctx = _linked()

    updated, _ = project_runtime_status(
        task, context=ctx, runtime_status="completed", result_summary={"api_key": "sk-live-x"}
    )

    record = _history(updated)[-1]
    assert record["result_available"] is False
    assert "result" not in record


@pytest.mark.parametrize("status", ["running", "planning", "failed", "cancelled", "timed_out"])
def test_non_completed_statuses_carry_no_result_fields(status: str) -> None:
    task, ctx = _linked(status="pending")

    updated, _ = project_runtime_status(
        task, context=ctx, runtime_status=status, result_summary=_hostile_result()
    )

    record = _history(updated)[-1]
    assert "result" not in record
    assert "result_available" not in record


def test_repeated_result_projection_is_idempotent() -> None:
    task, ctx = _linked()
    first, _ = project_runtime_status(
        task,
        context=ctx,
        runtime_status="completed",
        event_id="ev-done",
        sequence=9,
        result_summary=_hostile_result(),
    )
    before = len(_history(first))

    second, outcome = project_runtime_status(
        first,
        context=ctx,
        runtime_status="completed",
        event_id="ev-done",
        sequence=9,
        result_summary={"summary": "a completely different summary"},
    )

    assert outcome.action == "noop"
    assert len(_history(second)) == before
    assert _history(second)[-1]["result"]["summary"] == "区域销量已汇总。 异常清单 3 条。"


def test_the_history_record_stays_json_safe_with_a_hostile_result() -> None:
    task, ctx = _linked()

    updated, _ = project_runtime_status(
        task, context=ctx, runtime_status="completed", result_summary=_hostile_result()
    )

    serialized = json.dumps(_history(updated), ensure_ascii=False, sort_keys=True)
    assert "DO-NOT-LEAK" not in serialized

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        else:
            assert type(value).__module__.split(".")[0] not in {"agentscope", "app"}

    walk(_history(updated))


def test_the_shared_history_builder_sanitises_the_result_too() -> None:
    # The cancel path and any other call site use the same builder, so they cannot
    # bypass the sanitiser.
    record = build_runtime_history_record(
        runtime_status="completed",
        run_id=RUN_ID,
        org_scope_key="tc:org:org-a",
        result_summary=_hostile_result(),
    )

    assert record["result_available"] is True
    assert "DO-NOT-LEAK" not in json.dumps(record, ensure_ascii=False)
    assert set(record["result"]) <= set(RESULT_SUMMARY_KEYS)


# --- the event bridge ------------------------------------------------------


def test_a_result_on_a_completed_frame_is_projected_through_the_bridge() -> None:
    task, ctx = _linked(status="executing")

    updated, outcome = apply_runtime_event(
        task,
        context=ctx,
        event={
            "event_id": "ev-done",
            "run_id": RUN_ID,
            "sequence": 3,
            "type": "run.completed",
            "payload": {"result": _hostile_result()},
        },
    )

    assert outcome.status_after == "completed"
    record = _history(updated)[-1]
    assert record["result_available"] is True
    assert "DO-NOT-LEAK" not in json.dumps(_history(updated), ensure_ascii=False)
