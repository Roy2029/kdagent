"""ToolRegistry：工具注册 / 查找 / 枚举 schema（规格 03 §3.4，09 §3.5 延迟加载）。

工具的唯一事实来源。重名注册启动即报错（fail fast），防止内置与扩展冲突。
`schemas()` 产出的 tools 数组可直接喂给 02 的 Payload。

**延迟加载（09 §3.5，T21 已决）**：内置工具常驻、MCP 工具延迟（分界 = 数量可不可控）。
注册带 `defer=True` 的工具不立即进 payload，只把名字放进 system-reminder；
模型用 `ToolSearch` 按名/关键词拉取，`mark_discovered` 后下一轮进 tools 字段。
"""

from __future__ import annotations

from typing import Any

from kdagent.engine.llm.base import ToolSchema
from kdagent.tools.base import Tool


class ToolRegistry:
    """工具注册中心与查询门面。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._deferred: set[str] = set()  # 声明延迟的工具（MCP 等）
        self._discovered: set[str] = set()  # 已被 ToolSearch 拉取的延迟工具

    def register(self, tool: Tool, *, defer: bool = False) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重名注册：{tool.name}")
        self._tools[tool.name] = tool
        if defer:
            self._deferred.add(tool.name)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[ToolSchema]:
        """领域级工具描述 → 02 ToolSchema 列表（直接用于 API 请求）。"""
        return [
            ToolSchema(name=t.name, description=t.description, input_schema=t.input_schema)
            for t in self._tools.values()
        ]

    def payload_schemas(self) -> tuple[list[ToolSchema], list[str]]:
        """09 §3.5 payload 组装：常驻 + 已发现工具给完整 schema，未发现延迟工具只给名字。

        返回 `(tools, deferred_names)`；deferred_names 进 system-reminder 提示
        可用 ToolSearch 加载（不改 system 字段 → 前缀缓存不受影响）。
        """
        tools: list[ToolSchema] = []
        deferred: list[str] = []
        for t in self._tools.values():
            if self.should_defer(t.name) and not self.is_discovered(t.name):
                deferred.append(t.name)
            else:
                tools.append(
                    ToolSchema(name=t.name, description=t.description, input_schema=t.input_schema)
                )
        return tools, deferred

    # ---- 延迟加载（09 §3.5） ----

    def should_defer(self, name: str) -> bool:
        return name in self._deferred

    def is_discovered(self, name: str) -> bool:
        return name in self._discovered

    def mark_discovered(self, name: str) -> None:
        """ToolSearch 命中后标记：下一轮 payload 进完整 schema。"""
        self._discovered.add(name)

    def deferred_tool_names(self) -> list[str]:
        """尚未发现的延迟工具名字（ToolSearch 搜索范围）。"""
        return sorted(n for n in self._deferred if n not in self._discovered)

    def is_concurrency_safe(self, name: str, input: dict[str, Any]) -> bool:
        tool = self._tools.get(name)
        if tool is None:
            return False
        return tool.is_concurrency_safe(input)

    def validate(self, name: str, input: dict[str, Any]) -> list[str]:
        """参数校验；工具不存在返回错误（而非抛 KeyError）。"""
        tool = self._tools.get(name)
        if tool is None:
            return [f"工具不存在：{name}"]
        return tool.validate_input(input)
