"""Real chat entry → QAgent Runtime takeover (``AG-G2-CHAT-REAL-ENTRY-RUNTIME-001``).

This module owns the **flag-gated takeover** of the real chat page send:

* The chat page sends ``POST /api/langgraph/threads/{thread_id}/runs/stream``
  (see ``docs/plan/agentscope-2-g2-chat-entry-map.md`` §2/§3). The route below
  is registered on the already-mounted ``langgraph_proxy.router`` — which the
  Gateway includes *before* ``LazyLangGraphMount`` — so it intercepts the real
  entry without touching ``router_registry.py`` or the LangGraph mount.
* **flag off (default)**: the request is delegated to the mounted LangGraph app
  at ASGI level with a Mount-equivalent scope. The old chain is unchanged.
* **flag on** (``QAGENT_CHAT_RUNTIME_OPT_IN=1``): eligible chat sends run
  through :class:`app.qagent_runtime.service.RuntimeService` exactly once
  (``dispatch_chat_run`` → ``start_run`` → auto-grant → AgentScope execution),
  the same single-execution pattern as the Workflow bridge
  (``app_runtime_bridge.run_app_workflow_on_runtime``). The legacy LangGraph app
  is never invoked for a dispatched run — no double execution.
* **Projection only, no second state source**: Runtime is the execution source
  of truth. Stream frames are AG-UI wire frames the existing frontend already
  consumes (``TEXT_MESSAGE_CONTENT`` / ``RUN_STARTED`` / ``RUN_FINISHED`` /
  ``RUN_ERROR`` — see ``ws-client.js dispatchAgUiWireFrame``), and the final
  result is persisted through the *existing* chat transcript write path
  (``chat_svc.append_message_and_touch_session``, DB-level ``message_id``
  dedup — a retried send returns the already-persisted row) plus
  ``evoflow_chat_sessions.run_status`` terminal states.

Bounds: this module does not modify ``backend/app/qagent_runtime/**``,
migrations, the router registry, background startup, the LangGraph graph
implementation, shared SSE encoders, or the frontend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

from fastapi import Request
from fastapi.responses import StreamingResponse

from app.gateway.chat_runtime_dispatch import (
    CHAT_RUNTIME_OPT_IN_ENV,
    chat_runtime_opt_in_enabled,
    dispatch_chat_run,
)

logger = logging.getLogger(__name__)

#: Only the main chat graph is taken over. Other producers of
#: ``/runs/stream`` (claude-code chat, collab, tooling) keep the legacy chain.
_TAKEOVER_ASSISTANT_IDS = frozenset({"lead_agent"})

#: AG-UI event names emitted on the chat wire (asserted via the transform).
_RUN_STARTED = "RUN_STARTED"
_RUN_FINISHED = "RUN_FINISHED"
_RUN_ERROR = "RUN_ERROR"
_TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

_POLL_INTERVAL_S = 0.5
_SSE_PING_INTERVAL_S = 10.0

# --------------------------------------------------------------------------
# In-flight association (runtime run id → chat scope) for cancel hand-off.
# Process-local, mirroring app_runtime_bridge; never a second status source.
# --------------------------------------------------------------------------

_inflight_lock = threading.Lock()
_inflight_runtime_runs: dict[str, dict[str, str]] = {}


def _register_inflight(runtime_run_id: str, *, session_key: str, thread_id: str, org_id: str) -> None:
    with _inflight_lock:
        _inflight_runtime_runs[str(runtime_run_id)] = {
            "session_key": session_key,
            "thread_id": thread_id,
            "org_id": org_id,
        }


def _unregister_inflight(runtime_run_id: str) -> None:
    with _inflight_lock:
        _inflight_runtime_runs.pop(str(runtime_run_id), None)


def inflight_runtime_run_ids() -> set[str]:
    with _inflight_lock:
        return set(_inflight_runtime_runs)


# --------------------------------------------------------------------------
# Body parsing
# --------------------------------------------------------------------------


def parse_runs_stream_body(body: bytes) -> dict[str, Any]:
    """Extract the minimal chat identity from a ``/runs/stream`` request body.

    Returns ``{}`` when the body is not a recognizable chat send. Never raises.
    """
    try:
        raw = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    assistant_id = str(raw.get("assistant_id") or "").strip()
    inp = raw.get("input")
    messages = inp.get("messages") if isinstance(inp, dict) else None
    last_user: dict[str, Any] = {}
    if isinstance(messages, list):
        for m in reversed(messages):
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or m.get("type") or "").strip().lower()
            if role in {"user", "human"}:
                last_user = m
                break
    message_id = str(last_user.get("id") or last_user.get("message_id") or "").strip()
    return {
        "assistant_id": assistant_id,
        "message_id": message_id or None,
        "user_message": last_user or None,
    }


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and str(block.get("type") or "").lower() == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p).strip()
    return ""


# --------------------------------------------------------------------------
# LangGraph-shaped upstream SSE frames
#
# The real chat page runs behind ``PostStreamUiTransform`` (``ui_sse=1``): it
# parses this response's SSE frames and re-encodes them into the AG-UI wire the
# frontend already consumes (bootstrap RUN_STARTED / TEXT_MESSAGE_CONTENT /
# RUN_FINISHED / RUN_ERROR — see ``ws-client.js dispatchAgUiWireFrame``).
# Emitting raw AG-UI frames here would be swallowed by that transform, so the
# frames below are the transform's *input* contract (LangGraph event names).
# --------------------------------------------------------------------------


def lg_sse(event: str, data: Any) -> str:
    """One upstream-shaped SSE frame (``event`` + JSON ``data``).

    Always JSON-encoded — the transform's ``_parse_sse_frame`` runs
    ``json.loads`` on the data line and silently drops frames that fail.
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def anchor_values_frame(user_text: str, message_id: str | None) -> str:
    """A ``values`` snapshot carrying the user message so the transform anchors this turn.

    The transform anchors on the last human message matching the request body's
    user text (``UiStreamNormalizer._try_anchor``); without it, later
    ``messages`` chunks are buffered and never reach the wire.
    """
    human: dict[str, Any] = {"type": "human", "content": user_text}
    if message_id:
        human["id"] = str(message_id)
    return lg_sse("values", {"values": {"messages": [human]}})


