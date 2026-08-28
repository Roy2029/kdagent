"""LLM Provider 抽象层（规格 02 §3.2）。

核心原则：暴露领域语义、隐藏 SDK 细节。
adapter 把各家协议翻译成统一类型；上层换模型只改 ProviderConfig，代码零改动。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from kdagent.engine.messages import Message, ToolUseBlock


class PromptTooLongError(RuntimeError):
    """API 返回上下文超长（prompt too long）——01 §6 ③ 紧急压缩触发点。"""


class ToolTruncatedError(RuntimeError):
    """工具参数不完整（arguments JSON 解析失败，通常因输出被 max_tokens 截断）。

    区别于 PromptTooLongError（输入侧超长）：这是**输出侧**被截断，工具参数
    不可用。agent 收到后应反馈模型拆小输出、丢弃残缺 tool_use 并重试，而不是
    把空参数当合法调用执行——实测写大文件场景会因误导性「参数校验失败」死循环。

    `empty`：True = 输出被截断且**未产生任何文本/工具调用**（典型：模型先输出
    大量 reasoning 思考吃满 max_tokens，content 为空，parser 零事件）。agent
    应反馈模型「别过度思考、直接输出」，与拆小输出是不同引导（2026-08-28 21da
    会话实测：3 条用户消息全部静默无响应）。
    """

    def __init__(self, message: str, *, empty: bool = False) -> None:
        super().__init__(message)
        self.empty = empty


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
