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
    PromptTooLongError,
    ProviderConfig,
    ToolSchema,
    ToolTruncatedError,
    Usage,
)
from kdagent.engine.messages import Message, TextBlock, ToolResultBlock, ToolUseBlock

# 上下文超长错误标记（01 §6 ③：紧急压缩触发）。DeepSeek/OpenAI 兼容在 HTTP 400 body。
_PTL_MARKERS = (
    "maximum context length",
    "context_length_exceeded",
    "context length exceeded",
    "prompt is too long",
    "please reduce the length",
)


def _is_prompt_too_long(body: str) -> bool:
    return any(m in body.lower() for m in _PTL_MARKERS)


def _serialize_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """领域 Message → OpenAI 兼容 messages 数组。

    关键：ToolResultBlock 拆成独立 `role=tool` 消息（OpenAI 中 tool_result 是独立 role）。
    """
    out: list[dict[str, Any]] = []
    seen_tool_result: set[str] = set()
    for msg in messages:
        if msg.role == "assistant":
            text = "".join(b.text for b in msg.content if isinstance(b, TextBlock))
            tool_uses = [b for b in msg.content if isinstance(b, ToolUseBlock)]
            # content 用空串而非 None：OpenAI 兼容厂商（含 DeepSeek）对
            # content: null 的 assistant 消息有时返回 400（带 tool_calls 时亦然）。
            item: dict[str, Any] = {"role": "assistant", "content": text or ""}
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
            # tool 响应优先输出：OpenAI 要求 tool 消息紧跟其 assistant(tool_calls)。
            # 混合消息（text+tool_result 被 `_append` 合并）若先输出 user 文本会打断
            # tool 配对，触发 "insufficient tool messages following tool_calls" 400。
            for tr in tool_results:
                # E2 兜底（2026-08-29）：同一 tool_call_id 只输出一次 role=tool。
                # 旧会话文件（_flush_last 重复写盘）resume 后可能带重复 tool_result，
                # 重复的 role=tool 无前置 assistant tool_calls → DeepSeek 400。
                if tr.tool_use_id in seen_tool_result:
                    continue
                seen_tool_result.add(tr.tool_use_id)
                out.append({"role": "tool", "tool_call_id": tr.tool_use_id, "content": tr.content})
            if text_parts:
                out.append({"role": "user", "content": "".join(text_parts)})
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
        # B2：是否发过可见文本——finish_reason=length 且零文本零 tool_use 时，
        # 判定「输出被整体截断」（如 reasoning 吃满 max_tokens），不再静默。
        self._emitted_text = False

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
                self._emitted_text = True
                events.append(LLMStreamEvent(type="text_delta", text=text))
            # B2：reasoning_content（思考）静默丢弃但记录在案——它不产生 text/
            # tool_use 事件，若思考吃满 max_tokens，feed 其余字段全空，靠
            # _flush_tool_calls 的零内容截断检测兜底报错（21da 实测根因）。
            # （ThinkingBlock 映射延后，M1 阶段不消费 reasoning_content。）
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
        seen_ids: set[str] = set()
        truncated = self._stop_reason in ("length", "max_tokens")  # 输出被 max_tokens 截断
        emitted_tool = False
        emitted_error = False
        for index in sorted(self._tool_calls):
            entry = self._tool_calls[index]
            if not entry["name"]:
                continue  # 残缺 tool_call 丢弃
            if entry["id"] in seen_ids:
                continue  # 流式同 id 重复（厂商偶发）→ 保第一个，防历史链损坏
            if entry["id"]:
                seen_ids.add(entry["id"])
            try:
                arguments = json.loads(entry["arguments"]) if entry["arguments"] else {}
            except json.JSONDecodeError:
                # A：不再静默吞错为 {} —— 否则工具报「参数校验失败」误导模型以为
                # 是格式问题，反复重试同样超长输出（实测：贪吃蛇 HTML 被 4096
                # token 截断 → 空参数 → 无限死循环）。丢弃残缺 tool_use，向上抛
                # 明确错误，agent 反馈模型拆小输出后再重试。
                emitted_error = True
                events.append(
                    LLMStreamEvent(
                        type="error",
                        error=ToolTruncatedError(
                            f"工具 {entry['name']} 的参数不完整，JSON 解析失败"
                            + ("（输出被 max_tokens 截断）" if truncated else "（可能被截断）")
                        ),
                    )
                )
                continue
            events.append(
                LLMStreamEvent(
                    type="tool_use",
                    tool_use=ToolUseBlock(id=entry["id"], name=entry["name"], input=arguments),
                )
            )
            emitted_tool = True
        # B2：finish_reason=length 但既无文本也无可用 tool_use、且无既有错误 →
        # 输出被整体截断（典型：模型 reasoning 思考吃满 max_tokens，content 为空）。
        # 旧实现零事件 → agent 当「空回复」静默 TERMINAL（21da 会话实测「没报错但
        # 也没反应了」）。此处显式抛 empty 截断，agent 反馈「别过度思考」后重试。
        if truncated and not self._emitted_text and not emitted_tool and not emitted_error:
            events.append(
                LLMStreamEvent(
                    type="error",
                    error=ToolTruncatedError(
                        "输出被 max_tokens 截断，且未产生任何文本或工具调用"
                        "（可能是思考内容过长）",
                        empty=True,
                    ),
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
            if resp.status_code != 200:
                # 手动读响应体再抛，带上 DeepSeek 具体报错（raise_for_status 不含 body）。
                body_text = (await resp.aread())[:1000].decode("utf-8", errors="replace")
                if _is_prompt_too_long(body_text):
                    raise PromptTooLongError(f"LLM API {resp.status_code}: {body_text}")
                raise RuntimeError(f"LLM API {resp.status_code}: {body_text}")
            async for line in resp.aiter_lines():
                for event in parser.feed(line):
                    yield event
        for event in parser.finish():
            yield event

    def _build_body(self, payload: Payload) -> dict[str, Any]:
        messages = _serialize_messages(payload.messages)
        # C3 修复（2026-08-29）：system 放 messages[0]（role=system）——docstring 明写
        # 但此前从未实现，`payload.system`（含记忆索引/MCP/Skill 注入）整体被丢弃，
        # 模型从没收到记忆索引。anthropic.py 对照实现正确发送，此处补齐。
        if payload.system:
            messages = [{"role": "system", "content": payload.system}, *messages]
        body: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "max_tokens": payload.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if payload.tools:
            body["tools"] = _serialize_tools(payload.tools)
            body["tool_choice"] = "auto"
        return body