def message_chunk_frame(message_id: str, text: str) -> str:
    """An ``AIMessageChunk`` streaming row — the transform re-emits its text as live deltas."""
    return lg_sse("messages", {"type": "AIMessageChunk", "id": message_id, "content": text})


# --------------------------------------------------------------------------
# Legacy pass-through (flag off / not a chat send / Runtime unavailable)
# --------------------------------------------------------------------------


async def _pass_through_to_langgraph(
    request: Request, receive: Any, send: Any, body: bytes | None = None
) -> None:
    """Delegate to the mounted LangGraph app exactly as ``LazyLangGraphMount`` would.

    The scope is adjusted the way Starlette's ``Mount`` does (root_path gains
    the mount prefix, routing keys are cleared); the request body is replayed
    when it has already been consumed.
    """
    lg = getattr(request.app.state, "_lg_app", None)
    scope = dict(request.scope)
    scope["root_path"] = (str(scope.get("root_path") or "").rstrip("/")) + "/api/langgraph"
    for key in ("route", "endpoint", "path_params"):
        scope.pop(key, None)

    if body is None:
        used_receive = receive
    else:
        async def used_receive() -> dict[str, Any]:  # noqa: ANN401
            return {"type": "http.request", "body": body, "more_body": False}

    if lg is None:
        # Same response LazyLangGraphMount produces while the engine loads.
        payload = b'{"detail":"LangGraph engine loading"}'
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                    (b"retry-after", b"2"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload, "more_body": False})
        return
    await lg(scope, used_receive, send)


