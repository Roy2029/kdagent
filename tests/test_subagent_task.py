"""TaskManager：后台任务生命周期 + Task 工具（规格 10 §3.7）。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from conftest import FakeLLM, done

from kdagent.config import Config
from kdagent.engine.conversation import ConversationManager
from kdagent.subagent import BUILTIN_AGENTS_DIR
from kdagent.subagent.manager import AgentManager
from kdagent.subagent.model import AgentDef
from kdagent.subagent.runner import SubAgentRunner
from kdagent.subagent.task import (
    TaskCreate,
    TaskGet,
    TaskList,
    TaskManager,
    TaskUpdate,
)
from kdagent.tools import build_default_registry

EXPLORE = AgentDef(
    name="explore",
    description="readonly",
    system_prompt="你是 Explore 子 Agent。",
    disallowed_tools=("EditFile", "WriteFile"),
)


def _manager(tmp_path, *, parent: ConversationManager | None = None) -> TaskManager:
    runner = SubAgentRunner(
        llm=FakeLLM([done("后台探索完成")]),
        tools=build_default_registry(),
        config=Config(),
        work_dir=tmp_path,
    )
    mgr = TaskManager(runner, parent_conversation=parent)
    return mgr


async def _wait_until(pred: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("后台任务未在超时内完成")


@pytest.mark.asyncio
async def test_launch_runs_background_and_completes(tmp_path) -> None:
    mgr = _manager(tmp_path)
    task = mgr.launch(EXPLORE, "探索项目结构")
    assert task.status == "running"  # 立即返回，后台执行
    assert task.id == "task-1"
    await _wait_until(lambda: task.status in ("completed", "failed"))
    assert task.status == "completed"
    assert task.result == "后台探索完成"
    assert task.turns == 1
    assert task.duration_s >= 0


@pytest.mark.asyncio
async def test_launch_failed_on_exception(tmp_path) -> None:

    class _Boom:
        async def stream_chat(self, payload):
            raise RuntimeError("provider 挂了")
            yield  # pragma: no cover

    runner = SubAgentRunner(
        llm=_Boom(),  # type: ignore[arg-type]
        tools=build_default_registry(),
        config=Config(),
        work_dir=tmp_path,
    )
    mgr = TaskManager(runner)
    task = mgr.launch(EXPLORE, "任务")
    await _wait_until(lambda: task.status in ("completed", "failed"))
    assert task.status == "failed"
    assert "provider 挂了" in task.result  # 错误信息透传（sink.error）


@pytest.mark.asyncio
async def test_completion_notifies_parent_conversation(tmp_path) -> None:
    parent = ConversationManager()
    parent.add_user_message("主对话内容")
    mgr = _manager(tmp_path, parent=parent)
    task = mgr.launch(EXPLORE, "任务")
    await _wait_until(lambda: task.status in ("completed", "failed"))
    text = "".join(
        b.text
        for m in parent.messages
        for b in m.content
        if hasattr(b, "text")
    )
    assert "task-notification" in text
    assert "后台探索完成" in text


@pytest.mark.asyncio
async def test_task_list_and_get(tmp_path) -> None:
    mgr = _manager(tmp_path)
    list_tool = TaskList(mgr)
    get_tool = TaskGet(mgr)
    ctx = _Ctx()

    # 空列表
    r = await list_tool.execute(ctx, {})
    assert "无后台任务" in r.content

    task = mgr.launch(EXPLORE, "任务")
    await _wait_until(lambda: task.status in ("completed", "failed"))
    r = await list_tool.execute(ctx, {})
    assert "task-1" in r.content
    assert "[completed]" in r.content

    r = await get_tool.execute(ctx, {"id": "task-1"})
    assert "后台探索完成" in r.content

    r = await get_tool.execute(ctx, {"id": "task-999"})
    assert r.is_error
    assert "不存在" in r.content


@pytest.mark.asyncio
async def test_task_create_and_update(tmp_path) -> None:
    """TaskCreate 登记外部任务 + TaskUpdate 回填（Hook 用，10 §3.7）。"""
    mgr = _manager(tmp_path)
    create_tool = TaskCreate(mgr)
    update_tool = TaskUpdate(mgr)
    ctx = _Ctx()

    r = await create_tool.execute(ctx, {"type": "build", "task": "跑构建"})
    assert "task-1" in r.content
    task = mgr.get("task-1")
    assert task is not None
    assert task.status == "running"

    r = await update_tool.execute(ctx, {"id": "task-1", "status": "completed", "result": "构建通过"})
    assert "已更新" in r.content
    assert task.status == "completed"
    assert task.result == "构建通过"

    r = await update_tool.execute(ctx, {"id": "task-999", "status": "completed"})
    assert r.is_error


@pytest.mark.asyncio
async def test_task_create_uses_registered_definition(tmp_path) -> None:
    """TaskCreate type 匹配已注册 Agent → 用其真实定义（M5-c）。"""
    manager = AgentManager([BUILTIN_AGENTS_DIR])
    manager.scan()
    mgr = _manager(tmp_path)
    create_tool = TaskCreate(mgr, agent_manager=manager)
    r = await create_tool.execute(_Ctx(), {"type": "explore", "task": "探索"})
    assert "task-1" in r.content
    task = mgr.get("task-1")
    assert task is not None
    assert task.definition.name == "explore"
    assert task.definition.system_prompt != ""  # 真实定义（非空占位）


@pytest.mark.asyncio
async def test_task_create_unknown_type_falls_back(tmp_path) -> None:
    """TaskCreate type 未匹配 → 通用外部任务条目占位定义。"""
    manager = AgentManager([BUILTIN_AGENTS_DIR])
    manager.scan()
    mgr = _manager(tmp_path)
    create_tool = TaskCreate(mgr, agent_manager=manager)
    await create_tool.execute(_Ctx(), {"type": "build", "task": "跑构建"})
    task = mgr.get("task-1")
    assert task is not None
    assert task.definition.name == "build"
    assert task.definition.system_prompt == ""  # 占位


@pytest.mark.asyncio
async def test_launch_on_complete_called(tmp_path) -> None:
    """on_complete 钩子在任务终态（完成/失败/取消）都会触发（M5-c）。"""
    mgr = _manager(tmp_path)
    called: list[str] = []

    def _hook(bt) -> None:
        called.append(bt.status)

    task = mgr.launch(EXPLORE, "任务", on_complete=_hook)
    await _wait_until(lambda: task.status in ("completed", "failed"))
    assert called == ["completed"]


@pytest.mark.asyncio
async def test_launch_on_complete_called_on_cancel(tmp_path) -> None:
    """取消路径：on_complete 依然触发（终态钩子统一在 finally）。"""
    mgr = _manager(tmp_path)
    called: list[str] = []

    def _hook(bt) -> None:
        called.append(bt.status)

    task = mgr.launch(EXPLORE, "任务", on_complete=_hook)
    task.cancel()
    await _wait_until(lambda: task.status in ("completed", "failed"))
    assert called == ["failed"]
    assert task.is_error


@pytest.mark.asyncio
async def test_task_validation(tmp_path) -> None:
    mgr = _manager(tmp_path)
    get_tool = TaskGet(mgr)
    create_tool = TaskCreate(mgr)
    update_tool = TaskUpdate(mgr)
    assert get_tool.validate_input({}) == ["id 必填"]
    assert create_tool.validate_input({}) == ["type 必填", "task 必填"]
    assert update_tool.validate_input({"id": "x"}) == ["status 必填且为 completed/failed"]
    assert update_tool.validate_input({"id": "x", "status": "running"}) == [
        "status 必填且为 completed/failed"
    ]
    assert create_tool.validate_input({"type": "t", "task": "d"}) == []


@pytest.mark.asyncio
async def test_set_telemetry_forwards_to_runner(tmp_path) -> None:
    """10 §5 342（D78）：TaskManager.set_telemetry 转发 runner（装配后注入）。"""
    from kdagent.obs.telemetry import Telemetry

    runner = SubAgentRunner(
        llm=FakeLLM([done("x")]),
        tools=build_default_registry(),
        config=Config(),
        work_dir=tmp_path,
    )
    mgr = TaskManager(runner)
    assert runner._telemetry is None  # 初始无 telemetry（cli 构造早于 KDApp）
    telemetry = Telemetry(tmp_path / "obs")
    mgr.set_telemetry(telemetry)
    assert runner._telemetry is telemetry  # 转发生效


@pytest.mark.asyncio
async def test_background_task_trace_links_to_parent(tmp_path) -> None:
    """10 §5 342（D78）：后台任务 create_task 快照父上下文 → 子 Agent trace 挂父。"""
    import json

    from kdagent.obs.telemetry import Telemetry

    obs_dir = tmp_path / "obs"
    telemetry = Telemetry(obs_dir)
    telemetry.begin_trace("main", "父输入")
    with telemetry.span("trace.run", "session"):
        parent_trace_id = telemetry.current_context()[0]
        mgr = _manager(tmp_path)
        mgr.set_telemetry(telemetry)
        task = mgr.launch(EXPLORE, "后台任务")
        await _wait_until(lambda: task.status in ("completed", "failed"))
        assert task.status == "completed"
    telemetry.end_trace()

    headers: list[dict[str, object]] = []
    for f in (obs_dir / "traces").glob("**/*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["_type"] == "trace":
                headers.append(row)
    assert len(headers) == 2  # 父 + 后台子
    child = next(h for h in headers if h["parent_trace_id"] == parent_trace_id)
    assert child["trace_id"] != parent_trace_id


class _Ctx:
    """最小 ToolContext（仅 tool_use_id 有用）。"""

    tool_use_id = "t1"
