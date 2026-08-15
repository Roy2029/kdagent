"""L2 路径沙箱（规格 06 §3.4）：允许根内放行、越界拒绝、symlink 逃逸。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kdagent.permission.sandbox import PathSandbox


def test_inside_root_allowed(tmp_path: Path) -> None:
    sb = PathSandbox([tmp_path])
    assert sb.contains(tmp_path / "src" / "main.py")
    assert sb.contains(str(tmp_path / "data" / "x.json"))


def test_outside_root_denied(tmp_path: Path) -> None:
    # include_tempdir=False：tmp_path 本身就在系统临时目录下，否则"越界"也被放行。
    sb = PathSandbox([tmp_path], include_tempdir=False)
    outside = tmp_path.parent / "other" / "secret.txt"
    assert not sb.contains(outside)


def test_relative_path_resolved_against_work_dir(tmp_path: Path) -> None:
    sb = PathSandbox([tmp_path], work_dir=tmp_path, include_tempdir=False)
    assert sb.contains("README.md")  # 相对路径以 work_dir 为基准
    assert not sb.contains("../outside.txt")


def test_symlink_escape_detected(tmp_path: Path) -> None:
    """目录内 symlink 指向沙箱外 → 必须拦（resolve 后比对真实路径）。"""
    inside = tmp_path / "proj"
    inside.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = inside / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境无 symlink 权限（Windows 需管理员/开发者模式）")
    sb = PathSandbox([inside])
    assert not sb.contains(link)  # 字面路径在沙箱内，解析后指向沙箱外


def test_nonexistent_file_parent_in_sandbox_allowed(tmp_path: Path) -> None:
    """WriteFile 新建文件：文件本身不存在，父目录在沙箱内 → 放行（§3.4 建链）。"""
    sb = PathSandbox([tmp_path])
    new_file = tmp_path / "new" / "created.py"
    assert sb.contains(new_file)


@pytest.mark.skipif(os.name != "nt", reason="Windows 路径大小写不敏感")
def test_windows_case_insensitive(tmp_path: Path) -> None:
    sb = PathSandbox([tmp_path])
    upper = str(tmp_path).upper() + "\\MAIN.PY"
    assert sb.contains(upper)
