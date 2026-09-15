"""Boundary tests for the Runtime -> Task Center write-back path.

AG-G2-AUTO-004-A01.

`AG-G2-AUTO-003-A01` wired the projection: the queue tick now reads an
already-linked run and writes the run's own state back onto the task row. This
file adds the negative cases that path needs, and nothing else — no production
code is touched and no product behaviour is added.

What is pinned here:

* **stage coverage** — ``running`` / ``completed`` / ``failed`` / ``cancelled``
  each land on the frozen mapping, and a repeated tick for the same stage writes
  nothing a second time;
* **monotonicity** — a terminal task is never overwritten by a later terminal,
  and a late or out-of-order status (an older stage re-read, or an older
  sequence) never moves the task backwards;
* **redaction** — a failure summary reaches ``execution_history`` only through
  the existing sanitisers, and nothing the write-back path writes can carry a
  provider payload;
* **an unusable linkage is not "no linkage"** — a row whose linkage cannot be
  decoded must not be treated as an unlinked task, which is how a second run
  gets created;
* **a failed Runtime read is not a terminal answer** — it must neither fabricate
  a terminal state nor leave the task permanently claimed as running, and the
  next readable tick must converge it;
* **the switch** — with the opt-in off, no Runtime call happens at all.

Where a case is asserted at the projection boundary rather than through the tick,
the reason is stated in the test: the reconciliation wrapper the tick uses
carries the run's *status* only, so error/result payload handling is only
reachable — and only assertable — at the projection it calls. That is recorded as
a gap in the card verdict, not papered over.

The Runtime is always replaced at its public boundary by a recording fake; no
key is read and no paid call can happen.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from _runtime_flow_support import (
    LINKAGE_TASK_KEY,
    RUN_ID,
    SWITCH,
    FakeRuntime,
    block_outbound_network,
    build_client,
    history_of,
    park_other_unattended_tasks,
    queue_tick_via_route,
    reset_home,
    run_contract,
    save_task,
    schedule_a_run_via_route,
    stored_task,
)

QUEUE_SWITCH = "EVOFLOW_TASK_QUEUE_ENABLED"
ORG = "local"
OWNER = "local-admin"


@pytest.fixture()
def client():
    reset_home()
    return build_client()


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(SWITCH, "1")
    monkeypatch.delenv(QUEUE_SWITCH, raising=False)
    block_outbound_network(monkeypatch)


def _settle(fake: FakeRuntime, status: str) -> FakeRuntime:
    fake._status = status
    return fake


def _tick(client, task_id: str) -> dict[str, Any]:
    park_other_unattended_tasks(task_id)
    return queue_tick_via_route(client)


def _projection_for(tick_result: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    for entry in tick_result.get("results") or []:
        if str(entry.get("task_id") or "") != task_id:
            continue
        if str(entry.get("action") or "") == "runtime_projected":
            return dict(entry)
    return None


def _identity() -> dict[str, Any]:
    return {
        "org_id": ORG,
        "scope_id": f"personal:{OWNER}",
        "principal": {"principal_id": OWNER, "principal_type": "internal"},
    }


def _context(task: dict[str, Any]):
    from app.gateway.task_runtime_context import build_task_runtime_context

    return build_task_runtime_context(task=task, authz=_identity(), authorized=True)


class UnreadableRuntime(FakeRuntime):
    """A Runtime whose status read fails, the way a deleted run or a down store does."""

    def __init__(self, *, error: Exception, status: str = "completed") -> None:
        super().__init__(status=status)
        self._error = error

    async def get_run_status(self, run_id: str) -> dict[str, Any]:
        self.status_calls.append(run_id)
        raise self._error


class LeakyRuntime(FakeRuntime):
    """A Runtime whose status answer carries a provider payload nobody may store."""

    async def get_run_status(self, run_id: str) -> dict[str, Any]:
        self.status_calls.append(run_id)
        return {
            "run_id": run_id,
            "status": self._status,
            "org_id": ORG,
            "error": {
                "code": "provider_error",
                "message": "Traceback (most recent call last): File \"/srv/app/x.py\", line 42",
            },
            "result": {"summary": "ok", "internal_path": "/var/private/report.csv"},
        }


# --- stage coverage ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("runtime_status", "expected_task_status"),
    [
        ("running", "executing"),
        ("completed", "completed"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
    ],
)
def test_each_settled_stage_lands_on_the_frozen_mapping(
    client, monkeypatch: pytest.MonkeyPatch, runtime_status: str, expected_task_status: str
) -> None:
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    _settle(fake, runtime_status)

    _tick(client, task_id)

    stored = stored_task(task_id)
    assert stored["status"] == expected_task_status
    assert history_of(stored)[-1]["runtime_status"] == runtime_status


def test_timed_out_converges_the_task_to_failed_and_keeps_its_own_word(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    _settle(fake, "timed_out")

    _tick(client, task_id)

    stored = stored_task(task_id)
    assert stored["status"] == "failed"
    assert history_of(stored)[-1]["runtime_status"] == "timed_out"


# --- idempotence of a repeated tick ------------------------------------------------


@pytest.mark.parametrize("runtime_status", ["running", "completed", "failed", "cancelled"])
def test_a_repeated_tick_for_the_same_stage_writes_nothing_again(
    client, monkeypatch: pytest.MonkeyPatch, runtime_status: str
) -> None:
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    _settle(fake, runtime_status)

    _tick(client, task_id)
    after_first = history_of(stored_task(task_id))
    _tick(client, task_id)
    _tick(client, task_id)

    assert history_of(stored_task(task_id)) == after_first, (
        "a repeated tick must not append a second record for the same stage"
    )


def test_a_repeated_terminal_tick_does_not_append_a_second_terminal_record(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    _settle(fake, "completed")

    _tick(client, task_id)
    records = history_of(stored_task(task_id))
    terminal_records = [r for r in records if r.get("runtime_status") == "completed"]
    assert len(terminal_records) == 1

    _tick(client, task_id)

    again = history_of(stored_task(task_id))
    assert [r for r in again if r.get("runtime_status") == "completed"] == terminal_records


# --- monotonicity ------------------------------------------------------------------


def test_a_later_terminal_never_overwrites_an_earlier_one(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    _settle(fake, "completed")
    _tick(client, task_id)
    settled = history_of(stored_task(task_id))

    for later in ("failed", "cancelled", "timed_out"):
        _settle(fake, later)
        _tick(client, task_id)
        assert stored_task(task_id)["status"] == "completed", f"{later} overwrote completed"
        assert history_of(stored_task(task_id)) == settled, f"{later} appended a record"


def test_a_late_older_status_does_not_move_the_task_backwards(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A status re-read that is behind what the run already reported is refused.

    The tick's reconciliation carries no sequence, so this is the out-of-order
    case the write-back path can actually be handed: the Runtime answers an
    earlier stage than the one already projected.
    """
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    _settle(fake, "running")
    _tick(client, task_id)
    assert stored_task(task_id)["status"] == "executing"
    at_running = history_of(stored_task(task_id))

    _settle(fake, "queued")
    _tick(client, task_id)

    stored = stored_task(task_id)
    assert stored["status"] == "executing"
    assert history_of(stored) == at_running


