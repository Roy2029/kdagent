"""07 JsonlExporter / OTLP 占位测试（规格 07 §3.3/§3.9）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kdagent.obs.exporters import JsonlExporter, OTLPSpanExporter
from kdagent.obs.model import Span, SpanLog, Trace


def _trace(sid: str = "s1", tid: str = "t1") -> Trace:
    return Trace(
        trace_id=tid,
        session_id=sid,
        user_input_snapshot="原始输入摘要",
        root_span_id="span1",
        ts=1000,
    )


def _span() -> Span:
    return Span(
        span_id="span1",
        trace_id="t1",
        parent_span_id=None,
        name="llm.call",
        kind="client",
        start_ts=1000,
        end_ts=1010,
        duration_ms=10,
        attributes={"model": "deepseek-chat", "input_tokens": 5},
        logs=[SpanLog(level="info", message="正文", ts=1005)],
    )


def test_jsonl_exporter_layout(tmp_path: Path) -> None:
    exporter = JsonlExporter(tmp_path)
    trace = _trace()
    exporter.export_trace_header(trace)
    exporter.export(trace, _span())

    path = tmp_path / "traces" / "s1" / "t1.jsonl"
    assert path.is_file()
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    header, span_row = lines
    assert header["_type"] == "trace"
    assert header["user_input_snapshot"] == "原始输入摘要"
    assert header["session_id"] == "s1"
    # 10 §5 342（D78）：默认无父 → header 带空串 parent 字段（根 trace）。
    assert header["parent_trace_id"] == ""
    assert header["parent_span_id"] == ""
    assert span_row["_type"] == "span"
    assert span_row["name"] == "llm.call"
    assert span_row["status"] == "ok"
    assert span_row["attributes"]["model"] == "deepseek-chat"
    assert span_row["logs"][0]["message"] == "正文"


def test_jsonl_exporter_sanitizes_at_export(tmp_path: Path) -> None:
    exporter = JsonlExporter(tmp_path, sanitize_rules=[(r"api_key=(\w+)", "api_key=***")])
    span = _span()
    span.attributes["tool"] = "Bash"
    span.attributes["cmd"] = "curl api_key=secret123"
    trace = _trace()
    exporter.export(trace, span)

    row = json.loads(
        (tmp_path / "traces" / "s1" / "t1.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert row["attributes"]["cmd"] == "curl api_key=***"
    assert row["attributes"]["tool"] == "Bash"  # 非敏感值不受影响


def test_otlp_exporter_stub_raises() -> None:
    exporter = OTLPSpanExporter(endpoint="http://localhost:4317")
    with pytest.raises(NotImplementedError):
        exporter.export(_trace(), _span())
    with pytest.raises(NotImplementedError):
        exporter.export_trace_header(_trace())
