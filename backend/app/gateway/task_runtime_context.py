"""Trusted Task Center → Runtime request context adapter.

AG-G2-AUTO-002-B01: the minimal boundary between the existing Task Center /
unattended main-task pipeline and the existing QAgent Runtime public contract
(``RuntimeContract.create_run(*, org_id, task_id, input_payload, idempotency_key)``).

What it does
------------
Turns a server-loaded task row plus a server-resolved authz context into the
plain values a Runtime run needs, and refuses to do so when the trusted
preconditions are not satisfied.

Hard rules enforced here
------------------------
1. Identity and organization scope come **only** from the server-resolved
   ``AuthzContext`` (``evoflow.authz.context.resolve_request_authz``).
   Organization- or identity-bearing keys carried on the task mapping are never
   read, so a client cannot pick an org, a subject or a Runtime identity by
   writing task fields.
2. ``runtime_task_id`` and ``idempotency_key`` are deterministic for a given
   ``(org scope, task id, attempt)`` and are partitioned per organization, so
   two organizations can never share a Runtime identity or an idempotency
   space.
3. Fail closed: no trusted subject, no server-resolved organization, no task id,
   or no existing execution authorization means refusal — never a silent
   fallback and never a guessed default.
4. Nothing AgentScope-specific and no Runtime internal crosses this boundary;
   every produced value is a JSON primitive/container. The module depends on
   the standard library only, so it cannot drag the authz/persistence stack
   into an import cycle or into unrelated call sites.

Deliberately **not** implemented here (later cards): execution routing, provider
calls, task state transitions, approval granting, and persistence of the run
linkage.

The organization boundary used here is the Task Center one (``AuthzContext``).
Real request paths always carry one: ``build_authz_context`` fills an absent
``org_id`` with ``evoflow.authz.types.DEFAULT_ORG_ID`` server-side. This module
refuses a context that has no organization at all rather than re-deriving that
default itself, so the boundary can never drift from the identity domain.

``org_id`` is carried on the produced context so the call site that actually
creates the run can pass it to the Runtime explicitly (AG-G2-AUTO-008). The
mapping itself never chooses a Runtime identity: it only forwards the trusted
Task Center organization unchanged, and refuses when there is none. The Runtime
call site binds it; the Runtime's own default is never used as a substitute.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "TaskRuntimeContext",
    "TaskRuntimeContextError",
    "assert_json_serializable",
    "build_task_runtime_context",
    "idempotency_key_for",
]

# --- Keys that a client must never be able to influence -------------------
# Any of these appearing on the task mapping is ignored on read and stripped
# from the mapped input payload.
_IDENTITY_BEARING_KEYS = frozenset(
    {
        "org",
        "org_id",
        "orgId",
        "organization",
        "organization_id",
        "organizationId",
        "tenant",
        "tenant_id",
        "tenantId",
        "principal",
        "principal_id",
        "principalId",
        "subject",
        "subject_id",
        "subjectId",
        "user",
        "user_id",
        "userId",
        "runtime_identity",
        "runtime_org_id",
        "approver",
        "approver_id",
        "scope_id",
        "scopeId",
        "is_org_admin",
        "isOrgAdmin",
    }
)

# --- Task fields allowed into the Runtime input payload ------------------
# Derived from the fields the unattended pipeline actually stores on a main
# task row. Everything else stays behind this boundary.
_INPUT_ALLOWED_KEYS = ("name", "description", "prompt", "message", "input", "inputs")

# Digest lengths are kept well inside qagent_runs.task_id (String(64)).
_TASK_ID_DIGEST_LEN = 24
_IDEMPOTENCY_DIGEST_LEN = 32
_ORG_NAMESPACE = "tc"


class TaskRuntimeContextError(RuntimeError):
    """Raised when a trusted Runtime task context cannot be built."""


@dataclass(frozen=True)
class TaskRuntimeContext:
    """JSON-safe, AgentScope-neutral inputs for a Runtime run.

    Attributes
    ----------
    org_id:
        Trusted organization from the server-resolved authz context. Passed to
        the Runtime so a run is attributed to the owning organization instead of
        falling back to the Runtime's own default.
    org_scope_key:
        Trusted organization boundary, namespaced for Task Center origin.
        Part of every derived identity/idempotency value.
    subject_id:
        Trusted subject from the server-resolved authz context.
    principal_type:
        Trusted principal type from the server-resolved authz context.
    scope_id:
        Trusted visibility scope (personal/org) from the authz context.
    task_id:
        Existing Task Center main-task identity (source row key).
    attempt:
        Existing unattended attempt counter, used only for idempotency.
    runtime_task_id:
        Deterministic, org-partitioned task id for the Runtime.
    idempotency_key:
        Deterministic key, unique per ``(org scope, task id, attempt)``.
    input_payload:
        Task input, restricted to the allowed non-identity fields.
    authorized:
        Existing execution-authorization state observed on the server. This is
        a read-only precondition; this module never grants an approval.
    """

    org_id: str
    org_scope_key: str
    subject_id: str
    principal_type: str
    scope_id: str
    task_id: str
    attempt: int
    runtime_task_id: str
    idempotency_key: str
    input_payload: dict[str, Any] = field(default_factory=dict)
    authorized: bool = False

    def to_request_kwargs(self) -> dict[str, Any]:
        """The ``create_run`` keyword arguments this context supports.

        Only the Runtime public contract's own parameter names are returned, so
        nothing leaks into the Runtime call site that the contract does not
        already define.
        """
        return {
            "task_id": self.runtime_task_id,
            "input_payload": dict(self.input_payload),
            "idempotency_key": self.idempotency_key,
        }


def _digest(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def _trusted_org_id(authz: Mapping[str, Any]) -> str:
    """Resolve the organization boundary from the server-side authz context only.

    An absent organization is a refusal, not a default: the mapping must never
    invent an organization boundary, and it must never take one from the client.
    """
    raw = str(authz.get("org_id") or "").strip()
    if not raw:
        raise TaskRuntimeContextError("authz context carries no resolved org_id")
    return raw


def _trusted_principal(authz: Mapping[str, Any]) -> tuple[str, str]:
    principal = authz.get("principal")
    if not isinstance(principal, Mapping):
        raise TaskRuntimeContextError("authz context carries no resolved principal")
    subject_id = str(principal.get("principal_id") or "").strip()
    if not subject_id:
        raise TaskRuntimeContextError("authz principal has no principal_id")
    principal_type = str(principal.get("principal_type") or "").strip() or "internal"
    return subject_id, principal_type


def _trusted_scope_id(authz: Mapping[str, Any]) -> str:
    """Resolve the visibility scope from the server-side authz context only.

    No defaulting: a context that did not resolve a scope is refused rather than
    silently narrowed to (or widened from) a guessed one.
    """
    raw = str(authz.get("scope_id") or "").strip()
    if not raw:
        raise TaskRuntimeContextError("authz context carries no resolved scope_id")
    return raw


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Coerce a task input value to something JSON-serializable, or drop it."""
    if depth > 6:
        return None
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name in _IDENTITY_BEARING_KEYS:
                continue
            coerced = _json_safe(item, depth=depth + 1)
            if coerced is not None:
                out[name] = coerced
        return out
    if isinstance(value, list | tuple):
        items = []
        for item in value:
            coerced = _json_safe(item, depth=depth + 1)
            if coerced is not None:
                items.append(coerced)
        return items
    # Unknown objects never cross the boundary.
    return None


