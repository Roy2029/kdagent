"""工具系统（规格 03）。"""

from kdagent.tools.base import Tool, ToolContext, ToolResult
from kdagent.tools.filesystem import EditFile, Glob, Grep, ReadFile, WriteFile
from kdagent.tools.registry import ToolRegistry
from kdagent.tools.shell import Bash
from kdagent.tools.test_runner import TestRunner, parse_failed_tests
from kdagent.tools.todo import TodoWrite


def build_default_registry() -> ToolRegistry:
    """能跑档内置 7 工具注册（D10）。重名冲突在此 fail fast。"""
    registry = ToolRegistry()
    for tool in (ReadFile(), WriteFile(), EditFile(), Glob(), Grep(), Bash(), TodoWrite()):
        registry.register(tool)
    return registry


__all__ = [
    "Bash",
    "EditFile",
    "Glob",
    "Grep",
    "ReadFile",
    "TestRunner",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "TodoWrite",
    "WriteFile",
    "build_default_registry",
    "parse_failed_tests",
]
