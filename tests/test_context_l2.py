"""M2-b L2 在线摘要测试（规格 01 §5.3 / §11 第 3 项）。

覆盖：三门槛决策（作用域/信息密度/经济性）、原文落盘（D6）、两阶段摘要生成、
`<compressed-output>` 历史形态、Agent 端到端、失败回退。
"""

from __future__ import annotations

from pathlib import Path

from conftest import FakeLLM, done, tool_call

from kdagent.config import Config
from kdagent.context.compactor import (
    AVG_GROWTH_PER_TURN,
    TOOL_RESULT_SAVE_THRESHOLD_TOKENS,
    WINDOW_SIZE,
    CompressedOutput,
    CostParams,
    L2Compressor,
    should_online_compress,
)
from kdagent.context.context_manager import ContextManager
from kdagent.context.history import ProcessedToolResult
from kdagent.context.tool_result_handler import ToolResultHandler
from kdagent.engine.agent import Agent
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import LLMStreamEvent
from kdagent.engine.messages import ToolResultBlock
from kdagent.tools import build_default_registry
from kdagent.tools.base import ToolResult


def _long_log(n: int = 800) -> str:
    """低密度日志：35.2K 字符 → 8.8K token（≥ ONLINE_COMPRESS_MIN，< L1 落盘阈值）。"""
    return "[ERROR] worker-3: timeout connecting to db\n" * n


def _log_result(content: str, tool_use_id: str = "t1") -> ToolResult:
    return ToolResult(tool_use_id=tool_use_id, name="Bash", content=content)


def _summary_batch(text: str) -> list[LLMStreamEvent]:
    return [
        LLMStreamEvent(type="text_delta", text=text),
        LLMStreamEvent(type="stop", stop_reason="end_turn"),
    ]


# ---- 决策函数：三门槛 -------------------------------------------------------


def test_should_compress_low_density_long_context() -> None:
    """中等大小 + 低密度 + 回本复用次数够 → 触发摘要（§11 第 3 项）。"""
    result = _log_result(_long_log())
    cost = CostParams(c_in=1.0, c_out=1.0, c_hit=0.1)
    assert should_online_compress(result, p_tokens=50_000, expected_remaining=50, cost=cost) is True


def test_code_result_high_density_skips() -> None:
    """代码类结果 → HIGH 密度不压缩（§11 第 3 项）。"""
    code = "def f(n):\n    return n * 2\n\n" * 1600  # 40K 字符 → 10K token，X 在范围内
    result = ToolResult(tool_use_id="t1", name="ReadFile", content=code)
    assert should_online_compress(result, p_tokens=50_000, expected_remaining=100) is False


def test_below_online_compress_min_skips() -> None:
    """X < ONLINE_COMPRESS_MIN → 太小不值得（作用域门槛）。"""
    result = _log_result(_long_log(n=400))  # 17.6K 字符 → 4.4K token
    assert should_online_compress(result, p_tokens=50_000, expected_remaining=100) is False


def test_at_or_above_save_threshold_skips() -> None:
    """X ≥ 落盘阈值 → 走 L1，不触发 L2（作用域门槛上界）。"""
    result = _log_result(_long_log(n=1200))  # 52.8K 字符 → 13.2K token ≥ 12.5K
    assert should_online_compress(result, p_tokens=50_000, expected_remaining=100) is False
    assert TOOL_RESULT_SAVE_THRESHOLD_TOKENS == 12_500


def test_break_even_not_met_skips() -> None:
    """回本复用次数不够 → 经济性不通过，不压缩。"""
    result = _log_result(_long_log())
    cost = CostParams(c_in=1.0, c_out=1.0, c_hit=0.1)
    # break_even ≈ 17.5（同参数下 test_should_compress 用 50 通过），5 次复回不了本
    assert should_online_compress(result, p_tokens=50_000, expected_remaining=5, cost=cost) is False


def test_expected_remaining_default_from_window() -> None:
    """无 expected_remaining → 用剩余窗口 / 每轮平均增长估算复用轮数。"""
    result = _log_result(_long_log())
    estimated = (WINDOW_SIZE - 50_000) // AVG_GROWTH_PER_TURN  # 75
    assert estimated >= 50
    assert (
        should_online_compress(
            result, p_tokens=50_000, cost=CostParams(c_in=1.0, c_out=1.0, c_hit=0.1)
        )
        is True
    )


def test_small_p_tokens_still_break_even() -> None:
    """P 很小（会话刚起步）→ 缓存命中省得少，但压缩成本也低，通常仍值得。"""
    result = _log_result(_long_log())
    cost = CostParams(c_in=1.0, c_out=1.0, c_hit=0.1)
    assert should_online_compress(result, p_tokens=100, expected_remaining=50, cost=cost) is True


# ---- 摘要生成：落盘 + 两阶段 + 历史形态 ------------------------------------


async def test_compress_persists_original_and_returns_compressed_output(tmp_path: Path) -> None:
    """compress：原文落盘（D6）+ 两阶段取 <summary> 正文；历史无原文（§11 第 3 项）。"""
    llm = FakeLLM(
        [
            _summary_batch(
                "<analysis>日志噪声</analysis>\n<summary>42 条 ERROR，集中在 db 超时</summary>"
            )
        ]
    )
    compressor = L2Compressor(llm, persist_dir=tmp_path)
    result = _log_result(_long_log())
    processed = await compressor.compress(result)

    assert isinstance(processed, ProcessedToolResult)
    co = processed.compressed
    assert co is not None
    assert isinstance(co, CompressedOutput)
    assert "42 条 ERROR" in co.summary
    assert "<analysis>" not in co.summary  # 草稿被丢弃
    assert co.original_type == "log"
    assert co.info_density == "LOW"
    assert processed.persisted is not None
    # 原文落盘
    written = Path(processed.persisted.path).read_text(encoding="utf-8")
    assert written == result.content
    # 历史形态：<compressed-output> + 摘要，无原文
    assert "<compressed-output>" in processed.content
    assert "42 条 ERROR" in processed.content
    assert "timeout connecting to db" not in processed.content
    assert llm.call_count == 1


