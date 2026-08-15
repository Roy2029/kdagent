"""Agent 定义与 frontmatter 解析（规格 10 §3.4）。

Agent = 写给子 Agent 的系统提示：Markdown 文件，frontmatter（YAML）声明元信息，
正文是子 Agent 启动时的系统提示（决定这个新 Agent 是谁、能做什么）。

与 Skill（09）同构异义：结构都是 YAML frontmatter + Markdown body（解析逻辑复用）；
语义不同——Skill body 是注入对话的 SOP 指令（经 LoadSkill 进对话历史），
Agent body 是子 Agent 启动时的**系统提示**（伴随整个生命周期）。

字段（规格 10 §3.4）：name/description 必填；tools/disallowedTools（白名单确定范围、
黑名单排除——优先黑名单）；model（inherit 默认）；maxTurns（20 默认）；
permissionMode（default/acceptEdits/dontAsk）；isolation（worktree）；background。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Agent 类型名约束：小写字母/数字/连字符，字母/数字开头（= /name 命令名，同 Skill）。
AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# --- 分隔的 YAML frontmatter（DOTALL 让 body 里的 --- 不干扰）
_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

_PERMISSION_MODES = ("default", "acceptEdits", "dontAsk")
_ISOLATIONS = ("worktree",)

DEFAULT_MAX_TURNS = 20


@dataclass(frozen=True, slots=True)
class AgentDef:
    """解析后的 Agent 定义（内置 + 用户自定义统一模型）。"""

    name: str  # = agentType
    description: str  # = whenToUse
    tools: tuple[str, ...] = ()  # 白名单：非空则子 Agent 只能用这些
    disallowed_tools: tuple[str, ...] = ()  # 黑名单：从中排除
    model: str = "inherit"  # inherit = 沿用父模型
    max_turns: int = DEFAULT_MAX_TURNS
    permission_mode: str = "default"  # default/acceptEdits/dontAsk
    isolation: str = ""  # worktree（M5-b 落地）；空 = 共享主目录
    background: bool = False  # 定义级默认后台（当前仅 Fork 强制）
    system_prompt: str = ""  # 正文：子 Agent 系统提示
    path: Path | None = None  # 源文件路径


def parse_agent_file(path: Path) -> AgentDef | None:
    """解析 Agent 定义文件的 frontmatter → AgentDef；无法解析返回 None。

    扫描阶段对坏文件宽容（agents 目录混入 README 不阻断启动）：缺必填字段、
    非法 name、YAML 解析失败一律跳过。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return parse_agent_text(text, path=path)


def parse_agent_text(text: str, *, path: Path | None = None) -> AgentDef | None:
    """从文件文本解析 frontmatter；缺 name/description 或非法 → None。"""
    m = _FRONT_RE.match(text)
    if not m:
        return None
    raw = _safe_load_front(m.group(1))
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    description = raw.get("description")
    if not isinstance(name, str) or not AGENT_NAME_RE.fullmatch(name):
        return None
    if not isinstance(description, str) or not description.strip():
        return None
    tools = _str_list(raw.get("tools"))
    disallowed = _str_list(raw.get("disallowedTools"))
    model = raw.get("model", "inherit")
    if not isinstance(model, str) or not model:
        model = "inherit"
    max_turns = raw.get("maxTurns", DEFAULT_MAX_TURNS)
    if not isinstance(max_turns, int) or max_turns <= 0:
        max_turns = DEFAULT_MAX_TURNS
    permission_mode = raw.get("permissionMode", "default")
    if permission_mode not in _PERMISSION_MODES:
        permission_mode = "default"
    isolation = raw.get("isolation", "")
    if not isinstance(isolation, str) or isolation not in _ISOLATIONS:
        isolation = ""
    background = bool(raw.get("background", False))
    return AgentDef(
        name=name,
        description=description.strip(),
        tools=tools,
        disallowed_tools=disallowed,
        model=model,
        max_turns=max_turns,
        permission_mode=permission_mode,
        isolation=isolation,
        background=background,
        system_prompt=text[m.end() :].strip(),
        path=path,
    )


def _str_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(v) for v in value if isinstance(v, str) and v.strip())


def _safe_load_front(block: str) -> dict[str, Any] | None:
    try:
        loaded = yaml.safe_load(block)
    except yaml.YAMLError:
        return None
    return loaded if isinstance(loaded, dict) else None


def yaml_scalar(value: str) -> str:
    """frontmatter 写盘时的标量序列化：双引号包裹，保证含 ': ' 的描述可回读。"""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
