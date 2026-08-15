"""07 JsonlExporter / OTLP 导出测试（规格 07 §3.3/§3.9 + §5 275 D80）。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from kdagent.config import Config
from kdagent.obs.exporters import (
    JsonlExporter,
    OTLPSpanExporter,
    build_otlp_payload,
)
from kdagent.obs.model import Span, SpanLog, Trace
from kdagent.ui.app import _build_telemetry


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


# ---- OTLP/HTTP JSON 导出（07 §5 275 D80） ------------------------------------


def _trace_hex(sid: str = "s1") -> Trace:
    """OTLP fixture：trace_id 用真实 hex（gen_id 16 hex = 8 bytes，base64 可编解码）。"""
    return Trace(
        trace_id="0123456789abcdef",
        session_id=sid,
        user_input_snapshot="原始输入摘要",
        root_span_id="fedcba9876543210",
        ts=1000,
    )


def _span_hex() -> Span:
    return Span(
        span_id="fedcba9876543210",
        trace_id="0123456789abcdef",
        parent_span_id=None,
        name="llm.call",
        kind="client",
        start_ts=1000,
        end_ts=1010,
        duration_ms=10,
        attributes={"model": "deepseek-chat", "input_tokens": 5},
        logs=[SpanLog(level="info", message="正文", ts=1005)],
    )


def _spans() -> list[dict[str, object]]:
    payload = json.loads(build_otlp_payload(_trace_hex(), _span_hex()))
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def test_build_otlp_payload_span_fields() -> None:
    """OTLP span 语义字段：id base64 往返 / name / kind 映射 / 时间戳 nano。"""
    spans = _spans()
    assert len(spans) == 1
    s = spans[0]
    assert s["name"] == "llm.call"
    assert s["kind"] == 3  # client → OTel CLIENT
    # traceId/spanId base64 解码回原 bytes（gen_id 8 bytes = 16 hex）
    assert base64.b64decode(s["traceId"]) == bytes.fromhex("0123456789abcdef")
    assert base64.b64decode(s["spanId"]) == bytes.fromhex("fedcba9876543210")
    assert s["startTimeUnixNano"] == str(1000 * 1_000_000)  # uint64 → 字符串
    assert s["endTimeUnixNano"] == str(1010 * 1_000_000)
    assert s["status"] == {"code": 1}  # ok → OK(1)


def test_build_otlp_payload_parent_and_kinds() -> None:
    """parent_span_id 存在时落 parentSpanId；error 状态 → ERROR(2)。"""
    span = _span_hex()
    span.parent_span_id = "1111222233334444"
    span.status = "error"
    span.kind = "tool"
    payload = json.loads(build_otlp_payload(_trace_hex(), span))
    s = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert base64.b64decode(s["parentSpanId"]) == bytes.fromhex("1111222233334444")
    assert s["kind"] == 1  # tool → INTERNAL
    assert s["status"] == {"code": 2}  # error → ERROR


def test_build_otlp_payload_attributes_and_resource() -> None:
    """attribute 类型映射 + resource 承载 service/session/跨 trace 父/trace 级属性。"""
    trace = _trace_hex()
    trace.parent_trace_id = "pt1"
    trace.parent_span_id = "ps1"
    trace.attributes = {"eval.run_id": "run-1", "eval.passed": True}
    payload = json.loads(
        build_otlp_payload(
            trace, _span_hex(), session_id="s1", trace_attrs=dict(trace.attributes)
        )
    )
    res = payload["resourceSpans"][0]["resource"]["attributes"]
    by_key = {a["key"]: a["value"] for a in res}
    assert by_key["service.name"] == {"stringValue": "kdagent"}
    assert by_key["kdagent.session_id"] == {"stringValue": "s1"}
    assert by_key["kdagent.parent_trace_id"] == {"stringValue": "pt1"}
    assert by_key["kdagent.parent_span_id"] == {"stringValue": "ps1"}
    assert by_key["kdagent.eval.run_id"] == {"stringValue": "run-1"}
    assert by_key["kdagent.eval.passed"] == {"boolValue": True}

    attrs = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
    a = {x["key"]: x["value"] for x in attrs}
    assert a["model"] == {"stringValue": "deepseek-chat"}
    assert a["input_tokens"] == {"intValue": "5"}  # int64 → protojson 字符串


def test_otlp_exporter_posts_payload(tmp_path: Path) -> None:
    """export 注入 post：URL = {endpoint}/v1/traces，body = OTLP JSON（含 trace header 信息）。"""
    seen: list[tuple[str, bytes]] = []
    exporter = OTLPSpanExporter(
        endpoint="http://localhost:4317",
        post=lambda url, body: seen.append((url, body)),
    )
    trace = _trace_hex()
    exporter.export_trace_header(trace)  # 缓存 session_id → 进 resource
    exporter.export(trace, _span_hex())

    assert len(seen) == 1
    url, body = seen[0]
    assert url == "http://localhost:4317/v1/traces"
    payload = json.loads(body.decode("utf-8"))
    res = payload["resourceSpans"][0]["resource"]["attributes"]
    assert {"key": "kdagent.session_id", "value": {"stringValue": "s1"}} in res


def test_otlp_exporter_no_endpoint_skips() -> None:
    """endpoint 空（otel.enabled 但未配地址）：不发送，零配置可用。"""
    called = False

    def spy(url: str, body: bytes) -> None:
        nonlocal called
        called = True

    exporter = OTLPSpanExporter(post=spy)
    exporter.export_trace_header(_trace_hex())
    exporter.export(_trace_hex(), _span_hex())
    assert called is False


def test_otlp_exporter_failure_is_silent(tmp_path: Path) -> None:
    """上报失败静默：post 抛异常不阻断主流程。"""

    def boom(url: str, body: bytes) -> None:
        raise ConnectionError("collector 不可达")

    exporter = OTLPSpanExporter(endpoint="http://localhost:4317", post=boom)
    exporter.export_trace_header(_trace_hex())
    exporter.export(_trace_hex(), _span_hex())  # 不抛


def test_build_telemetry_otel_enabled_swaps_exporter(tmp_path: Path) -> None:
    """装配点：otel.enabled → OTLPSpanExporter；否则默认 JsonlExporter。"""
    cfg = Config(
        otel={"enabled": True, "endpoint": "http://localhost:4317"},
        obs={"sanitize": {"api_key": "***"}},
    )
    tele = _build_telemetry(cfg, tmp_path)
    assert tele is not None
    assert isinstance(tele._exporter, OTLPSpanExporter)
    assert tele._exporter._endpoint == "http://localhost:4317"

    plain = _build_telemetry(Config(), tmp_path)
    assert plain is not None
    assert isinstance(plain._exporter, JsonlExporter)
