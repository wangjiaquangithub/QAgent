"""Stable QAgent boundary around AgentScope 2.0.8."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from .contract import ExecutionResult, PlanResult
from .provider_factory import create_agentscope_model


class AgentScopeExecutionError(RuntimeError):
    def __init__(self, code: str, message: str = "AgentScope execution failed") -> None:
        super().__init__(message)
        self.code = code


class _RuntimeOutput(BaseModel):
    result: dict[str, Any]
    assets: list[dict[str, Any]] = []


class AgentScopeAdapter:
    """Translate QAgent input to/from AgentScope; no AgentScope types escape."""

    def __init__(self, *, model: Any | None = None, model_factory: Callable[[], Any] | None = None) -> None:
        self._model = model
        self._model_factory = model_factory or create_agentscope_model

    def plan(self, *, task_id: str, input_payload: dict[str, Any]) -> PlanResult:
        # Planning stays a small durable QAgent contract object. Execution below
        # is the real AgentScope call and is never a deterministic fallback.
        plan = {
            "version": "qagent-plan-v1",
            "task_id": task_id,
            "steps": [{"step_id": "agent-scope-execution", "kind": "agentscope"}],
            "planner": "agentscope-adapter",
        }
        return PlanResult(plan=plan, recovery_context={"next_step": "agent-scope-execution"})

    async def execute(
        self,
        *,
        run_id: str,
        task_id: str,
        input_payload: dict[str, Any],
        plan: dict[str, Any],
    ) -> ExecutionResult:
        del plan
        try:
            from agentscope.agent import Agent, ReActConfig
            from agentscope.message import Msg

            model = self._model if self._model is not None else self._model_factory()
            agent = Agent(
                name="qagent-runtime-agent",
                system_prompt="Return a JSON object with a result object and an assets array.",
                model=model,
                react_config=ReActConfig(max_iters=1),
            )
            message = Msg(
                name="user",
                role="user",
                content=[{"type": "text", "text": json.dumps({"task_id": task_id, "run_id": run_id, "input": input_payload}, ensure_ascii=False)}],
            )
            output = await agent.reply(message, structured_schema=_RuntimeOutput)
        except Exception as exc:
            name = type(exc).__name__
            if "Structured" in name or "JSON" in name:
                code = "agentscope_structured_output"
            elif "Timeout" in name:
                code = "agentscope_timeout"
            elif "Rate" in name or "429" in str(exc):
                code = "agentscope_rate_limit"
            elif "Auth" in name or "credential" in str(exc).lower() or "401" in str(exc):
                code = "agentscope_authentication"
            elif getattr(exc, "code", None):
                code = str(exc.code)
            else:
                code = "agentscope_execution"
            raise AgentScopeExecutionError(code) from exc

        structured = getattr(output, "structured_output", None)
        if structured is None:
            raise AgentScopeExecutionError("agentscope_structured_output")
        data = structured if isinstance(structured, dict) else structured.model_dump()
        result = data.get("result")
        if not isinstance(result, dict):
            raise AgentScopeExecutionError("agentscope_structured_output")
        assets = data.get("assets") or []
        if not isinstance(assets, list):
            raise AgentScopeExecutionError("agentscope_structured_output")
        from .contract import AssetMetadata
        normalized_assets = []
        for index, asset in enumerate(assets):
            if not isinstance(asset, dict) or not asset.get("uri"):
                raise AgentScopeExecutionError("agentscope_structured_output")
            normalized_assets.append(AssetMetadata(
                asset_id=str(asset.get("asset_id") or f"asset_{run_id}_{index}"),
                asset_type=str(asset.get("asset_type") or "agentscope_result"),
                uri=str(asset["uri"]),
                name=asset.get("name"),
                content_type=asset.get("content_type"),
                metadata=asset.get("metadata") or {"source": "agentscope"},
            ))
        return ExecutionResult(result=result, assets=normalized_assets)
