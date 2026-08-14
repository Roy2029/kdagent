"""工具调用活动区（规格 05 §3.1）："正在执行 X" 活动条 + 结果折叠一行。

避免工具调用刷屏对话区；ToolResultEvent 折叠为首行，成功绿/失败红 + 耗时。
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static


class ToolRegion(Vertical):
    """工具调用活动条（05 §3.1）。border 标题固定，内容行内堆叠。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._lines: list[str] = []

    def on_mount(self) -> None:
        self.border_title = "工具"
        self.display = False  # Claude Code 风格：初始收起，有活动才展开

    def _update_lines(self) -> None:
        # 注意：不能用 `_render` 命名——那是 Textual Widget 内部渲染方法，覆盖会崩。
        self.query_one("#tool-lines", Static).update("\n".join(self._lines))

    def show_running(self, name: str, input: dict[str, object]) -> None:
        """模型请求调用工具：活动条显示正在执行（ToolUseEvent）。"""
        args = " ".join(f"{k}={v}" for k, v in input.items())
        self._lines.append(f"[bold yellow]⚙ {name}[/bold yellow] {args}")
        self.display = True  # Claude Code 风格：有活动才展开
        self._update_lines()

    def show_result(self, name: str, content: str, is_error: bool, duration_ms: int) -> None:
        """结果折叠为一行：成功绿 ✓ / 失败红 ✗ + 耗时（ToolResultEvent）。"""
        color = "red" if is_error else "green"
        symbol = "✗" if is_error else "✓"
        first = content.splitlines()[0] if content else ""
        self._lines.append(f"[{color}]{symbol} {name}[/{color}] ({duration_ms}ms) {first}")
        self._update_lines()

    def reset(self) -> None:
        """一轮结束收起活动区（LoopCompleteEvent）。"""
        self._lines.clear()
        self.display = False
        self._update_lines()

    def compose(self) -> ComposeResult:
        yield Static("", id="tool-lines")
