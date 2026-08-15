"""skill-creator：创建 Skill 的元工具（规格 09 T24，元工具即验收样例）。

写入用户级 skills 目录（~/.kdagent/skills/，经 SkillManager.writable_dir），
写盘后刷新索引（本会话立即可 LoadSkill）。同名已存在拒绝覆盖——静默覆盖用户
内容有风险，用户可先 ReadFile 查看再决定。仅创建新文件 → 无需权限确认。
"""

from __future__ import annotations

from typing import Any

from kdagent.skill.manager import SkillManager
from kdagent.tools.base import ToolContext, ToolResult


class SkillCreator:
    """把用户重复解释的流程沉淀为可复用 Skill。"""

    name = "skill-creator"
    description = (
        "创建新的 Skill（可复用的操作流程，写给 Agent 的 SOP）。"
        "何时使用：用户想把一个可复用流程/规范沉淀为 Skill 时。"
        "参数：name（小写字母/数字/连字符）；description 一句话描述（用于意图识别）；"
        "instructions 为 SOP 正文（步骤、注意事项，越精确越好）；mode 可选 "
        "inline/fork（默认 inline）。"
        "创建后立即生效，本会话即可 LoadSkill 加载，后续会话自动出现在可用清单。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill 名（小写字母/数字/连字符）"},
            "description": {"type": "string", "description": "一句话描述（意图识别用）"},
            "instructions": {"type": "string", "description": "SOP 正文（步骤/注意事项）"},
            "mode": {"type": "string", "enum": ["inline", "fork"], "description": "默认 inline"},
        },
        "required": ["name", "description", "instructions"],
    }
    category = "system"
    require_confirm = False

    def __init__(self, manager: SkillManager) -> None:
        self._manager = manager

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return False

    def validate_input(self, input: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        for key in ("name", "description", "instructions"):
            value = input.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{key} 必填且为非空字符串")
        return errors

    async def execute(self, ctx: ToolContext, input: dict[str, Any]) -> ToolResult:
        name = input["name"]
        description = input["description"]
        instructions = input["instructions"]
        mode = input.get("mode", "inline")
        try:
            path = self._manager.create(
                name, description, instructions, mode=mode if isinstance(mode, str) else "inline"
            )
        except (ValueError, FileExistsError) as exc:
            return ToolResult(
                tool_use_id=ctx.tool_use_id, name=self.name, content=str(exc), is_error=True
            )
        return ToolResult(
            tool_use_id=ctx.tool_use_id,
            name=self.name,
            content=f"已创建 Skill /{name}：{path}\n后续可用 LoadSkill(\"{name}\") 加载。",
        )
