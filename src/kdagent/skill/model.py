"""Skill 定义与 frontmatter 解析（规格 09 §3.7）。

Skill = 写给 Agent 的 SOP：Markdown 文件，frontmatter（YAML）声明元信息，
正文为可执行指令。两阶段加载只在需要时读正文，frontmatter 轻量注册进
system-reminder（渐进式披露，选择压力低）。

字段（09 §3.7）：name/description 必填，mode（inline 默认|fork）、model（可选，
空 = 沿用主对话模型，08 §3.4 缓存按模型分原则）、context（fork 生效，full 默认）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# 名称约束：小写字母/数字/连字符，字母/数字开头（09 §3.7，也是 /name 命令名）。
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# --- 分隔的 YAML frontmatter（DOTALL 让 body 里的 --- 不干扰）
_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

_MODE_VALUES = ("inline", "fork")
_CONTEXT_VALUES = ("full", "recent", "none")

# Skill 清单注入上限（09 §3.12：100 条 / 8KB，防挤占工作记忆）。
SKILL_LIST_LIMIT = 100
SKILL_LIST_MAX_BYTES = 8 * 1024


@dataclass(frozen=True, slots=True)
class SkillMeta:
    """轻量注册信息（第一阶段：启动扫描只解析 frontmatter，不读 body）。"""

    name: str
    description: str
    mode: str = "inline"
    model: str = ""  # 空 = 沿用主对话模型（显式指定会破坏该 Skill 会话的缓存命中）
    context: str = "full"  # fork 模式生效：full/recent/none
    path: Path | None = None  # 源文件路径（单文件 *.md 或目录型 SKILL.md）


@dataclass(frozen=True, slots=True)
class Skill(SkillMeta):
    """完整 Skill（第二阶段：LoadSkill 读入正文 + $ARGUMENTS 替换）。"""

    body: str = ""


def parse_skill_file(path: Path) -> SkillMeta | None:
    """解析 SKILL.md 的 frontmatter → SkillMeta；无法解析返回 None。

    扫描阶段对坏文件宽容（skills 目录混入 README 不阻断启动）：缺必填字段、
    非法 name、YAML 解析失败一律跳过。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return parse_skill_text(text, path=path)


def parse_skill_text(text: str, *, path: Path | None = None) -> SkillMeta | None:
    """从文件文本解析 frontmatter；缺 name/description 或非法 → None。"""
    m = _FRONT_RE.match(text)
    if not m:
        return None
    raw = _safe_load_front(m.group(1))
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    description = raw.get("description")
    if not isinstance(name, str) or not SKILL_NAME_RE.fullmatch(name):
        return None
    if not isinstance(description, str) or not description.strip():
        return None
    mode = raw.get("mode", "inline")
    if mode not in _MODE_VALUES:
        mode = "inline"
    model = raw.get("model", "")
    if not isinstance(model, str):
        model = ""
    context = raw.get("context", "full")
    if context not in _CONTEXT_VALUES:
        context = "full"
    return SkillMeta(
        name=name,
        description=description.strip(),
        mode=mode,
        model=model,
        context=context,
        path=path,
    )


def skill_body(text: str) -> str:
    """SKILL.md 正文 = frontmatter 之后的全部文本（strip 首尾空行）。"""
    m = _FRONT_RE.match(text)
    return text[m.end() :].strip() if m else text.strip()


def _safe_load_front(block: str) -> dict[str, Any] | None:
    try:
        loaded = yaml.safe_load(block)
    except yaml.YAMLError:
        return None
    return loaded if isinstance(loaded, dict) else None


def yaml_scalar(value: str) -> str:
    """frontmatter 写盘时的标量序列化：双引号包裹，保证含 ': ' 的描述可回读。"""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
