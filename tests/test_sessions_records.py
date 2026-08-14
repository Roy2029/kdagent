"""会话记录映射测试（规格 04 §3.2：协议无关内部表示 ↔ 02 Message）。"""

from __future__ import annotations

import json

from kdagent.engine.messages import Message, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock
from kdagent.sessions.records import (
    SessionRecord,
    StepRecord,
    TodoItemRecord,
    todo_items_from_raw,
)


def test_from_message_user_text() -> None:
    rec = SessionRecord.from_message(Message(role="user", content=[TextBlock("你好")]), ts=100)
    assert rec.role == "user"
    assert rec.content == "你好"
    assert rec.tool_results is None


def test_from_message_assistant_tool_use_and_thinking() -> None:
    msg = Message(
        role="assistant",
        content=[
            TextBlock("我用工具"),
            ToolUseBlock(id="t1", name="ReadFile", input={"path": "a.txt"}),
            ThinkingBlock("先想想", "sig_1"),
        ],
    )
    rec = SessionRecord.from_message(msg, ts=200)
    assert rec.content == "我用工具"
    assert rec.tool_uses[0].tool_name == "ReadFile"
    assert rec.tool_uses[0].arguments == {"path": "a.txt"}
    assert rec.thinking is not None
    assert rec.thinking.signature == "sig_1"


def test_from_message_user_tool_result_content_empty() -> None:
    msg = Message(role="user", content=[ToolResultBlock(tool_use_id="t1", content="结果", is_error=True)])
    rec = SessionRecord.from_message(msg, ts=300)
    assert rec.content == ""  # 工具结果不是独立角色，content 通常为空（04 §3.2）
    assert rec.tool_results[0].is_error is True


def test_to_message_roundtrip_types() -> None:
    rec = SessionRecord.from_message(
        Message(role="assistant", content=[TextBlock("x"), ToolUseBlock(id="a", name="Glob", input={"pattern": "*.py"})]),
        ts=1,
    )
    msg = rec.to_message()
    assert msg.role == "assistant"
    assert isinstance(msg.content[0], TextBlock)
    assert isinstance(msg.content[1], ToolUseBlock)
    assert msg.content[1].id == "a"


def test_json_roundtrip_preserves_all_fields() -> None:
    msg = Message(
        role="assistant",
        content=[
            TextBlock("中文内容"),
            ToolUseBlock(id="a", name="Grep", input={"pattern": "x"}),
            ThinkingBlock("想", "s1"),
        ],
    )
    rec = SessionRecord.from_message(msg, ts=42)
    restored = SessionRecord.from_json(rec.to_json())
    assert restored.content == "中文内容"
    assert restored.tool_uses[0].tool_name == "Grep"
    assert restored.thinking is not None
    assert restored.thinking.signature == "s1"
    assert restored.ts == 42
    assert json.loads(rec.to_json())["role"] == "assistant"


def test_todos_serialization_roundtrip() -> None:
    todos = [TodoItemRecord(content="目标", status="completed", steps=[StepRecord("步骤", "判据")])]
    rec = SessionRecord(role="user", content="x", todos=todos, ts=1)
    restored = SessionRecord.from_json(rec.to_json())
    assert restored.todos is not None
    assert restored.todos[0].content == "目标"
    assert restored.todos[0].status == "completed"
    assert restored.todos[0].steps[0].accept_criteria == "判据"


def test_todos_roundtrip_preserves_group() -> None:
    todos = [
        TodoItemRecord(
            content="任务1",
            status="pending",
            steps=[StepRecord("步骤", "判据")],
            group="目标",
        )
    ]
    rec = SessionRecord(role="user", content="x", todos=todos, ts=1)
    restored = SessionRecord.from_json(rec.to_json())
    assert restored.todos is not None
    assert restored.todos[0].group == "目标"


def test_todo_items_from_raw_three_layer_to_items() -> None:
    """03 TodoWrite 三层结构 → 04 TodoItemRecord（task 为条目，group 记 todo 目标）。"""
    raw = [
        {
            "content": "写 HTTP 服务器",
            "tasks": [
                {
                    "content": "搭骨架",
                    "status": "completed",
                    "steps": [{"description": "建文件", "accept_criteria": "能 import"}],
                },
                {
                    "content": "编译通过",
                    "status": "pending",
                    "steps": [{"description": "跑 pytest"}],
                },
            ],
        }
    ]
    items = todo_items_from_raw(raw)
    assert len(items) == 2
    assert items[0].content == "搭骨架"
    assert items[0].status == "completed"
    assert items[0].group == "写 HTTP 服务器"
    assert items[0].steps[0].accept_criteria == "能 import"
    assert items[1].content == "编译通过"
    assert items[1].status == "pending"
    assert items[1].steps[0].description == "跑 pytest"


def test_todo_items_from_raw_empty_tasks_empty_list() -> None:
    assert todo_items_from_raw([{"content": "无任务的目标", "tasks": []}]) == []
