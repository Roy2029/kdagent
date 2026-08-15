"""判分后回填 trace 判定（07 §3.8 验收 276 / 11 §5 225，D72）。

`backfill_verdict` 改写命中 trace 的 header attributes（eval.passed/kind/reason），
原子写防半行、幂等可覆盖；runner 在判分后接线（resolved/failed 两路）。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from conftest import FakeLLM, done, tool_call

from kdagent.config import Config
from kdagent.eval import EvalRunner, EvalTask
from kdagent.eval.trace_store import backfill_verdict, load_traces
from kdagent.obs.telemetry import Telemetry
from kdagent.subagent import BUILTIN_AGENTS_DIR
from kdagent.subagent.manager import AgentManager
from kdagent.subagent.model import AgentDef
from kdagent.subagent.runner import SubAgentRunner
from kdagent.tools import build_default_registry

_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}


def _emit_trace(obs_dir: Path, run_id: str, task_id: str) -> None:
    """Telemetry 落盘一条带 eval 标记 + 1 个 span 的 trace（exporter 真实格式）。"""
    telemetry = Telemetry(obs_dir)
    token = telemetry.set_trace_attributes({"eval.run_id": run_id, "eval.task_id": task_id})
    try:
        telemetry.begin_trace("sess-1", "题目")
        with telemetry.span("tool.exec", "tool", {"tool": "ReadFile"}):
            pass
        telemetry.end_trace()
    finally:
        telemetry.reset_trace_attributes(token)


# ---- backfill_verdict 纯函数 ------------------------------------------------


def test_backfill_patches_matching_trace(tmp_path: Path) -> None:
    _emit_trace(tmp_path / "obs", "run-1", "t1")
    n = backfill_verdict(tmp_path / "obs", "run-1", "t1", False, "wrong_fix", "改错文件")
    assert n == 1
    traces = load_traces(tmp_path / "obs", run_id="run-1", task_id="t1")
    assert len(traces) == 1
    attrs = traces[0].attributes
    assert attrs["eval.passed"] is False
    assert attrs["eval.kind"] == "wrong_fix"
    assert attrs["eval.reason"] == "改错文件"


def test_backfill_preserves_span_lines(tmp_path: Path) -> None:
    """改写只动 header 行，span 行原样保留（原子整文件重写）。"""
    _emit_trace(tmp_path / "obs", "run-1", "t1")
    backfill_verdict(tmp_path / "obs", "run-1", "t1", True)
    path = next((tmp_path / "obs" / "traces").glob("**/*.jsonl"))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # header + 1 span
    header = json.loads(lines[0])
    assert header["attributes"]["eval.passed"] is True
    span = json.loads(lines[1])
    assert span["_type"] == "span" and span["name"] == "tool.exec"


def test_backfill_no_match_returns_zero(tmp_path: Path) -> None:
    _emit_trace(tmp_path / "obs", "run-1", "t1")
    assert backfill_verdict(tmp_path / "obs", "run-1", "nope", True) == 0
    assert backfill_verdict(tmp_path / "obs", "other", "t1", True) == 0
    # 未命中不改文件（header 无 eval.passed）
    traces = load_traces(tmp_path / "obs")
    assert "eval.passed" not in traces[0].attributes


def test_backfill_missing_obs_returns_zero(tmp_path: Path) -> None:
    assert backfill_verdict(tmp_path / "obs", "run-1", "t1", True) == 0


def test_backfill_idempotent_overwrites(tmp_path: Path) -> None:
    _emit_trace(tmp_path / "obs", "run-1", "t1")
    backfill_verdict(tmp_path / "obs", "run-1", "t1", False, "not_located", "旧")
    backfill_verdict(tmp_path / "obs", "run-1", "t1", False, "wrong_fix", "新")  # 人工复核覆盖
    traces = load_traces(tmp_path / "obs", run_id="run-1", task_id="t1")
    attrs = traces[0].attributes
    assert attrs["eval.passed"] is False
    assert attrs["eval.kind"] == "wrong_fix"
    assert attrs["eval.reason"] == "新"


def test_backfill_direct_layout_empty_session(tmp_path: Path) -> None:
    """空 session_id（子代理默认）→ jsonl 直接在 traces/ 下，同样命中。"""
    obs = tmp_path / "obs"
    base = obs / "traces"
    base.mkdir(parents=True)
    path = base / "tr-x.jsonl"
    header = {
        "_type": "trace", "trace_id": "tr-x", "session_id": "",
        "user_input_snapshot": "题目", "ts": 0,
        "attributes": {"eval.run_id": "run-1", "eval.task_id": "t1"},
    }
    span = {
        "_type": "span", "span_id": "s", "trace_id": "tr-x", "parent_span_id": None,
        "name": "llm.call", "kind": "client", "status": "ok",
        "start_ts": 0, "end_ts": 1, "duration_ms": 1, "attributes": {}, "logs": [],
    }
    path.write_text(
        json.dumps(header, ensure_ascii=False) + "\n"
        + json.dumps(span, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    assert backfill_verdict(obs, "run-1", "t1", False, "harness_fault") == 1
    traces = load_traces(obs, run_id="run-1", task_id="t1")
    assert len(traces) == 1
    assert traces[0].attributes["eval.passed"] is False


# ---- runner 判分后接线 -------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, env=_GIT_ENV, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "t@test.local"], cwd=repo, env=_GIT_ENV, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, env=_GIT_ENV, capture_output=True, text=True)
    (repo / "README.md").write_text("# t\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, env=_GIT_ENV, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, env=_GIT_ENV, capture_output=True, text=True)
    (repo / "bug.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, env=_GIT_ENV, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "add bug"], cwd=repo, env=_GIT_ENV, capture_output=True, text=True)
    return repo


def _base_commit(repo: Path) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD~1"], cwd=repo, env=_GIT_ENV,
        capture_output=True, text=True, encoding="utf-8",
    )
    return out.stdout.strip()


def _definition() -> AgentDef:
    manager = AgentManager([BUILTIN_AGENTS_DIR])
    manager.scan()
    definition = manager.get("general-purpose")
    assert definition is not None
    return definition


@pytest.mark.asyncio
async def test_runner_backfills_passed_on_resolve(repo: Path, tmp_path: Path) -> None:
    obs_dir = tmp_path / "obs"
    llm = FakeLLM([tool_call("Bash", {"command": "echo fixed > flag.txt"}), done("改完了")])
    runner = SubAgentRunner(llm=llm, tools=build_default_registry(), config=Config(), work_dir=tmp_path)
    task = EvalTask(
        instance_id="t1", base_commit=_base_commit(repo),
        problem_statement="制造一个 flag.txt", test_cmd="test -f flag.txt",
    )
    ev = EvalRunner(
        runner, definition=_definition(), source_repo=repo,
        work_dir=tmp_path / "eval", task_loader=lambda: [task], obs_dir=obs_dir,
    )
    report = await ev.run("run-1")
    assert report.resolved == ["t1"]
    traces = load_traces(obs_dir, run_id="run-1", task_id="t1")
    assert len(traces) == 1
    assert traces[0].attributes["eval.passed"] is True
    assert "eval.kind" not in traces[0].attributes


@pytest.mark.asyncio
async def test_runner_backfills_kind_on_failure(repo: Path, tmp_path: Path) -> None:
    obs_dir = tmp_path / "obs"
    llm = FakeLLM([done("我看了下，没问题")])
    runner = SubAgentRunner(llm=llm, tools=build_default_registry(), config=Config(), work_dir=tmp_path)
    task = EvalTask(
        instance_id="t2", base_commit=_base_commit(repo),
        problem_statement="修 bug", gold_patch="+++ README.md\n+fixed\n",
    )
    ev = EvalRunner(
        runner, definition=_definition(), source_repo=repo,
        work_dir=tmp_path / "eval", task_loader=lambda: [task], obs_dir=obs_dir,
    )
    report = await ev.run("run-2")
    assert report.failed[0].kind == "harness_fault"
    traces = load_traces(obs_dir, run_id="run-2", task_id="t2")
    assert len(traces) == 1
    attrs = traces[0].attributes
    assert attrs["eval.passed"] is False
    assert attrs["eval.kind"] == "harness_fault"


@pytest.mark.asyncio
async def test_runner_without_obs_skips_backfill(repo: Path, tmp_path: Path) -> None:
    """obs_dir=None → 判分正常、无 trace 落盘（原行为不变）。"""
    llm = FakeLLM([tool_call("Bash", {"command": "echo fixed > flag.txt"}), done("改完了")])
    runner = SubAgentRunner(llm=llm, tools=build_default_registry(), config=Config(), work_dir=tmp_path)
    task = EvalTask(
        instance_id="t3", base_commit=_base_commit(repo),
        problem_statement="制造一个 flag.txt", test_cmd="test -f flag.txt",
    )
    ev = EvalRunner(
        runner, definition=_definition(), source_repo=repo,
        work_dir=tmp_path / "eval", task_loader=lambda: [task],
    )
    report = await ev.run("run-3")
    assert report.resolved == ["t3"]
    assert load_traces(tmp_path / "obs") == []
