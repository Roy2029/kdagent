"""OpenAI 兼容 adapter（规格 02 §3.2，覆盖 DeepSeek/Kimi/Qwen/GLM，主 provider）。

差异点落实：
- system 放 `messages[0]`（role=system）
- tool_result → 独立 `role=tool` 消息（tool_call_id 对应）
- tool_use → assistant 消息的 `tool_calls` 数组
- 流式 SSE：`data: {...}` 行，`data: [DONE]` 终止；usage 在末尾 chunk
- 相邻同角色消息需客户端自行合并（领域层已保证不出现）

M1 阶段 ThinkingBlock 忽略（deepseek-chat 无 thinking；deepseek-reasoner 的
reasoning_content 映射延后）。
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
    Usage,
)
from kdagent.engine.messages import Message, TextBlock, ToolResultBlock, ToolUseBlock


def _serialize_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """领域 Message → OpenAI 兼容 messages 数组。

    关键：ToolResultBlock 拆成独立 `role=tool` 消息（OpenAI 中 tool_result 是独立 role）。
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "assistant":
            text = "".join(b.text for b in msg.content if isinstance(b, TextBlock))
            tool_uses = [b for b in msg.content if isinstance(b, ToolUseBlock)]
            item: dict[str, Any] = {"role": "assistant", "content": text or None}
            if tool_uses:
                item["tool_calls"] = [
                    {
                        "id": b.id,
                        "type": "function",
                        "function": {
                            "name": b.name,
                            "arguments": json.dumps(b.input, ensure_ascii=False),
                        },
                    }
                    for b in tool_uses
                ]
            out.append(item)
        else:  # user
            text_parts: list[str] = []
            tool_results: list[ToolResultBlock] = []
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                elif isinstance(block, ToolResultBlock):
                    tool_results.append(block)
            if text_parts:
                out.append({"role": "user", "content": "".join(text_parts)})
            for tr in tool_results:
                out.append({"role": "tool", "tool_call_id": tr.tool_use_id, "content": tr.content})
    return out


def _serialize_tools(tools: list[ToolSchema]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


def _parse_usage(data: dict[str, Any]) -> Usage:
    """DeepSeek 前缀缓存字段：prompt_cache_hit_tokens / prompt_cache_miss_tokens（M2 标定用）。"""
    return Usage(
        input_tokens=data.get("prompt_tokens", 0),
        output_tokens=data.get("completion_tokens", 0),
        cache_read_tokens=data.get("prompt_cache_hit_tokens", 0),
        cache_creation_tokens=data.get("prompt_cache_miss_tokens", 0),
    )


class _OpenAIStreamParser:
    """SSE 行 → LLMStreamEvent 的翻译器（独立以便单测）。

    tool_calls 分片按 index 累积、arguments 增量拼接；finish_reason 时 flush。
    """

    def __init__(self) -> None:
        self._tool_calls: dict[int, dict[str, str]] = {}
        self._stop_reason: str | None = None
        self._stop_sent = False

    def feed(self, line: str) -> list[LLMStreamEvent]:
        if not line.startswith("data:"):
            return []
        data = line[5:].strip()
        if data == "[DONE]":
            return self._ensure_stop()
        chunk = json.loads(data)
        events: list[LLMStreamEvent] = []
        if chunk.get("usage"):
            events.append(LLMStreamEvent(type="usage", usage=_parse_usage(chunk["usage"])))
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text:
                events.append(LLMStreamEvent(type="text_delta", text=text))
            for tc in delta.get("tool_calls") or []:
                self._feed_tool_call(tc)
            if choice.get("finish_reason"):
                self._stop_reason = choice["finish_reason"]
                events.extend(self._ensure_stop())
        return events

    def finish(self) -> list[LLMStreamEvent]:
        return self._ensure_stop()

    def _feed_tool_call(self, tc: dict[str, Any]) -> None:
        index = tc.get("index", 0)
        entry = self._tool_calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if tc.get("id"):
            entry["id"] = tc["id"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            entry["name"] = fn["name"]
        if fn.get("arguments"):
            entry["arguments"] += fn["arguments"]

    def _ensure_stop(self) -> list[LLMStreamEvent]:
        if self._stop_sent:
            return []
        self._stop_sent = True
        events = self._flush_tool_calls()
        events.append(LLMStreamEvent(type="stop", stop_reason=self._stop_reason))
        return events

    def _flush_tool_calls(self) -> list[LLMStreamEvent]:
        events: list[LLMStreamEvent] = []
        for index in sorted(self._tool_calls):
            entry = self._tool_calls[index]
            if not entry["name"]:
                continue  # 残缺 tool_call 丢弃
            try:
                arguments = json.loads(entry["arguments"]) if entry["arguments"] else {}
            except json.JSONDecodeError:
                arguments = {}
            events.append(
                LLMStreamEvent(
                    type="tool_use",
                    tool_use=ToolUseBlock(id=entry["id"], name=entry["name"], input=arguments),
                )
            )
        self._tool_calls = {}
        return events


class OpenAICompatClient:
    """OpenAI 兼容协议客户端（DeepSeek 主 provider 走此 adapter）。"""

    def __init__(self, config: ProviderConfig, timeout: float = 60.0) -> None:
        self._config = config
        self._timeout = timeout
        base = (config.base_url or "https://api.deepseek.com/v1").rstrip("/")
        self._url = f"{base}/chat/completions"

    async def stream_chat(self, payload: Payload) -> AsyncIterator[LLMStreamEvent]:
        body = self._build_body(payload)
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        parser = _OpenAIStreamParser()
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

    def _build_body(self, payload: Payload) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._config.model,
            "messages": _serialize_messages(payload.messages),
            "max_tokens": payload.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if payload.tools:
            body["tools"] = _serialize_tools(payload.tools)
            body["tool_choice"] = "auto"
        return body
