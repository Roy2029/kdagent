"""对话管理器测试（规格 02 §3.3：交替 / 配对 / 合并 / repair）。"""

from __future__ import annotations

from kdagent.engine.conversation import ConversationManager
from kdagent.engine.messages import Message, TextBlock, ToolResultBlock, ToolUseBlock
from kdagent.tools.base import ToolResult


def _manager() -> ConversationManager:
    return ConversationManager()


def test_user_assistant_alternate() -> None:
    cm = _manager()
    cm.add_user_message("问题")
    cm.add_assistant_message([TextBlock("回答")])
    cm.add_user_message("追问")
    roles = [m.role for m in cm.messages]
    assert roles == ["user", "assistant", "user"]


def test_adjacent_same_role_are_merged() -> None:
    cm = _manager()
    cm.add_assistant_message([TextBlock("第一段")])
    cm.add_assistant_message([TextBlock("第二段")])
    assert len(cm.messages) == 1
    assert cm.messages[0].role == "assistant"
    assert len(cm.messages[0].content) == 2


def test_tool_results_attach_as_user_and_pair_ids() -> None:
    cm = _manager()
    cm.add_assistant_message(
        [TextBlock("我用工具"), ToolUseBlock(id="call_1", name="ReadFile", input={})]
    )
    cm.add_tool_results([ToolResult(tool_use_id="call_1", name="ReadFile", content="内容")])
    last = cm.messages[-1]
    assert last.role == "user"  # 铁律 1：工具结果以 user 身份回传
    assert isinstance(last.content[0], ToolResultBlock)
    assert last.content[0].tool_use_id == "call_1"
    assert last.content[0].is_error is False


def test_error_tool_result_is_kept() -> None:
    cm = _manager()
    cm.add_tool_results([ToolResult(tool_use_id="call_1", name="ReadFile", content="失败", is_error=True)])
    block = cm.messages[-1].content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.is_error is True


def test_last_tool_use_ids() -> None:
    cm = _manager()
    cm.add_assistant_message([ToolUseBlock(id="a", name="Grep", input={}), ToolUseBlock(id="b", name="ReadFile", input={})])
    assert cm.last_tool_use_ids() == ["a", "b"]
    cm.add_user_message("下一轮")
    assert cm.last_tool_use_ids() == ["a", "b"]  # 只看最近 assistant


def test_assistant_text_and_tool_use_stay_one_message() -> None:
    cm = _manager()
    cm.add_assistant_message([TextBlock("我来查"), ToolUseBlock(id="c", name="Glob", input={})])
    assert len(cm.messages) == 1
    assert len(cm.messages[0].content) == 2  # 铁律 2：text 与 tool_use 不拆开


def test_repair_chain_drops_orphan_tool_results() -> None:
    cm = _manager()
    cm.add_user_message("开始")
    cm.add_tool_results([ToolResult(tool_use_id="gone", name="TodoWrite", content="孤立结果")])
    assert len(cm.messages) == 1  # 相邻 user 自动合并
    assert len(cm.messages[-1].content) == 2  # TextBlock + 孤立 ToolResultBlock
    cm.repair_chain()
    assert cm.messages[-1].role == "user"
    assert cm.messages[-1].content == [TextBlock("开始")]


def test_repair_chain_keeps_paired_tool_results() -> None:
    cm = _manager()
    cm.add_assistant_message([ToolUseBlock(id="keep", name="Bash", input={})])
    cm.add_tool_results([ToolResult(tool_use_id="keep", name="Bash", content="ok")])
    cm.repair_chain()
    assert cm.messages[-1].content[0].tool_use_id == "keep"


def test_repair_chain_dedups_duplicate_tool_use_in_assistant() -> None:
    """同一条 assistant 消息内重复 tool_call_id（流式偶发）→ 去重保第一个。"""
    cm = _manager()
    cm.restore(
        [
            Message(
                role="assistant",
                content=[
                    ToolUseBlock(id="dup", name="Bash", input={}),
                    ToolUseBlock(id="dup", name="Bash", input={}),
                ],
            )
        ]
    )
    cm.repair_chain()
    tool_uses = [
        b for m in cm.messages for b in m.content if isinstance(b, ToolUseBlock)
    ]
    assert [b.id for b in tool_uses] == ["dup"]
    # 去重后仍补一条 errorResult（assistant 请求了工具）
    results = [
        b for m in cm.messages for b in m.content if isinstance(b, ToolResultBlock)
    ]
    assert [b.tool_use_id for b in results] == ["dup"]
    assert results[0].is_error is True


def test_repair_chain_dedups_duplicate_tool_result_across_messages() -> None:
    """跨消息重复 tool_use_id（C2 场景：同一 tool_result 出现两次）→ 保第一个。"""
    cm = _manager()
    cm.restore(
        [
            Message(
                role="assistant",
                content=[
                    ToolUseBlock(id="call_A", name="Bash", input={}),
                    ToolUseBlock(id="call_B", name="Glob", input={}),
                ],
            ),
            Message(role="user", content=[ToolResultBlock(tool_use_id="call_A", content="r1")]),
            Message(
                role="user",
                content=[
                    ToolResultBlock(tool_use_id="call_A", content="r1-dup"),
                    ToolResultBlock(tool_use_id="call_B", content="r2"),
                ],
            ),
        ]
    )
    cm.repair_chain()
    results = [
        (b.tool_use_id, b.content)
        for m in cm.messages
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]
    assert results == [("call_A", "r1"), ("call_B", "r2")]
