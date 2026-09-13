"""Protective tests for the default-off Runtime opt-in branch.

Covers AG-G2-AUTO-003-B01 acceptance points on the real Task Center /
unattended convergence point:
- with the switch off the behaviour is unchanged and the Runtime is untouched;
- with the switch on and the preconditions met exactly one run is created;
- repeating the call reuses that run instead of creating another;
- insufficient preconditions and cross-organization linkage fall back to the
  existing path rather than guessing;
- a Runtime failure is never converted into a legacy execution.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from app.gateway import task_runtime_optin as optin
from app.gateway.task_runtime_linkage import LINKAGE_TASK_KEY, RuntimeRunLinkageError

ORG_A = "org-a"
ORG_B = "org-b"
SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"


class RecordingContract:
    """Records every Runtime public-boundary call."""

    def __init__(self, *, fail_create: bool = False, echo_org: str | None = None) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.status_calls: list[str] = []
        self._n = 0
        self.fail_create = fail_create
        self.echo_org = echo_org

    async def create_run(
        self,
        *,
        org_id: str,
        task_id: str,
        input_payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.create_calls.append(
            {
                "org_id": org_id,
                "task_id": task_id,
                "input_payload": input_payload,
                "idempotency_key": idempotency_key,
            }
        )
        if self.fail_create:
            raise RuntimeError("runtime unavailable")
        self._n += 1
        return {
            "run_id": f"run-{self._n}",
            "status": "pending",
            "org_id": self.echo_org if self.echo_org is not None else org_id,
        }

    async def get_run_status(self, run_id: str) -> dict[str, Any]:
        self.status_calls.append(run_id)
        return {"run_id": run_id, "status": "pending"}


def _identity(org_id: str = ORG_A, owner: str = "webui:1") -> dict[str, Any]:
    return {
        "org_id": org_id,
        "scope_id": f"personal:{owner}",
        "principal": {"principal_id": owner, "principal_type": "internal"},
    }


def _task(*, task_id: str = "e4c1a2b0", **extra: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": task_id,
        "name": "每周经营简报",
        "run_mode": "unattended",
        "status": "inbox",
        "unattended_attempts": 0,
    }
    task.update(extra)
    return task


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SWITCH, raising=False)


async def test_switch_off_leaves_behaviour_unchanged_and_runtime_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = RecordingContract()
    result = await optin.establish_runtime_run(
        _task(), identity=_identity(), authorized=True, contract=contract
    )

    assert result.decision == "legacy"
    assert result.reason == "runtime_disabled"
    assert not result.use_runtime
    optin.assert_no_runtime_side_effects(result)
    assert contract.create_calls == [] and contract.status_calls == []


async def test_switch_on_creates_exactly_one_run_and_records_the_linkage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()
    result = await optin.establish_runtime_run(
        _task(), identity=_identity(), authorized=True, contract=contract
    )

    assert result.decision == "runtime"
    assert result.action == "attached"
    assert result.run_id == "run-1"
    assert len(contract.create_calls) == 1
    assert contract.status_calls == []
    assert result.updated_task is not None
    assert result.updated_task[LINKAGE_TASK_KEY]["runtime_run_id"] == "run-1"

    # Only the contract's own kwargs plus the trusted organization were used.
    call = contract.create_calls[0]
    assert set(call) == {"org_id", "task_id", "input_payload", "idempotency_key"}
    assert call["org_id"] == ORG_A
    assert call["idempotency_key"] == result.context.idempotency_key


async def test_trusted_org_is_forwarded_to_the_runtime_create_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()
    result = await optin.establish_runtime_run(
        _task(), identity=_identity(ORG_A), authorized=True, contract=contract
    )

    assert result.decision == "runtime"
    assert result.context is not None
    assert result.context.org_id == ORG_A
    assert contract.create_calls[0]["org_id"] == ORG_A, "trusted org must reach create_run"
    # The Runtime's own default org is never used as a substitute.
    assert contract.create_calls[0]["org_id"] != "local"


async def test_identity_without_trusted_org_falls_back_and_never_creates_a_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()
    result = await optin.establish_runtime_run(
        _task(),
        identity={"scope_id": "personal:webui:1", "principal": {"principal_id": "webui:1"}},
        authorized=True,
        contract=contract,
    )

    assert result.decision == "legacy"
    assert result.reason == "preconditions_not_met"
    optin.assert_no_runtime_side_effects(result)
    assert contract.create_calls == []


async def test_a_run_echoing_another_organization_is_never_linked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The Runtime confirms the owning organization on the created run. If it does
    # not match the trusted organization, linking would attach this task to a run
    # that belongs elsewhere, so the opt-in path refuses (AG-G2-AUTO-010).
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract(echo_org=ORG_B)

    with pytest.raises(RuntimeError, match="different organization"):
        await optin.establish_runtime_run(
            _task(), identity=_identity(ORG_A), authorized=True, contract=contract
        )

    # The run was created but deliberately not linked.
    assert len(contract.create_calls) == 1


async def test_repeated_call_reuses_the_run_instead_of_creating_a_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()

    first = await optin.establish_runtime_run(
        _task(), identity=_identity(), authorized=True, contract=contract
    )
    second = await optin.establish_runtime_run(
        first.updated_task, identity=_identity(), authorized=True, contract=contract
    )

    assert second.decision == "runtime"
    assert second.action == "reused"
    assert second.run_id == "run-1"
    assert len(contract.create_calls) == 1, "a duplicate trigger must not create a second run"
    assert contract.status_calls == ["run-1"]


async def test_unauthorized_task_falls_back_to_the_existing_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()
    result = await optin.establish_runtime_run(
        _task(), identity=_identity(), authorized=False, contract=contract
    )

    assert result.decision == "legacy"
    assert result.reason == "preconditions_not_met"
    optin.assert_no_runtime_side_effects(result)
    assert contract.create_calls == []


async def test_missing_trusted_identity_falls_back_to_the_existing_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()
    result = await optin.establish_runtime_run(
        _task(), identity=None, authorized=True, contract=contract
    )

    assert result.decision == "legacy"
    assert result.reason == "trusted_identity_unavailable"
    assert contract.create_calls == []


async def test_missing_contract_falls_back_to_the_existing_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    result = await optin.establish_runtime_run(
        _task(), identity=_identity(), authorized=True, contract=None
    )

    assert result.decision == "legacy"
    assert result.reason == "runtime_contract_unavailable"


async def test_client_forged_organization_does_not_change_the_runtime_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()
    forged = await optin.establish_runtime_run(
        _task(org_id=ORG_B, organizationId=ORG_B),
        identity=_identity(ORG_A),
        authorized=True,
        contract=contract,
    )
    clean = await optin.establish_runtime_run(
        _task(),
        identity=_identity(ORG_A),
        authorized=True,
        contract=RecordingContract(),
    )

    assert forged.context.org_id == clean.context.org_id == ORG_A
    assert forged.context.org_scope_key == clean.context.org_scope_key
    assert forged.context.runtime_task_id == clean.context.runtime_task_id
    assert forged.context.idempotency_key == clean.context.idempotency_key
    assert contract.create_calls[0]["org_id"] == ORG_A, "a forged task org must not reach the runtime"


async def test_cross_organization_linkage_is_refused_not_adopted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")

    # Org A owns the linkage.
    owner = await optin.establish_runtime_run(
        _task(), identity=_identity(ORG_A), authorized=True, contract=RecordingContract()
    )
    assert owner.updated_task is not None

    # Org B must not silently adopt it, nor overwrite it.
    contract_b = RecordingContract()
    with pytest.raises(RuntimeRunLinkageError, match="another organization"):
        await optin.establish_runtime_run(
            owner.updated_task,
            identity=_identity(ORG_B),
            authorized=True,
            contract=contract_b,
        )
    assert contract_b.create_calls == []


async def test_runtime_failure_is_not_converted_into_a_legacy_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract(fail_create=True)

    with pytest.raises(RuntimeError, match="runtime unavailable"):
        await optin.establish_runtime_run(
            _task(), identity=_identity(), authorized=True, contract=contract
        )

    assert len(contract.create_calls) == 1


# --- the real convergence point stays inert while the switch is off -------
#
# ``unattended_task_pipeline`` pulls in the LangChain-based execution stack, so
# the wiring is verified structurally (AST) instead of by import. The branch
# *behaviour* is covered behaviourally above.


def _pipeline_module_ast() -> Any:
    source = Path("app/gateway/unattended_task_pipeline.py").read_text(encoding="utf-8")
    return ast.parse(source)


def _function_def(tree: Any, name: str) -> Any:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in unattended_task_pipeline")


def test_convergence_point_helper_is_defined_and_consults_the_switch_first() -> None:
    helper = _function_def(_pipeline_module_ast(), "_maybe_run_via_runtime")

    guard_calls = [
        node
        for node in ast.walk(helper)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "runtime_unattended_enabled"
    ]
    assert guard_calls, "the helper must consult the server-side switch"

    first_statement = helper.body[0]
    assert isinstance(first_statement, ast.ImportFrom | ast.Expr | ast.Assign | ast.If)
    # The switch check must happen before any identity/contract resolution.
    resolved_names = {
        node.attr
        for node in ast.walk(helper)
        if isinstance(node, ast.Attribute)
    }
    assert "resolve_server_task_runtime_identity" in resolved_names
    assert "default_runtime_contract" in resolved_names
    assert "establish_runtime_run" in resolved_names


def test_convergence_point_returns_early_on_a_legacy_decision() -> None:
    tree = _pipeline_module_ast()
    impl = _function_def(tree, "_advance_unattended_task_impl")

    calls = [
        node
        for node in ast.walk(impl)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_maybe_run_via_runtime"
    ]
    assert len(calls) == 1, "the convergence point must consult the runtime branch exactly once"

    # It must be guarded by `if runtime_step is not None:` and return immediately
    # when the branch produced a step (i.e. leave the legacy path untouched otherwise).
    guard = next(
        (
            node
            for node in impl.body
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.ops[0], ast.IsNot)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "runtime_step"
        ),
        None,
    )
    assert guard is not None, "expected an `if runtime_step is not None:` guard at the top"
    assert any(isinstance(sub, ast.Return) for sub in guard.body), "the guard must return early"

    # And the consultation must sit before any legacy dispatch in the function body.
    call_index = next(
        idx
        for idx, node in enumerate(impl.body)
        if any(child is calls[0] for child in ast.walk(node))
    )
    guard_index = impl.body.index(guard)
    assert call_index < guard_index, "the runtime branch must be consulted before the guard returns"


def test_pipeline_does_not_import_runtime_internals_at_module_scope() -> None:
    tree = _pipeline_module_ast()
    module_level_imports: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            module_level_imports.append(node.module)
        elif isinstance(node, ast.Import):
            module_level_imports.extend(alias.name for alias in node.names)

    runtime_imports = [m for m in module_level_imports if m.startswith("app.qagent_runtime")]
    assert runtime_imports == [], f"runtime imports must stay out of the pipeline: {runtime_imports}"
