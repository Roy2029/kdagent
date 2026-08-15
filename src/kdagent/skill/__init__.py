"""Skill 技能包（规格 09）：两阶段加载 / LoadSkill / skill-creator / /skills。

三级搜索（高优先级覆盖低优先级）：项目级 > 用户级 > 内置级（本包 builtin/）。
"""

from __future__ import annotations

from pathlib import Path

from kdagent.skill.loadskill import LoadSkill
from kdagent.skill.manager import SkillManager, build_skills_reminder
from kdagent.skill.model import (
    SKILL_LIST_LIMIT,
    SKILL_LIST_MAX_BYTES,
    Skill,
    SkillMeta,
    parse_skill_file,
    skill_body,
)
from kdagent.skill.skill_creator import SkillCreator

# 内置级 Skill 目录（开箱即用，随包分发；用户/项目级同名覆盖）。
BUILTIN_SKILLS_DIR = Path(__file__).parent / "builtin"

__all__ = [
    "BUILTIN_SKILLS_DIR",
    "LoadSkill",
    "SKILL_LIST_LIMIT",
    "SKILL_LIST_MAX_BYTES",
    "Skill",
    "SkillCreator",
    "SkillManager",
    "SkillMeta",
    "build_skills_reminder",
    "parse_skill_file",
    "skill_body",
]
