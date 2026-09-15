"""Public, AgentScope-neutral runtime contract."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class PlanResult:
    plan: dict[str, Any]
    recovery_context: dict[str, Any]


@dataclass(frozen=True)
class AssetMetadata:
    asset_id: str
    asset_type: str
    uri: str
    name: str | None = None
    content_type: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExecutionResult:
    result: dict[str, Any]
    assets: list[AssetMetadata]


class RuntimeContract(Protocol):
    async def create_run(
        self,
        *,
        org_id: str,
        task_id: str,
        input_payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]: ...

    async def start_run(self, run_id: str, *, org_id: str) -> dict[str, Any]: ...

    async def grant_approval(
        self,
        approval_id: str,
        *,
        decided_by: str | None = None,
        reason: str | None = None,
        org_id: str,
        run_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def get_run_status(self, run_id: str, *, org_id: str) -> dict[str, Any]: ...

    async def stream_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        org_id: str,
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def request_cancel(self, run_id: str, *, org_id: str) -> dict[str, Any]: ...

    async def resume_run(self, run_id: str, *, org_id: str) -> dict[str, Any]: ...

    async def get_result(self, run_id: str, *, org_id: str) -> dict[str, Any]: ...
