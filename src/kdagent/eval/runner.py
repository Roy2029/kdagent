"""评测流水线 MVP（规格 11 §3.2/§3.7，M5-e）。

无 Docker/WSL 依赖的轻量判分双轨：
- task.test_cmd 给了 → 在封史副本（Agent 已改动的树）里跑真实测试 → 过 = resolved
- 否则回退 gold_patch 文本相似度 > 阈值 = resolved（真实 SWE-bench 需官方 Docker harness）

隔离跑批（§3.7）：每题一个封史副本（`git archive` 单提交，防作弊）→
SubAgentRunner.run_to_completion 在副本目录跑（work_dir=副本）→ 无 worktree 依赖，
副本目录本身就是隔离单元。
"""

from __future__ import annotations

import asyncio
import difflib
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from kdagent.context.compactor import CostParams, estimate_token_cost
from kdagent.eval.docker_judge import DockerJudgeConfig, DockerJudgeError, judge
from kdagent.eval.model import (
    EvalReport,
    EvalTask,
    FailureCase,
    FailureKind,
)
from kdagent.eval.trace_store import backfill_verdict
from kdagent.obs.telemetry import Telemetry
from kdagent.subagent.model import AgentDef
from kdagent.subagent.runner import SubAgentRunner

_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}

# 补丁提取/判分要排除的运行时目录（不会成为改动的一部分）
_RUNTIME_DIRS = (".kdagent", ".claude", ".venv")  # .venv：D96 治理③ preinstall 建在副本内的 venv，不进补丁
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


def _force_rmtree(path: Path) -> None:
    """Windows 兼容强制删除：git init 后 `.git/objects` 设只读位，`shutil.rmtree`
    默认抛 PermissionError——重跑批时旧封史副本删不掉。onerror 里 chmod 去只读再重试。"""
    if not path.exists():
        return
    shutil.rmtree(path, onerror=_force_rmtree_onerror)


def _force_rmtree_onerror(func: Callable[[str], object], p: str, _exc: object) -> None:
    os.chmod(p, stat.S_IWRITE)  # 去只读位
    func(p)


