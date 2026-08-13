"""OpenAI 兼容 adapter 测试：序列化 + SSE 解析器 + 可选真实调用（规格 02 §3.2）。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kdagent.engine.llm.base import Payload, ProviderConfig
from kdagent.engine.llm.openai import OpenAICompatClient, _OpenAIStreamParser, _serialize_messages
from kdagent.engine.messages import Message, TextBlock, ToolResultBlock, ToolUseBlock

# ---------- 序列化 ----------

def test_serialize_user_text() -> None:
    messages = [Message(role="user", content=[TextBlock("你好")])]
    assert _serialize_messages(messages) == [{"role": "user", "content": "你好"}]


def test_serialize_tool_result_as_separate_tool_role() -> None:
    messages = [
        Message(
            role="assistant",
            content=[TextBlock("查一下"), ToolUseBlock(id="call_1", name="ReadFile", input={"path": "a.txt"})],
        ),
        Message(role="user", content=[ToolResultBlock(tool_use_id="call_1", content="内容")]),
    ]
    result = _serialize_messages(messages)
    assert result[0] == {
        "role": "assistant",
        "content": "查一下",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "ReadFile", "arguments": '{"path": "a.txt"}'},
            }
        ],
    }
    assert result[1] == {"role": "tool", "tool_call_id": "call_1", "content": "内容"}


def test_serialize_user_with_text_and_tool_result_split() -> None:
    messages = [
        Message(
            role="user",
            content=[TextBlock("说明"), ToolResultBlock(tool_use_id="x", content="结果")],
        )
    ]
    result = _serialize_messages(messages)
    assert result == [
        {"role": "user", "content": "说明"},
        {"role": "tool", "tool_call_id": "x", "content": "结果"},
    ]


# ---------- SSE 解析器 ----------

def _feed_all(parser: _OpenAIStreamParser, lines: list[str]) -> list:
    events = []
    for line in lines:
        events.extend(parser.feed(line))
    events.extend(parser.finish())
    return events


def test_parser_text_stream() -> None:
    events = _feed_all(
        _OpenAIStreamParser(),
        [
            'data: {"choices":[{"delta":{"content":"你"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":"好"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ],
    )
    types = [e.type for e in events]
    assert types == ["text_delta", "text_delta", "stop"]
    assert "".join(e.text for e in events if e.text) == "你好"
    assert events[-1].stop_reason == "stop"


def test_parser_tool_call_fragments_accumulate() -> None:
    lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"ReadFile","arguments":""}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"path\\":\\""}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"a.txt\\"}"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    events = _feed_all(_OpenAIStreamParser(), lines)
    tool_uses = [e.tool_use for e in events if e.type == "tool_use"]
    assert len(tool_uses) == 1
    assert tool_uses[0].id == "call_1"
    assert tool_uses[0].name == "ReadFile"
    assert tool_uses[0].input == {"path": "a.txt"}
    assert any(e.type == "stop" and e.stop_reason == "tool_calls" for e in events)


def test_parser_usage_chunk_maps_cache_fields() -> None:
    lines = [
        'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"prompt_cache_hit_tokens":3,"prompt_cache_miss_tokens":7}}',
        "data: [DONE]",
    ]
    events = _feed_all(_OpenAIStreamParser(), lines)
    usage_events = [e.usage for e in events if e.type == "usage"]
    assert len(usage_events) == 1
    assert usage_events[0].input_tokens == 10
    assert usage_events[0].output_tokens == 5
    assert usage_events[0].cache_read_tokens == 3
    assert usage_events[0].cache_creation_tokens == 7


def test_parser_ignores_non_data_lines() -> None:
    parser = _OpenAIStreamParser()
    assert parser.feed(": keep-alive") == []


# ---------- 可选真实调用 ----------

def _load_env_key() -> str | None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None


@pytest.mark.skipif(os.getenv("KDAAGENT_LIVE") != "1", reason="KDAAGENT_LIVE=1 才跑真实调用")
async def test_deepseek_live_stream() -> None:
    api_key = _load_env_key()
    assert api_key, ".env 缺少 DEEPSEEK_API_KEY"
    client = OpenAICompatClient(
        ProviderConfig(
            protocol="openai",
            model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
            api_key=api_key,
        )
    )
    payload = Payload(
        system="",
        messages=[Message(role="user", content=[TextBlock("只回复两个字：收到")])],
        max_tokens=16,
    )
    texts: list[str] = []
    async for ev in client.stream_chat(payload):
        if ev.type == "text_delta" and ev.text:
            texts.append(ev.text)
    assert "".join(texts).strip(), "DeepSeek 流式未返回文本"
