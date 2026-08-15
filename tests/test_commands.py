"""Slash Command 框架测试（规格 05 §3.5-3.6：注册/解析/别名冲突/补全/内置命令）。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import FakeLLM

from kdagent.config import Config
from kdagent.engine.agent import Agent
from kdagent.engine.conversation import ConversationManager
from kdagent.mcp.manager import MCPManager
from kdagent.memory.model import MemoryFile
from kdagent.memory.store import MemoryStore
from kdagent.sessions.manager import Session, SessionManager
from kdagent.skill.manager import SkillManager
from kdagent.subagent.runner import SubAgentRunner
from kdagent.subagent.task import TaskManager
from kdagent.tools import build_default_registry
from kdagent.ui.commands import (
    Command,
    CommandContext,
    CommandRegistry,
    UIController,
    build_default_commands,
    format_compact_report,
    parse_command,
    register_skill_commands,
)

TEST_COMMAND_NAMES = [
    "help",
    "status",
    "compact",
    "clear",
    "plan",
    "session",
    "exit",
    "permissions",
    "mcp",  # 09 M4-c：查看 MCP 连接状态
    "skills",  # 09 M4-d：查看已加载 Skill 清单
    "memory",  # 08 M4-e：查看/管理记忆
    "tasks",  # 10 M5-a：后台任务便捷列表
    "worktree",  # 10 M5-b：隔离工作目录 list/remove/cleanup
    "eval",  # 11 §3.4 TUI 版：/eval report <run_id> 打开评测报告屏
    "metrics",  # 07 §3.7 T9：/metrics 打开聚合指标面板
]


class RecordingUI:
    """实现 UIController 的测试替身：记录系统消息，其余空实现。"""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def add_system_message(self, text: str) -> None:
        self.messages.append(text)

    def send_user_message(self, text: str) -> None: ...

    def set_plan_mode(self, enabled: bool) -> None: ...

    def is_plan_mode(self) -> bool:
        return False

    def get_token_count(self) -> int:
        return 0

    def get_context_tokens(self) -> int:
        return 0

    def refresh_status(self) -> None: ...

    def clear_chat(self) -> None: ...

    def request_exit(self) -> None: ...

    def set_active_session(self, session: Session | None) -> None: ...

    def set_permission_mode(self, mode: str) -> None: ...

    def get_permission_mode(self) -> str:
        return "default"


def _ctx(
    tmp_path: Path,
    ui: UIController,
    args: str = "",
    manual_compact: Callable[[ConversationManager, str], None] | None = None,
    skill_manager: SkillManager | None = None,
    mcp_manager: MCPManager | None = None,
    memory_store: MemoryStore | None = None,
    task_manager: TaskManager | None = None,
) -> CommandContext:
    conv = ConversationManager()
    agent = Agent(
        config=Config(),
        llm=FakeLLM([]),
        conversation=conv,
        tools=build_default_registry(),
        events=lambda e: None,
        work_dir=tmp_path,
    )
    return CommandContext(
        args=args,
        agent=agent,
        conversation=conv,
        session=None,
        ui=ui,
        config=Config(),
        registry=build_default_commands(),
        session_manager=SessionManager(tmp_path / ".kdagent" / "sessions"),
        manual_compact=manual_compact,
        skill_manager=skill_manager,
        mcp_manager=mcp_manager,
        memory_store=memory_store,
        task_manager=task_manager,
    )


# ---- 解析 ----------------------------------------------------------------


def test_parse_non_command_is_not_command() -> None:
    assert parse_command("你好") == ("", "", False)


def test_parse_slash_command_lowercases() -> None:
    assert parse_command("/Help 查看") == ("help", "查看", True)


def test_parse_only_slash_lists_commands() -> None:
    assert parse_command("/") == ("", "", True)


def test_parse_with_args() -> None:
    assert parse_command("/session list") == ("session", "list", True)


# ---- 注册与别名冲突 --------------------------------------------------------


def test_register_duplicate_name_raises() -> None:
    reg = CommandRegistry()
    reg.register(Command(name="help", handler=None))
    with pytest.raises(ValueError, match="重名"):
        reg.register(Command(name="help", handler=None))


def test_register_alias_conflict_raises() -> None:
    reg = CommandRegistry()
    reg.register(Command(name="help", aliases=["h"], handler=None))
    with pytest.raises(ValueError, match="冲突"):
        reg.register(Command(name="hello", aliases=["h"], handler=None))


def test_alias_conflict_between_command_and_alias() -> None:
    reg = CommandRegistry()
    reg.register(Command(name="help", aliases=["s"], handler=None))
    with pytest.raises(ValueError, match="冲突"):
        reg.register(Command(name="s", handler=None))


def test_find_by_name_and_alias() -> None:
    reg = build_default_commands()
    assert reg.find("help").name == "help"  # type: ignore[union-attr]
    assert reg.find("h").name == "help"  # type: ignore[union-attr]
    assert reg.find("nope") is None


def test_complete_prefix_matching() -> None:
    reg = build_default_commands()
    matches = reg.complete("h")
    assert matches == ["h", "help"]  # 别名 h + 命令 help
    assert reg.complete("he") == ["help"]


# ---- 内置命令与 handler ----------------------------------------------------


def test_default_commands_all() -> None:
    reg = build_default_commands()
    names = [c.name for c in reg.all()]
    assert sorted(names) == sorted(TEST_COMMAND_NAMES)
    for name in TEST_COMMAND_NAMES:
        assert reg.find(name) is not None


def test_help_lists_commands(tmp_path: Path) -> None:
    ui = RecordingUI()
    build_default_commands().find("help").handler(_ctx(tmp_path, ui))  # type: ignore[union-attr]
    assert any("可用命令" in m for m in ui.messages)
    assert any("/help" in m for m in ui.messages)


def test_help_detail_for_command(tmp_path: Path) -> None:
    ui = RecordingUI()
    build_default_commands().find("help").handler(  # type: ignore[union-attr]
        _ctx(tmp_path, ui, args="status")
    )
    assert any("/status" in m and "模式" in m for m in ui.messages)


def test_help_unknown_command_guides(tmp_path: Path) -> None:
    ui = RecordingUI()
    build_default_commands().find("help").handler(  # type: ignore[union-attr]
        _ctx(tmp_path, ui, args="nope")
    )
    assert any("未知命令" in m and "/help" in m for m in ui.messages)


def test_status_shows_mode_token_tools(tmp_path: Path) -> None:
    ui = RecordingUI()
    build_default_commands().find("status").handler(_ctx(tmp_path, ui))  # type: ignore[union-attr]
    assert any("模式：DEFAULT" in m and "工具：7" in m for m in ui.messages)


def test_compact_below_threshold_hints_no_need(tmp_path: Path) -> None:
    ui = RecordingUI()
    build_default_commands().find("compact").handler(_ctx(tmp_path, ui))  # type: ignore[union-attr]
    assert any("无需压缩" in m for m in ui.messages)


# ---- /compact（M2-e：与自动共用 L3 逻辑 + 前后对比） -------------------------


class _CompactingUI(RecordingUI):
    """高上下文 token 的 UI 替身（触发 /compact 调度路径）。"""

    def __init__(self, tokens: int = 100_000) -> None:
        super().__init__()
        self.tokens = tokens

    def get_context_tokens(self) -> int:
        return self.tokens


def test_compact_dispatches_manual_compact(tmp_path: Path) -> None:
    """/compact（token ≥5K）→ 调度 manual_compact，focus 为空。"""
    ui = _CompactingUI()
    calls: list[tuple[ConversationManager, str]] = []
    ctx = _ctx(tmp_path, ui, manual_compact=lambda conv, focus: calls.append((conv, focus)))
    build_default_commands().find("compact").handler(ctx)  # type: ignore[union-attr]
    assert len(calls) == 1 and calls[0][1] == ""


def test_compact_forwards_focus(tmp_path: Path) -> None:
    """/compact <重点>：参数作为保留重点传给 manual_compact（05 §3.6 带参）。"""
    ui = _CompactingUI()
    calls: list[str] = []
    ctx = _ctx(tmp_path, ui, args="bug A", manual_compact=lambda conv, focus: calls.append(focus))
    build_default_commands().find("compact").handler(ctx)  # type: ignore[union-attr]
    assert calls == ["bug A"]


def test_compact_without_ability_guides(tmp_path: Path) -> None:
    """manual_compact 未注入（未接 L3）→ 提示能力不可用。"""
    ui = _CompactingUI()
    ctx = _ctx(tmp_path, ui)
    build_default_commands().find("compact").handler(ctx)  # type: ignore[union-attr]
    assert any("压缩能力不可用" in m for m in ui.messages)


def test_compact_report_shows_before_after() -> None:
    """前后 token 对比文案（05 §5：显示前后对比）。"""
    text = format_compact_report(120_000, 30_000)
    assert "120,000" in text and "30,000" in text and "90,000" in text


# ---- /session resume/new/delete（M1-f 接线） --------------------------------


class SwitchingUI(RecordingUI):
    """记录 set_active_session 的会话切换替身。"""

    def __init__(self) -> None:
        super().__init__()
        self.active: Session | None = None

    def set_active_session(self, session: Session | None) -> None:
        self.active = session


def _ctx_session(tmp_path: Path, ui: UIController, args: str = "") -> CommandContext:
    conv = ConversationManager()
    agent = Agent(
        config=Config(),
        llm=FakeLLM([]),
        conversation=conv,
        tools=build_default_registry(),
        events=lambda e: None,
        work_dir=tmp_path,
    )
    return CommandContext(
        args=args,
        agent=agent,
        conversation=conv,
        session=None,
        ui=ui,
        config=Config(),
        registry=build_default_commands(),
        session_manager=SessionManager(tmp_path / ".kdagent" / "sessions"),
    )


def test_session_new_creates_and_activates(tmp_path: Path) -> None:
    ui = SwitchingUI()
    cmd = build_default_commands().find("session")
    cmd.handler(_ctx_session(tmp_path, ui, args="new"))  # type: ignore[union-attr]
    assert ui.active is not None
    assert ui.active.conversation.messages == []
    assert any("已新建会话" in m for m in ui.messages)


def test_session_resume_activates_saved(tmp_path: Path) -> None:
    mgr = SessionManager(tmp_path / ".kdagent" / "sessions")
    saved = mgr.create()
    saved.append_user("你好")
    ui = SwitchingUI()
    cmd = build_default_commands().find("session")
    cmd.handler(_ctx_session(tmp_path, ui, args=f"resume {saved.id}"))  # type: ignore[union-attr]
    assert ui.active is not None and ui.active.id == saved.id
    assert [m.role for m in ui.active.conversation.messages] == ["user"]
    assert any("已恢复会话" in m for m in ui.messages)


def test_session_resume_unknown_guides(tmp_path: Path) -> None:
    ui = SwitchingUI()
    cmd = build_default_commands().find("session")
    cmd.handler(_ctx_session(tmp_path, ui, args="resume nope"))  # type: ignore[union-attr]
    assert ui.active is None
    assert any("会话不存在" in m for m in ui.messages)


def test_session_delete_removes_file(tmp_path: Path) -> None:
    mgr = SessionManager(tmp_path / ".kdagent" / "sessions")
    saved = mgr.create()
    saved.append_user("hi")
    ui = SwitchingUI()
    cmd = build_default_commands().find("session")
    cmd.handler(_ctx_session(tmp_path, ui, args=f"delete {saved.id}"))  # type: ignore[union-attr]
    assert mgr.list() == []
    assert any("已删除会话" in m for m in ui.messages)


def test_session_list_shows_entries(tmp_path: Path) -> None:
    mgr = SessionManager(tmp_path / ".kdagent" / "sessions")
    saved = mgr.create()
    saved.append_user("hi")
    ui = RecordingUI()
    cmd = build_default_commands().find("session")
    cmd.handler(_ctx_session(tmp_path, ui, args="list"))  # type: ignore[union-attr]
    assert any(saved.id in m for m in ui.messages)


# ---- /permissions（06 M3 可控档：权限模式查看/切换） -------------------------


class _PermissionUI(RecordingUI):
    """记录权限模式切换的 UI 替身。"""

    def __init__(self, mode: str = "default") -> None:
        super().__init__()
        self.mode = mode

    def set_permission_mode(self, mode: str) -> None:
        self.mode = mode

    def get_permission_mode(self) -> str:
        return self.mode


def test_permissions_no_args_shows_current_mode(tmp_path: Path) -> None:
    ui = _PermissionUI(mode="acceptEdits")
    cmd = build_default_commands().find("permissions")
    cmd.handler(_ctx(tmp_path, ui))  # type: ignore[union-attr]
    assert any("当前权限模式：acceptEdits" in m for m in ui.messages)


def test_permissions_switch_mode(tmp_path: Path) -> None:
    ui = _PermissionUI()
    cmd = build_default_commands().find("permissions")
    cmd.handler(_ctx(tmp_path, ui, args="plan"))  # type: ignore[union-attr]
    assert ui.mode == "plan"
    assert any("已切换到权限模式：plan" in m for m in ui.messages)


def test_permissions_unknown_mode_guides(tmp_path: Path) -> None:
    ui = _PermissionUI()
    cmd = build_default_commands().find("permissions")
    cmd.handler(_ctx(tmp_path, ui, args="nope"))  # type: ignore[union-attr]
    assert ui.mode == "default"
    assert any("未知权限模式" in m for m in ui.messages)


# ---- /mcp、/skills（09 M4-c/d 查看类命令） ---------------------------------


def test_mcp_no_manager_guides(tmp_path: Path) -> None:
    ui = RecordingUI()
    build_default_commands().find("mcp").handler(_ctx(tmp_path, ui))  # type: ignore[union-attr]
    assert any("MCP 系统不可用" in m for m in ui.messages)


def test_mcp_shows_connection_status(tmp_path: Path) -> None:
    from kdagent.tools.registry import ToolRegistry

    mgr = MCPManager(ToolRegistry())
    mgr._connected.add("github")
    mgr._failed["broken"] = "RuntimeError: nope"
    ui = RecordingUI()
    build_default_commands().find("mcp").handler(  # type: ignore[union-attr]
        _ctx(tmp_path, ui, mcp_manager=mgr)
    )
    assert any("github" in m and "broken" in m for m in ui.messages)


def test_skills_lists_loaded(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir(parents=True)
    (root / "commit.md").write_text(
        "---\nname: commit\ndescription: 分析 git 变更\n---\n\nbody\n", encoding="utf-8"
    )
    (root / "review.md").write_text(
        "---\nname: review\ndescription: 审查代码\nmode: fork\n---\n\nbody\n", encoding="utf-8"
    )
    mgr = SkillManager([root])
    mgr.scan()
    ui = RecordingUI()
    build_default_commands().find("skills").handler(  # type: ignore[union-attr]
        _ctx(tmp_path, ui, skill_manager=mgr)
    )
    assert any("commit" in m and "review（fork）" in m for m in ui.messages)


def test_skills_empty_hints_creator(tmp_path: Path) -> None:
    mgr = SkillManager([tmp_path / "skills"])
    mgr.scan()
    ui = RecordingUI()
    build_default_commands().find("skills").handler(  # type: ignore[union-attr]
        _ctx(tmp_path, ui, skill_manager=mgr)
    )
    assert any("暂无可用 Skill" in m and "skill-creator" in m for m in ui.messages)


# ---- /memory（08 M4-e：概要/list/add/delete/clear） -------------------------


def _memory_store(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(tmp_path / "user", tmp_path / "proj")
    store.create(MemoryFile(name="python-风格", description="PEP8 优先", type="user", content="用 type hints"))
    store.create(MemoryFile(name="rag-方向", description="RAG 检索", type="project", content="向量库研究"))
    return store


def test_memory_summary_counts_by_type(tmp_path: Path) -> None:
    ui = RecordingUI()
    build_default_commands().find("memory").handler(  # type: ignore[union-attr]
        _ctx(tmp_path, ui, memory_store=_memory_store(tmp_path))
    )
    assert any("记忆 2 条" in m and "user: 1" in m and "project: 1" in m for m in ui.messages)


def test_memory_list_shows_entries(tmp_path: Path) -> None:
    ui = RecordingUI()
    build_default_commands().find("memory").handler(  # type: ignore[union-attr]
        _ctx(tmp_path, ui, args="list", memory_store=_memory_store(tmp_path))
    )
    assert any("python-风格" in m and "rag-方向" in m for m in ui.messages)


def test_memory_add_delete_clear(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "user", tmp_path / "proj")
    ui = RecordingUI()
    cmd = build_default_commands().find("memory")

    cmd.handler(  # type: ignore[union-attr]
        _ctx(tmp_path, ui, args="add 新记忆 feedback 纠正 应该用 X", memory_store=store)
    )
    assert any("已添加记忆：新记忆" in m for m in ui.messages)
    assert store.read("新记忆") is not None
    assert store.read("新记忆").content == "应该用 X"

    cmd.handler(  # type: ignore[union-attr]
        _ctx(tmp_path, ui, args="add 新记忆 user 重复", memory_store=store)
    )
    assert any("记忆已存在" in m for m in ui.messages)

    cmd.handler(  # type: ignore[union-attr]
        _ctx(tmp_path, ui, args="delete 新记忆", memory_store=store)
    )
    assert store.read("新记忆") is None

    store.create(MemoryFile(name="a", description="a", type="user", content="a"))
    cmd.handler(  # type: ignore[union-attr]
        _ctx(tmp_path, ui, args="clear", memory_store=store)
    )
    assert store.list_all() == []


def test_memory_invalid_type_guides(tmp_path: Path) -> None:
    ui = RecordingUI()
    build_default_commands().find("memory").handler(  # type: ignore[union-attr]
        _ctx(tmp_path, ui, args="add x nope 描述", memory_store=MemoryStore(tmp_path / "u", tmp_path / "p"))
    )
    assert any("type 需为" in m for m in ui.messages)


# ---- /tasks（10 M5-a：后台任务便捷列表） ------------------------------------


def test_tasks_no_manager_guides(tmp_path: Path) -> None:
    ui = RecordingUI()
    build_default_commands().find("tasks").handler(  # type: ignore[union-attr]
        _ctx(tmp_path, ui)
    )
    assert any("后台任务系统不可用" in m for m in ui.messages)


def test_tasks_empty_list(tmp_path: Path) -> None:
    runner = SubAgentRunner(
        llm=FakeLLM([]),
        tools=build_default_registry(),
        config=Config(),
        work_dir=tmp_path,
    )
    tm = TaskManager(runner)
    ui = RecordingUI()
    build_default_commands().find("tasks").handler(  # type: ignore[union-attr]
        _ctx(tmp_path, ui, task_manager=tm)
    )
    assert any("当前无后台任务" in m for m in ui.messages)


# ---- Skill 自动注册 Slash 命令（09 M4-e：/name 显式触发） -------------------


def test_register_skill_commands_auto_slash(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir(parents=True)
    (root / "commit.md").write_text(
        "---\nname: commit\ndescription: 分析 git 变更\n---\n\n# 步骤\n1. git status\n参数：$ARGUMENTS\n",
        encoding="utf-8",
    )
    mgr = SkillManager([root])
    mgr.scan()
    reg = build_default_commands()
    register_skill_commands(reg, mgr)
    cmd = reg.find("commit")
    assert cmd is not None
    assert cmd.type == "prompt"
    assert "/commit" in cmd.usage  # type: ignore[operator]


def test_register_skill_commands_skips_builtin_conflict(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir(parents=True)
    (root / "help.md").write_text(
        "---\nname: help\ndescription: 试图占用内置命令\n---\n\nbody\n", encoding="utf-8"
    )
    mgr = SkillManager([root])
    mgr.scan()
    reg = build_default_commands()
    register_skill_commands(reg, mgr)
    # 不注册：/help 已被内置占用（描述仍是内置的）；Skill 仍可经 LoadSkill 触发
    assert "列出命令" in reg.find("help").description  # type: ignore[union-attr]
    assert mgr.get("help") is not None


class _SendingUI(RecordingUI):
    """记录 send_user_message（prompt 类命令把 SOP 转发给 Agent 的载体）。"""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    def send_user_message(self, text: str) -> None:
        self.sent.append(text)


def test_skill_slash_command_dispatches_sop(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir(parents=True)
    (root / "commit.md").write_text(
        "---\nname: commit\ndescription: 分析 git 变更\n---\n\n# 步骤\n1. git status\n参数：$ARGUMENTS\n",
        encoding="utf-8",
    )
    mgr = SkillManager([root])
    mgr.scan()
    reg = build_default_commands()
    register_skill_commands(reg, mgr)
    ui = _SendingUI()
    reg.find("commit").handler(  # type: ignore[union-attr]
        _ctx(tmp_path, ui, args="加个 docs", skill_manager=mgr)
    )
    # prompt 类命令：SOP（$ARGUMENTS 已替换）作为用户消息转发给 Agent 执行
    assert any("参数：加个 docs" in m for m in ui.sent)
    assert any("1. git status" in m for m in ui.sent)
