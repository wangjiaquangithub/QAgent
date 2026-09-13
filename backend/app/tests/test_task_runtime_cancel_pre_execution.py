"""Protective tests for cancelling a task before its run has executed.

Covers AG-G2-AUTO-023: a cancellation recorded while the run is still queued,
planning or waiting for an approval keeps the task cancelled, a later or stale
frame cannot revive it, a refusal from the Runtime is never recorded as a
cancellation, and no approval state is invented.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.gateway.task_runtime_cancel import cancel_linked_runtime_run
from app.gateway.task_runtime_context import build_task_runtime_context
from app.gateway.task_runtime_event_bridge import apply_runtime_event
from app.gateway.task_runtime_linkage import link_runtime_run
from app.gateway.task_runtime_projection import HISTORY_FIELD, project_runtime_status

ORG_A = "org-a"
TASK_ID = "e4c1a2b0"
RUN_ID = "run-1"

# The statuses the card names: the run exists but has not executed yet.
PRE_EXECUTION_STATUSES = ("queued", "planning", "waiting_approval")


class AcceptedContract:
    """The Runtime's real answer shape when the cancellation is accepted."""

    def __init__(self) -> None:
        self.cancel_calls: list[str] = []

    async def request_cancel(self, run_id: str) -> dict[str, Any]:
        self.cancel_calls.append(run_id)
        return {"run_id": run_id, "status": "cancelled"}


class RefusingContract:
    """The Runtime's real answer shape when the run had already settled."""

    def __init__(self, status: Any) -> None:
        self.cancel_calls: list[str] = []
        self._status = status

    async def request_cancel(self, run_id: str) -> dict[str, Any]:
        self.cancel_calls.append(run_id)
        return {"run_id": run_id, "status": self._status}


def _authz() -> dict[str, Any]:
    return {
        "org_id": ORG_A,
        "principal": {"principal_id": "webui:1", "principal_type": "internal"},
        "scope_id": "personal:webui:1",
        "is_org_admin": False,
    }


def _task(*, status: str = "pending", **extra: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": TASK_ID,
        "name": "每周经营简报",
        "run_mode": "unattended",
        "status": status,
        "unattended_attempts": 0,
        HISTORY_FIELD: [],
    }
    task.update(extra)
    return task


def _ctx(*, status: str = "pending") -> Any:
    return build_task_runtime_context(task=_task(status=status), authz=_authz(), authorized=True)


def _linked(*, status: str = "pending") -> tuple[dict[str, Any], Any]:
    ctx = _ctx(status=status)
    task, _ = link_runtime_run(_task(status=status), context=ctx, runtime_run_id=RUN_ID)
    return task, ctx


def _history(task: dict[str, Any]) -> list[dict[str, Any]]:
    return list(task.get(HISTORY_FIELD) or [])


def _frame(status: str, *, run_id: str = RUN_ID, sequence: int) -> dict[str, Any]:
    return {
        "event_id": f"ev-{status}-{sequence}",
        "run_id": run_id,
        "sequence": sequence,
        "type": f"run.{status}",
        "payload": {},
    }


async def _record_cancellation(task: dict[str, Any], ctx: Any) -> dict[str, Any]:
    outcome = await cancel_linked_runtime_run(task, context=ctx, contract=AcceptedContract())
    assert outcome.action == "runtime_cancelled"
    assert outcome.updated_task is not None
    return outcome.updated_task


# --- a cancellation before execution sticks -------------------------------


@pytest.mark.parametrize("upstream", PRE_EXECUTION_STATUSES)
async def test_a_pre_execution_cancellation_is_recorded(upstream: str) -> None:
    task, ctx = _linked()
    outcome = await cancel_linked_runtime_run(
        task, context=ctx, contract=AcceptedContract()
    )

    assert outcome.action == "runtime_cancelled"
    record = _history(outcome.updated_task or {})[-1]
    assert record["runtime_status"] == "cancelled"
    assert record["runtime_run_id"] == RUN_ID