# --------------------------------------------------------------------------
# Runtime-driven execution + projection
# --------------------------------------------------------------------------


def _assistant_message_id(runtime_run_id: str) -> str:
    """Deterministic transcript id: same Runtime run ⇒ same message row."""
    return f"qagent-runtime-{runtime_run_id}"


def _session_terminal_status(chat_session_status: str | None) -> str:
    st = str(chat_session_status or "").strip().lower()
    return st or "done"


def _persist_terminal(
    *,
    session_key: str,
    thread_id: str,
    runtime_run_id: str,
    principal_id: str | None,
    result_text: str | None,
    chat_live_status: str,
    chat_session_status: str,
) -> None:
    """Project a Runtime terminal onto the existing chat persistence model.

    Uses only pre-existing write paths: transcript append (message_id-idempotent),
    session run terminal state and the lightweight live-run snapshot.
    """
    from evoflow.persistence.chat_session_service import append_message_and_touch_session
    from evoflow.persistence.live_run_repositories import upsert_live_run_snapshot
    from evoflow.persistence.session_run_state import mark_session_run_ended

    if result_text:
        append_message_and_touch_session(
            session_key,
            role="assistant",
            content=result_text,
            message_id=_assistant_message_id(runtime_run_id),
            run_id=runtime_run_id,
            thread_id=thread_id,
            principal_id=principal_id,
        )
    mark_session_run_ended(
        session_key=session_key,
        thread_id=thread_id,
        terminal_status=_session_terminal_status(chat_session_status),
    )
    upsert_live_run_snapshot(
        session_key,
        run_id=runtime_run_id,
        thread_id=thread_id,
        status=chat_live_status,
        partial_text=result_text or "",
    )


