"""Bash 工具（规格 03 §3.5）。

保守声明：is_destructive / is_concurrency_safe 一律 True/False 保守取值，
命令级动态判断（只读命令如 ls/grep）留 06 权限系统，见 D10。
"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from typing import Any

from kdagent.tools.base import ToolContext, ToolResult

# 终端控制序列（CSI/OSC/其他 ESC 开头）——命令输出里的颜色/光标码必须剥离，
# 否则 \x1b 序列进入上下文污染模型（工具结果应为纯文本）。
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[@-_]")

# Windows 盘符路径：`D:\...` / `D:/...`（WSL 视图下不可见）。
_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _is_wsl_bash(bash: str | None) -> bool:
    """判断 Bash 是否为 WSL（wsl.exe 内嵌 / 原生 Linux）而非 Git Bash。

    WSL bash：`C:/Windows/System32/bash.exe`（wsl.exe 启动）或 `/usr/bin/bash`
    （WSL 内）。Git Bash（`Program Files/Git/bin/bash.exe`）能直接访问盘符路径
    `D:`，不需要映射诊断——排除之。
    """
    if not bash:
        return False
    b = bash.replace("\\", "/").lower()
    return (
        b.startswith("/usr/bin/bash")
        or b.startswith("/bin/bash")
        or ("system32" in b and b.endswith("bash.exe"))
    )


def _extract_rm_target(command: str) -> str | None:
    """提取 rm 命令的第一个目标参数（剥引号、剥 flags）。"""
    match = re.search(r"(?:^|[\s;&|])rm(?:\s+)?", command)
    if not match:
        return None
    rest = command[match.end():]
    for token in rest.split():
        stripped = token.strip("\"'")
        if stripped and not stripped.startswith("-") and not stripped.startswith("\\"):
            return stripped
    return None


def wsl_delete_diagnosis(command: str, bash: str | None) -> str | None:
    """R3 方案 B：Bash 删除命令的 WSL 映射诊断（纯函数）。

    场景：WSL bash 下 rm 目标是 Windows 盘符路径 `D:...` / `D:/...`——该路径在
    WSL 视图不可见，rm 静默 exit 0 报「已删除」实际未删（假成功）。命中返回 warn
    文本（含转换路径 `/mnt/d/...`），供 execute 附加到结果并标记 is_error。
    """
    if not _is_wsl_bash(bash):
        return None
    target = _extract_rm_target(command)
    if target is None or not _WIN_DRIVE_RE.match(target):
        return None
    drive = target[0].lower()
    rest = target[2:].replace("\\", "/").lstrip("/")
    wsl_path = f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"
    return (
        f"[映射诊断] 目标 {target} 是 Windows 盘符路径，WSL bash 视图下不可见，"
        f"rm 可能静默假成功（exit 0 但未删除）。转换路径：{wsl_path}——请改用该路径重试"
    )


class Bash:
    """执行 shell 命令，返回 stdout / stderr / exit code。"""

    name = "Bash"
    description = (
        "执行 shell 命令，返回 stdout、stderr 与退出码。"
        "何时使用：需要运行编译器、脚本、构建、文件操作命令时。"
        "何时不使用：纯文件读取/搜索请用 ReadFile/Grep（更安全、无副作用）。"
        "参数约束：command 为待执行命令，在项目工作目录下执行；优先 bash（Git Bash），"
        "无则回退系统 shell。"
        "返回格式：stdout + stderr + [exit] 退出码；退出码非 0 视为错误。"
        "配合：写代码 → Bash 编译验证。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "待执行的 shell 命令"},
        },
        "required": ["command"],
    }
    category = "shell"
    require_confirm = True

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return True

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return False

    def validate_input(self, input: dict[str, Any]) -> list[str]:
        command = input.get("command")
        if not isinstance(command, str) or not command.strip():
            return ["command 必填且非空"]
        return []

    async def execute(self, ctx: ToolContext, input: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        command = input["command"].strip()
        bash = shutil.which("bash")
        # R3 方案 B：WSL bash + Windows 盘符 rm 目标 → 命令照常执行，但结果附加
        # 映射诊断 warn 并标记 is_error（防假成功：WSL 视图下盘符路径不可见，rm
        # 静默 exit 0 但实际未删除）。
        diagnosis = wsl_delete_diagnosis(command, bash)
        try:
            if bash is not None:
                proc = await asyncio.create_subprocess_exec(
                    bash,
                    "-c",
                    command,
                    cwd=str(ctx.work_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=str(ctx.work_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            stdout, stderr = await proc.communicate()
        except OSError as exc:
            content = f"命令执行失败：{exc}"
            if diagnosis:
                content = f"{content}\n\n{diagnosis}"
            return ToolResult(
                tool_use_id=ctx.tool_use_id,
                name=self.name,
                content=content,
                is_error=True,
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        out = _ANSI_RE.sub("", stdout.decode(errors="replace")).rstrip()
        err = _ANSI_RE.sub("", stderr.decode(errors="replace")).rstrip()
        exit_code = proc.returncode
        parts: list[str] = []
        if out:
            parts.append(f"[stdout]\n{out}")
        if err:
            parts.append(f"[stderr]\n{err}")
        parts.append(f"[exit] {exit_code}")
        if diagnosis:
            parts.append(diagnosis)
        return ToolResult(
            tool_use_id=ctx.tool_use_id,
            name=self.name,
            content="\n".join(parts),
            is_error=exit_code != 0 or diagnosis is not None,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
