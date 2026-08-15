"""工具协议（规格 03 §3.2）。

不止「名字 + 执行」，每个工具还声明元信息：
- is_read_only / is_destructive：供权限系统（06）决策
- is_concurrency_safe：供 02 分批执行决策
- require_confirm：供 05 确认对话框前置
- validate_input：进入执行前先做参数校验

Tool 是 Protocol（结构子类型），内置工具用普通类实现；注册时 registry 校验其元信息完备。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeAlias

from kdagent.config import Config

# 05 UI 提供的确认钩子：工具名 + 参数 → 是否放行。async，因为确认对话框要等用户输入。
AsyncConfirm: TypeAlias = Callable[[str, dict[str, Any]], Awaitable[bool]]

# 03 TodoWrite 触发回调：归一化后的 todo 结构（todo→task→steps）→ 会话状态 + UI 渲染。
# 回调由 UI 层注入，TodoWrite 不感知 Session/UI（03 §3.6 数据流）。
TodosCallback: TypeAlias = Callable[[list[dict[str, Any]]], None]


@dataclass(slots=True)
class ToolContext:
    """一次工具调用的运行环境（依赖注入）。

    work_dir：相对路径基准（内置工具强制绝对路径时以此兜底）；
    config：运行时配置；
    tool_use_id：当前调用对应的 tool_use id，由 02 `_exec_one` 注入，供 ToolResult 回填；
    confirm：05 注入的确认钩子（require_confirm 前置），None 表示非交互环境直接执行；
    todos：03 TodoWrite 归一化结果回调（会话状态 + UI 渲染），None 表示未接线（纯工具测试）。
    """

    work_dir: Path
    config: Config
    tool_use_id: str = ""
    confirm: AsyncConfirm | None = None
    todos: TodosCallback | None = None
    # 01 §5.2 L1 落盘目录（{sessions_dir}/{sid}/tool-results/），ReadFile 读回豁免用
    persist_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    """工具执行结果（03 §3.2；07 可观测性消费 duration_ms）。

    persist_exempt：01 §5.2 L1 读回豁免——ReadFile 读回落盘文件时置 True，
    入口处理器跳过 L1（否则永远读不到全文）。
    """

    tool_use_id: str
    name: str
    content: str
    is_error: bool = False
    duration_ms: int = 0
    persist_exempt: bool = False


class Tool(Protocol):
    """统一工具协议：元信息 + 校验 + 执行。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    category: str
    require_confirm: bool

    def is_read_only(self) -> bool: ...

    def is_destructive(self) -> bool: ...

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool: ...

    def validate_input(self, input: dict[str, Any]) -> list[str]: ...

    async def execute(self, ctx: ToolContext, input: dict[str, Any]) -> ToolResult: ...
