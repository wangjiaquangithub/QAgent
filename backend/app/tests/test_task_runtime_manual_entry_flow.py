"""Vertical integration: the *manual* Task Center controls, driven by a user.

AG-G2-AUTO-039.

The other flows drive the Runtime opt-in through the queue tick. A user does not
press a queue tick: they press「开始执行」and then a run-now control. Those are two
real routes that already exist, and they are the entries this file exercises:

* ``POST /api/tasks/{task_id}/authorize-execution`` — the「开始执行」control. It
  authorizes the task and tries the worker dispatch. It must **not** be a second
  way to create a Runtime run.
* ``POST /api/tasks/{task_id}/start`` — the run-now control. For an unattended task
  it enqueues and immediately calls ``advance_unattended_task``, which is where the
  Runtime opt-in lives.

What is asserted is what a user can observe: exactly one run per attempt however
many times the control is pressed, a task whose authorization, status, history and
read responses agree with each other, and a failure that stays honest without
leaking the provider's own error. Nothing here hand-writes a final SQLite state,
and the Runtime is replaced at its public boundary by a recording fake, so no key
is read and no paid call can happen.
"""

from __future__ import annotations

import json

import pytest
from _runtime_flow_support import (
    LINKAGE_TASK_KEY,
    RUN_ID,
    FakeRuntime,
    authorize_via_route,
    block_outbound_network,
    build_client,
    create_unattended_task,
    frame,
    history_of,
    push_frames,
    reset_home,
    run_contract,
    save_task,
    start_via_route,
    statuses_of,
    stored_task,
)

SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"

SUCCESS_FRAMES = [
    frame("run.running", sequence=0),
    frame("run.completed", sequence=1, payload={"result": {"summary": "周报已生成"}}),
]

