"""OpenAI 兼容 adapter 测试：序列化 + SSE 解析器 + 可选真实调用（规格 02 §3.2）。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kdagent.engine.llm.base import Payload, ProviderConfig, ToolTruncatedError
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
    """混合消息（text+tool_result 被 `_append` 合并）序列化：tool 响应优先输出，
    保证紧跟其上一条 assistant(tool_calls)——否则打断 tool 配对触发 400。"""
    messages = [
        Message(
            role="user",
            content=[TextBlock("说明"), ToolResultBlock(tool_use_id="x", content="结果")],
        )
    ]
    result = _serialize_messages(messages)
    assert result == [
        {"role": "tool", "tool_call_id": "x", "content": "结果"},
        {"role": "user", "content": "说明"},
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


def test_parser_dedups_duplicate_tool_call_id() -> None:
    """同 id 重复 tool_call（厂商流式偶发）→ 只保留第一个，防历史链损坏。"""
    lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_A","function":{"name":"Bash","arguments":"{}"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"call_A","function":{"name":"Bash","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    events = _feed_all(_OpenAIStreamParser(), lines)
    tool_uses = [e.tool_use for e in events if e.type == "tool_use"]
    assert len(tool_uses) == 1  # call_A 去重，不产生同 id 两条
    assert tool_uses[0].id == "call_A"


def test_parser_keeps_distinct_tool_call_ids() -> None:
    """不同 id 的 tool_call 不去重（正常多工具并行）。"""
    lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_A","function":{"name":"Bash","arguments":"{}"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"call_B","function":{"name":"Glob","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    events = _feed_all(_OpenAIStreamParser(), lines)
    tool_uses = [e.tool_use for e in events if e.type == "tool_use"]
    assert sorted(tu.id for tu in tool_uses) == ["call_A", "call_B"]


def test_parser_truncated_arguments_emits_error_not_empty_tool() -> None:
    """输出被 max_tokens 截断 → arguments JSON 不完整 → 发 error 事件而非空 tool_use。

    旧行为：JSONDecodeError 被静默吞成 `{}` → 工具收到空参数报「参数校验失败」，
    误导模型反复重试同样超长输出（实测：贪吃蛇 HTML 死循环）。A 修复后显式抛
    ToolTruncatedError，agent 可反馈模型拆小输出。
    """
    lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_W","function":{"name":"WriteFile","arguments":"{\\"path\\": \\"game.html\\", \\"content\\": \\"<html>...."}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
        "data: [DONE]",
    ]
    events = _feed_all(_OpenAIStreamParser(), lines)
    tool_uses = [e.tool_use for e in events if e.type == "tool_use"]
    errors = [e.error for e in events if e.type == "error"]
    assert tool_uses == []  # 残缺 tool_use 不发
    assert len(errors) == 1
    assert isinstance(errors[0], ToolTruncatedError)
    assert "WriteFile" in str(errors[0]) and "截断" in str(errors[0])


def test_parser_complete_arguments_with_length_still_emits_tool() -> None:
    """finish_reason=length 但 arguments 完整闭合 → 仍正常发 tool_use（不误伤）。"""
    lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_B","function":{"name":"Bash","arguments":"{\\"command\\": \\"echo hi\\"}"}}]},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
        "data: [DONE]",
    ]
    events = _feed_all(_OpenAIStreamParser(), lines)
    tool_uses = [e.tool_use for e in events if e.type == "tool_use"]
    assert len(tool_uses) == 1
    assert tool_uses[0].input == {"command": "echo hi"}
    assert not [e for e in events if e.type == "error"]


# ---------- B2：reasoning 吃满 max_tokens → 空回复截断不静默（21da 会话实测） ----------

def test_parser_reasoning_only_truncated_emits_error() -> None:
    """思考内容（reasoning_content）吃满 max_tokens、content 为空 → 不静默，
    发 ToolTruncatedError（empty=True）。

    21da 会话根因：feed 忽略 reasoning_content 且零 text/tool_use → 零事件 →
    agent 当「空回复」静默 TERMINAL「没报错但也没反应了」。修复后明确报截断。
    """
    lines = [
        'data: {"choices":[{"delta":{"reasoning_content":"正在思考如何实现五项增强……"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
        "data: [DONE]",
    ]
    events = _feed_all(_OpenAIStreamParser(), lines)
    errors = [e.error for e in events if e.type == "error"]
    assert len(errors) == 1
    assert isinstance(errors[0], ToolTruncatedError)
    assert errors[0].empty is True
    # 不产生误导性 text_delta / tool_use
    assert not [e for e in events if e.type == "text_delta"]
    assert not [e for e in events if e.type == "tool_use"]


def test_parser_text_with_length_no_extra_error() -> None:
    """有可见文本但 finish_reason=length → 正常发 text，不追加额外截断错误。"""
    lines = [
        'data: {"choices":[{"delta":{"content":"部分内容"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
        "data: [DONE]",
    ]
    events = _feed_all(_OpenAIStreamParser(), lines)
    texts = [e.text for e in events if e.type == "text_delta"]
    assert texts == ["部分内容"]
    assert not [e for e in events if e.type == "error"]


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
