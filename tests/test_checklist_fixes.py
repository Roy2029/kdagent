"""检查清单 5 项修复的回归测试（2026-08-29 实测反馈批次）。

覆盖：
- B3+B4：Bash rm/mv/cp 写目标命中 kdagent 敏感路径 → deny（bypass 也不豁免）
- B5：prompt 型 hook 注入接线（HookEngine.set_prompt_inject + Agent 默认接线）
- C3：OpenAI 兼容 _build_body 把 payload.system 放 messages[0]
- C4：mcp 2.0 属性名漂移（input_schema / is_error / content 块对象）
- E2：role=tool 重复防护（序列化去重 + _flush_last 不重复写行 + resume 链修复）
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from kdagent.config import Config
from kdagent.engine.agent import Agent, HOOK_PROMPT_MARKER
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import LLMStreamEvent, Payload, ProviderConfig
from kdagent.engine.llm.openai import OpenAICompatClient, _serialize_messages
from kdagent.engine.messages import Message, TextBlock, ToolResultBlock, ToolUseBlock
from kdagent.hooks.engine import HookEngine
from kdagent.hooks.engine_types import HookContext
from kdagent.mcp.manager import _SDKClient
from kdagent.mcp.model import extract_text
from kdagent.permission.checker import PermissionChecker
from kdagent.permission.sandbox import PathSandbox
from kdagent.sessions.manager import Session, SessionManager
from kdagent.sessions.records import SessionRecord
from kdagent.tools import build_default_registry
from kdagent.tools.base import ToolResult
from kdagent.tools.shell import Bash


# ---- 公共构造 ----

def _checker(tmp_path: Path, *, mode: str = "default") -> PermissionChecker:
    """最小裁决器：沙箱仅 work_dir；kdagent_dirs 指向项目 .kdagent。"""
    sb = PathSandbox([tmp_path], work_dir=tmp_path, include_tempdir=False)
    return PermissionChecker(
        mode=mode,
        sandbox=sb,
        work_dir=tmp_path,
        kdagent_dirs=[tmp_path / ".kdagent"],
    )


class _FakeLLM:
    """按顺序弹事件批的假 LLM（同 test_agent_loop，流式消费）。"""

    def __init__(self, responses: list[list[LLMStreamEvent]]) -> None:
        self._responses = responses

    async def stream_chat(self, payload: Payload) -> AsyncIterator[LLMStreamEvent]:
        for ev in self._responses.pop(0):
            yield ev


def _done(text: str) -> list[LLMStreamEvent]:
    return [
        LLMStreamEvent(type="text_delta", text=text),
        LLMStreamEvent(type="stop", stop_reason="end_turn"),
    ]


def _tr(use_id: str, content: str) -> ToolResult:
    return ToolResult(tool_use_id=use_id, name="Bash", content=content)


# ---- B3+B4：Bash 写目标敏感禁写 ----

def test_bash_rm_relative_sensitive_path_denied_even_bypass(tmp_path: Path) -> None:
    ck = _checker(tmp_path, mode="bypassPermissions")
    d = ck.check(Bash(), {"command": "rm .kdagent/permissions.local.yaml"})
    assert d.effect == "deny"
    assert "敏感路径禁写" in d.reason


def test_bash_rm_absolute_sensitive_path_denied(tmp_path: Path) -> None:
    ck = _checker(tmp_path)
    target = tmp_path / ".kdagent" / "config.yaml"
    d = ck.check(Bash(), {"command": f"rm -rf {target}"})
    assert d.effect == "deny"


def test_bash_mv_into_sensitive_name_denied(tmp_path: Path) -> None:
    ck = _checker(tmp_path)
    d = ck.check(Bash(), {"command": f"mv notes.txt {tmp_path / '.kdagent' / 'permissions.yaml'}"})
    assert d.effect == "deny"


def test_bash_redirect_overwrites_sensitive_config_denied(tmp_path: Path) -> None:
    ck = _checker(tmp_path)
    d = ck.check(Bash(), {"command": f"echo x > {tmp_path / '.kdagent' / 'config.yaml'}"})
    assert d.effect == "deny"


def test_bash_rm_normal_file_not_blocked(tmp_path: Path) -> None:
    ck = _checker(tmp_path)
    d = ck.check(Bash(), {"command": "rm build/tmp.txt"})
    assert d.effect != "deny"  # 普通删除不误伤（后续按 L4 矩阵裁决）


def test_bash_readonly_command_not_blocked(tmp_path: Path) -> None:
    ck = _checker(tmp_path)
    d = ck.check(Bash(), {"command": "ls -la .kdagent"})
    assert d.effect != "deny"


# ---- B5：prompt 型 hook 注入 ----

def test_set_prompt_inject_wires_runtime_callback() -> None:
    injected: list[str] = []
    eng = HookEngine()  # 构造时未传 prompt_inject（cli 现状）
    eng.set_prompt_inject(injected.append)
    eng.load(
        {
            "hooks": [
                {"id": "p", "event": "turn_end", "action": {"type": "prompt", "prompt": "记得收尾"}}
            ]
        }
    )
    eng.run("turn_end", HookContext(event="turn_end"))
    assert injected == ["记得收尾"]


async def test_agent_wires_prompt_inject_by_default(tmp_path: Path) -> None:
    eng = HookEngine()
    eng.load(
        {
            "hooks": [
                {"id": "p", "event": "turn_start", "action": {"type": "prompt", "prompt": "先列计划"}}
            ]
        }
    )
    conv = ConversationManager()
    agent = Agent(
        config=Config(),
        llm=_FakeLLM([_done("ok")]),
        conversation=conv,
        tools=build_default_registry(),
        events=lambda e: None,
        work_dir=tmp_path,
        hooks=eng,
    )
    await agent.run("你好")
    assert any(
        isinstance(b, TextBlock) and b.text.startswith(HOOK_PROMPT_MARKER)
        for m in conv.messages
        for b in m.content
    )


async def test_agent_wire_hook_prompt_false_skips_injection(tmp_path: Path) -> None:
    eng = HookEngine()
    eng.load(
        {
            "hooks": [
                {"id": "p", "event": "turn_start", "action": {"type": "prompt", "prompt": "先列计划"}}
            ]
        }
    )
    conv = ConversationManager()
    agent = Agent(
        config=Config(),
        llm=_FakeLLM([_done("ok")]),
        conversation=conv,
        tools=build_default_registry(),
        events=lambda e: None,
        work_dir=tmp_path,
        hooks=eng,
        wire_hook_prompt=False,  # 子 Agent 路径：不覆盖主注入目标
    )
    await agent.run("你好")
    assert not any(
        isinstance(b, TextBlock) and b.text.startswith(HOOK_PROMPT_MARKER)
        for m in conv.messages
        for b in m.content
    )


# ---- C3：payload.system 进 messages[0] ----

def test_build_body_prepends_system_message() -> None:
    payload = Payload(
        system="记忆索引：/docs/x.md",
        messages=[Message(role="user", content=[TextBlock("hi")])],
        max_tokens=64,
    )
    client = OpenAICompatClient(ProviderConfig(protocol="openai", model="deepseek-chat"))
    body = client._build_body(payload)
    assert body["messages"][0] == {"role": "system", "content": "记忆索引：/docs/x.md"}
    assert body["messages"][1]["role"] == "user"


def test_build_body_no_system_no_prepend() -> None:
    payload = Payload(
        system="",
        messages=[Message(role="user", content=[TextBlock("hi")])],
        max_tokens=64,
    )
    client = OpenAICompatClient(ProviderConfig(protocol="openai", model="deepseek-chat"))
    body = client._build_body(payload)
    assert body["messages"][0]["role"] == "user"


# ---- C4：mcp 2.0 属性名漂移 ----

class _PDBlock:  # 模拟 pydantic TextContent 对象（mcp 2.0）
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


def test_extract_text_accepts_dict_and_object_blocks() -> None:
    content: list[Any] = [
        {"type": "text", "text": "a"},
        _PDBlock("b"),
        {"type": "image", "data": "x"},  # 非 text 块跳过
    ]
    assert extract_text(content) == "a\nb"


class _FakeTool2:
    name = "tool"
    description = "test tool"
    input_schema = {"type": "object", "properties": {}}  # mcp 2.0 新属性名


class _FakeListing:
    tools = [_FakeTool2()]


class _FakeCallResult:
    content = [{"type": "text", "text": "ok"}]
    is_error = True  # mcp 2.0 新属性名


class _FakeSession:
    async def list_tools(self) -> _FakeListing:
        return _FakeListing()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _FakeCallResult:
        return _FakeCallResult()


async def test_sdk_client_compat_with_mcp_2_names() -> None:
    client = _SDKClient(_FakeSession())
    tools = await client.list_tools()
    assert tools[0].input_schema == {"type": "object", "properties": {}}
    res = await client.call_tool("x", {})
    assert res.is_error is True
    assert extract_text(res.content) == "ok"


# ---- E2：role=tool 重复防护 ----

def test_serialize_dedups_repeated_tool_result() -> None:
    conv = ConversationManager()
    conv.add_user_message("q")
    conv.add_tool_results([_tr("t1", "a")])
    conv.add_tool_results([_tr("t1", "a")])  # 旧文件 resume 后可能带重复
    serialized = _serialize_messages(conv.messages)
    tool_msgs = [m for m in serialized if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "t1"


def test_flush_last_no_duplicate_lines_on_merge(tmp_path: Path) -> None:
    file = tmp_path / "s.jsonl"
    file.touch()
    conv = ConversationManager()
    sess = Session("s1", file, conv)
    sess.append_user("hi")  # 1 条
    sess.append_tool_results([_tr("t1", "a")])  # 合并进最后 user → 行数仍 1
    sess.append_tool_results([_tr("t2", "b")])  # 仍 1
    with file.open("r", encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip()]
    assert len(lines) == len(conv.messages) == 1


def test_resume_repairs_duplicate_tool_result(tmp_path: Path) -> None:
    file = tmp_path / "20260829-000000-0001.jsonl"
    msgs = [
        Message(role="user", content=[TextBlock("hi")]),
        Message(role="assistant", content=[ToolUseBlock(id="t1", name="Bash", input={})]),
        Message(role="user", content=[ToolResultBlock(tool_use_id="t1", content="a")]),
        Message(role="user", content=[ToolResultBlock(tool_use_id="t1", content="a")]),
    ]
    with file.open("w", encoding="utf-8") as f:
        for m in msgs:
            f.write(SessionRecord.from_message(m, 1).to_json() + "\n")
    sess = SessionManager(tmp_path).resume(file.stem)
    tr_count = sum(
        isinstance(b, ToolResultBlock)
        for m in sess.conversation.messages
        for b in m.content
    )
    assert tr_count == 1