LEAKY_MESSAGE = (
    "Traceback (most recent call last): File \"/var/app/worker.py\", line 42, "
    "in run\n  client.post('https://internal.corp/v1/llm', "
    "headers={'Authorization': 'Bearer ghp_ABCdef0123456789'})\n"
    "  password=secret api_key=sk-live-0123456789"
)
SECRETS = (
    "Traceback",
    "/var/app/worker.py",
    "internal.corp",
    "ghp_ABCdef0123456789",
    "sk-live-0123456789",
    "password=secret",
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


def _authorized_task(client, monkeypatch: pytest.MonkeyPatch, **extra) -> tuple[str, FakeRuntime]:
    """Create an unattended task and press「开始执行」— the user's first click."""
    fake = run_contract(FakeRuntime(frames=SUCCESS_FRAMES), monkeypatch)
    task_id = create_unattended_task(client, **extra)
    body = authorize_via_route(client, task_id)
    assert body["execution_authorized"] is True, body
    return task_id, fake


# --- the run-now control is the entry that reaches the Runtime --------------------


def test_the_manual_run_now_route_reaches_the_runtime_once(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = _authorized_task(client, monkeypatch)

    body = start_via_route(client, task_id)

    assert body["success"] is True, body
    assert body["advance"]["action"] == "runtime", body["advance"]
    assert body["advance"]["runtime_run_id"] == RUN_ID
    assert len(fake.create_calls) == 1


def test_the_manual_run_now_route_reports_the_runtime_step_back_to_the_caller(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The response and the stored linkage must agree on which run was started."""
    task_id, fake = _authorized_task(client, monkeypatch)

    body = start_via_route(client, task_id)
    stored = stored_task(task_id)

    assert body["advance"]["runtime_run_id"] == RUN_ID
    assert stored[LINKAGE_TASK_KEY]["runtime_run_id"] == RUN_ID
    assert stored[LINKAGE_TASK_KEY]["idempotency_key"] == fake.create_calls[0]["idempotency_key"]


def test_repeated_manual_run_now_does_not_create_a_second_run(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pressing run-now again in the same attempt must not start a second run."""
    task_id, fake = _authorized_task(client, monkeypatch)

    start_via_route(client, task_id)
    start_via_route(client, task_id)
    start_via_route(client, task_id)

    assert len(fake.create_calls) == 1
    assert stored_task(task_id)[LINKAGE_TASK_KEY]["runtime_run_id"] == RUN_ID


# --- the「开始执行」control is not a second way to create a run ---------------------


def test_the_authorize_route_alone_creates_no_run(client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = run_contract(FakeRuntime(frames=SUCCESS_FRAMES), monkeypatch)
    task_id = create_unattended_task(client)

    body = authorize_via_route(client, task_id)

    assert body["execution_authorized"] is True
    assert fake.create_calls == []
    assert LINKAGE_TASK_KEY not in stored_task(task_id)


def test_the_authorize_then_run_now_pair_creates_exactly_one_run(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Authorizing twice and starting twice still yields one run for this attempt."""
    task_id, fake = _authorized_task(client, monkeypatch)

    authorize_via_route(client, task_id)
    start_via_route(client, task_id)
    authorize_via_route(client, task_id)
    start_via_route(client, task_id)

    assert len(fake.create_calls) == 1


def test_the_authorize_route_reports_the_authorization_it_recorded(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_contract(FakeRuntime(frames=SUCCESS_FRAMES), monkeypatch)
    task_id = create_unattended_task(client)

    body = authorize_via_route(client, task_id)

    assert body["task_id"] == task_id
    assert body["execution_authorized"] is True
    assert body["authorized_by"] == "user"
    stored = stored_task(task_id)
    assert stored["execution_authorized"] is True
    assert stored["authorized_by"] == "user"


def test_an_unauthorized_manual_run_now_never_reaches_the_runtime(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run-now without「开始执行」first must stay on the pre-existing path."""
    fake = run_contract(FakeRuntime(frames=SUCCESS_FRAMES), monkeypatch)
    task_id = create_unattended_task(client)

    body = start_via_route(client, task_id)

    assert fake.create_calls == []
    assert body["advance"].get("action") != "runtime"
    assert LINKAGE_TASK_KEY not in stored_task(task_id)


# --- the manual run carries only the trusted identity -----------------------------


def test_the_manual_run_carries_the_trusted_organization(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = _authorized_task(client, monkeypatch)

    start_via_route(client, task_id)

    call = fake.create_calls[0]
    assert call["org_id"] == "local"
    # No client-supplied identity may reach the Runtime through the input payload.
    assert "org_id" not in call["input_payload"]
    assert "tenant_id" not in call["input_payload"]


def test_a_handwritten_organization_on_the_task_does_not_reach_the_manual_run(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = _authorized_task(client, monkeypatch)
    stored = stored_task(task_id)
    stored["org_id"] = "someone-elses-org"
    stored["tenant_id"] = "someone-elses-tenant"
    save_task(task_id, stored)

    start_via_route(client, task_id)

    assert fake.create_calls[0]["org_id"] == "local"


# --- what the user then reads back ------------------------------------------------


def test_the_manual_entry_projection_matches_the_read_routes(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = _authorized_task(client, monkeypatch)
    start_via_route(client, task_id)
    push_frames(task_id, fake.frames)

    stored = stored_task(task_id)
    body = client.get(f"/api/tasks/{task_id}").json()
    history = client.get(f"/api/tasks/{task_id}/execution-history").json()

    assert stored["status"] == "completed"
    assert body["status"] == "completed"
    assert [e["runtime_status"] for e in history["execution_history"]] == ["running", "completed"]
    assert [e["runtime_status"] for e in history_of(stored)] == statuses_of(stored)
    # The Runtime is the source of truth; the read responses carry only the projection.
    assert "runtime_run_linkage" not in body


def test_the_manual_entry_leaves_the_result_visible_through_the_routes(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = _authorized_task(client, monkeypatch)
    start_via_route(client, task_id)
    push_frames(task_id, fake.frames)

    history = client.get(f"/api/tasks/{task_id}/execution-history").json()
    record = history["execution_history"][-1]

    assert record["result_available"] is True
    assert record["result"] == {"summary": "周报已生成"}


# --- a failure from the manual entry stays honest and safe ------------------------


def test_the_manual_entry_failure_is_safe_and_honest(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = run_contract(
        FakeRuntime(
            frames=[
                frame("run.running", sequence=0),
                frame(
                    "run.failed",
                    sequence=1,
                    payload={"error": {"code": "provider_error", "message": LEAKY_MESSAGE}},
                ),
            ],
            status="failed",
        ),
        monkeypatch,
    )
    task_id = create_unattended_task(client)
    authorize_via_route(client, task_id)
    assert start_via_route(client, task_id)["advance"]["action"] == "runtime"
    push_frames(task_id, fake.frames)

    stored = stored_task(task_id)
    record = history_of(stored)[-1]

    assert stored["status"] == "failed"
    assert record["error_code"] == "provider_error"
    assert record["error_detail_redacted"] is True
    assert "reason" not in record


@pytest.mark.parametrize("secret", SECRETS)
def test_no_leaked_fragment_reaches_the_manual_entry_responses(
    client, monkeypatch: pytest.MonkeyPatch, secret: str
) -> None:
    fake = run_contract(
        FakeRuntime(
            frames=[
                frame("run.running", sequence=0),
                frame(
                    "run.failed",
                    sequence=1,
                    payload={"error": {"code": "provider_error", "message": LEAKY_MESSAGE}},
                ),
            ],
            status="failed",
        ),
        monkeypatch,
    )
    task_id = create_unattended_task(client)
    authorize_via_route(client, task_id)
    start_via_route(client, task_id)
    push_frames(task_id, fake.frames)

    assert secret not in json.dumps(stored_task(task_id), ensure_ascii=False)
    assert secret not in client.get(f"/api/tasks/{task_id}").text
    assert secret not in client.get(f"/api/tasks/{task_id}/execution-history").text


# --- the switch, which keeps the legacy path intact -------------------------------


def test_with_the_switch_off_the_manual_run_now_keeps_the_legacy_path(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = run_contract(FakeRuntime(frames=SUCCESS_FRAMES), monkeypatch)
    monkeypatch.setenv(SWITCH, "0")
    task_id = create_unattended_task(client)
    authorize_via_route(client, task_id)

    body = start_via_route(client, task_id)

    assert fake.create_calls == []
    assert body["advance"].get("action") != "runtime"
    assert LINKAGE_TASK_KEY not in stored_task(task_id)


def test_the_manual_entry_writes_history_only_after_the_projection(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starting the run writes the linkage; the frames are what write history."""
    task_id, fake = _authorized_task(client, monkeypatch)

    start_via_route(client, task_id)
    # ``history_of`` and not ``row[HISTORY_FIELD]``: the key is only materialised
    # on a read that went through the row mapper with at least one record, so a
    # direct index would depend on the project still being in the storage cache.
    assert history_of(stored_task(task_id)) == []

    push_frames(task_id, fake.frames)
    assert history_of(stored_task(task_id))
