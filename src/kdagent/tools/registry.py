"""ToolRegistry：工具注册 / 查找 / 枚举 schema（规格 03 §3.4）。

工具的唯一事实来源。重名注册启动即报错（fail fast），防止内置与扩展冲突。
`schemas()` 产出的 tools 数组可直接喂给 02 的 Payload。
"""

from __future__ import annotations

from typing import Any

from kdagent.engine.llm.base import ToolSchema
from kdagent.tools.base import Tool


class ToolRegistry:
    """工具注册中心与查询门面。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重名注册：{tool.name}")
        self._tools[tool.name] = tool

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
