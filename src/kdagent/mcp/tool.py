"""MCPToolWrapper：MCP 工具 → 内部 Tool 协议适配（09 §3.4）。

命名 `mcp_<server>_<tool>` 防冲突；元信息保守声明（非只读/非并发安全——MCP
无静态只读标记，宁可先问；用户用权限规则放行可信 Server）。结果经 `extract_text`
提取文本块，isError 透传。执行结果进 03 同一路径 → 01 入口 → 07 tool.exec span。
"""

from __future__ import annotations

import time
from typing import Any

from kdagent.mcp.model import MCPClient, MCPToolDef, extract_text
from kdagent.tools.base import ToolContext, ToolResult


class MCPToolWrapper:
    """把 MCP 工具适配为内部 Tool，对 Agent 完全透明。"""

    category = "mcp"
    require_confirm = False

    def __init__(self, server: str, tool: MCPToolDef, client: MCPClient) -> None:
        self._server = server
        self._tool = tool
        self._client = client
        # 实例属性直接定名（与内置工具 class 属性同风格）；`mcp_<server>_<tool>`
        # 前缀防不同 Server 同名工具冲突（09 §3.4）。
        self.name = f"mcp_{server}_{tool.name}"
        self.description = tool.description
        self.input_schema = tool.input_schema

    def is_read_only(self) -> bool:
        return False  # 保守声明（09 §3.4）：无静态只读标记，宁可先问

    def is_destructive(self) -> bool:
        return False  # 由权限规则按名裁决（06），本地不妄断

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return False  # MCP 调用有状态（Server 侧），保守分批

    def validate_input(self, input: dict[str, Any]) -> list[str]:
        return []  # 由 Server 侧校验，本地不透传约束（09 §3.4）

    async def execute(self, ctx: ToolContext, input: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        try:
            result = await self._client.call_tool(self._tool.name, input)
        except Exception as exc:
            return ToolResult(
                tool_use_id=ctx.tool_use_id,
                name=self.name,
                content=f"mcp_{self._server} 调用失败：{exc}",
                is_error=True,
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        return ToolResult(
            tool_use_id=ctx.tool_use_id,
            name=self.name,
            content=extract_text(result.content) or "(无文本返回)",
            is_error=result.is_error,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
