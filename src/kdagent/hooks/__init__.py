"""Hook 系统（规格 06 §3.10）：事件驱动自动化——事件 + 条件 + 动作。

`pre_tool_use` 是唯一能「说不」的事件；其余事件执行出错只记日志、不中断主流程。
"""

from __future__ import annotations

from kdagent.hooks.conditions import Condition, ConditionError, expand_variables, parse_condition
from kdagent.hooks.engine import EVENT_SET, HookConfig, HookEngine
from kdagent.hooks.engine_types import HookContext, HookReject

__all__ = [
    "Condition",
    "ConditionError",
    "EVENT_SET",
    "HookConfig",
    "HookContext",
    "HookEngine",
    "HookReject",
    "expand_variables",
    "parse_condition",
]
