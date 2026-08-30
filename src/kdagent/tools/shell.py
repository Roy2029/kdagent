"""Bash 工具（规格 03 §3.5）。

保守声明：is_destructive / is_concurrency_safe 一律 True/False 保守取值，
命令级动态判断（只读命令如 ls/grep）留 06 权限系统，见 D10。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
import time
from typing import Any

from kdagent.tools.base import ToolContext, ToolResult

# 终端控制序列（CSI/OSC/其他 ESC 开头）——命令输出里的颜色/光标码必须剥离，
# 否则 \x1b 序列进入上下文污染模型（工具结果应为纯文本）。
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[@-_]")

# Bash 命令超时（秒）。D87 踩坑：Agent 找不到测试文件发起 `find /` 全盘扫描
# 永久挂起（shell.py 原无超时）。超时后杀子进程树防孤儿。进程级固定值，
# config 接线（tools.bash_timeout_ms）后续补。
_BASH_TIMEOUT = 300.0


class _BashTimeoutError(TimeoutError):
    """Bash 命令超过 _BASH_TIMEOUT 秒未完成（已终止子进程树）。"""

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


# WSL 启动器（System32\bash.exe）冷启动失败 E_UNEXPECTED 后的重试参数。
# memory 实测「稍后重试 Bash 通常恢复」——虚拟机冷启动首次调用必撞
# Bash/Service/E_UNEXPECTED，等 WSL 起来后重试即可（2026-08-28 d17c 会话）。
_WSL_RETRIES = 2
_WSL_RETRY_DELAY = 5.0  # 秒


def _decode_output(data: bytes) -> str:
    """解码子进程输出，自动识别 UTF-16LE。

    WSL 启动器（System32\\bash.exe）冷启动失败时输出的是 UTF-16LE 错误信息
    （如 `错误码: Bash/Service/E_UNEXPECTED`）。统一按 UTF-8 解码会得到
    `~p��...\\u0000B\\u0000a...` 式乱码污染模型上下文（实测 2026-08-28 d17c
    会话）——先检测 UTF-16 特征（BOM 或 ASCII 区段空字节密集）再解码。
    """
    if not data:
        return ""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    # 无 BOM 的 UTF-16LE：ASCII 字符每 2 字节有 1 个 \x00（奇位）。半数以上
    # 奇位为空字节即判定为 UTF-16LE，避免把普通二进制当文本。
    n = len(data)
    if n >= 4 and data[1::2].count(b"\x00") >= max(n // 4, 2):
        return data.decode("utf-16-le", errors="replace")
    return data.decode(errors="replace")


def _is_wsl_launcher_failure(text: str) -> bool:
    """判断输出是否命中 WSL 启动器冷启动失败特征（Bash/Service/E_UNEXPECTED）。"""
    t = text.upper()
    return "E_UNEXPECTED" in t or "BASH/SERVICE" in t


def _terminate_tree(proc: asyncio.subprocess.Process) -> None:
    """超时后终止子进程及其后代（防孤儿进程继续吃 CPU）。

    Windows：taskkill /T /F 杀整棵进程树（asyncio 的 proc.kill 只杀主进程，
    bash -c 的子孙进程会残留）；POSIX：杀主进程组无 setsid 不可靠，先 SIGKILL
    主进程再 proc.kill 兜底。proc 已退出则 no-op。
    """
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
            )
        else:
            os.kill(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


async def _run_command(
    bash: str | None, command: str, cwd: str
) -> tuple[int, bytes, bytes]:
    """启动一个子进程执行命令，返回 (exit_code, stdout, stderr)。

    D87/F9：communicate 包 asyncio.wait_for(_BASH_TIMEOUT)——Agent 发起
    `find /` 全盘扫描等命令不再永久挂起；超时先 _terminate_tree 再收尾读取
    已缓冲输出，然后抛 _BashTimeoutError。
    """
    if bash is not None:
        proc = await asyncio.create_subprocess_exec(
            bash,
            "-c",
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_BASH_TIMEOUT)
    except asyncio.TimeoutError as exc:
        _terminate_tree(proc)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        except (asyncio.TimeoutError, OSError):
            stdout, stderr = b"", b""
        raise _BashTimeoutError(f"Bash 命令超过 {_BASH_TIMEOUT:.0f}s 未完成") from exc
    return proc.returncode, stdout, stderr


async def _sleep(seconds: float) -> None:
    """可被测试 monkeypatch 的休眠点（重试间隔）。"""
    await asyncio.sleep(seconds)


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
            exit_code, stdout, stderr = await _run_command(bash, command, str(ctx.work_dir))
            # WSL 启动器冷启动：首次调用常撞 Bash/Service/E_UNEXPECTED（memory 实测
            # 「稍后重试通常恢复」）。命中则短等后重试，避免一开场就连败 3 次撞进
            # 「连续失败」引导（实测 2026-08-28 d17c 会话）。
            retried = False
            attempts = 0
            while (
                exit_code != 0
                and attempts < _WSL_RETRIES
                and _is_wsl_bash(bash)
                and _is_wsl_launcher_failure(
                    _decode_output(stdout) + "\n" + _decode_output(stderr)
                )
            ):
                attempts += 1
                retried = True
                await _sleep(_WSL_RETRY_DELAY)
                exit_code, stdout, stderr = await _run_command(bash, command, str(ctx.work_dir))
        except _BashTimeoutError as exc:
            # 必须在 except OSError 之前：Python 3.11+ 内置 TimeoutError 是 OSError 子类
            content = f"命令执行超时（>{_BASH_TIMEOUT:.0f}s），已终止子进程树：{exc}"
            if diagnosis:
                content = f"{content}\n\n{diagnosis}"
            return ToolResult(
                tool_use_id=ctx.tool_use_id,
                name=self.name,
                content=content,
                is_error=True,
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
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
        out = _ANSI_RE.sub("", _decode_output(stdout)).rstrip()
        err = _ANSI_RE.sub("", _decode_output(stderr)).rstrip()
        parts: list[str] = []
        if out:
            parts.append(f"[stdout]\n{out}")
        if err:
            parts.append(f"[stderr]\n{err}")
        parts.append(f"[exit] {exit_code}")
        if retried and exit_code == 0:
            parts.append("[重试] WSL 冷启动后恢复")
        if diagnosis:
            parts.append(diagnosis)
        return ToolResult(
            tool_use_id=ctx.tool_use_id,
            name=self.name,
            content="\n".join(parts),
            is_error=exit_code != 0 or diagnosis is not None,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
