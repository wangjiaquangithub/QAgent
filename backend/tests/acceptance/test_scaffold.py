"""Contract-neutral checks for the AgentScope 2 acceptance scaffolding."""

import pytest

from .test_data import make_acceptance_fixture


@pytest.mark.agentscope_acceptance
def test_acceptance_fixture_is_reproducible(acceptance_fixture):
    """The same seed produces stable identities for repeatable evidence."""

    repeated = make_acceptance_fixture(evidence_root=acceptance_fixture.evidence_dir.parent)

    assert repeated == acceptance_fixture
    assert not acceptance_fixture.evidence_dir.exists()


@pytest.mark.agentscope_acceptance
def test_acceptance_fixture_seeds_do_not_share_identities(tmp_path):
    """Parallel scenarios must not accidentally reuse business identities."""

    first = make_acceptance_fixture(seed="scenario-a", evidence_root=tmp_path)
    second = make_acceptance_fixture(seed="scenario-b", evidence_root=tmp_path)

    assert first.org_id != second.org_id
    assert first.run_id != second.run_id
    assert first.command_id != second.command_id
    assert first.evidence_dir != second.evidence_dir
