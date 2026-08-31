"""工具调用活动区（规格 05 §3.1）：每条工具调用一个可展开/收起的详情条目。

「正在执行 X」活动条 + 结果折叠一行；点击条目展开完整参数 + 输出全文。
历史保留（U1：LoopComplete 后仍可回看，超上限裁剪最老条目，防无限增长）。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count
from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Collapsible, Static

from kdagent.ui._markup import escape_text

# U1：工具区保留最近条目数（超出丢最老；配合 `#tools` max-height 不爆屏）。
_MAX_ENTRIES = 10

# U1：全局递增序号（跨 trim 不重置），条目摘要显示 `#N` 使调用顺序可辨。
_SEQ = count(1)


@dataclass(slots=True)
class ToolEntry:
    """一条工具调用：running 中只有参数；结果到达后补输出全文。"""

    name: str
    args: str  # 完整参数文本
    content: str = ""  # 完整输出
    is_error: bool = False
    duration_ms: int = 0
    running: bool = True
    seq: int = 0  # U1：调用序号（显示顺序标识）


class ToolRegion(Vertical):
    """工具调用活动条（05 §3.1）：Collapsible 条目，点击展开完整内容。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._entries: list[ToolEntry] = []

    def on_mount(self) -> None:
        self.border_title = "工具"
        self.display = False  # Claude Code 风格：初始收起，有活动才展开

    def _summarize(self, e: ToolEntry) -> str:
        """一行摘要（可含 Rich markup）：序号 + 执行中 ⚙ / 结果 ✓✗ + 首行。

        动态文本（参数/输出首行）一律 `escape`——实测 Bash 参数含 `checks = [`
        时 `[` 被当 markup 开标签解析，tokenizer 抛 MarkupError，经事件派发
        杀死整个 agent 循环（见 be21512c 会话）。
        """
        tag = f"#{e.seq} " if e.seq else ""
        if e.running:
            return f"[bold yellow]⚙ {tag}{escape_text(e.name)}[/bold yellow] {escape_text(e.args)}"
        color = "red" if e.is_error else "green"
        symbol = "✗" if e.is_error else "✓"
        first = escape_text(e.content.splitlines()[0]) if e.content else ""
        return f"[{color}]{symbol} {tag}{escape_text(e.name)}[/{color}] ({e.duration_ms}ms) {first}"

    def _detail(self, e: ToolEntry) -> str:
        """详情体：完整参数 + 输出全文（动态文本一律 escape_text，防 markup 解析破坏）。"""
        parts = [f"[bold]参数：[/bold]{escape_text(e.args)}"]
        if e.content:
            parts.append(f"[bold]输出：[/bold]{escape_text(e.content)}")
        else:
            parts.append("[dim]（执行中…）[/dim]")
        return "\n".join(parts)

    def _rebuild(self) -> None:
        """全量重建 Collapsible 列表（条目数 ≤ 10，开销可接受）。"""
        container = self.query_one("#tool-items", VerticalScroll)
        container.remove_children()
        for e in self._entries:
            # 摘要行常显，点击展开完整内容（U1 详情页）。
            coll = Collapsible(
                Static(self._detail(e), classes="tool-detail"),
                title=self._summarize(e),
                collapsed=True,
            )
            container.mount(coll)
        self.border_title = f"工具（{len(self._entries)}）"
        self.display = True  # 有活动展开；历史保留不清空（U1 可回看）

    def show_running(self, name: str, input: dict[str, object]) -> None:
        """模型请求调用工具：追加一条 running 条目（ToolUseEvent）。"""
        args = " ".join(f"{k}={v}" for k, v in input.items())
        self._entries.append(
            ToolEntry(name=name, args=args, running=True, seq=next(_SEQ))
        )
        self._trim()
        self._rebuild()

    def show_result(self, name: str, content: str, is_error: bool, duration_ms: int) -> None:
        """结果填充到最后一个同名 running 条目（C1 并行工具配对），一行折叠。

        找不到配对（结果先到 / 单测直接发结果）时自建结果条目，不丢信息。
        """
        for e in reversed(self._entries):
            if e.running and e.name == name:
                e.content = content
                e.is_error = is_error
                e.duration_ms = duration_ms
                e.running = False
                break
        else:
            self._entries.append(
                ToolEntry(
                    name=name,
                    args="",
                    content=content,
                    is_error=is_error,
                    duration_ms=duration_ms,
                    running=False,
                    seq=next(_SEQ),
                )
            )
        self._trim()
        self._rebuild()

    def reset(self) -> None:
        """一轮结束（LoopCompleteEvent）：保留历史回看，仅修剪孤儿 running 条目。

        不再清空区（U1：完成后工具详情仍可展开查看）；`_trim` 防无限增长。
        """
        self._entries = [e for e in self._entries if not e.running]
        self._trim()
        self._rebuild()

    def _trim(self) -> None:
        if len(self._entries) > _MAX_ENTRIES:
            self._entries = self._entries[-_MAX_ENTRIES:]

    def compose(self) -> ComposeResult:
        # U1：VerticalScroll 使展开后的长输出可滚动（超出 #tools max-height 可见）。
        yield VerticalScroll(id="tool-items")
