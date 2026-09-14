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
    """Build a Runtime Event v1 frame, shaped exactly like ``event_dict()``."""
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


# --------------------------------------------------------------------------
# Cancel semantics
# --------------------------------------------------------------------------


def test_late_cancel_after_completion_does_not_regress_terminal() -> None:
    """A cancel arriving after completion must not emit a second terminal."""
    bridge = ChatRuntimeEventBridge()
    first = bridge.frames_for(_ev(1, "run.completed", {"text": "done"}))
    assert _parse(first[0])[1] == RUN_COMPLETED_EVENT

    late_cancel = bridge.frames_for(_ev(2, "run.cancelled"))
    assert late_cancel == []
    assert bridge.state.skipped == [2]
    assert bridge.terminal_seen is True


def test_cancel_before_completion_wins_and_closes_stream() -> None:
    """Cancel racing ahead of completion converges once and ends the stream."""
    bridge = ChatRuntimeEventBridge()
    events = [
        _ev(1, "run.running"),
        _ev(2, "run.cancelled"),
        _ev(3, "run.completed", {"text": "too late"}),  # must not reopen
    ]
    frames = list(bridge.stream(events))
    names = [_parse(f)[1] for f in frames]

    assert names == ["runStatus", RUN_COMPLETED_EVENT, DONE_EVENT]
    terminal = [_parse(f)[2] for f in frames if _parse(f)[1] == RUN_COMPLETED_EVENT][0]
    assert terminal["status"] == "cancelled"
    assert 3 in bridge.state.skipped


def test_cancel_reconnect_does_not_re_emit_cancelled_terminal() -> None:
    """Reconnecting with the cursor after a cancel must not replay it."""
    bridge = ChatRuntimeEventBridge()
    bridge.frames_for(_ev(1, "run.running"))
    bridge.frames_for(_ev(2, "run.cancelled"))
    cursor = bridge.last_sequence
    assert cursor == 2

    resumed = ChatRuntimeEventBridge(after_sequence=cursor)
    frames = list(resumed.stream([_ev(1, "run.running"), _ev(2, "run.cancelled")]))
    assert [_parse(f)[1] for f in frames] == [DONE_EVENT]


# --------------------------------------------------------------------------
# Reconnect convergence (the load-bearing cross-cutting scenario)
# --------------------------------------------------------------------------


def _run_prefix() -> list[dict]:
    """The shared run history, as the Runtime repository would replay it."""
    return [
        _ev(1, "run.created"),
        _ev(2, "run.running"),
        _ev(3, "run.executing"),
        _ev(4, "asset.available", {"uri": "s3://x/1.png"}),
        _ev(5, "run.completed", {"text": "final answer"}),
    ]


def test_full_reconnect_flow_converges_to_single_terminal_and_single_done() -> None:
    """Segmented reconnect over the whole run: one terminal, one done, no gaps."""
    history = _run_prefix()

    # Segment 1: fresh stream, drop the connection after seq 2.
    seg1 = ChatRuntimeEventBridge()
    seg1_frames = seg1.frames_for(history[0]) + seg1.frames_for(history[1])

    # Segment 2: client reconnects carrying the last seen cursor.
    seg2 = ChatRuntimeEventBridge(after_sequence=seg1.last_sequence)
    seg2_frames = list(seg2.stream(history))

    total = seg1_frames + seg2_frames
    names = [_parse(f)[1] for f in total]

    # Exactly one terminal and one done across the *whole* reconnected session.
    assert names.count(RUN_COMPLETED_EVENT) == 1
    assert names.count(DONE_EVENT) == 1
    assert names[-1] == DONE_EVENT

    # Every delivered frame carries a strictly increasing sequence.
    seqs = [_parse(f)[2]["sequence"] for f in total if _parse(f)[2]]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))

    # No gap: segments together cover the entire run without replaying seq 1/2.
    assert seqs == [1, 2, 3, 4, 5]


def test_reconnect_after_terminal_emits_only_done() -> None:
    """Reconnecting on an already-finished run yields nothing but the close frame."""
    history = _run_prefix()

    first = ChatRuntimeEventBridge()
    list(first.stream(history))
    assert first.terminal_seen is True

    resumed = ChatRuntimeEventBridge(after_sequence=first.last_sequence)
    frames = list(resumed.stream(history))
    assert [ _parse(f)[1] for f in frames ] == [DONE_EVENT]


def test_repeated_reconnect_delivers_terminal_once_per_stream() -> None:
    """Each attach emits at most one terminal and exactly one ``done``.

    A reconnect resuming *before* the terminal must re-deliver it (the client
    never saw it); a reconnect resuming *at or after* it must not. So the
    invariant is per-stream, not global: never two terminals in one stream, and
    always exactly one ``done``.
    """
    history = _run_prefix()

    per_stream_terminal: dict[int, int] = {}
    for cursor in (0, 2, 5):
        bridge = ChatRuntimeEventBridge(after_sequence=cursor)
        frames = list(bridge.stream(history))
        names = [_parse(f)[1] for f in frames]
        assert names.count(DONE_EVENT) == 1
        assert names.count(RUN_COMPLETED_EVENT) <= 1
        per_stream_terminal[cursor] = names.count(RUN_COMPLETED_EVENT)

    # Below the terminal -> delivered; at the terminal -> suppressed.
    assert per_stream_terminal[0] == 1
    assert per_stream_terminal[2] == 1
    assert per_stream_terminal[5] == 0
