"""Agent 工具：Agent≈Tool 统一入口（规格 10 §3.1-3.2）。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from conftest import FakeLLM, done

from kdagent.config import Config
from kdagent.engine.conversation import ConversationManager
from kdagent.subagent.agent_tool import Agent as AgentTool
from kdagent.subagent.manager import AgentManager
from kdagent.subagent.runner import SubAgentRunner
from kdagent.subagent.task import TaskManager
from kdagent.tools import build_default_registry


def _build(tmp_path) -> tuple[AgentTool, TaskManager]:
    manager = AgentManager([_builtin()])
    manager.scan()
    runner = SubAgentRunner(
        llm=FakeLLM([done("探索结论")]),
        tools=build_default_registry(),
        config=Config(),
        work_dir=tmp_path,
    )
    tm = TaskManager(runner)
    tool = AgentTool(runner, manager, tm)
    return tool, tm


def _builtin():
    from kdagent.subagent import BUILTIN_AGENTS_DIR

    return BUILTIN_AGENTS_DIR


class _Ctx:
    tool_use_id = "a1"


async def _wait_until(pred: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("超时")


def test_validate_input(tmp_path) -> None:
    tool, _ = _build(tmp_path)
    assert tool.validate_input({}) == ["prompt 必填且非空", "description 必填且非空"]
    assert tool.validate_input({"prompt": "p", "description": "d"}) == []


@pytest.mark.asyncio
async def test_unknown_type_returns_error(tmp_path) -> None:
    tool, _ = _build(tmp_path)
    r = await tool.execute(_Ctx(), {"prompt": "p", "description": "d", "subagent_type": "nope"})
    assert r.is_error
    assert "未知 Agent 类型" in r.content
    assert "explore" in r.content  # 类型清单提示


@pytest.mark.asyncio
async def test_foreground_defined_returns_result(tmp_path) -> None:
    tool, _ = _build(tmp_path)
    r = await tool.execute(
        _Ctx(), {"prompt": "探索结构", "description": "找入口", "subagent_type": "explore"}
    )
    assert not r.is_error
    assert "探索结论" in r.content
    assert "[Agent explore 完成" in r.content


@pytest.mark.asyncio
async def test_background_returns_task_id(tmp_path) -> None:
    tool, tm = _build(tmp_path)
    r = await tool.execute(
        _Ctx(),
        {"prompt": "探索", "description": "d", "subagent_type": "explore", "run_in_background": True},
    )
    assert "task-1" in r.content
    assert "已后台启动" in r.content
    task = tm.get("task-1")
    assert task is not None
    await _wait_until(lambda: task.status in ("completed", "failed"))
    assert task.status == "completed"
    assert task.result == "探索结论"


@pytest.mark.asyncio
async def test_fork_unbound_returns_error(tmp_path) -> None:
    tool, _ = _build(tmp_path)
    r = await tool.execute(_Ctx(), {"prompt": "子任务", "description": "d"})
    assert r.is_error
    assert "父对话未接线" in r.content


@pytest.mark.asyncio
async def test_fork_bound_launches_background(tmp_path) -> None:
    tool, tm = _build(tmp_path)
    parent = ConversationManager()
    parent.add_user_message("主任务")
    tool.set_parent_conversation(parent)
    r = await tool.execute(_Ctx(), {"prompt": "继续做子任务", "description": "d"})
    assert not r.is_error
    assert "task-1" in r.content  # Fork 无条件后台
    task = tm.get("task-1")
    assert task is not None
    assert task.definition.name == "fork"
    await _wait_until(lambda: task.status in ("completed", "failed"))
    assert task.status == "completed"
    # 通知注入父对话
    text = "".join(
        b.text for m in parent.messages for b in m.content if hasattr(b, "text")
    )
    assert "task-notification" in text
