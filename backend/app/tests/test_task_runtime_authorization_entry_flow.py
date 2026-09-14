"""Vertical integration: withdrawing execution consent, through its real route.

AG-G2-AUTO-046.

``POST /api/tasks/{task_id}/revoke-execution-authorization`` is the「修改计划」
control: it clears the user's execution consent (``execution_authorized``) and
downgrades an ``executing`` task back to ``planned``, so the next launch has to be
authorized again.

What it must **not** do is what this file is about. It says nothing about the
Runtime: an attempt may already have a run, and withdrawing consent is not a
cancellation — the run is left running, the linkage is neither moved nor cleared,
and the Runtime is never asked to cancel. What the withdrawal does change is a
**precondition**: a revoked task no longer satisfies the opt-in's "execution
authorized" requirement, so it cannot be launched again until it is authorized once
more. Re-authorizing then does not buy a second run either, because the *attempt* —
and therefore the idempotency key — has not changed.

Nothing here hand-writes a final SQLite state; the Runtime is replaced at its public
boundary by a recording fake, and every step goes through a route that already
exists.
"""

from __future__ import annotations

import pytest
from _runtime_flow_support import (
    LINKAGE_TASK_KEY,
    RUN_ID,
    SWITCH,
    authorize_via_route,
    block_outbound_network,
    build_client,
    create_unattended_task,
    reset_home,
    revoke_via_route,
    schedule_a_run_via_route,
    start_via_route,
    stored_task,
)


@pytest.fixture()
def client():
    reset_home()
    return build_client()


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch):
    """Runtime opt-in on, and no outbound network: the fake is the only runtime."""
    monkeypatch.setenv(SWITCH, "1")
    block_outbound_network(monkeypatch)


# --- the withdrawal itself --------------------------------------------------------


def test_revoking_clears_the_execution_consent(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, _ = schedule_a_run_via_route(client, monkeypatch)
    assert stored_task(task_id)["execution_authorized"] is True

    body = revoke_via_route(client, task_id)

    assert body["success"] is True
    assert body["execution_authorized"] is False
    assert stored_task(task_id)["execution_authorized"] is False


def test_revoking_creates_no_run(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)

    revoke_via_route(client, task_id)

    assert len(fake.create_calls) == 1, "withdrawing consent must not start anything"


def test_revoking_an_unauthorized_task_is_a_safe_no_op(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = create_unattended_task(client)  # created, never authorized

    body = revoke_via_route(client, task_id)

    assert body["success"] is True
    assert stored_task(task_id)["execution_authorized"] is False


def test_revoking_twice_is_a_no_op(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)

    first = revoke_via_route(client, task_id)
    second = revoke_via_route(client, task_id)

    assert first["execution_authorized"] is False
    assert second["execution_authorized"] is False
    assert len(fake.create_calls) == 1
    assert fake.cancel_calls == []


# --- the Runtime run is not the consent -------------------------------------------


def test_revoking_leaves_the_linked_run_untouched(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, _ = schedule_a_run_via_route(client, monkeypatch)
    before = dict(stored_task(task_id)[LINKAGE_TASK_KEY])

    revoke_via_route(client, task_id)

    assert stored_task(task_id)[LINKAGE_TASK_KEY] == before


def test_revoking_does_not_ask_the_runtime_to_cancel(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Withdrawing consent is not a cancellation: the run is left to run."""
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)

    revoke_via_route(client, task_id)

    assert fake.cancel_calls == []


def test_revoking_does_not_move_the_task_out_of_its_state(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing re-plans on a revoke: the linked task stays where it was."""
    task_id, _ = schedule_a_run_via_route(client, monkeypatch)

    revoke_via_route(client, task_id)

    stored = stored_task(task_id)
    assert stored["status"] == "pending"
    assert stored.get("plan_bound_at") in (None, "")


def test_the_revoke_response_reveals_no_runtime_internals(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = schedule_a_run_via_route(client, monkeypatch)

    response = client.post(f"/api/tasks/{task_id}/revoke-execution-authorization")

    assert LINKAGE_TASK_KEY not in response.text
    assert RUN_ID not in response.text


def test_the_task_still_reads_without_the_linkage_after_a_revoke(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = schedule_a_run_via_route(client, monkeypatch)
    revoke_via_route(client, task_id)

    body = client.get(f"/api/tasks/{task_id}")

    assert body.status_code == 200
    assert LINKAGE_TASK_KEY not in body.text


# --- the withdrawal is a real precondition for the next launch --------------------


def test_a_revoked_task_cannot_be_dispatched(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, _ = schedule_a_run_via_route(client, monkeypatch)
    revoke_via_route(client, task_id)

    response = client.post(f"/api/tasks/{task_id}/dispatch-execution")

    assert response.status_code >= 400, "the dispatch gate must see the withdrawal"


def test_the_refused_dispatch_creates_no_run(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    revoke_via_route(client, task_id)

    client.post(f"/api/tasks/{task_id}/dispatch-execution")

    assert len(fake.create_calls) == 1


def test_re_authorizing_reuses_the_same_attempts_run(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consent was withdrawn, not the attempt — so the run is reused, not re-created."""
    task_id, fake = schedule_a_run_via_route(client, monkeypatch)
    revoke_via_route(client, task_id)

    authorize_via_route(client, task_id)
    step = start_via_route(client, task_id)["advance"]

    assert step["action"] == "runtime", step
    assert step["runtime_action"] == "reused"
    assert step["runtime_run_id"] == RUN_ID
    assert len(fake.create_calls) == 1, "re-authorizing must not buy a second run"


def test_re_authorizing_does_not_clear_or_move_the_linkage(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = schedule_a_run_via_route(client, monkeypatch)
    before = dict(stored_task(task_id)[LINKAGE_TASK_KEY])
    revoke_via_route(client, task_id)

    authorize_via_route(client, task_id)
    start_via_route(client, task_id)

    assert stored_task(task_id)[LINKAGE_TASK_KEY] == before


def test_a_revoke_only_touches_the_task_it_names(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Another task's consent is its own: revoking one leaves the other alone."""
    first_id, fake = schedule_a_run_via_route(client, monkeypatch)
    second_id = create_unattended_task(client)
    authorize_via_route(client, second_id)

    revoke_via_route(client, first_id)

    assert stored_task(first_id)["execution_authorized"] is False
    assert stored_task(second_id)["execution_authorized"] is True
    assert len(fake.create_calls) == 1
