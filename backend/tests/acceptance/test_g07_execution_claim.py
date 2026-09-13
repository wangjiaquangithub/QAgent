"""G0.7 scenarios 3-5: execution claim and epoch semantics on real PostgreSQL.

Scenario 3 -- two backends contest one run's execution claim; only one may win.
Scenario 4 -- an expired claim can be reclaimed and the epoch must advance.
Scenario 5 -- a superseded (stale-epoch) writer must be rejected or a safe no-op.

The durable claim lives on the run row (``execution_claim`` /
``execution_epoch`` / ``execution_claimed_at``) and is taken under
``with_for_update()``, so these tests all exercise the same row-lock
serialization as production.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.qagent_runtime.events import EventType

from .pg_support import (
    ACCEPTANCE_ORG,
    ScenarioEvidence,
    assert_sequences_monotonic_unique,
    run_two_connection_race,
    seed_running_run,
    write_evidence,
)


def _claim(repository, *, run_id, reclaim=False):
    return repository.claim_execution(run_id, org_id=ACCEPTANCE_ORG, emit_event=True, reclaim=reclaim)


def _age_claim(harness, run_id: str, *, seconds: int) -> None:
    """Backdate ``execution_claimed_at`` so the claim looks expired.

    Uses an explicit user-supplied timestamp rather than sleeping, so the test
    is fast and deterministic. The run stays in whatever state it already had
    (``executing`` after a first claim); only the claim timestamp moves.
    """

    stale = datetime.now(UTC) - timedelta(seconds=seconds)
    with harness.engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE qagent_runs SET execution_claimed_at = :ts WHERE run_id = :run_id"),
            {"ts": stale, "run_id": run_id},
        )


pytestmark = [pytest.mark.postgres, pytest.mark.agentscope_acceptance]


def test_g07_03_concurrent_claim_single_holder(clean_runtime):
    """Scenario 3: two connections cannot both hold a valid execution claim."""

    harness = clean_runtime
    run_id = seed_running_run(harness.repository, task_id="g07-03-claim-contention")
    initial = harness.snapshot(run_id)

    outcomes = run_two_connection_race(
        url=harness.url,
        operations={
            "claim_a": lambda repo, engine: _claim(repo, run_id=run_id),
            "claim_b": lambda repo, engine: _claim(repo, run_id=run_id),
        },
    )

    claims = {name: outcome.result for name, outcome in outcomes.items()}
    winners = [name for name, claim in claims.items() if claim]

    final = harness.snapshot(run_id)
    sequences = assert_sequences_monotonic_unique(harness.repository, run_id)

    # Exactly one execution right may be granted.
    assert len(winners) == 1, f"expected exactly one claim holder, got {winners}"
    winner_claim = claims[winners[0]]
    assert final["run"]["execution_claim"] == winner_claim

    # Epoch advanced exactly once, and the run moved running -> executing.
    assert final["run"]["execution_epoch"] == int(initial["run"]["execution_epoch"]) + 1
    assert final["run"]["status"] == "executing"
    assert EventType.RUN_EXECUTING in final["event_types"]
    assert final["event_types"].count(EventType.RUN_EXECUTING) == 1

    write_evidence(
        ScenarioEvidence(
            scenario="g07_03_claim_contention",
            initial_state={
                "status": initial["run"]["status"],
                "execution_epoch": initial["run"]["execution_epoch"],
                "execution_claim": initial["run"]["execution_claim"],
            },
            concurrency_mode="two PostgreSQL backends, Barrier(2), concurrent claim_execution(reclaim=False)",
            final_state={
                "status": final["run"]["status"],
                "execution_claim": final["run"]["execution_claim"],
                "execution_epoch": final["run"]["execution_epoch"],
                "event_sequences": sequences,
                "event_types": final["event_types"],
            },
            extra={
                "winner": winners,
                "claims": claims,
                "pids": {name: outcome.pid for name, outcome in outcomes.items()},
            },
            conclusion=("Exactly one valid execution claim; no duplicate run.executing event; epoch incremented once."),
        ),
        harness.evidence_dir,
    )


def test_g07_04_expired_claim_reclaim_advances_epoch(clean_runtime):
    """Scenario 4: an expired claim can be reclaimed, epoch strictly increases.

    ``claim_execution`` treats ``reclaim=True`` as a trusted caller verdict: the
    *policy* check (claim older than ``execution_claim_timeout``) happens in
    ``RuntimeService.resume_run`` before the call, and the repository only
    enforces the row lock. This test therefore drives the repository primitive
    directly for the sequential reclaim contract, and separately records what a
    concurrent double-reclaim does (evidence only -- see the second half).
    """

    harness = clean_runtime
    run_id = seed_running_run(harness.repository, task_id="g07-04-claim-reclaim")

    first_claim = _claim(harness.repository, run_id=run_id)
    assert first_claim, "initial claim must succeed"
    after_first = harness.snapshot(run_id)

    # The run is now 'executing' with a live claim; a plain re-claim is refused.
    assert after_first["run"]["status"] == "executing"
    refused = _claim(harness.repository, run_id=run_id)
    assert refused is None, "a live claim must not be silently duplicated"

    # Age the claim past the timeout, then reclaim.
    _age_claim(harness, run_id, seconds=3600)
    second_claim = _claim(harness.repository, run_id=run_id, reclaim=True)
    assert second_claim and second_claim != first_claim

    final = harness.snapshot(run_id)
    sequences = assert_sequences_monotonic_unique(harness.repository, run_id)

    # Monotonic epoch: the second attempt strictly outranks the first.
    assert final["run"]["execution_epoch"] == int(after_first["run"]["execution_epoch"]) + 1
    assert final["run"]["execution_claim"] == second_claim

    # The old executor's claim no longer matches, so it has lost write authority.
    assert final["run"]["execution_claim"] != first_claim

    # --- Concurrent reclaim observation (evidence, not an assertion) ---------
    # After ageing again, two backends call reclaim=True at the same moment.
    # The row lock serializes them, but because reclaim is a trusted verdict
    # neither is refused, so both can take a claim in sequence. This records the
    # actual behaviour rather than asserting a single-winner guarantee the
    # repository does not currently provide.
    _age_claim(harness, run_id, seconds=3600)
    epoch_before_race = harness.snapshot(run_id)["run"]["execution_epoch"]
    race = run_two_connection_race(
        url=harness.url,
        operations={
            "reclaim": lambda repo, engine: _claim(repo, run_id=run_id, reclaim=True),
            "rival": lambda repo, engine: _claim(repo, run_id=run_id, reclaim=True),
        },
    )
    race_claims = {name: outcome.result for name, outcome in race.items()}
    race_winners = [name for name, claim in race_claims.items() if claim]
    epoch_after_race = harness.snapshot(run_id)["run"]["execution_epoch"]

    write_evidence(
        ScenarioEvidence(
            scenario="g07_04_claim_reclaim",
            initial_state={
                "first_claim": first_claim,
                "epoch_after_first": after_first["run"]["execution_epoch"],
                "reclaim_without_expiry": refused,
            },
            concurrency_mode=("sequential reclaim of an aged claim (authoritative contract), plus a two-backend concurrent reclaim observation"),
            final_state={
                "status": final["run"]["status"],
                "execution_claim": final["run"]["execution_claim"],
                "execution_epoch": final["run"]["execution_epoch"],
                "event_sequences": sequences,
                "event_types": final["event_types"],
            },
            extra={
                "second_claim": second_claim,
                "live_claim_refused": refused is None,
                "epoch_advanced_by_one": final["run"]["execution_epoch"] == int(after_first["run"]["execution_epoch"]) + 1,
                "concurrent_reclaim": {
                    "winners": race_winners,
                    "claims": race_claims,
                    "epoch_before": epoch_before_race,
                    "epoch_after": epoch_after_race,
                    "pids": {name: outcome.pid for name, outcome in race.items()},
                    "note": ("claim_execution trusts reclaim=True; the expiry policy lives in RuntimeService.resume_run, so the repository layer does not guarantee a single winner for concurrent raw reclaim calls."),
                },
            },
            conclusion=(
                "Sequential reclaim of an expired claim advanced the epoch by exactly one "
                "and revoked the old claim's authority. Concurrent raw reclaim calls both "
                "committed because reclaim is a trusted caller verdict (policy is in "
                "RuntimeService; repository only enforces the row lock) -- recorded as "
                "observed semantics."
            ),
        ),
        harness.evidence_dir,
    )


def test_g07_05_stale_epoch_writes_rejected(clean_runtime):
    """Scenario 5: a superseded executor cannot write result/error/terminal state."""

    harness = clean_runtime
    repository = harness.repository
    run_id = seed_running_run(repository, task_id="g07-05-stale-epoch")

    old_claim = _claim(repository, run_id=run_id)
    assert old_claim
    _age_claim(harness, run_id, seconds=3600)

    with harness.engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE qagent_runs SET status = 'running' WHERE run_id = :run_id"),
            {"run_id": run_id},
        )
    new_claim = _claim(repository, run_id=run_id, reclaim=True)
    assert new_claim and new_claim != old_claim

    after_reclaim = harness.snapshot(run_id)

    # (a) Stale set_result with the old claim must be a safe no-op.
    repository.set_result(
        run_id,
        {"stale": True},
        [],
        org_id=ACCEPTANCE_ORG,
        execution_claim=old_claim,
    )
    after_stale_result = harness.snapshot(run_id)

    # (b) Stale set_error with the old claim must also be a safe no-op.
    repository.set_error(
        run_id,
        {"code": "stale_executor", "message": "old attempt"},
        org_id=ACCEPTANCE_ORG,
        execution_claim=old_claim,
    )
    after_stale_error = harness.snapshot(run_id)

    assert after_stale_result["run"]["result_payload"] is None, "stale result was written"
    assert after_stale_result["run"]["status"] == after_reclaim["run"]["status"]
    assert after_stale_error["run"]["error_payload"] is None, "stale error was written"
    assert after_stale_error["run"]["status"] == after_reclaim["run"]["status"]
    assert after_stale_error["run"]["execution_claim"] == new_claim

    # (c) The new epoch still writes legitimately and completes the run.
    repository.set_result(
        run_id,
        {"ok": True},
        [],
        org_id=ACCEPTANCE_ORG,
        execution_claim=new_claim,
    )
    final = harness.snapshot(run_id)
    sequences = assert_sequences_monotonic_unique(repository, run_id)

    assert final["run"]["status"] == "completed"
    assert final["run"]["result_payload"] == {"ok": True}
    assert EventType.RUN_COMPLETED in final["event_types"]

    write_evidence(
        ScenarioEvidence(
            scenario="g07_05_stale_epoch_rejection",
            initial_state={"old_claim": old_claim, "new_claim": new_claim},
            concurrency_mode="sequential: claim -> age -> reclaim(new epoch) -> stale writes with old claim",
            final_state={
                "status": final["run"]["status"],
                "result_payload": final["run"]["result_payload"],
                "error_payload": final["run"]["error_payload"],
                "execution_claim": final["run"]["execution_claim"],
                "event_sequences": sequences,
                "event_types": final["event_types"],
            },
            extra={
                "stale_result_rejected": after_stale_result["run"]["result_payload"] is None,
                "stale_error_rejected": after_stale_error["run"]["error_payload"] is None,
                "epoch_after_reclaim": after_reclaim["run"]["execution_epoch"],
            },
            conclusion=("Stale-epoch result and error writes were no-ops; the new epoch completed the run without being overwritten."),
        ),
        harness.evidence_dir,
    )
