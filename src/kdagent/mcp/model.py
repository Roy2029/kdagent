"""MCP 数据模型（09 §3）：工具定义 / 调用结果 / client 协议 + 文本提取。

与官方 SDK 类型解耦：这里定义协议层，`manager` 用官方 SDK 适配，测试注入 fake。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

MCPToolDefName = str


@dataclass(frozen=True, slots=True)
class MCPToolDef:
    """MCP `tools/list` 返回的工具定义（包装层：name/description/inputSchema）。"""

    name: MCPToolDefName
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MCPCallResult:
    """MCP `tools/call` 返回的结果（`content` 块数组 + isError）。"""

    content: list[Any]
    is_error: bool = False


class MCPClient(Protocol):
    """单个 MCP Server 的 client 协议（官方 SDK session 或测试 fake）。"""

    async def list_tools(self) -> list[MCPToolDef]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPCallResult: ...


def extract_text(content: list[Any]) -> str:
    """从 MCP 返回的 content 块数组提取全部 text 块拼接（09 §3.4 extract_text）。

    C4 修复（2026-08-29）：mcp 2.0 的 content 块是 pydantic `TextContent` 对象而非
    dict（实测 `isinstance(block, dict)` 为 False，此前恒返回空串）。同时兼容两者。
    """
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text") if block.get("type") == "text" else None
        else:
            text = getattr(block, "text", None) if getattr(block, "type", "") == "text" else None
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)
