"""SpanExporter 协议与落盘实现（规格 07 §3.2/§3.9）。

默认 `JsonlExporter`：`{kdagent_dir}/obs/traces/{sid}/{trace_id}.jsonl`，每行一个 span，
头行（`_type: "trace"`）含输入摘要，span 树用 `parent_span_id` 重建。
`OTLPSpanExporter`（07 §5 275 D80）：otel.enabled 时替换默认——标准库实现的最小
OTLP/HTTP JSON 导出（无 opentelemetry SDK 依赖），产生合法 protojson payload。
"""

from __future__ import annotations

import base64
import contextlib
import json
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from kdagent.obs.log import redact_dict
from kdagent.obs.model import Span, Trace


class SpanExporter(Protocol):
    """span/trace 出口。M2 默认 JsonlExporter；otel.enabled 可换 OTLP（接口就绪）。"""

    def export_trace_header(self, trace: Trace) -> None: ...

    def export(self, trace: Trace, span: Span) -> None: ...


class JsonlExporter:
    """默认落盘 exporter：按 (sid, trace_id) 一个文件，实时逐 span append。"""

    def __init__(self, obs_dir: Path, sanitize_rules: list[tuple[str, str]] | None = None) -> None:
        self._obs_dir = obs_dir
        self._sanitize_rules = sanitize_rules or []

    def _path(self, trace: Trace) -> Path:
        return self._obs_dir / "traces" / trace.session_id / f"{trace.trace_id}.jsonl"

    def export_trace_header(self, trace: Trace) -> None:
        path = self._path(trace)
        path.parent.mkdir(parents=True, exist_ok=True)
        header = {
            "_type": "trace",
            "trace_id": trace.trace_id,
            "session_id": trace.session_id,
            "user_input_snapshot": redact_dict(
                {"v": trace.user_input_snapshot}, self._sanitize_rules
            )["v"],
            "ts": trace.ts,
            "attributes": trace.attributes,
            # 10 §5 342（D78）：父子 trace 关联——子 Agent trace 记录父 id，重建调用链。
            "parent_trace_id": trace.parent_trace_id,
            "parent_span_id": trace.parent_span_id,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(header, ensure_ascii=False) + "\n")

    def export(self, trace: Trace, span: Span) -> None:
        path = self._path(trace)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "_type": "span",
            "span_id": span.span_id,
            "trace_id": span.trace_id,
            "parent_span_id": span.parent_span_id,
            "name": span.name,
            "kind": span.kind,
            "status": span.status,
            "start_ts": span.start_ts,
            "end_ts": span.end_ts,
            "duration_ms": span.duration_ms,
            "attributes": redact_dict(span.attributes, self._sanitize_rules),
            "logs": [
                {
                    "level": log.level,
                    "message": redact_dict({"v": log.message}, self._sanitize_rules)["v"],
                    "ts": log.ts,
                    "attributes": redact_dict(log.attributes, self._sanitize_rules),
                }
                for log in span.logs
            ],
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")


def _b64(hex_id: str) -> str:
    """hex id（8 bytes，gen_id 16 hex）→ OTLP bytes 字段 base64（protojson standard）。"""
    return base64.b64encode(bytes.fromhex(hex_id)).decode("ascii")


def _nano(ms: int) -> str:
    """ms 时间戳 → OTLP uint64 epoch nanos（protojson 对 int64 编码为字符串）。"""
    return str(ms * 1_000_000)


def _attr_value(v: object) -> dict[str, object]:
    """OTLP AnyValue 映射：str/bool/int/float + list/dict 递归；其他转 str。"""
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, str):
        return {"stringValue": v}
    if isinstance(v, list):
        return {"arrayValue": {"values": [_attr_value(x) for x in v]}}
    if isinstance(v, dict):
        return {
            "kvlistValue": {
                "values": [{"key": str(k), "value": _attr_value(val)} for k, val in v.items()]
            }
        }
    return {"stringValue": str(v)}


def _kind_code(kind: str) -> int:
    """项目 kind（client/tool/permission/hook/context/session）→ OTel SpanKind。
    client（llm.call 外部调用）→ CLIENT(3)；其余项目内操作 → INTERNAL(1)。"""
    if kind == "client":
        return 3
    if kind == "server":
        return 2
    return 1


