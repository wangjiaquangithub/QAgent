from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.pool import StaticPool

from app.qagent_runtime.auth import RuntimePrincipal, get_runtime_principal
from app.qagent_runtime.contract import AssetMetadata, ExecutionResult, PlanResult
from app.qagent_runtime.repository import RuntimeRepository
from app.qagent_runtime.router import router
from app.qagent_runtime.service import RuntimeService


class RouterPlanner:
    def plan(self, *, task_id: str, input_payload: dict[str, Any]) -> PlanResult:
        return PlanResult(
            plan={"version": "router-test-v1", "task_id": task_id, "steps": []},
            recovery_context={"next_step": "server-subtask-1"},
        )


class RouterExecutor:
    def execute(
        self,
        *,
        run_id: str,
        task_id: str,
        input_payload: dict[str, Any],
        plan: dict[str, Any],
    ) -> ExecutionResult:
        return ExecutionResult(
            result={"ok": True},
            assets=[
                AssetMetadata(
                    asset_id=f"router-asset-{run_id}",
                    asset_type="router_test",
                    uri=f"qagent://runs/{run_id}/router-result",
                )
            ],
        )


@pytest.fixture
def unauthenticated_app() -> FastAPI:
    engine = sa.create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    repository = RuntimeRepository(engine, create_schema=True)
    application = FastAPI()
    application.include_router(router)
    application.state.qagent_runtime_service = RuntimeService(
        repository, planner=RouterPlanner(), executor=RouterExecutor()
    )
    yield application
    engine.dispose()


@pytest.fixture
def app() -> FastAPI:
    engine = sa.create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    repository = RuntimeRepository(engine, create_schema=True)
    service = RuntimeService(repository, planner=RouterPlanner(), executor=RouterExecutor())
    application = FastAPI()
    application.include_router(router)
    application.state.qagent_runtime_service = service
    application.dependency_overrides[get_runtime_principal] = lambda: RuntimePrincipal(
        org_id="identity:user:org-a",
        subject_id="user-a",
        identity_type="user",
    )
    yield application
    engine.dispose()


