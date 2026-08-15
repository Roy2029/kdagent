"""确认对话框（规格 05 §3.4）：极简 Y/N 确认。

ConfirmDialog：require_confirm 工具执行前弹（03 WriteFile/EditFile/Bash）。
ExitDialog：Ctrl+C /exit 的二次确认，避免误退。
两者均为 ModalScreen[bool]，dismiss(True/False)；Esc 默认关闭返回 None（= 拒绝/不退出）。
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

# ModalScreen 子内容默认贴左上角；`ConfirmDialog/ExitDialog/PermissionDialog` 类
# 选择器让 dialog 居中。
# 注意：不能用 `Screen`/`ModalScreen` 基类选择器——Textual 8.2.8 下其 DEFAULT_CSS
# 会覆盖 align（实测基类选择器不生效，具体类选择器才居中）。
_DIALOG_CSS = """
ConfirmDialog, ExitDialog, PermissionDialog { align: center middle; }
#dialog { width: 60; height: auto; min-height: 9; max-height: 18;
          border: thick $primary; background: $surface; padding: 1 2; }
#dialog-title { text-align: center; }
#dialog-args { margin: 1 0; color: $text-muted; text-align: center; }
#dialog-actions { align: center middle; padding-top: 1; }
#dialog-actions Button { margin: 0 1; }
"""

# 工具参数过长时截断显示，避免长命令把按钮挤出弹窗（M1-i 用户反馈）。
_MAX_ARGS_LEN = 80


class ConfirmDialog(ModalScreen[bool]):
    """允许/拒绝一次工具执行。结果经 dismiss 回传。

    y/n 键直选（不依赖 focus）；方向键在按钮间移动焦点。
    """

    CSS = _DIALOG_CSS
    BINDINGS = [
        Binding("y", "yes", "允许", show=False),
        Binding("n", "no", "拒绝", show=False),
        Binding("left", "focus_yes", "左", show=False),
        Binding("right", "focus_no", "右", show=False),
    ]

    def __init__(self, tool_name: str, tool_input: dict[str, Any]) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._tool_input = tool_input

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(f"允许执行 {self._tool_name}？", id="dialog-title")
            args = " ".join(f"{k}={v}" for k, v in self._tool_input.items()) or "（无参数）"
            if len(args) > _MAX_ARGS_LEN:
                args = args[:_MAX_ARGS_LEN] + "…"
            yield Static(args, id="dialog-args")
            with Horizontal(id="dialog-actions"):
                yield Button("允许", variant="success", id="yes")
                yield Button("拒绝", variant="error", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    def action_focus_yes(self) -> None:
        self.query_one("#yes", Button).focus()

    def action_focus_no(self) -> None:
        self.query_one("#no", Button).focus()


class PermissionDialog(ModalScreen[str]):
    """L5 HITL 权限审批（06 §3.7）：允许 / 拒绝 / 始终允许。

    结果经 dismiss 回传裁决串（allow/deny/allow_always）；Esc 视为拒绝（None → deny）。
    y/n/a 键直选；方向键在按钮间移动焦点。
    """

    CSS = _DIALOG_CSS
    BINDINGS = [
        Binding("y", "allow", "允许", show=False),
        Binding("n", "deny", "拒绝", show=False),
        Binding("a", "allow_always", "始终允许", show=False),
        Binding("left", "focus_prev", "左", show=False),
        Binding("right", "focus_next", "右", show=False),
    ]

    def __init__(self, tool_name: str, summary: str) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._summary = summary

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(f"权限请求：{self._tool_name}", id="dialog-title")
            args = self._summary or "（无参数）"
            if len(args) > _MAX_ARGS_LEN:
                args = args[:_MAX_ARGS_LEN] + "…"
            yield Static(args, id="dialog-args")
            with Horizontal(id="dialog-actions"):
                yield Button("允许 (y)", variant="success", id="allow")
                yield Button("拒绝 (n)", variant="error", id="deny")
                yield Button("始终允许 (a)", variant="primary", id="allow_always")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(str(event.button.id))

    def action_allow(self) -> None:
        self.dismiss("allow")

    def action_deny(self) -> None:
        self.dismiss("deny")

    def action_allow_always(self) -> None:
        self.dismiss("allow_always")

    def action_focus_prev(self) -> None:
        self.query_one("#allow", Button).focus()

    def action_focus_next(self) -> None:
        self.query_one("#deny", Button).focus()


class ExitDialog(ModalScreen[bool]):
    """退出二次确认（避免 Ctrl+C 误退）。y 退出 / n 取消，方向键切换。"""

    CSS = _DIALOG_CSS
    BINDINGS = [
        Binding("y", "yes", "退出", show=False),
        Binding("n", "no", "取消", show=False),
        Binding("left", "focus_yes", "左", show=False),
        Binding("right", "focus_no", "右", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("确定退出 KDAgent？", id="dialog-title")
            with Horizontal(id="dialog-actions"):
                yield Button("退出", variant="error", id="yes")
                yield Button("取消", variant="primary", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    def action_focus_yes(self) -> None:
        self.query_one("#yes", Button).focus()

    def action_focus_no(self) -> None:
        self.query_one("#no", Button).focus()