def _status_code(status: str) -> int:
    """项目 status（ok/error）→ OTel StatusCode：error→ERROR(2)，否则 OK(1)。"""
    return 2 if status == "error" else 1


def build_otlp_payload(
    trace: Trace,
    span: Span,
    *,
    session_id: str = "",
    trace_attrs: dict[str, Any] | None = None,
    service_name: str = "kdagent",
) -> str:
    """单 span 的 OTLP/HTTP JSON 消息（protojson 编码，07 §5 275 D80）。

    resource 承载 service + session + 跨 trace 父引用 + trace 级属性（eval 标记等，
    挂 `kdagent.*` 前缀避免与 OTel 语义键冲突）；span 承载 OTel 语义字段
    （traceId/spanId/parentSpanId/name/kind/status/attributes/时间戳）。
    返回字符串，`OTLPSpanExporter.export` POST 到 `{endpoint}/v1/traces`。
    """
    resource = [{"key": "service.name", "value": {"stringValue": service_name}}]
    if session_id:
        resource.append({"key": "kdagent.session_id", "value": {"stringValue": session_id}})
    if trace.parent_trace_id:
        resource.append(
            {"key": "kdagent.parent_trace_id", "value": {"stringValue": trace.parent_trace_id}}
        )
    if trace.parent_span_id:
        resource.append(
            {"key": "kdagent.parent_span_id", "value": {"stringValue": trace.parent_span_id}}
        )
    for k, v in (trace_attrs or {}).items():
        resource.append({"key": f"kdagent.{k}", "value": _attr_value(v)})

    span_obj: dict[str, object] = {
        "traceId": _b64(span.trace_id),
        "spanId": _b64(span.span_id),
        "name": span.name,
        "kind": _kind_code(span.kind),
        "startTimeUnixNano": _nano(span.start_ts),
        "endTimeUnixNano": _nano(span.end_ts),
        "status": {"code": _status_code(span.status)},
        "attributes": [
            {"key": k, "value": _attr_value(v)} for k, v in span.attributes.items()
        ],
    }
    if span.parent_span_id:
        span_obj["parentSpanId"] = _b64(span.parent_span_id)

    return json.dumps(
        {
            "resourceSpans": [
                {
                    "resource": {"attributes": resource},
                    "scopeSpans": [{"scope": {"name": "kdagent"}, "spans": [span_obj]}],
                }
            ]
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


class OTLPSpanExporter:
    """OTLP/HTTP JSON 导出（07 §3.9/§5 275 D80）：otel.enabled 时替换默认 JsonlExporter。

    标准库实现、无 opentelemetry SDK 依赖：`export_trace_header` 缓存 trace 级信息
    （session_id/trace 属性），`export` 产 OTLP/HTTP JSON payload POST 到
    `{endpoint}/v1/traces`。**上报失败静默**——可观测性故障不阻断主流程
    （contextlib.suppress）。`post` 可注入（测试捕获 payload 免真实网络）。
    生产级接入（gRPC/批处理/重试/OTel Collector 部署）留后续——本实现是
    「接口就绪」的诚实形态：切换后不炸、payload 合法可测、不依赖外部服务。
    """

    def __init__(
        self,
        endpoint: str = "",
        *,
        post: Callable[[str, bytes], None] | None = None,
        service_name: str = "kdagent",
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._post = post or self._http_post
        self._service_name = service_name
        self._session_id = ""
        self._trace_attrs: dict[str, Any] = {}

    def _http_post(self, url: str, payload: bytes) -> None:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2):  # noqa: S310 - 用户配置的 OTLP 端点
            pass

    def export_trace_header(self, trace: Trace) -> None:
        self._session_id = trace.session_id
        self._trace_attrs = dict(trace.attributes)

    def export(self, trace: Trace, span: Span) -> None:
        if not self._endpoint:
            return  # 未配 endpoint：不发送（otel.enabled 但零配置可用）
        payload = build_otlp_payload(
            trace,
            span,
            session_id=self._session_id,
            trace_attrs=self._trace_attrs,
            service_name=self._service_name,
        )
        with contextlib.suppress(Exception):
            self._post(f"{self._endpoint}/v1/traces", payload.encode("utf-8"))
