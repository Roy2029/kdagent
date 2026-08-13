"""消息模型测试（规格 02 §3.1）。"""

from __future__ import annotations

import pytest

from kdagent.engine.messages import (
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)


def test_text_block() -> None:
    block = TextBlock("你好")
    assert block.text == "你好"


def test_thinking_block_with_and_without_signature() -> None:
    assert ThinkingBlock("思考").signature is None
    assert ThinkingBlock("思考", "sig123").signature == "sig123"


def test_tool_use_and_result_pairing() -> None:
    use = ToolUseBlock(id="call_1", name="ReadFile", input={"path": "a.txt"})
    result = ToolResultBlock(tool_use_id="call_1", content="内容")
    assert use.id == result.tool_use_id
    assert result.is_error is False
    assert ToolResultBlock("call_1", "err", True).is_error is True


def test_message_is_frozen() -> None:
    msg = Message(role="user", content=[TextBlock("hi")])
    with pytest.raises(AttributeError):
        msg.role = "assistant"  # type: ignore[misc]


def test_message_roles_are_typed() -> None:
    assert Message("user", []).role == "user"
    assert Message("assistant", []).role == "assistant"
