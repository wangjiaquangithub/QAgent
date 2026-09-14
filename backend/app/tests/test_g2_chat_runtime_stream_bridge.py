"""Chat SSE bridge for QAgent Runtime Event v1.

Protects the three properties the existing frontend depends on:

1. **Ordered** — frames follow Runtime ``sequence``; replays/out-of-order input
   are dropped rather than reordered.
2. **Terminal converges once** — the first terminal event ends the stream; a
   late duplicate terminal or trailing progress cannot emit a second one.
3. **Cursor reconnect** — the SSE ``id:`` carries the Runtime sequence, so
   reconnecting with ``after_sequence`` resumes without re-delivering frames.

Frame shape matches the existing chat wire format
(``event: <name>\\ndata: <json>\\n\\n``), which the UI already parses.
"""

from __future__ import annotations

import json

import pytest

from app.gateway.chat_runtime_stream_bridge import (
    DONE_EVENT,
    ERROR_EVENT,
    RUN_COMPLETED_EVENT,
    ChatRuntimeEventBridge,
    sse_frame,
)

RUN_ID = "run-g2-stream"


def _ev(seq: int, etype: str, payload: dict | None = None) -> dict:
    return {
        "event_id": f"ev-{seq}",
        "run_id": RUN_ID,
        "sequence": seq,
        "type": etype,
        "payload": payload or {},
        "occurred_at": "2026-09-14T00:00:00+00:00",
    }


def _parse(frame: str) -> tuple[str | None, str, dict | None]:
    """Parse one SSE frame into (id, event, data)."""
    event_id = None
    name = ""
    data_raw = ""
    for line in frame.strip().split("\n"):
        if line.startswith("id: "):
            event_id = line[4:]
        elif line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            data_raw = line[6:]
    try:
        body = json.loads(data_raw)
    except json.JSONDecodeError:
        body = None
    return event_id, name, body


# --------------------------------------------------------------------------
# Wire format compatibility
# --------------------------------------------------------------------------


def test_sse_frame_matches_existing_wire_format() -> None:
    frame = sse_frame("runStatus", {"a": 1}, event_id="ev-1")
    assert frame == 'id: ev-1\nevent: runStatus\ndata: {"a": 1}\n\n'


def test_sse_frame_without_id_has_no_id_line() -> None:
    frame = sse_frame("done", "[DONE]")
    assert frame.startswith("event: done\n")
    assert "id:" not in frame
    assert frame.endswith("\n\n")


def test_unhandled_event_type_is_dropped_not_guessed() -> None:
    bridge = ChatRuntimeEventBridge()
    assert bridge.frames_for(_ev(1, "some.future.event")) == []
    assert bridge.last_sequence == 1


def test_non_dict_and_bad_sequence_are_ignored() -> None:
    bridge = ChatRuntimeEventBridge()
    assert bridge.frames_for("nope") == []  # type: ignore[arg-type]
    assert bridge.frames_for({"type": "run.running"}) == []
    assert bridge.frames_for({"type": "run.running", "sequence": "abc"}) == []
    assert bridge.last_sequence == 0


# --------------------------------------------------------------------------
# Ordered delivery
# --------------------------------------------------------------------------


def test_frames_follow_sequence_order() -> None:
    bridge = ChatRuntimeEventBridge()
    events = [
        _ev(1, "run.created"),
        _ev(2, "run.running"),
        _ev(3, "run.completed", {"text": "done"}),
    ]
    frames = list(bridge.stream(events))
    names = [_parse(f)[1] for f in frames]

    assert names == ["runStatus", "runStatus", RUN_COMPLETED_EVENT, DONE_EVENT]
    seqs = [_parse(f)[2]["sequence"] for f in frames if _parse(f)[2]]
    assert seqs == sorted(seqs)


def test_out_of_order_input_is_dropped_not_reordered() -> None:
    bridge = ChatRuntimeEventBridge()
    bridge.frames_for(_ev(5, "run.running"))
    # A stale frame must not be emitted after a newer one.
    assert bridge.frames_for(_ev(3, "run.running")) == []
    assert bridge.state.skipped == [3]
    assert bridge.last_sequence == 5


def test_duplicate_sequence_is_dropped() -> None:
    bridge = ChatRuntimeEventBridge()
    first = bridge.frames_for(_ev(2, "run.running"))
    second = bridge.frames_for(_ev(2, "run.running"))
    assert len(first) == 1
    assert second == []


