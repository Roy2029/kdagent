"""内置文件系统工具（规格 03 §3.5）：ReadFile / WriteFile / EditFile / Glob / Grep。

ReadFile / Glob / Grep 只读 + 并发安全；WriteFile / EditFile 破坏性 + 需确认。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from kdagent.tools.base import ToolContext, ToolResult
from kdagent.tools.shell import _terminate_tree


def _result(
    ctx: ToolContext,
    name: str,
    content: str,
    start: float,
    is_error: bool = False,
    persist_exempt: bool = False,
) -> ToolResult:
    return ToolResult(
        tool_use_id=ctx.tool_use_id,
        name=name,
        content=content,
        is_error=is_error,
        duration_ms=int((time.perf_counter() - start) * 1000),
        persist_exempt=persist_exempt,
    )


# WSL 挂载路径前缀：/mnt/<drive>/...（Bash 工具若命中 WSL bash，Agent 从输出
# 得到的文件路径即此风格；Windows Python 的 Path.is_absolute() 会误判为相对）
_WSL_MNT_RE = re.compile(r"^/mnt/([a-zA-Z])/")

# git-bash/MSYS 盘符路径：/d/...（本环境 Bash 工具是 git-bash，pwd 返回此风格，
# 与 WSL 的 /mnt/d/... 不同——D92 实测 Grep/Glob 收到 /d/... 被拼成 D:\d\... 双重盘符）
_MSYS_DRIVE_RE = re.compile(r"^/([a-zA-Z])(/|$)")

# Grep 超时（秒，D5 v052 review）：rg 扫大目录/网络挂载可能永久挂起，仿 shell.py
# D87 模式——超时杀进程树，输出已收集部分 + is_error 提示，不永久挂住 Agent。
_GREP_TIMEOUT = 60.0


def _drive_exists(drive: str) -> bool:
    """Windows 盘符是否存在（防把 /tmp 等单字符路径误当 MSYS 盘符路径）。"""
    return os.path.isdir(f"{drive}:\\")


def _wsl_path_to_windows(raw: str) -> str | None:
    """WSL 挂载路径 /mnt/<drive>/... → Windows 路径字符串；非 WSL 路径返回 None。"""
    m = _WSL_MNT_RE.match(raw)
    if not m:
        return None
    drive = m.group(1).upper()
    rest = raw[m.end() :].replace("/", "\\")
    return f"{drive}:\\{rest}"


def _msys_path_to_windows(raw: str) -> str | None:
    """git-bash/MSYS 盘符路径 /d/... → Windows 路径字符串；盘符存在才转防误伤。"""
    m = _MSYS_DRIVE_RE.match(raw)
    if not m or not _drive_exists(m.group(1).upper()):
        return None
    drive = m.group(1).upper()
    rest = raw[m.end() :].replace("/", "\\")
    return f"{drive}:\\{rest}"


def _resolve_path(raw: str) -> Path:
    """解析工具路径参数：win32 下把 WSL 挂载路径（/mnt/<drive>/...）与 git-bash
    盘符路径（/d/...）都转为 Windows 路径后返回。

    demo 实测：ReadFile/WriteFile 收到 Agent 从 Bash(pwd) 得到的 /mnt/c/...，
    WindowsPath.is_absolute() 判 False → 工具被拒，Agent 只能改用 Bash 写文件。
    D92 实测：git-bash 输出 /d/... 不被 /mnt/ 规则匹配 → 拼 work_dir 成 D:\d\... 双重
    盘符 → Grep/Glob 全失败。本函数让 ReadFile/WriteFile/EditFile/Glob/Grep 直接接受
    WSL 与 MSYS 两种盘符路径；Linux 原样。
    """
    if sys.platform == "win32":
        win = _wsl_path_to_windows(raw) or _msys_path_to_windows(raw)
        if win is not None:
            return Path(win)
    return Path(raw)


def _absolute(base: Path, raw: str) -> Path:
    p = _resolve_path(raw)
    return p if p.is_absolute() else base / p


class ReadFile:
    """读取文件，返回带行号文本；支持 offset/limit 局部读取。"""

    name = "ReadFile"
    description = (
        "读取指定文件的文本内容，返回带行号文本。"
        "何时使用：需要查看文件完整内容或特定行范围时。"
        "何时不使用：二进制文件不可读（请用 Bash）；只需定位关键词请用 Grep。"
        "参数约束：path 必须是绝对路径；大文件建议先 Grep 定位再用 offset/limit 读取片段。"
        "返回格式：每行带行号前缀；文件不存在返回错误。"
        "配合：定位用 Grep → 读片段用 ReadFile(offset/limit) → 改动用 EditFile。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件绝对路径"},
            "offset": {"type": "integer", "minimum": 0, "description": "起始行（0 基，默认 0）"},
            "limit": {"type": "integer", "minimum": 1, "description": "最大读取行数"},
        },
        "required": ["path"],
    }
    category = "filesystem"
    require_confirm = False

    def is_read_only(self) -> bool:
        return True

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return True

    def validate_input(self, input: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        path = input.get("path")
        if not isinstance(path, str) or not path:
            errors.append("path 必填且为字符串")
        elif not _resolve_path(path).is_absolute():
            errors.append("path 必须是绝对路径")
        return errors

    async def execute(self, ctx: ToolContext, input: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        path = _resolve_path(input["path"])
        offset = int(input.get("offset", 0))
        limit = input.get("limit")
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return _result(ctx, self.name, f"文件不存在：{path}", start, True)
        except IsADirectoryError:
            return _result(ctx, self.name, f"目标是目录，非文件：{path}", start, True)
        except PermissionError:
            return _result(ctx, self.name, f"无权限读取：{path}", start, True)
        lines = (
            lines[offset : offset + int(limit)] if limit is not None else lines[offset:]
        )
        content = "".join(f"{i + 1 + offset}: {line}" for i, line in enumerate(lines))
        # 01 §5.2 读回闭环：读回落盘文件（tool-results 目录内）→ 豁免 L1 二次落盘。
        persist_exempt = (
            ctx.persist_dir is not None and path.is_relative_to(ctx.persist_dir)
        )
        return _result(ctx, self.name, content, start, persist_exempt=persist_exempt)


class WriteFile:
    """创建或完全覆写文件（破坏性，需确认）。"""

    name = "WriteFile"
    description = (
        "创建新文件或完全覆写现有文件，返回写入统计。"
        "何时使用：需要创建新文件或全文重写时。"
        "何时不使用：局部修改请用 EditFile（精确替换，不破坏其余内容）。"
        "参数约束：path 必须是绝对路径；content 为完整文件内容（可为空串）。"
        "返回格式：写入字符数与路径。"
        "配合：先 ReadFile 确认现状再覆写。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件绝对路径"},
            "content": {"type": "string", "description": "完整文件内容"},
        },
        "required": ["path", "content"],
    }
    category = "filesystem"
    require_confirm = True

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return True

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return False

    def validate_input(self, input: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        path = input.get("path")
        if not isinstance(path, str) or not path:
            errors.append("path 必填且为字符串")
        elif not _resolve_path(path).is_absolute():
            errors.append("path 必须是绝对路径")
        if "content" not in input or not isinstance(input["content"], str):
            errors.append("content 必填且为字符串")
        return errors

    async def execute(self, ctx: ToolContext, input: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        path = _resolve_path(input["path"])
        content = input["content"]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return _result(ctx, self.name, f"写入失败：{exc}", start, True)
        return _result(ctx, self.name, f"已写入 {len(content)} 字符到 {path}", start)


class EditFile:
    """精确替换 old_string → new_string（破坏性，需确认；要求唯一匹配）。"""

    name = "EditFile"
    description = (
        "在指定文件中精确替换 old_string 为 new_string，返回替换统计。"
        "old_string 必须在文件中唯一匹配；出现多次会报错（避免误改）。"
        "何时使用：局部修改文件内容时。"
        "何时不使用：全文重写用 WriteFile；先确认现状请 ReadFile。"
        "参数约束：path 必须是绝对路径；old_string 唯一，多匹配报错。"
        "返回格式：替换结果与匹配计数。"
        "配合：先 Grep 定位 old_string 确保唯一。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件绝对路径"},
            "old_string": {"type": "string", "description": "待替换的原文（须唯一匹配）"},
            "new_string": {"type": "string", "description": "替换后的新文本"},
        },
        "required": ["path", "old_string", "new_string"],
    }
    category = "filesystem"
    require_confirm = True

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return True

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return False

    def validate_input(self, input: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        path = input.get("path")
        if not isinstance(path, str) or not path:
            errors.append("path 必填且为字符串")
        elif not _resolve_path(path).is_absolute():
            errors.append("path 必须是绝对路径")
        for key in ("old_string", "new_string"):
            if key not in input or not isinstance(input[key], str):
                errors.append(f"{key} 必填且为字符串")
        return errors

    async def execute(self, ctx: ToolContext, input: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        path = _resolve_path(input["path"])
        old_string = input["old_string"]
        new_string = input["new_string"]
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return _result(ctx, self.name, f"读取失败：{exc}", start, True)
        count = text.count(old_string)
        if count == 0:
            return _result(ctx, self.name, "old_string 未在文件中找到", start, True)
        if count > 1:
            return _result(
                ctx, self.name, f"old_string 出现 {count} 次，需唯一匹配才能替换", start, True
            )
        try:
            path.write_text(text.replace(old_string, new_string, 1), encoding="utf-8")
        except OSError as exc:
            return _result(ctx, self.name, f"写入失败：{exc}", start, True)
        return _result(ctx, self.name, f"已替换 1 处到 {path}", start)


class Glob:
    """按 glob 模式匹配文件路径，返回相对路径列表。"""

    name = "Glob"
    description = (
        "按路径模式匹配项目文件，返回匹配的相对路径列表。"
        "何时使用：探索项目结构、查找某类文件时。"
        "何时不使用：按内容搜索用 Grep。"
        "参数约束：pattern 支持 ** 递归；path 为搜索基准目录（默认工作目录），相对/绝对皆可。"
        "返回格式：每行一个匹配路径（相对基准目录）；无匹配返回空。"
        "配合：先 Glob 摸清文件布局 → Grep 定位具体内容 → ReadFile 读取片段。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob 模式，如 **/*.py"},
            "path": {"type": "string", "description": "搜索基准目录（默认工作目录）"},
        },
        "required": ["pattern"],
    }
    category = "filesystem"
    require_confirm = False

    def is_read_only(self) -> bool:
        return True

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return True

    def validate_input(self, input: dict[str, Any]) -> list[str]:
        pattern = input.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return ["pattern 必填且为字符串"]
        return []

    async def execute(self, ctx: ToolContext, input: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        pattern = input["pattern"]
        raw_path = input.get("path") or str(ctx.work_dir)
        base = _absolute(ctx.work_dir, raw_path)
        if not base.is_dir():
            return _result(ctx, self.name, f"目录不存在：{base}", start, True)
        try:
            matches = sorted(p.relative_to(base).as_posix() for p in base.glob(pattern))
        except (OSError, ValueError) as exc:
            return _result(ctx, self.name, f"匹配失败：{exc}", start, True)
        return _result(ctx, self.name, "\n".join(matches), start)


class Grep:
    """用 ripgrep 按正则搜索文件内容。"""

    name = "Grep"
    description = (
        "用 ripgrep 按正则表达式搜索文件内容，返回匹配行。"
        "何时使用：在项目中定位关键词、模式时。"
        "何时不使用：按文件名查找用 Glob。"
        "参数约束：pattern 为正则；path 为搜索基准目录（默认工作目录）；glob 过滤文件类型。"
        "返回格式：path:行号:内容，每行一个匹配；无匹配返回空（非错误）。"
        "配合：定位到行后用 ReadFile 读上下文。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式"},
            "path": {"type": "string", "description": "搜索基准目录（默认工作目录）"},
            "glob": {"type": "string", "description": "文件类型过滤，如 *.py"},
        },
        "required": ["pattern"],
    }
    category = "filesystem"
    require_confirm = False

    def is_read_only(self) -> bool:
        return True

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return True

    def validate_input(self, input: dict[str, Any]) -> list[str]:
        pattern = input.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return ["pattern 必填且为字符串"]
        return []

    async def execute(self, ctx: ToolContext, input: dict[str, Any]) -> ToolResult:
        start = time.perf_counter()
        rg = shutil.which("rg")
        if rg is None:
            return _result(ctx, self.name, "需要 ripgrep（rg）但未安装", start, True)
        pattern = input["pattern"]
        raw_path = input.get("path") or str(ctx.work_dir)
        base = _absolute(ctx.work_dir, raw_path)
        args = [rg, "--no-heading", "-n", "--color", "never"]
        glob_filter = input.get("glob")
        if isinstance(glob_filter, str) and glob_filter:
            args += ["--glob", glob_filter]
        args += [pattern, str(base)]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await _wait_communicate(proc, _GREP_TIMEOUT)
        except TimeoutError:
            # D87 模式：超时杀进程树防孤儿，再读 5s 收尾缓冲输出。
            _terminate_tree(proc)
            try:
                stdout, stderr = await _wait_communicate(proc, 5)
            except (TimeoutError, OSError):
                stdout, stderr = b"", b""
            err = stderr.decode(errors="replace").strip()
            content = f"rg 搜索超时（>{_GREP_TIMEOUT:.0f}s），已终止进程树"
            partial = stdout.decode(errors="replace").rstrip()
            if partial:
                content = f"{content}\n已收集输出结尾：\n{partial[-2000:]}"
            if err:
                content = f"{content}\nstderr：{err}"
            return _result(ctx, self.name, content, start, True)
        if proc.returncode == 2:
            err = stderr.decode(errors="replace").strip()
            return _result(ctx, self.name, f"rg 执行失败：{err}", start, True)
        # returncode == 1 表示无匹配，是正常结果而非错误
        content = stdout.decode(errors="replace").rstrip()
        return _result(ctx, self.name, content, start)


async def _wait_communicate(
    proc: asyncio.subprocess.Process, timeout: float
) -> tuple[bytes, bytes]:
    """包一层 wait_for（Grep 超时测试的 monkeypatch 目标，同 agent._retry_sleep 手法）。"""
    return await asyncio.wait_for(proc.communicate(), timeout=timeout)
