"""WorktreeManager：Git Worktree 空间隔离（规格 10 §3.10-3.13，M5-b 核心）。

测试在 tmp 临时 git 仓库内跑真实 `git worktree add/remove`（Git for Windows 2.5+）。
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import FakeLLM, done, tool_call

from kdagent.config import Config
from kdagent.subagent import BUILTIN_AGENTS_DIR
from kdagent.subagent.agent_tool import Agent as AgentTool
from kdagent.subagent.manager import AgentManager
from kdagent.subagent.runner import SubAgentRunner
from kdagent.subagent.task import TaskManager
from kdagent.subagent.worktree import (
    WorktreeError,
    WorktreeManager,
    validate_name,
)
from kdagent.tools import build_default_registry

_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}

_BUILTIN = BUILTIN_AGENTS_DIR


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, env=_GIT_ENV, capture_output=True, text=True, encoding="utf-8"
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout


def _make_repo(tmp_path: Path) -> Path:
    """初始化一个含初始 commit 的临时 git 仓库（worktree add 需要 HEAD）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@test.local")
    _git(repo, "config", "user.name", "test")
    (repo / ".gitignore").write_text(".kdagent/\n", encoding="utf-8")
    (repo / "README.md").write_text("# t\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def _manager(repo: Path, *, max_age: float = 3600.0) -> WorktreeManager:
    return WorktreeManager(repo, repo / ".kdagent" / "worktrees", max_age=max_age)


def _agent_tool(repo: Path, llm: FakeLLM) -> tuple[AgentTool, WorktreeManager]:
    manager = AgentManager([_BUILTIN])
    manager.scan()
    runner = SubAgentRunner(
        llm=llm,
        tools=build_default_registry(),
        config=Config(),
        work_dir=repo,
    )
    tm = TaskManager(runner)
    wm = _manager(repo)
    return AgentTool(runner, manager, tm, wm), wm


class _Ctx:
    tool_use_id = "c1"


# ---- slug 验证 ----

def test_validate_name_accepts_valid() -> None:
    for name in ("abc", "a1-_b", "team-refactor/alice", "wf_123"):
        validate_name(name)  # 不抛


def test_validate_name_rejects() -> None:
    for name in ("", "a" * 65, "../etc", ".hidden", "a//b", "a b", "a$b", "..", "a/.."):
        with pytest.raises(WorktreeError):
            validate_name(name)


# ---- 生命周期 ----

def test_create_list_get(repo: Path) -> None:
    wm = _manager(repo)
    wt = wm.create("agent-abc")
    assert wt.name == "agent-abc"
    assert wt.branch == "worktree-agent-abc"
    assert Path(wt.path).is_dir()
    assert Path(wt.path).joinpath("README.md").exists()  # 从 HEAD 检出
    assert wm.get("agent-abc") is wt
    assert wm.list() == [wt]
    assert wm.path("agent-abc") == Path(wt.path)


def test_create_duplicate_rejected(repo: Path) -> None:
    wm = _manager(repo)
    wm.create("agent-abc")
    with pytest.raises(WorktreeError, match="已存在"):
        wm.create("agent-abc")


def test_unknown_name(repo: Path) -> None:
    wm = _manager(repo)
    assert wm.get("nope") is None
    assert wm.path("nope") is None
    with pytest.raises(WorktreeError, match="不存在"):
        wm.remove("nope")


def test_create_worktree_dir_untracked_in_main(repo: Path) -> None:
    """worktree 目录本身不污染主目录 git status（.gitignore 兜底）。"""
    wm = _manager(repo)
    wm.create("agent-abc")
    assert _git(repo, "status", "--porcelain").strip() == ""


# ---- 变更检测 ----

def test_has_changes_clean_vs_modified(repo: Path) -> None:
    wm = _manager(repo)
    wt = wm.create("agent-abc")
    assert not wm.has_changes("agent-abc")  # 干净
    (Path(wt.path) / "new.txt").write_text("x\n", encoding="utf-8")
    assert wm.has_changes("agent-abc")  # 未跟踪文件


def test_has_changes_new_commit(repo: Path) -> None:
    wm = _manager(repo)
    wt = wm.create("agent-abc")
    (Path(wt.path) / "new.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "-C", wt.path, "add", ".")
    _git(repo, "-C", wt.path, "commit", "-m", "work")
    assert wm.has_changes("agent-abc")  # 新 commit


def test_has_changes_missing_dir_is_clean(repo: Path) -> None:
    wm = _manager(repo)
    wt = wm.create("agent-abc")
    shutil.rmtree(wt.path)
    assert not wm.has_changes("agent-abc")  # 目录消失视作无变更，不反复 fail-closed


# ---- 自动清理 ----

def test_auto_cleanup_removes_when_clean(repo: Path) -> None:
    wm = _manager(repo)
    wm.create("agent-abc")
    assert wm.auto_cleanup("agent-abc") is False
    assert wm.get("agent-abc") is None
    assert not (repo / ".kdagent" / "worktrees" / "agent-abc").exists()


def test_auto_cleanup_keeps_when_changed(repo: Path) -> None:
    wm = _manager(repo)
    wt = wm.create("agent-abc")
    (Path(wt.path) / "new.txt").write_text("x\n", encoding="utf-8")
    assert wm.auto_cleanup("agent-abc") is True
    assert wm.get("agent-abc") is not None  # 保留供 review


# ---- 删除（fail-closed） ----

def test_remove_refuses_changed(repo: Path) -> None:
    wm = _manager(repo)
    wt = wm.create("agent-abc")
    (Path(wt.path) / "new.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(WorktreeError, match="拒绝删除"):
        wm.remove("agent-abc")
    assert wm.get("agent-abc") is not None  # 未被删除


def test_remove_force_and_clean(repo: Path) -> None:
    wm = _manager(repo)
    wm.create("agent-abc")
    wm.remove("agent-abc")
    assert wm.get("agent-abc") is None

    wt = wm.create("agent-def")
    (Path(wt.path) / "new.txt").write_text("x\n", encoding="utf-8")
    wm.remove("agent-def", force=True)
    assert wm.get("agent-def") is None


# ---- 过期清理漏斗 ----

def test_cleanup_expired_removes_only_expired_temp(repo: Path) -> None:
    wm = _manager(repo, max_age=50.0)
    wm.create("agent-old")
    wm.create("agent-new")
    wm.create("my-feature")  # 非临时命名，永不清
    old = wm.get("agent-old")
    assert old is not None
    wm._active["agent-old"] = dataclasses.replace(old, created=old.created - 100.0)  # 拨回过期
    removed = wm.cleanup_expired()
    assert removed == 1
    assert wm.get("agent-old") is None
    assert wm.get("agent-new") is not None  # 未过期
    assert wm.get("my-feature") is not None  # 非临时命名


def test_cleanup_expired_fail_closed_keeps_changed(repo: Path) -> None:
    wm = _manager(repo, max_age=-1.0)  # 全部立即过期
    wt = wm.create("agent-abc")
    (Path(wt.path) / "new.txt").write_text("x\n", encoding="utf-8")
    assert wm.cleanup_expired() == 0  # 有变更，fail-closed 保留
    assert wm.get("agent-abc") is not None


# ---- 持久化 ----

def test_session_persists_across_instances(repo: Path) -> None:
    wm = _manager(repo)
    wm.create("agent-abc")
    session_file = repo / ".kdagent" / "worktrees" / "worktree_session.json"
    assert session_file.exists()
    data = json.loads(session_file.read_text(encoding="utf-8"))
    assert "agent-abc" in data["worktrees"]

    assert _manager(repo).get("agent-abc") is not None  # 新实例加载


def test_session_load_drops_missing_dir(repo: Path) -> None:
    wm = _manager(repo)
    wt = wm.create("agent-abc")
    shutil.rmtree(wt.path)  # 目录消失（外部 git worktree prune 场景）
    assert _manager(repo).get("agent-abc") is None  # 孤儿丢弃


# ---- 与 SubAgent 配合（§3.12） ----

@pytest.mark.asyncio
async def test_worktree_isolation_foreground_cleanup(repo: Path) -> None:
    """isolation=worktree 前台：子 Agent 在独立目录跑，无变更自动清理。"""
    tool, wm = _agent_tool(repo, FakeLLM([done("在独立目录完成")]))
    r = await tool.execute(
        _Ctx(), {"prompt": "探索", "description": "d", "subagent_type": "explore",
                 "isolation": "worktree"}
    )
    assert not r.is_error
    assert "已自动清理" in r.content
    assert wm.list() == []  # 无变更已删除


@pytest.mark.asyncio
async def test_worktree_isolation_keeps_on_write(repo: Path) -> None:
    """子 Agent 在 worktree 里制造变更 → 保留 + 路径/分支进返回结果。

    用 Bash（cwd=ctx.work_dir，spec 10 §3.11 explicit cwd 模式）在 worktree 内
    touch 文件：既验证 work_dir 覆盖落到隔离目录，又制造「有变更 → 保留」。
    """
    tool, wm = _agent_tool(
        repo,
        FakeLLM([tool_call("Bash", {"command": "touch new.txt"}), done("改完了")]),
    )
    r = await tool.execute(
        _Ctx(), {"prompt": "改文件", "description": "d", "subagent_type": "general-purpose",
                 "isolation": "worktree"}
    )
    assert not r.is_error
    assert "Worktree 保留于" in r.content
    assert len(wm.list()) == 1  # 保留供 review
    wt = wm.list()[0]
    assert (Path(wt.path) / "new.txt").exists()  # Bash cwd 落在 worktree（非主目录）
    assert not (repo / "new.txt").exists()  # 主目录不受影响（隔离生效）
    assert wt.branch in r.content


@pytest.mark.asyncio
async def test_worktree_isolation_background_rejected(repo: Path) -> None:
    tool, _ = _agent_tool(repo, FakeLLM([done("x")]))
    r = await tool.execute(
        _Ctx(), {"prompt": "p", "description": "d", "subagent_type": "explore",
                 "isolation": "worktree", "run_in_background": True}
    )
    assert r.is_error
    assert "M5-c" in r.content


@pytest.mark.asyncio
async def test_worktree_isolation_unwired(repo: Path) -> None:
    """WorktreeManager 未接线时明确报错，不静默降级共享目录。"""
    manager = AgentManager([_BUILTIN])
    manager.scan()
    runner = SubAgentRunner(
        llm=FakeLLM([done("x")]), tools=build_default_registry(), config=Config(), work_dir=repo
    )
    tool = AgentTool(runner, manager, TaskManager(runner), None)
    r = await tool.execute(
        _Ctx(), {"prompt": "p", "description": "d", "subagent_type": "explore",
                 "isolation": "worktree"}
    )
    assert r.is_error
    assert "未接线" in r.content


@pytest.mark.asyncio
async def test_unknown_isolation_falls_back(repo: Path) -> None:
    """未知 isolation 值：安全默认回共享目录（不阻断委派）。"""
    manager = AgentManager([_BUILTIN])
    manager.scan()
    runner = SubAgentRunner(
        llm=FakeLLM([done("共享目录结果")]), tools=build_default_registry(), config=Config(),
        work_dir=repo,
    )
    tool = AgentTool(runner, manager, TaskManager(runner), None)
    r = await tool.execute(
        _Ctx(), {"prompt": "p", "description": "d", "subagent_type": "explore",
                 "isolation": "bogus"}
    )
    assert not r.is_error
    assert "共享目录" in r.content


# ---- runner work_dir 覆盖（Bash cwd 落点，spec 10 §3.11 explicit cwd） ----

@pytest.mark.asyncio
async def test_run_to_completion_work_dir_override(repo: Path) -> None:
    """work_dir 覆盖后，子 Agent 的 Bash 以 worktree 路径为 cwd（非主目录）。"""
    manager = AgentManager([_BUILTIN])
    manager.scan()
    runner = SubAgentRunner(
        llm=FakeLLM([tool_call("Bash", {"command": "touch created.txt"}), done("完成")]),
        tools=build_default_registry(),
        config=Config(),
        work_dir=repo,
    )
    wm = _manager(repo)
    wt = wm.create("agent-abc")
    definition = manager.get("general-purpose")
    assert definition is not None
    result = await runner.run_to_completion(definition, "建文件", work_dir=Path(wt.path))
    assert not result.is_error
    assert (Path(wt.path) / "created.txt").exists()
    assert not (repo / "created.txt").exists()
    wm.remove("agent-abc", force=True)  # 有变更（created.txt），fail-closed 需 force
