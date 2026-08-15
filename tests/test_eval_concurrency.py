"""并发跑批（11 §3.7 可并行，D64）：Semaphore 限并发 + gather 并行 + 单任务异常隔离。

用鸭子类型假 Agent 测编排层（避免共享 FakeLLM 响应队列竞争导致 flaky）：
真实 SubAgentRunner 顺序集成已由 test_eval.py 覆盖，这里只测 D64 新增的并发语义。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from kdagent.eval import EvalRunner, EvalTask
from kdagent.subagent.model import AgentDef

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


# ---- 假 Agent 载荷（鸭子类型 SubAgentRunner） ----

@dataclass(frozen=True)
class _Usage:
    input_tokens: int = 10
    output_tokens: int = 5


@dataclass(frozen=True)
class _Result:
    text: str = "done"
    usage: _Usage = _Usage()
    is_error: bool = False
    turns: int = 2
    error: str = ""


class _ResolveRunner:
    """在 work_dir 写 flag.txt 产补丁 → test_cmd 判 resolved。"""

    async def run_to_completion(
        self, definition: object, task: str, *, work_dir: Path | None = None, **kw: object
    ) -> _Result:
        ((work_dir or Path(".")) / "flag.txt").write_text("fixed\n", encoding="utf-8")
        return _Result()


class _CrashRunner(_ResolveRunner):
    """问题标记 CRASH 的任务抛 RuntimeError——测单任务异常隔离。"""

    async def run_to_completion(
        self, definition: object, task: str, *, work_dir: Path | None = None, **kw: object
    ) -> _Result:
        if "CRASH" in task:
            raise RuntimeError("模拟跑批崩溃")
        ((work_dir or Path(".")) / "flag.txt").write_text("fixed\n", encoding="utf-8")
        return _Result()


class _BarrierRunner:
    """所有任务都进入 run_to_completion 后才放行——确定性证明并发（timing 无关）。"""

    def __init__(self, expected: int, max_active: list[int]) -> None:
        self._expected = expected
        self._entered = 0
        self._gate = asyncio.Event()
        self._max_active = max_active

    async def run_to_completion(
        self, definition: object, task: str, *, work_dir: Path | None = None, **kw: object
    ) -> _Result:
        self._entered += 1
        self._max_active[0] = max(self._max_active[0], self._entered)
        if self._entered == self._expected:
            self._gate.set()  # 最后一个进入 → 放行所有人（防死锁由 asyncio 保证）
        await self._gate.wait()
        ((work_dir or Path(".")) / "flag.txt").write_text("fixed\n", encoding="utf-8")
        return _Result()


def _make_ev(repo: Path, tmp_path: Path, runner: object, tasks: list[EvalTask]) -> EvalRunner:
    return EvalRunner(
        runner,  # type: ignore[arg-type] —— 鸭子类型，mypy 只查 src/
        definition=AgentDef(name="test", description="test"),
        source_repo=repo,
        work_dir=tmp_path / "eval",
        task_loader=lambda: tasks,
    )


def _tasks(repo: Path, count: int, marker: str = "") -> list[EvalTask]:
    base = _base_commit(repo)
    return [
        EvalTask(
            instance_id=f"t{i}",
            base_commit=base,
            problem_statement=f"{marker}任务{i}",
            test_cmd="test -f flag.txt",
        )
        for i in range(count)
    ]


# ---- 并发编排 ----

@pytest.mark.asyncio
async def test_concurrent_two_tasks_resolve(repo: Path, tmp_path: Path) -> None:
    """max_workers=2：两任务都 resolved，metrics 计数正确。"""
    ev = _make_ev(repo, tmp_path, _ResolveRunner(), _tasks(repo, 2))
    report = await ev.run("run-conc", max_workers=2)
    assert sorted(report.resolved) == ["t0", "t1"]
    assert report.metrics.total == 2
    assert report.metrics.resolved == 2
    assert report.failed == []


@pytest.mark.asyncio
async def test_concurrent_error_isolation(repo: Path, tmp_path: Path) -> None:
    """一任务跑批抛异常 → 记 harness_fault，另一任务仍 resolved，整批不中断。"""
    tasks = [
        EvalTask(
            instance_id="ok",
            base_commit=_base_commit(repo),
            problem_statement="正常任务",
            test_cmd="test -f flag.txt",
        ),
        EvalTask(
            instance_id="bad",
            base_commit=_base_commit(repo),
            problem_statement="CRASH 任务",
            test_cmd="test -f flag.txt",
        ),
    ]
    ev = _make_ev(repo, tmp_path, _CrashRunner(), tasks)
    report = await ev.run("run-iso", max_workers=2)
    assert report.resolved == ["ok"]
    assert len(report.failed) == 1
    assert report.failed[0].instance_id == "bad"
    assert report.failed[0].kind == "harness_fault"
    assert "RuntimeError" in report.failed[0].reason
    assert report.metrics.total == 2  # 失败任务也计入 total


@pytest.mark.asyncio
async def test_concurrent_overlaps(repo: Path, tmp_path: Path) -> None:
    """barrier 证明两个任务同时在 run_to_completion 里（并行而非串行拼接）。"""
    max_active: list[int] = [0]
    ev = _make_ev(repo, tmp_path, _BarrierRunner(2, max_active), _tasks(repo, 2))
    report = await ev.run("run-ov", max_workers=2)
    assert max_active[0] == 2  # 二者同时在飞
    assert sorted(report.resolved) == ["t0", "t1"]


# ---- 顺序路径回归 ----

@pytest.mark.asyncio
async def test_sequential_default_two_tasks(repo: Path, tmp_path: Path) -> None:
    """默认 max_workers=1 顺序跑仍全过（total 计数上移后不回归）。"""
    ev = _make_ev(repo, tmp_path, _ResolveRunner(), _tasks(repo, 2))
    report = await ev.run("run-seq")
    assert sorted(report.resolved) == ["t0", "t1"]
    assert report.metrics.total == 2
    assert report.failed == []


@pytest.mark.asyncio
async def test_sequential_error_isolation(repo: Path, tmp_path: Path) -> None:
    """顺序路径异常同样隔离（此前异常会直接向上抛，D64 改为 harness_fault）。"""
    ev = _make_ev(repo, tmp_path, _CrashRunner(), _tasks(repo, 1, marker="CRASH"))
    report = await ev.run("run-seq-iso")
    assert report.metrics.total == 1
    assert len(report.failed) == 1
    assert report.failed[0].kind == "harness_fault"


@pytest.mark.asyncio
async def test_max_workers_validation(repo: Path, tmp_path: Path) -> None:
    ev = _make_ev(repo, tmp_path, _ResolveRunner(), [])
    with pytest.raises(ValueError, match="max_workers"):
        await ev.run("run-bad", max_workers=0)
