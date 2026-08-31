"""内置文件系统工具测试（规格 03 §3.5）。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

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


@_skip_no_rg
async def test_grep_timeout_returns_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """rg 子进程超时 → is_error + 超时提示 + 杀进程树（D5 v052，仿 shell D87）。

    `_wait_communicate` 首调抛 TimeoutError 模拟 rg 永久挂起；断言走超时分支：
    终止进程树被调用、返回 is_error、带已终止提示。
    """
    import kdagent.tools.filesystem as fs_mod

    killed: list[Any] = []
    real_terminate = fs_mod._terminate_tree

    def _spy_terminate(proc: Any) -> None:
        killed.append(proc)
        real_terminate(proc)  # 真实杀进程树（防孤儿）

    calls = {"n": 0}

    async def _hang(proc: Any, timeout: float) -> tuple[bytes, bytes]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError()
        return b"", b""  # 收尾读取：无缓冲输出

    monkeypatch.setattr(fs_mod, "_wait_communicate", _hang)
    monkeypatch.setattr(fs_mod, "_terminate_tree", _spy_terminate)
    _write(tmp_path / "a.py", "x = 1\n")
    result = await Grep().execute(_ctx(tmp_path), {"pattern": "x"})
    assert result.is_error is True
    assert "超时" in result.content
    assert "已终止进程树" in result.content
    assert len(killed) == 1  # 终止被调用一次


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


# ---- MSYS/git-bash 盘符路径转换（D92 实测：git-bash 输出 /d/... 被拼成 D:\d\...） ----

def test_msys_path_to_windows() -> None:
    from kdagent.tools.filesystem import _msys_path_to_windows

    # 存在的盘符 → 转 Windows 路径
    drive_d = _msys_path_to_windows("/d/个人开发/benchmark/a.c")
    assert drive_d is not None
    assert drive_d.lower().startswith("d:\\")
    assert drive_d.endswith("个人开发\\benchmark\\a.c")
    # 大小写归一
    assert _msys_path_to_windows("/D/x/y") is not None and _msys_path_to_windows("/D/x/y").lower().startswith("d:\\")  # type: ignore[union-attr]
    # 仅盘符无路径
    d_root = _msys_path_to_windows("/d")
    assert d_root is not None and d_root.lower() == "d:\\"  # type: ignore[union-attr]
    # 不存在的盘符（如 /tmp 的 t）→ 不转（防误伤相对路径）
    assert _msys_path_to_windows("/tmp/foo") is None
    # 非 MSYS 路径原样
    assert _msys_path_to_windows("C:\\Users\\a") is None
    assert _msys_path_to_windows("/home/user") is None


def test_resolve_path_converts_msys_and_wsl() -> None:
    from kdagent.tools.filesystem import _resolve_path

    # git-bash /d/... → WindowsPath（D 盘存在时）
    p = _resolve_path("/d/个人开发/x")
    assert p.is_absolute()
    assert "个人开发" in str(p) and "\\d\\" not in str(p)
    # /tmp 单字符路径 → 保持相对（不误转）
    assert not _resolve_path("/tmp/foo").is_absolute()


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