def patch_applies(source_repo: Path, base_commit: str, patch: str) -> str | None:
    """D96 治理②：patch 前置预检——在 base_commit 干净树里 `git apply --check`。

    返回 None = 可应用；否则返回 git 报错（临时文件 hunk、上下文不匹配、CRLF 等
    patch 质量问题）。跑批产 patch 后、进 Docker 判分前逐题预检，坏 patch 本地拦截，
    不再白跑一轮容器（B3 3437 / B4 2322 的 Patch Apply Failed 即此路拦截对象）。
    只检查不落地；复用 seal_copy 的 git archive 建临时干净树，与容器内 base_commit
    状态一致。
    """
    if not patch.strip():
        return "空补丁"
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td)
        archive = subprocess.run(
            ["git", "-C", str(source_repo), "archive", "--format=tar", base_commit],
            env=_GIT_ENV,
            capture_output=True,
            timeout=120,
        )
        if archive.returncode != 0:
            return f"git archive 失败：{archive.stderr.strip() or archive.stdout.strip()}"
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
            try:
                tar.extractall(dest, filter="data")  # Python 3.12+（数据过滤）
            except TypeError:
                tar.extractall(dest)  # Python 3.11 无 filter 参数
        # D95 同款行尾归一化：容器内 git checkout 默认 LF，patch 必须 LF 才能 apply
        proc = subprocess.run(
            ["git", "apply", "--check", "-"],
            cwd=dest,
            input=patch.replace("\r\n", "\n"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if proc.returncode == 0:
            return None
        return (proc.stderr or proc.stdout).strip()[:300]


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
    # D96 治理②：splitlines() 去掉每行（含最后一行）结尾换行，join 后 patch 末行无 `\n`
    # → 容器 git apply 报 corrupt patch at line N（预检实测，flag.txt patch line 7 corrupt）。
    # git diff 输出总是以 `\n` 结尾，重建时补回，patch 才可被 git apply 接受。
    if not keep:
        return ""
    return "\n".join(keep) + "\n"


def install_repo_deps(sealed: Path, python: str | None = None) -> tuple[bool, str]:
    """D96 治理③：封史副本内建 venv + `pip install -e .`（模拟容器「环境构建」）。

    解决封史副本无依赖、模型 import 不到工作区源码的根因（B2 urllib3 全灭、
    B3 pip download 病）。副副本的 venv 是隔离的（不污染全局，并发题互不干扰）；
    失败不阻断跑批——模型仍可依 prompt 引导在副本内自装。
    返回 (成功, 消息)。
    """
    base_python = python or sys.executable
    venv = sealed / ".venv"
    try:
        proc = subprocess.run(
            [base_python, "-m", "venv", str(venv)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        if proc.returncode != 0:
            return False, f"venv 创建失败：{proc.stderr.strip()[:200]}"
        pip = venv / "Scripts" / "pip.exe" if os.name == "nt" else venv / "bin" / "pip"
        proc = subprocess.run(
            [str(pip), "install", "-e", str(sealed)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        if proc.returncode != 0:
            return False, f"pip install -e 失败：{proc.stderr.strip()[:200]}"
        return True, "依赖已预装到副本 .venv"
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"预装异常：{type(exc).__name__}: {exc}"


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


def gold_check(base_repo: Path, base_commit: str, gold_patch: str) -> bool:
    """gold 补丁能否干净应用到 base commit（11 §3.2 步骤 3 gold 校验，D82）。

    完整 gold 校验 = 应用补丁后跑 F2P 测试判 resolved（依赖 Docker harness，待 224）；
    本前置只验**补丁可应用性**——gold 补丁都打不上的题（文件缺失/历史不符）是
    环境失效，跑了也没有正确答案可对照，应剔除不进入计分（§3.2：防环境问题
    误算成 Agent 能力不足）。无 gold_patch 视为有效（无法校验）。
    """
    if not gold_patch:
        return True
    # 简化文本（无 hunk）是相似度判分兜底用的 gold 表示，不是可 apply 的补丁——
    # git apply 校验不了 → 视为有效放行，防误伤（真补丁才有环境校验意义）。
    if "@@ -" not in gold_patch:
        return True
    try:
        archive_bytes = subprocess.run(
            ["git", "-C", str(base_repo), "archive", "--format=tar", base_commit],
            env=_GIT_ENV,
            capture_output=True,
            timeout=120,
        ).stdout
        with tempfile.TemporaryDirectory(prefix="goldcheck-") as td:
            dest = Path(td)
            with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as tar:
                try:
                    tar.extractall(dest, filter="data")  # Python 3.12+（数据过滤）
                except TypeError:
                    tar.extractall(dest)  # Python 3.11 无 filter
            proc = subprocess.run(
                ["git", "apply", "--check", "-"],
                cwd=dest,
                input=gold_patch,
                env=_GIT_ENV,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            return proc.returncode == 0
    except (subprocess.SubprocessError, OSError, tarfile.TarError):
        return False  # 归档/应用异常 = 环境失效


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


def sort_report_by_task_order(report: EvalReport, tasks: list[EvalTask]) -> None:
    """报告按题序稳定排序（D65）：并发下 resolved/failed 是完成序追加，跨轮顺序
    会跳动——复核索引/复测对比按落盘顺序展示，题序归位保证可读性。原地排序。"""
    order = {t.instance_id: i for i, t in enumerate(tasks)}
    report.resolved.sort(key=lambda iid: order.get(iid, len(order)))
    report.failed.sort(key=lambda c: order.get(c.instance_id, len(order)))


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
        cost: CostParams | None = None,
        docker: DockerJudgeConfig | None = None,
        preinstall: bool = False,
    ) -> None:
        self._runner = runner
        self._definition = definition
        self._source_repo = source_repo
        self._work_dir = work_dir
        self._task_loader = task_loader
        self._similarity_threshold = similarity_threshold
        self._cost = cost  # 01 T5-1 计价（D83：config cost 段按 provider 注入，None 用默认）
        # 11 §5 224：官方 Docker harness 判分。给了则跑批只产 patch，判分统一走
        # harness（run() 末尾）；None 保持本地 test_cmd/gold 双轨降级。
        self._docker = docker
        # 07 trace 数据层：子 Agent 全程产 trace，带 eval.run_id/task_id 标记，
        # 供失败定位（11 §3.4/§3.5，trace_store.load_traces / failed_events）。
        self._telemetry = Telemetry(obs_dir) if obs_dir is not None else None
        # 07 §3.8 验收 276：判分后回填 trace 判定（passed/kind/reason）。
        self._obs_dir = obs_dir
        # D96 治理③：封史副本内建 venv + pip install -e .（模拟容器环境构建）。
        # 默认关（C 扩展/重依赖 repo 预装可能失败，失败不阻断跑批）；对轻依赖 repo 开。
        self._preinstall = preinstall

    async def run(self, run_id: str, max_workers: int = 1) -> EvalReport:
        """跑批（§3.2 第 2 步）：`max_workers=1` 顺序，>1 并发（§3.7 可并行）。

        D64 起支持并发：asyncio.Semaphore 限并发 + gather 并行跑 `_safe_task`。
        单任务异常由 `_safe_task` 隔离（记 harness_fault，不中断整批）。
        D65 起报告按题序稳定排序（并发下为完成序追加，复核/复测展示跨轮一致）。
        """
        if max_workers < 1:
            raise ValueError("max_workers 必须 >= 1")
        tasks = self._task_loader()
        report = EvalReport(run_id=run_id, tasks=tasks)
        # 11 §3.2 步骤 3 gold 校验（D82）：gold 补丁无法应用到 base commit 的题是
        # 环境失效（文件缺失/历史不符）——跑了也没有正确答案可对照，剔除不进入计分，
        # 不浪费 Agent 算力。无 gold_patch 视为有效（无法校验，保持原行为）。
        valid_tasks: list[EvalTask] = []
        for task in tasks:
            if self._gold_valid(task):
                task.env_valid = True
                valid_tasks.append(task)
            else:
                report.invalid.append(task.instance_id)
        start = time.perf_counter()
        if max_workers == 1:
            for task in valid_tasks:
                await self._safe_task(report, task)
        else:
            sem = asyncio.Semaphore(max_workers)

            async def _guarded(task: EvalTask) -> None:
                async with sem:
                    await self._safe_task(report, task)

            await asyncio.gather(*(_guarded(task) for task in valid_tasks))
        report.metrics.wall_s = time.perf_counter() - start
        # 11 §5 224：Docker 判分模式——跑批只产 patch，判分统一走官方 harness。
        # 放最后（Agent 跑完）批量判分，harness 一次起容器逐题跑 F2P/P2P。
        if self._docker is not None:
            self._docker_judge_report(report)
        sort_report_by_task_order(report, tasks)
        return report

    def _gold_valid(self, task: EvalTask) -> bool:
        """gold 校验（11 §3.2 步骤 3，D82）：gold 补丁能否干净应用到 base commit。

        无法应用 = 环境失效（历史不符/文件缺失），剔除不进入计分；单题校验异常只
        剔除该题，不阻断整个跑批。无 gold_patch 视为有效（gold_check 内部放行）。
        """
        try:
            return gold_check(self._source_repo, task.base_commit, task.gold_patch)
        except Exception:  # noqa: BLE001 —— 单题校验失败只该剔除该题
            return False

    async def _safe_task(self, report: EvalReport, task: EvalTask) -> None:
        """单任务执行 + 异常隔离：封史/跑批异常记 harness_fault，不拖垮整批（§3.7）。"""
        report.metrics.total += 1  # 每个任务计一次（成功/失败都计）
        try:
            await self._run_task(report, task)
        except Exception as exc:  # noqa: BLE001 —— 单任务异常只该记一笔
            # D95：记完整 traceback——只记 type/exc 丢堆栈，偶发并发 bug（KeyError: 'status'）
            # 复现两次都抓不到栈；带栈后下次复现直接在 report 定位根因。
            report.failed.append(
                FailureCase(
                    instance_id=task.instance_id,
                    kind="harness_fault",
                    reason=(
                        f"跑批异常：{type(exc).__name__}: {exc}\n"
                        f"{traceback.format_exc()}"
                    ),
                )
            )
            # D94：防重复归类——异常题从 report.tasks 摘除，不再进 Docker 判分
            # （否则 model_patch 空又被 harness 判 empty_patch，同题记两次，B3 实测）。
            report.tasks = [t for t in report.tasks if t.instance_id != task.instance_id]

    async def _run_task(self, report: EvalReport, task: EvalTask) -> None:
        work = self._work_dir / task.instance_id
        if work.exists():
            _force_rmtree(work)  # D90：git objects 只读位，Windows 需强制删
        sealed = seal_copy(self._source_repo, task.base_commit, work)
        # D96 治理③：预装副本依赖（venv + pip install -e .）——失败不阻断（模型可自装）。
        if self._preinstall:
            install_repo_deps(sealed)
        prompt = self._build_prompt(task, sealed)  # D96：传封史副本真实路径 → prompt 环境说明
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
        # 判分/复核共享数据：本地判分用、Docker 判分（predictions.jsonl）与 trace 复核用。
        task.model_patch = model_patch
        usage = result.usage
        report.metrics.total_turns += result.turns
        report.metrics.total_tokens += usage.input_tokens + usage.output_tokens
        # 计价明细（D67）：输入/输出/缓存命中分开累积，成本按 CostParams 计价
        report.metrics.input_tokens += usage.input_tokens
        report.metrics.output_tokens += usage.output_tokens
        report.metrics.cache_tokens += usage.cache_read_tokens
        report.metrics.cost_cny += estimate_token_cost(
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            cost=self._cost,  # T5-1：配置计价表按 provider 注入（None = DEFAULT_COST）
        )
        if self._docker is not None:
            # 11 §5 224：Docker 判分模式——本地双轨判分跳过（test_cmd 环境与官方
            # 容器不一致，判分口径以 harness 为准），统一在 run() 末尾批量判分。
            return
        test_passed: bool | None = None
        if task.test_cmd:
            test_passed = self._run_test(sealed, task.test_cmd)
        # 11 §3.2 单题判定（D81 补 P2P 保护）：FAIL_TO_PASS 全过 **且** PASS_TO_PASS
        # 无损坏才算 resolved。p2p_cmd 给了才跑 P2P 实测；未给保持原行为（只判 F2P）。
        p2p_passed: bool | None = None
        if task.p2p_cmd:
            p2p_passed = self._run_test(sealed, task.p2p_cmd)
        if test_passed is True:
            if p2p_passed is None or p2p_passed is True:
                report.resolved.append(task.instance_id)
                report.metrics.resolved += 1
                if p2p_passed is True:
                    report.metrics.passed_to_passed += 1  # P2P 实测确认无损坏
                self._backfill(report, task, True)  # 07 §3.8：判分通过回填 passed
                return
            # F2P 全过但 P2P 被碰坏 → regression（修好目标测试不能破坏原通过测试）
            reason = f"PASS_TO_PASS 测试被破坏（{task.p2p_cmd}）"
            report.failed.append(
                FailureCase(
                    instance_id=task.instance_id,
                    kind="regression",
                    reason=reason,
                    patch=model_patch,
                )
            )
            self._backfill(report, task, False, "regression", reason)
            return
        if test_passed is None and (
            gold_similarity(model_patch, task.gold_patch) >= self._similarity_threshold
        ):
            # gold 相似度路径：无真实测试，P2P 无从实测 → resolved 但不计 passed_to_passed
            report.resolved.append(task.instance_id)
            report.metrics.resolved += 1
            self._backfill(report, task, True)  # 07 §3.8：判分通过回填 passed
            return
        kind, reason = classify(task, model_patch, test_passed, result.error)
        report.failed.append(
            FailureCase(
                instance_id=task.instance_id, kind=kind, reason=reason, patch=model_patch
            )
        )
        self._backfill(report, task, False, kind, reason)  # 07 §3.8：失败归类回填

    def _backfill(
        self,
        report: EvalReport,
        task: EvalTask,
        passed: bool,
        kind: str | None = None,
        reason: str | None = None,
    ) -> None:
        """打分后回填 trace 判定（07 §3.8 验收 276）；obs 未启用时 no-op。

        回填失败不阻断判分（backfill_verdict 内部已防御 OSError/脏行跳过）——
        判定结果已由 report.json 承载，trace 回填是让 trace 自包含的加分项。
        """
        if self._obs_dir is None:
            return
        backfill_verdict(self._obs_dir, report.run_id, task.instance_id, passed, kind, reason)

    def _docker_judge_report(self, report: EvalReport) -> None:
        """11 §5 224：Docker 判分批量执行 + 报告回填。

        调官方 harness 判分全部已跑完的题 → 逐题更新 report：
        - resolved → 移入 report.resolved + 回填 passed=True；
        - 失败 → classify 归类（error/empty_patch 直接 harness_fault）→ failed + 回填。
        harness 整体失败（镜像缺失/Docker 未起/超时）→ 全部归 harness_fault，
        report 不为空（metrics.total 已在 _safe_task 计过，resolved=0 可读）。
        """
        assert self._docker is not None
        # D96 治理②：patch 前置预检——质量差的 patch（临时文件 hunk/上下文不匹配/CRLF）
        # 本地 git apply --check 拦截，不进 Docker 判分白跑一轮容器（B3 3437/B4 2322 实录）。
        kept: list[EvalTask] = []
        for task in report.tasks:
            apply_err = patch_applies(self._source_repo, task.base_commit, task.model_patch)
            if apply_err is not None:
                reason = f"patch 前置预检不通过（{apply_err}）"
                report.failed.append(
                    FailureCase(
                        instance_id=task.instance_id,
                        kind="harness_fault",
                        reason=reason,
                        patch=task.model_patch,
                    )
                )
                self._backfill(report, task, False, "harness_fault", reason)
            else:
                kept.append(task)
        report.tasks = kept
        if not report.tasks:
            return  # 全部被预检拦截，无题进容器
        try:
            outcomes = judge(
                self._docker,
                report.tasks,
                eval_work_dir=self._work_dir,
                eval_run_id=report.run_id,
            )
        except DockerJudgeError as exc:
            for task in report.tasks:
                kind: FailureKind = "harness_fault"
                reason = f"Docker 判分失败：{exc}"
                report.failed.append(
                    FailureCase(
                        instance_id=task.instance_id,
                        kind=kind,
                        reason=reason,
                        patch=task.model_patch,
                    )
                )
                self._backfill(report, task, False, kind, reason)
            return
        for task, outcome in zip(report.tasks, outcomes):
            if outcome.resolved:
                report.resolved.append(task.instance_id)
                report.metrics.resolved += 1
                self._backfill(report, task, True)
                continue
            if outcome.error or outcome.empty_patch or not task.model_patch.strip():
                # 环境/空补丁属 harness 侧问题，不是能力问题——直接 harness_fault
                kind: FailureKind = "harness_fault"
                reason = outcome.reason or "模型补丁为空"
            else:
                kind, reason = classify(task, task.model_patch, test_passed=False, agent_error="")
            report.failed.append(
                FailureCase(
                    instance_id=task.instance_id, kind=kind, reason=reason, patch=task.model_patch
                )
            )
            self._backfill(report, task, False, kind, reason)

    @staticmethod
    def _build_prompt(task: EvalTask, work_dir: Path | None = None) -> str:
        """任务 prompt：问题描述 + 环境说明（D96 治理①）。

        环境说明段针对实测踩坑（台账经验 10）：3437 首轮 `cd /testbed`（SWE-bench 容器
        惯例路径）导致 Bash/Grep 全失败；B3 3429/3679 弃工作区、pip download 外部源码、
        读 site-packages 验证导致空 patch。明示 Windows 绝对路径 + 工作区可 pip install
        自装依赖验证，把模型引导回工作区内。
        """
        env_note = ""
        if work_dir is not None:
            env_note = (
                f"工作目录（Windows 绝对路径）：{work_dir}\n"
                "环境说明：本环境是 Windows，工作目录是绝对路径；不存在 /testbed 等 Linux "
                "容器路径，不要使用 / 开头的路径。若工作目录内已预装依赖（存在 .venv），"
                "验证请用 `.venv/Scripts/python.exe`（或激活 .venv）；否则可在工作目录内执行 "
                "`pip install -e .`（必要时先 `pip install -r requirements.txt`）自装依赖。"
                "不要 pip download 到 /tmp、也不要读工作目录外的库源码来验证——所有改动与验证 "
                "必须基于工作目录内的源码。\n"
            )
        base = f"{env_note}问题描述：\n{task.problem_statement}\n"
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
