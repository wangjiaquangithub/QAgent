"""Pytest coverage for the offline AgentScope 2.0.8 compatibility contract."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# `backend/scripts/` is a tool directory rather than a Python package. Load the
# shared offline fake by file path so pytest collection is stable from CI and
# from IDEs that do not add `backend/` to `sys.path`.
_PROBE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "agentscope_compat_probe.py"
_PROBE_SPEC = importlib.util.spec_from_file_location("agentscope_compat_probe", _PROBE_PATH)
if _PROBE_SPEC is None or _PROBE_SPEC.loader is None:  # pragma: no cover - import setup failure
    raise ImportError(f"Unable to load AgentScope compatibility probe: {_PROBE_PATH}")
_PROBE_MODULE = importlib.util.module_from_spec(_PROBE_SPEC)
sys.modules[_PROBE_SPEC.name] = _PROBE_MODULE
_PROBE_SPEC.loader.exec_module(_PROBE_MODULE)

EXPECTED_VERSION = _PROBE_MODULE.EXPECTED_VERSION
FakeChatModel = _PROBE_MODULE.FakeChatModel
ProbeResult = _PROBE_MODULE.ProbeResult
make_agent = _PROBE_MODULE.make_agent
text_from_msg = _PROBE_MODULE.text_from_msg
user_msg = _PROBE_MODULE.user_msg

__all__ = [
    "EXPECTED_VERSION",
    "FakeChatModel",
    "ProbeResult",
    "make_agent",
    "text_from_msg",
    "user_msg",
]


@pytest.mark.asyncio
async def test_agentscope_exact_version_and_basic_single_turn() -> None:
    import agentscope
    from agentscope.message import Msg

    assert agentscope.__version__ == EXPECTED_VERSION == "2.0.8"

    fake = FakeChatModel("text")
    message = user_msg("hello")
    output = await make_agent(fake.instance).reply(message)

    assert isinstance(message, Msg)
    assert isinstance(output, Msg)
    assert text_from_msg(message) == "hello"
    assert text_from_msg(output) == "fake final answer"
    assert output.finished_reason.value == "completed"
    assert fake.instance.calls == 1


@pytest.mark.asyncio
async def test_structured_schema_returns_structured_output() -> None:
    fake = FakeChatModel("structured")

    output = await make_agent(fake.instance).reply(
        user_msg("return a result"),
        structured_schema=ProbeResult,
    )

    assert output.structured_output == {"answer": "structured", "score": 7}
    assert fake.instance.calls == 1


@pytest.mark.asyncio
async def test_missing_model_is_detected_at_reply_boundary() -> None:
    with pytest.raises(AttributeError, match="formatter"):
        await make_agent(None).reply(user_msg("hello"))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_invalid_structured_json_is_bounded_by_react_config() -> None:
    from agentscope.agent import ReActConfig

    fake = FakeChatModel("invalid-json")
    output = await make_agent(
        fake.instance,
        react_config=ReActConfig(max_iters=1, structured_output_grace_iters=1),
    ).reply(
        user_msg("return JSON"),
        structured_schema=ProbeResult,
    )

    assert output.finished_reason.value == "exceed_max_iters"
    assert output.structured_output is None
    assert fake.instance.calls <= 2


@pytest.mark.asyncio
async def test_react_config_max_iters_is_applied() -> None:
    from agentscope.agent import ReActConfig

    config = ReActConfig(max_iters=1, structured_output_grace_iters=1)
    assert config.max_iters == 1

    fake = FakeChatModel("invalid-json")
    output = await make_agent(fake.instance, react_config=config).reply(
        user_msg("return JSON"),
        structured_schema=ProbeResult,
    )

    assert output.finished_reason.value == "exceed_max_iters"
    assert fake.instance.calls <= 2