def _best_effort(fn_name: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.warning("chat runtime entry: %s failed", fn_name, exc_info=True)
        return None


async def _a_best_effort(fn_name: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Best-effort await of an async callable (never raises)."""
    try:
        return await fn(*args, **kwargs)
    except Exception:
        logger.warning("chat runtime entry: %s failed", fn_name, exc_info=True)
        return None


async def _persist_terminal_async(**kwargs: Any) -> None:
    """Run the sync persistence projection in a worker thread.

    Never on the event loop: the chat persistence layer serializes access
    through a process-wide lock, and taking it on the loop deadlocks against
    threadpool workers that wait back on the loop. One retry absorbs transient
    sqlite write contention (all three write paths are idempotent).
    """
    import time as _time

    def _with_retry() -> None:
        for attempt in range(2):
            try:
                _persist_terminal(**kwargs)
                return
            except Exception:
                if attempt == 0:
                    logger.warning(
                        "chat runtime entry: terminal persist attempt 1 failed — retrying",
                        exc_info=True,
                    )
                    _time.sleep(0.3)
                else:
                    logger.warning(
                        "chat runtime entry: terminal persist failed", exc_info=True
                    )

    await asyncio.to_thread(_with_retry)


async def _runtime_sse_response(
    request: Request,
    *,
    runtime_run_id: str,
    session_key: str,
    thread_id: str,
    org_id: str,
    principal_id: str | None,
    user_text: str,
    user_message_id: str | None,
) -> StreamingResponse:
    """Build the SSE response that drives one Runtime run to its terminal.

    Frames are LangGraph-shaped (the transform's input contract); the existing
    ``PostStreamUiTransform`` layer turns them into the AG-UI wire.
    """
    service = getattr(request.app.state, "qagent_runtime_service")

    async def _drive() -> None:
        """start_run → auto-grant (chat consent is the send itself)."""
        try:
            status = await service.start_run(runtime_run_id, org_id=org_id)
            if status.get("status") == "waiting_approval":
                approval = status.get("approval") or {}
                approval_id = str(approval.get("approval_id") or "").strip()
                if approval_id:
                    await service.grant_approval(
                        approval_id,
                        decided_by="chat-runtime-entry",
                        reason="chat send auto-grant (opt-in runtime)",
                        run_id=runtime_run_id,
                        org_id=org_id,
                    )
        except Exception:
            logger.exception(
                "chat runtime entry: drive failed run_id=%s — forcing terminal", runtime_run_id
            )
            await _a_best_effort("request_cancel", service.request_cancel, runtime_run_id, org_id=org_id)

    async def _gen():
        from app.gateway.chat_runtime_result import project_runtime_result

        session_terminal = False
        assistant_msg_id = _assistant_message_id(runtime_run_id)
        drive_task = asyncio.create_task(_drive())
        try:
            # Adopt this stream's run id (``PostStreamUiTransform`` reads
            # ``event: metadata`` and aligns the AG-UI wire's runId).
            yield lg_sse("metadata", {"run_id": runtime_run_id})

            # Hold the stream open while the Runtime executes; the route-logger
            # middleware runs its own heartbeat for the client.
            while True:
                try:
                    await asyncio.wait_for(asyncio.shield(drive_task), timeout=_SSE_PING_INTERVAL_S)
                    break
                except asyncio.TimeoutError:
                    continue
            await drive_task

            result = await service.get_result(runtime_run_id, org_id=org_id)
            projection = project_runtime_result(result)
            if projection is None:
                raise RuntimeError(f"Runtime returned no projectable result for {runtime_run_id}")

            await _persist_terminal_async(
                session_key=session_key,
                thread_id=thread_id,
                runtime_run_id=runtime_run_id,
                principal_id=principal_id,
                result_text=projection.result_text,
                chat_live_status=projection.chat_live_status,
                chat_session_status=projection.chat_session_status,
            )
            session_terminal = True

            if projection.runtime_status in {"failed", "timed_out"}:
                yield lg_sse("error", projection.error_message or "run failed")
            else:
                # completed and cancelled both converge on the single ``end`` terminal
                if projection.result_text:
                    # Anchor the turn, then stream the assistant text as one live chunk.
                    yield anchor_values_frame(user_text, user_message_id)
                    yield message_chunk_frame(assistant_msg_id, projection.result_text)
                yield lg_sse("end", {})
        except asyncio.CancelledError:
            # Client went away (stop/refresh): cancel the Runtime run so the
            # session cannot stay stuck on running, then surface the terminal.
            await _a_best_effort("request_cancel", service.request_cancel, runtime_run_id, org_id=org_id)
            try:
                await asyncio.wait_for(asyncio.shield(drive_task), timeout=5.0)
            except Exception:
                pass
            await _persist_terminal_async(
                session_key=session_key,
                thread_id=thread_id,
                runtime_run_id=runtime_run_id,
                principal_id=principal_id,
                result_text=None,
                chat_live_status="completed_cancelled",
                chat_session_status="cancelled",
            )
            session_terminal = True
            raise
        except Exception as exc:
            logger.exception("chat runtime entry: stream failed run_id=%s", runtime_run_id)
            await _persist_terminal_async(
                session_key=session_key,
                thread_id=thread_id,
                runtime_run_id=runtime_run_id,
                principal_id=principal_id,
                result_text=None,
                chat_live_status="completed_error",
                chat_session_status="fail",
            )
            session_terminal = True
            yield lg_sse("error", str(exc) or "chat runtime stream failed")
        finally:
            drive_task.cancel()
            _unregister_inflight(runtime_run_id)
            if not session_terminal:
                # Last resort: never leave the session on a running status.
                from evoflow.persistence.session_run_state import mark_session_run_ended

                await asyncio.to_thread(
                    _best_effort,
                    "mark_session_run_ended",
                    mark_session_run_ended,
                    session_key=session_key,
                    thread_id=thread_id,
                    terminal_status="fail",
                )

    return StreamingResponse(_gen(), headers=dict(_SSE_HEADERS), media_type="text/event-stream")


# --------------------------------------------------------------------------
# Route handlers
# --------------------------------------------------------------------------


def _should_take_over(request: Request, parsed: dict[str, Any]) -> tuple[bool, str]:
    """Decide whether this real chat send is taken over by the Runtime."""
    if not chat_runtime_opt_in_enabled():
        return False, "opt_in_disabled"
    if str(parsed.get("assistant_id") or "") not in _TAKEOVER_ASSISTANT_IDS:
        return False, "assistant_not_main_chat"
    if not parsed.get("message_id"):
        return False, "message_id_missing"
    stream_format = (request.query_params.get("stream_format") or "").strip().lower()
    if stream_format and stream_format != "agui":
        # The Runtime bridge emits AG-UI frames only; keep other wire formats legacy.
        return False, "wire_format_not_agui"
    return True, ""


async def _handle_run_stream(request: Request, thread_id: str, receive: Any, send: Any) -> StreamingResponse | None:
    """Take over (or pass through) the real chat send for one thread.

    Returns a ``StreamingResponse`` to send when the Runtime owns the run;
    ``None`` when the legacy LangGraph app has already been invoked directly.
    """
    from evoflow.authz.http_guard import require_thread_visible

    require_thread_visible(request, thread_id)

    if not chat_runtime_opt_in_enabled():
        await _pass_through_to_langgraph(request, receive, send)
        return None

    body = await request.body()
    parsed = parse_runs_stream_body(body)
    take_over, _reason = _should_take_over(request, parsed)
    if not take_over:
        await _pass_through_to_langgraph(request, receive, send, body)
        return None

    from evoflow.persistence.session_repositories import find_session_key_by_thread_id
    from evoflow.persistence.session_run_state import mark_session_run_started

    session_key = str(
        await asyncio.to_thread(find_session_key_by_thread_id, thread_id) or ""
    ).strip()
    if not session_key:
        # Not a chat-page session (studio/tooling run): keep the legacy chain.
        await _pass_through_to_langgraph(request, receive, send, body)
        return None

    try:
        from evoflow.authz.context import resolve_request_principal

        principal = (await asyncio.to_thread(resolve_request_principal, request)) or {}
    except Exception:
        principal = {}
    principal_id = str(principal.get("principal_id") or "").strip() or None

    user_text = _content_to_text((parsed.get("user_message") or {}).get("content"))
    dispatch = await dispatch_chat_run(
        request,
        identity=None,
        session_key=session_key,
        message_id=parsed.get("message_id"),
        principal_id=principal_id,
        input_payload={
            "thread_id": thread_id,
            "assistant_id": parsed.get("assistant_id"),
            "user_text": user_text,
        },
    )
    if not dispatch.dispatched:
        # Nothing was started in the Runtime — the legacy chain may proceed
        # (still exactly one execution chain for this message).
        await _pass_through_to_langgraph(request, receive, send, body)
        return None

    runtime_run_id = str(dispatch.run_id or "").strip()
    org_id = str(dispatch.org_id or "local")
    _register_inflight(
        runtime_run_id, session_key=session_key, thread_id=thread_id, org_id=org_id
    )
    await asyncio.to_thread(
        _best_effort,
        "mark_session_run_started",
        mark_session_run_started,
        session_key=session_key,
        thread_id=thread_id,
        run_id=runtime_run_id,
    )
    logger.info(
        "chat runtime entry: runtime owns chat send run_id=%s thread=%s session=%s org=%s",
        runtime_run_id,
        thread_id,
        session_key,
        org_id,
    )
    return await _runtime_sse_response(
        request,
        runtime_run_id=runtime_run_id,
        session_key=session_key,
        thread_id=thread_id,
        org_id=org_id,
        principal_id=principal_id,
        user_text=user_text,
        user_message_id=parsed.get("message_id"),
    )


async def _handle_cancel(request: Request, receive: Any, send: Any) -> Any:
    """Cancel hand-off for runtime-owned chat runs; everything else passes through.

    Returns a ``Response`` to send, or ``None`` when the legacy LangGraph app
    has already been invoked directly.
    """
    if not chat_runtime_opt_in_enabled():
        await _pass_through_to_langgraph(request, receive, send)
        return

    body = await request.body()
    try:
        raw = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        await _pass_through_to_langgraph(request, receive, send, body)
        return

    requested: list[str] = []
    rid = str(raw.get("run_id") or "").strip()
    if rid:
        requested.append(rid)
    for item in raw.get("run_ids") or []:
        rid = str(item or "").strip()
        if rid:
            requested.append(rid)
    if str(raw.get("status") or "").strip().lower() == "all":
        requested = sorted(inflight_runtime_run_ids())
    requested = [rid for rid in requested if rid in inflight_runtime_run_ids()]
    if not requested:
        await _pass_through_to_langgraph(request, receive, send, body)
        return

    service = getattr(request.app.state, "qagent_runtime_service", None)
    if service is None:
        await _pass_through_to_langgraph(request, receive, send, body)
        return

    outcomes: list[dict[str, Any]] = []
    for rid in requested:
        org_id = _inflight_runtime_runs.get(rid, {}).get("org_id", "local")
        try:
            status = await service.request_cancel(rid, org_id=org_id)
            outcomes.append({"run_id": rid, "status": status.get("status") or "cancelled"})
        except Exception:
            logger.exception("chat runtime entry: cancel failed run_id=%s", rid)
            outcomes.append({"run_id": rid, "status": "cancel_failed"})
    from fastapi.responses import JSONResponse

    return JSONResponse(content={"cancelled": outcomes})


class _RawAsgiEndpoint:
    """Callable-class wrapper so Starlette mounts the handler as raw ASGI.

    Starlette 0.50 wraps plain function endpoints with ``request_response``;
    only non-function callables are used as the ASGI app directly, which the
    legacy pass-through and streaming takeover both require.
    """

    def __init__(self, handler: Any) -> None:
        self._handler = handler

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self._handler(scope, receive, send)


async def run_stream_entry(scope: Any, receive: Any, send: Any) -> None:
    """Raw ASGI endpoint: real chat send → Runtime (opt-in) or legacy mount."""
    from starlette.requests import Request

    request = Request(scope, receive)
    thread_id = str((scope.get("path_params") or {}).get("thread_id") or "")
    response = await _handle_run_stream(request, thread_id, receive, send)
    if response is not None:
        await response(scope, receive, send)


async def cancel_entry(scope: Any, receive: Any, send: Any) -> None:
    """Raw ASGI endpoint: cancel → Runtime-owned runs (opt-in) or legacy mount."""
    from starlette.requests import Request

    request = Request(scope, receive)
    response = await _handle_cancel(request, receive, send)
    if response is not None:
        await response(scope, receive, send)


def register_chat_runtime_entry_routes(router: Any) -> None:
    """Register the takeover routes on the already-mounted ``langgraph_proxy.router``.

    Registered *before* ``LazyLangGraphMount`` in the Gateway route table, so
    these routes win Starlette's first-match order; the mount remains the
    fallback for every non-overridden path and method. Raw ASGI endpoints (not
    FastAPI APIRoutes) so the legacy pass-through can answer at the ASGI layer
    exactly as the mount would.
    """
    router.add_route(
        f"{router.prefix}/threads/{{thread_id}}/runs/stream",
        _RawAsgiEndpoint(run_stream_entry),
        methods=["POST"],
        include_in_schema=False,
        name="chat_runtime_entry_run_stream",
    )
    router.add_route(
        f"{router.prefix}/runs/cancel",
        _RawAsgiEndpoint(cancel_entry),
        methods=["POST"],
        include_in_schema=False,
        name="chat_runtime_entry_cancel",
    )
