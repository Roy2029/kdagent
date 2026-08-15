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
from kdagent.mcp.manager import MCPManager
from kdagent.memory.model import MEMORY_TYPES, MemoryFile
from kdagent.memory.store import MemoryStore
from kdagent.sessions.manager import Session, SessionManager
from kdagent.skill.manager import SkillManager
from kdagent.subagent.task import TaskManager

CommandType = Literal["local", "local-ui", "prompt"]


class UIController(Protocol):
    """命令不感知 UI 框架（05 §3.5）。KDApp 实现此协议。"""

    def add_system_message(self, text: str) -> None: ...
    def send_user_message(self, text: str) -> None: ...
    def set_plan_mode(self, enabled: bool) -> None: ...
    def is_plan_mode(self) -> bool: ...
    def get_token_count(self) -> int: ...
    def get_context_tokens(self) -> int: ...
    def refresh_status(self) -> None: ...
    def clear_chat(self) -> None: ...
    def request_exit(self) -> None: ...
    def set_active_session(self, session: Session | None) -> None: ...
    def set_permission_mode(self, mode: str) -> None: ...
    def get_permission_mode(self) -> str: ...


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
    # 04 §5 恢复③：/session resume 超 AUTO_COMPACT_TRIGGER 时触发压缩（App 异步接线）
    resume_compact: Callable[[ConversationManager], None] | None = None
    # 05 §3.6：/compact 手动压缩（App 同步调度异步执行；str=带参保留重点，同 resume 模式）
    manual_compact: Callable[[ConversationManager, str], None] | None = None
    # 09 M4-c/d 查看类命令：MCP 连接状态（/mcp）、Skill 清单（/skills）。
    mcp_manager: MCPManager | None = None
    skill_manager: SkillManager | None = None
    # 08 M4-e：/memory 查看/管理记忆（概要/list/add/delete/clear）。
    memory_store: MemoryStore | None = None
    # 10 M5-a：/tasks 后台任务便捷列表（Task 工具为主，/tasks 仅查看）。
    task_manager: TaskManager | None = None


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


def format_compact_report(before: int, after: int) -> str:
    """压缩前后 token 对比（05 §3.6：/compact 显示前后对比）。"""
    saved = max(0, before - after)
    return f"上下文压缩完成：{before:,} → {after:,} tokens（释放 {saved:,}）"


def _cmd_compact(ctx: CommandContext) -> None:
    # 窗口口径（05 §3.2：当前上下文占用/窗口上限）——<5K 无需压缩
    token = ctx.ui.get_context_tokens()
    if token < 5000:
        ctx.ui.add_system_message("当前上下文低于 5K token，无需压缩。")
        return
    if ctx.manual_compact is None:
        ctx.ui.add_system_message("上下文压缩能力不可用（未接入 L3 压缩引擎）。")
        return
    # 01 §7 手动触发：与自动共用同一套 L3 逻辑，仅触发方式不同（App 异步执行）；
    # 带参 = 保留重点（05 §3.6），透传给 Compactor 注入摘要指令。
    ctx.manual_compact(ctx.conversation, ctx.args)


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
            session = mgr.resume(arg, compact=ctx.resume_compact)
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


def _cmd_mcp(ctx: CommandContext) -> None:
    """09 §3.11：查看已连接 Server / 工具 / 连接状态（与 /permissions 同构查看类）。"""
    mgr = ctx.mcp_manager
    if mgr is None:
        ctx.ui.add_system_message("MCP 系统不可用（未配置 mcp_servers）。")
        return
    lines = ["MCP Server 状态："]
    if mgr.connected:
        lines.append(f"  已连接：{'、'.join(sorted(mgr.connected))}")
    else:
        lines.append("  已连接：无")
    if mgr.failed:
        for name, err in mgr.failed.items():
            lines.append(f"  失败：{name}（{err}）")
    mcp_tools = [n for n in ctx.agent.tool_names if n.startswith("mcp_")]
    lines.append(f"  工具：{len(mcp_tools)} 个（{', '.join(mcp_tools) if mcp_tools else '无'}）")
    ctx.ui.add_system_message("\n".join(lines))


