"""07 §3.7 T9 `/metrics` 聚合指标面板（Textual Screen，方案 A）。

数据层由 `obs.metrics`（D70）提供——读 trace JSONL 按 session 聚合，本屏只做
取数 + 渲染装配。渲染逻辑全部收敛到模块级纯函数 `render_metrics_text`（可单测），
Screen 只负责从 obs_dir 取数 + Static 显示 + Esc 关闭，与 11 §3.4 评测报告屏同构。
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical
from textual.screen import Screen
from textual.widgets import Static

from kdagent.obs.metrics import SessionMetrics, aggregate_metrics, session_metrics


def _fmt_tokens(n: int) -> str:
    return f"{n:,}"


def render_metrics_text(
    session_id: str,
    cur: SessionMetrics | None,
    history: list[SessionMetrics],
) -> str:
    """渲染 /metrics 面板文本（07 §3.5 聚合口径；纯函数供单测）。

    `cur` 为当前会话聚合桶（无 trace 则 None）；`history` 为其他会话桶
    （内部剔除 cur 所属会话，避免当前段与历史段重复计数）。
    """
    lines = ["可观测性 Metrics（07 §3.5）", ""]
    if cur is not None:
        history = [sm for sm in history if sm.session_id != cur.session_id]
    if cur is None and not history:
        lines.append(f"当前会话 {session_id} 暂无 trace 数据（未启用可观测性或尚无活动）。")
        return "\n".join(lines)
    if cur is not None:
        lines.append(f"── 当前会话 {cur.session_id} ──")
        lines.append(
            f"Trace {cur.traces} 个    LLM 调用 {cur.llm_calls} 次（错误 {cur.llm_errors}）"
        )
        lines.append(
            f"Token：in {_fmt_tokens(cur.input_tokens)} / out {_fmt_tokens(cur.output_tokens)}"
            f" / cache读 {_fmt_tokens(cur.cache_read_tokens)}"
            f" / cache写 {_fmt_tokens(cur.cache_creation_tokens)}"
        )
        lines.append(f"成本：¥{cur.cost_cny:.4f}（01 计价表，cache 按命中价）")
        lines.append(f"LLM 延迟：avg {cur.llm_avg_ms:.0f}ms / p99 {cur.llm_p99_ms}ms")
        if cur.providers:
            lines.append(f"Provider：{'、'.join(sorted(cur.providers))}")
        if cur.tools:
            lines.append("")
            lines.append("── 工具统计 ──")
            for name in sorted(cur.tools):
                stat = cur.tools[name]
                lines.append(
                    f"  {name}：{stat.calls} 次  成功率 {stat.success_rate:.0%}"
                    f"  avg {stat.avg_ms:.0f}ms"
                )
        if cur.compact:
            lines.append("")
            lines.append("── 压缩触发 ──")
            lines.append("  " + " / ".join(f"{k} {v}" for k, v in sorted(cur.compact.items())))
        if cur.permission:
            lines.append("")
            lines.append("── 权限裁决 ──")
            lines.append("  " + " / ".join(f"{k} {v}" for k, v in sorted(cur.permission.items())))
        lines.append("")
        lines.append(f"── Hook 运行 ──\n  {cur.hook_runs} 次")
    if history:
        lines.append("")
        lines.append(f"── 历史会话（{len(history)} 个）──")
        for sm in history:
            lines.append(
                f"  {sm.session_id}  trace {sm.traces}  llm {sm.llm_calls}"
                f"  token {_fmt_tokens(sm.input_tokens)}  ¥{sm.cost_cny:.4f}"
            )
    return "\n".join(lines)


_CSS = """
MetricsScreen {
    layout: vertical;
    padding: 1 2;
}
#metrics-screen { height: 100%; layout: vertical; }
#metrics-header { height: auto; padding-bottom: 1; text-style: bold; }
#metrics-body { height: 1fr; border: round $primary; padding: 0 1; }
"""


class MetricsScreen(Screen[None]):
    """聚合指标面板：`/metrics` 打开，Esc 关闭（07 §3.7 T9 方案 A，只读）。"""

    CSS = _CSS
    BINDINGS = [Binding("escape", "dismiss_screen", "关闭", show=False)]

    def __init__(self, obs_dir: Path, session_id: str) -> None:
        super().__init__()
        self._obs_dir = obs_dir
        self._session_id = session_id

    def compose(self) -> ComposeResult:
        with Vertical(id="metrics-screen"):
            yield Static("", id="metrics-header")
            with ScrollableContainer(id="metrics-body"):
                yield Static("", id="metrics-body-static", markup=False)

    def on_mount(self) -> None:
        cur = session_metrics(self._obs_dir, self._session_id)
        history = aggregate_metrics(self._obs_dir)
        self.query_one("#metrics-header", Static).update(f"Metrics · {self._session_id}")
        self.query_one("#metrics-body-static", Static).update(
            render_metrics_text(self._session_id, cur, history)
        )

    def action_dismiss_screen(self) -> None:
        self.dismiss()
