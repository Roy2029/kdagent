"""记忆数据模型（08 §3.2）：四类记忆 + frontmatter 解析/序列化。

记忆是普通 Markdown 文件——frontmatter（name/description/type）是检索/治理
元信息，正文是事实。文件名 = `{name}.md`；分类决定落盘目录：
user/feedback → 用户级 `~/.kdagent/memory/`，project/reference → 项目级
`{work_dir}/.kdagent/memory/`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

import yaml

MemoryType = Literal["user", "feedback", "project", "reference"]

MEMORY_TYPES: tuple[MemoryType, ...] = ("user", "feedback", "project", "reference")

# 索引注入上限（08 §3.2/§3.9）：固定开销 ≤ 1-2% 窗口。
INDEX_MAX_LINES = 200
INDEX_MAX_BYTES = 25 * 1024

_FRONTMATTER_RE = re.compile(r"^---\s*\n(?P<fm>.*?)\n---\s*\n?(?P<body>.*)$", re.S)


@dataclass(frozen=True, slots=True)
class MemoryFile:
    """一条记忆。`content` 是 frontmatter 之后的正文；`index_line` 是 MEMORY.md 指针行。"""

    name: str
    description: str
    type: MemoryType
    content: str = ""
    index_line: str = ""


def default_index_line(f: MemoryFile) -> str:
    """缺省索引行：`- [name]({name}.md) — description`（LLM 可给更精炼标题）。"""
    title = f.description or f.name
    return f"- [{title}]({f.name}.md) — {f.description}"


def serialize_memory(f: MemoryFile) -> str:
    """序列化为 Markdown：frontmatter + 空行 + 正文。"""
    head = "---\n"
    head += f"name: {f.name}\n"
    head += f"description: {f.description}\n"
    head += f"type: {f.type}\n"
    return f"{head}---\n\n{f.content.rstrip()}\n"


def parse_memory(text: str, *, fallback_name: str = "") -> MemoryFile | None:
    """解析记忆文件文本；frontmatter 缺失/非法 → None（不静默造数据）。"""
    m = _FRONTMATTER_RE.match(text)
    if m is None:
        return None
    try:
        meta = yaml.safe_load(m.group("fm"))
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None
    name = str(meta.get("name") or fallback_name or "").strip()
    description = str(meta.get("description", "")).strip()
    raw_type = meta.get("type", "")
    if name and raw_type in MEMORY_TYPES:
        return MemoryFile(
            name=name,
            description=description,
            type=raw_type,
            content=m.group("body").strip("\n"),
        )
    return None


def normalize_ops(raw: Any) -> list[dict[str, Any]]:
    """提取返回的 JSON → 规范化操作集（容错字段类型）。"""
    if isinstance(raw, dict):
        items = raw.get("ops")
    elif isinstance(raw, list):
        items = raw
    else:
        return []
    ops: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return ops
    for item in items:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action", "")).strip()
        if action not in ("create", "update", "delete"):
            continue
        ops.append(item)
    return ops
