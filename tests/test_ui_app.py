"""TUI 冒烟测试（规格 05 §5 能跑档项：三区域 / 事件渲染 / 命令分发 / Tab 补全）。

Textual `run_test()` headless 驱动；不启动真实终端。真实 DeepSeek 对话走 live 测试。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from conftest import FakeLLM

from kdagent.config import Config
from kdagent.engine.agent import Agent
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.events import LoopCompleteEvent, StreamTextEvent, ToolResultEvent
from kdagent.engine.llm.base import LLMStreamEvent
from kdagent.tools import build_default_registry
from kdagent.ui.app import KDApp
from kdagent.ui.chat import ChatView
from kdagent.ui.statusbar import StatusBar
from kdagent.ui.toolregion import ToolRegion


def _make_app(tmp_path: Path) -> KDApp:
    llm = FakeLLM([[LLMStreamEvent(type="text_delta", text="你好"), LLMStreamEvent(type="stop")]])
    return KDApp(
        config=Config(),
        llm=llm,
        conversation=ConversationManager(),
        tools=build_default_registry(),
        work_dir=tmp_path,
        sessions_dir=tmp_path / ".kdagent" / "sessions",
    )


async def test_three_region_layout(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#chat", ChatView)
        assert app.query_one("#tools", ToolRegion)
        assert app.query_one("#status", StatusBar)
        assert app.query_one("#input")


async def test_event_dispatch_updates_widgets(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._on_event(StreamTextEvent("hello stream"))
        app._on_event(
            ToolResultEvent(name="Grep", content="匹配到 3 行\nline1\nline2", is_error=False, duration_ms=5)
        )
        await pilot.pause()
        chat = app.query_one("#chat", ChatView)
        tools = app.query_one("#tools", ToolRegion)
        assert any("hello stream" in m for m in chat.messages)
        assert tools._lines and any("✓ Grep" in line for line in tools._lines)
        # LoopComplete → 收起工具区 + 滚动到底
        app._on_event(LoopCompleteEvent(turns=2, usage=None))
        await pilot.pause()
        assert any("完成" in m for m in chat.messages)
        assert tools._lines == []


async def test_slash_help_lists_commands(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("/", "h", "e", "l", "p", "enter")
        await pilot.pause()
        chat = app.query_one("#chat", ChatView)
        assert any("可用命令" in m for m in chat.messages)
        assert any("/exit" in m for m in chat.messages)


async def test_unknown_command_guides_to_help(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("/", "n", "o", "p", "e", "enter")
        await pilot.pause()
        chat = app.query_one("#chat", ChatView)
        assert any("未知命令" in m and "/help" in m for m in chat.messages)


async def test_tab_completion_fills_command(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one("#input")
        input_bar.focus()
        input_bar.value = "/he"
        app.action_complete_command()
        await pilot.pause()
        assert input_bar.value == "/help"


async def test_agent_run_sends_user_message(tmp_path: Path) -> None:
    """输入非命令 → Agent Loop：用户消息进对话区、LLM 被调用。"""
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("h", "i", "enter")
        await pilot.pause()
        chat = app.query_one("#chat", ChatView)
        assert any("hi" in m for m in chat.messages)
        assert app._agent.conversation.messages[-1].role == "assistant"


async def test_esc_cancels_agent_cleanly(tmp_path: Path) -> None:
    """Esc（action_cancel_agent）取消当前循环：CancelledEvent → ChatView"已取消"。"""

    class SlowLLM:
        async def stream_chat(self, payload: Any) -> Any:
            yield LLMStreamEvent(type="text_delta", text="开始输出")
            await asyncio.sleep(10)  # 挂起等待取消

    app = _make_app(tmp_path)
    app._agent = Agent(  # type: ignore[assignment]  # 替换为挂起 LLM
        config=Config(),
        llm=SlowLLM(),
        conversation=app._agent.conversation,
        tools=build_default_registry(),
        events=app._on_event,
        work_dir=tmp_path,
        confirm=app._confirm,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.send_user_message("慢慢来")
        await pilot.pause()  # 让 worker 启动并进入挂起
        app.action_cancel_agent()
        await pilot.pause()
        await pilot.pause()
        chat = app.query_one("#chat", ChatView)
        assert any("已取消" in m for m in chat.messages)
