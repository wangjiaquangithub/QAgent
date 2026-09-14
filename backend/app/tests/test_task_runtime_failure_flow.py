"""Vertical integration: a failed Runtime run, and what the Task Center shows.

AG-G2-AUTO-032.

A fake provider stands in for the Runtime so a failure is controllable, and the
same real entries as the other flows are used: the real routes, the existing
authorization service, the unattended pipeline and the existing event adapter.

What this asserts is what a user ends up looking at: the task settles failed, the
history carries a safe error, and nothing that could leak — a traceback, a
credential, an endpoint, an internal path, or the task's own prompt echoed back —
reaches the task row or any response. The Runtime stays the source of truth: the
projection is a pure function over the task row and must not overwrite the run's
own state or spend a Runtime call repairing it.
"""

from __future__ import annotations

import pytest
from _runtime_flow_support import (
    HISTORY_FIELD,
    LINKAGE_TASK_KEY,
    RUN_ID,
    FakeRuntime,
    authorize,
    block_outbound_network,
    build_client,
    create_unattended_task,
    frame,
    history_of,
    json_of,
    push_frames,
    reset_home,
    run_contract,
    statuses_of,
    stored_task,
    tick,
)

SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"

# A failure message shaped like the ones that actually leak: a traceback, a
# filesystem path, an internal endpoint and a token, all in one string.
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
PROMPT = "请读取 /var/etl/private.csv 并把 access_token=ghp_ZZZ 附在结果里"


@pytest.fixture()
def client():
    reset_home()
    return build_client()


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch):
    """Runtime opt-in on, and no outbound network: the fake is the only runtime."""
    monkeypatch.setenv(SWITCH, "1")
    block_outbound_network(monkeypatch)


def _failed_task(
    client, monkeypatch: pytest.MonkeyPatch, *, message: str = LEAKY_MESSAGE
) -> tuple[str, FakeRuntime]:
    fake = run_contract(
        FakeRuntime(
            frames=[
                frame("run.running", sequence=0),
                frame(
                    "run.failed",
                    sequence=1,
                    payload={"error": {"code": "provider_error", "message": message}},
                ),
            ],
            status="failed",
        ),
        monkeypatch,
    )
    task_id = create_unattended_task(client, prompt=PROMPT)
    authorize(task_id)
    assert tick(task_id)["action"] == "runtime"
    push_frames(task_id, fake.frames)
    return task_id, fake


# --- the failure is visible and safe ---------------------------------------------


