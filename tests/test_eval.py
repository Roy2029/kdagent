"""评估体系 MVP（规格 11，M5-e）：封史 / 判分 / 归类 / 流水线。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from conftest import FakeLLM, done, tool_call

from kdagent.config import Config
from kdagent.eval import (
    EvalRunner,
    EvalTask,
    classify,
    extract_patch,
    gold_similarity,
    seal_copy,
)
from kdagent.eval.cli import load_tasks_file
from kdagent.subagent import BUILTIN_AGENTS_DIR
from kdagent.subagent.manager import AgentManager
from kdagent.subagent.runner import SubAgentRunner
from kdagent.tools import build_default_registry

_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """含两个 commit 的临时 git 仓库（base = 第一次 commit）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@test.local")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("# t\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    (repo / "bug.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add bug")
    return repo


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, env=_GIT_ENV, capture_output=True, text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout


def _base_commit(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD~1").strip()


# ---- 封史副本（防作弊） ----

def test_seal_copy_single_commit(repo: Path) -> None:
    dest = repo.parent / "sealed"
    seal_copy(repo, _base_commit(repo), dest)
    log = _git(dest, "log", "--oneline")
    assert len(log.splitlines()) == 1  # 单提交：翻 git log 找不到答案
    assert "base" in log
    assert not (dest / "bug.py").exists()  # base 版本没有 bug.py
    assert (dest / "README.md").exists()


def test_seal_copy_origin_isolation(repo: Path) -> None:
    dest = repo.parent / "sealed"
    seal_copy(repo, _base_commit(repo), dest)
    assert _git(dest, "remote", "-v").strip() == ""  # 无 remote，无上游痕迹


# ---- 补丁提取 ----

def test_extract_patch_excludes_runtime_dirs(repo: Path) -> None:
    dest = repo.parent / "sealed"
    seal_copy(repo, _base_commit(repo), dest)
    (dest / "fix.txt").write_text("ok\n", encoding="utf-8")
    (dest / ".kdagent").mkdir()
    (dest / ".kdagent" / "meta.json").write_text("{}", encoding="utf-8")
    _git(dest, "add", "-A")
    patch = extract_patch(dest)
    assert "fix.txt" in patch
    assert ".kdagent" not in patch  # 运行时目录不进补丁


# ---- gold 相似度 ----

def test_gold_similarity() -> None:
    assert gold_similarity("", "a") == 0.0
    assert gold_similarity("a", "") == 0.0
    assert gold_similarity("same patch", "same patch") == 1.0
    assert gold_similarity("hello world", "hello world!") > 0.8


# ---- 失败归类五类 ----

def _task(**kw) -> EvalTask:
    base = dict(
        instance_id="t1",
        problem_statement="修 bug",
        fail_to_pass=["test_f.py"],
        pass_to_pass=["test_p.py"],
        gold_patch="",
    )
    base.update(kw)
    return EvalTask(**base)


def test_classify_harness_fault_on_error() -> None:
    task = _task()
    kind, reason = classify(task, "", None, "provider 挂了")
    assert kind == "harness_fault"
    assert "provider 挂了" in reason


def test_classify_harness_fault_on_empty_patch() -> None:
    kind, reason = classify(_task(), "", None, "")
    assert kind == "harness_fault"
    assert "补丁为空" in reason


def test_classify_not_located() -> None:
    task = _task(gold_patch="+++ src/a.py\n+fixed\n")
    kind, _ = classify(task, "+++ src/other.py\n+x\n", None, "")
    assert kind == "not_located"


def test_classify_wrong_fix() -> None:
    task = _task(gold_patch="+++ src/a.py\n+right\n")
    kind, _ = classify(task, "+++ src/a.py\n+wrong\n", False, "")
    assert kind == "wrong_fix"


def test_classify_regression_touches_test() -> None:
    kind, _ = classify(_task(), "+++ tests/test_bug.py\n+- bad\n", False, "")
    assert kind == "regression"


def test_classify_constraint_conflict() -> None:
    task = _task(constraint="不要改测试文件")
    kind, _ = classify(task, "+++ tests/test_bug.py\n+x\n", False, "")
    assert kind == "constraint_conflict"


# ---- 流水线端到端 ----

def _runner(tmp_path: Path, llm: FakeLLM) -> SubAgentRunner:
    return SubAgentRunner(
        llm=llm,
        tools=build_default_registry(),
        config=Config(),
        work_dir=tmp_path,
    )


@pytest.mark.asyncio
async def test_eval_runner_resolves_via_test_cmd(repo: Path, tmp_path: Path) -> None:
    """Bash 在封史副本里建 flag 文件 → test_cmd 通过 → resolved。"""
    llm = FakeLLM([tool_call("Bash", {"command": "echo fixed > flag.txt"}), done("改完了")])
    runner = _runner(tmp_path, llm)
    manager = AgentManager([BUILTIN_AGENTS_DIR])
    manager.scan()
    definition = manager.get("general-purpose")
    assert definition is not None
    task = EvalTask(
        instance_id="t1",
        base_commit=_base_commit(repo),
        problem_statement="制造一个 flag.txt",
        test_cmd="test -f flag.txt",
    )
    ev = EvalRunner(
        runner, definition=definition, source_repo=repo,
        work_dir=tmp_path / "eval", task_loader=lambda: [task],
    )
    report = await ev.run("run-1")
    assert report.resolved == ["t1"]
    assert report.metrics.resolved == 1
    assert report.metrics.total == 1
    assert report.failed == []


@pytest.mark.asyncio
async def test_eval_runner_harness_fault_on_no_patch(repo: Path, tmp_path: Path) -> None:
    """Agent 只回文本不改动 → 空补丁 → harness_fault。"""
    llm = FakeLLM([done("我看了下，没问题")])
    runner = _runner(tmp_path, llm)
    manager = AgentManager([BUILTIN_AGENTS_DIR])
    manager.scan()
    definition = manager.get("general-purpose")
    assert definition is not None
    task = EvalTask(
        instance_id="t2",
        base_commit=_base_commit(repo),
        problem_statement="修 bug",
        gold_patch="+++ README.md\n+fixed\n",
    )
    ev = EvalRunner(
        runner, definition=definition, source_repo=repo,
        work_dir=tmp_path / "eval", task_loader=lambda: [task],
    )
    report = await ev.run("run-2")
    assert report.resolved == []
    assert len(report.failed) == 1
    assert report.failed[0].kind == "harness_fault"


@pytest.mark.asyncio
async def test_eval_runner_gold_similarity_resolve(repo: Path, tmp_path: Path) -> None:
    """无 test_cmd：gold 相似度 ≥ 阈值 → resolved。"""
    gold = "+++ README.md\n+# fixed the bug\n+- done\n"
    llm = FakeLLM([tool_call("Bash", {"command": "printf '\\n# fixed the bug\\n' >> README.md"}), done("ok")])
    runner = _runner(tmp_path, llm)
    manager = AgentManager([BUILTIN_AGENTS_DIR])
    manager.scan()
    definition = manager.get("general-purpose")
    assert definition is not None
    task = EvalTask(
        instance_id="t3",
        base_commit=_base_commit(repo),
        problem_statement="修 bug",
        gold_patch=gold,
    )
    ev = EvalRunner(
        runner, definition=definition, source_repo=repo,
        work_dir=tmp_path / "eval", task_loader=lambda: [task], similarity_threshold=0.1,
    )
    report = await ev.run("run-3")
    assert "t3" in report.resolved


def test_summary_format(repo: Path) -> None:
    task = EvalTask(instance_id="t1", base_commit=_base_commit(repo))
    from kdagent.eval.model import EvalReport, RunMetrics

    report = EvalReport(
        run_id="run-x",
        tasks=[task],
        resolved=["t1"],
        metrics=RunMetrics(total=1, resolved=1),
    )
    s = report.summary()
    assert "run-x" in s
    assert "1/1" in s
    assert "通过" in s


# ---- tasks.json 解析 ----

def test_load_tasks_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f").write_text("x", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "i")
    cfg = tmp_path / "tasks.json"
    cfg.write_text(
        json.dumps(
            {
                "run_id": "eval-cfg",
                "repo_dir": str(repo),
                "tasks": [
                    {
                        "instance_id": "a1",
                        "base_commit": "HEAD",
                        "problem_statement": "修",
                        "fail_to_pass": ["test_a.py"],
                        "gold_patch": "+++ x",
                        "test_cmd": "python -m pytest",
                        "constraint": "别改测试",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id, repo_dir, work_dir, tasks = load_tasks_file(cfg)
    assert run_id == "eval-cfg"
    assert repo_dir == repo.resolve()
    assert len(tasks) == 1
    t = tasks[0]
    assert t.instance_id == "a1"
    assert t.fail_to_pass == ["test_a.py"]
    assert t.test_cmd == "python -m pytest"
    assert t.constraint == "别改测试"
    assert work_dir.is_dir()


def test_load_tasks_file_empty(tmp_path: Path) -> None:
    cfg = tmp_path / "tasks.json"
    cfg.write_text('{"repo_dir": ".", "tasks": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="tasks 为空"):
        load_tasks_file(cfg)
