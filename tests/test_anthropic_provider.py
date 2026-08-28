"""Anthropic adapter 测试：序列化 + SSE 解析器（规格 02 §3.2，备用 provider）。"""

from __future__ import annotations

from kdagent.engine.llm.anthropic import (
    _AnthropicStreamParser,
    _serialize_messages,
)
from kdagent.engine.llm.base import ToolTruncatedError
from kdagent.engine.messages import (
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

# ---------- 序列化 ----------

def test_serialize_tool_use_as_content_block() -> None:
    messages = [
        Message(
            role="assistant",
            content=[TextBlock("查一下"), ToolUseBlock(id="toolu_1", name="ReadFile", input={"path": "a.txt"})],
        )
    ]
    result = _serialize_messages(messages)
    assert result[0]["role"] == "assistant"
    assert result[0]["content"] == [
        {"type": "text", "text": "查一下"},
        {"type": "tool_use", "id": "toolu_1", "name": "ReadFile", "input": {"path": "a.txt"}},
    ]


def test_serialize_tool_result_inside_user_content() -> None:
    messages = [
        Message(
            role="user",
            content=[TextBlock("结果如下"), ToolResultBlock(tool_use_id="toolu_1", content="内容", is_error=True)],
        )
    ]
    result = _serialize_messages(messages)
    assert result[0] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "结果如下"},
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": "内容",
                "is_error": True,
            },
        ],
    }


def test_serialize_thinking_block_keeps_signature() -> None:
    messages = [
        Message(
            role="assistant",
            content=[ThinkingBlock("思考", signature="sig_abc")],
        )
    ]
    result = _serialize_messages(messages)
    assert result[0]["content"] == [{"type": "thinking", "thinking": "思考", "signature": "sig_abc"}]


# ---------- SSE 解析器 ----------

def _feed_all(parser: _AnthropicStreamParser, lines: list[str]) -> list:
    events = []
    for line in lines:
        events.extend(parser.feed(line))
    events.extend(parser.finish())
    return events


def test_parser_text_and_tool_use_and_usage() -> None:
    lines = [
        'event: message_start',
        'data: {"type":"message_start","message":{"usage":{"input_tokens":5}}}',
        'event: content_block_start',
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        'event: content_block_delta',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你好"}}',
        'event: content_block_stop',
        'data: {"type":"content_block_stop","index":0}',
        'event: content_block_start',
        'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_1","name":"ReadFile","input":{}}}',
        'event: content_block_delta',
        'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":\\""}}',
        'event: content_block_delta',
        'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"a.txt\\"}"}}',
        'event: content_block_stop',
        'data: {"type":"content_block_stop","index":1}',
        'event: message_delta',
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":8}}',
        'event: message_stop',
        'data: {"type":"message_stop"}',
    ]
    events = _feed_all(_AnthropicStreamParser(), lines)
    texts = "".join(e.text for e in events if e.type == "text_delta")
    assert texts == "你好"

    tool_uses = [e.tool_use for e in events if e.type == "tool_use"]
    assert len(tool_uses) == 1
    assert tool_uses[0].id == "toolu_1"
    assert tool_uses[0].name == "ReadFile"
    assert tool_uses[0].input == {"path": "a.txt"}

    usage_events = [e.usage for e in events if e.type == "usage"]
    assert len(usage_events) == 1
    assert usage_events[0].input_tokens == 5
    assert usage_events[0].output_tokens == 8

    stops = [e for e in events if e.type == "stop"]
    assert len(stops) == 1
    assert stops[0].stop_reason == "tool_use"


def test_parser_plain_chat_end_turn() -> None:
    lines = [
        'event: message_start',
        'data: {"type":"message_start","message":{"usage":{"input_tokens":3}}}',
        'event: content_block_delta',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"好的"}}',
        'event: message_delta',
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}',
        'event: message_stop',
        'data: {"type":"message_stop"}',
    ]
    events = _feed_all(_AnthropicStreamParser(), lines)
    stops = [e for e in events if e.type == "stop"]
    assert stops and stops[0].stop_reason == "end_turn"


def test_parser_thinking_delta_is_accumulated_not_emitted() -> None:
    parser = _AnthropicStreamParser()
    parser.feed("event: content_block_delta")
    events = parser.feed('data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"深思"}}')
    assert events == []  # M1 阶段 thinking 不发射事件
    assert parser._thinking == "深思"  # noqa: SLF001


def test_parser_thinking_only_truncated_emits_error() -> None:
    """thinking 吃满 max_tokens、text/tool_use 皆空 → 不静默，发 ToolTruncatedError。

    与 openai 端 B2 同场景（21da 会话实测「没报错但也没反应了」）：stop_reason=
    max_tokens 且零可见内容 → 明确报截断，agent 反馈「别过度思考」后重试。
    """
    lines = [
        "event: content_block_start",
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking"}}',
        "event: content_block_delta",
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"深思……"}}',
        "event: content_block_stop",
        'data: {"type":"content_block_stop","index":0}',
        "event: message_delta",
        'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"},"usage":{"output_tokens":4096}}',
        "event: message_stop",
        'data: {"type":"message_stop"}',
    ]
    events = _feed_all(_AnthropicStreamParser(), lines)
    errors = [e.error for e in events if e.type == "error"]
    assert len(errors) == 1
    assert isinstance(errors[0], ToolTruncatedError)
    assert errors[0].empty is True
    assert not [e for e in events if e.type == "text_delta"]


def test_parser_unclosed_tool_use_emits_error() -> None:
    """tool_use 块被 max_tokens 截断（content_block_stop 未到）→ 发错误而非静默丢弃。"""
    lines = [
        "event: content_block_start",
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_x","name":"WriteFile","input":{}}}',
        "event: content_block_delta",
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":\\"a"}}',
        "event: message_delta",
        'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"},"usage":{"output_tokens":4096}}',
        "event: message_stop",
        'data: {"type":"message_stop"}',
    ]
    events = _feed_all(_AnthropicStreamParser(), lines)
    errors = [e.error for e in events if e.type == "error"]
    assert len(errors) == 1
    assert isinstance(errors[0], ToolTruncatedError)
    assert errors[0].empty is False  # 有工具调用痕迹 → 拆小输出引导
    assert not [e for e in events if e.type == "tool_use"]
