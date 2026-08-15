"""MCP 工具生态（09 §3）：官方 SDK client + 延迟加载 + ToolSearch。

- `manager.MCPManager`：连接生命周期（启动即后台连接，失败隔离）
- `tool.MCPToolWrapper`：MCP 工具 → 内部 Tool 适配（命名 mcp_<server>_<tool>）
- `search.ToolSearch`：延迟工具拉取（按名/关键词，命中进下一轮 payload）
- `model`：MCPToolDef/MCPCallResult/MCPClient 协议 + extract_text
"""

from __future__ import annotations

from kdagent.mcp.manager import MCPManager, MCPServerConfig
from kdagent.mcp.model import MCPCallResult, MCPClient, MCPToolDef, extract_text
from kdagent.mcp.search import ToolSearch
from kdagent.mcp.tool import MCPToolWrapper

__all__ = [
    "MCPCallResult",
    "MCPClient",
    "MCPServerConfig",
    "MCPManager",
    "MCPToolDef",
    "MCPToolWrapper",
    "ToolSearch",
    "extract_text",
]
