"""require_confirm 前置确认钩子测试（规格 05 §3.4 / 03 §3.6）。

WriteFile require_confirm=True；注入 confirm 钩子：
- no → 返回 is_error=True "已被用户拒绝"，文件未创建，Loop 继续
- yes → 正常执行
- 无钩子（非交互环境）→ 直接执行（向后兼容）
"""

from __future__ import annotations

from pathlib import Path

from conftest import FakeLLM, done, tool_call

from kdagent.config import Config
from kdagent.engine.agent import Agent
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import LLMStreamEvent
from kdagent.engine.messages import ToolResultBlock
from kdagent.tools import build_default_registry
from kdagent.tools.base import AsyncConfirm


def _make_agent(
    responses: list[list[LLMStreamEvent]],
    tmp_path: Path,
    confirm: AsyncConfirm | None = None,
) -> tuple[Agent, ConversationManager, list[object]]:
    collected: list[object] = []
    conv = ConversationManager()
    agent = Agent(
        config=Config(),
        llm=FakeLLM(responses),
        conversation=conv,
        tools=build_default_registry(),
        events=collected.append,
        work_dir=tmp_path,
        confirm=confirm,
    )
    return agent, conv, collected


async def _deny(name: str, input: dict[str, object]) -> bool:
    return False


async def _approve(name: str, input: dict[str, object]) -> bool:
    return True


async def test_confirm_denied_returns_error_result(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    responses = [
        tool_call("WriteFile", {"path": str(target), "content": "x"}, id_="w1"),
        done("已调整"),
    ]
    agent, conv, _ = _make_agent(responses, tmp_path, confirm=_deny)
    await agent.run("写文件")
    result = conv.messages[2].content[0]
    assert isinstance(result, ToolResultBlock)
    assert result.is_error is True
    assert "拒绝" in result.content
    assert not target.exists()  # 文件未被写入


async def test_confirm_approved_executes(tmp_path: Path) -> None:
    target = tmp_path / "b.txt"
    responses = [
        tool_call("WriteFile", {"path": str(target), "content": "x"}, id_="w2"),
        done("完成"),
    ]
    agent, conv, _ = _make_agent(responses, tmp_path, confirm=_approve)
    await agent.run("写文件")
    result = conv.messages[2].content[0]
    assert isinstance(result, ToolResultBlock)
    assert result.is_error is False
    assert target.read_text(encoding="utf-8") == "x"


async def test_confirm_none_runs_directly(tmp_path: Path) -> None:
    """非交互环境（confirm=None）直接执行，不弹确认。"""
    target = tmp_path / "c.txt"
    responses = [
        tool_call("WriteFile", {"path": str(target), "content": "y"}, id_="w3"),
        done("完成"),
    ]
    agent, conv, _ = _make_agent(responses, tmp_path, confirm=None)
    await agent.run("写文件")
    assert target.read_text(encoding="utf-8") == "y"
