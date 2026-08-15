"""M2-c L3 Auto-Compact 测试（规格 01 §5.4/§6/§6.1 + 04 §5 + 12 §3.2）。

覆盖：触发线、check_before_call 决策（AUTO/FORCE/熔断）、9 部分摘要 + 快照重灌、
近期原文保留（tool 配对不切断）、独立预算（auto 熔断 / force 耗尽 ContextFullError /
成功复位）、摘要重试、Agent 阶段 A/B 端到端、恢复触发压缩 + todo 快照重灌、压缩后文件重写。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from conftest import FakeLLM, done

from kdagent.config import Config
from kdagent.context.compactor import (
    AUTO_COMPACT_TRIGGER,
    FORCE_COMPACT_LINE,
    Compactor,
    ContextFullError,
    _recent_keep,
    render_todo_snapshot,
)
from kdagent.context.context_manager import ContextManager
from kdagent.engine.agent import Agent
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import LLMClient, LLMStreamEvent, Payload, PromptTooLongError
from kdagent.engine.messages import Message, TextBlock, ToolResultBlock, ToolUseBlock
from kdagent.sessions.manager import (
    TODO_SNAPSHOT_MARKER,
    SessionManager,
    _is_todo_snapshot_message,
)
from kdagent.sessions.records import StepRecord, TodoItemRecord
from kdagent.tools import build_default_registry
from kdagent.tools.base import ToolResult


class _BoomLLM:
    """每次调用都以 error 事件抛异常的假 LLM（触发压缩失败路径）。"""

    async def stream_chat(self, payload: Payload) -> AsyncIterator[LLMStreamEvent]:
        while True:
            yield LLMStreamEvent(type="error", error=RuntimeError("boom"))


class _ScriptedLLM:
    """先失败 N 次、之后稳定返回摘要的假 LLM（预算复位测试用）。"""

    def __init__(self, failures: int = 0, summary: str = "ok") -> None:
        self._failures_left = failures
        self._summary = summary
        self.call_count = 0

    async def stream_chat(self, payload: Payload) -> AsyncIterator[LLMStreamEvent]:
        self.call_count += 1
        if self._failures_left > 0:
            self._failures_left -= 1
            yield LLMStreamEvent(type="error", error=RuntimeError("boom"))
            return
        yield LLMStreamEvent(type="text_delta", text=f"<summary>{self._summary}</summary>")
        yield LLMStreamEvent(type="stop", stop_reason="end_turn")


def _summary_batch(text: str) -> list[LLMStreamEvent]:
    return [
        LLMStreamEvent(type="text_delta", text=text),
        LLMStreamEvent(type="stop", stop_reason="end_turn"),
    ]


def _conv(n: int = 4) -> ConversationManager:
    """n 条小消息的会话（配合 recent_keep 5/2：尾部 2 条保留、前部被摘要）。

    用 restore 直灌独立 Message——`add_user_message` 会合并相邻同角色消息（02 §3.3
    交替规则），连续 user 消息会塌成一条，导致无早期可摘。
    """
    conv = ConversationManager()
    conv.restore(
        [
            Message(role="user", content=[TextBlock(f"内容 {i} " + "x" * 100)])
            for i in range(n)
        ]
    )
    return conv


def _small_compactor(llm: LLMClient) -> Compactor:
    """recent_keep 预算调小，让少量消息即可产生可摘要部分。"""
    return Compactor(llm, recent_keep_tokens=5, recent_keep_min_messages=2)


# ---- 触发线 / 决策 ---------------------------------------------------------


def test_compactor_thresholds() -> None:
    """自动 167K / 强制 177K（01 §5.5：强制线高于自动线）。"""
    c = Compactor(FakeLLM([]))
    assert c.auto_compact_trigger == AUTO_COMPACT_TRIGGER == 167_000
    assert c.force_compact_line == FORCE_COMPACT_LINE == 177_000
    assert c.should_compact(167_000) is True
    assert c.should_compact(166_999) is False


def test_check_before_call_decision_and_circuit(tmp_path: Path) -> None:
    """NORMAL / AUTO_COMPACT / FORCE_COMPACT 判定；熔断挡 AUTO 不挡 FORCE。"""
    cm = ContextManager(tmp_path, session_id="s", llm=FakeLLM([]))
    assert cm.check_before_call(166_999) == "NORMAL"
    assert cm.check_before_call(167_000) == "AUTO_COMPACT"
    assert cm.check_before_call(177_000) == "FORCE_COMPACT"
    # 无 LLM（无法压缩）→ 一律 NORMAL
    assert ContextManager(tmp_path, session_id="s").check_before_call(300_000) == "NORMAL"
    # 熔断只关自动路径：AUTO 被挡、FORCE 无视熔断
    cm._circuit_open = True  # 直接置位，熔断产生路径由 auto 失败测试覆盖
    assert cm.check_before_call(170_000) == "NORMAL"
    assert cm.check_before_call(180_000) == "FORCE_COMPACT"


# ---- 摘要生成：9 部分 + 快照 + 近期原文 ------------------------------------


async def test_compact_summarizes_and_keeps_recent(tmp_path: Path) -> None:
    """compact：早期历史摘要 + 尾部近期原文保留；两阶段取 <summary> 正文。"""
    llm = FakeLLM([_summary_batch("<analysis>草稿</analysis>\n<summary>早期读取逻辑</summary>")])
    c = _small_compactor(llm)
    conv = _conv(4)
    result = await c.compact(conv, session_path="/sess/s.jsonl")

    block = result.summary_message.content[0]
    assert isinstance(block, TextBlock)
    assert "<context-summary>" in block.text
    assert "早期读取逻辑" in block.text
    assert "<analysis>" not in block.text  # 草稿被丢弃
    assert "--- 会话记录 ---" in block.text
    assert "/sess/s.jsonl" in block.text
    assert len(result.kept_recent) == 2  # 尾部 2 条原文
    assert llm.call_count == 1


async def test_compact_noop_when_nothing_to_summarize(tmp_path: Path) -> None:
    """全部消息都在近期保留内（无早期部分）→ no-op，不调 LLM。"""
    llm = FakeLLM([])
    c = Compactor(llm, system_prompt="sys")
    conv = ConversationManager()
    conv.add_user_message("唯一一条消息")
    result = await c.compact(conv, session_path="/s.jsonl")
    block = result.summary_message.content[0]
    assert isinstance(block, TextBlock)
    assert "未超过可压缩阈值" in block.text
    assert result.kept_recent == conv.messages
    assert llm.call_count == 0


def test_recent_keep_preserves_tool_pair() -> None:
    """近期保留边界停在 tool_result 消息 → 前扩包含其 tool_use（配对不切断）。"""
    conv = ConversationManager()
    conv.add_user_message("开始")
    conv.add_assistant_message([ToolUseBlock(id="t1", name="ReadFile", input={"path": "a.py"})])
    conv.add_tool_results([ToolResult(tool_use_id="t1", name="ReadFile", content="文件内容")])
    messages = conv.messages
    kept = _recent_keep(messages, recent_keep_tokens=1, min_messages=1)
    # 边界落在 tool_result（最后一条）→ 前扩把 assistant tool_use 一并收进
    assert kept[-1] is messages[-1]
    assert any(isinstance(b, ToolUseBlock) for m in kept for b in m.content)


async def test_compact_snapshots_file_and_todo(tmp_path: Path) -> None:
    """压缩后恢复快照：会话路径 + 最近访问文件 + todo 快照重灌（12 §3.2）。"""
    conv = ConversationManager()
    conv.restore(
        [
            Message(role="user", content=[TextBlock("早期需求：实现读取")]),
            Message(
                role="assistant",
                content=[ToolUseBlock(id="t1", name="ReadFile", input={"path": "a.py"})],
            ),
            Message(
                role="user",
                content=[ToolResultBlock(tool_use_id="t1", content="文件内容……")],
            ),
            Message(role="user", content=[TextBlock("收尾")]),
        ]
    )
    llm = FakeLLM([_summary_batch("<analysis>草稿</analysis>\n<summary>已完成读取实现</summary>")])
    c = Compactor(llm, system_prompt="sys", recent_keep_tokens=1, recent_keep_min_messages=1)
    todos = [TodoItemRecord(content="任务A", status="in_progress", group="目标1")]
    result = await c.compact(conv, session_path="/sess/s.jsonl", todos=todos)

    block = result.summary_message.content[0]
    assert isinstance(block, TextBlock)
    assert "已完成读取实现" in block.text
    assert "--- 最近访问文件快照 ---" in block.text
    assert "a.py" in block.text and "文件内容" in block.text
    assert "--- 当前 todo 快照 ---" in block.text
    assert "[→]" in block.text and "任务A" in block.text and "（目标1）" in block.text


def test_render_todo_snapshot_format() -> None:
    """快照保真：状态标记 + group 前缀 + steps 缩进（12 §3.2）。"""
    todos = [
        TodoItemRecord(content="目标A", status="pending", steps=[StepRecord(description="步骤1")]),
        TodoItemRecord(content="任务1", status="in_progress"),
        TodoItemRecord(content="任务2", status="completed", group="目标A"),
    ]
    text = render_todo_snapshot(todos)
    assert "- [ ] 目标A" in text
    assert "    - 步骤1" in text
    assert "- [→] 任务1" in text
    assert "- [x] （目标A）任务2" in text


def test_summary_payload_includes_focus() -> None:
    """/compact 带参（M2-e）：focus 注入摘要指令——保留重点，其余与自动压缩逐字节一致。"""
    llm = FakeLLM([])
    c = _small_compactor(llm)
    payload = c._build_summary_payload(
        [Message(role="user", content=[TextBlock("历史")])], None, focus="bug A"
    )
    last = payload.messages[-1].content[-1]
    assert isinstance(last, TextBlock)
    assert "保留重点" in last.text and "bug A" in last.text
    # 无 focus 时指令与自动压缩完全相同（不含保留重点追加段）
    plain = c._build_summary_payload([], None)
    assert "保留重点" not in str(plain.messages[-1].content[-1])


# ---- 独立预算 / 熔断 / 复位（01 §6） ---------------------------------------


async def test_auto_compact_failure_trips_circuit(tmp_path: Path) -> None:
    """auto 连续失败 3 次 → 熔断自动路径（只关自动，不关强制）。"""
    llm = _BoomLLM()
    cm = ContextManager(tmp_path, session_id="s", llm=llm, compactor=_small_compactor(llm))
    conv = _conv()
    for _ in range(3):
        assert await cm.auto_compact(conv) is None
    assert cm.auto_fail == 3
    assert cm.circuit_open is True
    # 熔断后 AUTO 被挡、FORCE 仍可执行（check_before_call 不受 auto 预算影响）
    assert cm.check_before_call(170_000) == "NORMAL"
    assert cm.check_before_call(180_000) == "FORCE_COMPACT"


async def test_force_compact_budget_exhausted_raises(tmp_path: Path) -> None:
    """force 连续失败 3 次 → 预算耗尽抛 ContextFullError（01 §6 保底）。"""
    llm = _BoomLLM()
    cm = ContextManager(tmp_path, session_id="s", llm=llm, compactor=_small_compactor(llm))
    conv = _conv()
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cm.force_compact(conv)
    assert cm.force_fail == 3
    with pytest.raises(ContextFullError):
        await cm.force_compact(conv)


async def test_success_resets_budgets(tmp_path: Path) -> None:
    """任何一次压缩成功 → 双计数清零、熔断复位（01 §6.1）。"""
    llm = _ScriptedLLM(failures=6, summary="压完了")
    cm = ContextManager(tmp_path, session_id="s", llm=llm, compactor=_small_compactor(llm))
    conv = _conv()
    assert await cm.auto_compact(conv) is None  # 4 次重试全失败 → auto_fail=1
    assert cm.auto_fail == 1
    result = await cm.auto_compact(conv)  # 失败 2 次后成功
    assert result is not None
    assert cm.auto_fail == 0
    assert cm.force_fail == 0
    assert cm.circuit_open is False
    # restore 生效：摘要消息成为历史首条
    block = conv.messages[0].content[0]
    assert isinstance(block, TextBlock) and "压完了" in block.text


async def test_compact_rewrites_session_file(tmp_path: Path) -> None:
    """压缩后整体重写会话 JSONL：早期原文被摘要替代、尾部保留、内存与文件一致。"""
    llm = FakeLLM([_summary_batch("<analysis>草稿</analysis>\n<summary>已压缩早期</summary>")])
    cm = ContextManager(tmp_path, session_id="s-w", llm=llm, compactor=_small_compactor(llm))
    conv = _conv()
    await cm.force_compact(conv)

    file = tmp_path / "s-w.jsonl"
    assert file.exists()
    content = file.read_text(encoding="utf-8")
    assert "<context-summary>" in content
    assert "内容 0" not in content  # 早期（被摘要）
    assert "内容 2" in content  # 尾部（保留）
    # 内存与文件一致：每行一条记录
    assert len(conv.messages) == len([ln for ln in content.splitlines() if ln.strip()])


# ---- Agent 端到端：阶段 A 预防 / 阶段 B 紧急 ---------------------------------


async def test_agent_auto_compact_before_call(tmp_path: Path) -> None:
    """阶段 A：每轮 API 前 AUTO 触发压缩，摘要进历史 + 文件重写，主调用照常。"""
    work_dir = tmp_path / "proj"
    work_dir.mkdir()
    sessions_dir = tmp_path / "sessions"
    llm = FakeLLM(
        [
            _summary_batch("<analysis>草稿</analysis>\n<summary>早期上下文摘要</summary>"),
            done("完成"),
        ]
    )
    compactor = Compactor(
        llm,
        system_prompt="sys",
        window_size=100_000,
        summary_output_reserve=0,
        safety_margin=90_000,  # auto=10K，force=99K：预载会话落在 [10K, 99K) → AUTO
        force_extra_margin=1_000,
        recent_keep_tokens=5,
        recent_keep_min_messages=2,
    )
    cm = ContextManager(sessions_dir, session_id="s-a", llm=llm, compactor=compactor)
    conversation = ConversationManager()
    conversation.restore(
        [
            Message(role="user", content=[TextBlock("早期内容 " + "x" * 4000)])
            for _ in range(14)
        ]
    )
    agent = Agent(
        config=Config(),
        llm=llm,
        conversation=conversation,
        tools=build_default_registry(),
        events=lambda _ev: None,
        work_dir=work_dir,
        session_id="s-a",
        context_manager=cm,
    )
    await agent.run("继续")

    assert llm.call_count == 2  # 摘要 + 主调用
    texts = [b.text for m in conversation.messages for b in m.content if isinstance(b, TextBlock)]
    assert any("<context-summary>" in t for t in texts)
    assert any("早期上下文摘要" in t for t in texts)
    assert any(t == "完成" for t in texts)
    file = sessions_dir / "s-a.jsonl"
    assert file.exists() and "<context-summary>" in file.read_text(encoding="utf-8")


async def test_agent_emergency_compact_on_prompt_too_long(tmp_path: Path) -> None:
    """阶段 B：prompt_too_long 撞墙 → 紧急压缩（force 预算）→ 重建 payload 重试一次。"""
    work_dir = tmp_path / "proj"
    work_dir.mkdir()
    sessions_dir = tmp_path / "sessions"
    llm = FakeLLM(
        [
            [LLMStreamEvent(type="error", error=PromptTooLongError("maximum context length"))],
            _summary_batch("<analysis>草稿</analysis>\n<summary>上下文已压缩</summary>"),
            done("收尾完成"),
        ]
    )
    cm = ContextManager(
        sessions_dir, session_id="s-e", llm=llm, compactor=_small_compactor(llm)
    )
    conversation = ConversationManager()
    conversation.restore(
        [
            Message(role="user", content=[TextBlock(f"第 {i} 条内容 " + "x" * 200)])
            for i in range(4)
        ]
    )
    agent = Agent(
        config=Config(),
        llm=llm,
        conversation=conversation,
        tools=build_default_registry(),
        events=lambda _ev: None,
        work_dir=work_dir,
        session_id="s-e",
        context_manager=cm,
    )
    await agent.run("继续")

    assert llm.call_count == 3  # 主调用(超长) + 摘要 + 主调用(重试)
    texts = [b.text for m in conversation.messages for b in m.content if isinstance(b, TextBlock)]
    assert any("<context-summary>" in t for t in texts)
    assert any("上下文已压缩" in t for t in texts)
    assert any("收尾完成" in t for t in texts)


# ---- 恢复（04 §5 ③④）：超限触发压缩 + todo 快照重灌 -------------------------


def test_resume_triggers_compact_and_todo_snapshot(tmp_path: Path) -> None:
    """resume ③：token 超阈触发压缩回调；④：todo 快照以 system-reminder 重灌（时点④）。"""
    mgr = SessionManager(tmp_path)
    session = mgr.create()
    for _ in range(6):
        session.append_user("x" * 2000)
    session.set_todos([TodoItemRecord(content="任务A", status="in_progress")])
    session.append_user("最后一条")
    called: list[ConversationManager] = []
    resumed = mgr.resume(session.id, compact_threshold=100, compact=called.append)
    assert called and called[0] is resumed.conversation
    assert any(_is_todo_snapshot_message(m) for m in resumed.conversation.messages)
    assert resumed.todos is not None and resumed.todos[0].content == "任务A"


def test_resume_no_compact_under_threshold(tmp_path: Path) -> None:
    """token 未超阈 → 不触发压缩（回调不被调用）。"""
    mgr = SessionManager(tmp_path)
    session = mgr.create()
    session.append_user("小会话")
    called: list[ConversationManager] = []
    resumed = mgr.resume(session.id, compact_threshold=10_000, compact=called.append)
    assert called == []
    assert not any(_is_todo_snapshot_message(m) for m in resumed.conversation.messages)


def test_resume_skips_duplicate_todo_snapshot(tmp_path: Path) -> None:
    """文件已含 todo 快照消息 → 不再重复注入（防多次恢复累积）。"""
    mgr = SessionManager(tmp_path)
    session = mgr.create()
    session.set_todos([TodoItemRecord(content="任务A")])
    session.append_user("", extra_blocks=[TextBlock(f"{TODO_SNAPSHOT_MARKER}\n- [ ] 任务A")])
    resumed = mgr.resume(session.id)
    count = sum(1 for m in resumed.conversation.messages if _is_todo_snapshot_message(m))
    assert count == 1
