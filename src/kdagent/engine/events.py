"""AgentEvent 事件流（规格 02 §3.6）。

Agent 的输出是一个 AgentEvent 异步流，UI 是消费者（05）。Agent 不感知 UI 存在，
换 Web 界面 / 纯 JSON 输出 Agent 零改动。

M1-c 阶段事件以同步 sink（Callable）emit；M1-e TUI 用 queue 消费。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from kdagent.engine.llm.base import Usage

# L5 HITL 裁决（06 §3.7）：用户拍板的三种结果；allow_always → 追加本地规则。
PermissionVerdict = Literal["allow", "deny", "allow_always"]


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """事件基类（全部事件继承它）。"""


@dataclass(frozen=True, slots=True)
class StreamTextEvent(AgentEvent):
    """模型正在输出的文字增量。"""

    text: str


@dataclass(frozen=True, slots=True)
class ToolUseEvent(AgentEvent):
    """模型请求调用工具。"""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResultEvent(AgentEvent):
    """工具执行完成。"""

    name: str
    content: str
    is_error: bool
    duration_ms: int


@dataclass(frozen=True, slots=True)
class UsageEvent(AgentEvent):
    """token 用量更新。"""

    usage: Usage


@dataclass(frozen=True, slots=True)
class TurnCompleteEvent(AgentEvent):
    """一轮 LLM 调用完成（有工具调用）。"""

    turn: int


@dataclass(frozen=True, slots=True)
class LoopCompleteEvent(AgentEvent):
    """整个循环结束（模型主动完成）。"""

    turns: int
    usage: Usage | None


@dataclass(frozen=True, slots=True)
class ErrorEvent(AgentEvent):
    """异常上报。"""

    error: str


@dataclass(frozen=True, slots=True)
class CancelledEvent(AgentEvent):
    """用户中断（Esc / 取消），已收部分已落库。"""


@dataclass(frozen=True, slots=True)
class MaxIterationsReachedEvent(AgentEvent):
    """迭代上限强制停止。"""

    limit: int


@dataclass(frozen=True, slots=True)
class PermissionRequestEvent(AgentEvent):
    """L5 HITL 权限审批请求（06 §3.7）：Agent Loop 阻塞等 UI 回传裁决。

    `future`：UI 消费方用 `set_result(verdict)` 回传；verdict ∈ allow/deny/allow_always。
    """

    tool_name: str
    summary: str
    future: asyncio.Future[PermissionVerdict]


AgentEventSink: TypeAlias = Callable[[AgentEvent], None]
