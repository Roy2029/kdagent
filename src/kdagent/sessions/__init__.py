"""会话管理（规格 04）：JSONL 持久化 / 恢复四步 / 过期清理。

懒加载包：`manager` 引用 `01`（context.compactor 做恢复超限压缩），而 `compactor`
引用 `sessions.records`（todo 快照）——若此处 eager import manager，加载 compactor 时
会经 records 触发 __init__ → manager → compactor 成环。改用 PEP 562 按需取属性，
包本身保持零依赖，各子模块可独立按拓扑序加载。
"""

from __future__ import annotations

__all__ = [
    "STALE_AFTER_SECONDS",
    "STALE_REMINDER",
    "Session",
    "SessionManager",
    "SessionMeta",
    "SessionRecord",
    "StepRecord",
    "ThinkingRecord",
    "TodoItemRecord",
    "ToolResultRecord",
    "ToolUseRecord",
    "make_session_id",
]

_MANAGER_NAMES = {
    "STALE_AFTER_SECONDS",
    "STALE_REMINDER",
    "Session",
    "SessionManager",
    "SessionMeta",
    "make_session_id",
}
_RECORDS_NAMES = {
    "SessionRecord",
    "StepRecord",
    "ThinkingRecord",
    "TodoItemRecord",
    "ToolResultRecord",
    "ToolUseRecord",
}


def __getattr__(name: str) -> object:
    if name in _MANAGER_NAMES:
        from kdagent.sessions import manager as _m  # noqa: PLC0415

        return getattr(_m, name)
    if name in _RECORDS_NAMES:
        from kdagent.sessions import records as _r  # noqa: PLC0415

        return getattr(_r, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
