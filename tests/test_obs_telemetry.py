"""07 Telemetry 统一 sink 测试（规格 07 §3.2：自动父子 / 实时落盘 / 异常 / 脱敏）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from kdagent.obs.telemetry import Telemetry


def _read_spans(obs_dir: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for f in (obs_dir / "traces").glob("*/**/*.jsonl"):
        rows.extend(json.loads(line) for line in f.read_text(encoding="utf-8").splitlines())
    return rows


def test_span_nesting_parent_and_root(tmp_path: Path) -> None:
    telemetry = Telemetry(tmp_path)
    telemetry.begin_trace("s1", "hello")
    with telemetry.span("trace.run", "session") as root:
        root_id = root.span_id if root else ""
        with telemetry.span("llm.call", "client") as child:
            assert child and child.parent_span_id == root_id  # 自动父子
    telemetry.end_trace()

    rows = _read_spans(tmp_path)
    spans = [r for r in rows if r["_type"] == "span"]
    assert len(spans) == 2
    root_row = next(r for r in spans if r["name"] == "trace.run")
    llm_row = next(r for r in spans if r["name"] == "llm.call")
    assert root_row["parent_span_id"] is None
    assert llm_row["parent_span_id"] == root_id
    assert llm_row["status"] == "ok"
    assert llm_row["duration_ms"] >= 0


def test_span_error_records_stack_and_status(tmp_path: Path) -> None:
    telemetry = Telemetry(tmp_path)
    telemetry.begin_trace("s1", "x")
    with pytest.raises(RuntimeError), telemetry.span("llm.call", "client"):
        raise RuntimeError("boom")
    telemetry.end_trace()

    llm_row = next(r for r in _read_spans(tmp_path) if r["_type"] == "span")
    assert llm_row["status"] == "error"
    assert any("boom" in log["message"] for log in llm_row["logs"])


def test_span_without_trace_is_dropped(tmp_path: Path) -> None:
    telemetry = Telemetry(tmp_path)
    with telemetry.span("llm.call", "client") as span:
        assert span is None  # 无活动 trace → 游离 span 丢弃
    assert (tmp_path / "traces").exists() is False


def test_disabled_telemetry_noop(tmp_path: Path) -> None:
    telemetry = Telemetry(tmp_path, enabled=False)
    telemetry.begin_trace("s1", "x")  # type: ignore[func-returns-value]
    with telemetry.span("llm.call", "client") as span:
        assert span is None
    assert (tmp_path / "traces").exists() is False


def test_add_log_attaches_to_span(tmp_path: Path) -> None:
    telemetry = Telemetry(tmp_path)
    telemetry.begin_trace("s1", "x")
    with telemetry.span("llm.call", "client") as span:
        assert span is not None
        telemetry.add_log(span.span_id, "debug", "prompt 摘要")
    telemetry.end_trace()

    llm_row = next(r for r in _read_spans(tmp_path) if r["_type"] == "span")
    assert len(llm_row["logs"]) == 1
    assert llm_row["logs"][0]["level"] == "debug"
    assert llm_row["logs"][0]["message"] == "prompt 摘要"


def test_sanitize_applied_at_export(tmp_path: Path) -> None:
    telemetry = Telemetry(tmp_path, sanitize_rules={"secret": "***"})
    telemetry.begin_trace("s1", "含 secret 的输入")
    with telemetry.span("llm.call", "client", {"prompt": "a secret here"}) as span:
        assert span is not None
    telemetry.end_trace()

    header = next(r for r in _read_spans(tmp_path) if r["_type"] == "trace")
    span_row = next(r for r in _read_spans(tmp_path) if r["_type"] == "span")
    assert "***" in header["user_input_snapshot"]  # type: ignore[operator]
    assert span_row["attributes"]["prompt"] == "a *** here"


# ---- 10 §5 342（D78）：子 Agent trace 挂父 ----

def test_begin_trace_parent_records_header(tmp_path: Path) -> None:
    """begin_trace 传 parent → header 落 parent_trace_id/parent_span_id（挂父链）。"""
    telemetry = Telemetry(tmp_path)
    telemetry.begin_trace(
        "s1", "child", parent_trace_id="parent-123", parent_span_id="parent-span-1"
    )
    telemetry.end_trace()

    header = next(r for r in _read_spans(tmp_path) if r["_type"] == "trace")
    assert header["parent_trace_id"] == "parent-123"
    assert header["parent_span_id"] == "parent-span-1"
    assert header["trace_id"]  # 子 trace 自身 id 仍独立


def test_begin_trace_default_parent_empty(tmp_path: Path) -> None:
    """未传 parent → 空串（根 trace，无父）。"""
    telemetry = Telemetry(tmp_path)
    telemetry.begin_trace("s1", "root")
    telemetry.end_trace()

    header = next(r for r in _read_spans(tmp_path) if r["_type"] == "trace")
    assert header["parent_trace_id"] == ""
    assert header["parent_span_id"] == ""


def test_current_context_reads_active_trace(tmp_path: Path) -> None:
    """current_context 返回当前 (trace_id, span_id)——委派点读父 trace 的入口。"""
    telemetry = Telemetry(tmp_path)
    # 无活动 trace → ("", "", "")
    assert telemetry.current_context() == ("", "", "")

    telemetry.begin_trace("s1", "parent")
    with telemetry.span("trace.run", "session") as root:
        assert root is not None
        trace_id, span_id, session_id = telemetry.current_context()
        assert trace_id  # 读到父 trace_id
        assert span_id == root.span_id  # 读到当前 span
        assert session_id == "s1"  # 会话归属（子 trace 落父会话目录）
    telemetry.end_trace()

    # end_trace 后上下文恢复 → 空（防跨 trace 残留）
    assert telemetry.current_context() == ("", "", "")


def test_current_context_disabled_returns_empty(tmp_path: Path) -> None:
    """未启用 telemetry → current_context 恒空（no-op 零开销）。"""
    telemetry = Telemetry(tmp_path, enabled=False)
    assert telemetry.current_context() == ("", "", "")


# ---- 并发子 Agent：共享实例不串 trace token（2026-08-28 f45c 实测崩溃） ----

async def test_concurrent_subagent_traces_no_cross_context_crash(tmp_path: Path) -> None:
    """并发子 Agent（create_task 各复制 context）共享同一 Telemetry → 不崩、父 trace 存活。

    复现 f45c「Agent 异常：Token ... was created in a different Context」：原
    `self._trace_token` 是共享实例属性，后 begin 的子任务覆盖它，先 end 的子任务
    拿别人 Context 的 token 去 reset → RuntimeError 击穿主循环。改为 context-local
    token 栈后：每个子任务 push/pop 自己的栈，互不覆盖，父 trace 上下文不受影响。
    """
    telemetry = Telemetry(tmp_path)
    telemetry.begin_trace("parent", "父输入")
    parent_trace_id = telemetry.current_context()[0]

    async def child(name: str) -> str:
        telemetry.begin_trace(name, name)
        # 模拟子 Agent 跑 LLM/工具：让出控制权使 begin/end 跨任务交错
        for _ in range(3):
            await asyncio.sleep(0)
        sub_trace_id = telemetry.current_context()[0]
        telemetry.end_trace()
        return sub_trace_id

    tasks = [asyncio.create_task(child(n)) for n in ("c1", "c2", "c3")]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    # 子任务均正常完成（旧实现：两个子任务抛「was created in a different Context」）
    assert all(not isinstance(r, BaseException) for r in results)
    # 子 trace 各自独立，且与父不同
    assert len({r for r in results}) == 3
    assert all(r and r != parent_trace_id for r in results)
    # 父 trace 上下文在并发子任务后仍存活、id 未被覆盖
    assert telemetry.current_context()[0] == parent_trace_id
    telemetry.end_trace()
    assert telemetry.current_context() == ("", "", "")


async def test_nested_begin_trace_same_context_restores(tmp_path: Path) -> None:
    """同 context 内嵌套 begin/end（父 → 子 → 父）逐层恢复，不丢外层。"""
    telemetry = Telemetry(tmp_path)
    telemetry.begin_trace("outer", "外层")
    outer_id = telemetry.current_context()[0]

    telemetry.begin_trace("inner", "内层")
    inner_id = telemetry.current_context()[0]
    assert inner_id != outer_id
    telemetry.end_trace()  # 弹回外层

    assert telemetry.current_context()[0] == outer_id
    telemetry.end_trace()
    assert telemetry.current_context() == ("", "", "")
