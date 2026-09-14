"""Project QAgent Runtime results back into the Chat domain's visible model.

Fact-source rule (see ``docs/plan/agentscope-2-g2-chat-entry-map.md`` §4):

* The **Runtime is the execution source of truth.** Run status, result payload,
  error payload and asset metadata originate in ``qagent_runs`` /
  ``qagent_run_events`` and are read back through the Runtime service.
* The **Chat layer is a compatibility projection.** It maps those values onto
  the status vocabulary the existing chat UI already consumes
  (``evoflow_chat_live_runs`` / ``evoflow_chat_sessions.run_status``). It never
  stores a second, independently-mutable copy of execution truth and never
  invents outcomes the Runtime did not report.

Terminal outcomes never regress: once a Runtime run reaches a terminal status,
the projection is terminal and further non-terminal reports are ignored. This is
what keeps "reconnect / duplicate attach / late cancel" from moving a finished
message back to ``running``.

This module is pure and Chat-domain-local. It does not modify
``backend/app/qagent_runtime/**``, migrations, the Gateway trunk, or the frontend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Runtime terminal statuses (mirrors ``app.qagent_runtime.events.TERMINAL_STATUSES``).
RUNTIME_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "timed_out"})

#: Runtime status -> chat ``evoflow_chat_live_runs.status`` value.
#: The existing chat writers use ``completed_<outcome>`` for finished runs.
_RUNTIME_TO_CHAT_LIVE_STATUS: dict[str, str] = {
    "created": "queued",
    "queued": "queued",
    "planning": "running",
    "waiting_approval": "running",
    "running": "running",
    "executing": "running",
    "completed": "completed_success",
    "failed": "completed_error",
    "cancelled": "completed_cancelled",
    "timed_out": "completed_error",
}

#: Runtime status -> chat ``evoflow_chat_sessions.run_status`` value
#: (see ``evoflow.persistence.session_run_state``).
_RUNTIME_TO_CHAT_SESSION_STATUS: dict[str, str] = {
    "created": "pending",
    "queued": "pending",
    "planning": "running",
    "waiting_approval": "running",
    "running": "running",
    "executing": "running",
    "completed": "success",
    "failed": "fail",
    "cancelled": "cancelled",
    "timed_out": "fail",
}


def runtime_status_is_terminal(status: str | None) -> bool:
    return str(status or "").strip().lower() in RUNTIME_TERMINAL_STATUSES


def chat_live_status_for_runtime_status(status: str | None) -> str:
    """Map a Runtime run status onto the chat live-run status vocabulary."""
    st = str(status or "").strip().lower()
    mapped = _RUNTIME_TO_CHAT_LIVE_STATUS.get(st)
    if mapped is None:
        # Unknown/newer Runtime status: report progress rather than inventing a
        # terminal outcome the Runtime did not state.
        return "running"
    return mapped


def chat_session_status_for_runtime_status(status: str | None) -> str:
    """Map a Runtime run status onto ``evoflow_chat_sessions.run_status``."""
    st = str(status or "").strip().lower()
    mapped = _RUNTIME_TO_CHAT_SESSION_STATUS.get(st)
    if mapped is None:
        return "running"
    return mapped


def terminal_does_not_regress(previous: str | None, candidate: str | None) -> bool:
    """Whether ``candidate`` may replace ``previous`` without regressing a terminal state.

    Returns ``False`` when ``previous`` is already terminal and ``candidate`` is
    not — i.e. a late progress report must not reopen a finished run.
    """
    if not runtime_status_is_terminal(previous):
        return True
    return runtime_status_is_terminal(candidate)


@dataclass(frozen=True)
class ChatRunResultProjection:
    """Chat-visible view of one Runtime run result.

    ``runtime_status`` is the fact; the ``chat_*`` fields are compatibility
    projections onto the vocabulary the existing UI consumes. ``assets`` is
    pass-through metadata, never a second asset store.
    """

    run_id: str
    runtime_status: str
    is_terminal: bool
    chat_live_status: str
    chat_session_status: str
    result_text: str | None = None
    error_message: str | None = None
    error_code: str | None = None
    assets: list[dict[str, Any]] = field(default_factory=list)

    def as_live_run_snapshot_fields(self) -> dict[str, Any]:
        """Fields for the existing ``evoflow_chat_live_runs`` snapshot writer."""
        out: dict[str, Any] = {
            "runId": self.run_id,
            "status": self.chat_live_status,
        }
        if self.result_text:
            out["partialText"] = self.result_text
        return out

    def as_user_visible_result(self) -> dict[str, Any]:
        """User-visible result payload for the existing message/live-run model."""
        return {
            "runId": self.run_id,
            "status": self.chat_live_status,
            "terminal": self.is_terminal,
            "result": self.result_text,
            "error": self.error_message,
            "errorCode": self.error_code,
            "assets": list(self.assets),
        }


def _extract_result_text(result_payload: Any) -> str | None:
    """Pull the assistant-visible text out of a Runtime result payload."""
    if result_payload is None:
        return None
    if isinstance(result_payload, str):
        text = result_payload.strip()
        return text or None
    if isinstance(result_payload, dict):
        for key in ("text", "output", "content", "summary", "message"):
            value = result_payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        # Fall back to the last assistant-ish message if the payload is a list.
        messages = result_payload.get("messages")
        if isinstance(messages, list):
            for item in reversed(messages):
                if isinstance(item, dict):
                    value = item.get("content") or item.get("text")
                    if isinstance(value, str) and value.strip():
                        return value.strip()
    return None


def _extract_error(error_payload: Any) -> tuple[str | None, str | None]:
    """Return ``(message, code)`` from a Runtime error payload."""
    if error_payload is None:
        return None, None
    if isinstance(error_payload, str):
        text = error_payload.strip()
        return (text or None), None
    if isinstance(error_payload, dict):
        message = error_payload.get("message") or error_payload.get("error") or error_payload.get("detail")
        code = error_payload.get("code") or error_payload.get("type") or error_payload.get("kind")
        return (
            str(message).strip() if message else None,
            str(code).strip() if code else None,
        )
    return str(error_payload).strip() or None, None


def project_runtime_result(
    runtime_result: dict[str, Any] | None,
    *,
    previous_status: str | None = None,
) -> ChatRunResultProjection | None:
    """Project a ``RuntimeService.get_result`` response into the Chat model.

    Args:
        runtime_result: Value returned by ``RuntimeService.get_result`` —
            ``{run_id, status, result, assets, error}``. ``None`` projects to
            ``None`` so a missing run does not fabricate a chat outcome.
        previous_status: Last observed Runtime status. When it was terminal and
            the incoming status is not, the terminal projection is preserved.

    The returned projection always reflects the *Runtime's* status — except when
    honouring the no-regression rule, in which case the previously observed
    terminal status is kept and clearly reported as such.
    """
    if not isinstance(runtime_result, dict):
        return None

    incoming = str(runtime_result.get("status") or "").strip().lower()
    if not incoming:
        return None

    effective = incoming
    if previous_status is not None and not terminal_does_not_regress(previous_status, incoming):
        effective = str(previous_status).strip().lower()

    is_terminal = runtime_status_is_terminal(effective)
    result_text = _extract_result_text(runtime_result.get("result"))

    error_message: str | None = None
    error_code: str | None = None
    if effective in {"failed", "timed_out", "cancelled"}:
        error_message, error_code = _extract_error(runtime_result.get("error"))
        if effective == "cancelled" and not error_message:
            error_message = "run cancelled"
        if effective == "timed_out" and not error_message:
            error_message = "run timed out"
        if effective == "failed" and not error_message:
            error_message = "run failed"

    assets_raw = runtime_result.get("assets")
    assets = [a for a in assets_raw if isinstance(a, dict)] if isinstance(assets_raw, list) else []

    return ChatRunResultProjection(
        run_id=str(runtime_result.get("run_id") or ""),
        runtime_status=effective,
        is_terminal=is_terminal,
        chat_live_status=chat_live_status_for_runtime_status(effective),
        chat_session_status=chat_session_status_for_runtime_status(effective),
        result_text=result_text,
        error_message=error_message,
        error_code=error_code,
        assets=assets,
    )
