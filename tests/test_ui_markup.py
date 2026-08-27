"""`ui/_markup.py` 转义工具测试（05 §3.1 转义安全）。

Textual `markup.escape` 只转义 `[tag]` 样式片段，`[`+非字母 漏网（实测崩）；
`escape_text` 全量转义 `[`，保证任意动态文本可安全拼入 markup 字符串。
"""

from __future__ import annotations

from textual.markup import to_content

from kdagent.ui._markup import escape_text


def test_escape_text_handles_bare_bracket_followed_by_newline() -> None:
    """崩实场景：`checks = [` 后是换行（Textual escape 漏网的形态）。"""
    args = "checks = [\n    ('有 <canvas id=\"board\">', 'id=\"board\"' in html),\n]"
    content = to_content(f"[bold]参数：[/bold]{escape_text(args)}")
    assert content.plain == f"参数：{args}"


def test_escape_text_preserves_real_markup_tags() -> None:
    """内部 `[bold]` 标签不受影响，只有动态文本段被转义。"""
    content = to_content(f"[bold]参数：[/bold]{escape_text('a[b]c')}")
    assert content.plain == "参数：a[b]c"


def test_escape_text_renders_code_with_brackets() -> None:
    """代码片段（列表/下标/时间戳）字面量保留。"""
    content = to_content(escape_text("error in [a,b]  [17:32]"))
    assert content.plain == "error in [a,b]  [17:32]"
