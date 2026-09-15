"""Real chat entry → Runtime takeover (AG-G2-CHAT-REAL-ENTRY-RUNTIME-001).

Exercises the real HTTP entry — ``POST /api/langgraph/threads/{tid}/runs/stream``
on ``langgraph_proxy.router`` — against a stub mounted LangGraph app and a fake
``RuntimeService``, covering:

* flag-off: the legacy chain answers, the Runtime is untouched;
* flag-on: the Runtime owns the send (legacy app never called — no double
  execution), AG-UI frames stream to the page, the assistant message is
  persisted through the existing transcript write path, and the session ends in
  an explicit terminal state;
* retried sends with the same ``message_id`` reuse the Runtime run and do not
  duplicate the assistant message;
* failed / cancelled terminals are visible on the wire and persisted;
* ``/runs/cancel`` hands off to the Runtime for runtime-owned runs only.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.routing import Mount, Route

from app.gateway import chat_runtime_entry as entry_mod
from app.gateway.chat_runtime_dispatch import CHAT_RUNTIME_OPT_IN_ENV
from app.gateway.routers.langgraph_proxy import router

SESSION_KEY = "agent:main:g2-entry"
THREAD_ID = "thread-g2-entry-1"
RESULT_TEXT = "Runtime 回执：AgentScope 执行完成"
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "timed_out"}


# --------------------------------------------------------------------------
# Fakes & stubs
# --------------------------------------------------------------------------


class SpyLangGraph:
    """Stands in for the mounted LangGraph ASGI app; records every hit."""

    def __init__(self) -> None:
        self.scopes: list[dict[str, Any]] = []
        self.bodies: list[bytes] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.scopes.append(scope)
        if scope.get("type") != "http":
            return
        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body += message.get("body") or b""
                if not message.get("more_body", False):
                    break
            else:
                break
        self.bodies.append(body)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"legacy-langgraph", "more_body": False})


class FakeRuntimeService:
    """Scripted RuntimeService: same idempotency/terminal contract, no PostgreSQL."""

    def __init__(
        self,
        *,
        outcome: str = "completed",
        result_text: str | None = RESULT_TEXT,
        error: dict[str, Any] | None = None,
        fail_create: bool = False,
    ) -> None:
        assert outcome in TERMINAL_STATUSES
        self.outcome = outcome
        self.result_text = result_text
        self.error = error
        self.fail_create = fail_create
        self.create_calls: list[dict[str, Any]] = []
        self.start_calls: list[str] = []
        self.grant_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.executed = 0
        self.planned = 0
        self._by_idem: dict[str, dict[str, Any]] = {}
        self._statuses: dict[str, str] = {}

    async def create_run(
        self, *, org_id: str = "local", task_id: str, input_payload: dict, idempotency_key=None
    ) -> dict[str, Any]:
        self.create_calls.append(
            {
                "org_id": org_id,
                "task_id": task_id,
                "input_payload": input_payload,
                "idempotency_key": idempotency_key,
            }
        )
        if self.fail_create:
            raise RuntimeError("postgres unavailable")
        if idempotency_key in self._by_idem:
            return self._by_idem[idempotency_key]
        run_id = f"run_{len(self._statuses) + 1}"
        self._statuses[run_id] = "created"
        run = {"run_id": run_id, "status": "queued"}
        self._by_idem[idempotency_key] = run
        return run

    def _status_payload(self, run_id: str) -> dict[str, Any]:
        status = self._statuses[run_id]
        approval = {"approval_id": f"apr_{run_id}"} if status == "waiting_approval" else None
        return {"run_id": run_id, "status": status, "approval": approval}

    async def start_run(self, run_id: str, org_id: str | None = None) -> dict[str, Any]:
        self.start_calls.append(run_id)
        status = self._statuses[run_id]
        if status in TERMINAL_STATUSES or status == "waiting_approval":
            return self._status_payload(run_id)
        self._statuses[run_id] = "waiting_approval"
        self.planned += 1
        return self._status_payload(run_id)

    async def grant_approval(
        self, approval_id: str, *, decided_by=None, reason=None, run_id=None, org_id=None
    ) -> dict[str, Any]:
        self.grant_calls.append(approval_id)
        self.executed += 1
        self._statuses[run_id] = self.outcome
        return self._status_payload(run_id)

    async def stream_events(self, run_id: str, *, after_sequence: int = 0, org_id=None):
        sequence = after_sequence
        for event_type in ("run.created", "run.planning", "run.waiting_approval"):
            sequence += 1
            yield {"type": event_type, "sequence": sequence, "payload": {}}
        while self._statuses.get(run_id) not in TERMINAL_STATUSES:
            await asyncio.sleep(0.02)
        sequence += 1
        yield {"type": f"run.{self._statuses[run_id]}", "sequence": sequence, "payload": {}}

    async def get_result(self, run_id: str, org_id=None) -> dict[str, Any]:
        status = self._statuses.get(run_id, "created")
        result = {"text": self.result_text} if status == "completed" and self.result_text else None
        error = None
        if status in {"failed", "timed_out"}:
            error = self.error or {"code": "agentscope_execution", "message": "run failed"}
        elif status == "cancelled":
            error = {"message": "run cancelled"}
        return {"run_id": run_id, "status": status, "result": result, "assets": [], "error": error}

    async def request_cancel(self, run_id: str, org_id=None) -> dict[str, Any]:
        self.cancel_calls.append(run_id)
        if self._statuses.get(run_id) not in TERMINAL_STATUSES:
            self._statuses[run_id] = "cancelled"
        return self._status_payload(run_id)


class Rec:
    """Call recorder standing in for an evoflow persistence writer."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append({"args": args, "kwargs": kwargs})
        if self.name == "append":
            return {"message_id": kwargs.get("message_id")}
        return True


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def stream_body(
    *,
    message_id: str | None = "msg-1",
    assistant_id: str = "lead_agent",
    text: str = "帮我查一下今天的数据",
) -> bytes:
    payload: dict[str, Any] = {
        "assistant_id": assistant_id,
        "input": {"messages": []},
    }
    if message_id:
        payload["input"]["messages"].append({"role": "user", "content": text, "id": message_id})
    return json.dumps(payload).encode()


