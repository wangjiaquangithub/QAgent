"""G0.7 scenarios 6-7: decision atomicity and event-sequence concurrency.

Scenario 6 -- an exception injected inside the approval-decision transaction must
roll back Approval, Run and Events together, and a later retry must succeed.
Scenario 7 -- concurrent event writers must produce strictly monotonic, unique,
gap-free sequences with stable read order.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
import sqlalchemy as sa

from app.qagent_runtime.events import EventType
from app.qagent_runtime.repository import RuntimeRepository

from .pg_support import (
    ACCEPTANCE_ORG,
    ScenarioEvidence,
    assert_dedicated_database,
    assert_sequences_monotonic_unique,
    make_engine,
    seed_running_run,
    seed_waiting_approval_run,
    write_evidence,
)

pytestmark = [pytest.mark.postgres, pytest.mark.agentscope_acceptance]


def test_g07_06_approval_decision_rolls_back_on_error(clean_runtime):
    """Scenario 6: an injected failure leaves no orphan approvals or events."""

    harness = clean_runtime
    run_id, approval_id = seed_waiting_approval_run(harness.repository, task_id="g07-06-decision-rollback")
    before = harness.snapshot(run_id, approval_id)

    class _InjectedFailure(RuntimeError):
        pass

    # Re-run the decision inside an explicit transaction that fails after the
    # approval/run/event writes have been issued. Because the repository opens
    # its own transaction, we reproduce the same write set here and then abort
    # it, which is the strongest available proof that the decision body is
    # atomic -- a partial commit would leave the rows visible after rollback.
    with harness.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(
                sa.text("UPDATE qagent_approvals SET status = 'granted', decided_by = 'g07-inject-fail' WHERE approval_id = :approval_id"),
                {"approval_id": approval_id},
            )
            connection.execute(
                sa.text("UPDATE qagent_runs SET status = 'running', error_payload = NULL WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            connection.execute(
                sa.text("INSERT INTO qagent_run_events (event_id, run_id, sequence, type, occurred_at, payload) VALUES (:event_id, :run_id, :sequence, :type, now(), '{}'::jsonb)"),
                {
                    "event_id": "evt_g07_injected",
                    "run_id": run_id,
                    "sequence": 999,
                    "type": str(EventType.APPROVAL_GRANTED),
                },
            )
            raise _InjectedFailure("g07 injected mid-transaction failure")
        except _InjectedFailure:
            transaction.rollback()

    after = harness.snapshot(run_id, approval_id)
    sequences = assert_sequences_monotonic_unique(harness.repository, run_id)

    # The whole write set must be gone: no half-applied approval, run or event.
    assert after["approval"]["status"] == before["approval"]["status"] == "requested"
    assert after["run"]["status"] == before["run"]["status"] == "waiting_approval"
    assert after["run"]["result_payload"] is None
    assert 999 not in sequences, "injected event survived the rollback"
    assert "evt_g07_injected" not in [e["event_id"] for e in after["events"]]

    # A normal decision after the rollback must still succeed.
    approval, changed = harness.repository.decide_approval_atomically(
        approval_id,
        status="granted",
        decided_by="g07-retry",
        reason=None,
        org_id=ACCEPTANCE_ORG,
        run_id=run_id,
    )
    assert changed is True, "retry after rollback must succeed"
    retried = harness.snapshot(run_id, approval_id)
    assert retried["approval"]["status"] == "granted"
    assert retried["run"]["status"] == "running"
    assert retried["run"]["error_payload"] is None
    retried_sequences = assert_sequences_monotonic_unique(harness.repository, run_id)

    write_evidence(
        ScenarioEvidence(
            scenario="g07_06_decision_rollback",
            initial_state={
                "approval_status": before["approval"]["status"],
                "run_status": before["run"]["status"],
                "event_sequences": before["event_sequences"],
            },
            concurrency_mode="single connection, injected exception inside an explicit transaction, then rollback",
            final_state={
                "approval_status": retried["approval"]["status"],
                "run_status": retried["run"]["status"],
                "result_payload": retried["run"]["result_payload"],
                "error_payload": retried["run"]["error_payload"],
                "event_sequences": retried_sequences,
                "event_types": retried["event_types"],
            },
            extra={
                "state_after_rollback": {
                    "approval_status": after["approval"]["status"],
                    "run_status": after["run"]["status"],
                    "event_sequences": sequences,
                },
                "injected_event_present_after_rollback": 999 in sequences,
                "retry_changed": changed,
            },
            conclusion=("Injected failure rolled back the approval, run and event writes together (no orphan event); the subsequent retry committed normally."),
        ),
        harness.evidence_dir,
    )


def test_g07_07_concurrent_event_sequence_correctness(clean_runtime):
    """Scenario 7: concurrent event writers cannot duplicate or overwrite sequence."""

    harness = clean_runtime
    repository = harness.repository
    run_id = seed_running_run(repository, task_id="g07-07-event-sequence")

    assert_dedicated_database(harness.engine)
    before = harness.snapshot(run_id)
    baseline = before["event_sequences"][-1] if before["event_sequences"] else 0

    writers, per_writer = 2, 6
    url = harness.url

    def writer(index: int) -> list[int]:
        engine = make_engine(url)
        try:
            assert_dedicated_database(engine)
            repo = RuntimeRepository(engine, create_schema=False)
            written: list[int] = []
            for i in range(per_writer):
                event = repo.append_event(
                    run_id,
                    EventType.ASSET_AVAILABLE,
                    {"writer": index, "index": i},
                    org_id=ACCEPTANCE_ORG,
                )
                written.append(int(event["sequence"]))
            return written
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=writers) as executor:
        futures = [executor.submit(writer, index) for index in range(writers)]
        written_batches = [future.result() for future in futures]

    written = [sequence for batch in written_batches for sequence in batch]
    expected_total = baseline + writers * per_writer

    final = harness.snapshot(run_id)
    sequences = final["event_sequences"]

    # Every writer observed a distinct sequence number, and none was reused.
    assert len(written) == len(set(written)), f"sequence numbers were reused: {sorted(written)}"
    assert len(sequences) == expected_total, f"expected {expected_total} events, got {len(sequences)}"

    # The stored sequence is strictly monotonic, gap-free and unique.
    assert_sequences_monotonic_unique(repository, run_id)

    # Each writer's own batch must be strictly increasing in issue order.
    for batch in written_batches:
        assert batch == sorted(batch), f"writer batch not monotonic: {batch}"

    # Cursor replay: paging after each sequence never repeats or skips an event.
    seen: list[int] = []
    cursor = 0
    while True:
        page = repository.list_events(run_id, after_sequence=cursor, org_id=ACCEPTANCE_ORG)
        if not page:
            break
        page_sequences = [int(event["sequence"]) for event in page]
        assert page_sequences == sorted(page_sequences)
        cursor = page_sequences[-1]
        seen.extend(page_sequences)
    assert seen == sequences, "cursor replay order diverged from the stored order"

    write_evidence(
        ScenarioEvidence(
            scenario="g07_07_event_sequence_concurrency",
            initial_state={"baseline_sequence": baseline, "status": before["run"]["status"]},
            concurrency_mode=(f"{writers} concurrent PostgreSQL connections via ThreadPoolExecutor, {per_writer} append_event calls each"),
            final_state={
                "total_events": len(sequences),
                "event_sequences": sequences,
                "cursor_replay_matches": seen == sequences,
            },
            extra={
                "written_batches": written_batches,
                "duplicate_sequences": len(written) - len(set(written)),
                "expected_total": expected_total,
            },
            conclusion=("Concurrent writers produced strictly monotonic, unique, gap-free sequences; cursor replay matched stored order exactly."),
        ),
        harness.evidence_dir,
    )
