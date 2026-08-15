"""adoptRunning：前台→后台无缝移交（规格 10 §3.7 ②③ / 10 §5 336，D79）。

覆盖：TaskManager.adopt 接管运行中任务（完成/取消终态 + notify）、config 前台超时
阈值、Agent 工具前台超时/主 Agent 取消自动转后台（不杀掉重来）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest
from conftest import FakeLLM, done

from kdagent.config import Config
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import LLMStreamEvent
from kdagent.subagent import BUILTIN_AGENTS_DIR
from kdagent.subagent.agent_tool import Agent as AgentTool
from kdagent.subagent.manager import AgentManager
from kdagent.subagent.model import AgentDef
from kdagent.subagent.runner import SubAgentRunner
from kdagent.subagent.task import TaskManager
from kdagent.tools import build_default_registry

EXPLORE = AgentDef(
    name="explore",
    description="readonly",
    system_prompt="你是 Explore 子 Agent。",
    disallowed_tools=("EditFile", "WriteFile"),
)


class _SlowLLM:
    """慢 LLM：sleep 后返回完成——制造「运行中」可观测窗口。"""

    def __init__(self, text: str = "慢结果", delay: float = 0.5) -> None:
        self._text = text
        self._delay = delay

    async def stream_chat(self, payload) -> AsyncIterator[LLMStreamEvent]:
        await asyncio.sleep(self._delay)
        for ev in done(self._text):
            yield ev


class _Ctx:
    tool_use_id = "a1"


def _runner(tmp_path, llm) -> SubAgentRunner:
    return SubAgentRunner(
        llm=llm,  # type: ignore[arg-type]
        tools=build_default_registry(),
        config=Config(),
        work_dir=tmp_path,
    )


def _tool(tmp_path, *, auto_background_ms: int = 120_000) -> tuple[AgentTool, TaskManager]:
    manager = AgentManager([BUILTIN_AGENTS_DIR])
    manager.scan()
    runner = _runner(tmp_path, _SlowLLM(delay=0.2))
    tm = TaskManager(runner, auto_background_ms=auto_background_ms)
    tool = AgentTool(runner, manager, tm)
    return tool, tm


async def _wait_until(pred: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("超时未满足条件")


# ---- config：前台超时阈值（10 §3.7 ②） ----------------------------------------


def test_config_auto_background_ms_default_and_custom() -> None:
    assert Config().get_auto_background_ms() == 120_000
    assert Config(agents={"auto_background_ms": 5000}).get_auto_background_ms() == 5000
    # 非法值回退默认（零配置可用）
    assert Config(agents={"auto_background_ms": "bad"}).get_auto_background_ms() == 120_000


# ---- TaskManager.adopt：接管运行中任务（10 §3.7 adoptRunning） ------------------


@pytest.mark.asyncio
async def test_adopt_running_task_completes_and_notifies(tmp_path) -> None:
    parent = ConversationManager()
    parent.add_user_message("主对话内容")
    runner = _runner(tmp_path, FakeLLM([done("后台探索完成")]))
    mgr = TaskManager(runner, parent_conversation=parent)
    fg = asyncio.create_task(runner.run_to_completion(EXPLORE, "任务"))
    await asyncio.sleep(0.01)  # yield：让 run_to_completion 真正启动

    bt = mgr.adopt(fg, EXPLORE, "任务")
    assert bt.id == "task-1"
    assert bt.status == "running"  # 接管即视为运行中（不杀掉重来）
    assert mgr.get("task-1") is bt
    assert callable(bt.cancel)

    await _wait_until(lambda: bt.status in ("completed", "failed"))
    assert bt.status == "completed"
    assert bt.result == "后台探索完成"
    assert bt.turns == 1
    text = "".join(
        b.text for m in parent.messages for b in m.content if hasattr(b, "text")
    )
    assert "task-notification" in text  # 完成通知注入主对话


@pytest.mark.asyncio
async def test_adopt_cancelled_task_finalizes_failed(tmp_path) -> None:
    runner = _runner(tmp_path, _SlowLLM(delay=30))
    mgr = TaskManager(runner)
    fg = asyncio.create_task(runner.run_to_completion(EXPLORE, "任务"))
    await asyncio.sleep(0.01)

    bt = mgr.adopt(fg, EXPLORE, "任务")
    bt.cancel()  # 取消函数接管运行中任务（不泄漏）
    await _wait_until(lambda: bt.status in ("completed", "failed"))
    assert bt.status == "failed"
    assert bt.result == "（任务被取消）"
    assert bt.is_error


@pytest.mark.asyncio
async def test_adopt_task_exception_finalizes_failed(tmp_path) -> None:
    class _Boom:
        async def stream_chat(self, payload):
            raise RuntimeError("provider 挂了")
            yield  # pragma: no cover

    runner = _runner(tmp_path, _Boom())
    mgr = TaskManager(runner)
    fg = asyncio.create_task(runner.run_to_completion(EXPLORE, "任务"))
    await asyncio.sleep(0.01)

    bt = mgr.adopt(fg, EXPLORE, "任务")
    await _wait_until(lambda: bt.status in ("completed", "failed"))
    assert bt.status == "failed"
    assert "provider 挂了" in bt.result


# ---- Agent 工具前台路径：超时 / 主 Agent 取消自动转后台 ------------------------


@pytest.mark.asyncio
async def test_foreground_timeout_adopts_to_background(tmp_path) -> None:
    """② 前台超时（auto_background_ms）：子 Agent 不杀掉，转后台继续，返回 task id。"""
    tool, tm = _tool(tmp_path, auto_background_ms=50)  # 50ms 超时，_SlowLLM delay 0.2s
    r = await tool.execute(
        _Ctx(), {"prompt": "p", "description": "d", "subagent_type": "explore"}
    )
    assert "已后台启动" in r.content
    assert "task-1" in r.content
    task = tm.get("task-1")
    assert task is not None
    assert task.status == "running"  # 移交时仍运行中（无损）

    await _wait_until(lambda: task.status in ("completed", "failed"))
    assert task.status == "completed"
    assert "慢结果" in task.result  # 部分结果完整保留


@pytest.mark.asyncio
async def test_foreground_fast_returns_directly(tmp_path) -> None:
    """未超时：前台正常返回结果（adopt 不介入）。"""
    manager = AgentManager([BUILTIN_AGENTS_DIR])
    manager.scan()
    runner = _runner(tmp_path, FakeLLM([done("快结论")]))
    tm = TaskManager(runner, auto_background_ms=10_000)
    tool = AgentTool(runner, manager, tm)
    r = await tool.execute(
        _Ctx(), {"prompt": "p", "description": "d", "subagent_type": "explore"}
    )
    assert not r.is_error
    assert "快结论" in r.content
    assert "已后台启动" not in r.content
    assert tm.list() == []  # 无后台任务产生


@pytest.mark.asyncio
async def test_foreground_cancel_adopts_to_background(tmp_path) -> None:
    """③ 主 Agent 取消（用户 Esc）：子 Agent 不杀掉，adopt 转后台继续。"""
    tool, tm = _tool(tmp_path, auto_background_ms=10_000)  # 大超时，靠取消触发
    caller = asyncio.create_task(
        tool.execute(
            _Ctx(), {"prompt": "p", "description": "d", "subagent_type": "explore"}
        )
    )
    await asyncio.sleep(0.05)  # 让 execute 进入 asyncio.wait
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    task = tm.get("task-1")
    assert task is not None  # 取消即转后台接管
    await _wait_until(lambda: task.status in ("completed", "failed"))
    assert task.status == "completed"
    assert "慢结果" in task.result
