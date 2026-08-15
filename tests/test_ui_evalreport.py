"""11 §3.4 TUI 版评测报告屏测试（headless run_test）。

索引（失败题列表）→ 打开 span 树 → 事件详情 / 过滤 / 批注 / 退出。渲染与批注
逻辑在 eval/review.py（纯函数已单测），本测试验证 Screen 的命令解析 + widget 装配。

注：本 Textual 版本 `App.query_one(<ScreenClass>)` 查不到已 push 的屏幕，统一用
`app.screen`（当前活动屏）访问。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from conftest import FakeLLM

from kdagent.config import Config
from kdagent.engine.conversation import ConversationManager
from kdagent.eval.cli import persist_report
from kdagent.eval.model import EvalReport, EvalTask, FailureCase, RunMetrics
from kdagent.tools import build_default_registry
from kdagent.ui.app import KDApp
from kdagent.ui.evalreport import EvalReportScreen

_RUN = "run-1"


def _report() -> EvalReport:
    return EvalReport(
        run_id=_RUN,
        tasks=[EvalTask(instance_id=i) for i in ("p1", "t1")],
        resolved=["p1"],
        failed=[FailureCase(instance_id="t1", kind="not_located", reason="改错文件")],
        metrics=RunMetrics(total=2, resolved=1),
    )


def _write_trace(work: Path, task_id: str = "t1") -> None:
    """手写一条 eval trace（JsonlExporter 格式）：普通 tool.exec + 一条 error span。"""
    obs = work / ".kdagent" / "obs" / "traces"
    obs.mkdir(parents=True, exist_ok=True)
    path = obs / f"trace-{task_id}.jsonl"
    header = {
        "_type": "trace",
        "trace_id": f"tr-{task_id}",
        "session_id": "",
        "user_input_snapshot": "solve t1",
        "ts": 0,
        "attributes": {"eval.run_id": _RUN, "eval.task_id": task_id},
    }
    spans = [
        {
            "_type": "span",
            "span_id": "s-root",
            "trace_id": f"tr-{task_id}",
            "parent_span_id": None,
            "name": "run_to_completion",
            "kind": "task",
            "status": "ok",
            "start_ts": 0,
            "end_ts": 10,
            "duration_ms": 10,
            "attributes": {},
            "logs": [],
        },
        {
            "_type": "span",
            "span_id": "s-tool",
            "trace_id": f"tr-{task_id}",
            "parent_span_id": "s-root",
            "name": "tool.exec",
            "kind": "tool",
            "status": "ok",
            "start_ts": 1,
            "end_ts": 3,
            "duration_ms": 2,
            "attributes": {"name": "Grep", "input": '{"pattern": "foo"}'},
            "logs": [],
        },
        {
            "_type": "span",
            "span_id": "s-err",
            "trace_id": f"tr-{task_id}",
            "parent_span_id": "s-tool",
            "name": "tool.exec",
            "kind": "tool",
            "status": "error",
            "start_ts": 4,
            "end_ts": 6,
            "duration_ms": 2,
            "attributes": {"name": "WriteFile", "is_error": True},
            "logs": [],
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(header, ensure_ascii=False) + "\n")
        for span in spans:
            f.write(json.dumps(span, ensure_ascii=False) + "\n")


def _make_app(tmp_path: Path) -> tuple[KDApp, Path]:
    work = tmp_path / "wk"
    persist_report(work, _RUN, _report())
    _write_trace(work)
    llm = FakeLLM([])
    app = KDApp(
        config=Config(),
        llm=llm,
        conversation=ConversationManager(),
        tools=build_default_registry(),
        work_dir=work,
        sessions_dir=work / ".kdagent" / "sessions",
    )
    return app, work


def _screen(app: KDApp) -> EvalReportScreen:
    return cast(EvalReportScreen, app.screen)


def _body(screen: EvalReportScreen) -> str:
    """当前 body Static 的文本内容（Static.content 公开属性）。"""
    return str(screen.query_one("#eval-body-static").content)


async def test_eval_report_index_and_trace(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    async with app.run_test() as pilot:
        app.open_eval_report(_RUN)
        await pilot.pause()
        screen = _screen(app)
        assert "t1" in _body(screen)
        assert "改错文件" in _body(screen)
        assert "人工批注优先" in _body(screen)
        # 打开 trace：span 树 + 过滤命中数
        screen._handle_index("0")
        await pilot.pause()
        assert screen._mode == "trace"
        assert "tool.exec" in _body(screen)
        # 过滤 error → 高亮 ←
        screen._handle_trace("f0")
        await pilot.pause()
        assert "←" in _body(screen)
        # 详情
        screen._handle_trace("d1")
        await pilot.pause()
        assert screen._mode == "detail"
        assert "事件 1 详情" in _body(screen)
        # 返回索引
        screen._handle_detail("b")
        await pilot.pause()
        assert screen._mode == "trace"
        screen._handle_trace("b")
        await pilot.pause()
        assert screen._mode == "index"


async def test_eval_report_annotate_writes_annotations(tmp_path: Path) -> None:
    app, work = _make_app(tmp_path)
    async with app.run_test() as pilot:
        app.open_eval_report(_RUN)
        await pilot.pause()
        screen = _screen(app)
        screen._handle_index("0")
        await pilot.pause()
        screen._handle_trace("a wrong_fix 修错了位置")
        await pilot.pause()
        annotations = work / ".kdagent" / "eval" / _RUN / "annotations.json"
        assert annotations.is_file()
        data = json.loads(annotations.read_text(encoding="utf-8"))
        assert data["t1"]["kind"] == "wrong_fix"
        assert data["t1"]["note"] == "修错了位置"
        # 非法归类拒绝，不覆盖已有批注
        screen._handle_trace("a bad_kind")
        await pilot.pause()
        data = json.loads(annotations.read_text(encoding="utf-8"))
        assert data["t1"]["kind"] == "wrong_fix"


async def test_eval_report_missing_report_disables_input(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    async with app.run_test() as pilot:
        app.open_eval_report("no-such-run")
        await pilot.pause()
        screen = _screen(app)
        assert "找不到 run no-such-run 的报告" in _body(screen)
        assert screen.query_one("#eval-input").disabled


async def test_eval_command_dispatch_opens_screen(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    async with app.run_test() as pilot:
        app.dispatch_command("eval", f"report {_RUN}")
        await pilot.pause()
        assert isinstance(app.screen, EvalReportScreen)
        # 缺参 → 用法提示，不崩、不叠屏
        app.dispatch_command("eval", "")
        await pilot.pause()
        assert isinstance(app.screen, EvalReportScreen)


async def test_eval_report_quit_dismisses(tmp_path: Path) -> None:
    app, _ = _make_app(tmp_path)
    async with app.run_test() as pilot:
        app.open_eval_report(_RUN)
        await pilot.pause()
        screen = _screen(app)
        screen._handle_index("q")
        await pilot.pause()
        assert not isinstance(app.screen, EvalReportScreen)
