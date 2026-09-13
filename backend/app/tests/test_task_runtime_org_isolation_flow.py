"""Vertical integration: the two-organization boundary around a Runtime run.

AG-G2-AUTO-035.

Two trusted organizations exist in these tests: the one the server resolves for
the local caller, and a second one that is equally trusted but is not the caller.
A Task Center task may only reach, cancel, project or attach the run that belongs
to its own organization, and a client must not be able to choose that organization
by writing fields.

Two properties are asserted separately, because they are different failures:

- the *operation* is refused — nothing is read from the Runtime, nothing is
  cancelled, nothing is projected, nothing is attached;
- the *existence* of the other organization's run is not disclosed — no refusal
  carries its run id or its organization scope, so a refusal cannot be used as a
  probe.
"""

from __future__ import annotations

import json

import pytest
from _runtime_flow_support import (
    HISTORY_FIELD,
    LINKAGE_TASK_KEY,
    RUN_ID,
    FakeRuntime,
    authorize,
    block_outbound_network,
    build_client,
    create_unattended_task,
    frame,
    identity,
    push_frames,
    reset_home,
    run_contract,
    save_task,
    statuses_of,
    stored_task,
    tick,
)

SWITCH = "EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME"
OTHER_ORG = "org-b"
OTHER_RUN_ID = "run-other-org-1"
LOCAL_ORG = "local"


@pytest.fixture()
def client():
    reset_home()
    return build_client()


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch):
    """Runtime opt-in on, and no outbound network: the fake is the only runtime."""
    monkeypatch.setenv(SWITCH, "1")
    block_outbound_network(monkeypatch)


def _linked_task(client, monkeypatch: pytest.MonkeyPatch) -> tuple[str, FakeRuntime]:
    fake = run_contract(FakeRuntime(frames=[frame("run.running", sequence=0)]), monkeypatch)
    task_id = create_unattended_task(client)
    authorize(task_id)
    assert tick(task_id)["action"] == "runtime"
    push_frames(task_id, fake.frames)
    return task_id, fake


def _context_for(task_id: str, org_id: str):
    from app.gateway.task_runtime_context import build_task_runtime_context

    return build_task_runtime_context(
        task=stored_task(task_id), authz=identity(org_id, f"{org_id}-admin"), authorized=True
    )


def _store_foreign_linkage(task_id: str) -> None:
    """Rewrite the stored linkage as the other organization's, as a bad row looks."""
    stored = stored_task(task_id)
    stored[LINKAGE_TASK_KEY] = {
        "runtime_run_id": OTHER_RUN_ID,
        "org_scope_key": f"tc:org:{OTHER_ORG}",
        "task_id": task_id,
        "idempotency_key": "tc:" + "b" * 32,
    }
    save_task(task_id, stored)


# --- reading ----------------------------------------------------------------------


