"""Protective tests for the explicit Runtime opt-in decision.

Covers AG-G2-AUTO-022: the Runtime's opt-in keeps the existing
``execution_mode=task_center`` / environment semantics, an explicit opt-out is
honoured, absence changes nothing, and an invalid value never opts a task in.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from app.gateway import task_runtime_optin as optin

SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"
TASK_CENTER_SWITCH = "EVOFLOW_AUTOMATION_VIA_TASK"


def _task(**extra: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": "e4c1a2b0",
        "name": "每周经营简报",
        "run_mode": "unattended",
        "status": "pending",
        "unattended_attempts": 0,
    }
    task.update(extra)
    return task


def _identity() -> dict[str, Any]:
    return {
        "org_id": "org-a",
        "scope_id": "personal:webui:1",
        "principal": {"principal_id": "webui:1", "principal_type": "internal"},
    }


class RecordingContract:
    """Records every Runtime public-boundary call. Never performs I/O."""

    def __init__(self) -> None:
        self.create_calls: list[str | None] = []
        self.status_calls: list[str] = []

    async def create_run(
        self,
        *,
        org_id: str,
        task_id: str,
        input_payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.create_calls.append(idempotency_key)
        return {"run_id": "run-1", "status": "pending", "org_id": org_id}

    async def get_run_status(self, run_id: str, *, org_id: str | None = None) -> dict[str, Any]:
        self.status_calls.append(run_id)
        return {"run_id": run_id, "status": "pending"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SWITCH, raising=False)
    monkeypatch.delenv(TASK_CENTER_SWITCH, raising=False)


# --- the master switch is the gate ----------------------------------------


def test_the_server_switch_alone_decides_when_nothing_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert optin.decide_runtime_opt_in(_task()).use_runtime is False

    monkeypatch.setenv(SWITCH, "1")
    decision = optin.decide_runtime_opt_in(_task())

    assert decision.use_runtime is True
    assert decision.reason == "runtime_enabled_by_server"


def test_a_task_centered_automation_keeps_the_existing_opt_in_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")

    decision = optin.decide_runtime_opt_in(_task(execution_mode="task_center"))

    assert decision.use_runtime is True
    assert decision.reason == "execution_mode_opt_in"
    assert decision.execution_mode == "task_center"


@pytest.mark.parametrize("mode", ["task_center", "plan", "unattended", "task_center_plan"])
def test_every_recognised_opt_in_value_is_accepted(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv(SWITCH, "1")

    assert optin.decide_runtime_opt_in(_task(execution_mode=mode)).use_runtime is True


# --- an explicit opt-out wins ---------------------------------------------


@pytest.mark.parametrize(
    "mode",
    ["direct", "direct_langgraph", "langgraph", "execute", "runs_wait", "Direct-LangGraph"],
)
def test_an_explicit_opt_out_is_honoured_even_with_every_switch_on(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    monkeypatch.setenv(TASK_CENTER_SWITCH, "1")

    decision = optin.decide_runtime_opt_in(_task(execution_mode=mode))

    assert decision.use_runtime is False
    assert decision.reason == "execution_mode_opt_out"


async def test_an_opted_out_task_establishes_no_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()

    result = await optin.establish_runtime_run(
        _task(execution_mode="direct_langgraph"),
        identity=_identity(),
        authorized=True,
        contract=contract,
    )

    assert result.decision == "legacy"
    assert result.reason == "execution_mode_opt_out"
    optin.assert_no_runtime_side_effects(result)
    assert contract.create_calls == []
    assert contract.status_calls == []


def test_the_prompt_execution_mode_field_is_read_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")

    assert (
        optin.decide_runtime_opt_in(_task(prompt_execution_mode="direct")).use_runtime is False
    )
    assert (
        optin.decide_runtime_opt_in(_task(prompt_execution_mode="task_center")).use_runtime is True
    )


# --- an invalid value never opts in ---------------------------------------


@pytest.mark.parametrize("mode", ["banana", "task-center_plan_v2", "1", "TASK CENTER"])
def test_an_invalid_execution_mode_never_opts_in(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv(SWITCH, "1")

    decision = optin.decide_runtime_opt_in(_task(execution_mode=mode))

    assert decision.use_runtime is False
    assert decision.reason == "invalid_execution_mode"
    assert decision.execution_mode == mode.strip().lower().replace("-", "_")


async def test_an_invalid_execution_mode_falls_back_to_the_existing_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()

    result = await optin.establish_runtime_run(
        _task(execution_mode="banana"), identity=_identity(), authorized=True, contract=contract
    )

    assert result.decision == "legacy"
    assert result.reason == "invalid_execution_mode"
    assert contract.create_calls == []


def test_classification_is_explicit_about_what_it_saw() -> None:
    assert optin.classify_execution_mode(_task()).kind == "default"
    assert optin.classify_execution_mode(_task(execution_mode=" task_center ")).kind == "opt_in"
    assert optin.classify_execution_mode(_task(execution_mode="PLAN")).value == "plan"
    assert optin.classify_execution_mode(None).kind == "default"
    assert optin.classify_execution_mode(_task(execution_mode="nope")).was_explicit is False
    # Whitespace is absence, not an invalid value.
    assert optin.classify_execution_mode(_task(execution_mode="   ")).kind == "default"
    # A list where a string belongs is invalid, never a lucky match.
    assert optin.classify_execution_mode(_task(execution_mode=["task_center"])).kind == "invalid"


# --- the default is not flipped -------------------------------------------


def test_prompt_only_automations_are_not_defaulted_into_the_runtime() -> None:
    # No switch, no explicit mode: the Runtime must not be selected.
    assert optin.decide_runtime_opt_in(_task(run_mode="prompt")).use_runtime is False
    assert optin.decide_runtime_opt_in(None).use_runtime is False


def test_the_two_switch_names_stay_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    # The Runtime switch must not be the Task Center routing switch: opting into
    # the Task Center pipeline must not silently opt into the Runtime.
    monkeypatch.setenv(TASK_CENTER_SWITCH, "1")

    assert optin.runtime_unattended_enabled() is False
    assert optin.decide_runtime_opt_in(_task()).use_runtime is False


# --- the mode vocabulary cannot drift from the automation runner ----------


def test_the_mode_vocabulary_matches_the_automation_runner() -> None:
    runner = ast.parse(Path("app/gateway/automation_runner.py").read_text(encoding="utf-8"))
    helper = next(
        node
        for node in ast.walk(runner)
        if isinstance(node, ast.FunctionDef) and node.name == "_automation_via_task_center"
    )

    literals: set[str] = set()
    for node in ast.walk(helper):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name):
            if node.left.id != "explicit":
                continue
            for comparator in node.comparators:
                if isinstance(comparator, ast.Tuple):
                    for element in comparator.elts:
                        if isinstance(element, ast.Constant) and isinstance(element.value, str):
                            literals.add(element.value)

    recognisable = set(optin._EXPLICIT_OPT_IN_MODES) | set(optin._EXPLICIT_OPT_OUT_MODES)
    assert literals, "the automation runner's vocabulary could not be read"
    assert literals <= recognisable, "a value the runner honours is unknown to the Runtime opt-in"


def test_the_direct_langgraph_path_is_not_imported_here() -> None:
    tree = ast.parse(Path("app/gateway/task_runtime_optin.py").read_text(encoding="utf-8"))
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    offenders = [
        name for name in imported if "automation_runner" in name or "langgraph" in name.lower()
    ]
    assert not offenders, (
        "the opt-in boundary must not import the LangGraph execution stack; the mode "
        "vocabulary is duplicated and asserted instead"
    )