def make_app(service: FakeRuntimeService, spy: SpyLangGraph) -> FastAPI:
    app = FastAPI()
    app.include_router(router, include_in_schema=False)
    # Same order as router_registry: specific routes first, mount last.
    app.mount("/api/langgraph", spy)
    app.state._lg_app = spy
    app.state.qagent_runtime_service = service
    return app


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch):
    """Hermetic wiring: patch flag + chat persistence writers, collect calls."""
    state: dict[str, Any] = {
        "opt_in": False,
        "appended": Rec("append"),
        "run_started": Rec("run_started"),
        "run_ended": Rec("run_ended"),
        "live_snapshot": Rec("live_snapshot"),
    }

    from app.gateway import chat_runtime_dispatch as dispatch_mod

    monkeypatch.setattr(
        entry_mod, "chat_runtime_opt_in_enabled", lambda: state["opt_in"], raising=True
    )
    # dispatch_chat_run checks the flag inside its own module — keep both honest.
    monkeypatch.setattr(
        dispatch_mod, "chat_runtime_opt_in_enabled", lambda: state["opt_in"], raising=True
    )
    import evoflow.authz.context as authz_context
    import evoflow.authz.http_guard as http_guard
    import evoflow.persistence.chat_session_service as chat_svc
    import evoflow.persistence.live_run_repositories as live_repo
    import evoflow.persistence.session_repositories as sess_repo
    import evoflow.persistence.session_run_state as run_state

    monkeypatch.setattr(http_guard, "require_thread_visible", lambda request, tid: None)
    monkeypatch.setattr(sess_repo, "find_session_key_by_thread_id", lambda tid: SESSION_KEY)
    monkeypatch.setattr(
        authz_context, "resolve_request_principal", lambda request: {"principal_id": "user:local"}
    )
    monkeypatch.setattr(run_state, "mark_session_run_started", state["run_started"])
    monkeypatch.setattr(run_state, "mark_session_run_ended", state["run_ended"])
    monkeypatch.setattr(chat_svc, "append_message_and_touch_session", state["appended"])
    monkeypatch.setattr(live_repo, "upsert_live_run_snapshot", state["live_snapshot"])
    return state


