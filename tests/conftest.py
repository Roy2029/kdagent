"""pytest 共享工具：FakeLLM（假 LLMClient）与事件构造 helper。

test_agent_loop 内嵌自己的 FakeLLM 保持不动，此处服务 M1-e UI/命令/确认测试。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from kdagent.engine.llm.base import LLMStreamEvent, Payload
from kdagent.engine.messages import ToolUseBlock


class FakeLLM:
    """按顺序弹出预设事件批的假 LLM（async generator，匹配 LLMClient Protocol）。"""

    def __init__(self, responses: list[list[LLMStreamEvent]]) -> None:
        self._responses = responses
        self.call_count = 0

    async def stream_chat(self, payload: Payload) -> AsyncIterator[LLMStreamEvent]:
        self.call_count += 1
        for ev in self._responses.pop(0):
            yield ev


def done(text: str = "") -> list[LLMStreamEvent]:
    """一轮完整回复：可选文本 + end_turn。"""
    events: list[LLMStreamEvent] = []
    if text:
        events.append(LLMStreamEvent(type="text_delta", text=text))
    events.append(LLMStreamEvent(type="stop", stop_reason="end_turn"))
    return events


def tool_call(name: str, input: dict[str, Any], id_: str = "t") -> list[LLMStreamEvent]:
    """一轮工具调用请求。"""
    return [
        LLMStreamEvent(type="tool_use", tool_use=ToolUseBlock(id=id_, name=name, input=input)),
        LLMStreamEvent(type="stop", stop_reason="tool_use"),
    ]
