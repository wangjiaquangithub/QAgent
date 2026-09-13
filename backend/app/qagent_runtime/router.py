"""Independent HTTP + SSE API for QAgent Runtime."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from .auth import RuntimePrincipal, get_runtime_principal
from .repository import RuntimeRepository
from .service import RuntimeService

router = APIRouter(prefix="/api/qagent/runtime", tags=["qagent-runtime"])


class CreateRunBody(BaseModel):
    task_id: str | None = None
    input_payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class ApprovalBody(BaseModel):
    reason: str | None = None


def configure_runtime_service(request: Request, service: RuntimeService) -> None:
    request.app.state.qagent_runtime_service = service


def _service(request: Request) -> RuntimeService:
    service = getattr(request.app.state, "qagent_runtime_service", None)
    if service is None:
        try:
            service = RuntimeService(RuntimeRepository.from_config())
            request.app.state.qagent_runtime_service = service
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"QAgent Runtime PostgreSQL is unavailable: {exc}") from exc
    return service


def _not_found(exc: KeyError) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Run or approval not found: {exc.args[0]}")


@router.post("/runs", status_code=201)
async def create_run(
    body: CreateRunBody,
    request: Request,
    principal: RuntimePrincipal = Depends(get_runtime_principal),
) -> dict[str, Any]:
    service = _service(request)
    task_id = body.task_id or f"task_{__import__('uuid').uuid4().hex}"
    return await service.create_run(
        org_id=principal.org_id,
        task_id=task_id,
        input_payload=body.input_payload,
        idempotency_key=body.idempotency_key,
    )


@router.post("/runs/{run_id}/start")
async def start_run(
    run_id: str,
    request: Request,
    principal: RuntimePrincipal = Depends(get_runtime_principal),
) -> dict[str, Any]:
    try:
        return await _service(request).start_run(run_id, org_id=principal.org_id)
    except KeyError as exc:
        raise _not_found(exc) from exc


@router.get("/runs/{run_id}")
async def get_run_status(
    run_id: str,
    request: Request,
    principal: RuntimePrincipal = Depends(get_runtime_principal),
) -> dict[str, Any]:
    try:
        return await _service(request).get_run_status(run_id, org_id=principal.org_id)
    except KeyError as exc:
        raise _not_found(exc) from exc


@router.get("/runs/{run_id}/events")
async def stream_events(
    run_id: str,
    request: Request,
    after_sequence: int = Query(0, ge=0),
    principal: RuntimePrincipal = Depends(get_runtime_principal),
) -> EventSourceResponse:
    service = _service(request)
    try:
        service.status(run_id, org_id=principal.org_id)
    except KeyError as exc:
        raise _not_found(exc) from exc

    async def generator():
        async for event in service.stream_events(
            run_id,
            after_sequence=after_sequence,
            org_id=principal.org_id,
        ):
            yield {"id": event["event_id"], "event": event["type"], "data": json.dumps(event, ensure_ascii=False)}

    return EventSourceResponse(generator())


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    request: Request,
    principal: RuntimePrincipal = Depends(get_runtime_principal),
) -> dict[str, Any]:
    try:
        return await _service(request).request_cancel(run_id, org_id=principal.org_id)
    except KeyError as exc:
        raise _not_found(exc) from exc


@router.post("/runs/{run_id}/resume")
async def resume_run(
    run_id: str,
    request: Request,
    principal: RuntimePrincipal = Depends(get_runtime_principal),
) -> dict[str, Any]:
    try:
        return await _service(request).resume_run(run_id, org_id=principal.org_id)
    except KeyError as exc:
        raise _not_found(exc) from exc


@router.get("/runs/{run_id}/result")
async def get_result(
    run_id: str,
    request: Request,
    principal: RuntimePrincipal = Depends(get_runtime_principal),
) -> dict[str, Any]:
    try:
        return await _service(request).get_result(run_id, org_id=principal.org_id)
    except KeyError as exc:
        raise _not_found(exc) from exc


def _approval_for_run(
    service: RuntimeService,
    run_id: str,
    approval_id: str,
    *,
    org_id: str,
) -> None:
    # Validate ownership before the service mutates approval state. This keeps
    # a malformed URL from granting/rejecting an approval belonging to another
    # run.
    service._require_run(run_id, org_id=org_id)
    approval = service.repository.get_approval(
        approval_id,
        org_id=org_id,
        run_id=run_id,
    )
    if not approval:
        raise KeyError(approval_id)


@router.post("/runs/{run_id}/approvals/{approval_id}/grant")
async def grant_approval(
    run_id: str,
    approval_id: str,
    body: ApprovalBody,
    request: Request,
    principal: RuntimePrincipal = Depends(get_runtime_principal),
) -> dict[str, Any]:
    try:
        service = _service(request)
        _approval_for_run(service, run_id, approval_id, org_id=principal.org_id)
        return await service.grant_approval(
            approval_id,
            decided_by=principal.subject_id,
            reason=body.reason,
            org_id=principal.org_id,
            run_id=run_id,
        )
    except KeyError as exc:
        raise _not_found(exc) from exc


@router.post("/runs/{run_id}/approvals/{approval_id}/reject")
async def reject_approval(
    run_id: str,
    approval_id: str,
    body: ApprovalBody,
    request: Request,
    principal: RuntimePrincipal = Depends(get_runtime_principal),
) -> dict[str, Any]:
    try:
        service = _service(request)
        _approval_for_run(service, run_id, approval_id, org_id=principal.org_id)
        return await service.reject_approval(
            approval_id,
            decided_by=principal.subject_id,
            reason=body.reason,
            org_id=principal.org_id,
            run_id=run_id,
        )
    except KeyError as exc:
        raise _not_found(exc) from exc