async def post_stream(app: FastAPI, body: bytes, **params: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        return await client.post(
            f"/api/langgraph/threads/{THREAD_ID}/runs/stream", content=body, params=params
        )


def sse_events(body: str) -> list[dict[str, Any]]:
    """Parse ``event: ag-ui`` frames out of an SSE response body."""
    out: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        event_name = None
        data_raw = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: "):
                data_raw = line[len("data: "):]
        if event_name == "ag-ui" and data_raw:
            try:
                out.append(json.loads(data_raw))
            except json.JSONDecodeError:
                pass
    return out


def lg_events(body: str) -> list[tuple[str, Any]]:
    """Parse the raw upstream-shaped SSE frames the entry emits."""
    out: list[tuple[str, Any]] = []
    for block in body.split("\n\n"):
        event_name = None
        data_raw = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: "):
                data_raw = line[len("data: "):]
        if event_name and data_raw is not None:
            try:
                out.append((event_name, json.loads(data_raw)))
            except json.JSONDecodeError:
                out.append((event_name, data_raw))
    return out


def wire_agui_events(raw: str, body: bytes) -> list[dict[str, Any]]:
    """Run the entry's raw frames through the real ``PostStreamUiTransform``.

    This is exactly what the route-logger middleware does for the chat page
    (``ui_sse=1``): bootstrap frames at response start, upstream chunks fed
    through the transform, normalizer ``finish()`` at stream close. The result
    is the AG-UI wire the frontend actually consumes.
    """
    from app.gateway.streaming.post_stream_ui_normalize import PostStreamUiTransform

    transform = PostStreamUiTransform(
        thread_id=THREAD_ID, body=body, stream_format="agui", run_id=None, mirror_enabled=False
    )
    wire = b"".join(transform.normalizer.bootstrap_wire_bytes())
    wire += b"".join(transform._feed_upstream_chunk(raw.encode("utf-8")))
    wire += b"".join(transform._finish_normalizer())
    import os as _os

    if _os.getenv("G2_WIRE_DEBUG"):
        import sys as _sys

        print(f"[WIRE] {wire.decode('utf-8', 'replace')}", file=_sys.stderr, flush=True)
    return sse_events(wire.decode("utf-8"))


# --------------------------------------------------------------------------
# Route precedence
# --------------------------------------------------------------------------


def test_entry_routes_are_matched_before_the_langgraph_mount() -> None:
    app = make_app(FakeRuntimeService(), SpyLangGraph())
    paths = [getattr(r, "path", None) for r in app.routes]
    entry_index = paths.index("/api/langgraph/threads/{thread_id}/runs/stream")
    mount_index = next(
        i for i, r in enumerate(app.routes) if isinstance(r, Mount) and r.path == "/api/langgraph"
    )
    assert entry_index < mount_index
    route = app.routes[entry_index]
    assert isinstance(route, Route)
    assert route.methods == {"POST"}


# --------------------------------------------------------------------------
# flag off: legacy chain untouched
# --------------------------------------------------------------------------


async def test_flag_off_passes_through_to_legacy_langgraph(env, monkeypatch) -> None:
    monkeypatch.delenv(CHAT_RUNTIME_OPT_IN_ENV, raising=False)
    spy = SpyLangGraph()
    service = FakeRuntimeService()
    app = make_app(service, spy)

    response = await post_stream(app, stream_body())

    assert response.status_code == 200
    assert response.text == "legacy-langgraph"
    assert len(spy.scopes) == 1
    # Mount-equivalent scope: full original path, root_path gains the mount prefix.
    scope = spy.scopes[0]
    assert scope["path"] == f"/api/langgraph/threads/{THREAD_ID}/runs/stream"
    assert scope["root_path"].endswith("/api/langgraph")
    assert "endpoint" not in scope
    # The Runtime is not touched at all.
    assert service.create_calls == []


async def test_flag_on_with_unavailable_runtime_falls_back_to_legacy_chain(env) -> None:
    env["opt_in"] = True
    spy = SpyLangGraph()
    service = FakeRuntimeService(fail_create=True)
    app = make_app(service, spy)

    response = await post_stream(app, stream_body())

    # Dispatch failed before anything started in the Runtime → legacy chain
    # proceeds (still exactly one execution chain for the message).
    assert response.text == "legacy-langgraph"
    assert len(service.create_calls) == 1
    assert service.start_calls == []
    assert len(spy.scopes) == 1


# --------------------------------------------------------------------------
# flag on: Runtime owns the real entry
# --------------------------------------------------------------------------


async def test_flag_on_runs_runtime_and_never_the_legacy_chain(env) -> None:
    env["opt_in"] = True
    spy = SpyLangGraph()
    service = FakeRuntimeService()
    app = make_app(service, spy)

    response = await post_stream(app, stream_body(), ui_sse="1", stream_format="agui")

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    # The legacy LangGraph app must not be invoked — no double execution.
    assert spy.scopes == []

    # Raw frames follow the transform's input contract (LangGraph event names).
    raw = lg_events(response.text)
    raw_names = [name for name, _ in raw]
    assert raw_names[0] == "metadata"
    assert "values" in raw_names
    assert "messages" in raw_names
    assert raw_names[-1] == "end"
    # The metadata frame carries the Runtime run id (idempotency anchor).
    run_id = next(d for n, d in raw if n == "metadata")["run_id"]
    assert run_id in {"run_1", "run_2"}

    # Through the real page transform, the frontend consumes standard AG-UI.
    # (The bootstrap RUN_STARTED precedes the metadata frame — same generated
    # runId behavior as the legacy chain; the terminal frames adopt ours.)
    events = wire_agui_events(response.text, stream_body())
    types = [e["type"] for e in events]
    assert types[0] == "RUN_STARTED"
    assert "TEXT_MESSAGE_CONTENT" in types
    assert "RUN_FINISHED" in types
    assert types.index("RUN_FINISHED") > types.index("TEXT_MESSAGE_CONTENT")
    assert not any(e["type"] == "RUN_ERROR" for e in events)

    text_frame = next(e for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")
    assert text_frame["delta"] == RESULT_TEXT

    # Runtime drive: start → auto-grant → single AgentScope execution.
    assert service.planned == 1
    assert service.executed == 1

    # Assistant transcript row persisted through the existing write path.
    appends = env["appended"].calls
    assert len(appends) == 1
    kwargs = appends[0]["kwargs"]
    assert appends[0]["args"][0] == SESSION_KEY
    assert kwargs["role"] == "assistant"
    assert kwargs["content"] == RESULT_TEXT
    assert kwargs["message_id"] == f"qagent-runtime-{run_id}"
    assert kwargs["run_id"] == run_id

    # Session terminal state + live-run snapshot projection. The transform's
    # stream-end housekeeping also writes a terminal (legacy "done") after ours,
    # so assert on the entry's own write (the one without reason/source kwargs).
    assert env["run_started"].calls[0]["kwargs"]["run_id"] == run_id
    ended = next(
        c["kwargs"] for c in env["run_ended"].calls if "reason" not in c["kwargs"]
    )
    assert ended["terminal_status"] == "success"
    snapshot = env["live_snapshot"].calls[-1]["kwargs"]
    assert snapshot["status"] == "completed_success"
    assert snapshot["partial_text"] == RESULT_TEXT
    # In-flight association cleaned up.
    assert entry_mod.inflight_runtime_run_ids() == set()


async def test_flag_on_retry_same_message_id_reuses_run_and_message(env) -> None:
    env["opt_in"] = True
    spy = SpyLangGraph()
    service = FakeRuntimeService()
    app = make_app(service, spy)
    body = stream_body(message_id="msg-dup-1")

    first = await post_stream(app, body, stream_format="agui")
    second = await post_stream(app, body, stream_format="agui")

    assert first.status_code == second.status_code == 200
    assert spy.scopes == []

    # Same idempotency key → the Runtime reuses one run, planned and executed once.
    assert len(service.create_calls) == 2
    assert service.create_calls[0]["idempotency_key"] == service.create_calls[1]["idempotency_key"]
    assert service.create_calls[0]["idempotency_key"] == f"chat:{SESSION_KEY}:msg-dup-1"
    assert service.planned == 1
    assert service.executed == 1

    # The assistant message is appended twice but with the same deterministic
    # message_id — the DB-level dedup turns the retry into a no-op returning
    # the existing row, never a duplicate.
    message_ids = {c["kwargs"]["message_id"] for c in env["appended"].calls}
    assert message_ids == {"qagent-runtime-run_1"}
    assert len(env["appended"].calls) == 2
    assert RESULT_TEXT in first.text
    assert RESULT_TEXT in second.text


async def test_flag_on_failed_terminal_is_explicit(env) -> None:
    env["opt_in"] = True
    service = FakeRuntimeService(
        outcome="failed", error={"code": "agentscope_authentication", "message": "bad credentials"}
    )
    app = make_app(service, SpyLangGraph())

    response = await post_stream(app, stream_body(), stream_format="agui")

    # Raw contract: a single upstream ``error`` event, no ``end`` terminal.
    raw = lg_events(response.text)
    error_frames = [d for n, d in raw if n == "error"]
    assert len(error_frames) == 1
    assert error_frames[0] == "bad credentials"
    assert not any(n == "end" for n, _ in raw)

    # Through the page transform the frontend gets RUN_ERROR with the message.
    # (The transform's finish() adds a RUN_FINISHED after an error stream — its
    # behavior for any upstream error; the UI error state is already set.)
    events = wire_agui_events(response.text, stream_body())
    wire_errors = [e for e in events if e["type"] == "RUN_ERROR"]
    assert len(wire_errors) == 1
    assert wire_errors[0]["message"] == "bad credentials"

    # No assistant message on failure; the session lands on an explicit fail.
    assert env["appended"].calls == []
    ended = next(c["kwargs"] for c in env["run_ended"].calls if "reason" not in c["kwargs"])
    assert ended["terminal_status"] == "fail"
    assert env["live_snapshot"].calls[-1]["kwargs"]["status"] == "completed_error"


async def test_flag_on_cancelled_terminal_is_explicit(env) -> None:
    env["opt_in"] = True
    service = FakeRuntimeService(outcome="cancelled")
    app = make_app(service, SpyLangGraph())

    response = await post_stream(app, stream_body(), stream_format="agui")

    # Raw contract: cancelled converges on the single ``end`` terminal (no error channel).
    raw = lg_events(response.text)
    assert raw[-1][0] == "end"
    assert not any(n == "error" for n, _ in raw)

    # Through the page transform: RUN_FINISHED, no RUN_ERROR.
    events = wire_agui_events(response.text, stream_body())
    assert any(e["type"] == "RUN_FINISHED" for e in events)
    assert not any(e["type"] == "RUN_ERROR" for e in events)
    assert env["appended"].calls == []
    ended = next(c["kwargs"] for c in env["run_ended"].calls if "reason" not in c["kwargs"])
    assert ended["terminal_status"] == "cancelled"


# --------------------------------------------------------------------------
# flag on: non-chat sends keep the legacy chain
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs,query",
    [
        ({"assistant_id": "claude_code_chat"}, {"stream_format": "agui"}),
        ({"assistant_id": "lead_agent"}, {"stream_format": "openai"}),
        ({"message_id": None}, {"stream_format": "agui"}),
    ],
)
async def test_flag_on_non_chat_sends_pass_through(env, kwargs, query) -> None:
    env["opt_in"] = True
    spy = SpyLangGraph()
    service = FakeRuntimeService()
    app = make_app(service, spy)

    response = await post_stream(app, stream_body(**kwargs), **query)

    assert response.text == "legacy-langgraph"
    assert len(spy.scopes) == 1
    # Body consumed by the handler is replayed to the legacy app intact.
    assert spy.bodies[0] == stream_body(**kwargs)
    assert service.create_calls == []


async def test_flag_on_unmapped_thread_passes_through(env, monkeypatch) -> None:
    env["opt_in"] = True
    import evoflow.persistence.session_repositories as sess_repo

    monkeypatch.setattr(sess_repo, "find_session_key_by_thread_id", lambda tid: None)
    spy = SpyLangGraph()
    service = FakeRuntimeService()
    app = make_app(service, spy)

    response = await post_stream(app, stream_body(), stream_format="agui")

    assert response.text == "legacy-langgraph"
    assert service.create_calls == []


# --------------------------------------------------------------------------
# Cancel hand-off
# --------------------------------------------------------------------------


async def test_cancel_propagates_to_runtime_owned_run(env) -> None:
    env["opt_in"] = True
    service = FakeRuntimeService()
    app = make_app(service, SpyLangGraph())
    entry_mod._register_inflight(
        "run_live_1", session_key=SESSION_KEY, thread_id=THREAD_ID, org_id="local"
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        response = await client.post(
            "/api/langgraph/runs/cancel", content=json.dumps({"run_id": "run_live_1"}).encode()
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["cancelled"] == [{"run_id": "run_live_1", "status": "cancelled"}]
    assert service.cancel_calls == ["run_live_1"]
    entry_mod._unregister_inflight("run_live_1")


async def test_cancel_unknown_run_passes_through_to_legacy(env) -> None:
    env["opt_in"] = True
    spy = SpyLangGraph()
    service = FakeRuntimeService()
    app = make_app(service, spy)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        response = await client.post(
            "/api/langgraph/runs/cancel", content=json.dumps({"run_id": "run_not_ours"}).encode()
        )

    assert response.text == "legacy-langgraph"
    assert service.cancel_calls == []


async def test_cancel_flag_off_passes_through(env, monkeypatch) -> None:
    monkeypatch.delenv(CHAT_RUNTIME_OPT_IN_ENV, raising=False)
    spy = SpyLangGraph()
    app = make_app(FakeRuntimeService(), spy)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        response = await client.post(
            "/api/langgraph/runs/cancel", content=json.dumps({"run_id": "run_x"}).encode()
        )

    assert response.text == "legacy-langgraph"
