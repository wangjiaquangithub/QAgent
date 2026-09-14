"""Protective tests for the per-run Task Center stream cursor.

Covers AG-G2-AUTO-018: the cursor lives in the task row's existing extras slot
with no schema change, it only ever moves forward, a new attempt starts a new
stream, and a cursor belonging to another organization or task is refused.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.gateway.task_runtime_context import build_task_runtime_context
from app.gateway.task_runtime_cursor import (
    CURSOR_TASK_KEY,
    RuntimeCursorError,
    advance_runtime_cursor,
    applied_watermark,
    read_runtime_cursor,
)
from app.gateway.task_runtime_event_bridge import apply_runtime_event
from app.gateway.task_runtime_linkage import LINKAGE_TASK_KEY, link_runtime_run
from app.gateway.task_runtime_projection import HISTORY_FIELD

ORG_A = "org-a"
ORG_B = "org-b"
TASK_ID = "e4c1a2b0"
RUN_ID = "run-1"


def _authz(org_id: str = ORG_A) -> dict[str, Any]:
    return {
        "org_id": org_id,
        "principal": {"principal_id": "webui:1", "principal_type": "internal"},
        "scope_id": "personal:webui:1",
        "is_org_admin": False,
    }


def _task(*, task_id: str = TASK_ID, **extra: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": task_id,
        "name": "每周经营简报",
        "run_mode": "unattended",
        "status": "pending",
        "unattended_attempts": 0,
        "execution_history": [],
    }
    task.update(extra)
    return task


def _ctx(org_id: str = ORG_A, *, task_id: str = TASK_ID, attempt: int = 0) -> Any:
    return build_task_runtime_context(
        task=_task(task_id=task_id, unattended_attempts=attempt),
        authz=_authz(org_id),
        authorized=True,
    )


def _linked(*, task_id: str = TASK_ID, org_id: str = ORG_A) -> tuple[dict[str, Any], Any]:
    ctx = _ctx(org_id, task_id=task_id)
    task, _ = link_runtime_run(_task(task_id=task_id), context=ctx, runtime_run_id=RUN_ID)
    return task, ctx


def _history(task: dict[str, Any]) -> list[dict[str, Any]]:
    return list(task.get(HISTORY_FIELD) or [])


# --- where it lives --------------------------------------------------------


def test_the_cursor_uses_the_existing_task_extras_slot() -> None:
    from evoflow.persistence.task_row_mappers import _TASK_KNOWN

    assert CURSOR_TASK_KEY not in _TASK_KNOWN

    task, ctx = _linked()
    advanced, cursor = advance_runtime_cursor(task, context=ctx, run_id=RUN_ID, sequence=3)

    from evoflow.persistence import task_row_mappers as m

    known, extra_json = m._split_extra(advanced, m._TASK_KNOWN)
    stored = json.loads(extra_json)
    assert stored[CURSOR_TASK_KEY]["last_sequence"] == 3
    assert cursor is not None and cursor.last_sequence == 3
    # The linkage and the cursor coexist in the same pre-existing slot.
    assert LINKAGE_TASK_KEY in stored


def test_an_absent_cursor_reads_as_none() -> None:
    task, ctx = _linked()
    assert read_runtime_cursor(task, context=ctx) is None


# --- monotonicity ----------------------------------------------------------


def test_the_cursor_only_moves_forward() -> None:
    task, ctx = _linked()
    advanced, _ = advance_runtime_cursor(task, context=ctx, run_id=RUN_ID, sequence=5)

    after, cursor = advance_runtime_cursor(
        advanced, context=ctx, run_id=RUN_ID, sequence=3, event_id="ev-old"
    )

    assert cursor is not None
    assert cursor.last_sequence == 5, "an older position must not pull the watermark back"
    assert read_runtime_cursor(after, context=ctx).last_sequence == 5


def test_advancing_without_a_position_changes_nothing() -> None:
    task, ctx = _linked()
    advanced, _ = advance_runtime_cursor(task, context=ctx, run_id=RUN_ID, sequence=4)

    after, cursor = advance_runtime_cursor(after_task := advanced, context=ctx, run_id=RUN_ID)

    assert cursor is not None and cursor.last_sequence == 4
    assert after == after_task


def test_a_new_attempt_replaces_the_cursor_instead_of_merging() -> None:
    # A retry supersedes the linkage, so the new run is a new stream and the
    # watermark starts over instead of merging with the previous run's.
    ctx = _ctx()
    task, _ = link_runtime_run(_task(), context=ctx, runtime_run_id="run-1")
    task, _ = advance_runtime_cursor(task, context=ctx, run_id="run-1", sequence=9)

    retry = _ctx(attempt=1)
    task, _ = link_runtime_run(task, context=retry, runtime_run_id="run-2", supersede=True)

    after, cursor = advance_runtime_cursor(task, context=retry, run_id="run-2", sequence=1)

    assert cursor is not None
    assert cursor.run_id == "run-2"
    assert cursor.last_sequence == 1, "a new run is a new stream"


def test_a_superseded_run_cannot_take_the_watermark_back() -> None:
    ctx = _ctx()
    task, _ = link_runtime_run(_task(), context=ctx, runtime_run_id="run-1")
    task, _ = advance_runtime_cursor(task, context=ctx, run_id="run-1", sequence=9)

    retry = _ctx(attempt=1)
    task, _ = link_runtime_run(task, context=retry, runtime_run_id="run-2", supersede=True)
    task, _ = advance_runtime_cursor(task, context=retry, run_id="run-2", sequence=1)

    # A late frame from the run the task no longer follows is refused, and the
    # live watermark is left exactly where it was.
    with pytest.raises(RuntimeCursorError, match="does not follow"):
        advance_runtime_cursor(task, context=retry, run_id="run-1", sequence=20)

    cursor = read_runtime_cursor(task, context=retry)
    assert cursor is not None and cursor.run_id == "run-2"
    assert cursor.last_sequence == 1


def test_advancing_for_a_run_the_task_never_linked_is_refused() -> None:
    task, ctx = _linked()

    with pytest.raises(RuntimeCursorError, match="does not follow"):
        advance_runtime_cursor(task, context=ctx, run_id="run-9", sequence=1)

    assert read_runtime_cursor(task, context=ctx) is None


def test_advancing_needs_a_linked_run_at_all() -> None:
    ctx = _ctx()
    unlinked = _task()

    with pytest.raises(RuntimeCursorError, match="does not follow"):
        advance_runtime_cursor(unlinked, context=ctx, run_id=RUN_ID, sequence=1)


def test_the_cursor_never_goes_backwards_through_the_projection() -> None:
    task, ctx = _linked()

    def _apply(current: dict[str, Any], sequence: int, event_type: str) -> dict[str, Any]:
        frame = {
            "event_id": f"ev-{event_type}-{sequence}",
            "run_id": RUN_ID,
            "sequence": sequence,
            "type": event_type,
            "payload": {},
        }
        updated, _ = apply_runtime_event(current, context=ctx, event=frame)
        return updated

    task = _apply(task, 5, "run.running")
    task = _apply(task, 2, "run.planning")

    cursor = read_runtime_cursor(task, context=ctx)
    assert cursor is not None and cursor.last_sequence == 5
    assert task["status"] == "executing", "the old position must not move the task"


# --- safety ----------------------------------------------------------------


def test_a_cursor_from_another_organization_is_refused() -> None:
    task, ctx = _linked(org_id=ORG_A)
    advanced, _ = advance_runtime_cursor(task, context=ctx, run_id=RUN_ID, sequence=2)

    with pytest.raises(RuntimeCursorError, match="another organization"):
        read_runtime_cursor(advanced, context=_ctx(ORG_B))


def test_a_cursor_on_another_task_is_refused() -> None:
    task, ctx = _linked()
    advanced, _ = advance_runtime_cursor(task, context=ctx, run_id=RUN_ID, sequence=2)

    copied = dict(advanced)
    copied["id"] = "deadbeef"

    with pytest.raises(RuntimeCursorError, match="does not belong to this task"):
        read_runtime_cursor(copied, context=ctx)


@pytest.mark.parametrize(
    "stored",
    [
        "not-a-mapping",
        {},
        {"run_id": RUN_ID},
        {"run_id": RUN_ID, "org_scope_key": "tc:org:org-a", "task_id": TASK_ID},
        {
            "run_id": RUN_ID,
            "org_scope_key": "tc:org:org-a",
            "task_id": TASK_ID,
            "last_sequence": "later",
        },
        {
            "run_id": RUN_ID,
            "org_scope_key": "tc:org:org-a",
            "task_id": TASK_ID,
            "last_sequence": -1,
        },
    ],
)
def test_a_malformed_cursor_is_refused(stored: Any) -> None:
    task, ctx = _linked()
    broken = _task(**{CURSOR_TASK_KEY: stored})

    with pytest.raises(RuntimeCursorError):
        read_runtime_cursor(broken, context=ctx)


def test_advancing_requires_a_trusted_context_and_a_run() -> None:
    task, ctx = _linked()
    with pytest.raises(RuntimeCursorError, match="trusted task runtime context"):
        advance_runtime_cursor(task, context="not-a-context", run_id=RUN_ID)  # type: ignore[arg-type]
    with pytest.raises(RuntimeCursorError, match="runtime_run_id is required"):
        advance_runtime_cursor(task, context=ctx, run_id="")


# --- the watermark ---------------------------------------------------------


def test_the_watermark_reports_the_furthest_justified_position() -> None:
    task, ctx = _linked()
    task, _ = advance_runtime_cursor(task, context=ctx, run_id=RUN_ID, sequence=4)

    assert applied_watermark(
        task, context=ctx, run_id=RUN_ID, history=_history(task)
    ) == 4


def test_the_watermark_falls_back_to_the_history_when_the_cursor_is_missing() -> None:
    task, ctx = _linked()
    history = [
        {"runtime_run_id": RUN_ID, "sequence": 7, "runtime_status": "running"},
        {"runtime_run_id": "run-9", "sequence": 99, "runtime_status": "running"},
    ]

    assert applied_watermark(task, context=ctx, run_id=RUN_ID, history=history) == 7


def test_the_watermark_is_none_when_nothing_was_applied() -> None:
    task, ctx = _linked()
    assert applied_watermark(task, context=ctx, run_id=RUN_ID, history=[]) is None
    assert applied_watermark(task, context=ctx, run_id="", history=[]) is None