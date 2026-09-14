"""G0.7 scenarios 1-2: approval decision races on real PostgreSQL.

Scenario 1 -- Grant vs Reject on the same ``org_id + run_id + approval_id``.
Scenario 2 -- Cancel vs Grant on the same run.

Both races run on two independent PostgreSQL backends (see
``pg_support.run_two_connection_race``) and assert the same invariant: the run
row lock in ``decide_approval_atomically`` / ``_transition`` is the single
serialization point, so exactly one writer wins and the Approval/Event state can
never disagree with the Run state.
"""

from __future__ import annotations

import pytest

from app.qagent_runtime.events import EventType

from .pg_support import (
    ACCEPTANCE_ORG,
    ScenarioEvidence,
    assert_sequences_monotonic_unique,
    run_two_connection_race,
    seed_waiting_approval_run,
    write_evidence,
)

pytestmark = [pytest.mark.postgres, pytest.mark.agentscope_acceptance]

# A run that was granted must be running; a rejected run must be failed.
# Any other pairing means the approval decision and the run transition diverged.
CONSISTENT_PAIRS = {("granted", "running"), ("rejected", "failed")}


def _decide(repository, approval_id, *, status, run_id):
    """Call the atomic decision and report it in a race-safe, picklable shape."""

    approval, changed = repository.decide_approval_atomically(
        approval_id,
        status=status,
        decided_by=f"g07-{status}",
        reason=f"g07 {status}",
        org_id=ACCEPTANCE_ORG,
        run_id=run_id,
    )
    return {"changed": changed, "approval_status": (approval or {}).get("status")}


def test_g07_01_grant_vs_reject_single_winner(clean_runtime):
    """Scenario 1: concurrent Grant and Reject produce exactly one decision."""

    harness = clean_runtime
    run_id, approval_id = seed_waiting_approval_run(harness.repository, task_id="g07-01-grant-vs-reject")
    initial = harness.snapshot(run_id, approval_id)

    outcomes = run_two_connection_race(
        url=harness.url,
        operations={
            "grant": lambda repo, engine: _decide(repo, approval_id, status="granted", run_id=run_id),
            "reject": lambda repo, engine: _decide(repo, approval_id, status="rejected", run_id=run_id),
        },
    )

    winners = [name for name, outcome in outcomes.items() if outcome.result["changed"]]
    pids = {name: outcome.pid for name, outcome in outcomes.items()}

    final = harness.snapshot(run_id, approval_id)
    approval_status = final["approval"]["status"]
    run_status = final["run"]["status"]
    sequences = assert_sequences_monotonic_unique(harness.repository, run_id)

    # Exactly one of the two competing decisions may commit.
    assert len(winners) == 1, f"expected exactly one winning decision, got {winners}"

    # Approval and Run must agree; a half-applied decision is a contradiction.
    assert (approval_status, run_status) in CONSISTENT_PAIRS, f"approval/run diverged: approval={approval_status} run={run_status}"

    # Both workers really were distinct PostgreSQL backends.
    assert len(set(pids.values())) == 2, f"race not multi-connection: {pids}"

    # No duplicate decision events, and no contradictory terminal marker.
    decision_events = [event for event in final["event_types"] if event in {EventType.APPROVAL_GRANTED, EventType.APPROVAL_REJECTED}]
    assert len(decision_events) == 1, f"duplicate decision events: {final['event_types']}"

    write_evidence(
        ScenarioEvidence(
            scenario="g07_01_grant_vs_reject",
            initial_state={"approval_status": initial["approval"]["status"], "run_status": initial["run"]["status"]},
            concurrency_mode="two PostgreSQL backends, Barrier(2), decide_approval_atomically(grant) vs (reject)",
            final_state={
                "approval_status": approval_status,
                "run_status": run_status,
                "event_sequences": sequences,
                "event_types": final["event_types"],
            },
            extra={
                "winners": winners,
                "changed_flags": {name: outcome.result["changed"] for name, outcome in outcomes.items()},
                "backend_pids": pids,
                "version": final["run"]["version"],
                "execution_epoch": final["run"]["execution_epoch"],
            },
            conclusion=(f"Exactly one approval decision committed; Approval and Run stayed consistent ({approval_status}/{run_status}); event sequence strictly monotonic and unique."),
        ),
        harness.evidence_dir,
    )


def test_g07_02_cancel_vs_grant_single_winner(clean_runtime):
    """Scenario 2: concurrent Cancel and Grant never both commit.

    The card asks for the *existing* semantics to be recorded rather than a
    particular winner. Whichever writer wins the run row lock, the two outcomes
    must not both report success, and a Grant that lost to Cancel must leave the
    approval in ``requested`` (a safe no-op) instead of half-applying.
    """

    harness = clean_runtime
    run_id, approval_id = seed_waiting_approval_run(harness.repository, task_id="g07-02-cancel-vs-grant")
    initial = harness.snapshot(run_id, approval_id)

    def cancel(repository, engine):
        changed = repository.transition(
            run_id,
            "cancelled",
            EventType.RUN_CANCELLED,
            {"reason": "g07 cancel"},
            from_statuses={"waiting_approval"},
            org_id=ACCEPTANCE_ORG,
        )
        return {"changed": bool(changed)}

    outcomes = run_two_connection_race(
        url=harness.url,
        operations={
            "cancel": cancel,
            "grant": lambda repo, engine: _decide(repo, approval_id, status="granted", run_id=run_id),
        },
    )

    cancel_won = bool(outcomes["cancel"].result["changed"])
    grant_won = bool(outcomes["grant"].result["changed"])

    final = harness.snapshot(run_id, approval_id)
    approval_status = final["approval"]["status"]
    run_status = final["run"]["status"]
    sequences = assert_sequences_monotonic_unique(harness.repository, run_id)

    # The two writers contend for one run row lock, so not both can succeed.
    assert not (cancel_won and grant_won), "cancel and grant both committed"

    if cancel_won:
        assert run_status == "cancelled"
        # Recorded current semantics: a Grant that lost to Cancel is a no-op and
        # the approval stays 'requested' rather than becoming 'granted'.
        assert approval_status == "requested", f"grant after cancel must not flip the approval, got {approval_status}"
        assert EventType.RUN_CANCELLED in final["event_types"]
    else:
        # Grant won: the run moved on, so Cancel must have been rejected.
        assert run_status == "running"
        assert approval_status == "granted"

    write_evidence(
        ScenarioEvidence(
            scenario="g07_02_cancel_vs_grant",
            initial_state={"approval_status": initial["approval"]["status"], "run_status": initial["run"]["status"]},
            concurrency_mode="two PostgreSQL backends, Barrier(2), transition(cancelled) vs decide_approval_atomically(grant)",
            final_state={
                "approval_status": approval_status,
                "run_status": run_status,
                "event_sequences": sequences,
                "event_types": final["event_types"],
            },
            extra={
                "cancel_won": cancel_won,
                "grant_won": grant_won,
                "pids": {name: outcome.pid for name, outcome in outcomes.items()},
                "note": ("Cancel won: approval remains 'requested' -- this is the current implementation semantics, recorded rather than treated as a defect.") if cancel_won else "Grant won; cancel was rejected.",
            },
            conclusion=(f"single winner (cancel={cancel_won}, grant={grant_won}); run={run_status}, approval={approval_status}"),
        ),
        harness.evidence_dir,
    )
