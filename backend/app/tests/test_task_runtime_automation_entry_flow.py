"""Vertical integration: the Automation rule's manual run-now entry.

AG-G2-AUTO-042.

``POST /api/automation/tasks/{id}/run`` is the「手动运行」control. It hands the
run to ``_run_one_task``, which routes a prompt-only automation three ways:

* a **workflow-bound** automation (``app_id``) goes to the app runner
  (``run_app_workflow``) — it must not be diverted;
* a prompt-only automation that **opted in** to the Task Center pipeline
  (``execution_mode=task_center``, or ``EVOFLOW_AUTOMATION_VIA_TASK=1``) creates a
  Task Center task and kicks it — that kick is where the Runtime opt-in happens;
* a prompt-only automation that did **not** opt in keeps the direct LangGraph path.

AG-G2-AUTO-022 already locks the mode vocabulary shared by the two gates. What this
file covers is the behaviour behind it: the routing decision itself, and the
enqueue-plus-kick chain actually producing exactly one Task Center task and exactly
one correctly scoped Runtime run, without the Runtime switch changing whether a task
is created at all.

**Gap this suite surfaced, deliberately not papered over.** The Task Center task an
automation creates is given no owner scope, and the Runtime opt-in will not run
anything without a trusted server-side identity (AG-G2-AUTO-008). So in this
environment the kick resolves *no* identity and the opt-in defers to the legacy path
— which is the fail-safe, not a run with a guessed organization. The chain tests
below therefore state the identity precondition explicitly
(``_give_the_task_a_trusted_identity``) instead of pretending it is satisfied, and
``test_the_kick_without_a_trusted_identity_defers_to_the_legacy_path`` pins what
actually happens today. Inheriting the automation's own recorded owner scope (which
the automation create route writes) is the obvious wiring fix, but its ``created_by``
half needs a decision about which server-side identity an automation's tasks belong
to, so it is reported rather than invented here.

The Runtime is replaced at its public boundary by a recording fake.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest
from _runtime_flow_support import (
    LINKAGE_TASK_KEY,
    FakeRuntime,
    block_outbound_network,
    reset_home,
    run_contract,
    stored_task,
)

SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"
TASK_CENTER_SWITCH = "EVOFLOW_AUTOMATION_VIA_TASK"
RUNNER = Path("app/gateway/automation_runner.py")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch):
    """Runtime opt-in on, and no outbound network: the fake is the only runtime."""
    monkeypatch.setenv(SWITCH, "1")
    monkeypatch.delenv(TASK_CENTER_SWITCH, raising=False)
    reset_home()
    block_outbound_network(monkeypatch)


def _automation(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": "经营晨报",
        "prompt": "生成一份经营晨报",
        "schedule": "0 9 * * *",
        "status": "active",
    }
    row.update(over)
    return row


def _via_task_center(task: dict[str, Any] | None) -> bool:
    from app.gateway.automation_runner import _automation_via_task_center

    return _automation_via_task_center(task)


def _enqueue(task: dict[str, Any], *, run_id: str = "auto-run-1") -> dict[str, Any]:
    from app.gateway.automation_runner import _enqueue_automation_as_unattended_task

    return _enqueue_automation_as_unattended_task(
        "auto-1",
        task,
        run_id=run_id,
        trigger_type="manual_http",
        prompt=str(task.get("prompt") or ""),
    )


def _kick(task_id: str) -> None:
    from app.gateway.automation_runner import _kick_unattended_task

    asyncio.run(_kick_unattended_task(task_id))


def _give_the_task_a_trusted_identity(task_id: str) -> None:
    """Model a deployment where the task has a recorded owner scope.

    The Runtime opt-in requires a trusted server-side identity before it may run
    anything, and it reads that identity from the task's persisted owner scope
    columns. In this test environment nothing records one for a task an automation
    created (see the module docstring), so the scope is written here to exercise the
    rest of the chain. Nothing final is hand-written: the identity is resolved by
    the production resolver from these columns.
    """
    from evoflow.persistence.task_repositories import set_root_task_owner_scope

    set_root_task_owner_scope(
        task_id,
        org_id="local",
        owner_scope_id="personal:local-admin",
        created_by="local-admin",
    )


def _runner_function(name: str) -> ast.stmt:
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


# --- the routing decision --------------------------------------------------------


def test_an_explicit_task_center_mode_opts_a_prompt_only_automation_in() -> None:
    assert _via_task_center(_automation(execution_mode="task_center")) is True


def test_an_explicit_direct_mode_opts_a_prompt_only_automation_out() -> None:
    assert _via_task_center(_automation(execution_mode="direct_langgraph")) is False


def test_the_environment_switch_opts_a_prompt_only_automation_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TASK_CENTER_SWITCH, "1")

    assert _via_task_center(_automation()) is True


def test_an_automation_without_any_opt_in_stays_on_the_langgraph_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default must not drift: nothing opted in, nothing changes."""
    monkeypatch.setenv(SWITCH, "1")

    assert _via_task_center(_automation()) is False


def test_an_explicit_direct_mode_beats_the_environment_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TASK_CENTER_SWITCH, "1")

    assert _via_task_center(_automation(execution_mode="direct")) is False


