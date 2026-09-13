"""Per-task linkage between an existing Task Center task and a Runtime run.

AG-G2-AUTO-002-B02.

Where the linkage lives
-----------------------
On the task row's **existing** extras slot. ``task_row_mappers._split_extra``
routes every task-dict key that is not a known ``evoflow_collab_tasks`` column
into that row's existing ``extra_json`` column, and ``_merge_extra`` merges it
back onto the task dict on load. So a single extra key on the task dict is a
safe, persistent, queryable location for this linkage.

Consequences, all intentional:

- no new table, no migration, and no second authoritative task/run state source;
- the linkage travels with the task through the existing save/load path, so it
  survives restarts the same way every other task field does;
- nothing has to be invented: the slot already exists and is already used for
  other extra task fields.

Duplicate-trigger behaviour (the point of this card)
---------------------------------------------------
``link_runtime_run`` never creates a second Runtime Run silently:

- no linkage yet -> attach;
- same attempt (same idempotency key) and the same run -> reuse;
- same attempt but a *different* run -> refuse (the caller must reuse the
  existing run, not create another one);
- a different attempt -> refuse unless ``supersede=True`` is passed explicitly.

Cross-organization reads are refused, not treated as "absent", so a linkage can
never be silently adopted by another organization.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app.gateway.task_runtime_context import TaskRuntimeContext

__all__ = [
    "LINKAGE_TASK_KEY",
    "LinkOutcome",
    "RuntimeRunLinkage",
    "RuntimeRunLinkageError",
    "link_runtime_run",
    "read_runtime_run_linkage",
]

# The key that lands in the task row's existing extras / extra_json slot.
LINKAGE_TASK_KEY = "runtime_run_linkage"

LinkAction = Literal["attached", "reused", "superseded"]


class RuntimeRunLinkageError(RuntimeError):
    """Raised when a runtime run linkage is missing, malformed or unsafe."""


@dataclass(frozen=True)
class RuntimeRunLinkage:
    """A persisted association between one Task Center task and one Runtime run."""

    runtime_run_id: str
    org_scope_key: str
    task_id: str
    idempotency_key: str

    def to_dict(self) -> dict[str, str]:
        return {
            "runtime_run_id": self.runtime_run_id,
            "org_scope_key": self.org_scope_key,
            "task_id": self.task_id,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class LinkOutcome:
    """Result of a linkage attempt."""

    linkage: RuntimeRunLinkage
    action: LinkAction


def _decode(raw: Any) -> RuntimeRunLinkage:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeRunLinkageError("stored runtime run linkage is not valid JSON") from exc
    if not isinstance(raw, Mapping):
        raise RuntimeRunLinkageError("stored runtime run linkage is not a mapping")
    values: dict[str, str] = {}
    for field_name in ("runtime_run_id", "org_scope_key", "task_id", "idempotency_key"):
        value = str(raw.get(field_name) or "").strip()
        if not value:
            raise RuntimeRunLinkageError(f"stored runtime run linkage has no {field_name}")
        values[field_name] = value
    return RuntimeRunLinkage(**values)


def read_runtime_run_linkage(
    task: Mapping[str, Any] | None,
    *,
    org_scope_key: str,
) -> RuntimeRunLinkage | None:
    """Read the linkage for ``task``, scoped to the caller's trusted organization.

    Returns ``None`` only when no linkage exists. A linkage that exists but
    belongs to another organization, or to another task, is refused.
    """
    if not isinstance(task, Mapping):
        raise RuntimeRunLinkageError("no server-loaded task row supplied")
    trusted_org = str(org_scope_key or "").strip()
    if not trusted_org:
        raise RuntimeRunLinkageError("no trusted organization scope supplied")

    raw = task.get(LINKAGE_TASK_KEY)
    if raw is None:
        return None

    linkage = _decode(raw)
    if linkage.org_scope_key != trusted_org:
        raise RuntimeRunLinkageError("runtime run linkage belongs to another organization")
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        raise RuntimeRunLinkageError("task row has no id")
    if linkage.task_id != task_id:
        raise RuntimeRunLinkageError("runtime run linkage belongs to another task")
    return linkage


def link_runtime_run(
    task: Mapping[str, Any] | None,
    *,
    context: TaskRuntimeContext,
    runtime_run_id: str,
    supersede: bool = False,
) -> tuple[dict[str, Any], LinkOutcome]:
    """Attach or reuse the runtime run linkage on an existing task row.

    Returns the updated task dict (to be persisted through the existing task
    save path) plus what happened. Raises :class:`RuntimeRunLinkageError` rather
    than creating a second Runtime Run for the same trigger.
    """
    if not isinstance(task, Mapping):
        raise RuntimeRunLinkageError("no server-loaded task row supplied")
    if not isinstance(context, TaskRuntimeContext):
        raise RuntimeRunLinkageError("no trusted task runtime context supplied")

    run_id = str(runtime_run_id or "").strip()
    if not run_id:
        raise RuntimeRunLinkageError("runtime_run_id is required")

    task_id = str(task.get("id") or "").strip()
    if not task_id:
        raise RuntimeRunLinkageError("task row has no id")
    if task_id != context.task_id:
        raise RuntimeRunLinkageError("task runtime context does not belong to this task")
    if not context.authorized:
        raise RuntimeRunLinkageError("task runtime context is not authorized")

    existing = read_runtime_run_linkage(task, org_scope_key=context.org_scope_key)

    if existing is None:
        action: LinkAction = "attached"
        linkage = RuntimeRunLinkage(
            runtime_run_id=run_id,
            org_scope_key=context.org_scope_key,
            task_id=task_id,
            idempotency_key=context.idempotency_key,
        )
    elif existing.idempotency_key == context.idempotency_key:
        if existing.runtime_run_id != run_id:
            raise RuntimeRunLinkageError(
                "this trigger is already linked to another runtime run; "
                "reuse the existing run instead of creating a second one"
            )
        action = "reused"
        linkage = existing
    elif supersede:
        action = "superseded"
        linkage = RuntimeRunLinkage(
            runtime_run_id=run_id,
            org_scope_key=context.org_scope_key,
            task_id=task_id,
            idempotency_key=context.idempotency_key,
        )
    else:
        raise RuntimeRunLinkageError(
            "this task is already linked to a runtime run for another attempt; "
            "pass supersede=True to replace it explicitly"
        )

    updated = dict(task)
    updated[LINKAGE_TASK_KEY] = linkage.to_dict()
    return updated, LinkOutcome(linkage=linkage, action=action)
