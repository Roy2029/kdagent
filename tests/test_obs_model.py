"""07 可观测性数据模型测试（规格 07 §3.1）。"""

from __future__ import annotations

from kdagent.obs.model import Span, SpanLog, Trace, gen_id, now_ms


def test_gen_id_unique() -> None:
    ids = {gen_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(len(i) == 16 for i in ids)  # 16 hex 字符


def test_now_ms_is_positive() -> None:
    assert now_ms() > 0


def test_span_defaults() -> None:
    span = Span(span_id="a", trace_id="t", parent_span_id=None, name="llm.call", kind="client")
    assert span.status == "ok"
    assert span.duration_ms == 0
    assert span.attributes == {}
    assert span.logs == []


def test_span_log_attributes() -> None:
    log = SpanLog(level="error", message="boom", ts=1)
    assert log.level == "error"
    assert log.attributes == {}


def test_trace_holds_spans() -> None:
    trace = Trace(
        trace_id="t",
        session_id="s",
        user_input_snapshot="hello",
        root_span_id="",
    )
    trace.spans.append(Span(span_id="a", trace_id="t", parent_span_id=None, name="x", kind="s"))
    assert len(trace.spans) == 1
    assert trace.ts == 0
    assert trace.attributes == {}