def test_an_app_bound_automation_is_routed_before_the_task_center_gate() -> None:
    """The app runner owns ``app_id`` automations, so it must be reached first."""
    body = _runner_function("_run_one_task")
    first_line: dict[str, int] = {}
    for node in ast.walk(body):
        if isinstance(node, ast.Name) and node.id in (
            "_run_bound_app_for_automation",
            "_automation_via_task_center",
        ):
            first_line.setdefault(node.id, node.lineno)

    assert "_run_bound_app_for_automation" in first_line, first_line
    assert "_automation_via_task_center" in first_line, first_line
    assert first_line["_run_bound_app_for_automation"] < first_line["_automation_via_task_center"]


# --- the enqueue-plus-kick chain -------------------------------------------------


def test_the_task_center_branch_creates_one_task_center_task() -> None:
    body = _enqueue(_automation())

    assert body["ok"] is True, body
    task_id = str(body["collab_task_id"])
    stored = stored_task(task_id)

    assert stored["run_mode"] == "unattended"
    assert stored["execution_authorized"] is True
    assert stored["raised_by"] == "automation"
    assert stored["automation_id"] == "auto-1"
    assert stored["trigger_kind"] == "manual_http"


def test_the_kick_creates_exactly_one_runtime_run(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = run_contract(FakeRuntime(), monkeypatch)
    task_id = str(_enqueue(_automation())["collab_task_id"])
    _give_the_task_a_trusted_identity(task_id)

    _kick(task_id)

    assert len(fake.create_calls) == 1
    # The Runtime is addressed by the canonical task id derived from the org scope,
    # not by the Task Center row's own (timestamped) id, which is why the two differ.
    assert fake.create_calls[0]["task_id"]
    assert fake.create_calls[0]["task_id"] != task_id


def test_the_kick_uses_the_trusted_organization(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = run_contract(FakeRuntime(), monkeypatch)
    task_id = str(_enqueue(_automation())["collab_task_id"])
    _give_the_task_a_trusted_identity(task_id)

    _kick(task_id)

    call = fake.create_calls[0]

    assert call["org_id"] == "local"
    assert "org_id" not in call["input_payload"]
    assert "tenant_id" not in call["input_payload"]


def test_the_linkage_lands_on_the_task_the_automation_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_contract(FakeRuntime(), monkeypatch)
    task_id = str(_enqueue(_automation())["collab_task_id"])
    _give_the_task_a_trusted_identity(task_id)

    _kick(task_id)

    assert stored_task(task_id)[LINKAGE_TASK_KEY]["runtime_run_id"] == "run-flow-1"


def test_a_second_kick_does_not_create_a_second_run(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = run_contract(FakeRuntime(), monkeypatch)
    task_id = str(_enqueue(_automation())["collab_task_id"])
    _give_the_task_a_trusted_identity(task_id)

    _kick(task_id)
    _kick(task_id)

    assert len(fake.create_calls) == 1


def test_the_kick_without_a_trusted_identity_defers_to_the_legacy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An automation-created task has no owner scope of its own, so nothing runs.

    This is the AG-G2-AUTO-008 fail-safe: with no trusted server-side identity the
    Runtime is not guessed at, and the task keeps the path it had before.
    """
    fake = run_contract(FakeRuntime(), monkeypatch)
    task_id = str(_enqueue(_automation())["collab_task_id"])

    from app.gateway import task_runtime_optin as optin

    result = asyncio.run(
        optin.establish_runtime_run(
            stored_task(task_id),
            identity=optin.resolve_server_task_runtime_identity(task_id),
            authorized=True,
            contract=fake,
        )
    )

    assert optin.resolve_server_task_runtime_identity(task_id) is None
    assert result.decision == "legacy"
    assert result.reason == "trusted_identity_unavailable"
    assert fake.create_calls == []
    assert LINKAGE_TASK_KEY not in stored_task(task_id)


def test_the_runtime_switch_does_not_change_whether_a_task_is_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Task Center routing and the Runtime opt-in are separate switches."""
    fake = run_contract(FakeRuntime(), monkeypatch)
    monkeypatch.setenv(SWITCH, "0")

    body = _enqueue(_automation())
    task_id = str(body["collab_task_id"])
    _give_the_task_a_trusted_identity(task_id)

    from app.gateway import task_runtime_optin as optin

    result = asyncio.run(
        optin.establish_runtime_run(
            stored_task(task_id),
            identity=optin.resolve_server_task_runtime_identity(task_id),
            authorized=True,
            contract=fake,
        )
    )

    assert body["ok"] is True
    assert stored_task(task_id)["run_mode"] == "unattended"
    # With the Runtime switch off the pipeline keeps the path it had before.
    assert result.decision == "legacy"
    assert fake.create_calls == []


def test_a_kick_without_a_task_id_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = run_contract(FakeRuntime(), monkeypatch)

    _kick("")

    assert fake.create_calls == []


def test_an_automation_without_a_prompt_is_not_enqueued() -> None:
    body = _enqueue(_automation(prompt=""))

    assert body["ok"] is False
    assert body["collab_task_id"] == ""
