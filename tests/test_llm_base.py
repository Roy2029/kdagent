"""Provider 工厂分发测试（规格 02 §3.2，D9：切换 provider 上层零改动）。"""

from __future__ import annotations

import pytest

from kdagent.engine.llm import (
    AnthropicClient,
    OpenAICompatClient,
    ProviderConfig,
    build_llm_client,
)


def test_factory_dispatch_openai() -> None:
    config = ProviderConfig(protocol="openai", model="deepseek-chat")
    assert isinstance(build_llm_client(config), OpenAICompatClient)


def test_factory_dispatch_anthropic() -> None:
    config = ProviderConfig(protocol="anthropic", model="claude-opus-5")
    assert isinstance(build_llm_client(config), AnthropicClient)


def test_factory_rejects_unknown_protocol() -> None:
    with pytest.raises(ValueError, match="不支持的 provider 协议"):
        build_llm_client(ProviderConfig(protocol="other", model="x"))  # type: ignore[arg-type]
