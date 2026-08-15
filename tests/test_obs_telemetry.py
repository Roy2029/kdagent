"""07 Telemetry 统一 sink 测试（规格 07 §3.2：自动父子 / 实时落盘 / 异常 / 脱敏）。"""

from __future__ import annotations

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
