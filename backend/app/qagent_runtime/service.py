"""Application service implementing the QAgent Runtime Contract."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from .agentscope_adapter import AgentScopeAdapter
from .contract import RuntimeContract
from .events import INCOMPLETE_STATUSES, TERMINAL_STATUSES, EventType, event_dict
from .executor import ServerSubtaskExecutor
from .repository import RuntimeRepository


def _safe_execution_error(exc: Exception) -> dict[str, str]:
    code = getattr(exc, "code", None) or "agentscope_execution"
    messages = {
        "agentscope_configuration": "AgentScope provider configuration is invalid",
        "agentscope_authentication": "AgentScope authentication failed",
        "agentscope_timeout": "AgentScope execution timed out",
        "agentscope_rate_limit": "AgentScope provider rate limit exceeded",
        "agentscope_structured_output": "AgentScope returned invalid structured output",
        "agentscope_execution": "AgentScope execution failed",
    }
    if code not in messages:
        code = "agentscope_execution"
    return {"code": code, "message": messages[code]}


class RuntimeService(RuntimeContract):
    """Orchestrate a PostgreSQL-backed QAgent run.

    Locks are only an in-process concurrency aid. Run existence, state, event
    sequence, approvals, recovery points, and results are always read/written
    through ``RuntimeRepository``.
    """

    def __init__(
        self,
        repository: RuntimeRepository,
        *,
        planner: AgentScopeAdapter | None = None,
        executor: ServerSubtaskExecutor | None = None,
        execution_claim_timeout_seconds: float | None = None,
    ) -> None:
        self.repository = repository
        self.planner = planner or AgentScopeAdapter()
        self.executor = executor or ServerSubtaskExecutor()
        if execution_claim_timeout_seconds is None:
            raw_timeout = os.getenv("AGENTSCOPE_EXECUTION_CLAIM_TIMEOUT", "300")
            try:
                execution_claim_timeout_seconds = float(raw_timeout)
            except ValueError as exc:
                raise ValueError("AGENTSCOPE_EXECUTION_CLAIM_TIMEOUT must be a number") from exc
        if execution_claim_timeout_seconds <= 0:
            raise ValueError("AGENTSCOPE_EXECUTION_CLAIM_TIMEOUT must be positive")
        self.execution_claim_timeout_seconds = execution_claim_timeout_seconds
        self._run_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, run_id: str) -> asyncio.Lock:
        lock = self._run_locks.get(run_id)
        if lock is None:
            lock = asyncio.Lock()
            self._run_locks[run_id] = lock
        return lock

    async def create_run(
        self,
        *,
        org_id: str = "local",
        task_id: str,
        input_payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.repository.create_run(
            org_id=org_id,
            task_id=task_id,
            input_payload=input_payload,
            idempotency_key=idempotency_key,
        )

    async def start_run(self, run_id: str, org_id: str | None = None) -> dict[str, Any]:
        async with self._lock_for(run_id):
            try:
                return await self._start_run_locked(run_id, org_id=org_id)
            except Exception as exc:
                # A start failure is a Runtime failure, not a reason for a
                # caller to fall back to another executor. Persist the terminal
                # state through the existing Runtime state machine before
                # surfacing the status to the caller.
                self.repository.set_error(
                    run_id,
                    _safe_execution_error(exc),
                    org_id=org_id,
                )
                return self.status(run_id, org_id=org_id)

    async def _start_run_locked(self, run_id: str, *, org_id: str | None = None) -> dict[str, Any]:
        run = self._require_run(run_id, org_id=org_id)
        if run["status"] in TERMINAL_STATUSES or run["status"] == "waiting_approval":
            return self.status(run_id, org_id=org_id)
        if run["status"] in {"created", "queued"}:
            claimed = self.repository.transition(
                run_id,
                "planning",
                EventType.RUN_PLANNING,
                from_statuses={"created", "queued"},
                org_id=org_id,
            )
            if not claimed:
                # Another process owns the planning transition. Do not invoke
                # the planner a second time from a duplicate start request.
                return self.status(run_id, org_id=org_id)
            return await self._plan_and_wait_locked(run_id, org_id=org_id)
        # A planning or running run is resumed explicitly, not restarted by a
        # duplicate start request. This prevents duplicate planner/executor
        # side effects while preserving the durable state machine.
        return self.status(run_id, org_id=org_id)

    async def _plan_and_wait_locked(self, run_id: str, *, org_id: str | None = None) -> dict[str, Any]:
        run = self._require_run(run_id, org_id=org_id)
        if run["status"] == "waiting_approval":
            return self.status(run_id, org_id=org_id)
        if run["status"] != "planning":
            return self.status(run_id, org_id=org_id)

        existing_approval = self.repository.get_latest_approval(run_id, org_id=org_id)
        if run.get("plan_payload") is not None and existing_approval:
            self.repository.transition(
                run_id,
                "waiting_approval",
                EventType.RUN_WAITING_APPROVAL,
                {
                    "approval_id": existing_approval["approval_id"],
                    "plan_version": (run.get("plan_payload") or {}).get("version"),
                    "recovered": True,
                },
                from_statuses={"planning"},
                org_id=org_id,
            )
            return self.status(run_id, org_id=org_id)

        try:
            plan_result = await asyncio.to_thread(
                self.planner.plan,
                task_id=run["task_id"],
                input_payload=run["input_payload"],
            )
            # Cancellation or another terminal transition may have happened
            # while planning. Never create an approval for such a run.
            if self._require_run(run_id, org_id=org_id)["status"] != "planning":
                return self.status(run_id, org_id=org_id)
            approval = self.repository.set_plan_and_approval(
                run_id,
                plan_result.plan,
                plan_result.recovery_context,
                org_id=org_id,
            )
            if not approval:
                return self.status(run_id, org_id=org_id)
            self.repository.save_recovery_point(
                run_id,
                "planning-complete",
                self._last_sequence(run_id, org_id=org_id),
                plan_result.recovery_context,
                org_id=org_id,
            )
            self.repository.transition(
                run_id,
                "waiting_approval",
                EventType.RUN_WAITING_APPROVAL,
                {
                    "approval_id": approval["approval_id"],
                    "plan_version": plan_result.plan.get("version"),
                },
                from_statuses={"planning"},
                org_id=org_id,
            )
        except Exception as exc:
            self.repository.set_error(
                run_id,
                {"code": type(exc).__name__, "message": str(exc)},
                org_id=org_id,
            )
        return self.status(run_id, org_id=org_id)

    async def grant_approval(
        self,
        approval_id: str,
        *,
        decided_by: str | None = None,
        reason: str | None = None,
        org_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        approval = self.repository.get_approval(approval_id, org_id=org_id, run_id=run_id)
        if not approval:
            raise KeyError(approval_id)
        approval_run_id = approval["run_id"]
        async with self._lock_for(approval_run_id):
            approval, changed = self.repository.decide_approval_atomically(
                approval_id,
                status="granted",
                decided_by=decided_by,
                reason=reason,
                org_id=org_id,
                run_id=approval_run_id,
            )
            if not approval:
                raise KeyError(approval_id)
            if changed:
                await self._execute_locked(approval_run_id, org_id=org_id)
            return self.status(approval_run_id, org_id=org_id)

    async def reject_approval(
        self,
        approval_id: str,
        *,
        decided_by: str | None = None,
        reason: str | None = None,
        org_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        approval = self.repository.get_approval(approval_id, org_id=org_id, run_id=run_id)
        if not approval:
            raise KeyError(approval_id)
        approval_run_id = approval["run_id"]
        async with self._lock_for(approval_run_id):
            approval, _changed = self.repository.decide_approval_atomically(
                approval_id,
                status="rejected",
                decided_by=decided_by,
                reason=reason,
                org_id=org_id,
                run_id=approval_run_id,
            )
            if not approval:
                raise KeyError(approval_id)
            return self.status(approval_run_id, org_id=org_id)

    async def _execute_locked(
        self,
        run_id: str,
        *,
        org_id: str | None = None,
        reclaim: bool = False,
    ) -> None:
        run = self._require_run(run_id, org_id=org_id)
        if run["status"] not in {"running", "executing"} or run.get("result_payload") is not None:
            return
        is_async_executor = callable(getattr(self.executor, "execute_async", None))
        claim = self.repository.claim_execution(
            run_id,
            org_id=org_id,
            emit_event=is_async_executor,
            reclaim=reclaim,
        )
        if claim is None:
            return
        if is_async_executor:
            self.repository.save_recovery_point(
                run_id,
                "execution-started",
                self._last_sequence(run_id, org_id=org_id),
                {"execution_claim": claim, "execution_epoch": self._require_run(run_id, org_id=org_id).get("execution_epoch", 0)},
                org_id=org_id,
            )
        try:
            if is_async_executor:
                execution = await self.executor.execute_async(
                    run_id=run_id,
                    task_id=run["task_id"],
                    input_payload=run["input_payload"],
                    plan=run.get("plan_payload") or {},
                )
            else:
                execution = await asyncio.to_thread(
                    self.executor.execute,
                    run_id=run_id,
                    task_id=run["task_id"],
                    input_payload=run["input_payload"],
                    plan=run.get("plan_payload") or {},
                )
            self.repository.set_result(
                run_id,
                execution.result,
                [asset.__dict__ for asset in execution.assets],
                org_id=org_id,
                execution_claim=claim,
            )
            if is_async_executor:
                self.repository.save_recovery_point(
                    run_id,
                    "execution-completed",
                    self._last_sequence(run_id, org_id=org_id),
                    {"execution_claim": claim, "asset_count": len(execution.assets)},
                    org_id=org_id,
                )
        except Exception as exc:
            error = _safe_execution_error(exc)
            self.repository.save_recovery_point(
                run_id,
                "execution-failed",
                self._last_sequence(run_id, org_id=org_id),
                {"execution_claim": claim, "error_code": error["code"]},
                org_id=org_id,
            )
            self.repository.set_error(run_id, error, org_id=org_id, execution_claim=claim)

    async def get_run_status(self, run_id: str, *, org_id: str | None = None) -> dict[str, Any]:
        return self.status(run_id, org_id=org_id)

    async def stream_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        org_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        self._require_run(run_id, org_id=org_id)
        sequence = after_sequence
        while True:
            events = self.repository.list_events(run_id, after_sequence=sequence, org_id=org_id)
            for event in events:
                normalized = event_dict(event)
                sequence = normalized["sequence"]
                yield normalized
            run = self._require_run(run_id, org_id=org_id)
            if run["status"] in TERMINAL_STATUSES and not events:
                break
            await asyncio.sleep(0.2)

    async def request_cancel(self, run_id: str, org_id: str | None = None) -> dict[str, Any]:
        async with self._lock_for(run_id):
            self._require_run(run_id, org_id=org_id)
            self.repository.transition(
                run_id,
                "cancelled",
                EventType.RUN_CANCELLED,
                {},
                from_statuses=INCOMPLETE_STATUSES,
                org_id=org_id,
            )
            return self.status(run_id, org_id=org_id)

    async def resume_run(self, run_id: str, org_id: str | None = None) -> dict[str, Any]:
        async with self._lock_for(run_id):
            run = self._require_run(run_id, org_id=org_id)
            if run["status"] in TERMINAL_STATUSES:
                return self.status(run_id, org_id=org_id)
            self.repository.increment_resume(run_id, org_id=org_id)
            run = self._require_run(run_id, org_id=org_id)
            if run["status"] == "waiting_approval":
                return self.status(run_id, org_id=org_id)
            if run["status"] in {"created", "queued"}:
                claimed = self.repository.transition(
                    run_id,
                    "planning",
                    EventType.RUN_PLANNING,
                    {"recovered": True},
                    from_statuses={"created", "queued"},
                    org_id=org_id,
                )
                if not claimed:
                    return self.status(run_id, org_id=org_id)
                return await self._plan_and_wait_locked(run_id, org_id=org_id)
            if run["status"] == "planning":
                return await self._plan_and_wait_locked(run_id, org_id=org_id)
            if run["status"] in {"running", "executing"}:
                claim = run.get("execution_claim")
                claimed_at = run.get("execution_claimed_at")
                reclaim = False
                if claim and claimed_at is not None:
                    if claimed_at.tzinfo is None:
                        claimed_at = claimed_at.replace(tzinfo=UTC)
                    reclaim = datetime.now(UTC) - claimed_at >= timedelta(
                        seconds=self.execution_claim_timeout_seconds
                    )
                if not claim or reclaim:
                    await self._execute_locked(run_id, org_id=org_id, reclaim=reclaim)
            return self.status(run_id, org_id=org_id)

    async def get_result(self, run_id: str, org_id: str | None = None) -> dict[str, Any]:
        run = self._require_run(run_id, org_id=org_id)
        return {
            "run_id": run_id,
            "status": run["status"],
            "result": run.get("result_payload"),
            "assets": self.repository.list_assets(run_id, org_id=org_id),
            "error": run.get("error_payload"),
        }

    def status(self, run_id: str, *, org_id: str | None = None) -> dict[str, Any]:
        run = self._require_run(run_id, org_id=org_id)
        approval = self.repository.get_latest_approval(run_id, org_id=org_id)
        return {
            "run_id": run["run_id"],
            "task_id": run["task_id"],
            "org_id": run["org_id"],
            "status": run["status"],
            "version": run["version"],
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
            "resume_count": run["resume_count"],
            "plan": run.get("plan_payload"),
            "approval": approval,
            "error": run.get("error_payload"),
        }

    def _last_sequence(self, run_id: str, *, org_id: str | None = None) -> int:
        events = self.repository.list_events(run_id, org_id=org_id)
        return int(events[-1]["sequence"]) if events else 0

    def _require_run(self, run_id: str, *, org_id: str | None = None) -> dict[str, Any]:
        run = self.repository.get_run(run_id, org_id=org_id)
        if not run:
            raise KeyError(run_id)
        return run


async def recover_incomplete_runs(service: RuntimeService) -> list[str]:
    """Recover runs selected from PostgreSQL, never from an in-memory registry."""
    recovered: list[str] = []
    for run in service.repository.list_incomplete_runs():
        await service.resume_run(run["run_id"], org_id=run["org_id"])
        recovered.append(run["run_id"])
    return recovered
