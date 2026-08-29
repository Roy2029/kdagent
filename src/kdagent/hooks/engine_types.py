"""Hook 数据模型（规格 06 §3.10）。

`HookContext` 是跨 hook 生命周期传递的运行时上下文；`HookReject` 是
`pre_tool_use` 拦截的产出——工具调用取消，拒绝原因作为错误结果进历史。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class HookContext:
    """一次 hook 触发的运行时上下文（未定义字段为空串，不报错）。"""

    event: str
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    file_path: str = ""
    message: str = ""
    error: str = ""
    # pre_send 专用：本次 LLM 调用完整上下文的落盘文件路径（$PAYLOAD_PATH 展开）。
    # 内容超长，不走命令行参数/环境变量（Windows 命令行长度上限）。
    payload_path: str = ""


@dataclass(frozen=True, slots=True)
class HookReject:
    """pre_tool_use 拦截结果：拒绝原因作为 ToolResult 内容进历史。"""

    reason: str
