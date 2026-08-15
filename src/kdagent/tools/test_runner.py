"""TestRunner：测试驱动自闭环的执行器（规格 12 §3.1，M5 遗留补齐）。

「命令 + 隔离沙箱 + 结果解析」→ 输出结构化 TestingEvent 而非裸文本：
- passed：命令 exit 0（且回归命令未给或也通过）
- failed：命令 exit 非 0 → 归因信息（失败测试名 / 输出尾 / 退出码）供 LLM 定向修复
- regression_detected：主测试过但回归命令挂（Pass2Pass 被碰坏，§3.1 双重校验）

隔离（§3.1）：`worktree:<name>` 复用 10 worktree 沙箱（变更保护，跑挂不污染主
工作区）；`temp` 用临时目录副本（排除 .git/.venv/node_modules 等大目录）；
`current` 当前目录直跑（主 Agent 改完直接自测最常用）。

工具不感知 WorktreeManager 本体（避免 tools→subagent 包循环）：构造时注入
`resolve_worktree` 回调（cli 传 `worktree_manager.path`），纯工具测试可传 None。
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from kdagent.engine.events import TestingEvent
from kdagent.tools.base import ToolContext, ToolResult

TestStatus = Literal["passed", "failed", "regression_detected"]

# 终端控制序列（与 Bash 同口径）：命令输出的颜色/光标码剥离，工具结果应为纯文本。
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[@-_]")
# pytest short summary: "FAILED tests/test_math.py::test_add - AssertionError: ..."
_FAILED_SUMMARY_RE = re.compile(r"FAILED\s+(\S+?::\S+)")
# pytest inline: "tests/test_math.py::test_add FAILED   [ 50%]"
_FAILED_INLINE_RE = re.compile(r"^(\S+?::\S+)\s+(?:FAILED|ERROR)", re.M)
# pytest 旧格式头部: "____ test_add ____"
_HEADER_RE = re.compile(r"_{4,}\s*([\w\.]+)\s*_{4,}")

_TEMP_EXCLUDE = (".git", ".kdagent", ".venv", "node_modules", "__pycache__", ".pytest_cache")
_WORKTREE_PREFIX = "worktree:"
_DEFAULT_TIMEOUT = 180
_OUTPUT_TAIL_LINES = 30


class _SandboxError(Exception):
    """沙箱解析失败（worktree 不存在 / 未知 sandbox）。"""


@dataclass(slots=True)
class _ProcResult:
    returncode: int
    stdout: str
    stderr: str


def parse_failed_tests(output: str) -> list[str]:
    """从测试输出提取失败测试名（归因起点，12 §3.1）。pytest 三格式 + 去重。"""
    names: list[str] = []
    for m in _FAILED_SUMMARY_RE.finditer(output):
        names.append(m.group(1))
    if names:
        return list(dict.fromkeys(names))  # 保序去重
    for m in _FAILED_INLINE_RE.finditer(output):
        names.append(m.group(1))
    if names:
        return list(dict.fromkeys(names))
    for m in _HEADER_RE.finditer(output):
        names.append(m.group(1))
    return list(dict.fromkeys(names))


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text).rstrip()


def _tail(text: str, n: int = _OUTPUT_TAIL_LINES) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:]) if lines else ""


def _format_result(
    status: TestStatus, command: str, failed_tests: list[str], output: str
) -> str:
    """结构化结果文本（回传 LLM 归因；UI 可渲染 TestingEvent）。"""
    lines = [f"[TestRunner] status={status}", f"command: {command}"]
    if failed_tests:
        lines.append("failed_tests:")
        lines.extend(f"- {name}" for name in failed_tests)
    tail = _tail(_strip_ansi(output))
    if tail:
        lines.append("output_tail:")
        lines.append(tail)
    return "\n".join(lines)


class TestRunner:
    """跑测试命令，返回结构化 TestingEvent 而非裸文本（12 §3.1 自测环节）。"""

    __test__ = False  # 抑制 pytest 误收集（类名以 Test 开头）

    name = "TestRunner"
    description = (
        "运行测试命令并返回结构化结果（passed / failed / regression_detected），"
        "失败时附失败测试名与输出尾，供定向修复。"
        "何时使用：改完代码后验证改动（自测）；确认修复未破坏原有测试（Pass2Pass）。"
        "何时不使用：跑任意非测试命令请用 Bash。"
        "参数约束：command 为测试命令（在指定 sandbox 目录执行）；sandbox 支持 "
        "current（当前目录，默认）/ worktree:<name>（10 隔离 worktree）/ temp（临时目录副本，"
        "跑挂不污染当前目录）；regression_command 可选，为应保持通过的回归命令，"
        "主测试过但回归挂时返回 regression_detected。"
        "返回格式：[TestRunner] status=... + failed_tests + output_tail。"
        "配合：测试失败必须基于失败信息修复后重跑，不得绕开测试或伪造通过。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "测试命令（sandbox 目录下执行）"},
            "sandbox": {
                "type": "string",
                "description": "current（默认）/ worktree:<name> / temp",
            },
            "regression_command": {
                "type": "string",
                "description": "可选：应保持通过的回归测试命令（Pass2Pass 校验）",
            },
            "timeout": {"type": "integer", "description": "超时秒数（默认 180）"},
        },
        "required": ["command"],
    }
    category = "shell"
    require_confirm = False  # 自测验证动作，非任意命令执行

    def __init__(
        self, resolve_worktree: Callable[[str], Path | None] | None = None
    ) -> None:
        """resolve_worktree：worktree 名 → 路径（cli 注入 worktree_manager.path，解耦包循环）。"""
        self._resolve_worktree = resolve_worktree

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return True  # 保守声明（测试可能改动环境/文件）

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return False  # 测试有副作用（写缓存/改环境），串行

    def validate_input(self, input: dict[str, Any]) -> list[str]:
        command = input.get("command")
        if not isinstance(command, str) or not command.strip():
            return ["command 必填且非空"]
        timeout = input.get("timeout")
        if timeout is not None and (not isinstance(timeout, int) or timeout <= 0):
            return ["timeout 必须是正整数"]
        return []

    async def execute(self, ctx: ToolContext, input: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        command = str(input["command"]).strip()
        sandbox = str(input.get("sandbox") or "current")
        regression = str(input.get("regression_command") or "").strip()
        timeout = int(input.get("timeout") or _DEFAULT_TIMEOUT)
        try:
            run_dir, is_temp = self._resolve_run_dir(ctx, sandbox)
        except _SandboxError as exc:
            return self._result(ctx, start, str(exc), is_error=True)
        try:
            proc = await self._run(command, run_dir, timeout)
            if proc is None:
                return self._result(
                    ctx, start, f"[TestRunner] 命令执行失败/超时（{timeout}s）：{command}",
                    is_error=True,
                )
            if proc.returncode == 0:
                if regression:
                    rproc = await self._run(regression, run_dir, timeout)
                    if rproc is not None and rproc.returncode != 0:
                        status: TestStatus = "regression_detected"
                        failed = parse_failed_tests(rproc.stdout + rproc.stderr)
                        output = rproc.stdout + rproc.stderr
                    else:
                        status = "passed"
                        failed = []
                        output = proc.stdout + proc.stderr
                else:
                    status = "passed"
                    failed = []
                    output = proc.stdout + proc.stderr
            else:
                status = "failed"
                failed = parse_failed_tests(proc.stdout + proc.stderr)
                output = proc.stdout + proc.stderr
            content = _format_result(status, command, failed, output)
        finally:
            if is_temp:
                shutil.rmtree(run_dir, ignore_errors=True)
        if ctx.events is not None:
            ctx.events(
                TestingEvent(
                    status=status,
                    test_cmd=command,
                    failed_tests=tuple(failed),
                    summary=content,
                )
            )
        return self._result(
            ctx, start, content, is_error=status != "passed", duration=time.perf_counter() - start
        )

    # ---- 内部 ----

    def _resolve_run_dir(self, ctx: ToolContext, sandbox: str) -> tuple[Path, bool]:
        """解析沙箱 → (运行目录, 是否临时目录[执行后需清理])。"""
        if not sandbox or sandbox == "current":
            return ctx.work_dir, False
        if sandbox == "temp":
            return self._make_temp_copy(ctx.work_dir), True
        if sandbox.startswith(_WORKTREE_PREFIX):
            name = sandbox[len(_WORKTREE_PREFIX):]
            if self._resolve_worktree is not None:
                path = self._resolve_worktree(name)
                if path is not None:
                    return path, False
            raise _SandboxError(
                f"[TestRunner] worktree 不存在或未接线：{name}"
                "（可用 sandbox：current / temp / worktree:<name>）"
            )
        raise _SandboxError(
            f"[TestRunner] 未知 sandbox：{sandbox!r}（支持 current / temp / worktree:<name>）"
        )

    def _make_temp_copy(self, work_dir: Path) -> Path:
        """临时目录副本：排除 .git/.venv/node_modules 等大目录，跑挂不污染当前目录。"""
        tmp = Path(tempfile.mkdtemp(prefix="kdagent-test-"))
        shutil.copytree(
            work_dir,
            tmp,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(*_TEMP_EXCLUDE),
        )
        return tmp

    async def _run(
        self, command: str, cwd: Path, timeout: int
    ) -> _ProcResult | None:
        """执行命令（bash 优先，回退系统 shell）；超时/OSError 返回 None。"""
        bash = shutil.which("bash")
        try:
            if bash is not None:
                proc = await asyncio.create_subprocess_exec(
                    bash,
                    "-c",
                    command,
                    cwd=str(cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=str(cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                return None
        except OSError:
            return None
        return _ProcResult(
            # communicate() 后 returncode 已确定；类型上仍是 int | None，落到 1（非 0 即失败）
            returncode=proc.returncode if proc.returncode is not None else 1,
            stdout=_strip_ansi(stdout.decode(errors="replace")),
            stderr=_strip_ansi(stderr.decode(errors="replace")),
        )

    def _result(
        self,
        ctx: ToolContext,
        start: float,
        content: str,
        *,
        is_error: bool,
        duration: float | None = None,
    ) -> ToolResult:
        elapsed = duration if duration is not None else time.perf_counter() - start
        return ToolResult(
            tool_use_id=ctx.tool_use_id,
            name=self.name,
            content=content,
            is_error=is_error,
            duration_ms=int(elapsed * 1000),
        )
