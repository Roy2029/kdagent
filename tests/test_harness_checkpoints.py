"""双层检查点测试（规格 12 §3.3：声明驱动主检查点 + 行为观察兜底）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

# --- 复用 test_agent_loop 的驱动基建 ---
from kdagent.config import Config
from kdagent.engine.agent import Agent
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import LLMStreamEvent, Payload
from kdagent.engine.messages import TextBlock, ToolUseBlock
from kdagent.harness.checkpoints import (
    CheckpointEvent,
    build_checkpoint_reminder,
    build_large_change_warning,
    build_replan_reminder,
    build_stale_todo_reminder,
    todo_progress,
)
from kdagent.tools import build_default_registry
from kdagent.tools.base import ToolResult


class FakeLLM:
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


def _done(text: str = "完成") -> list[LLMStreamEvent]:
    return [
        LLMStreamEvent(type="text_delta", text=text),
        LLMStreamEvent(type="stop", stop_reason="end_turn"),
    ]


def _tool(name: str, input: dict[str, Any], id_: str = "t") -> list[LLMStreamEvent]:
    return [
        LLMStreamEvent(type="tool_use", tool_use=ToolUseBlock(id=id_, name=name, input=input)),
        LLMStreamEvent(type="stop", stop_reason="tool_use"),
    ]


def _text_blocks(conv: ConversationManager) -> list[str]:
    """对话中所有 TextBlock 文本（含 extra_blocks 注入的 system-reminder）。"""
    out: list[str] = []
    for msg in conv.messages:
        for block in msg.content:
            if isinstance(block, TextBlock):
                out.append(block.text)
    return out


# --- 纯函数：todo_progress 步骤边界 ---


def test_todo_progress_no_before_returns_none() -> None:
    todos = [{"content": "目标", "tasks": []}]
    assert todo_progress(None, todos) is None


def test_todo_progress_no_newly_completed_returns_none() -> None:
    before = [
        {"content": "目标", "tasks": [{"content": "A", "status": "in_progress", "steps": []}]}
    ]
    after = [
        {"content": "目标", "tasks": [{"content": "A", "status": "in_progress", "steps": []}]}
    ]
    assert todo_progress(before, after) is None


def test_todo_progress_boundary_with_criteria() -> None:
    before = [
        {
            "content": "目标",
            "tasks": [
                {
                    "content": "A",
                    "status": "in_progress",
                    "steps": [{"description": "改代码", "accept_criteria": "测试全绿"}],
                }
            ],
        }
    ]
    after = [
        {
            "content": "目标",
            "tasks": [
                {
                    "content": "A",
                    "status": "completed",
                    "steps": [{"description": "改代码", "accept_criteria": "测试全绿"}],
                }
            ],
        }
    ]
    ev = todo_progress(before, after)
    assert ev is not None
    assert ev.task_content == "A"
    assert ev.todo_content == "目标"
    assert ev.accept_criteria == "测试全绿"


def test_todo_progress_boundary_takes_last_step_criteria() -> None:
    before = [
        {
            "content": "目标",
            "tasks": [
                {
                    "content": "A",
                    "status": "in_progress",
                    "steps": [
                        {"description": "步骤1", "accept_criteria": "旧"},
                        {"description": "步骤2", "accept_criteria": "新判据"},
                    ],
                }
            ],
        }
    ]
    after = [
        {
            "content": "目标",
            "tasks": [
                {
                    "content": "A",
                    "status": "completed",
                    "steps": [
                        {"description": "步骤1", "accept_criteria": "旧"},
                        {"description": "步骤2", "accept_criteria": "新判据"},
                    ],
                }
            ],
        }
    ]
    ev = todo_progress(before, after)
    assert ev is not None
    assert ev.accept_criteria == "新判据"


def test_todo_progress_boundary_without_steps_criteria_empty() -> None:
    before = [{"content": "目标", "tasks": [{"content": "A", "status": "in_progress"}]}]
    after = [{"content": "目标", "tasks": [{"content": "A", "status": "completed"}]}]
    ev = todo_progress(before, after)
    assert ev is not None
    assert ev.accept_criteria == ""
    assert ev.step_description == ""


# --- 纯函数：reminder 构建 ---


def test_checkpoint_reminder_contains_snapshot_and_criteria() -> None:
    ev = CheckpointEvent(
        todo_content="目标", task_content="A", step_description="改代码", accept_criteria="测试全绿"
    )
    todos = [
        {"content": "目标", "tasks": [{"content": "A", "status": "completed", "steps": []}]}
    ]
    text = build_checkpoint_reminder(ev, todos)
    assert "<system-reminder>" in text and "</system-reminder>" in text
    assert "步骤边界检查点" in text and "刚完成任务「A」" in text
    assert "完成判据：测试全绿" in text
    assert "当前 todo 快照" in text and "目标" in text  # format_todos 完整快照


def test_checkpoint_reminder_without_criteria_self_assess() -> None:
    ev = CheckpointEvent(todo_content="目标", task_content="A", step_description="", accept_criteria="")
    text = build_checkpoint_reminder(ev, [])
    assert "无机械判据" in text


def test_stale_todo_reminder_contains_threshold_and_snapshot() -> None:
    todos = [
        {"content": "目标", "tasks": [{"content": "A", "status": "in_progress", "steps": []}]}
    ]
    text = build_stale_todo_reminder(todos)
    assert "<system-reminder>" in text
    assert "5 轮" in text
    assert "目标" in text  # 快照保真


def test_large_change_warning_under_limit_lists_all() -> None:
    text = build_large_change_warning(["a.py", "b.py"])
    assert "2 个文件" in text
    assert "- a.py" in text and "- b.py" in text
    assert "等" not in text


def test_large_change_warning_over_limit_truncates() -> None:
    paths = [f"f{i}.py" for i in range(12)]
    text = build_large_change_warning(paths)
    assert "12 个文件" in text
    assert "- f0.py" in text and "- f9.py" in text
    assert "- f10.py" not in text  # 截断到前 10
    assert "等 12 个" in text


def test_replan_reminder_contains_rewrite_instruction() -> None:
    todos = [
        {"content": "目标", "tasks": [{"content": "A", "status": "in_progress", "steps": []}]}
    ]
    text = build_replan_reminder(todos)
    assert "<system-reminder>" in text and "</system-reminder>" in text
    assert "路径反复受阻" in text
    assert "整体重写" in text and "不修补" in text
    assert "目标" in text  # 快照保真（废弃旧列表前先对表）


# --- agent 接线：第一层声明驱动 ---


async def test_todo_boundary_injects_checkpoint_reminder(tmp_path: Path) -> None:
    todos_in_progress = [
        {
            "content": "目标",
            "tasks": [
                {
                    "content": "A",
                    "status": "in_progress",
                    "steps": [{"description": "改代码", "accept_criteria": "测试全绿"}],
                }
            ],
        }
    ]
    todos_completed = [
        {
            "content": "目标",
            "tasks": [
                {
                    "content": "A",
                    "status": "completed",
                    "steps": [{"description": "改代码", "accept_criteria": "测试全绿"}],
                }
            ],
        }
    ]
    responses = [
        _tool("TodoWrite", {"todos": todos_in_progress}, id_="t1"),
        _tool("TodoWrite", {"todos": todos_completed}, id_="t2"),
        _done(),
    ]
    agent, conv, _ = _make_agent(responses, tmp_path)
    await agent.run("任务")
    joined = "\n".join(_text_blocks(conv))
    assert "步骤边界检查点" in joined
    assert "完成判据：测试全绿" in joined


async def test_todo_no_boundary_no_inject(tmp_path: Path) -> None:
    todos = [
        {
            "content": "目标",
            "tasks": [{"content": "A", "status": "in_progress", "steps": []}],
        }
    ]
    responses = [
        _tool("TodoWrite", {"todos": todos}, id_="t1"),
        _tool("TodoWrite", {"todos": todos}, id_="t2"),
        _done(),
    ]
    agent, conv, _ = _make_agent(responses, tmp_path)
    await agent.run("任务")
    joined = "\n".join(_text_blocks(conv))
    assert "步骤边界检查点" not in joined


async def test_todo_stale_injects_refresh_reminder(tmp_path: Path) -> None:
    todos = [
        {"content": "目标", "tasks": [{"content": "A", "status": "in_progress", "steps": []}]}
    ]
    responses = [
        _tool("TodoWrite", {"todos": todos}, id_="t0"),
        _tool("WriteFile", {"path": str(tmp_path / "a.py"), "content": "x = 1"}, id_="w1"),
        _tool("WriteFile", {"path": str(tmp_path / "b.py"), "content": "x = 2"}, id_="w2"),
        _tool("WriteFile", {"path": str(tmp_path / "c.py"), "content": "x = 3"}, id_="w3"),
        _tool("WriteFile", {"path": str(tmp_path / "d.py"), "content": "x = 4"}, id_="w4"),
        _tool("WriteFile", {"path": str(tmp_path / "e.py"), "content": "x = 5"}, id_="w5"),
        _done(),
    ]
    agent, conv, _ = _make_agent(responses, tmp_path)
    await agent.run("任务")
    joined = "\n".join(_text_blocks(conv))
    assert "todo 已连续 5 轮工具操作未更新" in joined


async def test_large_change_same_round_injects_warning(tmp_path: Path) -> None:
    # 一轮内 5 个 WriteFile（串行独立批）→ 跨批累计达阈值
    events: list[LLMStreamEvent] = []
    for i in range(5):
        events.append(
            LLMStreamEvent(
                type="tool_use",
                tool_use=ToolUseBlock(
                    id=f"w{i}",
                    name="WriteFile",
                    input={"path": str(tmp_path / f"f{i}.py"), "content": "x = 1"},
                ),
            )
        )
    events.append(LLMStreamEvent(type="stop", stop_reason="tool_use"))
    responses = [events, _done()]
    agent, conv, _ = _make_agent(responses, tmp_path)
    await agent.run("任务")
    joined = "\n".join(_text_blocks(conv))
    assert "跨文件大改" in joined
    assert "5 个文件" in joined


async def test_large_change_below_threshold_no_warning(tmp_path: Path) -> None:
    events: list[LLMStreamEvent] = []
    for i in range(2):
        events.append(
            LLMStreamEvent(
                type="tool_use",
                tool_use=ToolUseBlock(
                    id=f"w{i}",
                    name="WriteFile",
                    input={"path": str(tmp_path / f"f{i}.py"), "content": "x = 1"},
                ),
            )
        )
    events.append(LLMStreamEvent(type="stop", stop_reason="tool_use"))
    responses = [events, _done()]
    agent, conv, _ = _make_agent(responses, tmp_path)
    await agent.run("任务")
    joined = "\n".join(_text_blocks(conv))
    assert "跨文件大改" not in joined


# --- Replan 接入（D57，12 §3.3）：断路器反复触发 = 路径不可行 → 整体重写 todo ---


def _fail_batch(n: int) -> list[LLMStreamEvent]:
    """一批 3 个失败 Bash（→ 触发一次断路器，规格 02 §3.5）。"""
    return [_tool("Bash", {"command": "exit 1"}, id_=f"b{n}_{i}") for i in range(3)]


def test_circuit_breaker_second_trigger_injects_replan(tmp_path: Path) -> None:
    """断路器第 2 次触发（有 todo 快照）→ 注入 Replan 引导；第 1 次只注入熔断。"""
    agent, conv, _ = _make_agent([_done()], tmp_path)
    agent._last_todos = [
        {"content": "目标", "tasks": [{"content": "A", "status": "in_progress", "steps": []}]}
    ]
    fail = [ToolResult(tool_use_id="t", name="Bash", content="失败", is_error=True)]
    agent._update_circuit_breaker(fail * 3)  # 第 1 次触发：仅熔断
    assert "已连续失败 3 次" in "\n".join(_text_blocks(conv))
    assert "路径反复受阻" not in "\n".join(_text_blocks(conv))
    agent._update_circuit_breaker(fail * 3)  # 第 2 次触发：路径反复受阻 → Replan
    joined = "\n".join(_text_blocks(conv))
    assert "路径反复受阻" in joined
    assert "整体重写" in joined


def test_circuit_breaker_success_resets_replan_counter(tmp_path: Path) -> None:
    """一批全成功 = 路径有进展 → Replan 计数复位，不误判持续不可行。"""
    agent, conv, _ = _make_agent([_done()], tmp_path)
    agent._last_todos = [{"content": "目标", "tasks": []}]
    fail = [ToolResult(tool_use_id="t", name="Bash", content="失败", is_error=True)]
    ok = [ToolResult(tool_use_id="t", name="Bash", content="成功", is_error=False)]
    agent._update_circuit_breaker(fail * 3)  # 触发 #1
    agent._update_circuit_breaker(ok)  # 全成功 → 复位
    agent._update_circuit_breaker(fail * 3)  # 重新触发（第 1 次，非第 2 次）
    joined = "\n".join(_text_blocks(conv))
    assert "路径反复受阻" not in joined
    assert "已连续失败 3 次" in joined  # 熔断 reminder 本身仍在


def test_replan_skipped_without_todo_snapshot(tmp_path: Path) -> None:
    """无 todo 快照（_last_todos 为空）→ 不注入 Replan（无从对表重写）。"""
    agent, conv, _ = _make_agent([_done()], tmp_path)
    fail = [ToolResult(tool_use_id="t", name="Bash", content="失败", is_error=True)]
    agent._update_circuit_breaker(fail * 3)
    agent._update_circuit_breaker(fail * 3)
    joined = "\n".join(_text_blocks(conv))
    assert "已连续失败 3 次" in joined
    assert "路径反复受阻" not in joined


async def test_replan_injected_in_full_run_after_repeated_breaks(tmp_path: Path) -> None:
    """端到端：TodoWrite 建快照 → 两轮各触发一次断路器 → Replan 引导注入对话。"""
    todos = [
        {"content": "目标", "tasks": [{"content": "A", "status": "in_progress", "steps": []}]}
    ]
    responses = [
        _tool("TodoWrite", {"todos": todos}, id_="t0"),
        *_fail_batch(0),
        *_fail_batch(1),
        _done(),
    ]
    agent, conv, _ = _make_agent(responses, tmp_path)
    await agent.run("任务")
    joined = "\n".join(_text_blocks(conv))
    assert "已连续失败 3 次" in joined
    assert "路径反复受阻" in joined
    assert "整体重写" in joined
