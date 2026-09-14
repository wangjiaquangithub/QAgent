"""Server-config-controlled Runtime opt-in for App / Workflow runs.

AG-G2-APP-003-A01 — first closed loop: one real App entry → one Runtime
execution → terminal projected back onto the existing App Run record.

This module is deliberately inert by default: with the switch off nothing here
reads, creates or cancels a Runtime run, and the existing App Runner /
LangGraph execution path stays exactly as it was.

Mapping authority
-----------------
Field-level mapping follows ``artifacts/acceptance/agentscope-runtime/
AG-G2-APP-002-A01/20260914-a01/app-run-runtime-mapping.md``:

- the Runtime is called through its **public** contract only
  (``RuntimeService.create_run`` / ``start_run`` / ``get_run_status`` /
  ``grant_approval`` / ``get_result``) — the Runtime kernel is never modified;
- the idempotency association is derived from the existing stable App Run id
  (``app-run:{run_id}``), unique per ``(org_id, idempotency_key)`` in the
  Runtime's own store, so a retried trigger can never create a second Runtime
  run. No new column, table or second state source is involved;
- ``input_payload`` is a free-form JSON dict (app_id / app_version /
  parameters / execution_mode); ``task_id`` carries the App Run's existing
  Task Center main-task id;
- only terminal Runtime statuses are projected, and only onto the existing
  ``evoflow_app_runs`` fields through the existing ``update_run_status`` write
  point (``completed_at`` semantics unchanged). ``timed_out → failed`` follows
  the frozen Automation-domain semantics; ``waiting_approval`` is never
  projected;
- a terminal App Run is never overwritten (monotonicity, cf. AG-G2-AUTO-011);
- a Runtime call failure is never converted into a legacy execution — the run
  is marked ``failed`` through the existing terminal write path instead.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Protocol

logger = logging.getLogger(__name__)

__all__ = [
    "app_runtime_bridge_enabled",
    "app_run_idempotency_key",
    "mark_app_run_failed_terminal",
    "project_runtime_result_to_app_run",
    "run_app_workflow_on_runtime",
    "trigger_workflow_app_runtime_run",
]

# Server-side switch. Default off; no client input can influence it.
_ENV_SWITCH = "EVOFLOW_APP_WORKFLOW_RUNTIME"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# The Runtime's own terminal vocabulary (app/qagent_runtime/events.py).
_TERMINAL_RUNTIME_STATUSES = frozenset({"completed", "failed", "cancelled", "timed_out"})
# The App Run's own terminal vocabulary (app_repositories.update_run_status).
_TERMINAL_APP_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})

_RESULT_SUMMARY_MAX_CHARS = 2000
_ERROR_MAX_CHARS = 2000


class RuntimeServiceProtocol(Protocol):
    """Minimal shape of the Runtime public surface used here.

    Only public ``RuntimeService`` methods are called. Tests inject a
    stand-in implementing this protocol; production resolves the real
    ``RuntimeService`` lazily.
    """

    async def create_run(
        self,
        *,
        org_id: str = "local",
        task_id: str,
        input_payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]: ...

    async def start_run(self, run_id: str, org_id: str | None = None) -> dict[str, Any]: ...

    async def get_run_status(self, run_id: str, *, org_id: str | None = None) -> dict[str, Any]: ...

    async def grant_approval(
        self,
        approval_id: str,
        *,
        decided_by: str | None = None,
        reason: str | None = None,
        org_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def get_result(self, run_id: str, org_id: str | None = None) -> dict[str, Any]: ...


def app_runtime_bridge_enabled() -> bool:
    """Server-side opt-in switch. Default off."""
    return os.getenv(_ENV_SWITCH, "").strip().lower() in _TRUTHY


def app_run_idempotency_key(app_run_id: str) -> str:
    """Stable association key derived from the existing App Run id (A02)."""
    return f"app-run:{str(app_run_id or '').strip()}"


def _default_service() -> RuntimeServiceProtocol:
    from app.qagent_runtime.repository import RuntimeRepository
    from app.qagent_runtime.service import RuntimeService

    return RuntimeService(RuntimeRepository.from_config())


def _result_summary(result_payload: Any) -> str:
    if result_payload is None:
        return ""
    if isinstance(result_payload, str):
        summary = result_payload
    elif isinstance(result_payload, dict):
        summary = ""
        for key in ("summary", "answer", "text", "output", "result"):
            value = result_payload.get(key)
            if isinstance(value, str) and value.strip():
                summary = value
                break
        if not summary:
            summary = json.dumps(result_payload, ensure_ascii=False, default=str)
    else:
        summary = str(result_payload)
    return summary.strip()[:_RESULT_SUMMARY_MAX_CHARS]


def _error_message(error_payload: Any, fallback: str) -> str:
    message = fallback
    if isinstance(error_payload, dict):
        message = str(error_payload.get("message") or error_payload.get("code") or fallback)
    elif isinstance(error_payload, str) and error_payload.strip():
        message = error_payload
    return message.strip()[:_ERROR_MAX_CHARS] or fallback


def project_runtime_result_to_app_run(
    app_run_id: str,
    result: dict[str, Any] | None,
) -> str | None:
    """Project a Runtime ``get_result`` payload onto the existing App Run record.

    Write-through onto the existing ``update_run_status`` write point only —
    no new state source, no new fields. Returns the effective App Run status,
    or ``None`` when nothing was written (non-terminal Runtime status, unknown
    run, or an already-terminal App Run that must not be regressed).
    """
    from evoflow.persistence import app_repositories

    app_run_id = str(app_run_id or "").strip()
    if not app_run_id or not isinstance(result, dict):
        return None
    runtime_status = str(result.get("status") or "").strip().lower()
    if runtime_status not in _TERMINAL_RUNTIME_STATUSES:
        return None
    run = app_repositories.load_run(app_run_id)
    if run is None:
        return None
    current = str(run.get("status") or "").strip().lower()
    if current in _TERMINAL_APP_RUN_STATUSES:
        return current
    if runtime_status == "completed":
        app_repositories.update_run_status(
            app_run_id,
            "completed",
            progress=100,
            result_summary=_result_summary(result.get("result")),
        )
        return "completed"
    if runtime_status == "timed_out":
        app_repositories.update_run_status(
            app_run_id,
            "failed",
            error=_error_message(result.get("error"), "Runtime run timed out"),
        )
        return "failed"
    if runtime_status == "failed":
        app_repositories.update_run_status(
            app_run_id,
            "failed",
            error=_error_message(result.get("error"), "Runtime run failed"),
        )
        return "failed"
    app_repositories.update_run_status(
        app_run_id,
        "cancelled",
        error=_error_message(result.get("error"), "Runtime run cancelled"),
    )
    return "cancelled"


def mark_app_run_failed_terminal(app_run_id: str, message: str) -> None:
    """Existing terminal write path for a Runtime bridge failure.

    Never overwrites a terminal App Run; never leaves the run stuck.
    """
    from evoflow.persistence import app_repositories

    app_run_id = str(app_run_id or "").strip()
    if not app_run_id:
        return
    try:
        run = app_repositories.load_run(app_run_id)
        if run is None:
            return
        current = str(run.get("status") or "").strip().lower()
        if current in _TERMINAL_APP_RUN_STATUSES:
            return
        app_repositories.update_run_status(
            app_run_id, "failed", error=str(message or "").strip()[:_ERROR_MAX_CHARS]
        )
    except Exception:
        logger.exception(
            "app runtime bridge: failed-terminal write failed app_run_id=%s", app_run_id
        )


async def run_app_workflow_on_runtime(
    *,
    app_run_id: str,
    app_id: str,
    app_version: int | None,
    parameters: dict[str, str],
    task_id: str,
    org_id: str,
    service: RuntimeServiceProtocol | None = None,
) -> dict[str, Any]:
    """Run one App workflow execution through the Runtime public contract.

    ``create_run`` → ``start_run`` → (auto-grant the plan approval — workflow
    mode already auto-authorizes, ``run_app``: "clicking 运行 is consent") →
    ``get_result`` → project the terminal onto the existing App Run record.
    Raises on Runtime call failure; the detached wrapper turns that into the
    existing failed-terminal write path (never a legacy re-execution).
    """
    svc = service or _default_service()
    app_run_id = str(app_run_id or "").strip()
    if not app_run_id:
        raise ValueError("app_run_id is required for the Runtime bridge")

    input_payload: dict[str, Any] = {
        "app_id": str(app_id or "").strip(),
        "app_version": app_version,
        "parameters": dict(parameters or {}),
        "execution_mode": "workflow",
    }
    created = await svc.create_run(
        org_id=org_id,
        task_id=str(task_id or "").strip() or app_run_id,
        input_payload=input_payload,
        idempotency_key=app_run_idempotency_key(app_run_id),
    )
    runtime_run_id = str(created.get("run_id") or "").strip()
    if not runtime_run_id:
        raise ValueError("Runtime create_run did not return a run_id")

    await svc.start_run(runtime_run_id, org_id=org_id)
    status = await svc.get_run_status(runtime_run_id, org_id=org_id)
    if status.get("status") == "waiting_approval":
        approval = status.get("approval") or {}
        approval_id = str(approval.get("approval_id") or "").strip()
        if approval_id:
            await svc.grant_approval(approval_id, run_id=runtime_run_id, org_id=org_id)

    result = await svc.get_result(runtime_run_id, org_id=org_id)
    projected = project_runtime_result_to_app_run(app_run_id, result)
    return {
        "runtime_run_id": runtime_run_id,
        "runtime_status": result.get("status"),
        "app_run_status": projected,
    }


def trigger_workflow_app_runtime_run(
    *,
    run_id: str,
    app_id: str,
    app_version: int | None,
    parameters: dict[str, str],
    task_id: str,
    org_id: str,
    service: RuntimeServiceProtocol | None = None,
) -> bool:
    """Detached trigger used by ``app_runner.run_app`` (workflow mode).

    Returns ``True`` when the Runtime path owns this run (switch on and a real
    ``org_id`` supplied by the entry): the caller must then skip the legacy
    dispatch for this run — never fall back to it. Returns ``False`` when the
    bridge is not applicable and the legacy path must proceed unchanged.

    Runtime call failures are raised inside the detached coroutine and land on
    the existing failed-terminal write path; they never silently re-enter the
    legacy chain.
    """
    if not app_runtime_bridge_enabled() or not str(org_id or "").strip():
        return False
    run_id = str(run_id or "").strip()
    if not run_id:
        return False

    async def _go() -> None:
        try:
            outcome = await run_app_workflow_on_runtime(
                app_run_id=run_id,
                app_id=app_id,
                app_version=app_version,
                parameters=parameters,
                task_id=task_id,
                org_id=str(org_id).strip(),
                service=service,
            )
            logger.info(
                "app runtime bridge: run finished app_run_id=%s runtime_run_id=%s "
                "runtime_status=%s app_run_status=%s",
                run_id,
                outcome.get("runtime_run_id"),
                outcome.get("runtime_status"),
                outcome.get("app_run_status"),
            )
        except Exception as exc:
            logger.exception(
                "app runtime bridge: runtime execution failed app_run_id=%s", run_id
            )
            mark_app_run_failed_terminal(run_id, f"Runtime bridge execution failed: {exc}")

    from evoflow.subagents.detached_poll_scheduler import schedule_detached_poll

    schedule_detached_poll(_go(), name=f"app-runtime-{run_id[:12]}")
    return True
