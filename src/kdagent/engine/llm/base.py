"""LLM Provider 抽象层（规格 02 §3.2）。

核心原则：暴露领域语义、隐藏 SDK 细节。
adapter 把各家协议翻译成统一类型；上层换模型只改 ProviderConfig，代码零改动。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from kdagent.engine.messages import Message, ToolUseBlock


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Provider 配置，分发到对应 adapter（D9 多 provider 抽象）。"""

    protocol: Literal["anthropic", "openai"]
    model: str
    base_url: str | None = None
    api_key: str = ""  # 从环境变量读取，不进代码
    max_tokens: int = 4096


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """领域级工具描述（03 工具系统产出）。"""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Usage:
    """token 用量（对齐 01 的模型；M2 起由 01 统一持有）。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass(frozen=True, slots=True)
class Payload:
    """一次 LLM 请求的领域级载荷。"""

    system: str
    messages: list[Message]
    tools: list[ToolSchema] = field(default_factory=list)
    max_tokens: int = 4096


@dataclass(slots=True)
class LLMStreamEvent:
    """高层流式事件：adapter 把各家 SSE 序列翻译成这 5 种。"""

    type: Literal["text_delta", "tool_use", "usage", "stop", "error"]
    text: str | None = None
    tool_use: ToolUseBlock | None = None
    usage: Usage | None = None
    stop_reason: str | None = None
    error: Exception | None = None


class LLMClient(Protocol):
    """流式对话客户端。实现方为 async generator（`async def` + `yield`）。"""

    def stream_chat(self, payload: Payload) -> AsyncIterator[LLMStreamEvent]:
        ...
