"""Deterministic, interface-neutral data for AgentScope 2 acceptance tests.

This module deliberately models identifiers and evidence metadata only.  It does
not create database rows, call runtime APIs, or emulate a device protocol.  Once
stage 0 and the stage 1.5 vertical slice settle those contracts, scenario tests
can adapt these values into real fixtures without changing the acceptance IDs.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


@dataclass(frozen=True)
class AcceptanceFixture:
    """Stable identifiers shared by one isolated acceptance run."""

    org_id: str
    principal_id: str
    run_id: str
    command_id: str
    idempotency_key: str
    device_id: str
    asset_id: str
    asset_version_id: str
    migration_batch_id: str
    evidence_dir: Path


def _short_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:16]


def make_acceptance_fixture(*, seed: str = "agentscope-2-acceptance", evidence_root: Path | None = None) -> AcceptanceFixture:
    """Return deterministic IDs without assuming future persistence schemas.

    ``seed`` lets parallel runs remain isolated while keeping failures
    reproducible.  The helper only creates the path value; callers decide when
    and whether evidence should be written.
    """

    token = _short_hash(seed)
    root = evidence_root or Path("artifacts/acceptance")
    evidence_dir = root / token
    return AcceptanceFixture(
        org_id=f"org-acceptance-{token}",
        principal_id=f"principal-acceptance-{token}",
        run_id=f"run-acceptance-{token}",
        command_id=f"command-acceptance-{token}",
        idempotency_key=f"idem-acceptance-{token}",
        device_id=f"device-acceptance-{token}",
        asset_id=f"asset-acceptance-{token}",
        asset_version_id=f"asset-version-acceptance-{token}",
        migration_batch_id=f"migration-acceptance-{token}",
        evidence_dir=evidence_dir,
    )
