"""Protective tests for stable, deduplicated runtime history records.

Covers AG-G2-AUTO-017: the existing execution history is reused, each stable
business change is written at most once, the same stream position or terminal
transition is never recorded twice, and no raw provider payload can reach a
record.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.gateway.task_runtime_context import build_task_runtime_context
from app.gateway.task_runtime_event_bridge import apply_runtime_event
from app.gateway.task_runtime_linkage import link_runtime_run
from app.gateway.task_runtime_projection import HISTORY_FIELD

ORG_A = "org-a"
TASK_ID = "e4c1a2b0"
RUN_ID = "run-1"

# The complete set of keys a runtime record may carry. Anything outside it would
# mean something reached the history that was not meant to.
RECORD_KEYS = frozenset(
    {
        "source",
        "runtime_run_id",
        "runtime_status",
        "org_scope_key",
        "approval_required",
        "event_id",
        "sequence",
        "error_code",
        "reason",
        "error_detail_redacted",
        "hint",
        "result_available",
        "result",
        "asset",
    }
)


def _authz() -> dict[str, Any]:
    return {
        "org_id": ORG_A,
        "principal": {"principal_id": "webui:1", "principal_type": "internal"},
        "scope_id": "personal:webui:1",
        "is_org_admin": False,
    }


def _task(*, status: str = "pending") -> dict[str, Any]:
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


def _frame(
    event_type: str,
    *,
    event_id: str | None = "ev-1",
    sequence: int = 1,
    payload: Any = None,
) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "run_id": RUN_ID,
        "sequence": sequence,
        "type": event_type,
        "payload": {} if payload is None else payload,
    }
    if event_id is not None:
        frame["event_id"] = event_id
    return frame


def _apply(task: dict[str, Any], ctx: Any, frame: dict[str, Any]) -> tuple[dict, Any]:
    return apply_runtime_event(task, context=ctx, event=frame)


# --- deduplication ---------------------------------------------------------


def test_the_same_stream_position_is_never_written_twice() -> None:
    task, ctx = _linked()
    first, _ = _apply(task, ctx, _frame("run.running", event_id="ev-a", sequence=7))
    before = len(_history(first))

    second, outcome = _apply(first, ctx, _frame("run.running", event_id="ev-b", sequence=7))

    assert outcome.action == "noop"
    assert outcome.reason == "duplicate_event"
    assert len(_history(second)) == before, "one stream position, one record"


def test_a_position_is_a_duplicate_even_without_an_event_id() -> None:
    task, ctx = _linked()
    first, _ = _apply(task, ctx, _frame("run.running", event_id=None, sequence=4))
    before = len(_history(first))

    second, outcome = _apply(first, ctx, _frame("run.running", event_id=None, sequence=4))

    assert outcome.action == "noop"
    assert outcome.reason == "duplicate_event"
    assert len(_history(second)) == before


def test_a_different_status_at_an_occupied_position_is_still_a_repeat() -> None:
    task, ctx = _linked()
    first, _ = _apply(task, ctx, _frame("run.planning", event_id="ev-a", sequence=3))

    second, outcome = _apply(first, ctx, _frame("run.running", event_id="ev-b", sequence=3))

    assert outcome.action == "noop"
    assert outcome.reason == "duplicate_event"


def test_the_same_event_id_is_never_written_twice() -> None:
    task, ctx = _linked()
    first, _ = _apply(task, ctx, _frame("run.running", event_id="ev-same", sequence=1))
    before = len(_history(first))

    second, outcome = _apply(first, ctx, _frame("run.running", event_id="ev-same", sequence=9))

    assert outcome.action == "noop"
    assert outcome.reason == "duplicate_event"
    assert len(_history(second)) == before


def test_a_terminal_transition_is_recorded_exactly_once() -> None:
    task, ctx = _linked()
    done, _ = _apply(task, ctx, _frame("run.completed", event_id="ev-a", sequence=5))

    # A different id and a later position: still the same terminal transition.
    again, outcome = _apply(done, ctx, _frame("run.completed", event_id="ev-b", sequence=6))

    assert outcome.action == "noop"
    assert outcome.reason == "terminal_already_reached"
    assert len(_history(again)) == 1
    assert again["status"] == "completed"


def test_a_repeat_without_any_position_is_still_a_repeat() -> None:
    task, ctx = _linked()
    # Strip both identifiers to reach the status-only comparison.
    stripped = {"run_id": RUN_ID, "sequence": 0, "type": "run.queued"}

    first, first_outcome = _apply(task, ctx, stripped)
    assert first_outcome.action == "history_only"

    before = len(_history(first))
    second, outcome = _apply(first, ctx, stripped)

    assert outcome.action == "noop"
    assert len(_history(second)) == before


def test_every_recorded_position_is_unique_per_run() -> None:
    task, ctx = _linked()
    for index, event_type in enumerate(("run.created", "run.planning", "run.running"), start=1):
        task, _ = _apply(
            task, ctx, _frame(event_type, event_id=f"ev-{index}", sequence=index)
        )

    sequences = [entry["sequence"] for entry in _history(task)]
    assert sequences == sorted(set(sequences))


# --- no raw payload, stable order ------------------------------------------

_HOSTILE_PAYLOAD = {
    "prompt": "原始提示 DO-NOT-LEAK",
    "provider_raw": {"object": "DO-NOT-LEAK"},
    "api_key": "sk-live-DO-NOT-LEAK",
    "traceback": "Traceback DO-NOT-LEAK",
    "nested": {"deep": {"deeper": "DO-NOT-LEAK"}},
    "inputs": {"secret": "DO-NOT-LEAK"},
}


def test_a_record_carries_only_allowlisted_keys() -> None:
    task, ctx = _linked()
    frames = [
        _frame("run.created", event_id="e1", sequence=1, payload=_HOSTILE_PAYLOAD),
        _frame("run.waiting_approval", event_id="e2", sequence=2, payload=_HOSTILE_PAYLOAD),
        _frame("run.running", event_id="e3", sequence=3, payload=_HOSTILE_PAYLOAD),
        _frame("run.failed", event_id="e4", sequence=4, payload=_HOSTILE_PAYLOAD),
    ]
    for frame in frames:
        task, _ = _apply(task, ctx, frame)

    assert len(_history(task)) == len(frames)
    for entry in _history(task):
        assert set(entry) <= RECORD_KEYS, set(entry) - RECORD_KEYS


def test_no_raw_provider_payload_reaches_the_history() -> None:
    task, ctx = _linked()
    frames = [
        _frame("run.planning", event_id="e1", sequence=1, payload=_HOSTILE_PAYLOAD),
        _frame("run.failed", event_id="e2", sequence=2, payload=_HOSTILE_PAYLOAD),
        _frame(
            "asset.available",
            event_id="e3",
            sequence=3,
            payload={"asset": {"asset_id": "asset_1", **_HOSTILE_PAYLOAD}},
        ),
    ]
    for frame in frames:
        task, _ = _apply(task, ctx, frame)

    serialized = json.dumps(_history(task), ensure_ascii=False)
    assert "DO-NOT-LEAK" not in serialized
    for key in ("provider_raw", "api_key", "traceback", "prompt"):
        assert key not in serialized


@pytest.mark.parametrize(
    "order",
    [
        ("run.planning", "run.running", "run.completed"),
        ("run.running", "run.completed", "run.planning"),
        ("run.completed", "run.running", "run.planning"),
    ],
)
def test_the_final_status_does_not_depend_on_arrival_order(order: tuple[str, ...]) -> None:
    task, ctx = _linked()
    for index, event_type in enumerate(order, start=1):
        task, _ = _apply(task, ctx, _frame(event_type, event_id=f"ev-{event_type}", sequence=index))

    assert task["status"] == "completed", f"order {order} must converge"
    assert len({entry["sequence"] for entry in _history(task)}) == len(_history(task))
