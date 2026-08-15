"""GitRevert：精确回退工具（规格 12 §5 290 / 12 §3.2 代码回滚）。

worktree 变更保护下，失败改动可精确回退：`git checkout <target> -- <path>`
恢复文件/目录到指定提交；无 path 恢复整个工作区 tracked 改动到 target
（默认 HEAD = 丢弃未提交改动）。untracked 新文件**不回退**——保留由 Agent
自行判断（git clean 会误删新文件，危险，不做）。

- dry_run 只预览将回退的改动（git status --porcelain），不实际执行；
- target 允许任意 git 引用（HEAD / 分支 / SHA / HEAD~n）；
- git 子进程三重防挂起（GIT_TERMINAL_PROMPT=0 + GIT_ASKPASS="" + stdin 忽略，
  对齐 subagent/worktree.py _git 模式）；超时杀进程报错。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from kdagent.tools.base import ToolContext, ToolResult

_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}
_DEFAULT_TIMEOUT = 60


class _GitError(Exception):
    """git 调用失败 / 参数越界。"""


async def _run_git(work_dir: Path, *args: str) -> str:
    """git 子进程执行：成功返回 stdout，失败抛 _GitError。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(work_dir),
            *args,
            env=_GIT_ENV,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise _GitError("未找到 git 命令（Git for Windows 需安装并在 PATH）") from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_DEFAULT_TIMEOUT)
    except TimeoutError as exc:
        proc.kill()
        raise _GitError(f"git 调用超时（>{_DEFAULT_TIMEOUT}s）：git {' '.join(args)}") from exc
    if proc.returncode != 0:
        msg = (stderr or stdout).decode("utf-8", errors="replace").strip()
        raise _GitError(f"git {args[0]} 失败：{msg}")
    return stdout.decode("utf-8", errors="replace")


def _resolve_path(work_dir: Path, raw: str | None) -> Path | None:
    """解析 path 参数为绝对路径；越界 work_dir 抛错（LLM 输入不可信）。"""
    if raw is None:
        return None
    p = Path(raw)
    candidate = p if p.is_absolute() else work_dir / p
    resolved = candidate.resolve()
    root = work_dir.resolve()
    if not resolved.is_relative_to(root):
        raise _GitError(f"path 越界 work_dir：{raw!r}")
    return resolved


def _rel_path(work_dir: Path, path: Path) -> str:
    """work_dir 相对路径（git pathspec 用；反斜杠转正斜杠）。"""
    return path.relative_to(work_dir.resolve()).as_posix()


class GitRevert:
    """精确回退工作区/文件改动到指定状态（12 §3.2 代码回滚，不靠手动恢复）。"""

    name = "GitRevert"
    description = (
        "精确回退 git 工作区的失败改动：恢复指定文件/目录（或整个工作区 tracked 改动）"
        "到指定提交（默认 HEAD，即丢弃未提交改动）。"
        "何时使用：改动引入 bug 需要精确回退时；子 Agent 在 worktree 里搞坏代码要恢复时。"
        "何时不使用：回退 untracked 新文件（本工具刻意保留，由 Agent 判断）；"
        "查看改动内容请用 GitDiff/Grep。"
        "参数约束：path 为相对 work_dir 的文件或目录（不填 = 整个工作区 tracked 改动）；"
        "target 为 git 引用（HEAD / 分支 / SHA / HEAD~n，默认 HEAD）；"
        "dry_run=true 只预览将回退的改动不实际执行。"
        "返回格式：[GitRevert] target=... path=... + 回退前/后 git status。"
        "配合：改坏代码 → GitRevert 恢复 → 重试修改。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对 work_dir 的文件或目录；不填 = 回退整个工作区 tracked 改动",
            },
            "target": {
                "type": "string",
                "description": "回退目标 git 引用（HEAD / 分支 / SHA / HEAD~n，默认 HEAD）",
            },
            "dry_run": {"type": "boolean", "description": "只预览将回退的改动，不实际执行"},
        },
        "required": [],
    }
    category = "git"
    require_confirm = True  # 破坏性：丢弃工作区改动，不可逆

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return True

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return False  # git 操作有状态（index/工作区），串行

    def validate_input(self, input: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        for key in ("path", "target"):
            val = input.get(key)
            if val is not None and (not isinstance(val, str) or not val.strip()):
                errors.append(f"{key} 必须是非空字符串")
        dry = input.get("dry_run")
        if dry is not None and not isinstance(dry, bool):
            errors.append("dry_run 必须是布尔值")
        return errors

    def _result(
        self,
        ctx: ToolContext,
        content: str,
        start: float,
        *,
        is_error: bool = False,
    ) -> ToolResult:
        return ToolResult(
            tool_use_id=ctx.tool_use_id,
            name=self.name,
            content=content,
            is_error=is_error,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )

    async def execute(self, ctx: ToolContext, input: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        work_dir = ctx.work_dir
        raw_path = input.get("path")
        raw_path = str(raw_path).strip() if isinstance(raw_path, str) and raw_path.strip() else None
        target = str(input.get("target") or "HEAD").strip()
        dry_run = bool(input.get("dry_run", False))
        try:
            # 前置校验：必须是 git 仓库（git status 失败即非仓库，非「干净」）。
            before = (await _run_git(work_dir, "status", "--porcelain")).strip()
        except _GitError as exc:
            return self._result(ctx, f"[GitRevert] 非 git 仓库：{exc}", start, is_error=True)
        try:
            path = _resolve_path(work_dir, raw_path)
            if raw_path and path is not None and not path.exists():
                return self._result(
                    ctx,
                    f"[GitRevert] 路径不存在：{raw_path}",
                    start,
                    is_error=True,
                )
            if dry_run:
                preview = before or "（工作区干净，无 tracked 改动可回退）"
                return self._result(
                    ctx,
                    f"[GitRevert] 预览（dry_run）target={target} path={raw_path or '.'}\n"
                    f"将回退的改动：\n{preview}",
                    start,
                )
            if not before:
                return self._result(
                    ctx,
                    f"[GitRevert] 无未提交改动，无需回退（target={target} path={raw_path or '.'}）",
                    start,
                )
            # 精确回退：git checkout <target> -- <pathspec>（默认 target=HEAD）。
            specs = [_rel_path(work_dir, path)] if path else ["."]
            if target == "HEAD":
                await _run_git(work_dir, "checkout", "--", *specs)
            else:
                await _run_git(work_dir, "checkout", target, "--", *specs)
            after = (await _run_git(work_dir, "status", "--porcelain")).strip()
        except _GitError as exc:
            return self._result(ctx, f"[GitRevert] {exc}", start, is_error=True)
        reverted = len(before.splitlines()) - len(after.splitlines())
        lines = [
            f"[GitRevert] 已回退 target={target} path={raw_path or '.'}（{reverted} 条改动消失）",
            "before:",
            before,
            "after:",
            after or "（工作区干净）",
        ]
        return self._result(ctx, "\n".join(lines), start)
