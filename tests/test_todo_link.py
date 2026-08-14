"""M1-f 打通测试：TodoWrite → Session.todos → TodoRegion 渲染（03 §3.6 / 05 §3.2b）。"""

from __future__ import annotations

from pathlib import Path

from conftest import FakeLLM

from kdagent.config import Config
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import LLMStreamEvent
from kdagent.engine.messages import ToolUseBlock
from kdagent.sessions.records import todo_items_from_raw
from kdagent.tools import build_default_registry
from kdagent.tools.base import ToolContext
from kdagent.tools.todo import TodoWrite
from kdagent.ui.app import KDApp
from kdagent.ui.todoregion import TodoRegion


def _make_app(tmp_path: Path) -> KDApp:
    llm = FakeLLM([[LLMStreamEvent(type="text_delta", text="好"), LLMStreamEvent(type="stop")]])
    return KDApp(
        config=Config(),
        llm=llm,
        conversation=ConversationManager(),
        tools=build_default_registry(),
        work_dir=tmp_path,
        sessions_dir=tmp_path / ".kdagent" / "sessions",
    )


def _raw_todos() -> list[dict[str, object]]:
    return [
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


def test_todo_region_renders_three_layer() -> None:
    """TodoRegion 从 TodoItemRecord 渲染三层：group 目标 + task + steps。"""
    region = TodoRegion()
    region._items = todo_items_from_raw(_raw_todos())
    lines = region._render_lines()
    assert "写 HTTP 服务器" in lines  # group → todo 目标层
    assert "[x] 搭骨架" in lines  # task + 完成打勾
    assert "[ ] 编译通过" in lines
    assert "建文件" in lines  # steps 层
    assert "判据: 能 import" in lines
    assert lines.splitlines()[1].startswith("  [x]")  # task 缩进


def test_todo_region_empty_shows_placeholder() -> None:
    region = TodoRegion()
    assert "暂无待办" in region._render_lines()


def test_app_on_todos_updates_session_and_region(tmp_path: Path) -> None:
    """App 层：TodoWrite 回调 → Session.set_todos + TodoRegion 实时渲染。"""
    app = _make_app(tmp_path)
    # 不挂载 run_test：直接调回调（回调是 App 方法，不依赖 widget 树）
    app._on_todos(_raw_todos())
    assert app._session.todos is not None
    assert app._session.todos[0].group == "写 HTTP 服务器"
    assert app._session.todos[0].steps[0].accept_criteria == "能 import"


async def test_agent_todo_write_pipeline_updates_session(tmp_path: Path) -> None:
    """端到端：Agent 执行 TodoWrite → ToolContext.todos 回调 → App 会话 todos。"""

    class TodoThenDone:
        async def stream_chat(self, payload: object) -> object:
            yield LLMStreamEvent(
                type="tool_use",
                tool_use=ToolUseBlock(
                    id="t1", name="TodoWrite", input={"todos": _raw_todos()}
                ),
            )
            yield LLMStreamEvent(type="stop")

    app = _make_app(tmp_path)
    app._agent._llm = TodoThenDone()  # type: ignore[assignment]
    await app._agent.run("列计划")
    assert app._session.todos is not None
    assert app._session.todos[0].group == "写 HTTP 服务器"
    assert app._session.todos[0].steps[0].accept_criteria == "能 import"


async def test_todo_write_via_tool_context_callback(tmp_path: Path) -> None:
    """ToolContext.todos 注入：TodoWrite 直接执行触发回调（Agent 外部路径）。"""
    received: list[object] = []
    tool = TodoWrite()
    ctx = ToolContext(
        work_dir=tmp_path, config=Config(), tool_use_id="t1", todos=lambda raw: received.append(raw)
    )
    await tool.execute(ctx, {"todos": _raw_todos()})
    assert received
    assert received[0][0]["content"] == "写 HTTP 服务器"  # type: ignore[index]


async def test_ui_todo_region_renders_after_agent_run(tmp_path: Path) -> None:
    """TUI 端到端：Agent 执行 TodoWrite → run_test 挂载 → TodoRegion 面板显示三层。"""

    class TodoThenDone:
        async def stream_chat(self, payload: object) -> object:
            yield LLMStreamEvent(
                type="tool_use",
                tool_use=ToolUseBlock(
                    id="t1", name="TodoWrite", input={"todos": _raw_todos()}
                ),
            )
            yield LLMStreamEvent(type="stop")

    app = _make_app(tmp_path)
    app._agent._llm = TodoThenDone()  # type: ignore[assignment]
    async with app.run_test() as pilot:
        await pilot.pause()
        app.send_user_message("列计划")
        # 等 agent worker 完成
        import time

        deadline = time.time() + 10
        while time.time() < deadline:
            worker = app._agent_worker
            if worker is None or not worker.is_running:
                break
            await pilot.pause()
        todo = app.query_one("#todo", TodoRegion)
        assert todo._items, "TodoRegion 应有待办项"
        assert todo._items[0].group == "写 HTTP 服务器"
