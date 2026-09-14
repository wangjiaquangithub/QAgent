"""Bridge QAgent Runtime Event v1 frames onto the chat SSE wire format.

The chat UI consumes Server-Sent Events shaped as::

    event: <name>
    data: <json>

    ...

with a terminal ``runCompleted`` frame followed by ``done``. The QAgent Runtime
emits a different contract (``run.created`` / ``run.running`` / ``run.completed``
… with a monotonic ``sequence``). This module is the **local Chat-domain bridge**
between the two — it does not touch the global SSE registry, the Runtime event
model, or shared stream encoding helpers.

Guarantees:

* **Ordered.** Frames are emitted in ascending Runtime ``sequence`` order; an
  out-of-order or duplicated input frame is dropped rather than reordered.
* **Terminal converges once.** The first terminal event ends the stream. Any
  further frames (including another terminal, or late progress) are ignored, so
  a reconnect or duplicate attach cannot produce a second visible terminal.
  Note the convergence is *per stream*: a reconnect resuming before the terminal
  re-delivers it on purpose (that client never saw it), while a reconnect at or
  after it delivers nothing but ``done``.
* **Cursor is the Runtime sequence.** The SSE ``id:`` is the event sequence, so
  a client reconnecting with ``after_seq`` resumes exactly where it stopped.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

#: Chat SSE event names the existing frontend already handles.
RUN_COMPLETED_EVENT = "runCompleted"
CHAT_DELTA_EVENT = "chatDelta"
RUN_STATUS_EVENT = "runStatus"
DONE_EVENT = "done"
ERROR_EVENT = "error"

#: Runtime event type -> chat SSE event name.
_RUNTIME_EVENT_TO_SSE: dict[str, str] = {
    "run.created": RUN_STATUS_EVENT,
    "run.queued": RUN_STATUS_EVENT,
    "run.planning": RUN_STATUS_EVENT,
    "run.waiting_approval": RUN_STATUS_EVENT,
    "approval.granted": RUN_STATUS_EVENT,
    "approval.rejected": RUN_STATUS_EVENT,
    "run.running": RUN_STATUS_EVENT,
    "run.executing": RUN_STATUS_EVENT,
    "run.completed": RUN_COMPLETED_EVENT,
    "run.failed": ERROR_EVENT,
    "run.cancelled": RUN_COMPLETED_EVENT,
    "asset.available": RUN_STATUS_EVENT,
}

#: Runtime event types that end the stream.
_TERMINAL_EVENT_TYPES = frozenset({"run.completed", "run.failed", "run.cancelled"})

#: Runtime status carried by each terminal event type (for the runCompleted body).
_TERMINAL_STATUS_BY_EVENT = {
    "run.completed": "completed",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
}


def sse_frame(event: str, data: Any, *, event_id: str | None = None) -> str:
    """Render one chat SSE frame, matching the existing wire format."""
    body = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {body}")
    return "\n".join(lines) + "\n\n"


def _delta_text(payload: dict[str, Any]) -> str | None:
    """Extract incremental assistant text from a Runtime event payload."""
    for key in ("delta", "text", "chunk", "content"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    message = payload.get("message")
    if isinstance(message, dict):
        value = message.get("content") or message.get("text")
        if isinstance(value, str):
            return value
    return None


@dataclass
class ChatStreamBridgeState:
    """Cursor + convergence state for one bridged chat stream."""

    last_sequence: int = 0
    terminal_seen: bool = False
    last_status: str | None = None
    emitted: int = 0
    skipped: list[int] = field(default_factory=list)


class ChatRuntimeEventBridge:
    """Translate Runtime Event v1 frames into ordered chat SSE frames.

    Not thread-safe; use one bridge per stream. ``after_sequence`` seeds the
    cursor so a reconnecting client does not replay already-delivered frames.
    """

    def __init__(self, *, after_sequence: int = 0) -> None:
        if after_sequence < 0:
            raise ValueError("after_sequence must be >= 0")
        self.state = ChatStreamBridgeState(last_sequence=int(after_sequence))

    # -- helpers ---------------------------------------------------------

    @property
    def terminal_seen(self) -> bool:
        return self.state.terminal_seen

    @property
    def last_sequence(self) -> int:
        return self.state.last_sequence

    def _frame_for(self, event: dict[str, Any]) -> Iterator[str]:
        etype = str(event.get("type") or "").strip()
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        seq = event.get("sequence")
        event_id = str(event.get("event_id") or "") or None

        if etype in _TERMINAL_EVENT_TYPES:
            status = _TERMINAL_STATUS_BY_EVENT.get(etype, "completed")
            body: dict[str, Any] = {
                "runId": event.get("run_id"),
                "status": status,
                "sequence": seq,
            }
            error = payload.get("error") or payload.get("message")
            if error and status != "completed":
                body["error"] = error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)
            # Failed runs surface through the error channel the UI already handles;
            # completed/cancelled converge on the single runCompleted terminal frame.
            yield sse_frame(
                ERROR_EVENT if status == "failed" else RUN_COMPLETED_EVENT,
                body,
                event_id=event_id,
            )
            return

        mapped = _RUNTIME_EVENT_TO_SSE.get(etype)
        if mapped is None:
            # Unknown/newer Runtime event: do not guess a frame shape.
            return

        if mapped == CHAT_DELTA_EVENT:
            text = _delta_text(payload)
            if text is None:
                return
            yield sse_frame(CHAT_DELTA_EVENT, {"text": text, "sequence": seq}, event_id=event_id)
            return

        yield sse_frame(
            mapped,
            {"type": etype, "status": etype.split(".", 1)[-1], "sequence": seq},
            event_id=event_id,
        )

    # -- public API ------------------------------------------------------

    def frames_for(self, event: dict[str, Any]) -> list[str]:
        """Translate one Runtime event; returns ``[]`` when it must be dropped."""
        if not isinstance(event, dict):
            return []

        seq_raw = event.get("sequence")
        try:
            seq = int(seq_raw)
        except (TypeError, ValueError):
            return []

        # Ordered + no replay: strictly newer than the cursor only.
        if seq <= self.state.last_sequence:
            self.state.skipped.append(seq)
            return []

        # Terminal converges once: nothing may follow the first terminal frame.
        if self.state.terminal_seen:
            self.state.skipped.append(seq)
            return []

        self.state.last_sequence = seq
        frames = list(self._frame_for(event))
        if frames:
            self.state.emitted += len(frames)

        etype = str(event.get("type") or "").strip()
        self.state.last_status = etype.split(".", 1)[-1] if etype else self.state.last_status
        if etype in _TERMINAL_EVENT_TYPES:
            self.state.terminal_seen = True
        return frames

    def close(self) -> list[str]:
        """Emit the trailing ``done`` frame, exactly once."""
        if getattr(self, "_done_sent", False):
            return []
        self._done_sent = True
        return [sse_frame(DONE_EVENT, "[DONE]")]

    def stream(self, events: Iterable[dict[str, Any]]) -> Iterator[str]:
        """Bridge a sequence of Runtime events, ending with a single ``done``."""
        for event in events:
            yield from self.frames_for(event)
        yield from self.close()
