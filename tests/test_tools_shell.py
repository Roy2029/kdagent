"""Bash 工具测试（规格 03 §3.5：stdout / stderr / exit code）。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kdagent.config import Config
from kdagent.tools.base import ToolContext
from kdagent.tools.shell import Bash, wsl_delete_diagnosis


def _ctx(work_dir: Path) -> ToolContext:
    return ToolContext(work_dir=work_dir, config=Config(), tool_use_id="bash_1")


@pytest.mark.skipif(shutil.which("bash") is None, reason="无 bash")
async def test_bash_echo_stdout(tmp_path: Path) -> None:
    result = await Bash().execute(_ctx(tmp_path), {"command": "echo hello"})
    assert result.is_error is False
    assert "hello" in result.content
    assert "[exit] 0" in result.content


async def test_bash_nonzero_exit_is_error(tmp_path: Path) -> None:
    result = await Bash().execute(_ctx(tmp_path), {"command": "exit 3"})
    assert result.is_error is True
    assert "[exit] 3" in result.content


async def test_bash_cwd_is_work_dir(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("x", encoding="utf-8")
    result = await Bash().execute(_ctx(tmp_path), {"command": "ls"})
    assert result.is_error is False
    assert "marker.txt" in result.content


async def test_bash_empty_command_rejected(tmp_path: Path) -> None:
    errors = Bash().validate_input({"command": "   "})
    assert errors and "command" in errors[0]


def test_bash_meta_declarations() -> None:
    tool = Bash()
    assert tool.require_confirm is True
    assert tool.is_destructive() is True  # 保守声明（D10）
    assert tool.is_concurrency_safe({}) is False  # 保守声明
    assert tool.is_read_only() is False


# ---- R3：WSL 删除映射诊断（方案 B，纯函数） ----

def test_wsl_diagnosis_detects_drive_target() -> None:
    """WSL bash + Windows 盘符 rm 目标 → 命中，给出 /mnt/d 转换路径。"""
    d = wsl_delete_diagnosis('rm -f D:\\old-build', "C:\\Windows\\System32\\bash.exe")
    assert d is not None
    assert "映射诊断" in d
    assert "/mnt/d/old-build" in d


def test_wsl_diagnosis_unix_bash() -> None:
    """WSL 内嵌 Linux bash（/usr/bin/bash）同样命中。"""
    d = wsl_delete_diagnosis("rm -rf D:/Projects/old", "/usr/bin/bash")
    assert d is not None
    assert "/mnt/d/Projects/old" in d


def test_wsl_diagnosis_git_bash_skipped() -> None:
    """Git Bash（盘符路径可见）→ 不诊断（避免误报）。"""
    d = wsl_delete_diagnosis("rm -rf D:\\old-build", "C:\\Program Files\\Git\\bin\\bash.exe")
    assert d is None


def test_wsl_diagnosis_no_bash_skipped() -> None:
    """无 bash 可定位 → 不诊断。"""
    assert wsl_delete_diagnosis("rm -rf D:\\x", None) is None


def test_wsl_diagnosis_non_drive_target_skipped() -> None:
    """非盘符路径（Linux 路径）→ 不命中。"""
    d = wsl_delete_diagnosis("rm -rf /tmp/build", "/usr/bin/bash")
    assert d is None


def test_wsl_diagnosis_non_rm_skipped() -> None:
    """非 rm 命令 → 不命中（避免无关诊断）。"""
    d = wsl_delete_diagnosis("ls -la D:\\x", "/usr/bin/bash")
    assert d is None
