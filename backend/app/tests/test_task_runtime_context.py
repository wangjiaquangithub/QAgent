"""Protective tests for the trusted Task Center → Runtime context boundary.

Covers AG-G2-AUTO-002-B01 acceptance points:
- the mapping for the same Task is stable;
- different organizations never share identity or idempotency space;
- client-supplied organization/identity fields on a task row are inert.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.gateway.task_runtime_context import (
    TaskRuntimeContext,
    TaskRuntimeContextError,
    assert_json_serializable,
    build_task_runtime_context,
)

ORG_A = "org-a"
ORG_B = "org-b"


def _authz(org_id: str, principal_id: str = "webui:1", scope_id: str | None = None) -> dict[str, Any]:
    """A context shaped like the one ``resolve_request_authz`` produces."""
    return {
        "org_id": org_id,
        "principal": {
            "principal_id": principal_id,
            "org_id": org_id,
            "principal_type": "internal",
        },
        "scope_id": scope_id if scope_id is not None else f"personal:{principal_id}",
        "is_org_admin": False,
    }


def _task(**extra: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": "e4c1a2b0",
        "name": "每周经营简报",
        "description": "汇总区域销量并给出异常清单",
        "run_mode": "unattended",
        "status": "inbox",
        "unattended_attempts": 0,
    }
    task.update(extra)
    return task


# --- stability ------------------------------------------------------------


def test_mapping_is_stable_for_the_same_task() -> None:
    first = build_task_runtime_context(task=_task(), authz=_authz(ORG_A), authorized=True)
    second = build_task_runtime_context(task=_task(), authz=_authz(ORG_A), authorized=True)
    assert first == second
    assert first.runtime_task_id == second.runtime_task_id
    assert first.idempotency_key == second.idempotency_key


def test_attempt_changes_idempotency_but_not_runtime_task_identity() -> None:
    base = build_task_runtime_context(task=_task(), authz=_authz(ORG_A), authorized=True)
    retried = build_task_runtime_context(
        task=_task(unattended_attempts=1), authz=_authz(ORG_A), authorized=True
    )
    assert retried.runtime_task_id == base.runtime_task_id
    assert retried.idempotency_key != base.idempotency_key
    assert retried.attempt == 1

    # Re-observing the same attempt must converge on the identical key.
    assert (
        build_task_runtime_context(
            task=_task(unattended_attempts=1), authz=_authz(ORG_A), authorized=True
        ).idempotency_key
        == retried.idempotency_key
    )


# --- organization isolation ----------------------------------------------


def test_different_orgs_do_not_share_identity_or_idempotency_space() -> None:
    a = build_task_runtime_context(task=_task(), authz=_authz(ORG_A), authorized=True)
    b = build_task_runtime_context(task=_task(), authz=_authz(ORG_B), authorized=True)

    assert a.org_scope_key != b.org_scope_key
    assert a.runtime_task_id != b.runtime_task_id
    assert a.idempotency_key != b.idempotency_key

    # The same org id always collapses to the same partition.
    a2 = build_task_runtime_context(task=_task(), authz=_authz(ORG_A), authorized=True)
    assert a2.org_scope_key == a.org_scope_key
    assert a2.runtime_task_id == a.runtime_task_id
    assert a2.idempotency_key == a.idempotency_key


def test_refuses_when_organization_is_not_server_resolved() -> None:
    # A task row claiming an org must not be able to supply the boundary.
    with pytest.raises(TaskRuntimeContextError, match="no resolved org_id"):
        build_task_runtime_context(
            task=_task(org_id=ORG_B, organizationId=ORG_B),
            authz={"principal": {"principal_id": "webui:1"}},
            authorized=True,
        )


# --- client forgery must be inert ----------------------------------------


@pytest.mark.parametrize(
    "forged",
    [
        {"org_id": ORG_B},
        {"orgId": ORG_B},
        {"organization_id": ORG_B},
        {"organizationId": ORG_B},
        {"tenant_id": ORG_B},
        {"principal_id": "attacker"},
        {"subject_id": "attacker"},
        {"runtime_org_id": ORG_B},
        {"approver_id": "attacker"},
        {"scope_id": f"org:{ORG_B}"},
        {"is_org_admin": True},
    ],
)
def test_client_supplied_organization_or_identity_fields_are_inert(forged: dict[str, Any]) -> None:
    clean = build_task_runtime_context(task=_task(), authz=_authz(ORG_A), authorized=True)
    dirty = build_task_runtime_context(task=_task(**forged), authz=_authz(ORG_A), authorized=True)

    assert dirty == clean
    assert dirty.org_id == clean.org_id == ORG_A
    assert dirty.org_scope_key == clean.org_scope_key
    assert dirty.runtime_task_id == clean.runtime_task_id
    assert dirty.idempotency_key == clean.idempotency_key

    serialized = assert_json_serializable(dirty)
    for key in forged:
        assert key not in dirty.input_payload
        assert f'"{key}"' not in serialized


def test_forged_org_cannot_collide_with_the_real_owning_org() -> None:
    # Even when a task row claims another org, the derived identity stays inside
    # the authenticating org's partition.
    forged = build_task_runtime_context(
        task=_task(org_id=ORG_B, organizationId=ORG_B), authz=_authz(ORG_A), authorized=True
    )
    real_b = build_task_runtime_context(task=_task(), authz=_authz(ORG_B), authorized=True)
    assert forged.org_id == ORG_A
    assert forged.runtime_task_id != real_b.runtime_task_id
    assert forged.idempotency_key != real_b.idempotency_key


def test_context_carries_the_trusted_org_id_verbatim() -> None:
    ctx = build_task_runtime_context(task=_task(), authz=_authz(ORG_A), authorized=True)
    assert ctx.org_id == ORG_A

    other = build_task_runtime_context(task=_task(), authz=_authz(ORG_B), authorized=True)
    assert other.org_id == ORG_B
    assert other.org_id != ctx.org_id


def test_identity_bearing_keys_are_dropped_from_nested_input() -> None:
    ctx = build_task_runtime_context(
        task=_task(input={"org_id": ORG_B, "query": "销量", "tenant_id": ORG_B}),
        authz=_authz(ORG_A),
        authorized=True,
    )
    assert ctx.input_payload["input"] == {"query": "销量"}


# --- fail-closed ----------------------------------------------------------


def test_refuses_when_visibility_scope_is_not_server_resolved() -> None:
    with pytest.raises(TaskRuntimeContextError, match="no resolved scope_id"):
        build_task_runtime_context(
            task=_task(),
            authz={"org_id": ORG_A, "principal": {"principal_id": "webui:1"}},
            authorized=True,
        )


def test_refuses_when_execution_is_not_authorized() -> None:
    with pytest.raises(TaskRuntimeContextError, match="not authorized"):
        build_task_runtime_context(task=_task(), authz=_authz(ORG_A), authorized=False)


def test_refuses_without_server_task_row() -> None:
    with pytest.raises(TaskRuntimeContextError, match="server-loaded task row"):
        build_task_runtime_context(task=None, authz=_authz(ORG_A), authorized=True)


def test_refuses_without_authz_context() -> None:
    with pytest.raises(TaskRuntimeContextError, match="authz context"):
        build_task_runtime_context(task=_task(), authz=None, authorized=True)


def test_refuses_when_task_has_no_id() -> None:
    with pytest.raises(TaskRuntimeContextError, match="no id"):
        build_task_runtime_context(
            task={"name": "无 id"}, authz=_authz(ORG_A), authorized=True
        )


def test_refuses_when_principal_is_missing_or_untrusted() -> None:
    with pytest.raises(TaskRuntimeContextError, match="resolved principal"):
        build_task_runtime_context(task=_task(), authz={"org_id": ORG_A}, authorized=True)

    with pytest.raises(TaskRuntimeContextError, match="principal_id"):
        build_task_runtime_context(
            task=_task(), authz={"org_id": ORG_A, "principal": {}}, authorized=True
        )


# --- boundary hygiene -----------------------------------------------------


def test_context_is_json_only_and_free_of_agentscope_objects() -> None:
    ctx = build_task_runtime_context(task=_task(), authz=_authz(ORG_A), authorized=True)
    request = ctx.to_request_kwargs()

    # Exactly the Runtime public contract's own parameters — nothing more.
    assert set(request) == {"task_id", "input_payload", "idempotency_key"}
    json.dumps(request, sort_keys=True)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        else:
            assert type(value).__module__.split(".")[0] not in {"agentscope", "app"}

    walk(request)
    walk({k: v for k, v in vars(ctx).items() if k != "authorized"})


def test_task_identity_is_bounded_for_the_runtime_task_id_column() -> None:
    ctx = build_task_runtime_context(
        task=_task(id="t" * 200), authz=_authz(ORG_A), authorized=True
    )
    assert len(ctx.runtime_task_id) <= 64


def test_unknown_task_objects_do_not_cross_the_boundary() -> None:
    class Opaque:
        def __repr__(self) -> str:  # pragma: no cover - defensive
            return "<opaque>"

    ctx = build_task_runtime_context(
        task=_task(name="ok", description=Opaque(), message=Opaque()),
        authz=_authz(ORG_A),
        authorized=True,
    )
    assert ctx.input_payload == {"name": "ok"}
    assert isinstance(ctx, TaskRuntimeContext)
