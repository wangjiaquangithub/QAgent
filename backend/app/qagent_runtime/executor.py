"""Server-side executor for the QAgent Runtime AgentScope path."""

from __future__ import annotations

import asyncio
from typing import Any

from .agentscope_adapter import AgentScopeAdapter
from .contract import ExecutionResult


class ServerSubtaskExecutor:
    """Delegate the approved run to the stable AgentScope adapter boundary."""

    def __init__(self, adapter: AgentScopeAdapter | None = None) -> None:
        self.adapter = adapter or AgentScopeAdapter()

    async def execute_async(
        self,
        *,
        run_id: str,
        task_id: str,
        input_payload: dict[str, Any],
        plan: dict[str, Any],
    ) -> ExecutionResult:
        return await self.adapter.execute(
            run_id=run_id,
            task_id=task_id,
            input_payload=input_payload,
            plan=plan,
        )

    def execute(
        self,
        *,
        run_id: str,
        task_id: str,
        input_payload: dict[str, Any],
        plan: dict[str, Any],
    ) -> ExecutionResult:
        """Compatibility shim for old synchronous callers.

        RuntimeService uses ``execute_async`` for the real AgentScope path.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.execute_async(
                    run_id=run_id,
                    task_id=task_id,
                    input_payload=input_payload,
                    plan=plan,
                )
            )
        raise RuntimeError("ServerSubtaskExecutor.execute cannot run inside an active event loop")