def test_an_older_sequence_is_refused_at_the_projection_boundary(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sequence guard, asserted where a sequence exists.

    The reconciliation wrapper the tick uses projects a status without a
    sequence, so the stream position is only observable on the projection it
    calls. This pins that guard rather than assuming it.
    """
    from app.gateway.task_runtime_projection import project_runtime_status

    task_id, _ = schedule_a_run_via_route(client, monkeypatch)

    task = stored_task(task_id)
    updated, outcome = project_runtime_status(
        task, context=_context(task), runtime_status="planning", sequence=5
    )
    assert outcome.action in {"status_updated", "history_only"}, outcome
    save_task(task_id, updated)

    task = stored_task(task_id)
    before = history_of(task)
    # A *later* stage carrying an *older* stream position: without the sequence
    # guard this would move the task forward.
    same_run, outcome = project_runtime_status(
        task, context=_context(task), runtime_status="running", sequence=2
    )

    assert outcome.action == "stale"
    assert outcome.reason == "older_sequence", outcome.reason
    assert history_of(same_run) == before


# --- redaction ---------------------------------------------------------------------


def test_a_failure_summary_is_redacted_at_the_projection_boundary(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``failed`` / ``timed_out`` reach history only through the existing sanitisers.

    The reconciliation wrapper carries the run's status only, so the error fields
    are asserted at the projection it calls — which is the one place that can
    write them, and which sanitises them itself rather than trusting its caller.
    """
    from app.gateway.task_runtime_projection import project_runtime_status

    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    _settle(fake, "running")
    _tick(client, task_id)

    for runtime_status in ("failed", "timed_out"):
        task = stored_task(task_id)
        updated, outcome = project_runtime_status(
            task,
            context=_context(task),
            runtime_status=runtime_status,
            error_code="provider_error",
            reason='Traceback (most recent call last): File "/srv/app/x.py", line 42',
        )
        assert outcome.action in {"status_updated", "history_only"}, outcome
        save_task(task_id, updated)

        record = history_of(stored_task(task_id))[-1]
        assert record["runtime_status"] == runtime_status
        assert record["error_code"] == "provider_error"
        assert "reason" not in record, "a leaking message must be dropped, not truncated"
        assert record.get("error_detail_redacted") is True
        rendered = json.dumps(stored_task(task_id), ensure_ascii=False)
        assert "/srv/app" not in rendered
        assert "Traceback" not in rendered

        # Leave the task able to accept the next stage of this test.
        healed = stored_task(task_id)
        healed["status"] = "executing"
        save_task(task_id, healed)


def test_the_write_back_path_never_stores_a_provider_payload(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a Runtime answer full of internal detail must not reach the row."""
    task_id, _ = schedule_a_run_via_route(client, monkeypatch)
    fake = run_contract(LeakyRuntime(status="failed"), monkeypatch)

    _tick(client, task_id)

    rendered = json.dumps(stored_task(task_id), ensure_ascii=False)
    assert "/var/private/report.csv" not in rendered
    assert "/srv/app/x.py" not in rendered
    assert "Traceback" not in rendered
    assert fake.status_calls, "the run must have been read for this to mean anything"


# --- an unusable linkage is not "no linkage" ---------------------------------------


def test_an_undecodable_linkage_is_not_treated_as_an_unlinked_task(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure mode this guards: "no linkage" would authorise a second run."""
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    creates_before = len(fake.create_calls)

    broken = stored_task(task_id)
    broken[LINKAGE_TASK_KEY] = "not-a-linkage-at-all"
    save_task(task_id, broken)

    result = _tick(client, task_id)

    assert len(fake.create_calls) == creates_before, "a broken linkage must not create a run"
    entry = _projection_for(result, task_id)
    assert entry is not None, "a broken linkage must be reported, not silently skipped"
    assert entry["runtime_projection"] == "runtime_unavailable"
    assert entry["runtime_projection_reason"] == "linkage_malformed"
    assert entry["runtime_run_id"] is None, "a foreign run id must not be disclosed"
    assert stored_task(task_id)["status"] == "pending"


def test_a_linkage_missing_a_field_is_also_refused(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    creates_before = len(fake.create_calls)

    broken = stored_task(task_id)
    broken[LINKAGE_TASK_KEY] = {"runtime_run_id": RUN_ID}
    save_task(task_id, broken)

    result = _tick(client, task_id)

    assert len(fake.create_calls) == creates_before
    entry = _projection_for(result, task_id)
    assert entry is not None
    assert entry["runtime_projection"] == "runtime_unavailable"


# --- a failed Runtime read is not a terminal answer --------------------------------


def test_an_unreadable_runtime_neither_fabricates_nor_strands_the_task(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run is mid-flight, the Runtime goes unreadable, then comes back.

    Two things must hold: the failure must not be turned into a terminal state
    nobody reported, and it must not leave the task permanently claimed as
    running — the next readable tick converges it.
    """
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    _settle(fake, "running")
    _tick(client, task_id)
    assert stored_task(task_id)["status"] == "executing"
    at_running = history_of(stored_task(task_id))

    failing = UnreadableRuntime(error=KeyError(RUN_ID), status="completed")
    run_contract(failing, monkeypatch)
    result = _tick(client, task_id)

    entry = _projection_for(result, task_id)
    assert entry is not None, "an unreadable Runtime must be reported, not swallowed"
    assert entry["runtime_projection"] == "runtime_unavailable"
    assert entry["runtime_projection_reason"] == "run_missing"
    stored = stored_task(task_id)
    assert stored["status"] == "executing", "a read failure must not fabricate a terminal state"
    assert history_of(stored) == at_running

    healthy = run_contract(FakeRuntime(status="completed"), monkeypatch)
    _tick(client, task_id)

    assert stored_task(task_id)["status"] == "completed", (
        "a failed read must not strand the task as running forever"
    )
    assert healthy.status_calls


def test_an_unavailable_runtime_is_reported_as_retryable(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.gateway.task_runtime_degrade import is_retryable_reason

    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    _settle(fake, "running")
    _tick(client, task_id)

    failing = UnreadableRuntime(error=RuntimeError("store down"), status="completed")
    run_contract(failing, monkeypatch)
    entry = _projection_for(_tick(client, task_id), task_id)

    assert entry is not None
    assert entry["runtime_projection"] == "runtime_unavailable"
    assert entry["runtime_projection_reason"] == "runtime_unavailable"
    assert is_retryable_reason(entry["runtime_projection_reason"]) is True


# --- the switch --------------------------------------------------------------------


def test_with_the_switch_off_no_runtime_call_happens_and_nothing_is_written(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    _settle(fake, "completed")
    before_row = stored_task(task_id)
    before_calls = (len(fake.create_calls), len(fake.status_calls))
    monkeypatch.delenv(SWITCH, raising=False)

    result = _tick(client, task_id)

    assert _projection_for(result, task_id) is None
    assert stored_task(task_id) == before_row
    assert (len(fake.create_calls), len(fake.status_calls)) == before_calls


def test_the_write_back_path_cannot_create_a_run_even_when_it_fails(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No path in the write-back may fall back to creating work."""
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    creates_before = len(fake.create_calls)
    fake._status = "running"
    _tick(client, task_id)

    failing = UnreadableRuntime(error=RuntimeError("store down"), status="completed")
    run_contract(failing, monkeypatch)
    _tick(client, task_id)
    _tick(client, task_id)

    assert len(failing.create_calls) == 0
    assert len(fake.create_calls) == creates_before
    assert failing.cancel_calls == []