def test_this_organizations_linkage_is_readable(client, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.gateway.task_runtime_linkage import read_linked_runtime_run

    task_id, _ = _linked_task(client, monkeypatch)

    linkage = read_linked_runtime_run(stored_task(task_id), context=_context_for(task_id, LOCAL_ORG))

    assert linkage is not None
    assert linkage.runtime_run_id == RUN_ID


def test_another_organizations_linkage_is_refused(client, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.gateway.task_runtime_linkage import (
        RuntimeRunLinkageError,
        read_linked_runtime_run,
    )

    task_id, _ = _linked_task(client, monkeypatch)

    with pytest.raises(RuntimeRunLinkageError):
        read_linked_runtime_run(
            stored_task(task_id), context=_context_for(task_id, OTHER_ORG)
        )


def test_the_refusal_does_not_disclose_the_run(client, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.gateway.task_runtime_linkage import (
        RuntimeRunLinkageError,
        read_linked_runtime_run,
    )

    task_id, _ = _linked_task(client, monkeypatch)

    with pytest.raises(RuntimeRunLinkageError) as refusal:
        read_linked_runtime_run(
            stored_task(task_id), context=_context_for(task_id, OTHER_ORG)
        )

    message = str(refusal.value)
    assert RUN_ID not in message
    assert LOCAL_ORG not in message


def test_a_foreign_linkage_read_by_this_organization_is_refused(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.gateway.task_runtime_linkage import (
        RuntimeRunLinkageError,
        read_linked_runtime_run,
    )

    task_id, _ = _linked_task(client, monkeypatch)
    _store_foreign_linkage(task_id)

    with pytest.raises(RuntimeRunLinkageError):
        read_linked_runtime_run(stored_task(task_id), context=_context_for(task_id, LOCAL_ORG))


# --- projecting -------------------------------------------------------------------


def test_a_foreign_linkage_is_not_projected(client, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.gateway.task_runtime_projection import (
        RuntimeProjectionError,
        project_runtime_status,
    )

    task_id, _ = _linked_task(client, monkeypatch)
    _store_foreign_linkage(task_id)

    with pytest.raises(RuntimeProjectionError):
        project_runtime_status(
            stored_task(task_id),
            context=_context_for(task_id, LOCAL_ORG),
            runtime_status="completed",
        )


def test_a_frame_naming_another_run_is_refused(client, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.gateway.task_runtime_event_bridge import (
        RuntimeEventBridgeError,
        apply_runtime_event,
    )

    task_id, _ = _linked_task(client, monkeypatch)

    with pytest.raises(RuntimeEventBridgeError):
        apply_runtime_event(
            stored_task(task_id),
            context=_context_for(task_id, LOCAL_ORG),
            event=frame("run.completed", sequence=1, run_id=OTHER_RUN_ID),
        )


def test_the_foreign_run_id_never_reaches_the_task(client, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.gateway.task_runtime_event_bridge import RuntimeEventBridgeError, apply_runtime_event

    task_id, _ = _linked_task(client, monkeypatch)
    before = statuses_of(stored_task(task_id))

    with pytest.raises(RuntimeEventBridgeError):
        apply_runtime_event(
            stored_task(task_id),
            context=_context_for(task_id, LOCAL_ORG),
            event=frame("run.completed", sequence=1, run_id=OTHER_RUN_ID),
        )

    assert statuses_of(stored_task(task_id)) == before


def test_a_foreign_linkage_is_not_shown_through_the_routes(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = _linked_task(client, monkeypatch)
    _store_foreign_linkage(task_id)

    history = client.get(f"/api/tasks/{task_id}/execution-history").json()
    # The records the task already had are its own and stay; nothing new is added
    # for the foreign run, and its identity is nowhere in the response.
    assert all(entry.get("runtime_run_id") == RUN_ID for entry in history["execution_history"])
    rendered = json.dumps(history, ensure_ascii=False)
    assert OTHER_RUN_ID not in rendered
    assert OTHER_ORG not in rendered


# --- cancelling -------------------------------------------------------------------


def test_cancelling_does_not_reach_another_organizations_run(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fake = _linked_task(client, monkeypatch)
    _store_foreign_linkage(task_id)

    response = client.post(f"/api/tasks/{task_id}/cancel")

    assert response.status_code == 200, response.text
    assert OTHER_RUN_ID not in fake.cancel_calls
    assert fake.cancel_calls == []


def test_cancelling_a_foreign_linkage_does_not_reveal_it(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = _linked_task(client, monkeypatch)
    _store_foreign_linkage(task_id)

    response = client.post(f"/api/tasks/{task_id}/cancel")

    assert OTHER_RUN_ID not in response.text
    assert OTHER_ORG not in response.text


# --- attaching --------------------------------------------------------------------


def test_the_runtime_branch_never_adopts_a_foreign_linkage(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: the opt-in refuses rather than re-binding the foreign run."""
    from app.gateway.task_runtime_linkage import RuntimeRunLinkageError

    fake = run_contract(FakeRuntime(frames=[frame("run.running", sequence=0)]), monkeypatch)
    task_id = create_unattended_task(client)
    authorize(task_id)
    _store_foreign_linkage(task_id)

    with pytest.raises(RuntimeRunLinkageError):
        tick(task_id)

    assert fake.create_calls == []
    assert stored_task(task_id)[LINKAGE_TASK_KEY]["runtime_run_id"] == OTHER_RUN_ID


def test_the_queue_tick_survives_the_refusal(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Task Center does not fall over because one task's link is foreign."""
    fake = run_contract(FakeRuntime(frames=[frame("run.running", sequence=0)]), monkeypatch)
    task_id = create_unattended_task(client)
    authorize(task_id)
    _store_foreign_linkage(task_id)

    response = client.post("/api/tasks/queue/tick")

    assert response.status_code == 200, response.text
    assert OTHER_RUN_ID not in response.text
    assert fake.create_calls == []


# --- a client cannot choose the organization --------------------------------------


def test_a_forged_organization_in_the_request_is_ignored(client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = run_contract(FakeRuntime(frames=[frame("run.running", sequence=0)]), monkeypatch)

    response = client.post(
        "/api/tasks",
        json={
            "name": "越权尝试",
            "description": "d",
            "run_mode": "unattended",
            "org_id": OTHER_ORG,
            "tenant_id": OTHER_ORG,
            "organizationId": OTHER_ORG,
        },
    )
    assert response.status_code == 200, response.text
    task_id = str(response.json()["id"])

    from app.gateway.task_runtime_optin import resolve_server_task_runtime_identity

    resolved = resolve_server_task_runtime_identity(task_id)
    assert resolved is not None
    assert resolved["org_id"] != OTHER_ORG

    authorize(task_id)
    tick(task_id)

    assert fake.create_calls[0]["org_id"] != OTHER_ORG


def test_a_forged_organization_on_the_row_is_ignored(client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = run_contract(FakeRuntime(frames=[frame("run.running", sequence=0)]), monkeypatch)
    task_id = create_unattended_task(client)
    authorize(task_id)
    stored = stored_task(task_id)
    stored["org_id"] = OTHER_ORG
    stored["tenant_id"] = OTHER_ORG
    save_task(task_id, stored)

    from app.gateway.task_runtime_optin import resolve_server_task_runtime_identity

    resolved = resolve_server_task_runtime_identity(task_id)
    assert resolved is not None
    assert resolved["org_id"] != OTHER_ORG

    tick(task_id)

    assert fake.create_calls[0]["org_id"] != OTHER_ORG


def test_the_boundary_decision_names_the_reason_without_ids(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.gateway import task_runtime_degrade as degrade

    task_id, _ = _linked_task(client, monkeypatch)
    _store_foreign_linkage(task_id)

    health = degrade.assess_runtime_link(
        stored_task(task_id), context=_context_for(task_id, LOCAL_ORG)
    )

    assert health.state == "unusable"
    assert health.reason == "linkage_foreign_org"
    assert health.displayable_run_id is None
    assert OTHER_RUN_ID not in health.reason
    assert OTHER_ORG not in health.reason


def test_the_task_keeps_its_own_history_through_the_refusal(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _ = _linked_task(client, monkeypatch)
    before = statuses_of(stored_task(task_id))
    _store_foreign_linkage(task_id)

    assert statuses_of(stored_task(task_id)) == before
    assert HISTORY_FIELD in stored_task(task_id)
