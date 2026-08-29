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
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pyperclip  # type: ignore[import-untyped]
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import Key
from textual.message import Message
from textual.widgets import Header, TextArea
from textual.worker import Worker

from kdagent.config import Config, load_config
from kdagent.context.compactor import WINDOW_SIZE, estimate_messages_tokens, estimate_tokens
from kdagent.context.context_manager import ContextManager
from kdagent.engine.agent import DEFAULT_SYSTEM_PROMPT, Agent
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.events import (
    AgentEvent,
    CancelledEvent,
    ErrorEvent,
    LoopCompleteEvent,
    MaxIterationsReachedEvent,
    PermissionRequestEvent,
    PermissionVerdict,
    StreamTextEvent,
    TestingEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnCompleteEvent,
    UsageEvent,
)
from kdagent.engine.llm.base import LLMClient, Payload, Usage
from kdagent.engine.messages import Message as EngineMessage
from kdagent.engine.messages import TextBlock
from kdagent.hooks.engine import HookEngine
from kdagent.hooks.engine_types import HookContext
from kdagent.mcp.manager import MCPManager
from kdagent.memory.consolidator import MemoryConsolidator
from kdagent.memory.extractor import MemoryExtractor
from kdagent.memory.store import MemoryStore
from kdagent.obs import OTLPSpanExporter, SpanExporter, Telemetry
from kdagent.permission.checker import PermissionChecker
from kdagent.permission.modes import Mode
from kdagent.sessions.manager import Session, SessionManager, SessionMeta
from kdagent.sessions.records import todo_items_from_raw
from kdagent.skill.manager import SkillManager
from kdagent.subagent.agent_tool import Agent as SubAgentTool
from kdagent.subagent.manager import AgentManager
from kdagent.subagent.task import TaskManager
from kdagent.subagent.worktree import WorktreeManager
from kdagent.tools.registry import ToolRegistry
from kdagent.ui.chat import ChatView
from kdagent.ui.commands import (
    CommandContext,
    build_default_commands,
    format_compact_report,
    parse_command,
    register_skill_commands,
)
from kdagent.ui.confirm import (
    ConfirmDialog,
    ExitDialog,
    PermissionDialog,
    SessionPickerDialog,
)
from kdagent.ui.evalreport import EvalReportScreen
from kdagent.ui.metricsscreen import MetricsScreen
from kdagent.ui.statusbar import StatusBar
from kdagent.ui.todoregion import TodoRegion
from kdagent.ui.toolregion import ToolRegion

PLAN_SYSTEM_PROMPT = (
    "你是 KDAgent。当前处于 Plan 模式：只读探索、制定方案，不要修改文件或执行有副作用的操作。"
)

# Claude Code 风格布局（05 §3.1）：Chat 主区 1fr，todo/tools 有内容才展开，
# 输入框与状态栏固定底部。弹窗样式在 ui/confirm.py（ModalScreen 内）。
_CSS = """
#chat { height: 1fr; border: round $primary; margin: 0 1; }
#todo { height: auto; max-height: 8; border: round $accent; margin: 0 1; }
#tools { height: auto; max-height: 10; border: round $secondary; margin: 0 1; }
#input { height: 4; margin: 0 1; }
#status { height: 1; background: $panel; color: $text; padding: 0 1; }
"""


