"""QAgent Event Protocol v1.

These events are the stable wire contract. They intentionally do not mirror
LangGraph or AgentScope event frames.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    RUN_CREATED = "run.created"
    RUN_QUEUED = "run.queued"
    RUN_PLANNING = "run.planning"
    RUN_WAITING_APPROVAL = "run.waiting_approval"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_REJECTED = "approval.rejected"
    RUN_RUNNING = "run.running"
    RUN_EXECUTING = "run.executing"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    ASSET_AVAILABLE = "asset.available"


TERMINAL_STATUSES = {"completed", "failed", "cancelled", "timed_out"}
INCOMPLETE_STATUSES = {"created", "queued", "planning", "waiting_approval", "running", "executing"}


def utc_now() -> datetime:
    return datetime.now(UTC)


def event_dict(row: dict[str, Any]) -> dict[str, Any]:
    occurred_at = row["occurred_at"]
    if isinstance(occurred_at, datetime):
        occurred_at = occurred_at.astimezone(UTC).isoformat()
    return {
        "event_id": row["event_id"],
        "run_id": row["run_id"],
        "sequence": int(row["sequence"]),
        "occurred_at": occurred_at,
        "type": row["type"],
        "payload": row.get("payload") or {},
    }