@pytest.mark.asyncio
async def test_runtime_http_and_sse_contract(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created_response = await client.post(
            "/api/qagent/runtime/runs",
            json={"task_id": "router-task", "input_payload": {"x": 1}},
        )
        assert created_response.status_code == 201
        created = created_response.json()
        run_id = created["run_id"]

        waiting_response = await client.post(f"/api/qagent/runtime/runs/{run_id}/start")
        assert waiting_response.status_code == 200
        waiting = waiting_response.json()
        approval_id = waiting["approval"]["approval_id"]
        assert waiting["status"] == "waiting_approval"

        granted_response = await client.post(
            f"/api/qagent/runtime/runs/{run_id}/approvals/{approval_id}/grant",
            json={"decided_by": "api-user", "reason": "approved"},
        )
        assert granted_response.status_code == 200
        assert granted_response.json()["status"] == "completed"
        approval = app.state.qagent_runtime_service.repository.get_approval(approval_id)
        assert approval["decided_by"] == "user-a"

        result_response = await client.get(f"/api/qagent/runtime/runs/{run_id}/result")
        assert result_response.status_code == 200
        assert result_response.json()["assets"][0]["asset_type"] == "router_test"

        sse_response = await client.get(f"/api/qagent/runtime/runs/{run_id}/events")
        assert sse_response.status_code == 200
        assert "event: run.created" in sse_response.text
        assert "event: asset.available" in sse_response.text
        assert '"run_id": "' + run_id + '"' in sse_response.text
        assert "langgraph" not in sse_response.text.lower()
        assert "thread_id" not in sse_response.text


@pytest.mark.asyncio
async def test_approval_url_must_match_approval_run(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = (await client.post("/api/qagent/runtime/runs", json={"task_id": "first"})).json()
        second = (await client.post("/api/qagent/runtime/runs", json={"task_id": "second"})).json()
        first_waiting = (await client.post(f"/api/qagent/runtime/runs/{first['run_id']}/start")).json()
        approval_id = first_waiting["approval"]["approval_id"]

        wrong_path = await client.post(
            f"/api/qagent/runtime/runs/{second['run_id']}/approvals/{approval_id}/grant",
            json={"decided_by": "bad-route"},
        )
        assert wrong_path.status_code == 404
        assert app.state.qagent_runtime_service.repository.get_approval(approval_id)["status"] == "requested"


@pytest.mark.asyncio
async def test_run_read_is_rejected_across_organizations(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created_response = await client.post(
            "/api/qagent/runtime/runs",
            json={"task_id": "org-a-task", "input_payload": {"x": 1}},
        )
        assert created_response.status_code == 201
        run_id = created_response.json()["run_id"]

        app.dependency_overrides[get_runtime_principal] = lambda: RuntimePrincipal(
            org_id="identity:user:org-b",
            subject_id="user-b",
            identity_type="user",
        )
        other_org_response = await client.get(f"/api/qagent/runtime/runs/{run_id}")

    assert other_org_response.status_code == 404


@pytest.mark.asyncio
async def test_runtime_routes_require_bearer_auth(unauthenticated_app: FastAPI) -> None:
    run_id = "run_missing_auth"
    approval_id = "approval_missing_auth"
    requests = [
        ("POST", f"/api/qagent/runtime/runs/{run_id}/start"),
        ("GET", f"/api/qagent/runtime/runs/{run_id}/events"),
        ("POST", f"/api/qagent/runtime/runs/{run_id}/cancel"),
        ("POST", f"/api/qagent/runtime/runs/{run_id}/resume"),
        ("GET", f"/api/qagent/runtime/runs/{run_id}/result"),
        ("POST", f"/api/qagent/runtime/runs/{run_id}/approvals/{approval_id}/grant"),
        ("POST", f"/api/qagent/runtime/runs/{run_id}/approvals/{approval_id}/reject"),
    ]

    async with AsyncClient(transport=ASGITransport(app=unauthenticated_app), base_url="http://test") as client:
        for method, path in requests:
            response = await client.request(method, path, json={} if method == "POST" else None)
            assert response.status_code == 401, (method, path, response.text)
            assert response.headers.get("www-authenticate") == "Bearer"


@pytest.mark.asyncio
async def test_all_run_and_approval_routes_are_organization_scoped(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/api/qagent/runtime/runs",
            json={"task_id": "org-a-scope"},
        )
        assert created.status_code == 201
        run_id = created.json()["run_id"]
        waiting = await client.post(f"/api/qagent/runtime/runs/{run_id}/start")
        assert waiting.status_code == 200
        approval_id = waiting.json()["approval"]["approval_id"]

        app.dependency_overrides[get_runtime_principal] = lambda: RuntimePrincipal(
            org_id="identity:user:org-b",
            subject_id="user-b",
            identity_type="user",
        )
        requests = [
            ("POST", f"/api/qagent/runtime/runs/{run_id}/start"),
            ("GET", f"/api/qagent/runtime/runs/{run_id}/events"),
            ("POST", f"/api/qagent/runtime/runs/{run_id}/cancel"),
            ("POST", f"/api/qagent/runtime/runs/{run_id}/resume"),
            ("GET", f"/api/qagent/runtime/runs/{run_id}/result"),
            ("POST", f"/api/qagent/runtime/runs/{run_id}/approvals/{approval_id}/grant"),
            ("POST", f"/api/qagent/runtime/runs/{run_id}/approvals/{approval_id}/reject"),
        ]
        for method, path in requests:
            response = await client.request(method, path, json={} if method == "POST" else None)
            assert response.status_code == 404, (method, path, response.text)

        approval = app.state.qagent_runtime_service.repository.get_approval(approval_id)
        assert approval["status"] == "requested"


@pytest.mark.asyncio
async def test_reject_approval_uses_authenticated_subject_not_request_body(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/api/qagent/runtime/runs",
            json={"task_id": "router-reject"},
        )
        run_id = created.json()["run_id"]
        waiting = await client.post(f"/api/qagent/runtime/runs/{run_id}/start")
        approval_id = waiting.json()["approval"]["approval_id"]

        response = await client.post(
            f"/api/qagent/runtime/runs/{run_id}/approvals/{approval_id}/reject",
            json={"decided_by": "forged-client", "reason": "not approved"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "failed"
        approval = app.state.qagent_runtime_service.repository.get_approval(approval_id)
        assert approval["decided_by"] == "user-a"
