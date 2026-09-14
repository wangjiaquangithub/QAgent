"""Protective tests for Runtime asset metadata projection.

Covers AG-G2-AUTO-015: an asset's allowlisted metadata reaches the existing
history without its bytes or its storage location, never changes the task status,
is written at most once per asset per run, and cannot be attached across run or
organization.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.gateway.task_runtime_asset import ASSET_STATUS, sanitize_asset_reference
from app.gateway.task_runtime_context import build_task_runtime_context
from app.gateway.task_runtime_event_bridge import RuntimeEventBridgeError, apply_runtime_event
from app.gateway.task_runtime_linkage import link_runtime_run
from app.gateway.task_runtime_projection import HISTORY_FIELD, RuntimeProjectionError

ORG_A = "org-a"
ORG_B = "org-b"
TASK_ID = "e4c1a2b0"
RUN_ID = "run-1"
ASSET_ID = "asset_9f2c1d"


def _authz(org_id: str = ORG_A) -> dict[str, Any]:
    return {
        "org_id": org_id,
        "principal": {"principal_id": "webui:1", "principal_type": "internal"},
        "scope_id": "personal:webui:1",
        "is_org_admin": False,
    }


def _task(*, status: str = "executing") -> dict[str, Any]:
    return {
        "id": TASK_ID,
        "name": "每周经营简报",
        "run_mode": "unattended",
        "status": status,
        "unattended_attempts": 0,
        "execution_history": [],
    }


def _linked(*, status: str = "executing", org_id: str = ORG_A) -> tuple[dict[str, Any], Any]:
    ctx = build_task_runtime_context(task=_task(status=status), authz=_authz(org_id), authorized=True)
    task, _ = link_runtime_run(_task(status=status), context=ctx, runtime_run_id=RUN_ID)
    return task, ctx


def _history(task: dict[str, Any]) -> list[dict[str, Any]]:
    return list(task.get(HISTORY_FIELD) or [])


def _asset(**extra: Any) -> dict[str, Any]:
    asset: dict[str, Any] = {
        "asset_id": ASSET_ID,
        "asset_type": "report",
        "content_type": "application/pdf",
        "name": "区域销量周报.pdf",
        "uri": "reports/2026/w38.pdf",
        "size": 20480,
    }
    asset.update(extra)
    return asset


# --- the metadata sanitiser ------------------------------------------------


def test_a_plain_asset_keeps_only_display_fields() -> None:
    summary = sanitize_asset_reference(_asset(metadata={"internal": "secret"}, prompt="raw prompt"))

    assert summary is not None
    assert summary["asset_id"] == ASSET_ID
    assert summary["name"] == "区域销量周报.pdf"
    assert summary["asset_type"] == "report"
    assert summary["content_type"] == "application/pdf"
    assert summary["reference"] == "reports/2026/w38.pdf"
    assert summary["reference_available"] is True
    assert "metadata" not in summary
    assert "prompt" not in summary


@pytest.mark.parametrize(
    "uri",
    [
        "https://internal.example/v1/reports/1.pdf",
        "s3://bucket/key",
        "/var/private/reports/1.pdf",
        "C:\\Users\\ops\\report.pdf",
        "../../etc/passwd",
        "reports/../../../etc/passwd",
        "user@host:reports/1.pdf",
    ],
)
def test_a_storage_location_is_never_surfaced(uri: str) -> None:
    summary = sanitize_asset_reference(_asset(uri=uri))

    assert summary is not None
    assert "reference" not in summary
    assert summary["reference_available"] is False
    assert uri not in json.dumps(summary, ensure_ascii=False)


@pytest.mark.parametrize(
    "name",
    ["../etc/passwd", "reports/1.pdf", "C:\\ops\\1.pdf", "   "],
)
def test_a_name_that_is_really_a_path_is_dropped(name: str) -> None:
    summary = sanitize_asset_reference(_asset(name=name, uri=None))

    assert summary is not None
    assert "name" not in summary


@pytest.mark.parametrize(
    "asset",
    [
        None,
        "an asset id",
        {},
        {"name": "no identity"},
        {"asset_id": ""},
        {"asset_id": "has space"},
        {"asset_id": {"nested": "x"}},
    ],
)
def test_an_asset_without_a_usable_identity_is_not_displayable(asset: Any) -> None:
    assert sanitize_asset_reference(asset) is None


def test_asset_identity_accepts_the_id_alias() -> None:
    summary = sanitize_asset_reference({"id": ASSET_ID})

    assert summary == {"asset_id": ASSET_ID, "reference_available": False}


# --- projection ------------------------------------------------------------


def test_an_asset_is_recorded_without_moving_the_task_status() -> None:
    task, ctx = _linked(status="executing")
    from app.gateway.task_runtime_asset import project_runtime_asset

    updated, outcome = project_runtime_asset(task, context=ctx, asset=_asset())

    assert outcome.action == "history_only"
    assert outcome.reason == ASSET_STATUS
    assert updated["status"] == "executing", "an asset must not change the status"

    record = _history(updated)[-1]
    assert record["runtime_run_id"] == RUN_ID
    assert record["org_scope_key"] == ctx.org_scope_key
    assert record["runtime_status"] == ASSET_STATUS
    assert record["asset"]["asset_id"] == ASSET_ID


def test_an_asset_without_a_usable_identity_is_not_written() -> None:
    task, ctx = _linked()
    from app.gateway.task_runtime_asset import project_runtime_asset

    updated, outcome = project_runtime_asset(task, context=ctx, asset={"name": "no id"})

    assert outcome.action == "noop"
    assert outcome.reason == "asset_not_displayable"
    assert _history(updated) == []


def test_the_same_asset_is_written_once_even_from_different_events() -> None:
    task, ctx = _linked()
    from app.gateway.task_runtime_asset import project_runtime_asset

    first, first_outcome = project_runtime_asset(
        task, context=ctx, asset=_asset(), event_id="ev-a", sequence=1
    )
    assert first_outcome.action == "history_only"

    second, second_outcome = project_runtime_asset(
        first, context=ctx, asset=_asset(), event_id="ev-b", sequence=2
    )

    assert second_outcome.action == "noop"
    assert second_outcome.reason == "duplicate_asset"
    assert len(_history(second)) == 1


def test_two_different_assets_are_both_recorded() -> None:
    task, ctx = _linked()
    from app.gateway.task_runtime_asset import project_runtime_asset

    first, _ = project_runtime_asset(task, context=ctx, asset=_asset(), event_id="ev-a", sequence=1)
    second, outcome = project_runtime_asset(
        first,
        context=ctx,
        asset=_asset(asset_id="asset_77aa11", name="第二份.pdf"),
        event_id="ev-b",
        sequence=2,
    )

    assert outcome.action == "history_only"
    assert [entry["asset"]["asset_id"] for entry in _history(second)] == [
        ASSET_ID,
        "asset_77aa11",
    ]


def test_an_asset_cannot_be_attached_across_organization() -> None:
    task, _ = _linked(org_id=ORG_A)
    from app.gateway.task_runtime_asset import project_runtime_asset

    other = build_task_runtime_context(task=_task(), authz=_authz(ORG_B), authorized=True)

    with pytest.raises(RuntimeProjectionError, match="another organization"):
        project_runtime_asset(task, context=other, asset=_asset())


def test_an_asset_cannot_be_attached_to_an_unlinked_task() -> None:
    from app.gateway.task_runtime_asset import project_runtime_asset

    ctx = build_task_runtime_context(task=_task(), authz=_authz(ORG_A), authorized=True)

    with pytest.raises(RuntimeProjectionError, match="not linked to a runtime run"):
        project_runtime_asset(_task(), context=ctx, asset=_asset())


# --- through the event bridge ----------------------------------------------


def test_an_asset_frame_is_projected_through_the_bridge() -> None:
    task, ctx = _linked()

    updated, outcome = apply_runtime_event(
        task,
        context=ctx,
        event={
            "event_id": "ev-asset",
            "run_id": RUN_ID,
            "sequence": 2,
            "type": "asset.available",
            "payload": {"asset": _asset(metadata={"secret": "DO-NOT-LEAK"})},
        },
    )

    assert outcome.action == "history_only"
    record = _history(updated)[-1]
    assert record["asset"]["asset_id"] == ASSET_ID
    assert updated["status"] == "executing"

    serialized = json.dumps(_history(updated), ensure_ascii=False)
    assert "DO-NOT-LEAK" not in serialized
    assert "metadata" not in serialized


def test_an_asset_frame_for_another_run_is_refused() -> None:
    task, ctx = _linked()
    before = list(_history(task))

    with pytest.raises(RuntimeEventBridgeError, match="different runtime run"):
        apply_runtime_event(
            task,
            context=ctx,
            event={
                "event_id": "ev-asset",
                "run_id": "run-999",
                "sequence": 2,
                "type": "asset.available",
                "payload": {"asset": _asset()},
            },
        )

    assert _history(task) == before
