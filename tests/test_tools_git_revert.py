"""GitRevert 精确回退工具测试（规格 12 §5 290 / §3.2 代码回滚）。

临时 git 仓库驱动：init + commit 文件 → 制造改动 → GitRevert 回退 → 断言恢复。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from kdagent.config import Config
from kdagent.tools.base import ToolContext
from kdagent.tools.git_revert import GitRevert

_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    )


def _make_repo(tmp_path: Path, *, content: str = "v1") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")  # git 2.24 不支持 init -b，用默认分支
    _git(repo, "config", "user.email", "t@test.local")
    _git(repo, "config", "user.name", "t")
    (repo / "hello.txt").write_text(content, encoding="utf-8")
    (repo / "world.txt").write_text("world-v1", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def _ctx(repo: Path) -> ToolContext:
    return ToolContext(work_dir=repo, config=Config(), tool_use_id="t1")


@pytest.mark.asyncio
async def test_revert_file_to_head(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "hello.txt").write_text("modified", encoding="utf-8")
    result = await GitRevert().execute(_ctx(repo), {"path": "hello.txt"})
    assert not result.is_error
    assert (repo / "hello.txt").read_text(encoding="utf-8") == "v1"  # 恢复
    assert "已回退" in result.content
    assert "after" in result.content


@pytest.mark.asyncio
async def test_revert_whole_workdir(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "hello.txt").write_text("m1", encoding="utf-8")
    (repo / "world.txt").write_text("m2", encoding="utf-8")
    result = await GitRevert().execute(_ctx(repo), {})
    assert not result.is_error
    assert (repo / "hello.txt").read_text(encoding="utf-8") == "v1"
    assert (repo / "world.txt").read_text(encoding="utf-8") == "world-v1"


@pytest.mark.asyncio
async def test_revert_keeps_untracked(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "hello.txt").write_text("modified", encoding="utf-8")
    (repo / "new.txt").write_text("untracked", encoding="utf-8")
    result = await GitRevert().execute(_ctx(repo), {})
    assert not result.is_error
    assert (repo / "hello.txt").read_text(encoding="utf-8") == "v1"  # tracked 恢复
    assert (repo / "new.txt").exists()  # untracked 保留


@pytest.mark.asyncio
async def test_revert_to_target_commit(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, content="A")  # commit1: A
    (repo / "hello.txt").write_text("B", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "v2")  # commit2: B
    (repo / "hello.txt").write_text("C", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "v3")  # commit3(HEAD): C
    (repo / "hello.txt").write_text("D", encoding="utf-8")  # 工作区未提交
    result = await GitRevert().execute(_ctx(repo), {"path": "hello.txt", "target": "HEAD~1"})
    assert not result.is_error
    assert (repo / "hello.txt").read_text(encoding="utf-8") == "B"  # 回退到 commit2 版本


@pytest.mark.asyncio
async def test_dry_run_previews_only(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "hello.txt").write_text("modified", encoding="utf-8")
    result = await GitRevert().execute(_ctx(repo), {"dry_run": True})
    assert not result.is_error
    assert (repo / "hello.txt").read_text(encoding="utf-8") == "modified"  # 未执行回退
    assert "预览" in result.content
    assert "hello.txt" in result.content  # 快照列出改动


@pytest.mark.asyncio
async def test_revert_path_escape_rejected(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    result = await GitRevert().execute(_ctx(repo), {"path": str(outside)})
    assert result.is_error
    assert "越界" in result.content


@pytest.mark.asyncio
async def test_revert_not_git_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("x", encoding="utf-8")
    result = await GitRevert().execute(_ctx(plain), {})
    assert result.is_error
    assert "非 git 仓库" in result.content


@pytest.mark.asyncio
async def test_revert_clean_workdir_noop(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = await GitRevert().execute(_ctx(repo), {})
    assert not result.is_error
    assert "无需回退" in result.content


def test_validate_input_rejects_bad_types() -> None:
    tool = GitRevert()
    assert tool.validate_input({"path": 123}) != []
    assert tool.validate_input({"target": ""}) != []
    assert tool.validate_input({"dry_run": "yes"}) != []
    assert tool.validate_input({}) == []
