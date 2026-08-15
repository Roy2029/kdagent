"""复测对比（11 §3.5 diff_runs + §3.8 metrics_by_run）测试。

纯数据层：两个 report.json → 题级状态转移归类；单版本报表重读。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kdagent.eval.cli import persist_report
from kdagent.eval.model import EvalReport, EvalTask, FailureCase, RunMetrics
from kdagent.eval.report_diff import (
    diff_runs,
    load_report,
    metrics_by_run,
    render_run_diff,
    report_path,
)


def _report(run_id: str, resolved: list[str], failed: list[str]) -> EvalReport:
    return EvalReport(
        run_id=run_id,
        tasks=[EvalTask(instance_id=i) for i in resolved + failed],
        resolved=resolved,
        failed=[FailureCase(instance_id=i, kind="not_located", reason="x") for i in failed],
        metrics=RunMetrics(total=len(resolved + failed), resolved=len(resolved)),
    )


def _workdir(tmp_path: Path) -> Path:
    return tmp_path / "wk"


# ---- load_report ----

def test_load_report_roundtrip(tmp_path: Path) -> None:
    work = _workdir(tmp_path)
    persist_report(work, "run-1", _report("run-1", ["a"], ["b"]))
    report = load_report(work, "run-1")
    assert report is not None
    assert report.resolved == ["a"]
    assert report.failed[0].instance_id == "b"
    assert report.failed[0].kind == "not_located"


def test_load_report_missing_returns_none(tmp_path: Path) -> None:
    assert load_report(_workdir(tmp_path), "run-1") is None


def test_load_report_corrupt_returns_none(tmp_path: Path) -> None:
    path = report_path(_workdir(tmp_path), "run-1")
    path.parent.mkdir(parents=True)
    path.write_text("{bad", encoding="utf-8")
    assert load_report(_workdir(tmp_path), "run-1") is None


# ---- diff_runs ----

def test_diff_runs_four_transitions(tmp_path: Path) -> None:
    work = _workdir(tmp_path)
    persist_report(work, "a", _report("a", ["p1", "p2", "t1"], ["t2", "t3", "t4"]))
    persist_report(work, "b", _report("b", ["p1", "t1", "t4"], ["t2", "t3", "p2", "t5"]))
    diff = diff_runs(work, "a", "b")
    assert diff.fail2pass == ["t4"]  # a 失败 → b 通过（修复）
    assert diff.pass2fail == ["p2"]  # a 通过 → b 失败（回归）
    assert diff.fail2fail == ["t2", "t3"]  # 都失败（现象还在）
    assert diff.pass2pass == ["p1", "t1"]  # 都通过（稳定）
    assert diff.only_a == []  # t5 只在 b → only_b
    assert diff.only_b == ["t5"]


def test_diff_runs_missing_report_raises(tmp_path: Path) -> None:
    work = _workdir(tmp_path)
    persist_report(work, "a", _report("a", ["p1"], []))
    with pytest.raises(FileNotFoundError):
        diff_runs(work, "a", "b")  # b 的报告缺失 → 不静默


def test_render_run_diff_lists_and_conclusion(tmp_path: Path) -> None:
    work = _workdir(tmp_path)
    persist_report(work, "a", _report("a", ["p1"], ["t4"]))
    persist_report(work, "b", _report("b", ["p1", "t4"], []))
    text = render_run_diff(diff_runs(work, "a", "b"))
    assert "修复 fail2pass（1）：t4" in text
    assert "回归 pass2fail（0）：无" in text
    assert "有题从失败变通过" in text


def test_render_run_diff_identical_runs(tmp_path: Path) -> None:
    work = _workdir(tmp_path)
    persist_report(work, "a", _report("a", ["p1"], ["t4"]))
    persist_report(work, "b", _report("b", ["p1"], ["t4"]))
    text = render_run_diff(diff_runs(work, "a", "b"))
    assert "题级状态完全一致" in text


def test_render_run_diff_regression_flag(tmp_path: Path) -> None:
    work = _workdir(tmp_path)
    persist_report(work, "a", _report("a", ["p1", "p2"], []))
    persist_report(work, "b", _report("b", ["p1"], ["p2"]))
    text = render_run_diff(diff_runs(work, "a", "b"))
    assert "有题从通过变失败——改动引入了回归" in text


# ---- metrics_by_run ----

def test_metrics_by_run_returns_report(tmp_path: Path) -> None:
    work = _workdir(tmp_path)
    persist_report(work, "run-1", _report("run-1", ["a"], ["b"]))
    report = metrics_by_run(work, "run-1")
    assert report.metrics.total == 2
    assert report.metrics.resolved == 1
    assert "1/2 通过" in report.summary()


def test_metrics_by_run_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        metrics_by_run(_workdir(tmp_path), "run-1")
