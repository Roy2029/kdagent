"""错误模式沉淀（08 §3.3 feedback 消费方，T33-3）。

写工具（EditFile/WriteFile）失败 → 事件驱动的**客观诊断**（纯函数启发式，
不依赖 LLM）→ 沉淀为 feedback 类记忆，复用 08 既有 MEMORY.md 索引注入链路
（新会话自动加载，避免同类错误）。

与 08 静默写的边界：静默写（extractor，§3.4）由 LLM 从会话提炼「用户/项目
长期信息」（主观判断、每轮末节流触发）；本模块记录「工具失败根因」（客观事实）。
失败事件是硬事实，不该等静默写稀释，也不该靠 LLM 判断——工具返回的 is_error +
失败消息即可归类。同名记忆由 MemoryStore.create 天然去重（同类根因一条）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from kdagent.memory.model import MemoryFile

# 诊断覆盖的写工具（规格聚焦 Edit 失败；WriteFile 同属破坏性写路径一并覆盖）。
_WRITE_TOOLS = frozenset({"EditFile", "WriteFile"})
# 记忆正文内嵌失败内容的上限（防单条记忆被长错误消息撑爆）。
_CONTENT_LIMIT = 200


class ErrorPatternKind(StrEnum):
    """写工具失败根因模式（T33-3 诊断分类，按真实失败消息归纳）。"""

    TARGET_MISSING = "edit-target-missing"  # 「文件不存在」→ 编辑不存在的目标（先读后编辑铁律）
    NO_MATCH = "edit-no-match"  # old_string 未找到 → 原文与文件内容不符（没先读）
    AMBIGUOUS_MATCH = "edit-ambiguous-match"  # 「old_string 出现 N 次」→ 未唯一匹配（先 Grep 定位）
    PERMISSION_DENIED = "edit-permission-denied"  # 无权限/只读
    INVALID_PATH = "edit-invalid-path"  # 路径越界/非法（06 路径沙箱）
    INVALID_INPUT = "edit-invalid-input"  # 参数校验失败（path 非绝对/必填缺失）
    OTHER = "edit-failure"  # 未归类


# 失败消息 → 根因的启发式关键词（小写匹配；同类各取关键短语，命中即归类）。
# 顺序即优先级：先精确（NO_MATCH/AMBIGUOUS 的专属短语）后宽泛（权限/路径/参数）。
_TARGET_MISSING_HINTS = ("文件不存在", "目录不存在", "目标是目录", "no such file", "not exist")
_NO_MATCH_HINTS = ("未在文件中找到", "old_string 未找到")
_AMBIGUOUS_HINTS = ("需唯一匹配", "出现")
_PERMISSION_HINTS = ("无权限", "权限拒绝", "权限不足", "permission denied", "read-only", "denied")
_INVALID_INPUT_HINTS = ("校验失败", "必填", "validation")
_INVALID_PATH_HINTS = ("绝对路径", "越界", "非法路径", "超出", "outside", "escape")


@dataclass(frozen=True, slots=True)
class ErrorPattern:
    """一次写工具失败事件的诊断结果。"""

    tool: str
    kind: ErrorPatternKind
    content: str  # 失败内容原文（记忆正文的事实来源）


def diagnose_failure(tool: str, content: str) -> ErrorPattern | None:
    """写工具失败 → 根因模式；非写工具返回 None（只沉淀写路径失败）。"""
    if tool not in _WRITE_TOOLS:
        return None
    text = content.lower()
    if any(h in text for h in _TARGET_MISSING_HINTS):
        kind = ErrorPatternKind.TARGET_MISSING
    elif any(h in text for h in _NO_MATCH_HINTS):
        kind = ErrorPatternKind.NO_MATCH
    elif any(h in text for h in _AMBIGUOUS_HINTS):
        kind = ErrorPatternKind.AMBIGUOUS_MATCH
    elif any(h in text for h in _PERMISSION_HINTS):
        kind = ErrorPatternKind.PERMISSION_DENIED
    elif any(h in text for h in _INVALID_INPUT_HINTS):
        kind = ErrorPatternKind.INVALID_INPUT  # 「校验失败」前缀比「绝对路径」更明确，先判
    elif any(h in text for h in _INVALID_PATH_HINTS):
        kind = ErrorPatternKind.INVALID_PATH
    else:
        kind = ErrorPatternKind.OTHER
    return ErrorPattern(tool=tool, kind=kind, content=content)


def pattern_dedup_key(pattern: ErrorPattern) -> str:
    """同类根因去重键（= kind；同名记忆 MemoryStore.create 兜底防重）。"""
    return pattern.kind.value


def pattern_memory(pattern: ErrorPattern) -> MemoryFile:
    """诊断结果 → feedback 类记忆文件（用户级，跨项目复用）。"""
    title, how_to = _KIND_META[pattern.kind]
    name = f"error-pattern-{pattern.kind.value}"
    excerpt = pattern.content[: _CONTENT_LIMIT]
    body = (
        f"工具 {pattern.tool} 失败，根因：{title}。\n"
        f"**失败内容**：{excerpt}\n"
        f"**Why:** 该失败在真实会话由工具直接返回（is_error），是可复现的根因而非偶发——"
        f"反复踩同一模式成本高，值得沉淀让后续会话避开。\n"
        f"**How to apply:** {how_to}"
    )
    return MemoryFile(
        name=name,
        description=f"{pattern.tool} 失败根因：{title}",
        type="feedback",
        content=body,
        index_line=f"- [{title}]({name}.md) — {pattern.tool} 失败根因",
    )


# 每种根因的标题 + 复用建议（How to apply 引导模型避免同类错误）。
_KIND_META: dict[ErrorPatternKind, tuple[str, str]] = {
    ErrorPatternKind.TARGET_MISSING: (
        "编辑目标不存在",
        "编辑/覆写不存在的文件必然失败——先 ReadFile/Grep 确认目标存在与内容，"
        "再改动（12 铁律「先读后编辑」）。",
    ),
    ErrorPatternKind.NO_MATCH: (
        "编辑原文与文件内容不符",
        "old_string 找不到说明没先读最新内容或文件已被改过——Edit 前必须 "
        "ReadFile 取原文，而非凭记忆。",
    ),
    ErrorPatternKind.AMBIGUOUS_MATCH: (
        "编辑匹配不唯一",
        "old_string 多处匹配会拒绝替换——先 Grep 定位上下文，带足够上下文的 old_string 使其唯一。",
    ),
    ErrorPatternKind.PERMISSION_DENIED: (
        "写权限被拒绝",
        "目标文件/目录无写权限（只读/系统保护）——换可写路径，或先确认权限状态再写。",
    ),
    ErrorPatternKind.INVALID_PATH: (
        "路径非法或越界",
        "路径必须是工作区内的绝对路径——写操作前先 resolve 确认落在 base 内，避免遍历越界被拒。",
    ),
    ErrorPatternKind.INVALID_INPUT: (
        "参数校验失败",
        "path 必须是绝对路径、必填字段不能缺——先看工具 input_schema 再构造参数，别省略 required。",
    ),
    ErrorPatternKind.OTHER: (
        "写操作失败（未归类）",
        "工具返回写失败但根因不在常见模式——先读失败消息全文定位，修复后重试。",
    ),
}
