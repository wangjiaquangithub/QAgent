"""Chat-domain identity mapping onto the QAgent Runtime contract.

This module is the *only* place where Chat identifiers are translated into what
:class:`app.qagent_runtime.service.RuntimeService` requires. It exists so the
existing chat entrypoints can hand a request to the Runtime without teaching the
Runtime anything about Chat, and without Chat re-implementing identity rules.

Design constraints (see ``docs/plan/agentscope-2-g2-chat-entry-map.md``):

* ``org_id`` is derived **server-side** from the verified request identity.
  A client-supplied organization value is never trusted, so a request cannot
  claim membership of another organization.
* The mapping is a pure function of (request identity, session key, message id).
  It performs no writes, holds no state, and creates no tables — adding new
  Runtime columns/schema is explicitly out of scope for this layer.
* Idempotency is delegated to the Runtime: ``(org_id, idempotency_key)`` is
  unique in ``qagent_runs``, so a stable key makes repeated requests reuse the
  same run instead of starting uncontrolled duplicate executions.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

# Chat session keys look like ``agent:main:<slug>``; they are path-ish and may
# contain characters that are awkward inside a run/task identifier.
_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9._:-]+")
_MAX_TASK_ID_LEN = 96
_MAX_IDEMPOTENCY_KEY_LEN = 255  # qagent_runs.idempotency_key is String(255)


def _sanitize_id(raw: str, *, max_len: int) -> str:
    cleaned = _UNSAFE_ID_CHARS.sub("-", str(raw or "").strip()).strip("-")
    if len(cleaned) <= max_len:
        return cleaned
    # Keep the value stable and collision-resistant when truncating.
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:12]
    return f"{cleaned[: max_len - 13]}-{digest}"


def runtime_org_id_for_identity(identity: dict[str, Any] | None) -> str:
    """Derive the Runtime ``org_id`` from a *verified* request identity.

    Mirrors ``app.qagent_runtime.auth.get_runtime_principal``:
    ``identity:{identity_type}:{identity_id}``. When the request is
    unauthenticated, ``local`` is used so behaviour matches the Runtime's own
    default rather than silently borrowing another organization.

    The value is computed from server-side identity only — there is deliberately
    no parameter through which a caller could supply an organization.
    """
    ident = identity if isinstance(identity, dict) else {}
    identity_type = str(ident.get("identity_type") or "").strip()
    identity_id = str(ident.get("identity_id") or "").strip()
    if identity_type and identity_id:
        return f"identity:{identity_type}:{identity_id}"
    return "local"


@dataclass(frozen=True)
class ChatRunIdentity:
    """Runtime-facing identifiers derived from one Chat request."""

    org_id: str
    subject_id: str
    session_key: str
    message_id: str | None
    task_id: str
    idempotency_key: str
    input_payload: dict[str, Any] = field(default_factory=dict)

    def as_create_run_kwargs(self) -> dict[str, Any]:
        """Arguments accepted by ``RuntimeService.create_run``."""
        return {
            "org_id": self.org_id,
            "task_id": self.task_id,
            "input_payload": self.input_payload,
            "idempotency_key": self.idempotency_key,
        }


def build_chat_run_identity(
    *,
    identity: dict[str, Any] | None,
    session_key: str,
    message_id: str | None = None,
    run_id: str | None = None,
    principal_id: str | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> ChatRunIdentity:
    """Map a Chat request onto the Runtime identity contract.

    Args:
        identity: Verified request identity (``identity_type`` / ``identity_id``),
            as returned by the Gateway bearer verification. Never client input.
        session_key: Chat session key the request belongs to.
        message_id: Client message id, when supplied. Participates in the
            idempotency key so a retried send maps to the same Runtime run.
        run_id: Existing chat run id, when the session already has one.
        principal_id: Chat-domain principal id, recorded for traceability.
        extra_payload: Additional non-identity data for the Runtime payload.
    """
    sk = str(session_key or "").strip()
    if not sk:
        raise ValueError("session_key required to map a chat runtime identity")

    org_id = runtime_org_id_for_identity(identity)
    ident = identity if isinstance(identity, dict) else {}
    subject_id = str(ident.get("identity_id") or "").strip() or str(principal_id or "").strip()

    task_id = _sanitize_id(f"chat:{sk}", max_len=_MAX_TASK_ID_LEN)

    # Idempotency is scoped by org_id in qagent_runs, so a stable per-(session,
    # message) key is enough to prevent uncontrolled duplicate executions while
    # still allowing genuinely new messages to create new runs.
    basis = run_id or message_id or ""
    raw_key = f"chat:{sk}:{basis}" if basis else f"chat:{sk}"
    idempotency_key = _sanitize_id(raw_key, max_len=_MAX_IDEMPOTENCY_KEY_LEN)

    payload: dict[str, Any] = {
        "source": "chat",
        "session_key": sk,
    }
    if message_id:
        payload["message_id"] = str(message_id)
    if run_id:
        payload["chat_run_id"] = str(run_id)
    if principal_id:
        payload["principal_id"] = str(principal_id)
    if extra_payload:
        payload.update(extra_payload)

    return ChatRunIdentity(
        org_id=org_id,
        subject_id=subject_id,
        session_key=sk,
        message_id=str(message_id).strip() if message_id else None,
        task_id=task_id,
        idempotency_key=idempotency_key,
        input_payload=payload,
    )
