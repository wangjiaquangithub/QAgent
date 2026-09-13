"""Authentication and organization scoping for the QAgent Runtime API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Depends

from app.gateway.auth import verify_bearer_token


@dataclass(frozen=True)
class RuntimePrincipal:
    """Authenticated subject and its durable Runtime organization scope."""

    org_id: str
    subject_id: str
    identity_type: str


def get_runtime_principal(
    token_data: dict[str, Any] = Depends(verify_bearer_token),
) -> RuntimePrincipal:
    """Build the Runtime scope from the already verified bearer identity.

    The current gateway token record does not expose a separate organization
    identifier. Until the shared identity model provides one, the verified
    identity itself is the stable organization boundary. No client-provided
    organization value is accepted.
    """
    identity_type = str(token_data["identity_type"])
    identity_id = str(token_data["identity_id"])
    return RuntimePrincipal(
        org_id=f"identity:{identity_type}:{identity_id}",
        subject_id=identity_id,
        identity_type=identity_type,
    )