def _task_input_payload(task: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in _INPUT_ALLOWED_KEYS:
        if key in _IDENTITY_BEARING_KEYS:
            continue
        if key not in task:
            continue
        coerced = _json_safe(task.get(key))
        if coerced is None:
            continue
        payload[key] = coerced
    return payload


def _normalized_attempt(raw: Any) -> int:
    """The attempt number as an idempotency input: absent, junk and negatives are 0."""
    try:
        attempt = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        attempt = 0
    return attempt if attempt > 0 else 0


def _attempt_of(task: Mapping[str, Any]) -> int:
    return _normalized_attempt(task.get("unattended_attempts"))


def idempotency_key_for(org_scope_key: str, task_id: str, attempt: Any) -> str:
    """The deterministic trigger key for ``(org scope, task, attempt)``.

    Exposed so a caller that must not build a full context — a scheduler asking
    whether an attempt has *already* been handed to the Runtime — can recompute
    the same key from a persisted linkage plus the task row, with no authz
    resolution and no Runtime call (AG-G2-AUTO-021). The derivation is the one
    :func:`build_task_runtime_context` uses, so the two can never disagree about
    what "the same trigger" means.
    """
    scope = str(org_scope_key or "").strip()
    tid = str(task_id or "").strip()
    if not scope or not tid:
        raise TaskRuntimeContextError(
            "an idempotency key needs an organization scope and a task id"
        )
    digest = _digest(scope, tid, str(_normalized_attempt(attempt)))[:_IDEMPOTENCY_DIGEST_LEN]
    return f"{_ORG_NAMESPACE}:{digest}"


def build_task_runtime_context(
    *,
    task: Mapping[str, Any] | None,
    authz: Mapping[str, Any] | None,
    authorized: bool,
) -> TaskRuntimeContext:
    """Build the trusted Runtime task context for an existing Task Center task.

    Parameters are all server-side values: ``task`` is the stored main-task row,
    ``authz`` is a context produced by ``resolve_request_authz``, and
    ``authorized`` is the existing execution-authorization state. Raises
    :class:`TaskRuntimeContextError` (fail closed) when any trusted
    precondition is missing.
    """
    if not isinstance(task, Mapping):
        raise TaskRuntimeContextError("no server-loaded task row supplied")
    if not isinstance(authz, Mapping):
        raise TaskRuntimeContextError("no server-resolved authz context supplied")

    task_id = str(task.get("id") or "").strip()
    if not task_id:
        raise TaskRuntimeContextError("task row has no id")

    org_id = _trusted_org_id(authz)
    subject_id, principal_type = _trusted_principal(authz)
    scope_id = _trusted_scope_id(authz)
    org_scope_key = f"{_ORG_NAMESPACE}:org:{org_id}"

    if authorized is not True:
        raise TaskRuntimeContextError(
            "task is not authorized for execution; refusing to build a runtime context"
        )

    attempt = _attempt_of(task)
    runtime_task_id = f"{_ORG_NAMESPACE}-{_digest(org_scope_key, task_id)[:_TASK_ID_DIGEST_LEN]}"
    idempotency_key = idempotency_key_for(org_scope_key, task_id, attempt)

    return TaskRuntimeContext(
        org_id=org_id,
        org_scope_key=org_scope_key,
        subject_id=subject_id,
        principal_type=principal_type,
        scope_id=scope_id,
        task_id=task_id,
        attempt=attempt,
        runtime_task_id=runtime_task_id,
        idempotency_key=idempotency_key,
        input_payload=_task_input_payload(task),
        authorized=True,
    )


def assert_json_serializable(context: TaskRuntimeContext) -> str:
    """Test/debug helper: prove the context is a plain JSON document."""
    return json.dumps(context.to_request_kwargs(), sort_keys=True)
