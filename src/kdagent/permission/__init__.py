"""权限五层纵深防御（规格 06）：L1 黑名单 / L2 沙箱 / L3 规则 / L4 模式 / L5 HITL。

`PermissionChecker.check` 是无 UI 依赖的纯裁决函数，MCP wrapper 与 SubAgent 复用。
"""

from __future__ import annotations

from kdagent.permission.blacklist import CommandBlacklist
from kdagent.permission.checker import Decision, PermissionChecker, build_permission_checker
from kdagent.permission.modes import ALL_MODES, MODE_MATRIX, Mode, tool_class
from kdagent.permission.rules import Effect, PermissionRule, RuleEngine
from kdagent.permission.sandbox import PathSandbox

__all__ = [
    "ALL_MODES",
    "CommandBlacklist",
    "Decision",
    "Effect",
    "MODE_MATRIX",
    "Mode",
    "PathSandbox",
    "PermissionChecker",
    "PermissionRule",
    "RuleEngine",
    "build_permission_checker",
    "tool_class",
]
