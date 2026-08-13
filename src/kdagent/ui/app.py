"""KDAgent TUI 主体（规格 05 §3.1-3.4）。

KDApp 同时实现 UIController（05 §3.5）——命令系统的依赖注入背包含它自己。
事件流（02 AgentEvent）由 sync sink 消费，worker 与 UI 同处主事件循环，线程安全。

M1-e 能跑档：三区域布局 + 事件渲染 + 极简 Y/N + Slash 7 命令 + Esc/Ctrl+C/Tab。
M1-f：接入 SessionManager 持久会话；TodoRegion。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Header, Input
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
from kdagent.sessions.manager import SessionManager
from kdagent.tools.registry import ToolRegistry
from kdagent.ui.chat import ChatView
from kdagent.ui.commands import CommandContext, build_default_commands, parse_command
from kdagent.ui.confirm import ConfirmDialog, ExitDialog
from kdagent.ui.statusbar import StatusBar
from kdagent.ui.toolregion import ToolRegion

PLAN_SYSTEM_PROMPT = (
    "你是 KDAgent。当前处于 Plan 模式：只读探索、制定方案，"
    "不要修改文件或执行有副作用的操作。"
)

# 三区域布局：对话区 1fr、工具活动区固定、输入框与状态栏底部（05 §3.1）
_CSS = """
#chat { height: 1fr; border: round $primary; margin: 0 1; }
#tools { height: 6; border: round $secondary; margin: 0 1; }
#input { height: 3; margin: 0 1; }
#status { height: 1; background: $panel; color: $text; padding: 0 1; }
#dialog { width: 60; height: 9; border: thick $primary;
          background: $surface; padding: 1 2; align: center middle; }
#dialog-title { text-align: center; }
#dialog-args { text-align: center; margin: 1 0; color: $text-muted; }
#dialog-actions { align: center middle; }
#dialog-actions Button { margin: 0 1; }
"""


class InputBar(Input):
    """用户输入框：/ 开头走命令快车道。"""

    def on_mount(self) -> None:
        self.border_title = "输入"
        self.placeholder = "输入消息，/ 开头为命令（/help 查看）"


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
        # 属性名不用 `_registry`：Textual 内部也有 `_registry`（其 CommandRegistry），
        # 同名会覆盖冲突（启动即崩）。
        self._commands = build_default_commands()
        self._agent = Agent(
            config=config,
            llm=llm,
            conversation=conversation,
            tools=tools,
            events=self._on_event,
            work_dir=work_dir,
            system_prompt=system_prompt,
            confirm=self._confirm,
        )
        self._default_prompt = system_prompt
        self._agent_worker: Worker[Any] | None = None
        self._total_usage = Usage()
        self._plan_mode = False
        # widget 在 on_mount 后可用；mypy 不知道，先声明为 None 后按需断言
        self._chat: ChatView | None = None
        self._tools_region: ToolRegion | None = None
        self._status: StatusBar | None = None
        self._input: InputBar | None = None

    # ---- 布局与生命周期 ---------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield ChatView(id="chat")
        yield ToolRegion(id="tools")
        yield InputBar(id="input")
        yield StatusBar(id="status")

    def on_mount(self) -> None:
        self._chat = self.query_one("#chat", ChatView)
        self._tools_region = self.query_one("#tools", ToolRegion)
        self._status = self.query_one("#status", StatusBar)
        self._input = self.query_one("#input", InputBar)
        self._input.focus()
        self.refresh_status()

    # ---- AgentEvent 消费（05 §3.2 映射表） ---------------------------------

    def _on_event(self, ev: AgentEvent) -> None:
        chat = self._chat
        tools = self._tools_region
        if chat is None or tools is None:
            return  # 事件在 widget 挂载前到达（不应发生），静默丢弃
        if isinstance(ev, StreamTextEvent):
            chat.append_stream(ev.text)
        elif isinstance(ev, ToolUseEvent):
            tools.show_running(ev.name, ev.input)
        elif isinstance(ev, ToolResultEvent):
            tools.show_result(ev.name, ev.content, ev.is_error, ev.duration_ms)
        elif isinstance(ev, UsageEvent):
            self._accumulate_usage(ev.usage)
            self.refresh_status()
        elif isinstance(ev, TurnCompleteEvent):
            chat.append_system(f"— 第 {ev.turn + 1} 轮完成 —")
        elif isinstance(ev, LoopCompleteEvent):
            chat.append_system(f"✔ 完成（{ev.turns} 轮）")
            tools.reset()
            chat.scroll_end(animate=False)
        elif isinstance(ev, ErrorEvent):
            chat.append_error(ev.error)
            chat.append_system("输入 /help 查看命令")
        elif isinstance(ev, CancelledEvent):
            chat.append_system("已取消。")
        elif isinstance(ev, MaxIterationsReachedEvent):
            chat.append_error(f"达到迭代上限（{ev.limit} 轮），已强制停止。")

    def _accumulate_usage(self, usage: Usage) -> None:
        self._total_usage = Usage(
            input_tokens=self._total_usage.input_tokens + usage.input_tokens,
            output_tokens=self._total_usage.output_tokens + usage.output_tokens,
            cache_read_tokens=self._total_usage.cache_read_tokens + usage.cache_read_tokens,
            cache_creation_tokens=self._total_usage.cache_creation_tokens
            + usage.cache_creation_tokens,
        )

    # ---- 用户输入：命令快车道 vs Agent Loop（05 §3.3） ---------------------

    @on(Input.Submitted)
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value
        if not text.strip():
            return  # 空输入不发 API，防误触（05 §3.3）
        input_bar = self._input
        if input_bar is not None:
            input_bar.value = ""
        name, args, is_cmd = parse_command(text)
        if is_cmd:
            self.dispatch_command(name, args)
            return
        self.send_user_message(text)

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
            session=None,
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
        if input_bar is None or not input_bar.value.startswith("/"):
            return
        rest = input_bar.value[1:].strip()
        prefix = rest.split()[0] if rest else ""
        matches = self._commands.complete(prefix)
        if len(matches) == 1:
            cmd_name = matches[0]
            input_bar.value = f"/{cmd_name}" if cmd_name != prefix else f"/{cmd_name} "
            input_bar.cursor_position = len(input_bar.value)
        elif len(matches) > 1:
            chat = self._chat
            if chat is not None:
                chat.append_system("候选：/ " + " /".join(matches))
