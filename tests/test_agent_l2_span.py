"""L2 压缩路径 context.l2_compress span（07 §3.6 T8 标定数据源，D69）。

T8 验收 268「L2 压缩成本数据可聚合」的数据源：每次 L2 在线压缩产出 span，
attributes 含 X/S token（压缩前/后估算）+ LLM usage（in/out/cache），按 provider
（trace 根）聚合可标定 C_in/C_out/C_hit。
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import FakeLLM

from kdagent.context.compactor import CostParams, L2Compressor
from kdagent.context.context_manager import ContextManager
from kdagent.context.tool_result_handler import ToolResultHandler
from kdagent.engine.llm.base import LLMStreamEvent, Usage
from kdagent.obs.telemetry import Telemetry
from kdagent.tools.base import ToolResult


def _trace_rows(obs_dir: Path) -> list[dict]:
    files = list((obs_dir / "traces").glob("*/*.jsonl"))
    assert len(files) == 1, f"期望 1 个 trace 文件，实际 {len(files)}"
    return [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]


def _summarize_llm() -> FakeLLM:
    """摘要调用响应：usage + 两阶段 <summary> 文本 + stop。"""
    return FakeLLM(
        [
            [
                LLMStreamEvent(
                    type="usage",
                    usage=Usage(input_tokens=100, output_tokens=50, cache_read_tokens=20, cache_creation_tokens=10),
                ),
                LLMStreamEvent(type="text_delta", text="<summary>已压缩的摘要内容</summary>"),
                LLMStreamEvent(type="stop", stop_reason="end_turn"),
            ]
        ]
    )


def _result() -> ToolResult:
    return ToolResult(tool_use_id="r1", name="Grep", content="行 1\n行 2\n" * 200)


async def test_l2_compress_emits_span(tmp_path: Path) -> None:
    """L2 压缩产 span：X/S token + usage 齐全，status=ok。"""
    obs_dir = tmp_path / "obs"
    telemetry = Telemetry(obs_dir)
    l2 = L2Compressor(_summarize_llm(), persist_dir=tmp_path / "persist", telemetry=telemetry)
    telemetry.begin_trace("s-l2", "l2 span test")  # span 无 trace 根时丢弃，需先建根
    try:
        out = await l2.compress(_result())
    finally:
        telemetry.end_trace()
    assert out.compressed is not None

    rows = _trace_rows(obs_dir)
    spans = [r for r in rows if r.get("_type") == "span" and r["name"] == "context.l2_compress"]
    assert len(spans) == 1
    attrs = spans[0]["attributes"]
    assert attrs["tool_use_id"] == "r1"
    assert attrs["x_tokens"] > 0  # 压缩前原文 token
    assert attrs["s_tokens"] > 0  # 压缩后摘要 token
    assert attrs["input_tokens"] == 100
    assert attrs["output_tokens"] == 50
    assert attrs["cache_read_tokens"] == 20
    assert spans[0]["status"] == "ok"


async def test_l2_compress_no_telemetry_unchanged(tmp_path: Path) -> None:
    """telemetry=None 时压缩行为不变（nullcontext 兜底，不产 span 不报错）。"""
    l2 = L2Compressor(_summarize_llm(), persist_dir=tmp_path / "persist")
    out = await l2.compress(_result())
    assert out.compressed is not None
    assert not list((tmp_path / "obs").glob("**/*"))


async def test_context_manager_passes_telemetry_to_l2(tmp_path: Path) -> None:
    """CM 装配的 L2 压缩器携带 telemetry（set_telemetry 后补接线）→ 压缩产 span。"""
    obs_dir = tmp_path / "obs"
    telemetry = Telemetry(obs_dir)
    llm = _summarize_llm()
    cm = ContextManager(sessions_dir=tmp_path / "sessions", llm=llm, telemetry=telemetry)
    handler = cm._tool_result_handler()  # 惰性装配：L2 带 telemetry
    assert handler._l2 is not None
    telemetry.begin_trace("s-cm", "cm l2 span test")
    try:
        out = await handler._l2.compress(_result())
    finally:
        telemetry.end_trace()
    assert out.compressed is not None
    rows = _trace_rows(obs_dir)
    assert any(
        r.get("name") == "context.l2_compress"
        for r in rows
        if r.get("_type") == "span"
    )


def _low_density_log() -> ToolResult:
    """低密度日志：35.2K 字符 → 8.8K token（在 L2 窗口内，log 类型）。"""
    return ToolResult(
        tool_use_id="r2", name="Bash", content="[ERROR] worker-3: timeout connecting to db\n" * 800
    )


async def test_l2_decide_emits_span_with_econ_attrs(tmp_path: Path) -> None:
    """判定点落 context.l2_decide span：reason/密度/类型 + 经济性中间量齐全（07 T8）。"""
    obs_dir = tmp_path / "obs"
    telemetry = Telemetry(obs_dir)
    llm = _summarize_llm()  # decide 不耗响应；compress 消费 1 批
    l2 = L2Compressor(
        llm,
        persist_dir=tmp_path / "persist",
        telemetry=telemetry,
        cost=CostParams(c_in=1.0, c_out=1.0, c_hit=0.1),
    )
    handler = ToolResultHandler(tmp_path, l2=l2)
    telemetry.begin_trace("s-l2d", "l2 decide span test")
    try:
        await handler.handle_single(_low_density_log(), p_tokens=50_000)
    finally:
        telemetry.end_trace()

    rows = _trace_rows(obs_dir)
    decide = [
        r for r in rows if r.get("_type") == "span" and r["name"] == "context.l2_decide"
    ]
    assert len(decide) == 1
    attrs = decide[0]["attributes"]
    assert attrs["tool_use_id"] == "r2"
    assert attrs["tool"] == "Bash"
    assert attrs["reason"] == "compress"
    assert attrs["info_density"] == "LOW"
    assert attrs["original_type"] == "log"
    assert attrs["x_tokens"] > 0
    assert attrs["break_even_n"] > 0  # 经济性中间量
    assert attrs["expected_n"] > 0
    # 判定 accepted → 触发压缩，l2_compress span 也落
    assert any(
        r.get("name") == "context.l2_compress"
        for r in rows
        if r.get("_type") == "span"
    )


async def test_l2_decide_emits_econ_fail_span(tmp_path: Path) -> None:
    """经济性不通过（expected ≤ break_even）→ reason=econ_fail，仍落 span。"""
    obs_dir = tmp_path / "obs"
    telemetry = Telemetry(obs_dir)
    l2 = L2Compressor(
        FakeLLM([]),  # decide 不消费响应；econ_fail 不触发 compress
        persist_dir=tmp_path / "persist",
        telemetry=telemetry,
        cost=CostParams(c_in=1.0, c_out=1.0, c_hit=0.1),
    )
    handler = ToolResultHandler(tmp_path, l2=l2)
    telemetry.begin_trace("s-econ", "l2 econ_fail test")
    try:
        # P 接近窗口顶（195K/200K）→ expected≈2 < break_even≈42 → econ_fail
        await handler.handle_single(_low_density_log(), p_tokens=195_000)
    finally:
        telemetry.end_trace()

    rows = _trace_rows(obs_dir)
    decide = [
        r for r in rows if r.get("_type") == "span" and r["name"] == "context.l2_decide"
    ]
    assert len(decide) == 1
    assert decide[0]["attributes"]["reason"] == "econ_fail"
    assert decide[0]["attributes"]["break_even_n"] >= decide[0]["attributes"]["expected_n"]
    # 未触发压缩 → 无 l2_compress span、无 LLM 调用
    assert not any(
        r.get("name") == "context.l2_compress"
        for r in rows
        if r.get("_type") == "span"
    )
