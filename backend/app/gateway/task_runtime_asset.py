"""Safe, displayable Runtime asset metadata for the Task Center.

AG-G2-AUTO-015.

The Runtime owns the asset bytes; the Task Center must never hold them. Only the
metadata a user needs in order to *know an asset exists and what it is* is
projected, so the existing ``execution_history`` stays a small, plain JSON list
and no binary or large object ever reaches SQLite.

What is safe to show, and what is not:

- ``asset_id`` is the identity and is required — it is also the dedup key, so an
  asset repeatedly announced by the Runtime is written once;
- ``asset_type`` / ``content_type`` / ``name`` are display fields; the name is
  constrained so it cannot smuggle a filesystem path;
- ``uri`` is **not** copied. A Runtime uri is an internal storage reference and
  can name a host, a bucket or a local path, so it is only surfaced when it is a
  plain relative reference, and otherwise replaced by
  ``reference_available: true`` — the asset is still discoverable, without
  learning where the server keeps it;
- ``metadata`` is never copied, because it is unbounded and not a presentation
  field;
- every record carries the run and organization scope, so an asset projection can
  always be tied back to the run and the organization it belongs to.

An incoming asset is attributed to the task's linked run only; a frame naming
another run (or another organization's run) is refused by the same gate as every
other frame, so an asset can never be attached across run or organization.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.gateway.task_runtime_context import TaskRuntimeContext
from app.gateway.task_runtime_linkage import RuntimeRunLinkageError, read_linked_runtime_run
from app.gateway.task_runtime_projection import (
    HISTORY_FIELD,
    ProjectionOutcome,
    RuntimeProjectionError,
    build_runtime_history_record,
)

__all__ = [
    "ASSET_STATUS",
    "project_runtime_asset",
    "sanitize_asset_reference",
]

# The history marker for an asset announcement. It is not a run status: the task
# status is never changed by an asset, the record only makes it visible.
ASSET_STATUS = "asset_available"

MAX_NAME = 200
MAX_REFERENCE = 200

_ID_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_TYPE_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
_CONTENT_TYPE_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}/[A-Za-z0-9._+-]{1,63}")
_SAFE_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


def _clean_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _ID_SHAPE.fullmatch(candidate) else None


def _clean_type(value: Any, pattern: re.Pattern[str]) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if pattern.fullmatch(candidate) else None


def _clean_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = " ".join(value.split())[:MAX_NAME]
    # A display name must not be a path: a separator would carry a location.
    if not name or any(sep in name for sep in ("/", "\\", "..")):
        return None
    return name


def _clean_reference(value: Any) -> str | None:
    """A uri only when it is a plain relative reference, never a location."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if "://" in candidate or candidate.startswith("/") or "\\" in candidate:
        return None
    if ".." in candidate or "@" in candidate:
        return None
    return candidate if _SAFE_REFERENCE.fullmatch(candidate) else None


def _clean_size(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def sanitize_asset_reference(asset: Any) -> dict[str, Any] | None:
    """The displayable summary of a Runtime asset, or ``None``.

    ``None`` means the asset cannot be shown at all (no usable identity), which is
    a normal outcome rather than a failure.
    """
    if not isinstance(asset, Mapping):
        return None

    asset_id = _clean_id(asset.get("asset_id") or asset.get("id"))
    if asset_id is None:
        return None

    summary: dict[str, Any] = {"asset_id": asset_id}

    name = _clean_name(asset.get("name"))
    if name is not None:
        summary["name"] = name

    asset_type = _clean_type(asset.get("asset_type"), _TYPE_SHAPE)
    if asset_type is not None:
        summary["asset_type"] = asset_type

    content_type = _clean_type(asset.get("content_type"), _CONTENT_TYPE_SHAPE)
    if content_type is not None:
        summary["content_type"] = content_type

    size = _clean_size(asset.get("size") if "size" in asset else asset.get("byte_size"))
    if size is not None:
        summary["size"] = size

    reference = _clean_reference(asset.get("uri"))
    if reference is not None:
        summary["reference"] = reference
    summary["reference_available"] = reference is not None

    return summary


def _already_projected(task: Mapping[str, Any], *, run_id: str, asset_id: str) -> bool:
    history = task.get(HISTORY_FIELD)
    if not isinstance(history, list):
        return False
    for entry in history:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("runtime_run_id") or "") != run_id:
            continue
        asset = entry.get("asset")
        if isinstance(asset, Mapping) and str(asset.get("asset_id") or "") == asset_id:
            return True
    return False


def project_runtime_asset(
    task: Mapping[str, Any] | None,
    *,
    context: TaskRuntimeContext,
    asset: Any,
    event_id: str | None = None,
    sequence: int | None = None,
    expected_run_id: str | None = None,
) -> tuple[dict[str, Any], ProjectionOutcome]:
    """Record a Runtime asset's metadata on a linked task row.

    Returns the updated task dict (persist through the existing save path) and
    what happened. The task status is never changed: an asset is a side record,
    and it is written at most once per asset per run.

    ``expected_run_id`` is for callers carrying a run id from an incoming frame.
    When given it must match the stored linkage, so an asset announced for another
    run — or for another organization's run — is refused instead of being attached
    to this task's run.
    """
    if not isinstance(task, Mapping):
        raise RuntimeProjectionError("no server-loaded task row supplied")

    try:
        linkage = read_linked_runtime_run(task, context=context)
    except RuntimeRunLinkageError as exc:
        raise RuntimeProjectionError(str(exc)) from exc
    if linkage is None:
        raise RuntimeProjectionError("task is not linked to a runtime run")

    run_id = linkage.runtime_run_id

    if expected_run_id is not None and str(expected_run_id).strip() != run_id:
        raise RuntimeProjectionError("runtime event belongs to a different runtime run")

    current = str(task.get("status") or "").strip().lower()

    summary = sanitize_asset_reference(asset)
    if summary is None:
        return dict(task), ProjectionOutcome("noop", "asset_not_displayable", current, current)

    if _already_projected(task, run_id=run_id, asset_id=str(summary["asset_id"])):
        return dict(task), ProjectionOutcome("noop", "duplicate_asset", current, current)

    record = build_runtime_history_record(
        runtime_status=ASSET_STATUS,
        run_id=run_id,
        org_scope_key=context.org_scope_key,
        event_id=event_id,
        sequence=sequence,
    )
    record["asset"] = summary

    updated = dict(task)
    history = updated.get(HISTORY_FIELD)
    updated[HISTORY_FIELD] = [*(history if isinstance(history, list) else []), record]
    return updated, ProjectionOutcome("history_only", ASSET_STATUS, current, current, record)
