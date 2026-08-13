"""对话管理器测试（规格 02 §3.3：交替 / 配对 / 合并 / repair）。"""

from __future__ import annotations

from kdagent.engine.conversation import ConversationManager, ToolResult
from kdagent.engine.messages import TextBlock, ToolResultBlock, ToolUseBlock


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
    cm.add_tool_results([ToolResult(tool_use_id="call_1", content="内容")])
    last = cm.messages[-1]
    assert last.role == "user"  # 铁律 1：工具结果以 user 身份回传
    assert isinstance(last.content[0], ToolResultBlock)
    assert last.content[0].tool_use_id == "call_1"
    assert last.content[0].is_error is False


def test_error_tool_result_is_kept() -> None:
    cm = _manager()
    cm.add_tool_results([ToolResult(tool_use_id="call_1", content="失败", is_error=True)])
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
    cm.add_tool_results([ToolResult(tool_use_id="gone", content="孤立结果")])
    assert len(cm.messages) == 1  # 相邻 user 自动合并
    assert len(cm.messages[-1].content) == 2  # TextBlock + 孤立 ToolResultBlock
    cm.repair_chain()
    assert cm.messages[-1].role == "user"
    assert cm.messages[-1].content == [TextBlock("开始")]


def test_repair_chain_keeps_paired_tool_results() -> None:
    cm = _manager()
    cm.add_assistant_message([ToolUseBlock(id="keep", name="Bash", input={})])
    cm.add_tool_results([ToolResult(tool_use_id="keep", content="ok")])
    cm.repair_chain()
    assert cm.messages[-1].content[0].tool_use_id == "keep"
