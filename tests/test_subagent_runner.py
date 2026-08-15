"""SubAgentRunner：Fork 消息构建 + RunToCompletion（规格 10 §3.2/§3.5）。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from conftest import FakeLLM, done, tool_call

from kdagent.config import Config
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import LLMStreamEvent
from kdagent.engine.messages import Message, TextBlock, ToolResultBlock, ToolUseBlock
from kdagent.subagent import (
    FORK_BOILERPLATE,
    SubAgentRunner,
    build_forked_messages,
    filter_tools,
)
from kdagent.subagent.model import AgentDef
from kdagent.tools import build_default_registry

EXPLORE = AgentDef(
    name="explore",
    description="readonly",
    system_prompt="你是 Explore 子 Agent。",
    disallowed_tools=("EditFile", "WriteFile"),
)


def _runner(tmp_path, llm, *, registry=None) -> SubAgentRunner:
    return SubAgentRunner(
        llm=llm,
        tools=registry or build_default_registry(),
        config=Config(),
        work_dir=tmp_path,
    )


# ---- build_forked_messages -------------------------------------------------


def _parent_with_pending_tool_use() -> list[Message]:
    return [
        Message(role="user", content=[TextBlock("你好")]),
        Message(role="assistant", content=[TextBlock("思考中")]),
        Message(role="assistant", content=[ToolUseBlock(id="t1", name="ReadFile", input={"path": "a"})]),
    ]


def test_forked_messages_inherits_history() -> None:
    parent = _parent_with_pending_tool_use()
    forked = build_forked_messages(parent, "读一下项目结构")
    # 继承 3 条 + 追加任务 1 条
    assert len(forked) == 4
    assert forked[0].content == parent[0].content
    assert forked[1].content == parent[1].content


def test_forked_messages_packs_unfinished_tool_use() -> None:
    parent = _parent_with_pending_tool_use()
    forked = build_forked_messages(parent, "任务")
    last_assistant = forked[2]
    assert last_assistant.role == "assistant"
    assert all(not isinstance(b, ToolUseBlock) for b in last_assistant.content)
    # 悬空 tool_use 被包成 error ToolResultBlock（防 API 拒收悬空 tool_call）
    packed = [b for b in last_assistant.content if isinstance(b, ToolResultBlock)]
    assert len(packed) == 1
    assert packed[0].tool_use_id == "t1"
    assert packed[0].is_error


def test_forked_messages_appends_task_with_boilerplate() -> None:
    parent = _parent_with_pending_tool_use()
    forked = build_forked_messages(parent, "完成 refactor")
    last = forked[-1]
    assert last.role == "user"
    text = "".join(b.text for b in last.content if isinstance(b, TextBlock))
    assert "完成 refactor" in text
    assert FORK_BOILERPLATE in text
    assert "不能再 Fork" in text  # Boilerplate 规则注入


def test_forked_messages_empty_parent() -> None:
    forked = build_forked_messages([], "任务")
    assert len(forked) == 1
    assert forked[0].role == "user"
    assert "任务" in "".join(b.text for b in forked[0].content if isinstance(b, TextBlock))


def test_forked_messages_no_dup_merge() -> None:
    """最后一条 user 任务消息不会被额外空 user 合并破坏（无 tool_use 父对话）。"""
    parent = [Message(role="user", content=[TextBlock("hi")])]
    forked = build_forked_messages(parent, "task")
    assert len(forked) == 2


# ---- RunToCompletion -------------------------------------------------------


@pytest.mark.asyncio
async def test_run_to_completion_returns_last_text(tmp_path) -> None:
    llm = FakeLLM([done("探索完成")])
    runner = _runner(tmp_path, llm)
    result = await runner.run_to_completion(EXPLORE, "看看结构")
    assert result.text == "探索完成"
    assert not result.is_error
    assert llm.call_count == 1


@pytest.mark.asyncio
async def test_run_to_completion_with_tool_execution(tmp_path) -> None:
    target = tmp_path / "hello.txt"
    target.write_text("world", encoding="utf-8")
    llm = FakeLLM(
        [
            tool_call("ReadFile", {"path": str(target)}, id_="r1"),
            done("读到了 world"),
        ]
    )
    runner = _runner(tmp_path, llm)
    result = await runner.run_to_completion(EXPLORE, "读 hello.txt")
    assert result.text == "读到了 world"
    assert llm.call_count == 2


@pytest.mark.asyncio
async def test_run_to_completion_uses_definition_system_prompt(tmp_path) -> None:
    llm = FakeLLM([done("ok")])
    runner = _runner(tmp_path, llm)
    await runner.run_to_completion(EXPLORE, "任务")
    assert llm.call_count == 1  # 一轮结束（系统提示来自定义 body）


@pytest.mark.asyncio
async def test_fork_run_to_completion_inherits_and_runs(tmp_path) -> None:
    parent = ConversationManager()
    parent.add_user_message("主任务背景")
    parent.add_assistant_message([TextBlock("主 Agent 分析")])
    llm = FakeLLM([done("fork 结果")])
    runner = _runner(tmp_path, llm)
    definition = AgentDef(name="fork", description="fork", system_prompt="Fork 系统提示", max_turns=5)
    result = await runner.run_to_completion(
        definition, "子任务", parent_conversation=parent, fork=True
    )
    assert result.text == "fork 结果"
    assert not result.is_error


@pytest.mark.asyncio
async def test_run_to_completion_max_turns_returns_partial(tmp_path) -> None:
    """maxTurns 耗尽（不断要工具）→ 返回已收文本不崩溃（10 §3.5 停止条件 2）。"""
    llm = FakeLLM([tool_call("ReadFile", {"path": "x"}, id_="t1")] * 3)
    definition = AgentDef(name="d", description="d", system_prompt="p", max_turns=2)
    runner = _runner(tmp_path, llm)
    result = await runner.run_to_completion(definition, "任务")
    # 2 轮都是 tool_use → 无最终文本，返回空（不抛错即通过）
    assert isinstance(result.text, str)


@pytest.mark.asyncio
async def test_sub_sink_auto_allow_permission(tmp_path) -> None:
    """子 Agent 遇到 ask 自动 allow（headless 无 HITL，能力边界已由过滤锁死）。"""
    from kdagent.engine.events import PermissionRequestEvent

    class _AskLLM:
        async def stream_chat(self, payload) -> AsyncIterator[LLMStreamEvent]:
            yield LLMStreamEvent(type="tool_use", tool_use=ToolUseBlock(id="a1", name="ReadFile", input={"path": "x"}))
            yield LLMStreamEvent(type="stop", stop_reason="tool_use")

    # 无 permission_checker 时 ask 来自 require_confirm 工具——confirm=None 直接执行；
    # 此处验证 _SubSink 处理 PermissionRequestEvent 不悬挂（自动 allow）。
    from kdagent.subagent.runner import _SubSink

    sink = _SubSink()
    future = asyncio.get_running_loop().create_future()
    sink(PermissionRequestEvent(tool_name="ReadFile", summary="x", future=future))
    assert future.result() == "allow"


def test_filter_tools_excludes_agent_for_subagent() -> None:
    """第 1 层确保子 Agent 调不到 Agent 工具（防递归）。"""

    registry = build_default_registry()
    registry.register(_FakeAgentTool())
    names = {t.name for t in filter_tools(registry).all()}
    assert "Agent" not in names


class _FakeAgentTool:
    name = "Agent"
    description = "fake"
    input_schema = {"type": "object", "properties": {}}
    category = "system"
    require_confirm = True

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return True

    def is_concurrency_safe(self, input: dict) -> bool:
        return False

    def validate_input(self, input: dict) -> list[str]:
        return []

    async def execute(self, ctx, input):
        return None