@pytest.mark.parametrize("upstream", PRE_EXECUTION_STATUSES)
async def test_a_later_running_frame_cannot_revive_a_cancelled_task(upstream: str) -> None:
    task, ctx = _linked(status="pending")
    cancelled = await _record_cancellation(task, ctx)

    after, projection = project_runtime_status(
        cancelled, context=ctx, runtime_status="running", sequence=7
    )

    assert projection.action == "stale"
    assert projection.reason == "task_cancellation_recorded"
    assert after["status"] == "pending", "the task must not be pulled into executing"


async def test_the_guard_is_what_stops_the_resurrection() -> None:
    # Without the cancellation record the very same frame does move the task, so
    # the test above is not passing for some unrelated reason.
    task, ctx = _linked(status="pending")

    after, projection = project_runtime_status(
        task, context=ctx, runtime_status="running", sequence=7
    )

    assert projection.action == "status_updated"
    assert after["status"] == "executing"


@pytest.mark.parametrize("stale_status", ["created", "queued", "planning", "waiting_approval"])
async def test_a_stale_pre_cancel_frame_cannot_revive_the_task(stale_status: str) -> None:
    task, ctx = _linked()
    cancelled = await _record_cancellation(task, ctx)

    after, projection = project_runtime_status(
        cancelled, context=ctx, runtime_status=stale_status, sequence=1
    )

    assert projection.action == "stale"
    assert after["status"] == "pending"


async def test_an_old_event_arriving_late_cannot_revive_the_task() -> None:
    task, ctx = _linked()
    cancelled = await _record_cancellation(task, ctx)

    after, outcome = apply_runtime_event(
        cancelled, context=ctx, event=_frame("running", sequence=99)
    )

    assert outcome.action == "stale"
    assert after["status"] == "pending"

    # And a repeat of the same late frame changes nothing either.
    again, repeat = apply_runtime_event(after, context=ctx, event=_frame("running", sequence=99))
    assert repeat.action in {"stale", "noop"}
    assert again["status"] == "pending"


async def test_a_completion_after_the_cancellation_does_not_overwrite_it() -> None:
    task, ctx = _linked()
    cancelled = await _record_cancellation(task, ctx)

    after, projection = project_runtime_status(
        cancelled, context=ctx, runtime_status="completed", sequence=50
    )

    assert projection.action == "noop"
    assert projection.reason == "cancellation_already_recorded"
    assert after["status"] == "pending"
    assert [e["runtime_status"] for e in _history(after)] == ["cancelled"]


async def test_a_cancelled_frame_converges_without_a_second_record() -> None:
    task, ctx = _linked()
    cancelled = await _record_cancellation(task, ctx)

    after, projection = project_runtime_status(
        cancelled, context=ctx, runtime_status="cancelled", sequence=51
    )

    assert projection.action == "status_updated"
    assert projection.reason == "cancellation_converged"
    assert after["status"] == "cancelled"
    assert len(_history(after)) == 1, "the cancellation must not be recorded twice"


async def test_a_second_convergence_is_a_noop() -> None:
    task, ctx = _linked()
    cancelled = await _record_cancellation(task, ctx)
    converged, _ = project_runtime_status(
        cancelled, context=ctx, runtime_status="cancelled", sequence=51
    )

    after, projection = project_runtime_status(
        converged, context=ctx, runtime_status="cancelled", sequence=52
    )

    assert projection.action == "noop"
    assert projection.reason in {"cancellation_already_recorded", "terminal_already_reached"}
    assert len(_history(after)) == 1


async def test_an_already_terminal_task_keeps_its_existing_behaviour() -> None:
    # The status-based guard still wins when the caller already moved the task, so
    # the existing local cancel semantics are unchanged.
    task, ctx = _linked(status="pending")
    cancelled = await _record_cancellation(task, ctx)
    locally_cancelled = dict(cancelled) | {"status": "cancelled"}

    after, projection = project_runtime_status(
        locally_cancelled, context=ctx, runtime_status="completed", sequence=60
    )

    assert projection.action == "noop"
    assert projection.reason == "terminal_already_reached"
    assert after["status"] == "cancelled"


