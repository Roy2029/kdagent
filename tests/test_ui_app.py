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
from kdagent.engine.messages import TextBlock
from kdagent.tools import build_default_registry
from kdagent.ui.app import ChatInput, KDApp
from kdagent.ui.chat import ChatView
from kdagent.ui.statusbar import StatusBar
from kdagent.ui.todoregion import TodoRegion
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


async def test_four_region_layout(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#chat", ChatView)
        assert app.query_one("#todo", TodoRegion)
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
        input_bar.text = "/he"  # ChatInput(TextArea)：文本属性是 .text（.value 仅 Input 有）
        app.action_complete_command()
        await pilot.pause()
        assert input_bar.text == "/help"


async def test_agent_run_sends_user_message(tmp_path: Path) -> None:
    """输入非命令 → Agent Loop：用户消息进对话区、LLM 被调用。"""
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("h", "i", "enter")
        await pilot.pause()
        chat = app.query_one("#chat", ChatView)
        assert any("hi" in m for m in chat.messages)
        assert app._agent.conversation.messages[-1].role == "assistant"


async def test_session_new_switches_conversation(tmp_path: Path) -> None:
    """/session new → Agent 切换到新会话（conversation 换新对象）。"""
    app = _make_app(tmp_path)
    old_sid = app._session.id
    old_conv = app._agent.conversation
    async with app.run_test() as pilot:
        await pilot.pause()
        app.dispatch_command("session", "new")
        await pilot.pause()
        assert app._session.id != old_sid
        assert app._agent.conversation is not old_conv


async def test_session_resume_switches_conversation(tmp_path: Path) -> None:
    """/session resume <id> → Agent 切回旧会话，消息历史可读。"""
    app = _make_app(tmp_path)
    # 先在当前会话落一条消息
    app._session.append_user("存档内容")
    old_conv = app._agent.conversation
    async with app.run_test() as pilot:
        await pilot.pause()
        app.dispatch_command("session", "resume " + app._session.id)
        await pilot.pause()
        assert app._agent.conversation is not old_conv
        texts = [
            b.text for m in app._agent.conversation.messages for b in m.content if isinstance(b, TextBlock)
        ]
        assert any("存档内容" in t for t in texts)


def test_control_re_whitelists_editing_keys() -> None:
    """M1-i2：_CONTROL_RE 放行删除键与中文 UTF-8 续字节，仅挡 ESC/8-bit CSI 头。"""
    for ch in ("\x08", "\x7f", "\xe4", "\xbd", "\x80", "\xa0"):
        assert not ChatInput._CONTROL_RE.search(ch), f"应放行 {ch!r}"
    for ch in ("\x1b", "\x9b"):
        assert ChatInput._CONTROL_RE.search(ch), f"应拦截 {ch!r}"


def test_chat_input_bindings_include_editing() -> None:
    """M1-i2：Textual 合并父类绑定（dom._merge_bindings），ChatInput 须含删除/复制/粘贴。"""
    keys = set(ChatInput._merged_bindings.key_to_bindings)
    assert {"backspace", "ctrl+h", "ctrl+c", "ctrl+v"} <= keys


async def test_chat_input_backspace_deletes(tmp_path: Path) -> None:
    """M1-i2：ctrl+h（= \\x08，Windows 传统 backspace）触发 delete_left 删除字符。"""
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one("#input", ChatInput)
        input_bar.focus()
        input_bar.text = "abc"
        input_bar.cursor_location = (0, 3)
        await pilot.press("ctrl+h")
        await pilot.pause()
        assert input_bar.text == "ab"


async def test_chat_input_copy(tmp_path: Path) -> None:
    """M1-i2：Ctrl+C（priority）走 copy action → 系统剪贴板（不触发 App 退出）。"""
    app = _make_app(tmp_path)
    copied: list[str] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        app.copy_to_clipboard = lambda text: copied.append(str(text))  # type: ignore[method-assign]
        input_bar = app.query_one("#input", ChatInput)
        input_bar.focus()
        input_bar.text = "hello"
        input_bar.select_all()
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert copied == ["hello"]


async def test_chat_input_paste(tmp_path: Path, monkeypatch: Any) -> None:
    """M1-i2：Ctrl+V（priority）走 paste action → 读系统剪贴板插入。"""
    from kdagent.ui import app as ui_app

    monkeypatch.setattr(ui_app.pyperclip, "paste", lambda: "你好")
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one("#input", ChatInput)
        input_bar.focus()
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert input_bar.text == "你好"


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
