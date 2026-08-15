"""评测流水线 MVP（规格 11 §3.2/§3.7，M5-e）。

无 Docker/WSL 依赖的轻量判分双轨：
- task.test_cmd 给了 → 在封史副本（Agent 已改动的树）里跑真实测试 → 过 = resolved
- 否则回退 gold_patch 文本相似度 > 阈值 = resolved（真实 SWE-bench 需官方 Docker harness）

隔离跑批（§3.7）：每题一个封史副本（`git archive` 单提交，防作弊）→
SubAgentRunner.run_to_completion 在副本目录跑（work_dir=副本）→ 无 worktree 依赖，
副本目录本身就是隔离单元。
"""

from __future__ import annotations

import difflib
import io
import os
import shutil
import subprocess
import tarfile
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from kdagent.eval.model import (
    EvalReport,
    EvalTask,
    FailureCase,
    FailureKind,
)
from kdagent.obs.telemetry import Telemetry
from kdagent.subagent.model import AgentDef
from kdagent.subagent.runner import SubAgentRunner

_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}

# 补丁提取/判分要排除的运行时目录（不会成为改动的一部分）
_RUNTIME_DIRS = (".kdagent", ".claude")
# 判分时不参与「改对了」判定的测试文件（回归防护用）
_TEST_FILE_HINTS = ("test_", "_test", "tests/", "spec.", ".test.")

# gold 相似度回退判分的阈值（无 test_cmd 时）
_GOLD_SIM_THRESHOLD = 0.5


