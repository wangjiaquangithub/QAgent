"""Protective tests for Task stream recovery after a disconnect.

Covers AG-G2-AUTO-019: after a dropped connection a client can read the current
projection from already persisted state, with no dependence on in-process memory,
without re-executing the run and without duplicating a history record.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from app.gateway.task_runtime_context import build_task_runtime_context
from app.gateway.task_runtime_cursor import CURSOR_TASK_KEY
from app.gateway.task_runtime_event_bridge import apply_runtime_event
from app.gateway.task_runtime_linkage import (
    LINKAGE_TASK_KEY,
    RuntimeRunLinkageError,
    link_runtime_run,
)
from app.gateway.task_runtime_projection import HISTORY_FIELD
from app.gateway.task_runtime_recovery import (
    RuntimeRecoveryError,
    read_task_stream_view,
    recover_task_stream,
)

ORG_A = "org-a"
ORG_B = "org-b"
TASK_ID = "e4c1a2b0"
RUN_ID = "run-1"
SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"


class RecordingContract:
    """Records every Runtime public-boundary call. Never performs I/O."""

    def __init__(self, *, status: Any = None) -> None:
        self.status_calls: list[str] = []
        self.create_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self._status = {"run_id": RUN_ID, "status": "running"} if status is None else status

    async def get_run_status(self, run_id: str) -> dict[str, Any]:
        self.status_calls.append(run_id)
        return self._status

    async def create_run(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        self.create_calls.append("called")
        raise AssertionError("recovery must never create a run")

    async def request_cancel(self, run_id: str) -> dict[str, Any]:  # pragma: no cover
        self.cancel_calls.append(run_id)
        raise AssertionError("recovery must never cancel a run")


def _authz(org_id: str = ORG_A) -> dict[str, Any]:
    return {
        "org_id": org_id,
        "principal": {"principal_id": "webui:1", "principal_type": "internal"},
        "scope_id": "personal:webui:1",
        "is_org_admin": False,
    }


def _task(*, task_id: str = TASK_ID, status: str = "pending", **extra: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": task_id,
        "name": "每周经营简报",
        "run_mode": "unattended",
        "status": status,
        "unattended_attempts": 0,
        HISTORY_FIELD: [],
    }
    task.update(extra)
    return task


def _ctx(org_id: str = ORG_A, *, task_id: str = TASK_ID) -> Any:
    return build_task_runtime_context(
        task=_task(task_id=task_id), authz=_authz(org_id), authorized=True
    )


def _linked(*, status: str = "pending", org_id: str = ORG_A) -> tuple[dict[str, Any], Any]:
    ctx = _ctx(org_id)
    task, _ = link_runtime_run(_task(status=status), context=ctx, runtime_run_id=RUN_ID)
    return task, ctx


def _reload(task: dict[str, Any]) -> dict[str, Any]:
    """Round-trip a task row through the real persistence mapper.

    This is what a process that has never seen the task loads: the known columns
    plus the extras JSON blob. Anything a restart loses, this loses too.
    """
    from evoflow.persistence import task_row_mappers as m

    known, extra_json = m._split_extra(dict(task), m._TASK_KNOWN)
    return m._merge_extra(known, extra_json)


def _apply(current: dict[str, Any], ctx: Any, *, sequence: int, event_type: str) -> dict[str, Any]:
    frame = {
        "event_id": f"ev-{event_type}-{sequence}",
        "run_id": RUN_ID,
        "sequence": sequence,
        "type": event_type,
        "payload": {},
    }
    updated, _ = apply_runtime_event(current, context=ctx, event=frame)
    return updated


# --- the view is built from persisted state only ---------------------------


def test_an_unlinked_task_still_produces_a_view() -> None:
    ctx = _ctx()
    task = _task(status="executing", execution_history=[{"source": "local", "note": "x"}])
    task[CURSOR_TASK_KEY] = {"run_id": RUN_ID}  # ignored: nothing is linked

    view = read_task_stream_view(task, context=ctx)

    assert view.linked is False
    assert view.runtime_run_id is None
    assert view.status == "executing"
    assert len(view.history) == 1
    assert view.watermark is None


def test_a_reloaded_row_yields_the_same_view_as_the_live_one() -> None:
    task, ctx = _linked()
    task = _apply(task, ctx, sequence=5, event_type="run.running")

    live = read_task_stream_view(task, context=ctx)
    after_restart = read_task_stream_view(_reload(task), context=ctx)

    assert after_restart == live
    assert after_restart.status == "executing"
    assert after_restart.runtime_run_id == RUN_ID
    assert after_restart.watermark == 5
    assert len(after_restart.history) == 1


def test_the_recorded_projection_survives_a_restart_read() -> None:
    from evoflow.persistence import task_row_mappers as m

    task, ctx = _linked()
    task = _apply(task, ctx, sequence=1, event_type="run.queued")
    task = _apply(task, ctx, sequence=4, event_type="run.planning")

    reloaded = _reload(task)
    view = read_task_stream_view(reloaded, context=ctx)

    assert view.status == "planning"
    assert [entry.get("runtime_status") for entry in view.history] == ["queued", "planning"]
    assert view.watermark == 4
    # The linkage and the cursor both live in the same pre-existing extras slot.
    assert LINKAGE_TASK_KEY in reloaded
    assert CURSOR_TASK_KEY in reloaded
    known, extra_json = m._split_extra(reloaded, m._TASK_KNOWN)
    assert CURSOR_TASK_KEY in extra_json


# --- recovery without touching the Runtime ---------------------------------


async def test_a_pure_read_never_calls_the_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    task, ctx = _linked()
    task = _apply(task, ctx, sequence=3, event_type="run.running")
    contract = RecordingContract()

    outcome = await recover_task_stream(
        _reload(task), context=ctx, contract=contract, catch_up=False
    )

    assert outcome.action == "read"
    assert outcome.reason == "read_from_stored_state"
    assert outcome.view.status == "executing"
    assert outcome.view.watermark == 3
    assert contract.status_calls == []
    assert contract.create_calls == []


async def test_recovery_of_an_unlinked_task_reports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    contract = RecordingContract()

    outcome = await recover_task_stream(_task(), context=_ctx(), contract=contract)

    assert outcome.action == "not_linked"
    assert outcome.reason == "no_runtime_linkage"
    assert outcome.view.linked is False
    assert contract.status_calls == []


# --- catch-up --------------------------------------------------------------


async def test_a_reconnect_catches_up_from_the_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    task, ctx = _linked()
    contract = RecordingContract(status={"run_id": RUN_ID, "status": "executing"})

    outcome = await recover_task_stream(_reload(task), context=ctx, contract=contract)

    assert outcome.action == "caught_up"
    assert outcome.changed is True
    assert contract.status_calls == [RUN_ID]
    assert contract.create_calls == []
    assert outcome.view.status == "executing"
    assert len(outcome.view.history) == 1
    # A status read carries no stream position, so a catch-up cannot invent one.
    assert outcome.view.watermark is None


async def test_recovering_twice_does_not_duplicate_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    task, ctx = _linked()
    task = _apply(task, ctx, sequence=5, event_type="run.running")
    reloaded = _reload(task)

    first = await recover_task_stream(reloaded, context=ctx, contract=RecordingContract())
    second = await recover_task_stream(
        first.updated_task or reloaded, context=ctx, contract=RecordingContract()
    )

    assert first.action == "unchanged"
    assert second.action == "unchanged"
    assert second.reason == "duplicate_status"
    assert len(read_task_stream_view(reloaded, context=ctx).history) == 1


async def test_a_terminal_task_is_not_re_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    task, ctx = _linked(status="completed")
    contract = RecordingContract()

    outcome = await recover_task_stream(_reload(task), context=ctx, contract=contract)

    assert outcome.action == "terminal"
    assert outcome.view.terminal is True
    assert outcome.view.status == "completed"
    assert contract.status_calls == []


# --- the Runtime being unavailable is not a failure of the view ------------


async def test_the_switch_being_off_still_returns_the_persisted_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SWITCH, raising=False)
    task, ctx = _linked()
    task = _apply(task, ctx, sequence=2, event_type="run.running")
    contract = RecordingContract()

    outcome = await recover_task_stream(_reload(task), context=ctx, contract=contract)

    assert outcome.action == "disabled"
    assert outcome.view.status == "executing"
    assert outcome.view.watermark == 2
    assert contract.status_calls == []


async def test_a_missing_contract_still_returns_the_persisted_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    task, ctx = _linked()

    outcome = await recover_task_stream(_reload(task), context=ctx, contract=None)

    assert outcome.action == "runtime_unavailable"
    assert outcome.reason == "runtime_contract_unavailable"
    assert outcome.view.runtime_run_id == RUN_ID


@pytest.mark.parametrize("answer", ["not-a-mapping", {"run_id": RUN_ID}, {}, {"status": ""}])
async def test_an_unusable_answer_still_returns_the_persisted_view(
    monkeypatch: pytest.MonkeyPatch, answer: Any
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    task, ctx = _linked()

    outcome = await recover_task_stream(
        _reload(task), context=ctx, contract=RecordingContract(status=answer)
    )

    assert outcome.action == "runtime_unavailable"
    assert outcome.view.runtime_run_id == RUN_ID
    assert outcome.view.status == "pending"


# --- a broken cursor must not hide the task --------------------------------


def test_a_malformed_cursor_is_reported_instead_of_hiding_the_task() -> None:
    task, ctx = _linked()
    broken = dict(task)
    broken[CURSOR_TASK_KEY] = {"run_id": RUN_ID}  # incomplete

    view = read_task_stream_view(broken, context=ctx)

    assert view.cursor_rejected is True
    assert view.watermark is None
    assert view.linked is True
    assert view.status == "pending"


async def test_recovery_still_works_with_a_malformed_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SWITCH, "1")
    task, ctx = _linked()
    task = _apply(task, ctx, sequence=3, event_type="run.running")
    broken = dict(task)
    broken[CURSOR_TASK_KEY] = "not-a-mapping"

    outcome = await recover_task_stream(broken, context=ctx, contract=RecordingContract())

    assert outcome.view.cursor_rejected is True
    assert outcome.view.status == "executing"
    assert len(outcome.view.history) == 1


# --- a foreign linkage is refused, not displayed --------------------------


def test_another_organizations_linkage_is_refused() -> None:
    task, _ = _linked(org_id=ORG_A)

    with pytest.raises(RuntimeRunLinkageError, match="another organization"):
        read_task_stream_view(task, context=_ctx(ORG_B))


async def test_recovery_refuses_another_organizations_linkage() -> None:
    task, _ = _linked(org_id=ORG_A)

    with pytest.raises(RuntimeRunLinkageError, match="another organization"):
        await recover_task_stream(task, context=_ctx(ORG_B), contract=RecordingContract())


def test_another_tasks_linkage_is_refused() -> None:
    task, ctx = _linked()
    copied = dict(task)
    copied["id"] = "deadbeef"

    with pytest.raises(RuntimeRunLinkageError, match="does not belong to this task"):
        read_task_stream_view(copied, context=ctx)


def test_a_view_needs_a_task_row_and_a_trusted_context() -> None:
    _task_row, ctx = _linked()

    with pytest.raises(RuntimeRecoveryError, match="no server-loaded task row"):
        read_task_stream_view(None, context=ctx)
    with pytest.raises(RuntimeRecoveryError, match="no trusted task runtime context"):
        read_task_stream_view(_task(), context="not-a-context")  # type: ignore[arg-type]


# --- the module cannot restart anything ------------------------------------


def test_recovery_has_no_way_to_create_start_resume_or_cancel_a_run() -> None:
    source = Path("app/gateway/task_runtime_recovery.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (called & {"create_run", "start_run", "resume_run", "request_cancel", "cancel"})

    # The Runtime contract it accepts is the read-only slice, unchanged.
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "RuntimeReconcileContract" in imported
