"""LLM provider 工厂：protocol 分发到对应 adapter（规格 02 §3.2，D9）。

换模型改 ProviderConfig，上层零改动。
"""

from __future__ import annotations

from kdagent.engine.llm.anthropic import AnthropicClient
from kdagent.engine.llm.base import (
    LLMClient,
    LLMStreamEvent,
    Payload,
    ProviderConfig,
    ToolSchema,
    Usage,
)
from kdagent.engine.llm.openai import OpenAICompatClient


def build_llm_client(config: ProviderConfig) -> LLMClient:
    """按 protocol 分发：openai（主，DeepSeek 等）/ anthropic（备）。"""
    if config.protocol == "openai":
        return OpenAICompatClient(config)
    if config.protocol == "anthropic":
        return AnthropicClient(config)
    raise ValueError(f"不支持的 provider 协议：{config.protocol}")  # pragma: no cover


__all__ = [
    "AnthropicClient",
    "LLMClient",
    "LLMStreamEvent",
    "OpenAICompatClient",
    "Payload",
    "ProviderConfig",
    "ToolSchema",
    "Usage",
    "build_llm_client",
]
