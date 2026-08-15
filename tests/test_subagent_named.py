"""命名 Agent + SendMessage 消息投递（规格 10 §3.15，M5-d）。"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import FakeLLM, done

from kdagent.config import Config
from kdagent.engine.conversation import ConversationManager
from kdagent.subagent import BUILTIN_AGENTS_DIR
from kdagent.subagent.manager import AgentManager
from kdagent.subagent.named import NamedAgentError, NamedAgentManager, SendMessage
from kdagent.subagent.runner import SubAgentRunner
from kdagent.subagent.task import TaskManager
from kdagent.subagent.worktree import WorktreeManager
from kdagent.tools import build_default_registry

_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}


def _make_repo(tmp_path: Path) -> Path:
    """初始化一个含初始 commit 的临时 git 仓库（worktree add 需要 HEAD）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init",),
        ("config", "user.email", "test@test.local"),
        ("config", "user.name", "test"),
    ):
        subprocess.run(["git", *args], cwd=repo, env=_GIT_ENV, capture_output=True)
    (repo / ".gitignore").write_text(".kdagent/\n", encoding="utf-8")
    (repo / "README.md").write_text("# t\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, env=_GIT_ENV, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, env=_GIT_ENV, capture_output=True)
    return repo


async def _wait_until(pred: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("超时")


def _manager(tmp_path: Path, llm: FakeLLM) -> NamedAgentManager:
    runner = SubAgentRunner(
        llm=llm,
        tools=build_default_registry(),
        config=Config(),
        work_dir=tmp_path,
    )
    return NamedAgentManager(runner)


def _agent_managers(tmp_path: Path) -> AgentManager:
    manager = AgentManager([BUILTIN_AGENTS_DIR])
    manager.scan()
    return manager


class _Ctx:
    tool_use_id = "n1"


# ---- 注册与初始任务 ----

@pytest.mark.asyncio
async def test_register_runs_initial_then_idle(tmp_path: Path) -> None:
    nm = _manager(tmp_path, FakeLLM([done("探索结果 A")]))
    agent = nm.register(
        _agent_managers(tmp_path).get("explore"),  # type: ignore[arg-type]
        "wander",
        "看看结构",
    )
    await _wait_until(lambda: agent.status == "idle")
    assert "探索结果 A" in agent.result_history
    assert agent.status == "idle"  # 等消息，不销毁
    assert agent.definition.name == "explore"
    assert agent.turns >= 1


# ---- SendMessage 续跑 ----

@pytest.mark.asyncio
async def test_send_message_continues_agent(tmp_path: Path) -> None:
    """SendMessage 唤醒 idle Agent，结果追加进 result_history，轮次累计。"""
    llm = FakeLLM([done("第一轮结果"), done("第二轮结果")])
    nm = _manager(tmp_path, llm)
    definition = _agent_managers(tmp_path).get("explore")
    assert definition is not None
    agent = nm.register(definition, "wander", "任务一")
    await _wait_until(lambda: agent.status == "idle")
    assert "第一轮结果" in agent.result_history

    assert nm.send("wander", "再查一下")
    await _wait_until(lambda: "第二轮结果" in agent.result_history)
    assert agent.status == "idle"
    assert agent.turns >= 2
    assert llm.call_count == 2  # 两次 RunToCompletion，各一轮


@pytest.mark.asyncio
async def test_send_unknown_name_returns_false(tmp_path: Path) -> None:
    nm = _manager(tmp_path, FakeLLM([done("x")]))
    assert not nm.send("nobody", "hi")  # 未注册 → False（工具转 is_error）


# ---- 重名与 name 校验 ----

@pytest.mark.asyncio
async def test_duplicate_name_rejected(tmp_path: Path) -> None:
    nm = _manager(tmp_path, FakeLLM([done("x"), done("y")]))
    definition = _agent_managers(tmp_path).get("explore")
    assert definition is not None
    nm.register(definition, "dup", "任务一")
    with pytest.raises(NamedAgentError, match="已存在"):
        nm.register(definition, "dup", "任务二")


@pytest.mark.asyncio
async def test_bad_name_rejected(tmp_path: Path) -> None:
    """name slug 注入（../../etc / 空 / 超长）被拒——复用 worktree validate_name。"""
    nm = _manager(tmp_path, FakeLLM([done("x")]))
    definition = _agent_managers(tmp_path).get("explore")
    assert definition is not None
    for bad in ("", "a" * 65, "../etc", "a b"):
        with pytest.raises(NamedAgentError):
            nm.register(definition, bad, "t")


# ---- SendMessage 工具 ----

@pytest.mark.asyncio
async def test_send_message_tool(tmp_path: Path) -> None:
    nm = _manager(tmp_path, FakeLLM([done("第一轮"), done("续跑结果")]))
    definition = _agent_managers(tmp_path).get("explore")
    assert definition is not None
    nm.register(definition, "wander", "任务一")
    tool = SendMessage(nm)
    await _wait_until(lambda: nm.get("wander") is not None and nm.get("wander").status == "idle")  # type: ignore[union-attr]

    r = await tool.execute(_Ctx(), {"to": "wander", "message": "再查一下"})
    assert not r.is_error
    assert "已投递给命名 Agent wander" in r.content

    async def _saw_result() -> None:
        def _has() -> bool:
            agent = nm.get("wander")
            return agent is not None and "续跑结果" in agent.result_history
        await _wait_until(_has)

    await _saw_result()


@pytest.mark.asyncio
async def test_send_message_tool_unknown_target(tmp_path: Path) -> None:
    nm = _manager(tmp_path, FakeLLM([done("x")]))
    tool = SendMessage(nm)
    r = await tool.execute(_Ctx(), {"to": "nobody", "message": "hi"})
    assert r.is_error
    assert "命名 Agent 不存在" in r.content


def test_send_message_validation(tmp_path: Path) -> None:
    tool = SendMessage(_manager(tmp_path, FakeLLM([done("x")])))
    assert tool.validate_input({}) == ["to 必填", "message 必填"]
    assert tool.validate_input({"to": "a"}) == ["message 必填"]
    assert tool.validate_input({"to": "a", "message": "b"}) == []


# ---- Fork 命名（继承父对话） ----

@pytest.mark.asyncio
async def test_fork_named_uses_parent_conversation(tmp_path: Path) -> None:
    nm = _manager(tmp_path, FakeLLM([done("继承结果")]))
    parent = ConversationManager()
    parent.add_user_message("主对话内容")
    agent = nm.register(
        _fork_def(),
        "forky",
        "继续我查的资料",
        fork=True,
        parent_conversation=parent,
    )
    await _wait_until(lambda: agent.status == "idle")
    assert "继承结果" in agent.result_history


def _fork_def():
    from kdagent.subagent.model import AgentDef

    return AgentDef(name="fork", description="fork", system_prompt="", max_turns=5)


# ---- Agent 工具 name 参数接线 ----

def _build_agent_tool(tmp_path: Path, llm: FakeLLM):
    from kdagent.subagent.agent_tool import Agent as AgentTool

    manager = _agent_managers(tmp_path)
    runner = SubAgentRunner(
        llm=llm,
        tools=build_default_registry(),
        config=Config(),
        work_dir=tmp_path,
    )
    tm = TaskManager(runner)
    nm = NamedAgentManager(runner)
    wm = WorktreeManager(tmp_path, tmp_path / ".kdagent" / "worktrees")
    tool = AgentTool(runner, manager, tm, wm, nm)
    return tool, nm, wm


@pytest.mark.asyncio
async def test_agent_tool_name_registers(tmp_path: Path) -> None:
    tool, nm, _ = _build_agent_tool(tmp_path, FakeLLM([done("命名探索完成")]))
    r = await tool.execute(
        _Ctx(), {"prompt": "查一下", "description": "d", "name": "wander",
                 "subagent_type": "explore", "run_in_background": True}
    )
    assert not r.is_error
    assert "[命名 Agent wander 已注册并后台启动" in r.content
    agent = nm.get("wander")
    assert agent is not None
    await _wait_until(lambda: agent.status == "idle")
    assert "命名探索完成" in agent.result_history


@pytest.mark.asyncio
async def test_agent_tool_name_duplicate_rejected(tmp_path: Path) -> None:
    tool, _, _ = _build_agent_tool(tmp_path, FakeLLM([done("x"), done("y")]))
    await tool.execute(
        _Ctx(), {"prompt": "p", "description": "d", "name": "dup",
                 "subagent_type": "explore", "run_in_background": True}
    )
    r = await tool.execute(
        _Ctx(), {"prompt": "p", "description": "d", "name": "dup",
                 "subagent_type": "explore", "run_in_background": True}
    )
    assert r.is_error
    assert "已存在" in r.content


@pytest.mark.asyncio
async def test_agent_tool_name_with_worktree_kept(tmp_path: Path) -> None:
    """命名 Agent + worktree：创建 worktree、work_dir 落点、不自动清理。"""
    repo = _make_repo(tmp_path)
    tool, nm, wm = _build_agent_tool(repo, FakeLLM([done("独立完成")]))
    r = await tool.execute(
        _Ctx(), {"prompt": "改文件", "description": "d", "name": "wander",
                 "subagent_type": "general-purpose", "isolation": "worktree",
                 "run_in_background": True}
    )
    assert not r.is_error
    assert "worktree：" in r.content
    assert len(wm.list()) == 1  # worktree 创建且保留（命名 Agent 不自动清理）
    agent = nm.get("wander")
    assert agent is not None
    assert agent.work_dir is not None
    assert Path(agent.work_dir).is_dir()
    await _wait_until(lambda: agent.status == "idle")
    assert len(wm.list()) == 1  # 清理留给 /worktree，不随消息结束回收