def test_frame_id_carries_runtime_sequence() -> None:
    bridge = ChatRuntimeEventBridge()
    frames = bridge.frames_for(_ev(7, "run.running"))
    assert _parse(frames[0])[0] == "ev-7"


# --------------------------------------------------------------------------
# Terminal converges exactly once
# --------------------------------------------------------------------------


def test_terminal_emits_once_and_ignores_trailing_events() -> None:
    bridge = ChatRuntimeEventBridge()
    events = [
        _ev(1, "run.running"),
        _ev(2, "run.completed", {"text": "final"}),
        _ev(3, "run.running"),  # late progress must not reopen
        _ev(4, "run.completed"),  # duplicate terminal must not repeat
    ]
    frames = list(bridge.stream(events))
    names = [_parse(f)[1] for f in frames]

    assert names.count(RUN_COMPLETED_EVENT) == 1
    assert names.count(DONE_EVENT) == 1
    assert names[-1] == DONE_EVENT
    assert bridge.terminal_seen is True
    assert 3 in bridge.state.skipped and 4 in bridge.state.skipped


def test_multiple_terminals_across_reconnects_converge_once() -> None:
    """A reconnect that replays the terminal must not emit a second one."""
    bridge = ChatRuntimeEventBridge()
    bridge.frames_for(_ev(1, "run.completed", {"text": "x"}))

    # Reconnect path replays the same terminal frame.
    assert bridge.frames_for(_ev(1, "run.completed", {"text": "x"})) == []
    assert bridge.close() == [sse_frame(DONE_EVENT, "[DONE]")]
    # close() is idempotent.
    assert bridge.close() == []


def test_failed_run_surfaces_on_error_channel() -> None:
    bridge = ChatRuntimeEventBridge()
    frames = bridge.frames_for(_ev(1, "run.failed", {"error": "provider 503"}))
    event_id, name, body = _parse(frames[0])
    assert name == ERROR_EVENT
    assert body["status"] == "failed"
    assert body["error"] == "provider 503"
    assert bridge.terminal_seen is True


def test_cancelled_run_converges_as_terminal_completion() -> None:
    bridge = ChatRuntimeEventBridge()
    frames = bridge.frames_for(_ev(1, "run.cancelled"))
    _event_id, name, body = _parse(frames[0])
    assert name == RUN_COMPLETED_EVENT
    assert body["status"] == "cancelled"
    assert bridge.terminal_seen is True


# --------------------------------------------------------------------------
# Cursor / reconnect
# --------------------------------------------------------------------------


def test_cursor_skips_already_delivered_frames() -> None:
    """Reconnect with after_sequence must not replay earlier frames."""
    bridge = ChatRuntimeEventBridge(after_sequence=4)
    assert bridge.frames_for(_ev(3, "run.running")) == []
    assert bridge.frames_for(_ev(4, "run.running")) == []
    assert len(bridge.frames_for(_ev(5, "run.running"))) == 1
    assert bridge.last_sequence == 5


def test_cursor_advances_only_on_delivery() -> None:
    bridge = ChatRuntimeEventBridge()
    bridge.frames_for(_ev(3, "run.running"))
    assert bridge.last_sequence == 3
    bridge.frames_for(_ev(2, "run.running"))
    assert bridge.last_sequence == 3


def test_negative_cursor_rejected() -> None:
    with pytest.raises(ValueError):
        ChatRuntimeEventBridge(after_sequence=-1)


def test_reconnect_from_cursor_resumes_without_gap_or_replay() -> None:
    """Full reconnect scenario: stop at seq 2, resume from 2, converge once."""
    events = [
        _ev(1, "run.created"),
        _ev(2, "run.running"),
        _ev(3, "run.running"),
        _ev(4, "run.completed", {"text": "final"}),
    ]

    first = ChatRuntimeEventBridge()
    delivered = first.frames_for(events[0]) + first.frames_for(events[1])
    assert len(delivered) == 2
    cursor = first.last_sequence
    assert cursor == 2

    resumed = ChatRuntimeEventBridge(after_sequence=cursor)
    tail = list(resumed.stream(events))
    names = [_parse(f)[1] for f in tail]

    # No replay of seq 1/2, no gap: seq 3 progress then one terminal then done.
    assert names == ["runStatus", RUN_COMPLETED_EVENT, DONE_EVENT]
    seqs = [_parse(f)[2]["sequence"] for f in tail if _parse(f)[2]]
    assert seqs == [3, 4]
    assert names.count(RUN_COMPLETED_EVENT) == 1