async def test_compress_two_phase_without_tags(tmp_path: Path) -> None:
    """模型没输出 <summary> 标签 → 剥掉 <analysis> 草稿取余下纯文本。"""
    llm = FakeLLM([_summary_batch("<analysis>草稿</analysis>纯文本摘要，无标签")])
    compressor = L2Compressor(llm, persist_dir=tmp_path)
    processed = await compressor.compress(_log_result(_long_log()))
    assert processed.compressed is not None
    assert processed.compressed.summary == "纯文本摘要，无标签"


# ---- 处理器接线 ------------------------------------------------------------


async def test_handler_routes_l2_on_low_density(tmp_path: Path) -> None:
    """handler 接线：低密度长结果 → L2 摘要；小结果原样且不调用 LLM。"""
    llm = FakeLLM([_summary_batch("<summary>db 超时集中</summary>")])
    l2 = L2Compressor(llm, persist_dir=tmp_path)
    handler = ToolResultHandler(tmp_path, l2=l2)

    processed = await handler.handle_single(_log_result(_long_log()), p_tokens=50_000)
    assert processed.compressed is not None
    assert "<compressed-output>" in processed.content

    processed_small = await handler.handle_single(_log_result("ok"), p_tokens=50_000)
    assert processed_small.compressed is None
    assert processed_small.content == "ok"
    assert llm.call_count == 1  # 只有大日志触发了一次摘要调用


async def test_handler_l2_skips_for_persist_exempt_readback(tmp_path: Path) -> None:
    """ReadFile 读回落盘文件（persist_exempt）→ 跳过 L2，不摘要原文（读回闭环延伸）。"""
    llm = FakeLLM([_summary_batch("<summary>不应被调用</summary>")])
    l2 = L2Compressor(llm, persist_dir=tmp_path)
    handler = ToolResultHandler(tmp_path, l2=l2)
    readback = ToolResult(
        tool_use_id="t2", name="ReadFile", content=_long_log(), persist_exempt=True
    )
    processed = await handler.handle_single(readback, p_tokens=50_000)
    assert processed.compressed is None
    assert processed.persisted is None
    assert processed.content == readback.content
    assert llm.call_count == 0


async def test_handler_l2_failure_falls_back_to_original(tmp_path: Path) -> None:
    """摘要调用失败（error 事件）→ 回退原文，不打断循环。"""
    llm = FakeLLM([[LLMStreamEvent(type="error", error=RuntimeError("boom"))]])
    l2 = L2Compressor(llm, persist_dir=tmp_path)
    handler = ToolResultHandler(tmp_path, l2=l2)
    result = _log_result(_long_log())
    processed = await handler.handle_single(result, p_tokens=50_000)
    assert processed.compressed is None
    assert processed.content == result.content


async def test_context_manager_wires_l2_via_llm(tmp_path: Path) -> None:
    """ContextManager 传 llm → 落盘目录与 L2 摘要均按会话装配。"""
    llm = FakeLLM([_summary_batch("<summary>db 超时</summary>")])
    cm = ContextManager(tmp_path, session_id="s-1", llm=llm, system_prompt="sys")
    processed = await cm.on_tool_result(_log_result(_long_log()), p_tokens=50_000)
    assert processed.compressed is not None
    assert processed.compressed.path is not None
    assert Path(processed.compressed.path).parent == tmp_path / "s-1" / "tool-results"


# ---- Agent 端到端 ----------------------------------------------------------


async def test_agent_l2_summarizes_tool_result_to_history(tmp_path: Path) -> None:
    """端到端：ReadFile 读到低密度长日志 → L2 摘要进历史，原文落盘。"""
    work_dir = tmp_path / "proj"
    work_dir.mkdir()
    big_log = work_dir / "app.log"
    big_log.write_text(_long_log(), encoding="utf-8")

    sessions_dir = tmp_path / "sessions"
    llm = FakeLLM(
        [
            tool_call("ReadFile", {"path": str(big_log)}),
            _summary_batch("<summary>db 超时集中，共 800 条 ERROR</summary>"),
            done("完成"),
        ]
    )
    conversation = ConversationManager()
    agent = Agent(
        config=Config(),
        llm=llm,
        conversation=conversation,
        tools=build_default_registry(),
        events=lambda _ev: None,  # type: ignore[arg-type]
        work_dir=work_dir,
        session_id="s-l2",
        context_manager=ContextManager(
            sessions_dir, session_id="s-l2", llm=llm, system_prompt="sys"
        ),
    )
    await agent.run("读 app.log")

    tool_blocks = [
        block
        for msg in conversation.messages
        for block in msg.content
        if isinstance(block, ToolResultBlock)
    ]
    assert len(tool_blocks) == 1
    content = tool_blocks[0].content
    assert "<compressed-output>" in content
    assert "db 超时集中" in content
    assert "timeout connecting to db" not in content
    # 原文落盘存在（ReadFile 带行号前缀，落盘的是带行号的完整内容）
    persisted_files = list((sessions_dir / "s-l2" / "tool-results").glob("*.txt"))
    assert len(persisted_files) == 1
    persisted = persisted_files[0].read_text(encoding="utf-8")
    assert "timeout connecting to db" in persisted
