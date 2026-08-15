"""LoadSkill：按需加载完整 SOP（规格 09 §3.9）。

readonly 内置工具，与 ReadFile/Grep 同通道**不触发权限确认**。模型看到
system-reminder「可用 Skill」清单后，判断用户意图匹配即调用本工具——返回的
完整 SOP 作为工具结果进历史，下一轮模型按 SOP + 既有工具执行（inline 共享上下文）。

fork 模式（09 §3.10，T22）：依赖 10 未落地，降级 inline + 明确警告。
"""

from __future__ import annotations

from typing import Any

from kdagent.skill.manager import SkillManager
from kdagent.tools.base import ToolContext, ToolResult


class LoadSkill:
    """加载 Skill 完整 SOP 并注入对话。"""

    name = "LoadSkill"
    description = (
        "加载 Skill 的完整操作流程（SOP）。"
        "何时使用：system-reminder 列出可用 Skill，且用户请求匹配其中某个时，"
        "调用本工具把该 Skill 的完整 SOP 注入对话，按流程执行。"
        "参数：name 为 Skill 名（清单中的名字，如 commit）；arguments 为可选的"
        "用户需求原文，替换 SOP 中的 $ARGUMENTS 占位符。"
        "返回：完整 SOP（步骤/注意事项）；Skill 不存在返回错误。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill 名（如 commit）"},
            "arguments": {"type": "string", "description": "用户参数（可选，替换 $ARGUMENTS）"},
        },
        "required": ["name"],
    }
    category = "system"
    require_confirm = False

    def __init__(self, manager: SkillManager) -> None:
        self._manager = manager

    def is_read_only(self) -> bool:
        return True

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return False

    def validate_input(self, input: dict[str, Any]) -> list[str]:
        name = input.get("name")
        if not isinstance(name, str) or not name:
            return ["name 必填且为字符串"]
        return []

    async def execute(self, ctx: ToolContext, input: dict[str, Any]) -> ToolResult:
        name = input["name"]
        arguments = input.get("arguments")
        skill = self._manager.load(name, arguments if isinstance(arguments, str) else "")
        if skill is None:
            return ToolResult(
                tool_use_id=ctx.tool_use_id,
                name=self.name,
                content=f"Skill 不存在：{name}（可用 /skills 查看清单）",
                is_error=True,
            )
        content = skill.body
        if skill.mode == "fork":
            # T22：fork 依赖 10（SubAgent 基建）未落地，降级 inline + 明确警告。
            content = (
                "⚠ fork 模式尚未落地（依赖 SubAgent，规格 10），本次降级为 inline "
                "执行，共享当前对话上下文。\n\n" + content
            )
        return ToolResult(tool_use_id=ctx.tool_use_id, name=self.name, content=content)
