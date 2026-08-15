"""L4 权限模式矩阵（规格 06 §3.6）。

四模式覆盖「完全不信任 → 完全信任」光谱；工具按三类落入矩阵：
只读工具（is_read_only）/ 文件写工具（filesystem 非只读）/ Bash（shell）。
planning 类（TodoWrite）与 bypass 单独处理，见 `PermissionChecker`。
"""

from __future__ import annotations

from typing import Literal

from kdagent.permission.rules import Effect
from kdagent.tools.base import Tool

Mode = Literal["default", "acceptEdits", "plan", "bypassPermissions"]

_READ: Literal["read"] = "read"
_WRITE: Literal["write"] = "write"
_SHELL: Literal["shell"] = "shell"

# 规格 06 §3.6 表。
MODE_MATRIX: dict[Mode, dict[str, Effect]] = {
    "default": {_READ: "allow", _WRITE: "ask", _SHELL: "ask"},
    "acceptEdits": {_READ: "allow", _WRITE: "allow", _SHELL: "ask"},
    "plan": {_READ: "allow", _WRITE: "ask", _SHELL: "ask"},
    "bypassPermissions": {_READ: "allow", _WRITE: "allow", _SHELL: "allow"},
}

ALL_MODES: tuple[Mode, ...] = ("default", "acceptEdits", "plan", "bypassPermissions")


def tool_class(tool: Tool) -> Literal["read", "write", "shell"]:
    """工具 → 矩阵列。Bash 归 shell；filesystem 非只读归 write；其余只读归 read。

    planning 类（TodoWrite）由 `PermissionChecker` 特判恒放行，不落矩阵。
    """
    if tool.category == "shell":
        return _SHELL
    if tool.is_read_only():
        return _READ
    return _WRITE
