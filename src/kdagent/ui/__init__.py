"""Textual TUI（规格 05）。

暴露 KDApp（可运行 App）与各 widget，便于测试与二次组装。
"""

from kdagent.ui.app import KDApp
from kdagent.ui.chat import ChatView
from kdagent.ui.commands import Command, CommandRegistry, build_default_commands, parse_command
from kdagent.ui.confirm import ConfirmDialog, ExitDialog
from kdagent.ui.statusbar import StatusBar
from kdagent.ui.toolregion import ToolRegion

__all__ = [
    "ChatView",
    "Command",
    "CommandRegistry",
    "ConfirmDialog",
    "ExitDialog",
    "KDApp",
    "StatusBar",
    "ToolRegion",
    "build_default_commands",
    "parse_command",
]