class ChatInput(TextArea):
    """用户输入框（TextArea 而非 Input）：IME 组合输入支持更完整（参考 mewcode）。

    `priority=True` 关键：widget 层绑定优先于 Textual Screen 默认的
    `tab=app.focus_next` / App 层 `ctrl+c=退出`，避免抢键。
    ctrl+c/ctrl+v 用 TextArea 内置绑定（经 app 系统剪贴板）。
    """

    # 输入框高度上下限（U2）：至少 4 行，最多 10 行，随内容自动扩展。
    _HEIGHT_MIN = 4
    _HEIGHT_MAX = 10

    BINDINGS = [
        Binding("enter", "submit", "提交", priority=True),
        Binding("shift+enter", "newline", "换行", priority=True),
        Binding("ctrl+j", "newline", "换行", priority=True),
        Binding("tab", "complete", "补全", priority=True),
        # U4：↑/↓ 历史输入切换（类似 shell history）。光标在首行/末行时才翻历史，
        # 中间行保留 TextArea 的光标上下移动——避免劫持多行编辑的光标导航。
        Binding("up", "history_prev", "上一条输入", priority=True),
        Binding("down", "history_next", "下一条输入", priority=True),
        # M1-i2：禁用 Kitty（compat.py）后 Windows 传统模式 backspace 发 \x08 → Textual
        # 解析为 key='ctrl+h'，而 TextArea 只有 backspace(=\x7f) 绑定 → 加 ctrl+h 兜底，
        # \x08 与 \x7f 两路都能删除。Textual BINDINGS 会合并父类（dom._merge_bindings），
        # 此处显式声明不丢 TextArea 的编辑绑定。
        Binding("ctrl+h", "delete_left", "删除", priority=True),
        # 显式 priority 复制/粘贴：焦点在输入框时防 App 层 ctrl+c=request_quit 抢键。
        Binding("ctrl+c", "copy", "复制", priority=True),
        Binding("ctrl+v", "paste", "粘贴", priority=True),
    ]

    # 鼠标序列泄漏防御（M1-i 加固）：完整 \x1b[<...M 会被 Textual 解析为 MouseEvent，
    # 但缺 ESC 前缀的 `[<35;56;28m`（Windows 终端丢 ESC/协议不全）会被拆成
    # 逐个可打印字符 Key 插入。单字符过滤挡不住 → 用状态机：`[` 后跟 `<数字;数字` 到
    # `m/M` 闭合才判定为泄漏整段丢弃；否则回补缓冲字符（正常输入仅延迟一个 `[`）。
    _LEAK_FULL_RE = re.compile(r"\[<[0-9;]*[Mm]")
    _LEAK_PREFIX_RE = re.compile(r"\[<[0-9;]*$")
    # M1-i2 收窄：原 `[\x00-\x1f\x7f-\x9f]` 误杀 backspace（\x08/\x7f）与中文
    # UTF-8 续字节（\x80-\x9f）→ 仅挡 ESC(\x1b) 与 8-bit CSI(\x9b)（鼠标序列泄漏的
    # control 头）；删除/换行/Tab 及 IME 字节均放行。
    _CONTROL_RE = re.compile(r"[\x1b\x9b]")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._leak_buf = ""
        # U4 历史输入：提交过的命令队列 + 浏览游标（-1 = 未浏览，回退到草稿）。
        self._history: list[str] = []
        self._hist_index = -1
        self._draft = ""

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
        self._apply_height()

    @on(TextArea.Changed)
    def _on_text_changed(self, event: TextArea.Changed) -> None:
        """U2：内容变化 → 按行数自动扩展高度（下限 4 行、上限 10 行）。"""
        self._apply_height()

    def _apply_height(self) -> None:
        """根据当前行数设定输入框高度（TextArea height 需手动跟随内容）。

        行数 = 换行符数量 + 1；长行不换行（TextArea 横向滚动），故只按硬换行计。
        """
        lines = len(self.text.split("\n"))
        target = min(max(lines + 1, self._HEIGHT_MIN), self._HEIGHT_MAX)
        self.styles.height = target

    def action_submit(self) -> None:
        self._flush_leak_buf()
        text = self.text.strip()
        if not text:
            return  # 空输入不发 API，防误触（05 §3.3）
        # U4：非重复输入才入历史（同文本连续提交去重），浏览游标复位。
        if not self._history or self._history[-1] != text:
            self._history.append(text)
        self._hist_index = -1
        self._draft = ""
        self.post_message(self.Submitted(text))
        self.text = ""
        self._apply_height()

    def action_newline(self) -> None:
        self._flush_leak_buf()
        self.insert("\n")
        self._apply_height()

    def _line_count(self) -> int:
        """当前文本行数（按硬换行计；长行横向滚动不换行）。"""
        return len(self.text.split("\n"))

    def action_history_prev(self) -> None:
        """U4 ↑：光标在首行翻上一条历史；否则仅移动光标到首行（保留编辑）。"""
        row, _ = self.cursor_location
        if row > 0:
            self.move_cursor((0, 0), select=False)
            return
        if not self._history:
            return
        if self._hist_index == -1:
            self._draft = self.text  # 首次进入历史浏览，暂存当前草稿
        if self._hist_index < len(self._history) - 1:
            self._hist_index += 1
            self.text = self._history[len(self._history) - 1 - self._hist_index]
            self.cursor_location = (self._line_count() - 1, 0)
            self._apply_height()

    def action_history_next(self) -> None:
        """U4 ↓：光标在末行翻下一条/回草稿；否则仅移动光标到末行。"""
        row, _ = self.cursor_location
        if row < self._line_count() - 1:
            self.move_cursor((self._line_count() - 1, 0), select=False)
            return
        if not self._history:
            return
        if self._hist_index > 0:
            self._hist_index -= 1
            self.text = self._history[len(self._history) - 1 - self._hist_index]
        elif self._hist_index == 0:
            self._hist_index = -1
            self.text = self._draft  # 回到底部 → 恢复最初草稿
        self.cursor_location = (self._line_count() - 1, 0)
        self._apply_height()

    def action_complete(self) -> None:
        """Tab：发出 TabComplete 消息，App 侧做 / 前缀补全。"""
        self._flush_leak_buf()
        self.post_message(self.TabComplete())


