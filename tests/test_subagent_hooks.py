"""子 Agent Hook 生效（规格 10 §5 333）：SubAgentRunner 共享主 HookEngine。

D75：runner 加 hooks 透传 + cli 装配传主引擎同一实例；子 Agent 内
pre_tool_use / post_tool_use 与生命周期 hook 同触发（10 §3.3 Hook 引擎共享语义）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from conftest import FakeLLM, done, tool_call

from kdagent.config import Config
from kdagent.hooks.engine import HookEngine
from kdagent.subagent import SubAgentRunner
from kdagent.subagent.model import AgentDef
from kdagent.tools import build_default_registry

EXPLORE = AgentDef(
    name="explore",
    description="readonly",
    system_prompt="你是 Explore 子 Agent。",
    disallowed_tools=("EditFile", "WriteFile"),
)


def _runner(tmp_path: Path, llm: FakeLLM, hooks: HookEngine | None) -> SubAgentRunner:
    return SubAgentRunner(
        llm=llm,
        tools=build_default_registry(),
        config=Config(),
        work_dir=tmp_path,
        hooks=hooks,
    )


def _hooks_yaml(items: list[dict]) -> dict:
    return {"hooks": items}


async def _flush() -> None:
    """让事件循环跑完 HookEngine._dispatch 后台调度的 prompt 注入任务。"""
    await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_subagent_pre_post_hooks_fire(tmp_path: Path) -> None:
    """子 Agent 执行工具时 pre_tool_use + post_tool_use hook 触发（10 §3.3 共享）。"""
    target = tmp_path / "hello.txt"
    target.write_text("world", encoding="utf-8")
    llm = FakeLLM(
        [
            tool_call("ReadFile", {"path": str(target)}, id_="r1"),
            done("读到了 world"),
        ]
    )
    injected: list[str] = []
    hooks = HookEngine(prompt_inject=injected.append)
    hooks.load(
        _hooks_yaml(
            [
                {"id": "pre", "event": "pre_tool_use", "action": {"type": "prompt", "prompt": "PRE"}},
                {"id": "post", "event": "post_tool_use", "action": {"type": "prompt", "prompt": "POST"}},
            ]
        )
    )
    runner = _runner(tmp_path, llm, hooks)
    result = await runner.run_to_completion(EXPLORE, "读 hello.txt")
    await _flush()
    assert result.text == "读到了 world"
    assert "PRE" in injected  # 子 Agent 工具调用前 hook 命中
    assert "POST" in injected  # 子 Agent 工具调用后 hook 命中


@pytest.mark.asyncio
async def test_subagent_hook_reject_blocks_tool(tmp_path: Path) -> None:
    """pre_tool_use reject：子 Agent 工具调用被拦截，结果 is_error 进历史重决策。"""
    llm = FakeLLM(
        [
            tool_call("ReadFile", {"path": "x"}, id_="t1"),
            done("已绕过"),
        ]
    )
    hooks = HookEngine()
    hooks.load(
        _hooks_yaml(
            [
                {
                    "id": "block",
                    "event": "pre_tool_use",
                    "reject": True,
                    "action": {"type": "prompt", "prompt": "禁止 ReadFile"},
                }
            ]
        )
    )
    runner = _runner(tmp_path, llm, hooks)
    result = await runner.run_to_completion(EXPLORE, "读文件")
    assert result.text == "已绕过"
    assert not result.is_error  # reject 是工具结果 is_error，非 Agent 错误
    assert llm.call_count == 2  # 第一轮被拒 → 第二轮重新决策


@pytest.mark.asyncio
async def test_subagent_lifecycle_hook_fires(tmp_path: Path) -> None:
    """生命周期 hook（turn_end）在子 Agent 内触发。"""
    llm = FakeLLM([done("ok")])
    injected: list[str] = []
    hooks = HookEngine(prompt_inject=injected.append)
    hooks.load(
        _hooks_yaml(
            [{"id": "t", "event": "turn_end", "action": {"type": "prompt", "prompt": "TURN"}}]
        )
    )
    runner = _runner(tmp_path, llm, hooks)
    result = await runner.run_to_completion(EXPLORE, "任务")
    await _flush()
    assert result.text == "ok"
    assert "TURN" in injected


@pytest.mark.asyncio
async def test_subagent_hooks_none_unchanged(tmp_path: Path) -> None:
    """不传 hooks（None）：子 Agent 行为不变（M5-a 原行为回归）。"""
    target = tmp_path / "hello.txt"
    target.write_text("world", encoding="utf-8")
    llm = FakeLLM(
        [
            tool_call("ReadFile", {"path": str(target)}, id_="r1"),
            done("读到了 world"),
        ]
    )
    runner = _runner(tmp_path, llm, None)
    result = await runner.run_to_completion(EXPLORE, "读 hello.txt")
    assert result.text == "读到了 world"
    assert llm.call_count == 2
