"""AgentScope model/provider construction.

The runtime never silently falls back to a fake or deterministic provider.
Tests inject a local AgentScope ``ChatModelBase`` instance into the adapter;
this factory is only for explicitly configured production providers.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


class ProviderConfigurationError(RuntimeError):
    code = "agentscope_configuration"


@dataclass(frozen=True)
class AgentScopeProviderConfig:
    provider: str
    model: str
    endpoint: str | None
    api_key: str | None
    timeout: float

    @classmethod
    def from_env(cls) -> AgentScopeProviderConfig:
        provider = os.getenv("AGENTSCOPE_PROVIDER", "openai").strip().lower()
        model = os.getenv("AGENTSCOPE_MODEL", "").strip()
        endpoint = os.getenv("AGENTSCOPE_ENDPOINT") or None
        api_key = os.getenv("AGENTSCOPE_API_KEY") or None
        try:
            timeout = float(os.getenv("AGENTSCOPE_TIMEOUT", "60"))
        except ValueError as exc:
            raise ProviderConfigurationError("AGENTSCOPE_TIMEOUT must be a number") from exc
        if provider != "openai":
            raise ProviderConfigurationError(f"unsupported provider: {provider}")
        if not model:
            raise ProviderConfigurationError("AGENTSCOPE_MODEL is required")
        if not api_key:
            raise ProviderConfigurationError("AGENTSCOPE_API_KEY is required")
        if timeout <= 0:
            raise ProviderConfigurationError("AGENTSCOPE_TIMEOUT must be positive")
        return cls(provider, model, endpoint, api_key, timeout)


def create_agentscope_model(config: AgentScopeProviderConfig | None = None) -> Any:
    """Create the configured AgentScope 2.0.8 model, without network I/O."""
    config = config or AgentScopeProviderConfig.from_env()
    try:
        from agentscope.credential import OpenAICredential
        from agentscope.model import OpenAIChatModel
    except Exception as exc:  # pragma: no cover - environment/packaging failure
        raise ProviderConfigurationError("AgentScope OpenAI provider is unavailable") from exc
    credential = OpenAICredential(api_key=config.api_key or "", base_url=config.endpoint)
    parameters = OpenAIChatModel.Parameters(temperature=0.0)
    return OpenAIChatModel(
        credential=credential,
        model=config.model,
        parameters=parameters,
        stream=False,
        max_retries=0,
        client_kwargs={"timeout": config.timeout},
    )