def _cmd_skills(ctx: CommandContext) -> None:
    """09 §3.11：查看已加载 Skill 清单（两阶段加载第一阶段产物）。"""
    mgr = ctx.skill_manager
    if mgr is None:
        ctx.ui.add_system_message("Skill 系统不可用。")
        return
    skills = mgr.list()
    if not skills:
        ctx.ui.add_system_message("暂无可用 Skill。可用 skill-creator 工具创建一个。")
        return
    lines = [f"可用 Skill（{len(skills)} 个）："]
    for s in skills:
        mode = "" if s.mode == "inline" else f"（{s.mode}）"
        lines.append(f"  {s.name}{mode}：{s.description}")
    lines.append("调用 LoadSkill(\"<name>\") 加载完整 SOP。")
    ctx.ui.add_system_message("\n".join(lines))


def _cmd_memory(ctx: CommandContext) -> None:
    """08 §3.5 /memory 命令：概要 / list / add / delete / clear（查看/管理记忆）。"""
    store = ctx.memory_store
    if store is None:
        ctx.ui.add_system_message("记忆系统不可用。")
        return
    parts = ctx.args.split()
    sub = parts[0] if parts else ""
    if not sub:
        entries = store.list_all()
        if not entries:
            ctx.ui.add_system_message("暂无记忆。可用 /memory add 手动添加。")
            return
        counts = {t: 0 for t in MEMORY_TYPES}
        for e in entries:
            counts[e.type] += 1
        lines = [f"记忆 {len(entries)} 条："]
        for t in MEMORY_TYPES:
            if counts[t]:
                lines.append(f"  {t}: {counts[t]}")
        lines.append("/memory list 查看明细；/memory add 手动添加。")
        ctx.ui.add_system_message("\n".join(lines))
        return
    if sub == "list":
        entries = store.list_all()
        if not entries:
            ctx.ui.add_system_message("暂无记忆。")
            return
        lines = ["记忆列表："]
        for e in entries:
            scope = "用户" if e.type in ("user", "feedback") else "项目"
            lines.append(f"  [{e.type}] {e.name}（{scope}）：{e.description or '(无描述)'}")
        ctx.ui.add_system_message("\n".join(lines))
        return
    if sub == "add":
        # /memory add <name> <type> <description> <content...>
        if len(parts) < 4:
            ctx.ui.add_system_message(
                "/memory add <name> <type> <description> <content>：手动添加记忆"
            )
            return
        name, raw_type, description = parts[1], parts[2], parts[3]
        if raw_type not in MEMORY_TYPES:
            ctx.ui.add_system_message(
                f"type 需为 {'/'.join(MEMORY_TYPES)}（收到：{raw_type}）"
            )
            return
        content = " ".join(parts[4:]) or description
        # raw_type 经 MEMORY_TYPES 校验已收窄为 MemoryType
        ok = store.create(
            MemoryFile(name=name, description=description, type=raw_type, content=content)
        )
        ctx.ui.add_system_message(f"已添加记忆：{name}" if ok else f"记忆已存在：{name}")
        return
    if sub == "delete":
        if len(parts) < 2:
            ctx.ui.add_system_message("/memory delete <name>：删除指定记忆。")
            return
        ok = store.delete(parts[1])
        ctx.ui.add_system_message(
            f"已删除记忆：{parts[1]}" if ok else f"记忆不存在：{parts[1]}"
        )
        return
    if sub == "clear":
        entries = store.list_all()
        for e in entries:
            store.delete(e.name)
        ctx.ui.add_system_message(f"已清空 {len(entries)} 条记忆。")
        return
    ctx.ui.add_system_message(
        "/memory 支持：无参（概要）、list、add <name> <type> <description> <content>、"
        "delete <name>、clear。"
    )


def _cmd_tasks(ctx: CommandContext) -> None:
    """10 §3.7：后台任务便捷列表（local 仅查看；查询/管理以 TaskList/TaskGet 为主）。"""
    mgr = ctx.task_manager
    if mgr is None:
        ctx.ui.add_system_message("后台任务系统不可用。")
        return
    tasks = mgr.list()
    if not tasks:
        ctx.ui.add_system_message(
            "当前无后台任务。主 Agent 可用 Agent 工具（run_in_background=true）启动。"
        )
        return
    lines = [f"后台任务 {len(tasks)} 个："]
    for t in tasks:
        lines.append(f"  {t.summary()}")
    lines.append("查询详情让主 Agent 用 TaskGet；状态变化会注入对话通知。")
    ctx.ui.add_system_message("\n".join(lines))


