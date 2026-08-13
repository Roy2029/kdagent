"""确认对话框（规格 05 §3.4）：极简 Y/N 确认。

ConfirmDialog：require_confirm 工具执行前弹（03 WriteFile/EditFile/Bash）。
ExitDialog：Ctrl+C /exit 的二次确认，避免误退。
两者均为 ModalScreen[bool]，dismiss(True/False)；Esc 默认关闭返回 None（= 拒绝/不退出）。
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class ConfirmDialog(ModalScreen[bool]):
    """允许/拒绝一次工具执行。结果经 dismiss 回传。"""

    def __init__(self, tool_name: str, tool_input: dict[str, Any]) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._tool_input = tool_input

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(f"允许执行 {self._tool_name}？", id="dialog-title")
            args = " ".join(f"{k}={v}" for k, v in self._tool_input.items()) or "（无参数）"
            yield Static(args, id="dialog-args")
            with Horizontal(id="dialog-actions"):
                yield Button("允许", variant="success", id="yes")
                yield Button("拒绝", variant="error", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class ExitDialog(ModalScreen[bool]):
    """退出二次确认（避免 Ctrl+C 误退）。"""

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("确定退出 KDAgent？", id="dialog-title")
            with Horizontal(id="dialog-actions"):
                yield Button("退出", variant="error", id="yes")
                yield Button("取消", variant="primary", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")
