"""ReAct Loop 测试（规格 02 §3.4-3.6：流式消费 / 四停止条件 / 断路器）。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

import kdagent.engine.agent as agent_mod
from kdagent.config import Config
from kdagent.engine.agent import Agent
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.events import (
    CancelledEvent,
    ErrorEvent,
    LoopCompleteEvent,
    MaxIterationsReachedEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnCompleteEvent,
    UsageEvent,
)
from kdagent.engine.llm.base import LLMStreamEvent, Payload, ToolTruncatedError, Usage
from kdagent.engine.messages import TextBlock, ToolResultBlock, ToolUseBlock
from kdagent.tools import build_default_registry


class FakeLLM:
    """按顺序弹出预设事件批的假 LLM（async generator，匹配 LLMClient Protocol）。"""

    def __init__(self, responses: list[list[LLMStreamEvent]]) -> None:
        self._responses = responses
        self.call_count = 0

    async def stream_chat(self, payload: Payload) -> AsyncIterator[LLMStreamEvent]:
        self.call_count += 1
        for ev in self._responses.pop(0):
            yield ev


def _make_agent(
    responses: list[list[LLMStreamEvent]], work_dir: Path
) -> tuple[Agent, ConversationManager, list[Any]]:
    collected: list[Any] = []
    conv = ConversationManager()
    agent = Agent(
        config=Config(),
        llm=FakeLLM(responses),
        conversation=conv,
        tools=build_default_registry(),
        events=collected.append,
        work_dir=work_dir,
    )
    return agent, conv, collected


def _done(text: str) -> list[LLMStreamEvent]:
    return [LLMStreamEvent(type="text_delta", text=text), LLMStreamEvent(type="stop", stop_reason="end_turn")]


def _tool(name: str, input: dict[str, Any], id_: str = "t") -> list[LLMStreamEvent]:
    return [
        LLMStreamEvent(type="tool_use", tool_use=ToolUseBlock(id=id_, name=name, input=input)),
        LLMStreamEvent(type="stop", stop_reason="tool_use"),
    ]


async def test_single_turn_terminates(tmp_path: Path) -> None:
    agent, conv, collected = _make_agent([_done("你好")], tmp_path)
    await agent.run("打个招呼")
    assert any(isinstance(e, LoopCompleteEvent) for e in collected)
    assert not any(isinstance(e, TurnCompleteEvent) for e in collected)
    assert [m.role for m in conv.messages] == ["user", "assistant"]
    assert conv.messages[1].content[0].text == "你好"


async def test_tool_call_loop_then_done(tmp_path: Path) -> None:
    responses = [
        _tool("TodoWrite", {"todos": [{"content": "目标", "tasks": []}]}, id_="t1"),
        _done("完成"),
    ]
    agent, conv, collected = _make_agent(responses, tmp_path)
    await agent.run("规划并完成")
    assert any(isinstance(e, ToolUseEvent) for e in collected)
    assert any(isinstance(e, ToolResultEvent) for e in collected)
    assert any(isinstance(e, TurnCompleteEvent) for e in collected)
    assert any(isinstance(e, LoopCompleteEvent) for e in collected)
    # 交替合法 + 铁律 1：tool_result 以 user 身份回传
    assert [m.role for m in conv.messages] == ["user", "assistant", "user", "assistant"]
    assert isinstance(conv.messages[2].content[0], ToolResultBlock)
    assert conv.messages[2].content[0].tool_use_id == "t1"


async def test_unknown_tool_error_result_does_not_stop_loop(tmp_path: Path) -> None:
    responses = [
        _tool("NoSuchTool", {}, id_="x"),
        _done("调整"),
    ]
    agent, conv, collected = _make_agent(responses, tmp_path)
    await agent.run("任务")
    # errorResult 进历史（不终止），模型下轮继续
    result_block = conv.messages[2].content[0]
    assert isinstance(result_block, ToolResultBlock)
    assert result_block.is_error is True
    assert "工具不存在" in result_block.content
    assert [m.role for m in conv.messages] == ["user", "assistant", "user", "assistant"]
    assert any(isinstance(e, LoopCompleteEvent) for e in collected)


async def test_usage_event_emitted(tmp_path: Path) -> None:
    responses = [
        [
            LLMStreamEvent(type="usage", usage=Usage(input_tokens=10, output_tokens=5)),
            LLMStreamEvent(type="stop"),
        ]
    ]
    agent, conv, collected = _make_agent(responses, tmp_path)
    await agent.run("hi")
    usage_events = [e for e in collected if isinstance(e, UsageEvent)]
    assert usage_events
    assert usage_events[0].usage.input_tokens == 10


async def test_max_iterations_reached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(agent_mod, "MAX_ITERATIONS", 3)
    responses = [_tool("TodoWrite", {"todos": [{"content": "x"}]}, id_=f"t{i}") for i in range(3)]
    agent, conv, collected = _make_agent(responses, tmp_path)
    await agent.run("一直循环")
    assert any(isinstance(e, MaxIterationsReachedEvent) for e in collected)
    assert not any(isinstance(e, LoopCompleteEvent) for e in collected)


async def test_cancel_flushes_partial_to_history(tmp_path: Path) -> None:
    class CancelLLM:
        async def stream_chat(self, payload: Payload) -> AsyncIterator[LLMStreamEvent]:
            yield LLMStreamEvent(type="text_delta", text="部分回复")
            raise asyncio.CancelledError()

    collected: list[Any] = []
    conv = ConversationManager()
    agent = Agent(
        config=Config(),
        llm=CancelLLM(),
        conversation=conv,
        tools=build_default_registry(),
        events=collected.append,
        work_dir=tmp_path,
    )
    await agent.run("开始")
    assert any(isinstance(e, CancelledEvent) for e in collected)
    # 已收部分落成一条 assistant 消息，不碎不丢（规格 02 §3.6）
    assert conv.messages[-1].role == "assistant"
    assert conv.messages[-1].content[0].text == "部分回复"


async def test_circuit_breaker_injects_reminder(tmp_path: Path) -> None:
    responses = [_tool("Bash", {"command": "exit 1"}, id_=f"b{i}") for i in range(3)]
    responses.append(_done("搞定"))
    agent, conv, collected = _make_agent(responses, tmp_path)
    await agent.run("跑命令")
    texts = [b.text for m in conv.messages for b in m.content if isinstance(b, TextBlock)]
    assert any("system-reminder" in t for t in texts)


async def test_provider_error_terminates_with_error_event(tmp_path: Path) -> None:
    class FailLLM:
        async def stream_chat(self, payload: Payload) -> AsyncIterator[LLMStreamEvent]:
            yield LLMStreamEvent(type="error", error=RuntimeError("连接失败"))

    collected: list[Any] = []
    conv = ConversationManager()
    agent = Agent(
        config=Config(),
        llm=FailLLM(),
        conversation=conv,
        tools=build_default_registry(),
        events=collected.append,
        work_dir=tmp_path,
    )
    await agent.run("hi")
    error_events = [e for e in collected if isinstance(e, ErrorEvent)]
    assert error_events and "连接失败" in error_events[0].error


# ---- B2：空回复截断不再静默（2026-08-28 21da 会话「没报错但也没反应了」根因） ----

def _truncated_empty() -> list[LLMStreamEvent]:
    """只含 usage+stop、无文本无工具、输出打满 max_tokens 的空回复流。"""
    return [
        LLMStreamEvent(type="usage", usage=Usage(input_tokens=100, output_tokens=100000)),
        LLMStreamEvent(type="stop", stop_reason="length"),
    ]


async def test_empty_reply_at_max_tokens_retries_then_succeeds(tmp_path: Path) -> None:
    """空回复 + 输出打满 max_tokens → 注入「别过度思考」反馈并重试，第二次正常完成。"""
    responses = [_truncated_empty(), _done("好的，直接做")]
    agent, conv, collected = _make_agent(responses, tmp_path)
    await agent.run("全部都做")
    assert any(isinstance(e, LoopCompleteEvent) for e in collected)
    assert not any(isinstance(e, ErrorEvent) for e in collected)
    # 反馈消息注入（空回复截断引导，非工具参数截断引导）
    texts = [b.text for m in conv.messages for b in m.content if isinstance(b, TextBlock)]
    assert any("max_tokens" in t and "思考" in t for t in texts)
    assert conv.messages[-1].role == "assistant"
    assert conv.messages[-1].content[0].text == "好的，直接做"


async def test_empty_reply_at_max_tokens_exhausts_retries_errors(tmp_path: Path) -> None:
    """连续 2 次空回复截断 → ErrorEvent 终止（有报错，不再静默）。"""
    responses = [_truncated_empty(), _truncated_empty()]
    agent, conv, collected = _make_agent(responses, tmp_path)
    await agent.run("全部都做")
    error_events = [e for e in collected if isinstance(e, ErrorEvent)]
    assert error_events and "截断" in error_events[0].error
    assert not any(isinstance(e, LoopCompleteEvent) for e in collected)


async def test_parser_empty_truncated_error_uses_thinking_feedback(tmp_path: Path) -> None:
    """parser 抛 empty=True 截断错误事件 → 反馈用「别过度思考」而非「拆小输出」。"""
    responses = [
        [LLMStreamEvent(type="error", error=ToolTruncatedError("空回复截断", empty=True))],
        _done("好的，直接做"),
    ]
    agent, conv, collected = _make_agent(responses, tmp_path)
    await agent.run("全部都做")
    assert any(isinstance(e, LoopCompleteEvent) for e in collected)
    texts = [b.text for m in conv.messages for b in m.content if isinstance(b, TextBlock)]
    assert any("思考" in t for t in texts)  # empty 引导
    assert not any("拆小输出" in t for t in texts)  # 非工具参数引导


# ---- B 953e：/session new 重载配置——set_config 换引用后 max_tokens 惰性生效 ----


def test_set_config_reload_propagates_max_tokens(tmp_path: Path) -> None:
    """/session new 重载配置：Agent 换 Config 引用后，payload 用新 max_tokens。

    953e 实测：进程顶格旧 max_tokens=4096，写大文件 WriteFile 参数被截断、JSON
    解析失败、任务终止。`_assemble_payload` 组装时现读 `self._config.extra`，
    换引用即生效（不必重启进程）。
    """
    agent, _conv, _collected = _make_agent([_done("ok")], tmp_path)
    assert agent._assemble_payload().max_tokens == 100000  # 默认
    agent.set_config(Config(extra={"max_tokens": 100000}))
    assert agent._assemble_payload().max_tokens == 100000  # 重载后惰性生效
