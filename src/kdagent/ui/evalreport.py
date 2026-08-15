"""11 §3.4 TUI 版评测报告屏：失败题索引 → span 树 → 事件详情 + 批注（Textual Screen）。

D61 复核界面 CLI 已跑通定位/阅读/批注闭环；本屏是其在 Textual 内的内嵌版本
（05 挂载点复用，`/eval report <run_id>` local 命令触发）。渲染与批注逻辑全部
复用 `kdagent.eval.review`（纯函数），本模块只做命令解析 + widget 装配——命令
语法与 CLI 一致（题号 / `d<行号>` / `f<类型>` / `b` / `q` / `a <kind> [备注]`）。
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical
from textual.screen import Screen
from textual.widgets import Input, Static

from kdagent.eval.model import EvalReport, FailureCase, FailureKind
from kdagent.eval.report_diff import load_report, report_path
from kdagent.eval.review import (
    Annotation,
    focus_labels,
    focus_spans,
    merged_kind,
    render_failure_index,
    render_span_tree,
    save_annotation,
    span_detail,
    spans_in_tree_order,
)
from kdagent.eval.trace_store import failed_events, load_traces
from kdagent.obs.model import Span, Trace

_CMD_HINT = "题号看详情 / d<行号> 事件详情 / f<类型> 过滤 / b 返回 / q 退出"
_ANNOT_VALID = ("not_located", "wrong_fix", "regression", "harness_fault", "constraint_conflict")

_CSS = """
EvalReportScreen {
    layout: vertical;
    padding: 1 2;
}
#eval-screen { height: 100%; layout: vertical; }
#eval-header { height: auto; padding-bottom: 1; text-style: bold; }
#eval-body { height: 1fr; border: round $primary; padding: 0 1; }
#eval-input { margin-top: 1; }
"""


class EvalReportScreen(Screen[None]):
    """评测报告浏览（11 §3.4 TUI 版）：`/eval report <run_id>` 打开。

    命令语法与 CLI `--report` 一致；批注经 `a <kind> [备注]`（当前题）写回
    annotations.json，复测携带。Esc 关闭返回主界面。
    """

    CSS = _CSS
    BINDINGS = [Binding("escape", "dismiss_screen", "关闭", show=False)]

    def __init__(self, run_id: str, work_dir: Path) -> None:
        super().__init__()
        self._run_id = run_id
        self._work_dir = work_dir
        self._obs_dir = work_dir / ".kdagent" / "obs"
        self._report: EvalReport | None = None  # 报告缺失时置 None 并禁输入
        self._index: list[tuple[FailureCase, FailureKind, int, Annotation]] = []
        self._mode = "index"  # index / trace / detail
        self._current = 0  # 当前失败题（_index 下标）
        self._trace: Trace | None = None  # 当前题 trace（无则 None）
        self._ordered: list[tuple[int, Span]] = []  # spans_in_tree_order 结果
        self._mark: set[str] = set()  # 过滤命中的 span_id
        self._flash_msg = ""

    # ---- 装配 -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="eval-screen"):
            yield Static("", id="eval-header")
            with ScrollableContainer(id="eval-body"):
                yield Static("", id="eval-body-static", markup=False)
            yield Input(placeholder=_CMD_HINT, id="eval-input")

    def on_mount(self) -> None:
        report = load_report(self._work_dir, self._run_id)
        if report is None:
            path = report_path(self._work_dir, self._run_id)
            self._flash_msg = f"找不到 run {self._run_id} 的报告：{path}"
            self._redraw()
            self.query_one("#eval-input").disabled = True
            return
        self._report = report
        self._index = self._build_index()
        self._redraw()
        self.query_one("#eval-input").focus()

    # ---- 数据 -------------------------------------------------------------

    def _build_index(self) -> list[tuple[FailureCase, FailureKind, int, Annotation]]:
        """失败题索引：归类合并（人工优先）+ 失败事件数（复用 CLI _review_index）。"""
        assert self._report is not None  # on_mount 先赋值再建索引
        index: list[tuple[FailureCase, FailureKind, int, Annotation]] = []
        for case in self._report.failed:
            traces = load_traces(self._obs_dir, run_id=self._run_id, task_id=case.instance_id)
            bad = len(failed_events(traces[0])) if traces else 0
            kind, annotation = merged_kind(
                self._obs_dir, self._run_id, case.instance_id, case.kind
            )
            index.append((case, kind, bad, annotation))
        return index

    # ---- 渲染（命名 `_redraw`：Textual Widget 已有 `_render` 钩子）--------

    def _redraw(self) -> None:
        header = self.query_one("#eval-header", Static)
        body = self.query_one("#eval-body-static", Static)
        report = self._report
        if report is None:
            header.update(f"评测报告 run={self._run_id}")
            body.update(self._flash_msg)
            return
        rate = f"{report.metrics.resolved}/{report.metrics.total}"
        title = f"评测报告 run={self._run_id}：{rate} 通过"
        if self._flash_msg:
            title += f"　{self._flash_msg}"
            self._flash_msg = ""
        header.update(title)
        if self._mode == "index":
            body.update(self._render_index())
        elif self._mode == "trace":
            body.update(self._render_trace())
        else:
            body.update(self._render_detail())

    def _render_index(self) -> str:
        report = self._report
        if report is None or not report.failed:
            return "该 run 无失败题（全部通过）。"
        lines = ["失败归类（人工批注优先）："]
        for i, (case, _kind, bad, annotation) in enumerate(self._index):
            lines.append(f"  {i}: {render_failure_index(case, annotation, bad)}")
        hint = "、".join(f"f{i}={label}" for i, (_, label) in enumerate(focus_labels()))
        lines.append(f"（{hint}）")
        return "\n".join(lines)

    def _render_trace(self) -> str:
        case, _kind, _bad, _ann = self._index[self._current]
        trace = self._trace
        if trace is None:
            return f"{case.instance_id}：无 trace"
        self._ordered = spans_in_tree_order(trace)
        return render_span_tree(trace, mark_ids=self._mark)

    def _render_detail(self) -> str:
        case, _kind, _bad, _ann = self._index[self._current]
        trace = self._trace
        if trace is None or not self._ordered:
            return f"{case.instance_id}：无 trace"
        tree = render_span_tree(trace, mark_ids=self._mark)
        line, span = self._ordered[self._detail_line]
        return f"{tree}\n\n── 事件 {line} 详情 ──\n{span_detail(span)}"

    # ---- 命令解析（与 CLI 同语法） ----------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if self._mode == "index":
            self._handle_index(text)
        elif self._mode == "trace":
            self._handle_trace(text)
        else:
            self._handle_detail(text)

    def _handle_index(self, text: str) -> None:
        if text in ("q", "quit"):
            self.dismiss()
            return
        if text.startswith("f") and text[1:2].isdigit():
            idx = int(text[1:])
            if 0 <= idx < len(self._index):
                self._open_trace(idx, focus=int(text[1:]))
            return
        if text.isdigit():
            self._open_trace(int(text))
            return
        self._flash("题号看详情 / f<类型> 过滤（见列表编号）/ q 退出")

    def _handle_trace(self, text: str) -> None:
        if text in ("q", "quit"):
            self.dismiss()
            return
        if text in ("b", "back"):
            self._mode = "index"
            self._mark = set()
            self._redraw()
            return
        if text.startswith("d") and text[1:2].isdigit():
            idx = int(text[1:])
            if 0 <= idx < len(self._ordered):
                self._detail_line = idx
                self._mode = "detail"
                self._redraw()
            return
        if text.startswith("f"):
            self._apply_filter(text)
            return
        if text.startswith("a"):
            self._annotate(text)
            return
        self._flash("d<行号> 详情 / f<类型> 过滤 / b 返回 / q 退出 / a <kind> [备注] 批注")

    def _handle_detail(self, text: str) -> None:
        if text in ("q", "quit"):
            self.dismiss()
            return
        if text in ("b", "back"):
            self._mode = "trace"
            self._redraw()
            return
        self._flash("b 返回 span 树 / q 退出")

    def _open_trace(self, i: int, focus: int | None = None) -> None:
        """打开失败题 span 树；focus 传入时按类型过滤高亮。"""
        if not 0 <= i < len(self._index):
            return
        self._current = i
        case = self._index[i][0]
        traces = load_traces(self._obs_dir, run_id=self._run_id, task_id=case.instance_id)
        self._trace = traces[0] if traces else None
        self._mode = "trace"
        self._mark = set()
        self._ordered = []
        if focus is not None:
            self._apply_filter(f"f{focus}")
        self._redraw()

    def _apply_filter(self, text: str) -> None:
        """f<类型> 过滤高亮；f 单独 → 清过滤。"""
        trace = self._trace
        codes = {i: code for i, (code, _) in enumerate(focus_labels())}
        if text == "f":
            self._mark = set()
            self._redraw()
            return
        if text[1:2].isdigit() and int(text[1:]) in codes:
            hits = focus_spans(trace, codes[int(text[1:])]) if trace else []
            self._mark = {s.span_id for s in hits}
            self._redraw()
            return
        self._flash("f<类型> 过滤（f0=报错 f1=压缩 f2=权限）/ f 清过滤")

    def _annotate(self, text: str) -> None:
        """a <kind> [备注]：批注当前题（写回 annotations.json）。"""
        if text.startswith("a"):
            text = text[1:].strip()  # 剥掉命令前缀，剩下 <kind> [备注]
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            self._flash(f"a <kind> [备注]：归类需为 {'/'.join(_ANNOT_VALID)}")
            return
        kind = parts[0]
        if kind not in _ANNOT_VALID:
            self._flash(f"非法归类：{kind}（可选 {'/'.join(_ANNOT_VALID)}）")
            return
        note = parts[1] if len(parts) > 1 else ""
        case = self._index[self._current][0]
        save_annotation(
            self._obs_dir, self._run_id, case.instance_id, cast(FailureKind, kind), note
        )
        self._index = self._build_index()  # 人工改判标记 / 归类刷新
        self._flash(f"已批注 {case.instance_id} → [{kind}]")

    def _flash(self, msg: str) -> None:
        self._flash_msg = msg
        self._redraw()

    def action_dismiss_screen(self) -> None:
        self.dismiss()
