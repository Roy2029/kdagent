"""TodoWrite 测试（规格 03 §3.6：规范化 / 快照 / 状态）。"""

from __future__ import annotations

from pathlib import Path

from kdagent.config import Config
from kdagent.tools.base import ToolContext
from kdagent.tools.todo import TodoWrite, format_todos, normalize_todos


def _ctx(work_dir: Path) -> ToolContext:
    return ToolContext(work_dir=work_dir, config=Config(), tool_use_id="todo_1")


def test_normalize_filters_invalid_entries() -> None:
    raw = [
        {"content": "  ", "tasks": []},  # 空 content 剔除
        {"content": "目标A", "tasks": [{"content": "", "steps": []}]},  # 空 task 剔除
        {
            "content": "目标A",
            "tasks": [{"content": "任务1", "steps": [{"description": ""}]}],  # 空 step 剔除
        },
    ]
    normalized = normalize_todos(raw)
    # 第二个 todo（空 task）与第三个 todo（task 内空 step）都保留
    assert normalized == [
        {"content": "目标A", "tasks": []},
        {"content": "目标A", "tasks": [{"content": "任务1", "status": "pending", "steps": []}]},
    ]


def test_normalize_defaults_status_and_criteria() -> None:
    raw = [
        {
            "content": "目标",
            "tasks": [
                {
                    "content": "任务",
                    "status": "illegal",
                    "steps": [{"description": "步骤", "accept_criteria": "判据"}],
                }
            ],
        }
    ]
    normalized = normalize_todos(raw)
    task = normalized[0]["tasks"][0]
    assert task["status"] == "pending"  # 非法 status 归 pending
    assert task["steps"] == [{"description": "步骤", "accept_criteria": "判据"}]


def test_format_todos_snapshot() -> None:
    todos = [
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
    text = format_todos(todos)
    assert "- 写 HTTP 服务器" in text
    assert "[x] 搭骨架" in text
    assert "[ ] 编译通过" in text
    assert "[判据: 能 import]" in text


async def test_execute_updates_snapshot(tmp_path: Path) -> None:
    tool = TodoWrite()
    ctx = _ctx(tmp_path)
    result = await tool.execute(
        ctx,
        {"todos": [{"content": "目标", "tasks": [{"content": "任务", "steps": []}]}]},
    )
    assert result.name == "TodoWrite"
    assert result.tool_use_id == "todo_1"
    assert result.is_error is False
    assert "- 目标" in result.content
    assert "  [ ] 任务" in result.content
    assert result.duration_ms >= 0


async def test_execute_replaces_full_list(tmp_path: Path) -> None:
    tool = TodoWrite()
    ctx = _ctx(tmp_path)
    await tool.execute(ctx, {"todos": [{"content": "旧"}]})
    result = await tool.execute(ctx, {"todos": [{"content": "新"}]})
    assert "旧" not in result.content
    assert "新" in result.content


def test_validate_input_requires_nonempty_todos() -> None:
    tool = TodoWrite()
    assert tool.validate_input({})  # todos 缺失
    assert tool.validate_input({"todos": []})  # todos 空
    assert tool.validate_input({"todos": [{"content": "a"}]}) == []


def test_meta_declarations() -> None:
    tool = TodoWrite()
    assert tool.category == "planning"
    assert tool.is_read_only() is False
    assert tool.is_destructive() is False
    assert tool.is_concurrency_safe({}) is False
    assert tool.require_confirm is False


async def test_execute_triggers_todos_callback(tmp_path: Path) -> None:
    """M1-f：TodoWrite execute 触发 todos 回调（→ 04 会话状态 + 05 面板渲染）。"""
    received: list[dict[str, object]] = []

    def capture(raw: list[dict[str, object]]) -> None:
        received.append(raw)

    tool = TodoWrite()
    ctx = ToolContext(
        work_dir=tmp_path, config=Config(), tool_use_id="todo_1", todos=capture
    )
    result = await tool.execute(
        ctx,
        {"todos": [{"content": "目标", "tasks": [{"content": "任务", "steps": []}]}]},
    )
    assert received, "todos 回调应被触发"
    assert received[0][0]["content"] == "目标"
    assert received[0][0]["tasks"][0]["content"] == "任务"
    assert result.is_error is False
