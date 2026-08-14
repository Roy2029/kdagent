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
            return ToolResult(
                tool_use_id=ctx.tool_use_id,
                name=self.name,
                content=f"命令执行失败：{exc}",
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
        return ToolResult(
            tool_use_id=ctx.tool_use_id,
            name=self.name,
            content="\n".join(parts),
            is_error=exit_code != 0,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
