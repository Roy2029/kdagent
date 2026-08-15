"""对话区（规格 05 §3.1）：消息流容器 + markdown 渲染。

- 流式：text_delta 累积到同一 Static（纯文本实时显示），流结束/工具调用前
  一次性渲染成 Markdown（代码块/加粗/列表着色）。
- 转义安全：Static 内容一律 `markup.escape`，防止 `[`/`]` 被当 markup 解析破坏布局。
- `messages` 列表为测试/调试辅助，与渲染内容同源。
"""

from __future__ import annotations

from typing import Any

from textual.containers import VerticalScroll
from textual.markup import escape
from textual.widgets import Markdown, Static

from kdagent.engine.messages import Message, TextBlock


class ChatView(VerticalScroll):
    """消息流：user/assistant/system/error 各自成行；assistant 支持 markdown。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.messages: list[str] = []
        self._stream_text = ""
        self._stream_widget: Static | None = None

    # ---- 追加（语义化） ----------------------------------------------------

    def append_user(self, text: str) -> None:
        self.messages.append(text)
        self.mount(Static(f"[bold cyan]❯ {escape(text)}[/bold cyan]", classes="chat-user"))
        self.scroll_end(animate=False)

    def append_assistant(self, text: str) -> None:
        """完整 assistant 消息（resume 历史等）：直接 Markdown 渲染。"""
        self.messages.append(text)
        if text.strip():
            self.mount(Markdown(text, classes="chat-ai"))
            self.scroll_end(animate=False)

    def append_stream(self, text: str) -> None:
        """流式 text_delta：累积到当前行 Static，实时显示纯文本。"""
        self.messages.append(text)
        if self._stream_widget is None:
            self._stream_widget = Static("", classes="chat-ai")
            self.mount(self._stream_widget)
        self._stream_text += text
        self._stream_widget.update(escape(self._stream_text))
        self.scroll_end(animate=False)

    def finish_stream(self) -> None:
        """流结束：把累积文本替换为 Markdown 渲染（工具调用前 / LoopComplete / 中断）。"""
        if self._stream_widget is None:
            return
        self._stream_widget.remove()
        self._stream_widget = None
        if self._stream_text.strip():
            self.mount(Markdown(self._stream_text, classes="chat-ai"))
        self._stream_text = ""
        self.scroll_end(animate=False)

    def append_system(self, text: str) -> None:
        self.messages.append(text)
        self.mount(Static(f"[dim italic]{escape(text)}[/dim italic]", classes="chat-system"))
        self.scroll_end(animate=False)

    def append_error(self, text: str) -> None:
        self.messages.append(text)
        self.mount(Static(f"[bold red]✗ {escape(text)}[/bold red]", classes="chat-error"))
        self.scroll_end(animate=False)

    def append_testing(
        self,
        status: str,
        test_cmd: str,
        failed_tests: tuple[str, ...],
        summary: str,
    ) -> None:
        """TestRunner 结构化结果三态渲染（05 §5 239 / 02 §5 346）。

        状态徽标 + 命令 + 失败测试名 + 输出尾一行。markup 一律 escape。
        """
        marker, label = {
            "passed": ("✓", "测试通过"),
            "failed": ("✗", "测试失败"),
            "regression_detected": ("⚠", "回归检测"),
        }.get(status, ("·", "测试"))
        color = "green" if status == "passed" else "red" if status == "failed" else "yellow"
        lines = [f"[bold {color}]{marker} {label}[/bold {color}] · {escape(test_cmd)}"]
        if failed_tests:
            lines.append("失败用例：" + "、".join(escape(t) for t in failed_tests))
        tail = summary.splitlines()[-1] if summary else ""
        if tail:
            lines.append(f"[dim]{escape(tail)}[/dim]")
        text = "\n".join(lines)
        self.messages.append(text)
        self.mount(Static(text, classes="chat-test"))
        self.scroll_end(animate=False)

    def clear_messages(self) -> None:
        for widget in list(self.children):
            widget.remove()
        self.messages.clear()
        self._stream_text = ""
        self._stream_widget = None

    def load_conversation(self, messages: list[Message]) -> None:
        """`/session resume` 后把会话历史渲染进对话区（M1-f 收尾修复）。

        只渲染可读内容：user/assistant 文本；tool_use/tool_result 属调用过程，
        由 ToolRegion 承担，不进对话区。空消息（如纯 tool_result 的 user 消息）跳过。
        """
        self.clear_messages()
        for msg in messages:
            text = "".join(b.text for b in msg.content if isinstance(b, TextBlock))
            if not text:
                continue
            if msg.role == "user":
                self.append_user(text)
            elif msg.role == "assistant":
                self.append_assistant(text)
