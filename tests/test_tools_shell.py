"""Bash 工具测试（规格 03 §3.5：stdout / stderr / exit code）。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kdagent.config import Config
from kdagent.tools.base import ToolContext
from kdagent.tools.shell import Bash


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
