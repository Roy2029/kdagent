"""对话区（规格 05 §3.1）：RichLog 滚动日志 + 语义化追加封装。

流式 text_delta 逐条 append；`messages` 列表为测试/调试辅助，与渲染内容同源。
"""

from __future__ import annotations

from typing import Any

from textual.widgets import RichLog


class ChatView(RichLog):
    """消息流（用户/助手/工具/系统）。markup 开关富文本着色。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(highlight=True, markup=True, auto_scroll=True, **kwargs)
        self.messages: list[str] = []

    def _write(self, text: str) -> None:
        self.write(text)
        self.messages.append(text)

    def append_user(self, text: str) -> None:
        self._write(f"[bold cyan]❯ {text}[/bold cyan]")

    def append_assistant(self, text: str) -> None:
        self._write(f"[green]{text}[/green]")

    def append_stream(self, text: str) -> None:
        self._write(text)

    def append_system(self, text: str) -> None:
        self._write(f"[dim italic]{text}[/dim italic]")

    def append_error(self, text: str) -> None:
        self._write(f"[bold red]✗ {text}[/bold red]")

    def clear_messages(self) -> None:
        self.clear()
        self.messages.clear()
