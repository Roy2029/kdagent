"""评测报告对比：一次一变量复测工具（规格 11 §3.5/§3.8，M5-e 遗留补全）。

`diff_runs(a, b)` 对比两轮跑批的 resolved/failed → fail2pass（修复）/ pass2fail（回归）
/ fail2fail / pass2pass——11 §3.5 迭代闭环：「只换一处，重跑，看那个具体现象还在不在」。
数据源 = 跑批落盘的 report.json（D61 起 run_eval_cli 持久化），纯函数可单测。

也承载单版本报表 `metrics_by_run`（§3.8：通过率/token/耗时；成本需计价表，MVP 不含）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from kdagent.eval.model import EvalReport, FailureCase, RunMetrics


@dataclass(slots=True)
class RunDiff:
    """两轮跑批的题级变化（一次一变量复测的「具体现象还在不在」）。"""

    run_a: str
    run_b: str
    fail2pass: list[str] = field(default_factory=list)  # a 失败 → b 修复
    pass2fail: list[str] = field(default_factory=list)  # a 通过 → b 回归
    fail2fail: list[str] = field(default_factory=list)  # 都失败（现象还在）
    pass2pass: list[str] = field(default_factory=list)  # 都通过
    only_a: list[str] = field(default_factory=list)  # 只出现在 a（不可比）
    only_b: list[str] = field(default_factory=list)  # 只出现在 b（不可比）


def report_path(work_dir: Path, run_id: str) -> Path:
    """跑批报告落盘路径（与 eval/cli.persist_report 同位置）。"""
    return work_dir / ".kdagent" / "eval" / run_id / "report.json"


def load_report(work_dir: Path, run_id: str) -> EvalReport | None:
    """读跑批报告（不存在/损坏 → None，调用方给提示）。"""
    try:
        data = json.loads(report_path(work_dir, run_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    failed = [
        FailureCase(
            instance_id=str(c["instance_id"]),
            kind=str(c["kind"]),  # type: ignore[arg-type]
            reason=str(c["reason"]),
            patch=str(c.get("patch", "")),
        )
        for c in data.get("failed", [])
    ]
    metrics_raw = data.get("metrics") or {}
    metrics = RunMetrics(
        total=int(metrics_raw.get("total", 0)),
        resolved=int(metrics_raw.get("resolved", 0)),
        passed_to_passed=int(metrics_raw.get("passed_to_passed", 0)),
        total_turns=int(metrics_raw.get("total_turns", 0)),
        total_tokens=int(metrics_raw.get("total_tokens", 0)),
        wall_s=float(metrics_raw.get("wall_s", 0.0)),
    )
    return EvalReport(
        run_id=str(data.get("run_id", run_id)),
        resolved=[str(i) for i in data.get("resolved", [])],
        failed=failed,
        metrics=metrics,
    )


def diff_runs(work_dir: Path, run_a: str, run_b: str) -> RunDiff:
    """两轮跑批题级对比（§3.5 一次一变量复测的对比工具）。

    以两报告各自主集合的并集划分：只在一侧的题进 only_*（不可比），双侧都有按
    状态转移归类。缺失任一报告 → 抛 FileNotFoundError（不让「少跑一轮」静默通过）。
    """
    report_a = load_report(work_dir, run_a)
    report_b = load_report(work_dir, run_b)
    if report_a is None:
        raise FileNotFoundError(f"找不到 run {run_a} 的报告：{report_path(work_dir, run_a)}")
    if report_b is None:
        raise FileNotFoundError(f"找不到 run {run_b} 的报告：{report_path(work_dir, run_b)}")

    def sets(report: EvalReport) -> tuple[set[str], set[str]]:
        return set(report.resolved), {c.instance_id for c in report.failed}

    resolved_a, failed_a = sets(report_a)
    resolved_b, failed_b = sets(report_b)
    all_ids = resolved_a | failed_a | resolved_b | failed_b
    diff = RunDiff(run_a=run_a, run_b=run_b)
    for task_id in sorted(all_ids):
        a_ok, b_ok = task_id in resolved_a, task_id in resolved_b
        if task_id not in (resolved_a | failed_a):
            diff.only_b.append(task_id)  # a 无此题
        elif task_id not in (resolved_b | failed_b):
            diff.only_a.append(task_id)  # b 无此题
        elif a_ok and b_ok:
            diff.pass2pass.append(task_id)
        elif not a_ok and not b_ok:
            diff.fail2fail.append(task_id)
        elif not a_ok and b_ok:
            diff.fail2pass.append(task_id)
        else:
            diff.pass2fail.append(task_id)
    return diff


def render_run_diff(diff: RunDiff) -> str:
    """文本渲染（CLI 输出）：逐类列题 + 现象解读。"""
    lines = [
        f"复测对比 {diff.run_a} → {diff.run_b}：",
        f"  修复 fail2pass（{len(diff.fail2pass)}）：{'、'.join(diff.fail2pass) or '无'}",
        f"  回归 pass2fail（{len(diff.pass2fail)}）：{'、'.join(diff.pass2fail) or '无'}",
        f"  现象还在 fail2fail（{len(diff.fail2fail)}）：{'、'.join(diff.fail2fail) or '无'}",
        f"  稳定 pass2pass（{len(diff.pass2pass)}）：{'、'.join(diff.pass2pass) or '无'}",
    ]
    if diff.only_a:
        lines.append(f"  仅 {diff.run_a} 有（{len(diff.only_a)}）：{'、'.join(diff.only_a)}")
    if diff.only_b:
        lines.append(f"  仅 {diff.run_b} 有（{len(diff.only_b)}）：{'、'.join(diff.only_b)}")
    if not diff.fail2pass and not diff.pass2fail:
        lines.append("结论：两轮题级状态完全一致（fail2fail 是稳定失败，不是变化）。")
    elif diff.fail2pass:
        lines.append("结论：有题从失败变通过——改动可能修对了什么（再核对该题 trace 现象）。")
    elif diff.pass2fail:
        lines.append("结论：有题从通过变失败——改动引入了回归（优先排查）。")
    return "\n".join(lines)


def metrics_by_run(work_dir: Path, run_id: str) -> EvalReport:
    """读单版本报告（§3.8 metrics_by_run；成本需计价表，MVP 不含）。"""
    report = load_report(work_dir, run_id)
    if report is None:
        raise FileNotFoundError(f"找不到 run {run_id} 的报告：{report_path(work_dir, run_id)}")
    return report
