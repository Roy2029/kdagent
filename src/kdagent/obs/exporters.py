"""SpanExporter 协议与落盘实现（规格 07 §3.2/§3.9）。

默认 `JsonlExporter`：`{kdagent_dir}/obs/traces/{sid}/{trace_id}.jsonl`，每行一个 span，
头行（`_type: "trace"`）含输入摘要，span 树用 `parent_span_id` 重建。
OTLP 留接口（D2：`otel.enabled` 可插拔，M5 实装）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

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


class OTLPSpanExporter:
    """OTel OTLP 导出占位（M2 仅接口就绪，D2 预留，M5 实装）。"""

    def __init__(self, endpoint: str = "") -> None:
        self._endpoint = endpoint

    def export_trace_header(self, trace: Trace) -> None:  # pragma: no cover - M5 实装
        raise NotImplementedError("OTLP 导出在 M5（生产级）实装")

    def export(self, trace: Trace, span: Span) -> None:  # pragma: no cover - M5 实装
        raise NotImplementedError("OTLP 导出在 M5（生产级）实装")
