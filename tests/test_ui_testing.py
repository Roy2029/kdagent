"""TestingEvent UI 三态渲染测试（规格 05 §5 239 / 02 §5 346）。

D74：ChatView.append_testing 纯渲染单测 + app._on_event 事件分发集成。
Textual `run_test()` headless 驱动；不启动真实终端。
"""

from __future__ import annotations

from pathlib import Path

from conftest import FakeLLM

from kdagent.config import Config
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.events import TestingEvent
from kdagent.engine.llm.base import LLMStreamEvent
from kdagent.tools import build_default_registry
from kdagent.ui.app import KDApp
from kdagent.ui.chat import ChatView


def _make_app(tmp_path: Path) -> KDApp:
    llm = FakeLLM([[LLMStreamEvent(type="text_delta", text=""), LLMStreamEvent(type="stop")]])
    return KDApp(
        config=Config(),
        llm=llm,
        conversation=ConversationManager(),
        tools=build_default_registry(),
        work_dir=tmp_path,
        sessions_dir=tmp_path / ".kdagent" / "sessions",
    )


async def test_append_testing_passed(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one("#chat", ChatView)
        chat.append_testing("passed", "uv run pytest", (), "1 passed")
        await pilot.pause()
        assert any("✓ 测试通过" in m for m in chat.messages)
        assert any("uv run pytest" in m for m in chat.messages)


async def test_append_testing_failed_lists_cases(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one("#chat", ChatView)
        chat.append_testing("failed", "uv run pytest", ("test_one", "test_two"), "2 failed")
        await pilot.pause()
        assert any("✗ 测试失败" in m for m in chat.messages)
        assert any("test_one" in m and "test_two" in m for m in chat.messages)


async def test_append_testing_regression_detected(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one("#chat", ChatView)
        chat.append_testing("regression_detected", "uv run pytest", ("test_reg",), "regress")
        await pilot.pause()
        assert any("⚠ 回归检测" in m for m in chat.messages)
        assert any("test_reg" in m for m in chat.messages)


async def test_on_event_dispatches_testing(tmp_path: Path) -> None:
    """_on_event 收到 TestingEvent → ChatView 渲染三态文本（02 §5 346 事件流闭环）。"""
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._on_event(
            TestingEvent(
                status="failed",
                test_cmd="uv run pytest tests/test_demo.py",
                failed_tests=("test_two",),
                summary="3 passed, 1 failed\nFAILED tests/test_demo.py::test_two",
            )
        )
        await pilot.pause()
        chat = app.query_one("#chat", ChatView)
        assert any("✗ 测试失败" in m for m in chat.messages)
        assert any("test_two" in m for m in chat.messages)
