"""TUI 冒烟测试（规格 05 §5 能跑档项：三区域 / 事件渲染 / 命令分发 / Tab 补全）。

Textual `run_test()` headless 驱动；不启动真实终端。真实 DeepSeek 对话走 live 测试。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from conftest import FakeLLM
from textual.widgets import Collapsible, ListView, Static

from kdagent.config import Config
from kdagent.engine.agent import Agent
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.events import (
    LoopCompleteEvent,
    PermissionRequestEvent,
    StreamTextEvent,
    ToolResultEvent,
    ToolUseEvent,
)
from kdagent.engine.llm.base import LLMStreamEvent
from kdagent.engine.messages import TextBlock
from kdagent.permission.checker import PermissionChecker
from kdagent.tools import build_default_registry
from kdagent.ui.app import ChatInput, KDApp
from kdagent.ui.chat import ChatView
from kdagent.ui.confirm import PermissionDialog, SessionPickerDialog
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
        assert tools._entries and any("Grep" in e.name for e in tools._entries)
        # LoopComplete → 工具区历史保留（U1 可回看），Chat 滚动到底
        app._on_event(LoopCompleteEvent(turns=2, usage=None))
        await pilot.pause()
        assert any("完成" in m for m in chat.messages)
        assert len(tools._entries) == 1  # 仅保留结果条目，孤儿 running 被修剪
        assert tools._entries[0].name == "Grep"


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


async def test_permission_request_dialog_resolves_future(tmp_path: Path) -> None:
    """06 §3.7 HITL：PermissionRequestEvent → 弹 PermissionDialog，n 键 → future=deny。"""
    app = _make_app(tmp_path)
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    ev = PermissionRequestEvent(
        tool_name="Bash", summary="Bash git status", future=future
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app._on_event(ev)
        await pilot.pause()
        assert app.screen is PermissionDialog or any(
            isinstance(s, PermissionDialog) for s in app.screen_stack
        )
        await pilot.press("n")  # 拒绝 → 回传 deny
        await pilot.pause()
        assert future.done() and future.result() == "deny"


async def test_permission_mode_switch_updates_status(tmp_path: Path) -> None:
    """/permissions plan → 状态栏显示权限 plan（无 checker 时静默）。"""
    app = _make_app(tmp_path)
    app._permission_checker = PermissionChecker(mode="default")  # type: ignore[assignment]
    async with app.run_test() as pilot:
        await pilot.pause()
        app.dispatch_command("permissions", "plan")
        await pilot.pause()
        status = app.query_one("#status", StatusBar)
        assert "权限 plan" in str(status.render())


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


# ---- U2：输入框高度自动扩展（至少 4 行） ----

async def test_chat_input_height_expands_with_lines(tmp_path: Path) -> None:
    """U2：多行内容 → 输入框高度自动增长；短内容保持下限 4 行。"""
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one("#input", ChatInput)
        input_bar.text = "单行"
        input_bar._apply_height()
        assert input_bar.styles.height.value == 4  # 下限
        input_bar.text = "一\n二\n三\n四\n五\n六"  # 6 行
        input_bar._apply_height()
        assert input_bar.styles.height.value == 7  # 行数 + 1 光标行
        long = "\n".join(f"行{i}" for i in range(20))
        input_bar.text = long
        input_bar._apply_height()
        assert input_bar.styles.height.value == 10  # 上限


async def test_chat_input_auto_resize_on_type(tmp_path: Path) -> None:
    """U2：输入换行 → Changed 事件自动调高度（不手动调 _apply_height）。"""
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one("#input", ChatInput)
        input_bar.focus()
        input_bar.text = "一\n二\n三\n四\n五"
        await pilot.pause()
        assert input_bar.styles.height.value == 6  # 5 行内容 + 光标行，自动从 4 长到 6


# ---- U4：↑/↓ 历史输入切换 ----

async def test_chat_input_history_navigation(tmp_path: Path) -> None:
    """U4：提交三条 → ↑ 翻历史（最后提交最近），↓ 回到底恢复草稿。"""
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one("#input", ChatInput)
        input_bar.focus()
        # 连续提交三条
        for text in ("第一条", "第二条", "第三条"):
            input_bar.text = text
            input_bar.action_submit()
            await pilot.pause()
        assert input_bar._history == ["第一条", "第二条", "第三条"]
        # ↑：第三条 → 第二条 → 第一条
        input_bar.text = "新草稿"
        await pilot.press("up")
        await pilot.pause()
        assert input_bar.text == "第三条"
        await pilot.press("up")
        await pilot.pause()
        assert input_bar.text == "第二条"
        await pilot.press("up")
        await pilot.pause()
        assert input_bar.text == "第一条"
        # ↓：回到空草稿（_draft 已在首次 ↑ 时存为"新草稿"）
        await pilot.press("down")
        await pilot.pause()
        assert input_bar.text == "第二条"
        await pilot.press("down")
        await pilot.pause()
        assert input_bar.text == "第三条"
        await pilot.press("down")
        await pilot.pause()
        assert input_bar.text == "新草稿"


async def test_chat_input_history_dedups_consecutive(tmp_path: Path) -> None:
    """U4：连续提交相同文本只入历史一次。"""
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one("#input", ChatInput)
        for _ in range(3):
            input_bar.text = "重复"
            input_bar.action_submit()
            await pilot.pause()
        assert input_bar._history == ["重复"]


# ---- U3：/session list 弹选单 + 自动标题 ----------------------------------

async def test_session_picker_dialog_selects_sid(tmp_path: Path) -> None:
    """U3：SessionPickerDialog ↑/↓ 选择 + Enter 回传 sid；Esc 取消回 None。"""
    app = _make_app(tmp_path)
    picked: list[str | None] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        # Enter 路径：↑ 从当前 s2 移到 s1，Enter 回传 sid
        dialog = SessionPickerDialog([("s1", "会话一"), ("s2", "会话二")], current_sid="s2")
        app.push_screen(dialog, picked.append)
        await pilot.pause()
        picker = dialog.query_one("#session-picker", ListView)
        picker.focus()
        await pilot.press("up")  # 高亮从当前 s2 → s1
        await pilot.press("enter")
        await pilot.pause()
        assert picked == ["s1"]
        assert dialog not in app.screen_stack
        # Esc 取消路径：同一 run_test 内再推一个 dialog
        picked2: list[str | None] = []
        dialog2 = SessionPickerDialog([("s1", "会话一")], current_sid="")
        app.push_screen(dialog2, picked2.append)
        await pilot.pause()
        dialog2.query_one("#session-picker", ListView).focus()
        await pilot.press("escape")
        await pilot.pause()
        assert picked2 == [None]


async def test_loop_complete_auto_titles_session(tmp_path: Path) -> None:
    """U3：LoopComplete 且会话无标题 → 后台一次性 LLM 总结生成标题。"""
    app = _make_app(tmp_path)
    # 覆盖为「标题总结」专用响应：文本只输出标题本身（无引号）。
    app._llm = FakeLLM(
        [[LLMStreamEvent(type="text_delta", text="重构权限模块"), LLMStreamEvent(type="stop")]]
    )
    app._session.append_user("帮我重构权限模块，把 L2 的 checker 拆开")
    async with app.run_test() as pilot:
        await pilot.pause()
        app._on_event(LoopCompleteEvent(turns=1, usage=None))
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        assert app._session.title == "重构权限模块"


async def test_session_list_opens_picker_and_switches(tmp_path: Path) -> None:
    """U3：/session list → 弹选单；Enter 选当前会话并关弹窗（resume 路径通）。"""
    app = _make_app(tmp_path)
    old_sid = app._session.id
    async with app.run_test() as pilot:
        await pilot.pause()
        app.dispatch_command("session", "list")
        await pilot.pause()
        dialog = next(
            (s for s in app.screen_stack if isinstance(s, SessionPickerDialog)), None
        )
        assert dialog is not None
        dialog.query_one("#session-picker", ListView).focus()
        await pilot.press("enter")  # 高亮当前会话 → resume 同 sid
        await pilot.pause()
        assert not any(isinstance(s, SessionPickerDialog) for s in app.screen_stack)
        assert app._session.id == old_sid


# ---- U1：工具监控可展开/收起详情页 + 历史保留 -----------------------------

async def test_tool_region_entry_expands_details(tmp_path: Path) -> None:
    """U1：工具条目默认收起摘要，点击/展开后含完整参数 + 输出全文。"""
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        tools = app.query_one("#tools", ToolRegion)
        app._on_event(ToolUseEvent(id="t1", name="Bash", input={"command": "ls -la /tmp" * 5}))
        app._on_event(
            ToolResultEvent(
                name="Bash", content="行1\n行2\n行3\n行4\n行5", is_error=False, duration_ms=3
            )
        )
        await pilot.pause()
        colls = list(tools.query(Collapsible))
        assert len(colls) == 1
        # 默认收起摘要；数据层完整输出已保存（详情体含全文）
        assert colls[0].collapsed
        assert "行5" in tools._entries[0].content
        # 展开后详情 body（Static）渲染含完整输出
        colls[0].collapsed = False
        await pilot.pause()
        body = colls[0].query_one(".tool-detail", Static)
        assert "行1" in str(body.render())
        assert "行5" in str(body.render())


async def test_tool_region_keeps_history_and_trims(tmp_path: Path) -> None:
    """U1：LoopComplete 保留历史；超过 _MAX_ENTRIES 裁剪最老条目。"""
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        tools = app.query_one("#tools", ToolRegion)
        for i in range(12):
            app._on_event(ToolUseEvent(id=f"t{i}", name="Grep", input={"pattern": f"p{i}"}))
            app._on_event(
                ToolResultEvent(name="Grep", content=f"结果{i}", is_error=False, duration_ms=1)
            )
        await pilot.pause()
        assert len(tools._entries) == 10  # 裁剪到上限
        assert tools._entries[-1].name == "Grep"
        assert "结果11" in tools._entries[-1].content  # 最新保留
        assert tools._entries[0].content == "结果2"  # 最老两条被丢
        # LoopComplete 后仍可回看
        app._on_event(LoopCompleteEvent(turns=1, usage=None))
        await pilot.pause()
        assert len(tools._entries) == 10
        assert tools._entries[-1].content == "结果11"
