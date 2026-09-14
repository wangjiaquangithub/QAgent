"""Chat projection of QAgent Runtime results.

Verifies the projection rules that keep the existing chat UI correct after a
Runtime-backed run:

* The Runtime stays the execution fact source; the chat fields are compatibility
  projections onto the vocabulary the UI already consumes.
* Terminal outcomes never regress — a late ``running`` report cannot reopen a
  finished run (this is the protection against reconnect/duplicate-attach/bad
  cancel ordering producing a second visible terminal).
* Both success and provider-failure paths map onto user-visible fields, with the
  failure surfaced as an error rather than a silent success.

The Runtime kernel is not exercised; inputs are the documented
``RuntimeService.get_result`` shape.
"""

from __future__ import annotations

import pytest

from app.gateway.chat_runtime_result import (
    RUNTIME_TERMINAL_STATUSES,
    chat_live_status_for_runtime_status,
    chat_session_status_for_runtime_status,
    project_runtime_result,
    runtime_status_is_terminal,
    terminal_does_not_regress,
)

RUN_ID = "run-g2-1"


def _contains(value: str | None, needle: str) -> bool:
    return bool(value) and needle in str(value)


def _result(status: str, *, result: object = None, error: object = None, assets: object = None) -> dict:
    return {
        "run_id": RUN_ID,
        "status": status,
        "result": result,
        "assets": assets if assets is not None else [],
        "error": error,
    }


# --------------------------------------------------------------------------
# Fact source + vocabulary mapping
# --------------------------------------------------------------------------


def test_runtime_terminal_set_matches_kernel_contract() -> None:
    """Must stay aligned with app.qagent_runtime.events.TERMINAL_STATUSES."""
    assert RUNTIME_TERMINAL_STATUSES == {"completed", "failed", "cancelled", "timed_out"}


@pytest.mark.parametrize(
    ("runtime_status", "live", "session"),
    [
        ("created", "queued", "pending"),
        ("queued", "queued", "pending"),
        ("planning", "running", "running"),
        ("waiting_approval", "running", "running"),
        ("running", "running", "running"),
        ("executing", "running", "running"),
        ("completed", "completed_success", "success"),
        ("failed", "completed_error", "fail"),
        ("cancelled", "completed_cancelled", "cancelled"),
        ("timed_out", "completed_error", "fail"),
    ],
)
def test_status_vocabulary_projection(runtime_status: str, live: str, session: str) -> None:
    assert chat_live_status_for_runtime_status(runtime_status) == live
    assert chat_session_status_for_runtime_status(runtime_status) == session


def test_unknown_status_reports_progress_not_terminal() -> None:
    """A newer/unknown Runtime status must not be projected as a finished run."""
    assert chat_live_status_for_runtime_status("some_future_status") == "running"
    assert chat_session_status_for_runtime_status("some_future_status") == "running"


def test_none_result_projects_to_none() -> None:
    """A missing run must not fabricate a chat outcome."""
    assert project_runtime_result(None) is None
    assert project_runtime_result({}) is None
    assert project_runtime_result({"run_id": RUN_ID}) is None


# --------------------------------------------------------------------------
# Success path
# --------------------------------------------------------------------------


def test_success_path_maps_result_text_and_assets() -> None:
    proj = project_runtime_result(
        _result(
            "completed",
            result={"text": "hello from runtime"},
            assets=[{"asset_id": "a1", "kind": "file"}],
        )
    )
    assert proj is not None
    assert proj.runtime_status == "completed"
    assert proj.is_terminal is True
    assert proj.chat_live_status == "completed_success"
    assert proj.chat_session_status == "success"
    assert proj.result_text == "hello from runtime"
    assert proj.error_message is None
    assert proj.assets == [{"asset_id": "a1", "kind": "file"}]


def test_success_path_supports_message_list_payload() -> None:
    proj = project_runtime_result(
        _result(
            "completed",
            result={"messages": [{"role": "user", "content": "q"}, {"role": "assistant", "content": "final answer"}]},
        )
    )
    assert proj is not None
    assert proj.result_text == "final answer"


def test_success_path_live_snapshot_fields_match_existing_writer() -> None:
    """Fields must be consumable by the existing evoflow_chat_live_runs writer."""
    proj = project_runtime_result(_result("completed", result={"text": "done"}))
    assert proj is not None
    fields = proj.as_live_run_snapshot_fields()
    assert fields["runId"] == RUN_ID
    assert fields["status"] == "completed_success"
    assert fields["partialText"] == "done"


def test_user_visible_result_shape() -> None:
    proj = project_runtime_result(_result("completed", result={"text": "ok"}))
    assert proj is not None
    visible = proj.as_user_visible_result()
    assert visible["runId"] == RUN_ID
    assert visible["terminal"] is True
    assert visible["result"] == "ok"
    assert visible["error"] is None


# --------------------------------------------------------------------------
# Provider failure path
# --------------------------------------------------------------------------


def test_provider_failure_surfaces_error_not_success() -> None:
    proj = project_runtime_result(
        _result("failed", error={"message": "provider 503", "code": "upstream_error"})
    )
    assert proj is not None
    assert proj.runtime_status == "failed"
    assert proj.is_terminal is True
    assert proj.chat_live_status == "completed_error"
    assert proj.chat_session_status == "fail"
    assert proj.error_message == "provider 503"
    assert proj.error_code == "upstream_error"
    # A failed run must not present a success result body.
    assert proj.result_text is None


def test_provider_failure_without_payload_still_reports_error() -> None:
    proj = project_runtime_result(_result("failed"))
    assert proj is not None
    assert proj.error_message == "run failed"
    assert proj.chat_session_status == "fail"


def test_timeout_and_cancel_are_terminal_errors() -> None:
    timed_out = project_runtime_result(_result("timed_out"))
    assert timed_out is not None
    assert timed_out.is_terminal is True
    assert timed_out.chat_session_status == "fail"
    assert _contains(timed_out.error_message, "timed out")

    cancelled = project_runtime_result(_result("cancelled"))
    assert cancelled is not None
    assert cancelled.is_terminal is True
    assert cancelled.chat_session_status == "cancelled"
    assert _contains(cancelled.error_message, "cancelled")


# --------------------------------------------------------------------------
# Terminal must not regress
# --------------------------------------------------------------------------


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled", "timed_out"])
def test_terminal_state_never_regresses_to_running(terminal: str) -> None:
    proj = project_runtime_result(_result("running"), previous_status=terminal)
    assert proj is not None
    assert proj.runtime_status == terminal
    assert proj.is_terminal is True


def test_non_terminal_to_terminal_is_allowed() -> None:
    proj = project_runtime_result(_result("completed", result={"text": "x"}), previous_status="running")
    assert proj is not None
    assert proj.runtime_status == "completed"
    assert proj.is_terminal is True


def test_terminal_to_different_terminal_is_allowed() -> None:
    """A genuinely later terminal (e.g. failed after cancel) still wins."""
    proj = project_runtime_result(_result("failed"), previous_status="cancelled")
    assert proj is not None
    assert proj.runtime_status == "failed"


def test_helpers_agree_with_projection_rule() -> None:
    assert terminal_does_not_regress("completed", "running") is False
    assert terminal_does_not_regress("running", "completed") is True
    assert terminal_does_not_regress(None, "running") is True
    assert runtime_status_is_terminal("completed") is True
    assert runtime_status_is_terminal("running") is False
