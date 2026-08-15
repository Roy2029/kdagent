"""评估失败定位的 trace 数据层（07 §3.8 消费方，M5 遗留）。

D55 之后 07 trace 带完整 input；评估子代理接 telemetry 后，每次任务一条 trace
落盘到 obs 目录。本模块读取 JSONL（JsonlExporter 格式），按 eval 标记过滤、重构
span 树、定位失败事件——为「失败归类（11 §3.4）/ trace 排查（11 §3.5）」供数据。

纯读、无副作用；测试隔离用 tmp obs_dir。
"""

from __future__ import annotations

import json
from pathlib import Path

from kdagent.obs.model import Span, Trace

_EVAL_RUN_KEY = "eval.run_id"
_EVAL_TASK_KEY = "eval.task_id"


def _load_one(path: Path) -> Trace | None:
    """读单条 trace jsonl（header + spans 行）。"""
    trace: Trace | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("_type") == "trace":
            trace = Trace(
                trace_id=str(row.get("trace_id", "")),
                session_id=str(row.get("session_id", "")),
                user_input_snapshot=str(row.get("user_input_snapshot", "")),
                root_span_id=str(row.get("root_span_id", "")),
                ts=int(row.get("ts", 0)),
                attributes=dict(row.get("attributes") or {}),
            )
        elif row.get("_type") == "span" and trace is not None:
            trace.spans.append(
                Span(
                    span_id=str(row.get("span_id", "")),
                    trace_id=str(row.get("trace_id", "")),
                    parent_span_id=(
                        str(row["parent_span_id"]) if row.get("parent_span_id") else None
                    ),
                    name=str(row.get("name", "")),
                    kind=str(row.get("kind", "")),
                    status=row.get("status", "ok"),
                    start_ts=int(row.get("start_ts", 0)),
                    end_ts=int(row.get("end_ts", 0)),
                    duration_ms=int(row.get("duration_ms", 0)),
                    attributes=dict(row.get("attributes") or {}),
                )
            )
    return trace


def load_traces(
    obs_dir: Path, run_id: str | None = None, task_id: str | None = None
) -> list[Trace]:
    """扫 obs/traces/ 全部 trace，按 eval 标记过滤（07 §3.8 trace_by_task）。

    无过滤返回全部（按 mtime 序）；run_id/task_id 命中 attributes 才返回。
    """
    traces: list[Trace] = []
    base = obs_dir / "traces"
    if not base.is_dir():
        return traces
    # 布局兼容：session_id 非空 → 目录；空（子代理默认 ""）→ jsonl 直接在 traces/ 下
    entries: list[Path] = []
    for entry in sorted(base.iterdir()):
        if entry.is_dir():
            entries.extend(p for p in sorted(entry.iterdir()) if p.name.endswith(".jsonl"))
        elif entry.name.endswith(".jsonl"):
            entries.append(entry)
    for path in entries:
        try:
            trace = _load_one(path)
        except (OSError, ValueError):
            continue  # 脏行跳过（不阻断排查）
        if trace is None:
            continue
        if run_id is not None and trace.attributes.get(_EVAL_RUN_KEY) != run_id:
            continue
        if task_id is not None and trace.attributes.get(_EVAL_TASK_KEY) != task_id:
            continue
        traces.append(trace)
    return traces


def failed_events(trace: Trace) -> list[Span]:
    """定位失败事件：is_error 的 span（工具失败/权限拒绝，07 §3.5 过滤项）。"""
    return [
        s
        for s in trace.spans
        if s.status == "error" or s.attributes.get("is_error") is True
    ]
