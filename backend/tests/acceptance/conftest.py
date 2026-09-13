"""Fixtures for opt-in AgentScope 2 acceptance tests.

The fixtures are intentionally contract-light: they provide IDs and an
isolated evidence location, but do not stand in for PostgreSQL, AgentScope, or
a device client before those interfaces are fixed by stage 0 / stage 1.5.

Real-PostgreSQL acceptance tests additionally need ``app`` to resolve to the
backend package rather than the harness copy. See the ``app`` shadow guard
below.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# --------------------------------------------------------------------------
# ``app`` package shadow guard
# --------------------------------------------------------------------------
# ``backend/packages/harness/app/`` is a second, unrelated package that also
# claims the top-level name ``app`` (it holds only ``gateway``). The shared
# ``backend/tests/conftest.py`` inserts the harness directory at ``sys.path[0]``
# so the harness copy wins and ``import app.qagent_runtime`` fails with a
# misleading ``ModuleNotFoundError``. The acceptance tests need the backend
# package, so make the backend root authoritative here.
#
# This only reorders ``sys.path`` for the acceptance suite; it does not modify
# the shared conftest or either package.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _ensure_backend_app_wins() -> None:
    backend_root = str(_BACKEND_ROOT)
    # Drop every existing entry that points at the backend root, then put it
    # first so the backend ``app`` package is found before the harness copy.
    sys.path[:] = [entry for entry in sys.path if str(Path(entry or ".").resolve()) != backend_root]
    sys.path.insert(0, backend_root)


_ensure_backend_app_wins()

# Re-export the real-PostgreSQL harness fixtures so scenario modules can request
# them without importing the support module directly.
from .pg_support import clean_runtime, runtime_harness  # noqa: E402,F401
from .test_data import AcceptanceFixture, make_acceptance_fixture  # noqa: E402


@pytest.fixture
def acceptance_fixture(tmp_path: Path) -> AcceptanceFixture:
    """Provide deterministic per-test IDs and a temporary evidence directory."""

    return make_acceptance_fixture(evidence_root=tmp_path / "acceptance")
