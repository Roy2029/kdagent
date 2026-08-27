"""UI markup 转义工具（05 §3.1 转义安全）。

Textual 的 `textual.markup.escape` 只转义 `[tag]` 样式片段：`[` 后跟非字母
（换行/数字/括号/中文等）时漏网，仍被当 markup 开标签解析抛 MarkupError——
实测 Bash 参数含 `checks = [`（`[` 后是换行）直接经事件派发杀死整个 agent
循环（2026-08-27 be21512c 会话）。本模块提供针对**任意文本**的完整转义。

规则：`[` → `\\[`（字面量）；裸 `]` 在文本位置无害，无需转义。
"""

from __future__ import annotations


def escape_text(text: str) -> str:
    """把动态文本转义成可在 markup 字符串中安全渲染的字面量。"""
    return text.replace("[", "\\[")