def skill_command_handler(skill_name: str) -> Callable[[CommandContext], None]:
    """Skill 自动注册的 /name 命令 handler（09 §3.9 显式触发路径）。

    prompt 类命令：加载完整 SOP（$ARGUMENTS 已替换为用户参数）→ 转发给 Agent
    执行。fork 模式未落地 → 降级 inline + 警告（与 LoadSkill 同文案）。
    """

    def handler(ctx: CommandContext) -> None:
        mgr = ctx.skill_manager
        if mgr is None:
            ctx.ui.add_system_message("Skill 系统不可用。")
            return
        skill = mgr.load(skill_name, ctx.args)
        if skill is None:
            ctx.ui.add_system_message(f"Skill 不存在：{skill_name}。")
            return
        body = skill.body
        if skill.mode == "fork":
            body = "⚠ fork 模式未落地，降级为 inline 执行。\n\n" + body
        ctx.ui.send_user_message(body)

    return handler


def register_skill_commands(
    registry: CommandRegistry, skill_manager: SkillManager
) -> None:
    """启动时把已加载 Skill 自动注册为 /name 命令（09 §3.9 显式触发路径）。

    与内置/已有命令冲突的 Skill 跳过（如 name: help——命令重名启动即崩，跳过
    并保留 LoadSkill/意图识别两条路径）。会话中新建的 Skill 需重启才注册命令
    （仍可经 LoadSkill 使用）。
    """
    for skill in skill_manager.list():
        if registry.find(skill.name) is not None:
            continue
        registry.register(
            Command(
                name=skill.name,
                description=skill.description,
                usage=f"/{skill.name} [参数]",
                type="prompt",
                handler=skill_command_handler(skill.name),
            )
        )


# 06 M3 可控档：权限模式清单（/permissions 可切换；bypassPermissions 仅黑名单仍生效）。
_PERMISSION_MODES = ("default", "acceptEdits", "plan", "bypassPermissions")


def _cmd_permissions(ctx: CommandContext) -> None:
    mode = ctx.ui.get_permission_mode()
    if not ctx.args:
        options = "、".join(f"/permissions {m}" for m in _PERMISSION_MODES)
        lines = [f"当前权限模式：{mode}", f"可切换：{options}"]
        ctx.ui.add_system_message("\n".join(lines))
        return
    target = ctx.args.strip().lower()
    if target not in _PERMISSION_MODES:
        ctx.ui.add_system_message(f"未知权限模式：{ctx.args}（可选：{'、'.join(_PERMISSION_MODES)}）")
        return
    ctx.ui.set_permission_mode(target)
    ctx.ui.add_system_message(f"已切换到权限模式：{target}")


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
    registry.register(
        Command(
            name="permissions",
            aliases=["perm"],
            description="权限模式：查看 / 切换（default/acceptEdits/plan/bypassPermissions）",
            usage="/permissions [模式]",
            type="local",
            handler=_cmd_permissions,
        )
    )
    registry.register(
        Command(
            name="mcp",
            description="MCP 状态：已连接 Server / 工具列表 / 连接失败",
            usage="/mcp",
            type="local",
            handler=_cmd_mcp,
        )
    )
    registry.register(
        Command(
            name="skills",
            description="Skill 清单：查看已加载 Skill（name + description）",
            usage="/skills",
            type="local",
            handler=_cmd_skills,
        )
    )
    registry.register(
        Command(
            name="memory",
            description="记忆：概要 / list / add / delete / clear",
            usage="/memory [list|add <name> <type> <desc> <content>|delete <name>|clear]",
            type="local",
            handler=_cmd_memory,
        )
    )
    registry.register(
        Command(
            name="tasks",
            description="后台任务便捷列表（/tasks；详情让主 Agent 用 TaskGet）",
            usage="/tasks",
            type="local",
            handler=_cmd_tasks,
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
    "format_compact_report",
    "parse_command",
    "register_skill_commands",
]
