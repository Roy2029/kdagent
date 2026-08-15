"""MCPManager：连接生命周期（09 §3.3）。

**启动即后台连接**（与 MewCode/Claude Code 同策略）：懒连接到调用时才连会造成
「Agent 不知道工具存在 → 不调用 → 永不连接」死循环。启动时异步连接全部 Server，
`tools/list` 拿工具定义 → 包装注册进 registry（defer=True，09 §3.5 延迟加载）。

单 Server 生命周期：启动子进程（stdio）→ initialize 握手 → notifications/initialized
→ tools/list → 包装注册 → 后续 tools/call×N（连接缓存复用）。连接对象由
`AsyncExitStack` 持有，随 manager 生命周期存活。

失败隔离：一个 Server 连接失败只记 failed，不阻止启动（Token 过期不影响其他）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from kdagent.mcp.model import MCPCallResult, MCPToolDef
from kdagent.mcp.tool import MCPToolWrapper
from kdagent.tools.registry import ToolRegistry

# 可注入的连接器类型：server 配置 → (client, 工具定义列表)。真实实现走官方 SDK，
# 测试注入 fake（不需要真实 npx 子进程）。
ConnectFn = Callable[["MCPServerConfig"], Any]


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """`mcp_servers` 段单 Server 配置（09 §3.2）。"""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None


class _SDKClient:
    """官方 mcp ClientSession 的薄适配（满足 MCPClient 协议）。"""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def list_tools(self) -> list[MCPToolDef]:
        from kdagent.mcp.model import MCPToolDef

        listing = await self._session.list_tools()
        return [
            MCPToolDef(
                name=t.name,
                description=t.description or "",
                input_schema=dict(t.inputSchema or {}),
            )
            for t in listing.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPCallResult:
        result = await self._session.call_tool(name, arguments)
        return MCPCallResult(content=result.content or [], is_error=bool(result.isError))


class MCPManager:
    """加载配置 → 后台连接全部 Server → 工具注册进 registry。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        connect: ConnectFn | None = None,
    ) -> None:
        self._registry = registry
        # connect 注入点：默认 `_connect_stdio`（官方 SDK）；测试注入 fake。
        self._connect = connect or self._connect_stdio
        self._servers: dict[str, MCPServerConfig] = {}
        self._stacks: dict[str, AsyncExitStack] = {}  # 持有连接上下文，防 gc
        self._connected: set[str] = set()
        self._failed: dict[str, str] = {}

    # ---- 配置 ----

    def load_configs(self, mcp_servers: dict[str, object]) -> None:
        """解析 `mcp_servers` 段；非法条目跳过（零配置可用）。"""
        for name, raw in mcp_servers.items():
            if not isinstance(raw, dict):
                continue
            command = raw.get("command")
            if not isinstance(command, str) or not command.strip():
                continue
            args = raw.get("args")
            env = raw.get("env")
            self._servers[name] = MCPServerConfig(
                name=name,
                command=command,
                args=list(args) if isinstance(args, list) else [],
                env=dict(env) if isinstance(env, dict) else None,
            )

    # ---- 连接 ----

    async def connect_all(self) -> None:
        """并发连接全部 Server；单个失败只记 failed 不阻止其他。"""
        await asyncio.gather(
            *[self._connect_server(name, cfg) for name, cfg in self._servers.items()],
            return_exceptions=True,
        )

    async def _connect_server(self, name: str, cfg: MCPServerConfig) -> None:
        try:
            client, tools = await self._connect(cfg)
        except Exception as exc:
            self._failed[name] = f"{type(exc).__name__}: {exc}"
            return
        for tool in tools:
            self._registry.register(
                MCPToolWrapper(server=name, tool=tool, client=client), defer=True
            )
        self._connected.add(name)

    async def _connect_stdio(self, cfg: MCPServerConfig) -> tuple[_SDKClient, list[MCPToolDef]]:
        """真实 stdio 连接（官方 mcp SDK）：stdio_client + ClientSession 握手 + tools/list。"""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(command=cfg.command, args=list(cfg.args), env=cfg.env)
        stack = AsyncExitStack()
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        client = _SDKClient(session)
        tools = await client.list_tools()
        self._stacks[cfg.name] = stack  # 持有连接上下文，随 manager 存活
        return client, tools

    # ---- 状态（诊断 / 测试断言） ----

    @property
    def connected(self) -> set[str]:
        return set(self._connected)

    @property
    def failed(self) -> dict[str, str]:
        return dict(self._failed)

    async def aclose(self) -> None:
        """关闭全部连接（退出时调用，释放子进程）。"""
        for stack in self._stacks.values():
            await stack.aclose()
        self._stacks.clear()
