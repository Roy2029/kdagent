"""kdagent eval CLI 子命令（规格 11 §3.9：长任务后台执行，不走 TUI）。

用法：
- kdagent eval <tasks.json>                         跑一轮评测
- kdagent eval <tasks.json> --workers N             并发跑批（§3.7 可并行，D64；默认 1 顺序）
- kdagent eval <tasks.json> --report <run_id>      只读复核（11 §3.4：失败题 → span 树 → 事件流）
- kdagent eval <tasks.json> --annotate <run_id> <task_id> <kind> [--note ...]   人工批注修正归类

tasks.json 结构：
{
  "run_id": "eval-1",
  "repo_dir": "path/to/source/git/repo",   # 含 base_commit 的原始仓库（封史来源）
  "work_dir": "path/to/eval/workspace",    # 封史副本的存放目录（可选，默认 repo_dir/.kdagent/eval）
  "tasks": [ { "instance_id", "base_commit", "problem_statement",
               "fail_to_pass", "pass_to_pass", "gold_patch", "test_cmd", "constraint" } ]
}

复核数据落盘（{work_dir}/.kdagent/eval/<run_id>/）：
- report.json      跑批报告（失败归类），--report 读它
- annotations.json 人工批注（--annotate 写），复测携带不丢
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import cast

from kdagent.config import load_api_key, load_config
from kdagent.context.compactor import cost_params_from_table
from kdagent.engine.llm.base import ProviderConfig
from kdagent.engine.llm.openai import OpenAICompatClient
from kdagent.eval.model import EvalReport, EvalTask, FailureCase, FailureKind
from kdagent.eval.report_diff import diff_runs, load_report, metrics_by_run, render_run_diff
from kdagent.eval.review import (
    focus_labels,
    focus_spans,
    merged_kind,
    render_failure_index,
    render_span_tree,
    save_annotation,
    span_detail,
    spans_in_tree_order,
)
from kdagent.eval.runner import EvalRunner
from kdagent.eval.trace_store import failed_events, load_traces
from kdagent.subagent import BUILTIN_AGENTS_DIR, AgentManager, SubAgentRunner
from kdagent.tools import build_default_registry


def load_workspace(path: Path) -> tuple[Path, Path]:
    """只读命令：从 tasks.json 取 (repo_dir, work_dir)，不要求 tasks 非空。

    复核/对比/报表只看落盘报告与 obs，tasks 内容无关——与 load_tasks_file 分离。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取评测配置 {path}：{exc}") from exc
    repo_dir = Path(data.get("repo_dir", "")).resolve()
    if not repo_dir.is_dir():
        raise ValueError(f"repo_dir 不存在：{repo_dir}")
    work_dir = Path(data.get("work_dir", str(repo_dir / ".kdagent" / "eval"))).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    return repo_dir, work_dir


