"""Cancel an already-linked Runtime run through the Runtime public boundary.

AG-G2-AUTO-005-B02.

Cancel is the one place where the Task Center must reach the Runtime while
tearing state down, so the rules here are deliberately asymmetric with the
opt-in path:

- the task's existing ``execution_authorized`` flag is **not** a precondition:
  cancelling is what revokes it, so requiring it would make cancellation
  impossible exactly when it is needed;
- nothing writes to the Runtime's own tables. Cancellation goes through the
  existing public contract call ``request_cancel(run_id)`` and nothing else;
- a task without a linkage keeps the untouched legacy cancel path;
- cancelling twice is a no-op, decided from the history already on the task, so
  the Runtime is not asked a second time;
- the recorded terminal state is ``cancelled``, and the projection refuses to
  move a terminal task, so an old executor cannot later overwrite it with
  ``completed``;
- the Runtime's answer is read rather than assumed. Its cancel call answers with
  the run's own state, so a state other than ``cancelled`` means the request was
  **refused** — typically because the run had already settled. A refusal is never
  recorded as a cancellation: the run's actual state is recorded instead, so the
  task stays consistent and explainable instead of claiming a cancel that did not
  happen (AG-G2-AUTO-023);
- no approval state is invented. A refusal is a run status record, not a new
  approval or a new task status, and nothing here writes one.

Cancellation races (AG-G2-AUTO-024)
-----------------------------------
Cancel and completion are two writers racing over one task, and the order they
arrive in decides what is true:

- **cancel first** — a cancellation is recorded, so a completion or failure the
  Runtime reports afterwards is refused by the projection and never overwrites
  the cancelled outcome;
- **settled first** — the Runtime refuses the cancellation because the run had
  already ended. The Task must not claim it cancelled something that was over, so
  its status is restored to the outcome the Runtime actually reports, using the
  Task Center's *existing* status vocabulary through the projection's own mapping.
  A run the Runtime still reports as running is left alone: there the user's
  cancellation is simply still in force;
- **repeat** — cancelling twice is decided from the history already recorded, so
  the Runtime is never asked a second time and the state cannot drift.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from app.gateway.task_runtime_context import TaskRuntimeContext
from app.gateway.task_runtime_linkage import RuntimeRunLinkageError, read_linked_runtime_run
from app.gateway.task_runtime_projection import (
    HISTORY_FIELD,
    build_runtime_history_record,
    task_status_for_runtime_status,
)

__all__ = [
    "RuntimeCancelOutcome",
    "RuntimeCancelError",
    "RuntimeCancelContract",
    "cancel_linked_runtime_run",
]

CancelAction = Literal[
    "runtime_cancelled",
    "legacy_not_linked",
    "already_cancelled",
    "runtime_unavailable",
    "runtime_refused",
]

# The Runtime's own status vocabulary. A refusal is only recorded when it names a
# state the Task Center already understands, so no unrecognised string is copied
# into the task's history.
_KNOWN_RUN_STATUSES = frozenset(
    {
        "created",
        "queued",
        "planning",
        "waiting_approval",
        "running",
        "executing",
        "completed",
        "failed",
        "cancelled",
        "timed_out",
    }
)

_ACCEPTED_CANCEL_STATUS = "cancelled"

# A run that has already ended. Only these make a cancellation too late to be
# truthful; a run the Runtime still reports as running leaves the user's
# cancellation in force.
_SETTLED_RUN_STATUSES = frozenset({"completed", "failed", "timed_out"})


class RuntimeCancelContract(Protocol):
    """The subset of the Runtime public contract used for cancellation."""

    async def request_cancel(self, run_id: str) -> dict[str, Any]: ...


class RuntimeCancelError(RuntimeError):
    """Raised when a Runtime cancellation must not be attempted for this task."""


@dataclass(frozen=True)
class RuntimeCancelOutcome:
    """What the cancellation attempt did."""

    action: CancelAction
    reason: str
    runtime_run_id: str | None = None
    runtime_response: dict[str, Any] | None = None
    updated_task: dict[str, Any] | None = None

    @property
    def used_runtime(self) -> bool:
        return self.action == "runtime_cancelled"

    @property
    def touched_runtime(self) -> bool:
        """Whether a Runtime call was actually made, accepted or refused."""
        return self.action in {"runtime_cancelled", "runtime_refused"}


_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})

# The TaskStatus the existing local cancel handler writes. It is the one terminal
# status this module may correct, because it is the one it may have caused.
_LOCAL_CANCEL_STATUS = "cancelled"


def _truthful_status_after_refusal(
    task: Mapping[str, Any], *, reported: str
) -> str | None:
    """The status to restore when a cancellation arrived after the run settled.

    Returns ``None`` when nothing should change: the Runtime did not report a
    settled run, its answer has no Task Center equivalent, the task already shows
    that outcome, or the task carries a terminal outcome of its own that is not
    the cancellation which just failed to take effect. The last case keeps the
    existing terminal protection intact: this path corrects a too-late
    cancellation, it does not arbitrate between terminal states.
    """
    if reported not in _SETTLED_RUN_STATUSES:
        return None
    mapped = task_status_for_runtime_status(reported)
    if mapped is None:
        return None
    current = str(task.get("status") or "").strip().lower()
    if current == mapped:
        return None
    if current in _TERMINAL_TASK_STATUSES and current != _LOCAL_CANCEL_STATUS:
        return None
    return mapped


def _history_of(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = task.get(HISTORY_FIELD)
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


def _already_cancelled(task: Mapping[str, Any], *, run_id: str) -> bool:
    for entry in _history_of(task):
        if str(entry.get("runtime_run_id") or "") != run_id:
            continue
        if str(entry.get("runtime_status") or "") == _LOCAL_CANCEL_STATUS:
            return True
    return False


def _settled_outcome_recorded(
    task: Mapping[str, Any], *, run_id: str, status: str
) -> bool:
    """Whether this run's most recent record already states ``status``.

    A refusal is a fact about a run, so stating it twice adds nothing: a repeated
    cancellation of an already-settled run must not grow the history. Only the
    latest record for the run is compared, so a run that genuinely moved on is
    still recorded.
    """
    for entry in reversed(_history_of(task)):
        if str(entry.get("runtime_run_id") or "") != run_id:
            continue
        return str(entry.get("runtime_status") or "") == status
    return False


def _refusal_reason(reported: str) -> str:
    if reported in _KNOWN_RUN_STATUSES:
        return f"runtime_reported_{reported}"
    return "runtime_reported_unknown_status"


async def cancel_linked_runtime_run(
    task: Mapping[str, Any] | None,
    *,
    context: TaskRuntimeContext,
    contract: RuntimeCancelContract | None,
) -> RuntimeCancelOutcome:
    """Cancel the Runtime run linked to ``task``, if there is one.

    Never raises for a task that is simply not linked — that is the legacy path.
    Raises :class:`RuntimeCancelError` when the linkage is foreign, belongs to
    another organization, or the cancellation itself fails, so the caller can log
    it instead of silently leaving a run alive.
    """
    if not isinstance(task, Mapping):
        raise RuntimeCancelError("no server-loaded task row supplied")
    if not isinstance(context, TaskRuntimeContext):
        raise RuntimeCancelError("no trusted task runtime context supplied")

    task_id = str(task.get("id") or "").strip()
    if not task_id:
        raise RuntimeCancelError("task row has no id")

    # The shared org-consistency gate: a foreign organization's linkage is
    # refused here, so this path can never cancel another org's run.
    try:
        linkage = read_linked_runtime_run(task, context=context)
    except RuntimeRunLinkageError as exc:
        raise RuntimeCancelError(str(exc)) from exc

    if linkage is None:
        return RuntimeCancelOutcome("legacy_not_linked", "no_runtime_linkage")

    run_id = linkage.runtime_run_id

    if _already_cancelled(task, run_id=run_id):
        return RuntimeCancelOutcome("already_cancelled", "cancelled_already_recorded", run_id)

    if contract is None:
        return RuntimeCancelOutcome("runtime_unavailable", "runtime_contract_unavailable", run_id)

    # The only way this module touches the Runtime: its existing public call.
    response = await contract.request_cancel(run_id)
    if not isinstance(response, Mapping):
        response = {"raw": response}

    reported = str(response.get("status") or "").strip().lower()

    if reported and reported != _ACCEPTED_CANCEL_STATUS:
        # The Runtime's cancel call answers with the run's state. A state other
        # than "cancelled" means it did not cancel the run -- typically because
        # the run had already settled. Recording a cancellation here would make
        # the task claim something that did not happen, so record what the
        # Runtime actually says instead and leave the local cancellation the user
        # asked for exactly as it is.
        refused = dict(task)
        changed = False

        if reported in _KNOWN_RUN_STATUSES and not _settled_outcome_recorded(
            task, run_id=run_id, status=reported
        ):
            history_list = _history_of(task)
            history_list.append(
                build_runtime_history_record(
                    runtime_status=reported,
                    run_id=run_id,
                    org_scope_key=context.org_scope_key,
                    reason="runtime declined the cancellation request",
                )
            )
            refused[HISTORY_FIELD] = history_list
            changed = True

        # A cancellation that arrived after the run settled cannot be honoured,
        # so the task must not display "cancelled" for it. Restore the outcome the
        # Runtime actually reports, in the Task Center's existing vocabulary. An
        # already-terminal status other than the just-requested cancellation is
        # never overwritten, so the terminal protection still holds.
        restored = _truthful_status_after_refusal(task, reported=reported)
        if restored is not None:
            refused["status"] = restored
            changed = True

        if not changed:
            # Nothing new to state: either the run's outcome is already recorded,
            # or the state is not one this module may copy into the task.
            return RuntimeCancelOutcome(
                "runtime_refused",
                _refusal_reason(reported),
                run_id,
                dict(response),
            )
        return RuntimeCancelOutcome(
            "runtime_refused",
            _refusal_reason(reported),
            run_id,
            dict(response),
            refused,
        )

    # Record the request in the shared runtime history vocabulary without
    # touching the task status: the existing cancel handler already owns that,
    # and the projection must stay free to refuse terminal regressions.
    updated_task = dict(task)
    history = updated_task.get(HISTORY_FIELD)
    history_list = list(history) if isinstance(history, list) else []
    history_list.append(
        build_runtime_history_record(
            runtime_status="cancelled",
            run_id=run_id,
            org_scope_key=context.org_scope_key,
            reason="cancellation requested through the runtime public boundary",
        )
    )
    updated_task[HISTORY_FIELD] = history_list

    return RuntimeCancelOutcome(
        "runtime_cancelled",
        "request_cancel_accepted",
        run_id,
        dict(response),
        updated_task,
    )
