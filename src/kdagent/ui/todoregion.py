"""Todo 面板（规格 05 §3.2b）：从会话状态渲染 todo → task → steps。

数据源：SessionRecord.todos（`04` §3.2），TodoWrite 每次调用后实时更新（03 §3.6）。
只读展示：todo 更新只经 TodoWrite 工具，UI 不提供手动编辑（05 §3.2b）。
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from kdagent.sessions.records import TodoItemRecord
from kdagent.ui._markup import escape_text

_COMPLETED = "completed"


class TodoRegion(Vertical):
    """todo 面板：border 标题固定，内容按 group 还原三层结构。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._items: list[TodoItemRecord] = []

    def on_mount(self) -> None:
        self.border_title = "待办"
        self.display = False  # Claude Code 风格：初始收起，有 todo 才展开

    def show_todos(self, items: list[TodoItemRecord]) -> None:
        """TodoWrite 回调 → 实时刷新（03 §3.6 数据流：05 从会话状态读渲染）。"""
        self._items = list(items)
        self.display = bool(items)  # Claude Code 风格：空面板收起，有内容才展开
        self._update_lines()

    def reset(self) -> None:
        """切换会话 / 清空时收起面板。"""
        self._items = []
        self.display = False
        self._update_lines()

    def _update_lines(self) -> None:
        # 注意：不能用 `_render` 命名——那是 Textual Widget 内部渲染方法，覆盖会崩。
        self.query_one("#todo-lines", Static).update(self._render_lines())

    def _render_lines(self) -> str:
        if not self._items:
            return "[dim]（暂无待办）[/dim]"
        lines: list[str] = []
        current_group = ""
        for item in self._items:
            if item.group and item.group != current_group:
                lines.append(f"[bold]{escape_text(item.group)}[/bold]")
                current_group = item.group
            mark = "x" if item.status == _COMPLETED else " "
            # item.content 是模型生成文本，可能含 `[`（代码片段等）——必须
            # escape_text，否则 `[` 被当 markup 开标签解析抛 MarkupError。
            lines.append(f"  [{mark}] {escape_text(item.content)}")
            for step in item.steps or []:
                criteria = (
                    f" [dim]\\[判据: {escape_text(step.accept_criteria or '')}][/dim]"
                    if step.accept_criteria
                    else ""
                )
                lines.append(f"    - {escape_text(step.description)}{criteria}")
        return "\n".join(lines)

    def compose(self) -> ComposeResult:
        yield Static("", id="todo-lines")
