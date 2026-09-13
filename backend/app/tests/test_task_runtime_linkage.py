"""Protective tests for the persisted Task Center ↔ Runtime run linkage.

Covers AG-G2-AUTO-002-B02 acceptance points:
- the linkage is stored in an already-existing structured task field;
- the existing persistence contract round-trips it;
- a duplicate trigger reuses the same linkage or is safely refused, never a
  silent second Runtime Run;
- a linkage cannot be read across organizations or across tasks.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.gateway.task_runtime_context import build_task_runtime_context
from app.gateway.task_runtime_linkage import (
    LINKAGE_TASK_KEY,
    RuntimeRunLinkageError,
    link_runtime_run,
    read_linked_runtime_run,
    read_runtime_run_linkage,
)

ORG_A = "org-a"
ORG_B = "org-b"


def _authz(org_id: str, principal_id: str = "webui:1") -> dict[str, Any]:
    return {
        "org_id": org_id,
        "principal": {"principal_id": principal_id, "principal_type": "internal"},
        "scope_id": f"personal:{principal_id}",
        "is_org_admin": False,
    }


def _task(*, task_id: str = "e4c1a2b0", attempts: int = 0, **extra: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": task_id,
        "name": "每周经营简报",
        "run_mode": "unattended",
        "status": "inbox",
        "unattended_attempts": attempts,
    }
    task.update(extra)
    return task


def _ctx(org_id: str = ORG_A, *, task_id: str = "e4c1a2b0", attempts: int = 0):
    task = _task(task_id=task_id, attempts=attempts)
    return build_task_runtime_context(task=task, authz=_authz(org_id), authorized=True)


# --- the storage slot is an existing one ---------------------------------


def test_linkage_uses_the_existing_task_extras_slot() -> None:
    # The key must not be a known evoflow_collab_tasks column, otherwise it
    # would not route into the existing extra_json slot.
    from evoflow.persistence.task_row_mappers import _TASK_KNOWN

    assert LINKAGE_TASK_KEY not in _TASK_KNOWN


def test_linkage_round_trips_through_the_existing_persistence_contract() -> None:
    from evoflow.persistence import task_row_mappers as m

    ctx = _ctx()
    task, _ = link_runtime_run(_task(), context=ctx, runtime_run_id="run-1")

    # Write direction: exactly what bundle_to_rows does per task row.
    known, extra_json = m._split_extra(task, m._TASK_KNOWN)
    assert LINKAGE_TASK_KEY not in known
    stored = json.loads(extra_json)
    assert stored[LINKAGE_TASK_KEY]["runtime_run_id"] == "run-1"

    # Read direction: exactly what the row mappers do on load.
    restored = m._merge_extra(dict(known), extra_json)
    assert restored[LINKAGE_TASK_KEY]["runtime_run_id"] == "run-1"
    assert read_runtime_run_linkage(restored, org_scope_key=ctx.org_scope_key) is not None


def test_linkage_is_written_by_the_public_bundle_mapper() -> None:
    from evoflow.persistence.task_row_mappers import bundle_to_rows

    ctx = _ctx()
    task, _ = link_runtime_run(_task(), context=ctx, runtime_run_id="run-1")

    parts = bundle_to_rows({"id": "proj-1", "tasks": [task], "name": "p"})
    rows = [r for r in parts["tasks"] if r.get("task_id") == "e4c1a2b0"]
    assert rows, "expected a task row for the linked task"
    assert json.loads(rows[0]["extra_json"])[LINKAGE_TASK_KEY]["runtime_run_id"] == "run-1"


# --- duplicate triggers ---------------------------------------------------


def test_first_trigger_attaches_the_linkage() -> None:
    ctx = _ctx()
    updated, outcome = link_runtime_run(_task(), context=ctx, runtime_run_id="run-1")

    assert outcome.action == "attached"
    assert outcome.linkage.runtime_run_id == "run-1"
    assert outcome.linkage.org_scope_key == ctx.org_scope_key
    assert outcome.linkage.idempotency_key == ctx.idempotency_key
    assert updated[LINKAGE_TASK_KEY]["runtime_run_id"] == "run-1"


def test_duplicate_trigger_reuses_the_same_linkage_without_a_second_run() -> None:
    ctx = _ctx()
    first, _ = link_runtime_run(_task(), context=ctx, runtime_run_id="run-1")

    second, outcome = link_runtime_run(first, context=ctx, runtime_run_id="run-1")

    assert outcome.action == "reused"
    assert outcome.linkage.runtime_run_id == "run-1"
    # Still exactly one linkage, still the first run: no second Runtime Run.
    assert second[LINKAGE_TASK_KEY] == first[LINKAGE_TASK_KEY]
    assert len([k for k in second if k == LINKAGE_TASK_KEY]) == 1


def test_duplicate_trigger_with_a_different_run_is_refused() -> None:
    ctx = _ctx()
    first, _ = link_runtime_run(_task(), context=ctx, runtime_run_id="run-1")

    with pytest.raises(RuntimeRunLinkageError, match="reuse the existing run"):
        link_runtime_run(first, context=ctx, runtime_run_id="run-2")

    # The refusal left the original linkage untouched.
    assert first[LINKAGE_TASK_KEY]["runtime_run_id"] == "run-1"


def test_new_attempt_is_refused_unless_explicitly_superseded() -> None:
    first_ctx = _ctx(attempts=0)
    first, _ = link_runtime_run(_task(), context=first_ctx, runtime_run_id="run-1")

    retry_ctx = _ctx(attempts=1)
    with pytest.raises(RuntimeRunLinkageError, match="another attempt"):
        link_runtime_run(first, context=retry_ctx, runtime_run_id="run-2")

    superseded, outcome = link_runtime_run(
        first, context=retry_ctx, runtime_run_id="run-2", supersede=True
    )
    assert outcome.action == "superseded"
    assert superseded[LINKAGE_TASK_KEY]["runtime_run_id"] == "run-2"
    assert (
        superseded[LINKAGE_TASK_KEY]["idempotency_key"] == retry_ctx.idempotency_key
    )


# --- cross-organization isolation ----------------------------------------


def test_cross_organization_read_is_refused_not_treated_as_absent() -> None:
    ctx_a = _ctx(ORG_A)
    stored, _ = link_runtime_run(_task(), context=ctx_a, runtime_run_id="run-1")

    with pytest.raises(RuntimeRunLinkageError, match="another organization"):
        read_runtime_run_linkage(stored, org_scope_key="tc:org:org-b")


def test_cross_organization_attach_is_refused() -> None:
    ctx_a = _ctx(ORG_A)
    stored, _ = link_runtime_run(_task(), context=ctx_a, runtime_run_id="run-1")

    with pytest.raises(RuntimeRunLinkageError, match="another organization"):
        link_runtime_run(stored, context=_ctx(ORG_B), runtime_run_id="run-9")


def test_linkage_on_a_different_task_is_refused() -> None:
    ctx = _ctx(task_id="e4c1a2b0")
    stored, _ = link_runtime_run(_task(task_id="e4c1a2b0"), context=ctx, runtime_run_id="run-1")

    copied = dict(stored)
    copied["id"] = "deadbeef"
    with pytest.raises(RuntimeRunLinkageError, match="another task"):
        read_runtime_run_linkage(copied, org_scope_key=ctx.org_scope_key)


# --- the single shared org-consistency gate (AG-G2-AUTO-009) -------------


def test_the_gate_returns_the_linkage_for_the_trusted_scope() -> None:
    ctx = _ctx(ORG_A)
    stored, _ = link_runtime_run(_task(), context=ctx, runtime_run_id="run-1")

    linkage = read_linked_runtime_run(stored, context=ctx)
    assert linkage is not None
    assert linkage.runtime_run_id == "run-1"
    assert linkage.org_scope_key == ctx.org_scope_key


def test_the_gate_reads_an_absent_linkage_as_none() -> None:
    assert read_linked_runtime_run(_task(), context=_ctx(ORG_A)) is None


def test_the_gate_refuses_a_linkage_owned_by_another_organization() -> None:
    stored, _ = link_runtime_run(_task(), context=_ctx(ORG_A), runtime_run_id="run-1")

    with pytest.raises(RuntimeRunLinkageError, match="another organization"):
        read_linked_runtime_run(stored, context=_ctx(ORG_B))


def test_the_gate_refuses_a_context_for_another_task() -> None:
    stored, _ = link_runtime_run(_task(), context=_ctx(ORG_A), runtime_run_id="run-1")

    with pytest.raises(RuntimeRunLinkageError, match="does not belong to this task"):
        read_linked_runtime_run(stored, context=_ctx(ORG_A, task_id="deadbeef"))


def test_the_gate_refuses_a_missing_or_untrusted_context() -> None:
    with pytest.raises(RuntimeRunLinkageError, match="trusted task runtime context"):
        read_linked_runtime_run(_task(), context="not-a-context")  # type: ignore[arg-type]
    with pytest.raises(RuntimeRunLinkageError, match="server-loaded task row"):
        read_linked_runtime_run(None, context=_ctx(ORG_A))


# --- malformed / unsafe input --------------------------------------------


def test_absent_linkage_reads_as_none() -> None:
    assert read_runtime_run_linkage(_task(), org_scope_key="tc:org:org-a") is None


@pytest.mark.parametrize(
    "stored",
    [
        "not-json",
        json.dumps({"runtime_run_id": "run-1"}),
        json.dumps({"runtime_run_id": "", "org_scope_key": "tc:org:org-a", "task_id": "e4c1a2b0", "idempotency_key": "k"}),
    ],
)
def test_malformed_linkage_is_refused(stored: Any) -> None:
    with pytest.raises(RuntimeRunLinkageError):
        read_runtime_run_linkage(
            _task(**{LINKAGE_TASK_KEY: stored}), org_scope_key="tc:org:org-a"
        )


def test_link_runtime_run_requires_a_trusted_context_and_run_id() -> None:
    ctx = _ctx()
    with pytest.raises(RuntimeRunLinkageError, match="runtime_run_id is required"):
        link_runtime_run(_task(), context=ctx, runtime_run_id="")
    with pytest.raises(RuntimeRunLinkageError, match="trusted task runtime context"):
        link_runtime_run(_task(), context="not-a-context", runtime_run_id="run-1")  # type: ignore[arg-type]


def test_context_for_another_task_cannot_be_linked() -> None:
    other = _ctx(task_id="deadbeef")
    with pytest.raises(RuntimeRunLinkageError, match="does not belong to this task"):
        link_runtime_run(_task(task_id="e4c1a2b0"), context=other, runtime_run_id="run-1")


# --- lifecycle hardening (AG-G2-AUTO-010) ---------------------------------


@pytest.mark.parametrize(
    "bad_run_id",
    [
        "",
        "   ",
        "run 1",
        "run\n1",
        "run/1",
        "run;drop",
        "r" * 200,
    ],
)
def test_a_malformed_run_id_cannot_be_linked(bad_run_id: str) -> None:
    with pytest.raises(RuntimeRunLinkageError):
        link_runtime_run(_task(), context=_ctx(ORG_A), runtime_run_id=bad_run_id)


def test_a_stored_linkage_with_a_malformed_run_id_is_refused() -> None:
    stored = _task(
        **{
            LINKAGE_TASK_KEY: {
                "runtime_run_id": "run 1;--",
                "org_scope_key": "tc:org:org-a",
                "task_id": "e4c1a2b0",
                "idempotency_key": "k",
            }
        }
    )
    with pytest.raises(RuntimeRunLinkageError, match="malformed"):
        read_linked_runtime_run(stored, context=_ctx(ORG_A))
