"""KDAgent TUI 主体（规格 05 §3.1-3.4）。

KDApp 同时实现 UIController（05 §3.5）——命令系统的依赖注入背包含它自己。
事件流（02 AgentEvent）由 sync sink 消费，worker 与 UI 同处主事件循环，线程安全。

M1-e 能跑档：三区域布局 + 事件渲染 + 极简 Y/N + Slash 7 命令 + Esc/Ctrl+C/Tab。
M1-f：接入 SessionManager 持久会话；TodoRegion。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
from typing import Any

import pyperclip  # type: ignore[import-untyped]
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import Key
from textual.message import Message
from textual.widgets import Header, TextArea
from textual.worker import Worker

from kdagent.config import Config
from kdagent.engine.agent import DEFAULT_SYSTEM_PROMPT, Agent
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.events import (
    AgentEvent,
    CancelledEvent,
    ErrorEvent,
    LoopCompleteEvent,
    MaxIterationsReachedEvent,
    StreamTextEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnCompleteEvent,
    UsageEvent,
)
from kdagent.engine.llm.base import LLMClient, Usage
from kdagent.sessions.manager import Session, SessionManager
from kdagent.sessions.records import todo_items_from_raw
from kdagent.tools.registry import ToolRegistry
from kdagent.ui.chat import ChatView
from kdagent.ui.commands import CommandContext, build_default_commands, parse_command
from kdagent.ui.confirm import ConfirmDialog, ExitDialog
from kdagent.ui.statusbar import StatusBar
from kdagent.ui.todoregion import TodoRegion
from kdagent.ui.toolregion import ToolRegion

PLAN_SYSTEM_PROMPT = (
    "你是 KDAgent。当前处于 Plan 模式：只读探索、制定方案，"
    "不要修改文件或执行有副作用的操作。"
)

# Claude Code 风格布局（05 §3.1）：Chat 主区 1fr，todo/tools 有内容才展开，
# 输入框与状态栏固定底部。弹窗样式在 ui/confirm.py（ModalScreen 内）。
_CSS = """
#chat { height: 1fr; border: round $primary; margin: 0 1; }
#todo { height: auto; max-height: 8; border: round $accent; margin: 0 1; }
#tools { height: auto; max-height: 10; border: round $secondary; margin: 0 1; }
#input { height: 3; margin: 0 1; }
#status { height: 1; background: $panel; color: $text; padding: 0 1; }
"""


class ChatInput(TextArea):
    """用户输入框（TextArea 而非 Input）：IME 组合输入支持更完整（参考 mewcode）。

    `priority=True` 关键：widget 层绑定优先于 Textual Screen 默认的
    `tab=app.focus_next` / App 层 `ctrl+c=退出`，避免抢键。
    ctrl+c/ctrl+v 用 TextArea 内置绑定（经 app 系统剪贴板）。
    """

    BINDINGS = [
        Binding("enter", "submit", "提交", priority=True),
        Binding("shift+enter", "newline", "换行", priority=True),
        Binding("ctrl+j", "newline", "换行", priority=True),
        Binding("tab", "complete", "补全", priority=True),
    ]

    # 鼠标序列泄漏防御（M1-i 加固）：完整 \x1b[<...M 会被 Textual 解析为 MouseEvent，
    # 但缺 ESC 前缀的 `[<35;56;28m`（Windows 终端丢 ESC/协议不全）会被拆成
    # 逐个可打印字符 Key 插入。单字符过滤挡不住 → 用状态机：`[` 后跟 `<数字;数字` 到
    # `m/M` 闭合才判定为泄漏整段丢弃；否则回补缓冲字符（正常输入仅延迟一个 `[`）。
    _LEAK_FULL_RE = re.compile(r"\[<[0-9;]*[Mm]")
    _LEAK_PREFIX_RE = re.compile(r"\[<[0-9;]*$")
    _CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._leak_buf = ""

    def _flush_leak_buf(self) -> None:
        """priority 绑定（enter/newline/tab）绕过 _on_key，需在此回补残留缓冲。"""
        if self._leak_buf:
            self.insert(self._leak_buf)
            self._leak_buf = ""

    async def _on_key(self, event: Key) -> None:
        ch = event.character
        if ch is not None and self._CONTROL_RE.search(ch):
            event.stop()
            event.prevent_default()
            return
        if ch is None:
            # 非可打印键（enter/方向键等）：单个 `[` 回补，多字符残留丢弃
            if len(self._leak_buf) == 1:
                self.insert(self._leak_buf)
            self._leak_buf = ""
            await super()._on_key(event)
            return
        if self._leak_buf:
            candidate = self._leak_buf + ch
            if self._LEAK_FULL_RE.fullmatch(candidate):
                self._leak_buf = ""  # 命中鼠标序列 → 整段丢弃
                event.stop()
                event.prevent_default()
                return
            if self._LEAK_PREFIX_RE.match(candidate):
                self._leak_buf = candidate  # 继续收集
                event.stop()
                event.prevent_default()
                return
            buf, self._leak_buf = self._leak_buf, ""  # 不是泄漏 → 回补缓冲
            if buf:
                self.insert(buf)
        elif ch == "[":
            self._leak_buf = "["
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class TabComplete(Message):
        """Tab 请求补全（交给 App 处理，mewcode 同款模式）。"""

    def on_mount(self) -> None:
        self.border_title = "输入"
        self.placeholder = "输入消息，/ 开头为命令（/help 查看）"

    def action_submit(self) -> None:
        self._flush_leak_buf()
        text = self.text.strip()
        if not text:
            return  # 空输入不发 API，防误触（05 §3.3）
        self.post_message(self.Submitted(text))
        self.text = ""

    def action_newline(self) -> None:
        self._flush_leak_buf()
        self.insert("\n")

    def action_complete(self) -> None:
        """Tab：发出 TabComplete 消息，App 侧做 / 前缀补全。"""
        self._flush_leak_buf()
        self.post_message(self.TabComplete())


class KDApp(App[None]):
    """KDAgent 主应用。实现 UIController，命令系统直接注入本实例。"""

    TITLE = "KDAgent"
    SUB_TITLE = "能跑档 · M1-e"
    CSS = _CSS
    BINDINGS = [
        Binding("escape", "cancel_agent", "取消", show=False),
        Binding("ctrl+c", "request_quit", "退出", show=False),
        Binding("tab", "complete_command", "补全", show=False),
    ]
    ENABLE_COMMAND_PALETTE = False

    def __init__(
        self,
        *,
        config: Config,
        llm: LLMClient,
        conversation: ConversationManager,
        tools: ToolRegistry,
        work_dir: Path,
        sessions_dir: Path | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ) -> None:
        super().__init__()
        self._config = config
        self._work_dir = work_dir
        self._session_manager = SessionManager(sessions_dir or Path(".kdagent") / "sessions")
        # 当前会话（M1-f：App 持 Session，Agent 对话实时落盘，/session 切换）。
        self._session = self._session_manager.create(conversation)
        # 属性名不用 `_registry`：Textual 内部也有 `_registry`（其 CommandRegistry），
        # 同名会覆盖冲突（启动即崩）。
        self._commands = build_default_commands()
        self._agent = Agent(
            config=config,
            llm=llm,
            conversation=self._session.conversation,
            tools=tools,
            events=self._on_event,
            work_dir=work_dir,
            system_prompt=system_prompt,
            confirm=self._confirm,
            todos=self._on_todos,
            on_conversation_change=self._on_conversation_change,
        )
        self._default_prompt = system_prompt
        self._agent_worker: Worker[Any] | None = None
        self._total_usage = Usage()
        self._plan_mode = False
        # widget 在 on_mount 后可用；mypy 不知道，先声明为 None 后按需断言
        self._chat: ChatView | None = None
        self._tools_region: ToolRegion | None = None
        self._todo_region: TodoRegion | None = None
        self._status: StatusBar | None = None
        self._input: ChatInput | None = None

    # ---- 布局与生命周期 ---------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield ChatView(id="chat")
        yield TodoRegion(id="todo")
        yield ToolRegion(id="tools")
        yield ChatInput(id="input")
        yield StatusBar(id="status")

    def on_mount(self) -> None:
        self._chat = self.query_one("#chat", ChatView)
        self._tools_region = self.query_one("#tools", ToolRegion)
        self._todo_region = self.query_one("#todo", TodoRegion)
        self._status = self.query_one("#status", StatusBar)
        self._input = self.query_one("#input", ChatInput)
        self._input.focus()
        self.refresh_status()

    # ---- 系统剪贴板（Textual 默认是进程内 _clipboard，不接系统剪贴板） -------

    @property
    def clipboard(self) -> str:
        """读取系统剪贴板；失败降级为 Textual 进程内值（headless/无剪贴板）。"""
        try:
            return str(pyperclip.paste())
        except Exception:
            return self._clipboard

    def copy_to_clipboard(self, text: str) -> None:
        """写系统剪贴板；失败降级为 Textual 进程内值。"""
        self._clipboard = text
        with contextlib.suppress(Exception):
            pyperclip.copy(text)

    # ---- AgentEvent 消费（05 §3.2 映射表） ---------------------------------

    def _on_event(self, ev: AgentEvent) -> None:
        chat = self._chat
        tools = self._tools_region
        if chat is None or tools is None:
            return  # 事件在 widget 挂载前到达（不应发生），静默丢弃
        if isinstance(ev, StreamTextEvent):
            chat.append_stream(ev.text)
        elif isinstance(ev, ToolUseEvent):
            chat.finish_stream()  # 流式文本到此结束 → 渲染 markdown，再显示工具
            tools.show_running(ev.name, ev.input)
        elif isinstance(ev, ToolResultEvent):
            tools.show_result(ev.name, ev.content, ev.is_error, ev.duration_ms)
        elif isinstance(ev, UsageEvent):
            self._accumulate_usage(ev.usage)
            self.refresh_status()
        elif isinstance(ev, TurnCompleteEvent):
            chat.finish_stream()
            chat.append_system(f"— 第 {ev.turn + 1} 轮完成 —")
        elif isinstance(ev, LoopCompleteEvent):
            chat.finish_stream()
            chat.append_system(f"✔ 完成（{ev.turns} 轮）")
            tools.reset()
            chat.scroll_end(animate=False)
        elif isinstance(ev, ErrorEvent):
            chat.finish_stream()
            chat.append_error(ev.error)
            chat.append_system("输入 /help 查看命令")
        elif isinstance(ev, CancelledEvent):
            chat.finish_stream()
            chat.append_system("已取消。")
        elif isinstance(ev, MaxIterationsReachedEvent):
            chat.finish_stream()
            chat.append_error(f"达到迭代上限（{ev.limit} 轮），已强制停止。")

    def _accumulate_usage(self, usage: Usage) -> None:
        self._total_usage = Usage(
            input_tokens=self._total_usage.input_tokens + usage.input_tokens,
            output_tokens=self._total_usage.output_tokens + usage.output_tokens,
            cache_read_tokens=self._total_usage.cache_read_tokens + usage.cache_read_tokens,
            cache_creation_tokens=self._total_usage.cache_creation_tokens
            + usage.cache_creation_tokens,
        )

    # ---- TodoWrite 数据流（03 §3.6）：归一化 → 会话状态 + 面板渲染 -----------

    def _on_todos(self, raw_todos: list[dict[str, Any]]) -> None:
        """TodoWrite 回调：更新当前 Session 的 todos 并实时渲染面板。"""
        items = todo_items_from_raw(raw_todos)
        self._session.set_todos(items)
        todo_region = self._todo_region
        if todo_region is not None:
            todo_region.show_todos(items)

    def _on_conversation_change(self) -> None:
        """每落一条消息后实时写盘（04 §3.2 实时落盘；todos 随最新记录落盘）。"""
        self._session.flush_last()

    # ---- 用户输入：命令快车道 vs Agent Loop（05 §3.3） ---------------------

    @on(ChatInput.Submitted)
    def _on_input_submitted(self, event: ChatInput.Submitted) -> None:
        text = event.text
        if not text.strip():
            return  # 空输入不发 API，防误触（05 §3.3）；输入框已在 action_submit 清空
        name, args, is_cmd = parse_command(text)
        if is_cmd:
            self.dispatch_command(name, args)
            return
        self.send_user_message(text)

    @on(ChatInput.TabComplete)
    def _on_tab_complete(self, event: ChatInput.TabComplete) -> None:
        self.action_complete_command()

    def send_user_message(self, text: str) -> None:
        chat = self._chat
        if chat is not None:
            chat.append_user(text)
        self._agent_worker = self.run_agent(text)

    def dispatch_command(self, name: str, args: str) -> None:
        if not name:
            self.dispatch_command("help", "")  # 只输 / → 列命令
            return
        cmd = self._commands.find(name)
        if cmd is None or cmd.handler is None:
            chat = self._chat
            if chat is not None:
                chat.append_error(f"未知命令：/{name}，输入 /help 查看可用命令")
            return
        cmd.handler(self._make_ctx(args))

    def _make_ctx(self, args: str) -> CommandContext:
        return CommandContext(
            args=args,
            agent=self._agent,
            conversation=self._agent.conversation,
            session=self._session,
            ui=self,
            config=self._config,
            registry=self._commands,
            session_manager=self._session_manager,
        )

    @work(group="agent", exclusive=False)
    async def run_agent(self, text: str) -> None:
        """Agent Loop 的 Textual worker。事件经 `_on_event` sink 回流 UI。"""
        try:
            await self._agent.run(text)
        except asyncio.CancelledError:
            pass  # Agent 内部已发 CancelledEvent 并落库
        except Exception as exc:
            chat = self._chat
            if chat is not None:
                chat.append_error(f"Agent 异常：{exc}")

    # ---- 确认钩子（05 §3.4）：Y/N 前置 ------------------------------------

    async def _confirm(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        """UI 提供的确认钩子：ModalScreen 等待用户 Y/N；Esc 视为拒绝。"""
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        def _on_result(result: bool | None) -> None:
            future.set_result(bool(result))

        self.push_screen(ConfirmDialog(tool_name, tool_input), _on_result)
        return await future

    # ---- UIController 实现（命令系统依赖注入背包含 self） ------------------

    def add_system_message(self, text: str) -> None:
        chat = self._chat
        if chat is not None:
            chat.append_system(text)

    def set_plan_mode(self, enabled: bool) -> None:
        self._plan_mode = enabled
        self._agent.set_system_prompt(PLAN_SYSTEM_PROMPT if enabled else self._default_prompt)
        self.refresh_status()

    def is_plan_mode(self) -> bool:
        return self._plan_mode

    def get_token_count(self) -> int:
        u = self._total_usage
        return u.input_tokens + u.output_tokens + u.cache_read_tokens + u.cache_creation_tokens

    def refresh_status(self) -> None:
        status = self._status
        if status is None:
            return
        mode = "PLAN" if self._plan_mode else "DEFAULT"
        status.update_status(
            mode=mode,
            token_count=self.get_token_count(),
            tool_count=self._agent.tool_count,
            work_dir=str(self._work_dir),
        )

    def clear_chat(self) -> None:
        chat = self._chat
        if chat is not None:
            chat.clear_messages()

    def set_active_session(self, session: Session | None) -> None:
        """切换当前会话（/session new/resume）：换 conversation + 恢复对话/todo。"""
        if session is None:
            return
        self._session = session
        self._agent.set_conversation(session.conversation)
        chat = self._chat
        if chat is not None:
            chat.load_conversation(session.conversation.messages)
        todo_region = self._todo_region
        if todo_region is not None:
            if session.todos:
                todo_region.show_todos(session.todos)
            else:
                todo_region.reset()
        self.refresh_status()

    def request_exit(self) -> None:
        self.push_screen(ExitDialog(), self._maybe_exit)

    def _maybe_exit(self, confirmed: bool | None) -> None:
        if confirmed:
            self.exit()

    # ---- 键盘绑定 actions --------------------------------------------------

    def action_cancel_agent(self) -> None:
        """Esc：取消当前 Agent 循环，程序不退（05 §3.3）。"""
        worker = self._agent_worker
        if worker is not None and worker.is_running:
            worker.cancel()

    def action_request_quit(self) -> None:
        """Ctrl+C：二次确认退出（05 §3.3）。"""
        self.request_exit()

    def action_complete_command(self) -> None:
        """Tab：/ 后前缀补全；单选补全，多选列候选（05 §3.5）。"""
        input_bar = self._input
        if input_bar is None or not input_bar.text.startswith("/"):
            return
        rest = input_bar.text[1:].strip()
        prefix = rest.split()[0] if rest else ""
        matches = self._commands.complete(prefix)
        if len(matches) == 1:
            cmd_name = matches[0]
            input_bar.text = f"/{cmd_name}" if cmd_name != prefix else f"/{cmd_name} "
            input_bar.cursor_location = (0, len(input_bar.text))
        elif len(matches) > 1:
            chat = self._chat
            if chat is not None:
                chat.append_system("候选：/ " + " /".join(matches))
