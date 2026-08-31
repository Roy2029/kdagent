"""LLM 瞬态重试链测试（02 §3.9，v052-review-remediation）。

覆盖：429 恢复继续当前轮（重试不推进轮次）、连续瞬态失败超上限 TERMINAL、
401 认证错误不重试立即终止、重试事件可见、SSE 注释/空行/非 JSON 行跳过、
Anthropic prompt too long 映射、is_transient_llm_error 分类边界。
"""

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
from kdagent.engine.events import ErrorEvent
from kdagent.engine.llm.anthropic import _raise_prompt_too_long
from kdagent.engine.llm.base import (
    LLMStreamEvent,
    Payload,
    PromptTooLongError,
    ToolTruncatedError,
    is_transient_llm_error,
)
from kdagent.engine.llm.openai import _OpenAIStreamParser
from kdagent.tools import build_default_registry


class FlakyLLM:
    """前 N 次调用抛指定异常，之后按队列弹事件（模拟瞬态故障恢复）。"""

    def __init__(
        self, failures: list[Exception], responses: list[list[LLMStreamEvent]]
    ) -> None:
        self._failures = list(failures)
        self._responses = responses
        self.call_count = 0

    async def stream_chat(self, payload: Payload) -> AsyncIterator[LLMStreamEvent]:
        self.call_count += 1
        if self._failures:
            raise self._failures.pop(0)
        for ev in self._responses.pop(0):
            yield ev


def _done(text: str) -> list[LLMStreamEvent]:
    return [
        LLMStreamEvent(type="text_delta", text=text),
        LLMStreamEvent(type="stop", stop_reason="end_turn"),
    ]


def _make_agent(
    llm: Any, work_dir: Path
) -> tuple[Agent, ConversationManager, list[Any]]:
    collected: list[Any] = []
    conv = ConversationManager()
    agent = Agent(
        config=Config(),
        llm=llm,
        conversation=conv,
        tools=build_default_registry(),
        events=collected.append,
        work_dir=work_dir,
    )
    return agent, conv, collected


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """退避休眠置零（测试不真实等待 1s/2s/4s）。"""

    async def _instant(_seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(agent_mod, "_retry_sleep", _instant)


# ---- agent 层：瞬态重试 ------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_429_recovers(tmp_path: Path) -> None:
    """429 一次后成功 → 继续当前轮正常收尾，事件含「正在重试 1/3」。"""
    llm = FlakyLLM([RuntimeError("429 Too Many Requests, please retry later")], [_done("ok")])
    agent, _conv, events = _make_agent(llm, tmp_path)
    status = await agent.run("hi")
    assert status == "CONTINUE" or agent._stop_reason != "error"
    assert llm.call_count == 2
    retry_events = [e for e in events if isinstance(e, ErrorEvent) and "正在重试" in e.error]
    assert len(retry_events) == 1
    assert "1/3" in retry_events[0].error


@pytest.mark.asyncio
async def test_retry_then_tool_call_completes(tmp_path: Path) -> None:
    """重试后接工具调用轮：循环状态干净（pending 缓冲已重置），正常走完。"""
    from conftest import done as _done_events
    from conftest import tool_call

    llm = FlakyLLM(
        [ConnectionResetError("connection reset by peer")],
        [
            tool_call("ReadFile", {"path": str(tmp_path / "a.txt")}),
            _done_events("读完"),
        ],
    )
    agent, conv, _events = _make_agent(llm, tmp_path)
    await agent.run("read a.txt")
    # 重试 + 工具轮 + 收尾 = 3 次调用；轮次未因重试多算（assistant 消息 2 条）
    assert llm.call_count == 3
    assistant_msgs = [m for m in conv.messages if m.role == "assistant"]
    assert len(assistant_msgs) == 2


@pytest.mark.asyncio
async def test_transient_exhausts_retries_terminal(tmp_path: Path) -> None:
    """连续 4 次瞬态失败（初始 + 3 重试）→ TERMINAL，错误上报。"""
    llm = FlakyLLM(
        [RuntimeError("503 Service Unavailable") for _ in range(4)], [_done("never")]
    )
    agent, _conv, events = _make_agent(llm, tmp_path)
    await agent.run("hi")
    assert agent._stop_reason == "error"
    assert llm.call_count == 4  # 初始 1 次 + 重试 3 次
    assert any(isinstance(e, ErrorEvent) and "正在重试" in e.error for e in events)
    assert any(isinstance(e, ErrorEvent) and "正在重试" not in e.error for e in events)


@pytest.mark.asyncio
async def test_auth_error_no_retry(tmp_path: Path) -> None:
    """401 认证失败非瞬态：立即终止，只调 1 次无退避。"""
    llm = FlakyLLM([RuntimeError("401 Unauthorized: invalid api key")], [_done("never")])
    agent, _conv, events = _make_agent(llm, tmp_path)
    await agent.run("hi")
    assert agent._stop_reason == "error"
    assert llm.call_count == 1
    assert not any(isinstance(e, ErrorEvent) and "正在重试" in e.error for e in events)


# ---- 分类纯函数 -------------------------------------------------------------


class _Resp:
    def __init__(self, code: int) -> None:
        self.status_code = code


class _StatusErr(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"HTTP {code}")
        self.response = _Resp(code)


@pytest.mark.parametrize(
    ("err", "expect"),
    [
        (_StatusErr(429), True),
        (_StatusErr(500), True),
        (_StatusErr(503), True),
        (_StatusErr(401), False),
        (_StatusErr(400), False),
        (RuntimeError("ReadTimeout"), True),
        (RuntimeError("ConnectionError: reset"), True),
        (RuntimeError("429 Too Many Requests"), True),
        (RuntimeError("401 Unauthorized"), False),
        (RuntimeError("invalid json body"), False),
        (PromptTooLongError("too long"), False),
        (ToolTruncatedError("truncated", empty=True), False),
    ],
)
def test_is_transient_llm_error(err: Exception, expect: bool) -> None:
    assert is_transient_llm_error(err) is expect


# ---- SSE 解析容错（openai adapter）------------------------------------------


def test_openai_parser_skips_noise_lines() -> None:
    """空行 / `:` 注释行 / 非 JSON data 行跳过不抛，后续正常事件不受影响。"""
    p = _OpenAIStreamParser()
    assert p.feed("") == []
    assert p.feed(": keep-alive") == []
    assert p.feed("event: ping") == []
    assert p.feed("data: not-json{{{") == []
    line = (
        '{"choices":[{"delta":{"content":"hi"},"finish_reason":null}],'
        '"object":"chat.completion.chunk"}'
    )
    events = p.feed(f"data: {line}")
    assert [e.type for e in events] == ["text_delta"]
    assert events[0].text == "hi"


# ---- Anthropic PromptTooLong 映射 ------------------------------------------


def test_anthropic_prompt_too_long_mapping() -> None:
    """400/413 + 超窗特征 → PromptTooLongError；其余走通用错误路径。"""
    with pytest.raises(PromptTooLongError):
        _raise_prompt_too_long(400, '{"error":{"message":"prompt is too long: 250000 tokens"}}')
    with pytest.raises(PromptTooLongError):
        _raise_prompt_too_long(413, "request entity too large: prompt too long")
    # 400 但非超窗 → 不抛（由 raise_for_status 处理）
    _raise_prompt_too_long(400, '{"error":{"message":"invalid request"}}')
    # 500 超窗字样也不映射（状态码不在 400/413）
    _raise_prompt_too_long(500, "prompt is too long")