class EvalError(Exception):
    """评测流水线异常（封史失败 / 判分命令缺失）。"""


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=_GIT_ENV,
        input="",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if check and proc.returncode != 0:
        raise EvalError(f"git {args[0]} 失败：{proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def seal_copy(source_repo: Path, base_commit: str, dest: Path) -> Path:
    """封史副本（§3.2 第 2 步，防作弊最关键的防抄）：git archive 抽文件树 → 新目录
    git init 重提一个单提交——Agent 翻 git log 也找不到正确答案。"""
    dest.mkdir(parents=True, exist_ok=True)
    archive_bytes = subprocess.run(
        ["git", "-C", str(source_repo), "archive", "--format=tar", base_commit],
        env=_GIT_ENV,
        capture_output=True,
        timeout=120,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as tar:
        try:
            tar.extractall(dest, filter="data")  # Python 3.12+（数据过滤，防穿越）
        except TypeError:
            tar.extractall(dest)  # Python 3.11 无 filter 参数
    _git(dest, "init", "-q")
    _git(dest, "config", "user.email", "eval@kdagent.local")
    _git(dest, "config", "user.name", "kdagent-eval")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "sealed base")
    return dest


def extract_patch(work_dir: Path) -> str:
    """模型补丁：git add -A + git diff --cached，排除运行时目录（§3.2 第 4 步）。"""
    _git(work_dir, "add", "-A")
    diff = _git(work_dir, "diff", "--cached")
    # 过滤运行时目录的变更块（保留 test 文件——判分在 Agent 已改动的树里跑）
    keep: list[str] = []
    in_drop = False
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            in_drop = any(f"/{d}/" in line or line.endswith(f"/{d}") for d in _RUNTIME_DIRS)
        if not in_drop:
            keep.append(line)
    return "\n".join(keep)


def _touches_test_file(patch: str) -> bool:
    """补丁是否改了测试文件（回归防护：判分 harness 注入自己的测试，补丁碰测试会冲突）。"""
    for line in patch.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            path = line[4:].split("\t")[0]
            if any(hint in path for hint in _TEST_FILE_HINTS):
                return True
    return False


def _file_overlap(model_patch: str, gold_patch: str) -> bool:
    """模型补丁与官方补丁是否改了同一个文件（「定位对」的弱判定）。"""
    def files(patch: str) -> set[str]:
        return {
            line[4:].split("\t")[0]
            for line in patch.splitlines()
            if line.startswith("+++ ")
        }
    return bool(files(model_patch) & files(gold_patch))


def gold_similarity(model_patch: str, gold_patch: str) -> float:
    if not gold_patch:
        return 0.0
    if not model_patch:
        return 0.0
    return difflib.SequenceMatcher(None, model_patch, gold_patch).ratio()


def classify(
    task: EvalTask, model_patch: str, test_passed: bool | None, agent_error: str
) -> tuple[FailureKind, str]:
    """失败归类五类（11 §3.4，启发式）：纯函数，可单测。

    优先级：中途放弃/工具报错 → 约束冲突 → 碰坏测试 → 定位 vs 修法。
    """
    if agent_error or not model_patch.strip():
        return "harness_fault", agent_error or "模型补丁为空（中途退出/未产出改动）"
    if test_passed is True:
        return "harness_fault", "test 通过但 gold 校验失败"  # 理论不可达（判分先过）
    if _touches_test_file(model_patch):
        if task.constraint:
            return "constraint_conflict", f"补丁改了测试文件，违反约束「{task.constraint}」"
        return "regression", "补丁改了测试文件（PASS_TO_PASS 有损坏风险）"
    if _file_overlap(model_patch, task.gold_patch):
        return "wrong_fix", "定位到目标文件但修法不对（测试仍不过）"
    return "not_located", "未定位到该改的文件（补丁与官方补丁无交集）"


class EvalRunner:
    """跑批编排（§3.2 跑批阶段）：封史 → 隔离跑 → 判分 → 归类 → 报告。"""

    def __init__(
        self,
        runner: SubAgentRunner,
        *,
        definition: AgentDef,
        source_repo: Path,
        work_dir: Path,
        task_loader: Callable[[], list[EvalTask]],
        similarity_threshold: float = _GOLD_SIM_THRESHOLD,
        obs_dir: Path | None = None,
    ) -> None:
        self._runner = runner
        self._definition = definition
        self._source_repo = source_repo
        self._work_dir = work_dir
        self._task_loader = task_loader
        self._similarity_threshold = similarity_threshold
        # 07 trace 数据层：子 Agent 全程产 trace，带 eval.run_id/task_id 标记，
        # 供失败定位（11 §3.4/§3.5，trace_store.load_traces / failed_events）。
        self._telemetry = Telemetry(obs_dir) if obs_dir is not None else None

    async def run(self, run_id: str) -> EvalReport:
        tasks = self._task_loader()
        report = EvalReport(run_id=run_id, tasks=tasks)
        start = time.perf_counter()
        for task in tasks:
            await self._run_task(report, task)
        report.metrics.wall_s = time.perf_counter() - start
        return report

    async def _run_task(self, report: EvalReport, task: EvalTask) -> None:
        work = self._work_dir / task.instance_id
        if work.exists():
            shutil.rmtree(work)
        sealed = seal_copy(self._source_repo, task.base_commit, work)
        prompt = self._build_prompt(task)
        # 07 §3.8 eval 标记：contextvar 隔离（D60）——并发跑批各自 set 互不覆盖，
        # try/finally reset 防跨任务残留。
        token = (
            self._telemetry.set_trace_attributes(
                {"eval.run_id": report.run_id, "eval.task_id": task.instance_id}
            )
            if self._telemetry is not None
            else None
        )
        try:
            result = await self._runner.run_to_completion(
                self._definition, prompt, work_dir=sealed, telemetry=self._telemetry
            )
        finally:
            if token is not None and self._telemetry is not None:
                self._telemetry.reset_trace_attributes(token)
        model_patch = extract_patch(sealed)
        report.metrics.total += 1
        report.metrics.total_turns += result.turns
        report.metrics.total_tokens += (
            result.usage.input_tokens + result.usage.output_tokens
        )
        test_passed: bool | None = None
        if task.test_cmd:
            test_passed = self._run_test(sealed, task.test_cmd)
        if test_passed is True or (
            test_passed is None and gold_similarity(model_patch, task.gold_patch)
            >= self._similarity_threshold
        ):
            report.resolved.append(task.instance_id)
            report.metrics.resolved += 1
            return
        kind, reason = classify(task, model_patch, test_passed, result.error)
        report.failed.append(
            FailureCase(
                instance_id=task.instance_id, kind=kind, reason=reason, patch=model_patch
            )
        )
        report.metrics.passed_to_passed += 0  # PASS_TO_PASS 无损坏（MVP 无 p2p 实测）

    @staticmethod
    def _build_prompt(task: EvalTask) -> str:
        base = f"问题描述：\n{task.problem_statement}\n"
        if task.fail_to_pass:
            base += f"\n必须修复的测试：{task.fail_to_pass}\n"
        if task.pass_to_pass:
            base += f"不可破坏的测试：{task.pass_to_pass}\n"
        if task.constraint:
            base += f"约束：{task.constraint}\n"
        base += "\n修复问题，改动写入当前工作目录。完成后报告改了什么与如何验证。"
        return base

    def _run_test(self, work_dir: Path, test_cmd: str) -> bool:
        """在 Agent 已改动的副本树里跑判分测试（exit 0 = 通过）。"""
        try:
            proc = subprocess.run(
                test_cmd,
                cwd=work_dir,
                env=_GIT_ENV,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            return proc.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False


def task_list_to_dicts(report: EvalReport) -> list[dict[str, object]]:
    return [asdict(t) for t in report.tasks]
