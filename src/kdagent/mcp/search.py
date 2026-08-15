"""ToolSearch：延迟工具拉取工具（09 §3.5）。

常驻内置工具。模型两种用法：
- `select: "mcp_github_search_issues"` → 按名精确拉取（模型已知道用哪个）
- `keywords: "prometheus"` → 在延迟工具的名称+描述里搜（模型不确定具体名）

命中 → 返回完整 schema + `mark_discovered` → 下一轮 payload 进 tools 字段。
搜索范围 = 尚未发现的延迟工具（MCP 工具；内置"深载"工具同机制扩展）。
"""

from __future__ import annotations

from typing import Any

from kdagent.tools.base import ToolContext, ToolResult
from kdagent.tools.registry import ToolRegistry

# 延迟工具 schema 渲染（完整喂给模型，供正常调用）。
_SCHEMA_TEMPLATE = """工具 {name}（延迟，已加载，下一轮可用）

{description}

输入 Schema：
{schema}
"""


class ToolSearch:
    """搜索并加载延迟工具（返回完整 schema，下一轮进 tools 字段）。"""

    name = "ToolSearch"
    description = (
        "搜索并加载延迟工具（MCP Server 工具）。两个用法二选一："
        "select 精确指定工具名（如 \"mcp_github_search_issues\"）按名拉取；"
        "keywords 在延迟工具的名称+描述里搜索（不确定具体名时用）。"
        "命中后返回该工具的完整描述与输入 Schema，下一轮即可直接调用。"
        "何时使用：system-reminder 列出可通过 ToolSearch 加载的工具，需要其中之一时。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "select": {"type": "string", "description": "要加载的工具全名（mcp_<server>_<tool>）"},
            "keywords": {"type": "string", "description": "按关键词搜索延迟工具"},
        },
        "anyOf": [{"required": ["select"]}, {"required": ["keywords"]}],
    }
    category = "system"
    require_confirm = False

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def is_read_only(self) -> bool:
        return True

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return False

    def validate_input(self, input: dict[str, Any]) -> list[str]:
        if not input.get("select") and not input.get("keywords"):
            return ["select 与 keywords 至少填一个"]
        return []

    async def execute(self, ctx: ToolContext, input: dict[str, Any]) -> ToolResult:
        sel = input.get("select")
        if sel:
            return self._by_name(sel, ctx.tool_use_id)
        return self._by_keywords(str(input.get("keywords", "")), ctx.tool_use_id)

    # ---- 内部 ----

    def _by_name(self, name: str, tool_use_id: str) -> ToolResult:
        tool = self._registry.get(name)
        if tool is None or not self._registry.should_defer(name):
            return ToolResult(
                tool_use_id=tool_use_id,
                name=self.name,
                content=f"延迟工具不存在：{name}（可用 ToolSearch keywords 搜索）",
                is_error=True,
            )
        self._registry.mark_discovered(name)
        return ToolResult(
            tool_use_id=tool_use_id,
            name=self.name,
            content=_SCHEMA_TEMPLATE.format(
                name=name, description=tool.description, schema=_schema_text(tool.input_schema)
            ),
        )

    def _by_keywords(self, keywords: str, tool_use_id: str) -> ToolResult:
        kw = keywords.strip().lower()
        if not kw:
            return ToolResult(
                tool_use_id=tool_use_id, name=self.name, content="keywords 为空", is_error=True
            )
        hits: list[str] = []
        for name in self._registry.deferred_tool_names():
            tool = self._registry.get(name)
            if tool is None:
                continue
            if kw in name.lower() or kw in (tool.description or "").lower():
                first_line = tool.description.splitlines()[0] if tool.description else ""
                hits.append(f"- {name}：{first_line}")
        if not hits:
            return ToolResult(
                tool_use_id=tool_use_id,
                name=self.name,
                content=f"未命中延迟工具（关键词：{keywords}）",
                is_error=True,
            )
        return ToolResult(
            tool_use_id=tool_use_id,
            name=self.name,
            content="命中延迟工具（可用 select 精确加载）：\n" + "\n".join(hits),
        )


def _schema_text(schema: dict[str, Any]) -> str:
    import json

    return json.dumps(schema, ensure_ascii=False, indent=2)
