"""Agent Loop × 权限/Hook 集成（规格 06 §3.9-3.10）。

覆盖：DENY/ASK（allow/deny/allow_always）→ 拒绝不终止 Loop；pre_tool_use 拦截
短路；post_tool_use 触发；生命周期 hook 顺序。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from kdagent.config import Config
from kdagent.engine.agent import Agent
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.events import (
    LoopCompleteEvent,
    PermissionRequestEvent,
)
from kdagent.engine.llm.base import LLMStreamEvent, Payload
from kdagent.engine.messages import ToolResultBlock, ToolUseBlock
from kdagent.hooks.engine import HookEngine
from kdagent.permission.blacklist import CommandBlacklist
from kdagent.permission.checker import PermissionChecker
from kdagent.permission.rules import RuleEngine
from kdagent.permission.sandbox import PathSandbox
from kdagent.tools import build_default_registry


class _FakeLLM:
    def __init__(self, responses: list[list[LLMStreamEvent]]) -> None:
        self._responses = responses

    async def stream_chat(self, payload: Payload) -> AsyncIterator[LLMStreamEvent]:
        for ev in self._responses.pop(0):
            yield ev


class _AskEvents:
    """事件 sink + PermissionRequestEvent 自动裁决（headless 测试注入）。"""

    def __init__(self, verdict: str = "allow") -> None:
        self.verdict = verdict
        self.events: list[Any] = []
        self.requests: list[PermissionRequestEvent] = []

    def __call__(self, ev: Any) -> None:
        self.events.append(ev)
        if isinstance(ev, PermissionRequestEvent):
            self.requests.append(ev)
            ev.future.set_result(self.verdict)


def _tool(name: str, input: dict[str, Any], id_: str = "t") -> list[LLMStreamEvent]:
    return [
        LLMStreamEvent(type="tool_use", tool_use=ToolUseBlock(id=id_, name=name, input=input)),
        LLMStreamEvent(type="stop", stop_reason="tool_use"),
    ]


def _done(text: str = "完成") -> list[LLMStreamEvent]:
    return [
        LLMStreamEvent(type="text_delta", text=text),
        LLMStreamEvent(type="stop", stop_reason="end_turn"),
    ]


def _checker(tmp_path: Path, *, rules: RuleEngine | None = None) -> PermissionChecker:
    sb = PathSandbox([tmp_path], work_dir=tmp_path, include_tempdir=False)
    return PermissionChecker(
        mode="default",
        blacklist=CommandBlacklist(),
        sandbox=sb,
        rules=rules,
        work_dir=tmp_path,
    )


async def test_deny_returns_error_loop_continues(tmp_path: Path) -> None:
    """黑名单命中 → DENY → is_error 进历史，Loop 继续到完成（拒绝不终止）。"""
    events = _AskEvents()
    conv = ConversationManager()
    agent = Agent(
        config=Config(),
        llm=_FakeLLM([_tool("Bash", {"command": "rm -rf /"}, "t1"), _done()]),
        conversation=conv,
        tools=build_default_registry(),
        events=events,
        work_dir=tmp_path,
        permission_checker=_checker(tmp_path),
    )
    await agent.run("清理")
    result = conv.messages[2].content[0]
    assert isinstance(result, ToolResultBlock)
    assert result.is_error is True
    assert "权限拒绝" in result.content
    assert any(isinstance(e, LoopCompleteEvent) for e in events.events)


async def test_ask_allow_executes(tmp_path: Path) -> None:
    """ASK → allow → 执行；文件真实写入。"""
    events = _AskEvents(verdict="allow")
    conv = ConversationManager()
    target = tmp_path / "src" / "a.py"
    agent = Agent(
        config=Config(),
        llm=_FakeLLM([_tool("WriteFile", {"path": str(target), "content": "x"}, "t1"), _done()]),
        conversation=conv,
        tools=build_default_registry(),
        events=events,
        work_dir=tmp_path,
        permission_checker=_checker(tmp_path),  # default：write=ask → 走 HITL
    )
    await agent.run("写文件")
    assert len(events.requests) == 1
    assert target.read_text(encoding="utf-8") == "x"
    result = conv.messages[2].content[0]
    assert isinstance(result, ToolResultBlock) and not result.is_error


async def test_ask_deny(tmp_path: Path) -> None:
    """ASK → deny → is_error，文件未写入，Loop 继续。"""
    events = _AskEvents(verdict="deny")
    conv = ConversationManager()
    target = tmp_path / "src" / "a.py"
    agent = Agent(
        config=Config(),
        llm=_FakeLLM([_tool("WriteFile", {"path": str(target), "content": "x"}, "t1"), _done()]),
        conversation=conv,
        tools=build_default_registry(),
        events=events,
        work_dir=tmp_path,
        permission_checker=_checker(tmp_path),
    )
    await agent.run("写文件")
    assert not target.exists()
    result = conv.messages[2].content[0]
    assert isinstance(result, ToolResultBlock) and result.is_error
    assert "已被用户拒绝" in result.content


async def test_ask_allow_always_learns(tmp_path: Path) -> None:
    """「始终允许」→ 追加本地规则，同类操作第二次不再问。"""
    rules = RuleEngine()
    rules.load_many([], local_path=tmp_path / "permissions.local.yaml")
    events = _AskEvents(verdict="allow_always")
    conv = ConversationManager()
    cmd = "git commit -m fix"
    agent = Agent(
        config=Config(),
        llm=_FakeLLM([_tool("Bash", {"command": cmd}, "t1"), _tool("Bash", {"command": cmd}, "t2"), _done()]),
        conversation=conv,
        tools=build_default_registry(),
        events=events,
        work_dir=tmp_path,
        permission_checker=_checker(tmp_path, rules=rules),
    )
    await agent.run("提交")
    assert len(events.requests) == 1  # 第二次 Bash 已被本地规则直接放行
    # 落盘可见
    assert "effect: allow" in (tmp_path / "permissions.local.yaml").read_text(encoding="utf-8")


async def test_pre_tool_reject_blocks_tool(tmp_path: Path) -> None:
    """pre_tool_use reject → 工具取消，理由作为错误结果进历史。"""
    events = _AskEvents()
    hooks = HookEngine()
    hooks.load(
        {
            "hooks": [
                {
                    "id": "block-bash",
                    "event": "pre_tool_use",
                    "reject": True,
                    "action": {"type": "prompt", "prompt": "禁止直接执行 Bash"},
                }
            ]
        }
    )
    conv = ConversationManager()
    agent = Agent(
        config=Config(),
        llm=_FakeLLM([_tool("Bash", {"command": "ls"}, "t1"), _done()]),
        conversation=conv,
        tools=build_default_registry(),
        events=events,
        work_dir=tmp_path,
        hooks=hooks,
    )
    await agent.run("执行")
    result = conv.messages[2].content[0]
    assert isinstance(result, ToolResultBlock) and result.is_error
    assert "禁止直接执行 Bash" in result.content
    assert any(isinstance(e, LoopCompleteEvent) for e in events.events)  # 拒绝不终止


async def test_post_tool_prompt_hook_fires(tmp_path: Path) -> None:
    """post_tool_use prompt hook：工具执行后注入系统提示。"""
    events = _AskEvents()
    injected: list[str] = []
    hooks = HookEngine(prompt_inject=injected.append)
    hooks.load(
        {
            "hooks": [
                {
                    "id": "fmt",
                    "event": "post_tool_use",
                    "if": 'tool == "TodoWrite"',
                    "action": {"type": "prompt", "prompt": "[system-reminder] 已规划完成"},
                }
            ]
        }
    )
    conv = ConversationManager()
    agent = Agent(
        config=Config(),
        llm=_FakeLLM(
            [
                _tool("TodoWrite", {"todos": [{"content": "目标", "tasks": []}]}, "t1"),
                _done(),
            ]
        ),
        conversation=conv,
        tools=build_default_registry(),
        events=events,
        work_dir=tmp_path,
        hooks=hooks,
    )
    await agent.run("规划")
    assert injected == ["[system-reminder] 已规划完成"]


async def test_lifecycle_hook_order(tmp_path: Path) -> None:
    """生命周期 hook 顺序：session_start → turn_start → turn_end → session_end。"""
    events = _AskEvents()
    record: list[str] = []
    hooks = HookEngine(prompt_inject=record.append)
    hooks.load(
        {
            "hooks": [
                {"id": "s", "event": "session_start", "action": {"type": "prompt", "prompt": "session_start"}},
                {"id": "t", "event": "turn_start", "action": {"type": "prompt", "prompt": "turn_start"}},
                {"id": "te", "event": "turn_end", "action": {"type": "prompt", "prompt": "turn_end"}},
                {"id": "e", "event": "session_end", "action": {"type": "prompt", "prompt": "session_end"}},
            ]
        }
    )
    conv = ConversationManager()
    agent = Agent(
        config=Config(),
        llm=_FakeLLM([_done()]),
        conversation=conv,
        tools=build_default_registry(),
        events=events,
        work_dir=tmp_path,
        hooks=hooks,
    )
    await agent.run("你好")
    assert record == ["session_start", "turn_start", "turn_end", "session_end"]


async def test_no_checker_keeps_legacy_confirm(tmp_path: Path) -> None:
    """无 checker 时保留 M1 require_confirm 行为（回归护栏）。"""
    events = _AskEvents()

    async def _reject(name: str, input: dict[str, Any]) -> bool:
        return False

    conv = ConversationManager()
    agent = Agent(
        config=Config(),
        llm=_FakeLLM([_tool("Bash", {"command": "ls"}, "t1"), _done()]),
        conversation=conv,
        tools=build_default_registry(),
        events=events,
        work_dir=tmp_path,
        confirm=_reject,
    )
    await agent.run("执行")
    result = conv.messages[2].content[0]
    assert isinstance(result, ToolResultBlock) and result.is_error
    assert "已被用户拒绝" in result.content