# --- a refusal is never recorded as a cancellation ------------------------


@pytest.mark.parametrize("reported", ["completed", "failed", "timed_out"])
async def test_a_refused_cancellation_is_not_recorded_as_one(reported: str) -> None:
    task, ctx = _linked()
    contract = RefusingContract(reported)

    outcome = await cancel_linked_runtime_run(task, context=ctx, contract=contract)

    assert outcome.action == "runtime_refused"
    assert outcome.used_runtime is False
    assert outcome.touched_runtime is True
    assert outcome.reason == f"runtime_reported_{reported}"
    assert contract.cancel_calls == [RUN_ID]
    assert outcome.updated_task is not None
    record = _history(outcome.updated_task)[-1]
    assert record["runtime_status"] == reported, "the task must show what the Runtime said"
    assert record["runtime_run_id"] == RUN_ID
    assert [e["runtime_status"] for e in _history(outcome.updated_task)] != ["cancelled"]


async def test_a_refusal_leaves_the_task_status_untouched() -> None:
    task, ctx = _linked()
    outcome = await cancel_linked_runtime_run(
        task, context=ctx, contract=RefusingContract("completed")
    )

    assert outcome.updated_task is not None
    assert outcome.updated_task["status"] == task["status"]


async def test_a_refused_cancellation_does_not_block_a_later_real_one() -> None:
    task, ctx = _linked()
    refused = await cancel_linked_runtime_run(
        task, context=ctx, contract=RefusingContract("completed")
    )
    assert refused.updated_task is not None

    # Nothing was recorded as cancelled, so a subsequent accepted cancel still
    # records one: the refusal is not mistaken for a completed cancellation.
    accepted = await cancel_linked_runtime_run(
        refused.updated_task, context=ctx, contract=AcceptedContract()
    )

    assert accepted.action == "runtime_cancelled"
    assert _history(accepted.updated_task or {})[-1]["runtime_status"] == "cancelled"


@pytest.mark.parametrize("reported", ["something-else", "Cancelled!", 7])
async def test_an_unrecognised_refusal_state_is_not_copied_into_history(
    reported: Any,
) -> None:
    task, ctx = _linked()

    outcome = await cancel_linked_runtime_run(
        task, context=ctx, contract=RefusingContract(reported)
    )

    assert outcome.action == "runtime_refused"
    assert outcome.reason == "runtime_reported_unknown_status"
    assert outcome.updated_task is None, "nothing explainable to record, so nothing is recorded"
    assert _history(task) == []


async def test_a_refusal_does_not_pretend_the_request_was_cancelled() -> None:
    task, ctx = _linked()
    outcome = await cancel_linked_runtime_run(
        task, context=ctx, contract=RefusingContract("completed")
    )

    assert outcome.action != "runtime_cancelled"
    assert outcome.used_runtime is False


# --- no approval state is invented ----------------------------------------


@pytest.mark.parametrize("upstream", PRE_EXECUTION_STATUSES)
async def test_waiting_for_an_approval_cancels_without_inventing_an_approval_state(
    upstream: str,
) -> None:
    task, ctx = _linked()
    outcome = await cancel_linked_runtime_run(
        task, context=ctx, contract=AcceptedContract()
    )
    assert outcome.updated_task is not None

    records = _history(outcome.updated_task)
    assert records, "the cancellation must be recorded"
    for record in records:
        status = str(record.get("runtime_status") or "")
        assert not status.startswith("approval"), "no approval state may be invented"
        assert record.get("approval_required") is False
    assert all(key != "approval" for record in records for key in record)


async def test_a_refusal_invents_no_approval_state_either() -> None:
    task, ctx = _linked()
    outcome = await cancel_linked_runtime_run(
        task, context=ctx, contract=RefusingContract("completed")
    )
    assert outcome.updated_task is not None

    for record in _history(outcome.updated_task):
        assert not str(record.get("runtime_status") or "").startswith("approval")
    assert "approval" not in outcome.updated_task
