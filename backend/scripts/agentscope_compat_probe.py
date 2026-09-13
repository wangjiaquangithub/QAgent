"""Offline AgentScope 2.0.8 compatibility probe.

This probe intentionally uses a local fake ChatModelBase implementation.  It never
constructs a provider client and therefore must not make a network or paid-model
request.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

EXPECTED_VERSION = "2.0.8"


class ProbeResult(BaseModel):
    answer: str
    score: int


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


class FakeModelError(RuntimeError):
    """Raised by the fake provider to verify provider-error propagation."""


class FakeChatModel:
    """A small ChatModelBase fake with text, structured, invalid, and error modes."""

    def __init__(self, mode: str) -> None:
        from agentscope.formatter import OpenAIChatFormatter
        from agentscope.model import ChatModelBase

        class Parameters(BaseModel):
            temperature: float = 0.0

        class Model(ChatModelBase):
            def __init__(self, selected_mode: str) -> None:
                super().__init__(
                    credential=None,
                    model="offline-fake-model",
                    parameters=Parameters(),
                    stream=False,
                    max_retries=0,
                )
                self.formatter = OpenAIChatFormatter()
                self.selected_mode = selected_mode
                self.calls = 0

            async def _call_api(
                self,
                model_name: str,
                messages: list[dict[str, Any]],
                tools: list[dict[str, Any]] | None = None,
                tool_choice: Any = None,
                **kwargs: Any,
            ) -> Any:
                del model_name, messages, tools, tool_choice, kwargs
                self.calls += 1

                from agentscope.message import TextBlock, ToolCallBlock
                from agentscope.model import ChatResponse

                if self.selected_mode == "text":
                    return ChatResponse(
                        content=[TextBlock(text="fake final answer")],
                        is_last=True,
                    )
                if self.selected_mode == "structured":
                    return ChatResponse(
                        content=[
                            ToolCallBlock(
                                id="offline-call-1",
                                name="GenerateStructuredOutput",
                                input='{"answer":"structured","score":7}',
                            ),
                        ],
                        is_last=True,
                    )
                if self.selected_mode == "invalid-json":
                    return ChatResponse(
                        content=[
                            ToolCallBlock(
                                id=f"offline-call-{self.calls}",
                                name="GenerateStructuredOutput",
                                input="{not json}",
                            ),
                        ],
                        is_last=True,
                    )
                if self.selected_mode == "error":
                    raise FakeModelError("fake provider boom")
                raise AssertionError(f"unknown fake mode: {self.selected_mode}")

        self.instance = Model(mode)


def user_msg(text: str):
    from agentscope.message import Msg

    return Msg(
        name="user",
        role="user",
        content=[{"type": "text", "text": text}],
    )


def text_from_msg(msg: Any) -> str:
    return "".join(
        block.text
        for block in msg.content
        if getattr(block, "type", None) == "text"
    )


def make_agent(model: Any, *, react_config: Any = None):
    from agentscope.agent import Agent

    kwargs: dict[str, Any] = {
        "name": "compat-probe",
        "system_prompt": "You are an offline compatibility probe.",
        "model": model,
    }
    if react_config is not None:
        kwargs["react_config"] = react_config
    return Agent(**kwargs)


async def check_import_and_version() -> None:
    import agentscope

    assert agentscope.__version__ == EXPECTED_VERSION, agentscope.__version__


async def check_exception_exports() -> None:
    from agentscope import exception

    for name in (
        "StructuredOutputError",
        "ToolJSONDecodeError",
        "ToolInterruptedError",
    ):
        assert hasattr(exception, name), name


async def check_msg_creation() -> None:
    msg = user_msg("hello")
    assert msg.role == "user"
    assert msg.name == "user"
    assert text_from_msg(msg) == "hello"


async def check_single_turn_text() -> None:
    from agentscope.message import Msg

    fake = FakeChatModel("text")
    output = await make_agent(fake.instance).reply(user_msg("hello"))
    assert isinstance(output, Msg)
    assert text_from_msg(output) == "fake final answer"
    assert output.finished_reason.value == "completed"
    assert fake.instance.calls == 1


async def check_structured_output() -> None:
    fake = FakeChatModel("structured")
    output = await make_agent(fake.instance).reply(
        user_msg("return a result"),
        structured_schema=ProbeResult,
    )
    assert output.structured_output == {"answer": "structured", "score": 7}
    assert fake.instance.calls == 1


async def check_invalid_msg_validation() -> None:
    from agentscope.message import Msg

    try:
        Msg(name="user", role="user", content="not-a-block-list")  # type: ignore[arg-type]
    except ValidationError:
        return
    raise AssertionError("Msg accepted string content; expected pydantic ValidationError")


async def check_invalid_schema_validation() -> None:
    fake = FakeChatModel("text")
    try:
        await make_agent(fake.instance).reply(
            user_msg("hello"),
            structured_schema=int,  # type: ignore[arg-type]
        )
    except ValidationError:
        return
    raise AssertionError("reply accepted int as structured_schema")


async def check_missing_model_boundary() -> None:
    # Agent's constructor is permissive; the missing/incompatible model is
    # detected when reply() needs model.formatter.
    try:
        await make_agent(None).reply(user_msg("hello"))  # type: ignore[arg-type]
    except AttributeError as exc:
        assert "formatter" in str(exc)
        return
    raise AssertionError("missing model did not fail during reply()")


async def check_provider_error_boundary() -> None:
    fake = FakeChatModel("error")
    try:
        await make_agent(fake.instance).reply(user_msg("hello"))
    except FakeModelError as exc:
        assert str(exc) == "fake provider boom"
        return
    raise AssertionError("fake provider error was swallowed")


async def check_invalid_json_is_bounded() -> None:
    from agentscope.agent import ReActConfig

    fake = FakeChatModel("invalid-json")
    output = await make_agent(
        fake.instance,
        react_config=ReActConfig(max_iters=1, structured_output_grace_iters=1),
    ).reply(user_msg("return JSON"), structured_schema=ProbeResult)

    # AgentScope retries malformed structured tool calls, so use a bounded
    # ReActConfig. It returns an error-bearing terminal Msg instead of raising.
    assert output.finished_reason.value == "exceed_max_iters"
    assert output.structured_output is None
    assert fake.instance.calls <= 2


async def check_dict_react_config_is_rejected_at_reply() -> None:
    fake = FakeChatModel("text")
    try:
        await make_agent(
            fake.instance,
            react_config={"max_iters": 1},
        ).reply(user_msg("hello"))
    except AttributeError as exc:
        assert "max_iters" in str(exc)
        return
    raise AssertionError("dict react_config unexpectedly worked")


async def run() -> int:
    # AgentScope logs one warning for the intentionally malformed JSON case.
    # Keep the probe output machine-readable as PASS/FAIL lines.
    logging.disable(logging.WARNING)

    checks = [
        ("import and exact version", check_import_and_version),
        ("exception exports", check_exception_exports),
        ("Msg creation", check_msg_creation),
        ("single Agent / single turn text", check_single_turn_text),
        ("structured JSON output", check_structured_output),
        ("invalid Msg validation", check_invalid_msg_validation),
        ("invalid structured_schema validation", check_invalid_schema_validation),
        ("missing model error boundary", check_missing_model_boundary),
        ("provider execution error boundary", check_provider_error_boundary),
        ("bounded invalid structured JSON behavior", check_invalid_json_is_bounded),
        ("dict react_config error boundary", check_dict_react_config_is_rejected_at_reply),
    ]
    results: list[Check] = []
    for name, check in checks:
        try:
            await check()
        except Exception as exc:  # Keep all checks running and report one summary.
            results.append(Check(name, False, f"{type(exc).__name__}: {exc}"))
        else:
            results.append(Check(name, True, "ok"))

    print(f"AgentScope compatibility probe (expected {EXPECTED_VERSION})")
    print("offline fake provider: no network, no API key, no paid model call")
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"{status:<4} {result.name}: {result.detail}")

    failed = [result for result in results if not result.passed]
    print(f"\nSummary: {len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