def _build_telemetry(config: Config, obs_dir: Path | None) -> Telemetry | None:
    """按配置组装 07 Telemetry：exporter 可插拔（otel.enabled）、脱敏规则、全文日志。"""
    if obs_dir is None:
        return None
    exporter: SpanExporter | None = None
    if bool(config.otel.get("enabled")):
        exporter = OTLPSpanExporter(str(config.otel.get("endpoint", "")))
    sanitize = config.obs.get("sanitize")
    return Telemetry(
        obs_dir,
        exporter=exporter,
        sanitize_rules=sanitize if isinstance(sanitize, dict) else None,
        log_full_prompt=bool(config.debug.get("log_full_prompt", False)),
    )


class KDApp(App[None]):
    """KDAgent 主应用。实现 UIController，命令系统直接注入本实例。"""

    TITLE = "KDAgent"
    SUB_TITLE = "可控档 · M3-d"
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
        model_name: str = "",
        obs_dir: Path | None = None,
        context_manager: ContextManager | None = None,
        permission_checker: PermissionChecker | None = None,
        hooks: HookEngine | None = None,
        memory_store: MemoryStore | None = None,
        memory_extractor: MemoryExtractor | None = None,
        memory_consolidator: MemoryConsolidator | None = None,
        mcp_manager: MCPManager | None = None,
        skill_manager: SkillManager | None = None,
        task_manager: TaskManager | None = None,
        agent_manager: AgentManager | None = None,
        worktree_manager: WorktreeManager | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._work_dir = work_dir
        self._session_manager = SessionManager(
            sessions_dir or Path(".kdagent") / "sessions", obs_dir=obs_dir
        )
        # 当前会话（M1-f：App 持 Session，Agent 对话实时落盘，/session 切换）。
        self._session = self._session_manager.create(conversation)
        # 07 埋点 sink（M2-d）：trace 落盘 {kdagent_dir}/obs/traces/{sid}/。
        self._telemetry = _build_telemetry(config, obs_dir)
        # 07 §3.7 T9：/metrics 面板聚合源（obs_dir None = 未启用可观测性）。
        self._obs_dir = obs_dir
        # 属性名不用 `_registry`：Textual 内部也有 `_registry`（其 CommandRegistry），
        # 同名会覆盖冲突（启动即崩）。
        self._commands = build_default_commands()
        self._tools = tools  # 10 M5-a：Agent 工具父对话绑定（SubAgentTool 取回）
        self._llm = llm  # U3 自动标题：轻量单次 LLM 调用（无工具）
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
            session_id=self._session.id,
            model_name=model_name,
            telemetry=self._telemetry,
            context_manager=context_manager,
            permission_checker=permission_checker,
            hooks=hooks,
            memory_store=memory_store,
            memory_extractor=memory_extractor,
            memory_consolidator=memory_consolidator,
            skills=skill_manager,
        )
        if context_manager is not None:
            context_manager.set_session_id(self._session.id)  # 01：落盘目录随初始 sid
            # 07 T8：CM 在 cli 装配（早于 Telemetry 构建），后补接线使 L2 压缩器产 span。
            context_manager.set_telemetry(self._telemetry)
        self._context_manager = context_manager  # 04 §5 恢复③：/session resume 压缩接线
        # 06 M3 可控档：五层裁决器 + Hook 引擎（cli 装配传入，/permissions 切换模式）。
        self._permission_checker = permission_checker
        self._hooks = hooks
        # 09 M4-c 工具生态：MCP Manager（启动即后台连接，on_unmount 关闭）。
        self._mcp_manager = mcp_manager
        # 09 M4-d Skill：SkillManager（/skills 查看清单；LoadSkill 已注册进 registry）。
        self._skill_manager = skill_manager
        # 08 M4-e /memory 命令：记忆查看/管理（概要/list/add/delete/clear）。
        self._memory_store = memory_store
        # 09 §3.9 显式触发：已加载 Skill 自动注册 /name 命令（与内置冲突的跳过）。
        if skill_manager is not None:
            register_skill_commands(self._commands, skill_manager)
        # 10 M5-a SubAgent：后台任务管理器 + Agent 工具父对话绑定（Fork 继承源 +
        # 完成通知注入目标）。TaskManager 与 Agent 工具共享主对话引用——KDApp 持有
        # 唯一 Agent 实例，构造完成后在此接线（cli 阶段无 agent 可绑）。
        self._task_manager = task_manager
        self._agent_manager = agent_manager
        self._worktree_manager = worktree_manager
        if task_manager is not None:
            task_manager.set_parent_conversation(self._agent.conversation)
            # 10 §5 342（D78）：子 Agent 挂父 trace——runner 装配后注入主 telemetry。
            # obs_dir None = 未启用可观测性，不注入（子 Agent 保持无 trace 行为）。
            if self._telemetry is not None:
                task_manager.set_telemetry(self._telemetry)
        agent_tool = self._tools.get("Agent")
        if isinstance(agent_tool, SubAgentTool):
            agent_tool.set_parent_conversation(self._agent.conversation)
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
        self._run_app_hook("startup")  # 06 §3.10：应用生命周期 hook
        self.refresh_status()
        # 09 §3.3 启动即后台连接 MCP Server（失败隔离不阻止启动；懒连会造成
        # 「不知道工具存在 → 不调用 → 永不连接」死循环）。
        if self._mcp_manager is not None:
            asyncio.create_task(self._mcp_manager.connect_all())

    async def on_unmount(self) -> None:
        self._run_app_hook("shutdown")
        if self._mcp_manager is not None:
            await self._mcp_manager.aclose()

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
            self._maybe_auto_title()  # U3：会话无标题时后台一次性总结
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
        elif isinstance(ev, TestingEvent):
            # 05 §5 239 / 02 §5 346：TestRunner 结构化结果三态渲染。
            chat.finish_stream()
            chat.append_testing(ev.status, ev.test_cmd, ev.failed_tests, ev.summary)
        elif isinstance(ev, PermissionRequestEvent):
            self._request_permission(ev)

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
        """每落一条消息后实时写盘（04 §3.2 实时落盘）+ 刷新状态栏窗口占用（05 §3.2）。"""
        self._session.flush_last()
        self.refresh_status()

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
            resume_compact=self._schedule_resume_compact,
            manual_compact=self._schedule_manual_compact,
            mcp_manager=self._mcp_manager,
            skill_manager=self._skill_manager,
            memory_store=self._memory_store,
            task_manager=self._task_manager,
            worktree_manager=self._worktree_manager,
        )

    def _schedule_resume_compact(self, conversation: ConversationManager) -> None:
        """04 §5 恢复③：/session resume 超阈时的压缩入口（同步调度、异步执行）。

        只在事件循环里调度，不在命令 handler 里阻塞等压缩——resume 立即返回，
        压缩任务随后跑；失败静默（主循环阶段 A 会兜底再试）。
        """
        if self._context_manager is None:
            return
        asyncio.create_task(self._run_resume_compact(conversation))

    async def _run_resume_compact(self, conversation: ConversationManager) -> None:
        cm = self._context_manager
        if cm is None:
            return
        # resume 压缩失败不阻塞恢复；01 §6.1 主循环阶段 A 兜底再试。
        with contextlib.suppress(Exception):
            await cm.force_compact(conversation)

    def _schedule_manual_compact(self, conversation: ConversationManager, focus: str) -> None:
        """05 §3.6：/compact 调度入口（同步调度、异步执行，同 resume 压缩模式）。

        focus = /compact 带参的保留重点；压缩后由 `_run_manual_compact` 报前后对比。
        """
        if self._context_manager is None:
            self.add_system_message("上下文压缩能力不可用（未接入 L3 压缩引擎）。")
            return
        asyncio.create_task(self._run_manual_compact(conversation, focus))

    async def _run_manual_compact(
        self, conversation: ConversationManager, focus: str
    ) -> None:
        """执行手动压缩 + 前后 token 对比 + 刷新 ChatView/状态栏（05 §5 验收项）。"""
        cm = self._context_manager
        if cm is None:
            return
        before = self.get_context_tokens()
        try:
            await cm.manual_compact(conversation, focus=focus)
        except Exception as exc:
            chat = self._chat
            if chat is not None:
                chat.append_error(f"压缩失败：{exc}")
            return
        after = self.get_context_tokens()
        self.add_system_message(format_compact_report(before, after))
        chat = self._chat
        if chat is not None:
            chat.load_conversation(conversation.messages)  # 摘要消息替代早期原文
        self.refresh_status()

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

    # ---- U3：自动会话标题（LoopComplete 一次性轻量总结） --------------------

    def _maybe_auto_title(self) -> None:
        """LoopComplete 且会话无标题时触发标题 worker（不打断主循环）。"""
        if self._session.title:
            return  # 已有标题不覆盖（人工/先前自动生成）
        if not any(m.role == "user" for m in self._agent.conversation.messages):
            return  # 无用户消息，不值得起标题
        self._auto_title_worker()

    @work(group="agent", exclusive=False)
    async def _auto_title_worker(self) -> None:
        """轻量标题总结：无工具单次 LLM 调用，收集 text_delta 拼标题。

        输入只取最近 ~4000 字符的 user/assistant 文本（重建为单条 user 消息，
        不传完整历史避免 API 校验/超长）；失败静默（标题是增强信息，不打扰）。
        """
        try:
            conv = self._agent.conversation
            chunks: list[str] = []
            total = 0
            for msg in reversed(conv.messages):
                text = " ".join(
                    b.text for b in msg.content if isinstance(b, TextBlock)
                ).strip()
                if not text:
                    continue
                line = f"{'用户' if msg.role == 'user' else '助手'}：{text}"
                total += len(line)
                chunks.append(line)
                if total > 4000:
                    break
            if not chunks:
                return
            payload = Payload(
                system=(
                    "你是会话标题生成器。根据对话内容用一句话（不超过 15 个字）概括"
                    "会话主题。只输出标题本身：不要引号、不要句号、不要解释。"
                ),
                messages=[
                    EngineMessage(role="user", content=[TextBlock("\n".join(reversed(chunks)))])
                ],
                tools=[],
                max_tokens=30,
            )
            parts: list[str] = []
            async for ev in self._llm.stream_chat(payload):
                if ev.type == "error" and ev.error:
                    return
                if ev.type == "text_delta" and ev.text:
                    parts.append(ev.text)
            title = "".join(parts).strip().strip('"').strip("'").strip()
            title = title.splitlines()[0][:40].strip() if title else ""
            if title:
                self._session.set_title(title)
                self.refresh_status()
        except Exception:
            pass  # 标题是增强信息，失败静默

    # ---- 确认钩子（05 §3.4）：Y/N 前置 ------------------------------------

    async def _confirm(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        """UI 提供的确认钩子：ModalScreen 等待用户 Y/N；Esc 视为拒绝。"""
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        def _on_result(result: bool | None) -> None:
            future.set_result(bool(result))

        self.push_screen(ConfirmDialog(tool_name, tool_input), _on_result)
        return await future

    # ---- 06 M3 可控档：L5 HITL 权限审批 + 应用生命周期 hook ----------------

    def _request_permission(self, ev: PermissionRequestEvent) -> None:
        """L5 HITL（06 §3.7）：弹 PermissionDialog，用户裁决回填 future。

        `_on_event` 由 Agent 事件流同步调用，push_screen 立即返回（不阻塞事件流）；
        Agent 侧阻塞在 `await future`，等用户点完按钮 dismiss 才继续。
        """

        def _on_result(verdict: str | None) -> None:
            result = verdict if verdict is not None else "deny"
            ev.future.set_result(cast(PermissionVerdict, result))

        self.push_screen(PermissionDialog(ev.tool_name, ev.summary), _on_result)

    def _run_app_hook(self, event: str) -> None:
        """应用级生命周期 hook（startup/shutdown）；hooks None = 无自动化。"""
        if self._hooks is not None:
            self._hooks.run(event, HookContext(event=event))

    def open_eval_report(self, run_id: str) -> None:
        """11 §3.4 TUI 版：`/eval report` 打开内嵌评测报告屏（只读浏览 + 批注）。"""
        self.push_screen(EvalReportScreen(run_id, self._work_dir))

    def open_metrics(self) -> None:
        """07 §3.7 T9：`/metrics` 打开聚合指标面板（obs 未启用时提示不叠屏）。"""
        obs_dir = self._obs_dir
        if obs_dir is None:
            self.add_system_message("可观测性未启用（未指定 obs 目录），无法打开 /metrics 面板。")
            return
        self.push_screen(MetricsScreen(obs_dir, self._session.id))

    def set_permission_mode(self, mode: str) -> None:
        """切换五层裁决器模式（/permissions 命令；无 checker 时静默忽略）。

        N2：切换成功即落盘 `{项目}/.kdagent/permissions.mode`，重启恢复
        （见 cli 启动装配——持久化文件优先于 config.permissions.mode）。
        """
        checker = self._permission_checker
        if checker is not None:
            checker.set_mode(cast(Mode, mode))
            self._persist_permission_mode(mode)
            self.refresh_status()

    def _persist_permission_mode(self, mode: str) -> None:
        """N2：把当前模式写入 `{work_dir}/.kdagent/permissions.mode`。"""
        try:
            kd_dir = self._work_dir / (self._config.kdagent_dir or ".kdagent")
            kd_dir.mkdir(parents=True, exist_ok=True)
            (kd_dir / "permissions.mode").write_text(mode, encoding="utf-8")
        except OSError:
            pass  # 落盘失败不阻塞切换（内存模式已生效）

    def get_permission_mode(self) -> str:
        """当前权限模式（无 checker 时显示 default 占位）。"""
        checker = self._permission_checker
        return checker.mode if checker is not None else "default"

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

    def get_context_tokens(self) -> int:
        """当前上下文窗口占用估算（05 §3.2 tokens: x/y k 的 x；压缩前后对比口径）。

        = system prompt + 全部消息（01 §5.4 窗口口径，同 Agent 阶段 A 判定）。
        """
        return estimate_messages_tokens(self._agent.conversation.messages) + estimate_tokens(
            self._agent.system_prompt
        )

    def refresh_status(self) -> None:
        status = self._status
        if status is None:
            return
        mode = "PLAN" if self._plan_mode else "DEFAULT"
        status.update_status(
            mode=mode,
            token_count=self.get_context_tokens(),
            window_size=WINDOW_SIZE,
            tool_count=self._agent.tool_count,
            work_dir=str(self._work_dir),
            permission=self.get_permission_mode(),
        )

    def clear_chat(self) -> None:
        chat = self._chat
        if chat is not None:
            chat.clear_messages()

    def reload_config(self) -> str:
        """重载磁盘配置并应用（/session new：改完 config.yaml 不必重启进程）。

        2026-08-28 953e 实测：`/session new` 不重载配置，进程一直顶格旧
        max_tokens=4096——写大文件时 WriteFile 参数被截断、JSON 解析失败、任务
        终止。Agent 读 `self._config` 是惰性的（`extra.max_tokens` 在组装 payload
        时读），换引用即生效。返回变更提示（无变化返回空串）。
        """
        fresh = load_config(self._work_dir)  # D98：与 build_kdapp 同根，work_dir 而非 cwd
        old_max = self._config.extra.get("max_tokens")
        new_max = fresh.extra.get("max_tokens")
        self._config = fresh
        self._agent.set_config(fresh)
        if new_max != old_max:
            return f"（配置已重载：extra.max_tokens {old_max} → {new_max}）"
        return ""

    def set_active_session(self, session: Session | None) -> None:
        """切换当前会话（/session new/resume）：换 conversation + 恢复对话/todo。"""
        if session is None:
            return
        self._session = session
        self._agent.set_conversation(session.conversation)
        self._agent.set_session_id(session.id)  # 07：trace 关联切换后的 sid
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

    def open_session_picker(
        self, metas: list[SessionMeta], current: Session | None
    ) -> bool:
        """U3：/session list 弹会话切换选单（ListView 上下键 + Enter 切换）。

        返回 True（已接管弹窗）；回调里按选中 sid resume 并切换。会话全部位于
        当前 session_manager 时才有意义——无 manager 直接返回 False 降级。
        """
        if not metas:
            return False
        current_sid = current.id if current is not None else ""
        items = [
            (
                m.sid,
                f"{m.title}  「{m.sid}」 活跃 "
                + datetime.fromtimestamp(m.last_active_ts).strftime("%m-%d %H:%M")
                if m.title
                else f"{m.sid}  活跃 "
                + datetime.fromtimestamp(m.last_active_ts).strftime("%m-%d %H:%M"),
            )
            for m in metas[:20]
        ]

        def _on_picked(sid: str | None) -> None:
            if sid is None or self._session_manager is None:
                return
            try:
                session = self._session_manager.resume(sid, compact=self._schedule_resume_compact)
            except Exception as exc:
                chat = self._chat
                if chat is not None:
                    chat.append_error(f"恢复会话失败：{exc}")
                return
            self.set_active_session(session)
            self.add_system_message(f"已切换会话：{sid}")

        self.push_screen(SessionPickerDialog(items, current_sid), _on_picked)
        return True

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
