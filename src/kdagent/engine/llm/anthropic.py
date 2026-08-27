"""Anthropic Messages adapter（规格 02 §3.2，备用 provider）。

差异点落实：
- system 顶层参数
- tool_result → user 消息里的 `tool_use` 配对 block
- tool_use → assistant 消息里的 block
- 流式 SSE：`message_start → content_block_* → message_delta → message_stop`
- 相邻同角色消息由 API 自动合并

M1 阶段 thinking_delta 仅累积丢弃，不发事件；Extended Thinking 原样回传延后
（agent 层组装 assistant 消息时按 block 还原，见规格 02 §3.5）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from kdagent.engine.llm.base import (
    LLMStreamEvent,
    Payload,
    ProviderConfig,
    ToolSchema,
    ToolTruncatedError,
    Usage,
)
from kdagent.engine.messages import (
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)


def _serialize_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """领域 Message → Anthropic Messages 数组（tool_result 内嵌 user content）。"""
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "assistant":
            content: list[dict[str, Any]] = []
            for block in msg.content:
                if isinstance(block, TextBlock):
                    content.append({"type": "text", "text": block.text})
                elif isinstance(block, ThinkingBlock):
                    item: dict[str, Any] = {"type": "thinking", "thinking": block.thinking}
                    if block.signature:
                        item["signature"] = block.signature
                    content.append(item)
                elif isinstance(block, ToolUseBlock):
                    content.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
                    )
            out.append({"role": "assistant", "content": content})
        else:  # user
            content = []
            for block in msg.content:
                if isinstance(block, TextBlock):
                    content.append({"type": "text", "text": block.text})
                elif isinstance(block, ToolResultBlock):
                    content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.tool_use_id,
                            "content": block.content,
                            "is_error": block.is_error,
                        }
                    )
            out.append({"role": "user", "content": content})
    return out


def _serialize_tools(tools: list[ToolSchema]) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in tools
    ]


class _AnthropicStreamParser:
    """Anthropic SSE（event:/data: 两行）→ LLMStreamEvent 的翻译器（独立以便单测）。

    thinking_delta 累积在 _thinking 但丢弃（M1 预留）。
    """

    def __init__(self) -> None:
        self._event_name: str | None = None
        self._tool_use: dict[str, Any] | None = None  # 当前累积的 tool_use block
        self._input_buffer = ""
        self._thinking = ""
        self._usage = Usage()
        self._stop_reason: str | None = None
        self._stop_sent = False

    def feed(self, line: str) -> list[LLMStreamEvent]:
        line = line.strip()
        if line.startswith("event:"):
            self._event_name = line[6:].strip()
            return []
        if line.startswith("data:"):
            data = line[5:].strip()
            if not data:
                return []
            return self._handle_data(data)
        return []

    def finish(self) -> list[LLMStreamEvent]:
        return self._ensure_stop()

    def _handle_data(self, data: str) -> list[LLMStreamEvent]:
        payload = json.loads(data)
        kind = payload.get("type", self._event_name)
        if kind == "message_start":
            msg_usage = (payload.get("message") or {}).get("usage") or {}
            self._usage = Usage(input_tokens=msg_usage.get("input_tokens", 0))
            return []
        if kind == "content_block_start":
            block = payload.get("content_block") or {}
            if block.get("type") == "tool_use":
                self._tool_use = {"id": block.get("id", ""), "name": block.get("name", "")}
                self._input_buffer = ""
            return []
        if kind == "content_block_delta":
            delta = payload.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                return [LLMStreamEvent(type="text_delta", text=delta.get("text", ""))]
            if dtype == "thinking_delta":
                self._thinking += delta.get("thinking", "")
                return []
            if dtype == "input_json_delta":
                self._input_buffer += delta.get("partial_json", "")
                return []
            return []
        if kind == "content_block_stop":
            if self._tool_use is not None:
                block = self._tool_use
                self._tool_use = None
                try:
                    arguments = json.loads(self._input_buffer) if self._input_buffer else {}
                except json.JSONDecodeError:
                    # A：同 openai adapter——不静默吞错为 {}（否则空参数执行 →
                    # 误导性「参数校验失败」→ 模型反复重试死循环）。丢弃残缺
                    # tool_use 抛明确错误，agent 反馈模型拆小输出。
                    # stop_reason 在 message_delta 才到，此处无法精确判定截断，
                    # 统一用「可能被截断」。
                    self._input_buffer = ""
                    return [
                        LLMStreamEvent(
                            type="error",
                            error=ToolTruncatedError(
                                f"工具 {block['name']} 的参数不完整，JSON 解析失败"
                                "（可能被 max_tokens 截断）"
                            ),
                        )
                    ]
                self._input_buffer = ""
                return [
                    LLMStreamEvent(
                        type="tool_use",
                        tool_use=ToolUseBlock(
                            id=block["id"], name=block["name"], input=arguments
                        ),
                    )
                ]
            return []
        if kind == "message_delta":
            delta = payload.get("delta") or {}
            if delta.get("stop_reason"):
                self._stop_reason = delta["stop_reason"]
            msg_usage = payload.get("usage") or {}
            self._usage = Usage(
                input_tokens=self._usage.input_tokens,
                output_tokens=msg_usage.get("output_tokens", 0),
            )
            return [LLMStreamEvent(type="usage", usage=self._usage)]
        if kind == "message_stop":
            return self._ensure_stop()
        return []

    def _ensure_stop(self) -> list[LLMStreamEvent]:
        if self._stop_sent:
            return []
        self._stop_sent = True
        return [LLMStreamEvent(type="stop", stop_reason=self._stop_reason)]


class AnthropicClient:
    """Anthropic Messages 协议客户端（备用 provider）。"""

    def __init__(self, config: ProviderConfig, timeout: float = 60.0) -> None:
        self._config = config
        self._timeout = timeout
        base = (config.base_url or "https://api.anthropic.com/v1").rstrip("/")
        self._url = f"{base}/messages"

    async def stream_chat(self, payload: Payload) -> AsyncIterator[LLMStreamEvent]:
        body: dict[str, Any] = {
            "model": self._config.model,
            "max_tokens": payload.max_tokens,
            "system": payload.system,
            "messages": _serialize_messages(payload.messages),
            "stream": True,
        }
        if payload.tools:
            body["tools"] = _serialize_tools(payload.tools)
        headers = {
            "x-api-key": self._config.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        parser = _AnthropicStreamParser()
        async with (
            httpx.AsyncClient(timeout=self._timeout) as client,
            client.stream("POST", self._url, json=body, headers=headers) as resp,
        ):
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                for event in parser.feed(line):
                    yield event
        for event in parser.finish():
            yield event
