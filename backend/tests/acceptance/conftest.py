"""Fixtures for opt-in AgentScope 2 acceptance tests.

The fixtures are intentionally contract-light: they provide IDs and an
isolated evidence location, but do not stand in for PostgreSQL, AgentScope, or
a device client before those interfaces are fixed by stage 0 / stage 1.5.
"""

from pathlib import Path

import pytest

from .test_data import AcceptanceFixture, make_acceptance_fixture


@pytest.fixture
def acceptance_fixture(tmp_path: Path) -> AcceptanceFixture:
    """Provide deterministic per-test IDs and a temporary evidence directory."""

    return make_acceptance_fixture(evidence_root=tmp_path / "acceptance")
