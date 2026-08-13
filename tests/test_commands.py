"""Slash Command 框架测试（规格 05 §3.5-3.6：注册/解析/别名冲突/补全/内置命令）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeLLM

from kdagent.config import Config
from kdagent.engine.agent import Agent
from kdagent.engine.conversation import ConversationManager
from kdagent.sessions.manager import SessionManager
from kdagent.tools import build_default_registry
from kdagent.ui.commands import (
    Command,
    CommandContext,
    CommandRegistry,
    UIController,
    build_default_commands,
    parse_command,
)

TEST_COMMAND_NAMES = ["help", "status", "compact", "clear", "plan", "session", "exit"]


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

    def refresh_status(self) -> None: ...

    def clear_chat(self) -> None: ...

    def request_exit(self) -> None: ...


def _ctx(tmp_path: Path, ui: UIController, args: str = "") -> CommandContext:
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


def test_default_commands_all_seven() -> None:
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
