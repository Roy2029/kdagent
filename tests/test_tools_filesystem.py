"""内置文件系统工具测试（规格 03 §3.5）。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from kdagent.config import Config
from kdagent.tools.base import ToolContext
from kdagent.tools.filesystem import (
    EditFile,
    Glob,
    Grep,
    ReadFile,
    WriteFile,
    _wsl_path_to_windows,
)


def _ctx(work_dir: Path) -> ToolContext:
    return ToolContext(work_dir=work_dir, config=Config(), tool_use_id="call_x")


_skip_no_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="无 ripgrep（rg）")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def test_read_file_with_line_numbers(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    _write(target, "第一行\n第二行\n")
    result = await ReadFile().execute(_ctx(tmp_path), {"path": str(target)})
    assert result.is_error is False
    assert "1: 第一行" in result.content
    assert "2: 第二行" in result.content


async def test_read_file_offset_limit(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    _write(target, "l1\nl2\nl3\nl4\n")
    result = await ReadFile().execute(_ctx(tmp_path), {"path": str(target), "offset": 1, "limit": 2})
    assert "2: l2" in result.content
    assert "3: l3" in result.content
    assert "l1" not in result.content
    assert "l4" not in result.content


async def test_read_file_not_found_is_error(tmp_path: Path) -> None:
    result = await ReadFile().execute(_ctx(tmp_path), {"path": str(tmp_path / "nope.txt")})
    assert result.is_error is True
    assert "文件不存在" in result.content


async def test_read_file_requires_absolute_path(tmp_path: Path) -> None:
    errors = ReadFile().validate_input({"path": "relative.txt"})
    assert errors and "绝对路径" in errors[0]


async def test_write_file_creates_and_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "new.txt"
    result = await WriteFile().execute(_ctx(tmp_path), {"path": str(target), "content": "hello"})
    assert result.is_error is False
    assert target.read_text(encoding="utf-8") == "hello"
    # 覆写
    await WriteFile().execute(_ctx(tmp_path), {"path": str(target), "content": "world"})
    assert target.read_text(encoding="utf-8") == "world"


async def test_edit_file_unique_replace(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    _write(target, "def f():\n    pass\n")
    result = await EditFile().execute(
        _ctx(tmp_path), {"path": str(target), "old_string": "pass", "new_string": "return 1"}
    )
    assert result.is_error is False
    assert "return 1" in target.read_text(encoding="utf-8")


async def test_edit_file_duplicate_old_string_fails(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    _write(target, "x = 1\nx = 2\n")
    result = await EditFile().execute(
        _ctx(tmp_path), {"path": str(target), "old_string": "x", "new_string": "y"}
    )
    assert result.is_error is True
    assert "2 次" in result.content


async def test_edit_file_missing_old_string_fails(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    _write(target, "abc\n")
    result = await EditFile().execute(
        _ctx(tmp_path), {"path": str(target), "old_string": "zzz", "new_string": "yyy"}
    )
    assert result.is_error is True


async def test_glob_relative_patterns(tmp_path: Path) -> None:
    _write(tmp_path / "pkg" / "a.py", "")
    _write(tmp_path / "pkg" / "b.py", "")
    _write(tmp_path / "README.md", "")
    result = await Glob().execute(_ctx(tmp_path), {"pattern": "pkg/*.py"})
    assert result.is_error is False
    assert result.content.splitlines() == ["pkg/a.py", "pkg/b.py"]


async def test_glob_base_path_override(tmp_path: Path) -> None:
    _write(tmp_path / "sub" / "c.py", "")
    result = await Glob().execute(_ctx(tmp_path), {"pattern": "*.py", "path": str(tmp_path / "sub")})
    assert result.content.splitlines() == ["c.py"]


@_skip_no_rg
async def test_grep_returns_matches(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "def alpha():\n    pass\nbeta = 1\n")
    _write(tmp_path / "b.py", "gamma = 2\n")
    result = await Grep().execute(_ctx(tmp_path), {"pattern": "alpha|beta", "glob": "*.py"})
    assert result.is_error is False
    assert "a.py" in result.content
    assert "alpha" in result.content
    assert "gamma" not in result.content


@_skip_no_rg
async def test_grep_no_match_is_not_error(tmp_path: Path) -> None:
    _write(tmp_path / "a.txt", "hello\n")
    result = await Grep().execute(_ctx(tmp_path), {"pattern": "nothere"})
    assert result.is_error is False
    assert result.content == ""


def test_meta_declarations() -> None:
    assert ReadFile().is_read_only() is True
    assert WriteFile().is_destructive() is True
    assert WriteFile().require_confirm is True
    assert EditFile().require_confirm is True
    assert Glob().is_concurrency_safe({}) is True
    assert Grep().is_concurrency_safe({}) is True


# ---- WSL 路径转换（demo 实测：Bash 走 WSL 时 ReadFile/WriteFile 拒绝 /mnt/c/...） ----
# 见 src/kdagent/tools/filesystem.py `_resolve_path` 注释。


def test_wsl_path_to_windows() -> None:
    assert _wsl_path_to_windows("/mnt/c/Users/Roy/x.c") == "C:\\Users\\Roy\\x.c"
    assert _wsl_path_to_windows("/mnt/d/") == "D:\\"
    assert _wsl_path_to_windows("/mnt/C/Users/a") == "C:\\Users\\a"  # 盘符大小写归一
    assert _wsl_path_to_windows("C:\\Users\\Roy\\x.c") is None  # 非 WSL 路径原样
    assert _wsl_path_to_windows("/home/user/x.c") is None


@pytest.mark.skipif(sys.platform != "win32", reason="WSL 路径转换仅在 win32 生效")
def test_validate_accepts_wsl_absolute_path() -> None:
    assert ReadFile().validate_input({"path": "/mnt/c/Users/Roy/x.c"}) == []
    assert WriteFile().validate_input(
        {"path": "/mnt/d/out.txt", "content": "x"}
    ) == []
    assert EditFile().validate_input(
        {"path": "/mnt/e/a.txt", "old_string": "a", "new_string": "b"}
    ) == []


@pytest.mark.skipif(sys.platform != "win32", reason="WSL 路径转换仅在 win32 生效")
async def test_read_file_accepts_wsl_path(tmp_path: Path) -> None:
    """Agent 从 Bash 拿到的 /mnt/<drive>/... 路径可直接读（demo 中被拒的复现）。"""
    target = tmp_path / "a.txt"
    _write(target, "hello wsl")
    drive = target.drive[0].lower()
    wsl_path = f"/mnt/{drive}/{target.relative_to(target.anchor).as_posix()}"
    result = await ReadFile().execute(_ctx(tmp_path), {"path": wsl_path})
    assert result.is_error is False
    assert "hello wsl" in result.content
