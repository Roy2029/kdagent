"""Bash 工具超时（F9 / D87 踩坑②）：communicate 包 wait_for + 杀进程树防孤儿。

单独文件：test_tools_shell.py 的 monkeypatch 契约（_run_command 3 参）不能动，
超时是 _run_command 内部逻辑，独立测试文件隔离。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import kdagent.tools.shell as shell_mod
from kdagent.config import Config
from kdagent.tools.base import ToolContext
from kdagent.tools.shell import Bash


def _ctx(work_dir: Path) -> ToolContext:
    return ToolContext(work_dir=work_dir, config=Config(), tool_use_id="bash_t1")


@pytest.mark.skipif(shell_mod.shutil.which("bash") is None, reason="无 bash")
async def test_bash_timeout_returns_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """命令超过阈值 → is_error + 超时提示（不再永久挂起）。"""
    monkeypatch.setattr(shell_mod, "_BASH_TIMEOUT", 0.3)
    result = await Bash().execute(_ctx(tmp_path), {"command": "sleep 10"})
    assert result.is_error is True
    assert "超时" in result.content
    assert "已终止子进程树" in result.content


@pytest.mark.skipif(shell_mod.shutil.which("bash") is None, reason="无 bash")
async def test_bash_timeout_kills_tree_not_hang(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """关键回归：sleep 30 在 0.3s 阈值内被杀返回，调用全程不挂起。"""
    monkeypatch.setattr(shell_mod, "_BASH_TIMEOUT", 0.3)
    start = time.perf_counter()
    await Bash().execute(_ctx(tmp_path), {"command": "sleep 30"})
    elapsed = time.perf_counter() - start
    assert elapsed < 10  # 远小于 sleep 30 本身，证明进程树被终止而非等完


async def test_bash_timeout_normal_command_unaffected(tmp_path: Path) -> None:
    """快命令不受超时影响（正常路径回归）。"""
    result = await Bash().execute(_ctx(tmp_path), {"command": "echo hi"})
    assert result.is_error is False
    assert "hi" in result.content