def load_tasks_file(path: Path) -> tuple[str, Path, Path, list[EvalTask]]:
    """解析 tasks.json → (run_id, repo_dir, work_dir, tasks)。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取评测配置 {path}：{exc}") from exc
    repo_dir = Path(data.get("repo_dir", "")).resolve()
    if not repo_dir.is_dir():
        raise ValueError(f"repo_dir 不存在：{repo_dir}")
    work_dir = Path(data.get("work_dir", str(repo_dir / ".kdagent" / "eval"))).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    tasks: list[EvalTask] = []
    for raw in data.get("tasks", []):
        tasks.append(
            EvalTask(
                instance_id=str(raw.get("instance_id", "")),
                repo=str(raw.get("repo", "")),
                base_commit=str(raw.get("base_commit", "")),
                problem_statement=str(raw.get("problem_statement", "")),
                fail_to_pass=[str(x) for x in raw.get("fail_to_pass", [])],
                pass_to_pass=[str(x) for x in raw.get("pass_to_pass", [])],
                gold_patch=str(raw.get("gold_patch", "")),
                test_cmd=str(raw.get("test_cmd", "")),
                p2p_cmd=str(raw.get("p2p_cmd", "")),
                constraint=str(raw.get("constraint", "")),
            )
        )
    if not tasks:
        raise ValueError("tasks 为空")
    return str(data.get("run_id", "eval-default")), repo_dir, work_dir, tasks


def run_eval_cli(tasks_file: Path, workers: int = 1) -> int:
    """跑一轮评测并打印报告（退出码：0 全过 / 1 有失败 / 2 配置错误）。

    `workers > 1` 走并发跑批（11 §3.7 可并行）：Semaphore 限并发，单任务异常
    隔离记 harness_fault，不中断整批。
    """
    try:
        run_id, repo_dir, work_dir, tasks = load_tasks_file(tasks_file)
    except ValueError as exc:
        print(f"评测配置错误：{exc}", file=sys.stderr)
        return 2

    config = load_config()
    api_key = load_api_key()
    if not api_key:
        print("未配置 DEEPSEEK_API_KEY：评测需要真实 LLM，请在项目根 .env 设置", file=sys.stderr)
        return 2
    llm = OpenAICompatClient(
        ProviderConfig(
            protocol="openai",
            model=config.model or "deepseek-chat",
            base_url="https://api.deepseek.com/v1",
            api_key=api_key,
        )
    )
    registry = build_default_registry()
    agent_manager = AgentManager([BUILTIN_AGENTS_DIR])
    agent_manager.scan()
    runner = SubAgentRunner(
        llm=llm,
        tools=registry,
        config=config,
        work_dir=repo_dir,
    )
    definition = agent_manager.get("general-purpose")
    if definition is None:
        print("内置 general-purpose Agent 缺失", file=sys.stderr)
        return 2
    obs_dir = work_dir / ".kdagent" / "obs"  # 07 trace 落盘（评估本地观测）
    eval_runner = EvalRunner(
        runner,
        definition=definition,
        source_repo=repo_dir,
        work_dir=work_dir,
        task_loader=lambda: tasks,
        obs_dir=obs_dir,
        # T5-1：计价表按 provider 配置化（D83，None = DEFAULT_COST；数值待实测标定）
        cost=cost_params_from_table(config.get_cost_table(), config.provider),
    )
    report: EvalReport = asyncio.run(eval_runner.run(run_id, max_workers=workers))
    persist_report(work_dir, run_id, report)
    print(report.summary())
    if report.failed:
        print("\n失败定位（07 trace）：")
        for case in report.failed:
            traces = load_traces(obs_dir, run_id=run_id, task_id=case.instance_id)
            if not traces:
                print(f"- {case.instance_id}：无 trace（子代理未接 telemetry 或落盘失败）")
                continue
            bad = failed_events(traces[0])
            detail = f"{len(bad)} 个失败事件" if bad else "trace 完整但无 error span"
            print(f"- {case.instance_id}：{detail}（{traces[0].session_id}/{traces[0].trace_id}）")
    return 0 if not report.failed else 1


def persist_report(work_dir: Path, run_id: str, report: EvalReport) -> Path:
    """落盘跑批报告（复核界面数据源：失败题 + 自动归类）。"""
    path = work_dir / ".kdagent" / "eval" / run_id / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _load_report(work_dir: Path, run_id: str) -> list[FailureCase]:
    """读跑批报告失败题（重建 FailureCase）。报告缺失 → 报错退出。"""
    report = load_report(work_dir, run_id)
    if report is None:
        hint = f"（先跑 `kdagent eval <tasks.json>` 生成，run_id={run_id}）"
        print(f"找不到该 run 的报告：\n{hint}", file=sys.stderr)
        sys.exit(2)
    if not report.failed:
        print("该 run 无失败题（全部通过，无需复核）")
        sys.exit(0)
    return report.failed


def _review_index(
    obs_dir: Path, work_dir: Path, run_id: str
) -> list[tuple[FailureCase, FailureKind, int]]:
    """复核列表：每失败题 → (case, 合并后归类, 失败事件数)。"""
    index: list[tuple[FailureCase, FailureKind, int]] = []
    for case in _load_report(work_dir, run_id):
        traces = load_traces(obs_dir, run_id=run_id, task_id=case.instance_id)
        bad = len(failed_events(traces[0])) if traces else 0
        kind, _ = merged_kind(obs_dir, run_id, case.instance_id, case.kind)
        index.append((case, kind, bad))
    return index


def run_review_cli(tasks_file: Path, run_id: str) -> int:
    """只读复核（11 §3.4 定位/阅读）：失败题索引 → 选一题 → span 树 + 过滤跳转 + 事件流。

    免 api_key（只读 traces + report.json）。交互：题号展开 span 树；`f<类型>` 过滤
    （e 报错 / c 压缩 / p 权限）；`d<行号>` 看详情；`b` 返回索引；`q` 退出。
    """
    try:
        _, work_dir = load_workspace(tasks_file)
    except ValueError as exc:
        print(f"评测配置错误：{exc}", file=sys.stderr)
        return 2
    obs_dir = work_dir / ".kdagent" / "obs"
    index = _review_index(obs_dir, work_dir, run_id)

    while True:
        print(f"\n失败归类复核 run={run_id}（人工批注优先）：")
        for i, (case, _kind, bad) in enumerate(index):
            _, annotation = merged_kind(obs_dir, run_id, case.instance_id, case.kind)
            print(f"  {i}: {render_failure_index(case, annotation, bad)}")
        choice = input("\n题号看 span 树 / f<类型> 过滤 / q 退出 > ").strip()
        if choice in ("q", "quit", ""):
            break
        if choice.startswith("f") and choice[1:2].isdigit():
            _review_focus(obs_dir, run_id, index[int(choice[1:])])
            continue
        if choice.isdigit() and 0 <= int(choice) < len(index):
            _review_trace(obs_dir, run_id, index[int(choice)])
            continue
        print(f"（{focus_hint()}）")

    return 0


def focus_hint() -> str:
    """过滤交互提示（读题即可用，不依赖 report）。"""
    return "，".join(f"f{i}={label}" for i, (_, label) in enumerate(focus_labels()))


def _review_focus(obs_dir: Path, run_id: str, case: tuple[FailureCase, FailureKind, int]) -> None:
    """定位过滤：展开该题 trace，按类型过滤跳转高亮（报错/压缩/权限）。"""
    c, _, _ = case
    traces = load_traces(obs_dir, run_id=run_id, task_id=c.instance_id)
    if not traces:
        print(f"{c.instance_id}：无 trace")
        return
    any_hit = False
    for code, label in focus_labels():
        hits = focus_spans(traces[0], code)
        if hits:
            any_hit = True
            print(f"\n[{label} {len(hits)} 个]：")
            print(render_span_tree(traces[0], mark_ids={s.span_id for s in hits}))
    if not any_hit:
        print(f"{c.instance_id}：该 trace 无报错/压缩/权限事件，全量树如下")
        print(render_span_tree(traces[0]))


def _review_trace(obs_dir: Path, run_id: str, case: tuple[FailureCase, FailureKind, int]) -> None:
    """阅读：展开某题 span 树；`d<行号>` 看单事件详情。"""
    c, _, _ = case
    traces = load_traces(obs_dir, run_id=run_id, task_id=c.instance_id)
    if not traces:
        print(f"{c.instance_id}：无 trace")
        return
    trace = traces[0]
    while True:
        ordered = spans_in_tree_order(trace)
        print(f"\n── {c.instance_id} span 树（{len(ordered)} 事件）──")
        print(render_span_tree(trace))
        choice = input("\nd<行号> 看详情 / f<类型> 过滤 / b 返回 / q 退出 > ").strip()
        if choice in ("b", "q", "quit"):
            break
        if choice.startswith("d") and choice[1:2].isdigit():
            idx = int(choice[1:])
            if 0 <= idx < len(ordered):
                print("\n" + span_detail(ordered[idx][1]))
            continue
        if choice.startswith("f"):
            codes = dict(enumerate(code for code, _ in focus_labels()))
            if choice[1:2].isdigit() and int(choice[1:]) in codes:
                hits = focus_spans(trace, codes[int(choice[1:])])
                if hits:
                    print(render_span_tree(trace, mark_ids={s.span_id for s in hits}))
                else:
                    print(f"无{codes[int(choice[1:])]}类事件")
            continue
        print("（b 返回 / q 退出 / d0 d1 … 看事件详情）")


def run_annotate_cli(
    tasks_file: Path, run_id: str, task_id: str, kind: str, note: str
) -> int:
    """批注：人工修正失败归类 + 备注 → annotations.json（复测携带，不丢人工修正）。"""
    valid = {"not_located", "wrong_fix", "regression", "harness_fault", "constraint_conflict"}
    if kind not in valid:
        print(f"非法归类：{kind}（可选 {sorted(valid)}）", file=sys.stderr)
        return 2
    try:
        _, work_dir = load_workspace(tasks_file)
    except ValueError as exc:
        print(f"评测配置错误：{exc}", file=sys.stderr)
        return 2
    obs_dir = work_dir / ".kdagent" / "obs"
    path = save_annotation(obs_dir, run_id, task_id, cast(FailureKind, kind), note)
    print(f"已批注 {task_id} → [{kind}]：{path}")
    return 0


def run_diff_cli(tasks_file: Path, run_a: str, run_b: str) -> int:
    """复测对比（11 §3.5 一次一变量复测）：两轮 run 的题级变化。免 api_key。"""
    try:
        _, work_dir = load_workspace(tasks_file)
    except ValueError as exc:
        print(f"评测配置错误：{exc}", file=sys.stderr)
        return 2
    try:
        diff = diff_runs(work_dir, run_a, run_b)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(render_run_diff(diff))
    return 0


def run_metrics_cli(tasks_file: Path, run_id: str) -> int:
    """单版本报表（11 §3.8 metrics_by_run）：重看一轮历史 run 的指标。免 api_key。"""
    try:
        _, work_dir = load_workspace(tasks_file)
    except ValueError as exc:
        print(f"评测配置错误：{exc}", file=sys.stderr)
        return 2
    try:
        report = metrics_by_run(work_dir, run_id)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(report.summary())
    return 0
