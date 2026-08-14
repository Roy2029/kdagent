"""Slash Command 框架（规格 05 §3.5-3.6）。

定位：让"清屏、查 token、切模式"这类无需 AI 的操作绕过 Agent Loop——杀鸡焉用牛刀。
解决注册、解析、执行三大问题；命令不感知 UI 框架（UIController Protocol 隔离）。

三类命令：local（不走 Loop）、local-ui（同样本地但改 UI 状态）、prompt（构造 prompt 转交 Agent）。
M1-e 能跑档内置 7 个：/help /status /compact /clear /plan /session /exit。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from kdagent.config import Config
from kdagent.engine.agent import Agent
from kdagent.engine.conversation import ConversationManager
from kdagent.sessions.manager import Session, SessionManager

CommandType = Literal["local", "local-ui", "prompt"]


class UIController(Protocol):
    """命令不感知 UI 框架（05 §3.5）。KDApp 实现此协议。"""

    def add_system_message(self, text: str) -> None: ...
    def send_user_message(self, text: str) -> None: ...
    def set_plan_mode(self, enabled: bool) -> None: ...
    def is_plan_mode(self) -> bool: ...
    def get_token_count(self) -> int: ...
    def refresh_status(self) -> None: ...
    def clear_chat(self) -> None: ...
    def request_exit(self) -> None: ...
    def set_active_session(self, session: Session | None) -> None: ...


@dataclass
class CommandContext:
    """依赖注入背包，隔离 UI 实现细节（05 §3.5）。"""

    args: str
    agent: Agent
    conversation: ConversationManager
    session: Session | None
    ui: UIController
    config: Config
    registry: CommandRegistry | None = None
    session_manager: SessionManager | None = None


@dataclass
class Command:
    name: str
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    usage: str = ""
    type: CommandType = "local"
    arg_prompt: str = ""  # 缺参时的提示，比"参数缺失"友好
    hidden: bool = False
    handler: Callable[[CommandContext], None] | None = None


class CommandRegistry:
    """注册 / 查找（先命令名后别名）/ Tab 补全。

    别名冲突启动时检测（05 §3.5）：注册即 fail fast，别等用户用时行为不确定。
    """

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._by_alias: dict[str, str] = {}

    def register(self, cmd: Command) -> None:
        name = cmd.name.lower()
        if name in self._commands:
            raise ValueError(f"命令重名：/{name}")
        keys = [name, *[a.lower() for a in cmd.aliases]]
        for key in keys:
            if key in self._commands or key in self._by_alias:
                raise ValueError(f"命令/别名冲突：/{key}")
        self._commands[name] = cmd
        for alias in cmd.aliases:
            self._by_alias[alias.lower()] = name

    def find(self, name: str) -> Command | None:
        key = name.lower()
        canonical = self._by_alias.get(key, key)
        return self._commands.get(canonical)

    def complete(self, prefix: str) -> list[str]:
        """前缀匹配命令名+别名，返回候选（命令名形式）。"""
        keys = {*self._commands.keys(), *self._by_alias.keys()}
        return sorted(k for k in keys if k.startswith(prefix.lower()))

    def all(self) -> list[Command]:
        return sorted(self._commands.values(), key=lambda c: c.name)


def parse_command(text: str) -> tuple[str, str, bool]:
    """`/name args` → (name, args, True)；非命令 → ("", "", False)。

    只输入 `/` → 空命令，命令系统列清单（05 §3.5）。
    """
    if not text.startswith("/"):
        return "", "", False
    stripped = text[1:].strip()
    if not stripped:
        return "", "", True
    parts = stripped.split(maxsplit=1)
    name = parts[0].lower()  # /Help = /help
    args = parts[1].strip() if len(parts) > 1 else ""
    return name, args, True


# ---- 内置命令 handler -----------------------------------------------------


def _cmd_help(ctx: CommandContext) -> None:
    if ctx.args:
        cmd = ctx.registry.find(ctx.args) if ctx.registry else None
        if cmd is None:
            ctx.ui.add_system_message(f"未知命令：/{ctx.args}，输入 /help 查看可用命令")
            return
        lines = [f"/{cmd.name}", f"  说明：{cmd.description}"]
        if cmd.aliases:
            lines.append(f"  别名：{'、'.join(cmd.aliases)}")
        if cmd.usage:
            lines.append(f"  用法：{cmd.usage}")
        ctx.ui.add_system_message("\n".join(lines))
        return
    cmds = ctx.registry.all() if ctx.registry else []
    lines = ["可用命令："]
    for cmd in cmds:
        if not cmd.hidden:
            lines.append(f"  /{cmd.name:<10} {cmd.description}")
    lines.append("/help <命令> 查看详情；/exit 或 Ctrl+C 退出")
    ctx.ui.add_system_message("\n".join(lines))


def _cmd_status(ctx: CommandContext) -> None:
    mode = "PLAN" if ctx.ui.is_plan_mode() else "DEFAULT"
    token = ctx.ui.get_token_count()
    lines = [
        f"模式：{mode}",
        f"Token：{token}",
        f"工具：{ctx.agent.tool_count} 个",
    ]
    if ctx.session is not None:
        lines.append(f"会话：{ctx.session.id}")
    ctx.ui.add_system_message("\n".join(lines))


def _cmd_compact(ctx: CommandContext) -> None:
    token = ctx.ui.get_token_count()
    if token < 5000:
        ctx.ui.add_system_message("当前 token 低于 5K，无需压缩。")
        return
    # 01 三层压缩 M2 落地（13 路线图 M2-c）；M1-e 提供占位与前置检查。
    ctx.ui.add_system_message(
        "上下文压缩引擎（01 三层压缩）在 M2 落地，当前仅做 token 检查。"
    )


def _cmd_clear(ctx: CommandContext) -> None:
    ctx.ui.clear_chat()
    ctx.ui.add_system_message("已清空对话显示（历史会话可经 /session 恢复）。")


def _cmd_plan(ctx: CommandContext) -> None:
    enabled = not ctx.ui.is_plan_mode()
    ctx.ui.set_plan_mode(enabled)
    ctx.ui.add_system_message(
        "已切换到 Plan Mode（只读探索）。" if enabled else "已退出 Plan Mode。"
    )
    if ctx.args:
        ctx.ui.send_user_message(ctx.args)  # 带参时同时作为任务描述发送


def _cmd_session(ctx: CommandContext) -> None:
    if not ctx.args:
        if ctx.session is not None:
            ctx.ui.add_system_message(
                f"当前会话：{ctx.session.id}，消息 {len(ctx.session.conversation.messages)} 条"
            )
        else:
            ctx.ui.add_system_message("当前无会话。/session list 查看历史。")
        return
    parts = ctx.args.split(maxsplit=1)
    sub, arg = parts[0], (parts[1] if len(parts) > 1 else "")
    mgr = ctx.session_manager
    if mgr is None:
        ctx.ui.add_system_message("会话管理器不可用。")
        return
    if sub == "list":
        metas = mgr.list()
        if not metas:
            ctx.ui.add_system_message("暂无历史会话。")
            return
        lines = ["历史会话（按最后活跃倒序）："]
        for m in metas[:10]:
            active = datetime.fromtimestamp(m.last_active_ts).strftime("%m-%d %H:%M")
            lines.append(f"  {m.sid}  活跃 {active}")
        ctx.ui.add_system_message("\n".join(lines))
    elif sub == "new":
        session = mgr.create()
        ctx.ui.set_active_session(session)
        ctx.ui.add_system_message(f"已新建会话：{session.id}")
    elif sub == "resume":
        if not arg:
            ctx.ui.add_system_message("/session resume <id>：恢复指定会话。")
            return
        try:
            session = mgr.resume(arg)
        except FileNotFoundError as exc:
            ctx.ui.add_system_message(str(exc))
            return
        ctx.ui.set_active_session(session)
        ctx.ui.add_system_message(
            f"已恢复会话：{arg}（消息 {len(session.conversation.messages)} 条）"
        )
    elif sub == "delete":
        if not arg:
            ctx.ui.add_system_message("/session delete <id>：删除指定会话。")
            return
        mgr.delete(arg)
        ctx.ui.add_system_message(f"已删除会话：{arg}")
    else:
        ctx.ui.add_system_message(
            "/session 支持：无参（概要）、list、new、resume <id>、delete <id>。"
        )


def _cmd_exit(ctx: CommandContext) -> None:
    ctx.ui.request_exit()


def build_default_commands() -> CommandRegistry:
    """注册能跑档 7 个内置命令（05 §3.6）。"""
    registry = CommandRegistry()
    registry.register(
        Command(
            name="help",
            aliases=["h", "?"],
            description="列出命令；/help <命令> 看详情",
            usage="/help [命令名]",
            type="local",
            handler=_cmd_help,
        )
    )
    registry.register(
        Command(
            name="status",
            aliases=["s"],
            description="模式 / token / 工具数 / 会话",
            usage="/status",
            type="local",
            handler=_cmd_status,
        )
    )
    registry.register(
        Command(
            name="compact",
            aliases=["c"],
            description="上下文压缩（M2 落地，当前占位）",
            usage="/compact",
            type="local",
            handler=_cmd_compact,
        )
    )
    registry.register(
        Command(
            name="clear",
            description="清空对话显示",
            usage="/clear",
            type="local-ui",
            handler=_cmd_clear,
        )
    )
    registry.register(
        Command(
            name="plan",
            aliases=["p"],
            description="Plan Mode 切换（只读探索）",
            usage="/plan [任务描述]",
            type="local-ui",
            handler=_cmd_plan,
        )
    )
    registry.register(
        Command(
            name="session",
            description="会话：概要 / list / new / resume / delete",
            usage="/session [list|new|resume <id>|delete <id>]",
            type="local",
            handler=_cmd_session,
        )
    )
    registry.register(
        Command(
            name="exit",
            description="退出 KDAgent",
            usage="/exit",
            type="local",
            handler=_cmd_exit,
        )
    )
    return registry


__all__ = [
    "Command",
    "CommandContext",
    "CommandRegistry",
    "CommandType",
    "UIController",
    "build_default_commands",
    "parse_command",
]