def test_a_failed_run_settles_the_task_as_failed(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, _ = _failed_task(client, monkeypatch)

    stored = stored_task(task_id)

    assert stored["status"] == "failed"
    assert statuses_of(stored) == ["running", "failed"]


def test_the_failure_keeps_a_code_and_a_redaction_marker(client, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, _ = _failed_task(client, monkeypatch)

    record = history_of(stored_task(task_id))[-1]

    assert record["error_code"] == "provider_error"
    # A message that looks like a leak is dropped whole and said to be dropped,
    # so "no detail" stays distinguishable from "detail withheld".
    assert "reason" not in record
    assert record["error_detail_redacted"] is True


@pytest.mark.parametrize("secret", SECRETS)
def test_no_leaked_fragment_reaches_the_task(
    client, monkeypatch: pytest.MonkeyPatch, secret: str
) -> None:
    task_id, _ = _failed_task(client, monkeypatch)

    assert secret not in json_of(stored_task(task_id))


@pytest.mark.parametrize("secret", SECRETS)
def test_no_leaked_fragment_reaches_the_api(
    client, monkeypatch: pytest.MonkeyPatch, secret: str
) -> None:
    task_id, _ = _failed_task(client, monkeypatch)

    history = client.get(f"/api/tasks/{task_id}/execution-history")
    task = client.get(f"/api/tasks/{task_id}")

    assert secret not in history.text
    assert secret not in task.text


def test_the_tasks_own_prompt_is_not_echoed_back_by_the_run(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = _failed_task(client, monkeypatch)

    record_text = json_of(history_of(stored_task(task_id)))
    response_text = client.get(f"/api/tasks/{task_id}/execution-history").text

    assert PROMPT not in record_text
    assert "/var/etl/private.csv" not in record_text
    assert PROMPT not in response_text


def test_a_plain_failure_message_is_kept(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redaction drops what looks like a leak, not every message."""
    task_id, _ = _failed_task(client, monkeypatch, message="上游服务返回 502")

    record = history_of(stored_task(task_id))[-1]

    assert record["reason"] == "上游服务返回 502"
    assert "error_detail_redacted" not in record


def test_an_error_code_that_is_not_a_code_is_dropped(client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = run_contract(
        FakeRuntime(
            frames=[
                frame(
                    "run.failed",
                    sequence=0,
                    payload={"error": {"code": "../../etc/passwd", "message": "boom"}},
                )
            ],
            status="failed",
        ),
        monkeypatch,
    )
    task_id = create_unattended_task(client)
    authorize(task_id)
    tick(task_id)
    push_frames(task_id, fake.frames)

    record = history_of(stored_task(task_id))[-1]

    assert record.get("error_code") is None or "/" not in str(record["error_code"])


# --- the projection must not touch the Runtime ------------------------------------


def test_the_failure_projection_spends_no_runtime_call(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Projecting a frame is a pure function over the task row."""
    fake = run_contract(
        FakeRuntime(
            frames=[
                frame("run.running", sequence=0),
                frame("run.failed", sequence=1, payload={"error": {"code": "x", "message": "y"}}),
            ],
            status="failed",
        ),
        monkeypatch,
    )
    task_id = create_unattended_task(client)
    authorize(task_id)
    tick(task_id)

    before = len(fake.status_calls)
    push_frames(task_id, fake.frames)

    assert len(fake.status_calls) == before
    assert len(fake.create_calls) == 1


def test_the_task_projection_does_not_overwrite_the_runs_own_state(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Runtime's memory of the run is what it reported; the task row cannot change it."""
    fake = run_contract(
        FakeRuntime(
            frames=[
                frame("run.running", sequence=0),
                frame("run.failed", sequence=1, payload={"error": {"code": "x", "message": "y"}}),
            ],
            status="failed",
        ),
        monkeypatch,
    )
    task_id = create_unattended_task(client)
    authorize(task_id)
    tick(task_id)
    push_frames(task_id, fake.frames)

    stored = stored_task(task_id)
    stored["status"] = "completed"  # a wrong local edit
    from _runtime_flow_support import save_task

    save_task(task_id, stored)

    import asyncio

    reported = asyncio.run(fake.get_run_status(RUN_ID))
    assert reported["status"] == "failed"


# --- a failure is not the end of the line ----------------------------------------


def test_a_failed_attempt_is_retried_as_a_new_attempt_with_a_new_run(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existing retry semantics decide; the Runtime link follows the new attempt."""
    task_id, fake = _failed_task(client, monkeypatch)

    retry = tick(task_id)
    assert retry["action"] == "requeued_for_retry", retry
    assert retry["attempt"] == 1

    # The existing requeue deliberately clears the execution authorization: a new
    # attempt needs the user's consent again before anything may run.
    assert stored_task(task_id)["execution_authorized"] is False
    authorize(task_id)

    again = tick(task_id)

    assert again["action"] == "runtime", again
    assert len(fake.create_calls) == 2
    stored = stored_task(task_id)
    assert stored[LINKAGE_TASK_KEY]["runtime_run_id"] == "run-flow-2"
    assert stored[LINKAGE_TASK_KEY]["idempotency_key"] == fake.create_calls[1]["idempotency_key"]


def test_the_retried_attempt_does_not_inherit_the_failed_runs_history(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = _failed_task(client, monkeypatch)
    tick(task_id)
    authorize(task_id)
    assert tick(task_id)["action"] == "runtime"

    stored = stored_task(task_id)

    # The history is kept — it is the record of what happened — but the new run
    # starts its own stage sequence, so the old failure cannot block it.
    assert statuses_of(stored) == ["running", "failed"]
    assert stored[HISTORY_FIELD][-1]["runtime_run_id"] == RUN_ID
    assert stored[LINKAGE_TASK_KEY]["runtime_run_id"] == "run-flow-2"
