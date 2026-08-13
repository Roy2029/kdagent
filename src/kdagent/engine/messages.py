"""消息模型（规格 02 §3.1）。

所有 provider 差异被挡在 adapter 里，上层只见统一类型。
响应里 content 永远是数组——一次回复可能是 text + 多个 tool_use。

两条铁律（领域层语义约定）：
1. 工具结果以 user 身份回传（API 视角"所有你发给模型的归 user"）。
2. assistant 的 text 与 tool_use 不拆开，一次模型回复是一条 assistant 消息。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str


@dataclass(frozen=True, slots=True)
class ThinkingBlock:
    thinking: str
    signature: str | None = None  # 带签名必须原样回传，不能丢


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False


ContentBlock: TypeAlias = TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock


@dataclass(frozen=True, slots=True)
class Message:
    role: Literal["user", "assistant"]
    content: list[ContentBlock]
