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

_COMPLETED = "completed"


class TodoRegion(Vertical):
    """todo 面板：border 标题固定，内容按 group 还原三层结构。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._items: list[TodoItemRecord] = []

    def on_mount(self) -> None:
        self.border_title = "待办"

    def show_todos(self, items: list[TodoItemRecord]) -> None:
        """TodoWrite 回调 → 实时刷新（03 §3.6 数据流：05 从会话状态读渲染）。"""
        self._items = list(items)
        self._update_lines()

    def reset(self) -> None:
        """切换会话 / 清空时收起面板。"""
        self._items = []
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
                lines.append(f"[bold]{item.group}[/bold]")
                current_group = item.group
            mark = "x" if item.status == _COMPLETED else " "
            lines.append(f"  [{mark}] {item.content}")
            for step in item.steps or []:
                criteria = (
                    f" [dim][判据: {step.accept_criteria}][/dim]" if step.accept_criteria else ""
                )
                lines.append(f"    - {step.description}{criteria}")
        return "\n".join(lines)

    def compose(self) -> ComposeResult:
        yield Static("", id="todo-lines")
