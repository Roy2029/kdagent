"""评估失败定位的 trace 数据层（07 §3.8 消费方，M5 遗留）。

D55 之后 07 trace 带完整 input；评估子代理接 telemetry 后，每次任务一条 trace
落盘到 obs 目录。本模块读取 JSONL（JsonlExporter 格式），按 eval 标记过滤、重构
span 树、定位失败事件——为「失败归类（11 §3.4）/ trace 排查（11 §3.5）」供数据。

纯读无副作用；判分后回填判定（`backfill_verdict`，07 §3.8 验收 276）是对已落盘
trace 的追加改写（原子写防半行），不改 exporter 写入路径。测试隔离用 tmp obs_dir。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from kdagent.obs.model import LogLevel, Span, SpanLog, Trace

_EVAL_RUN_KEY = "eval.run_id"
_EVAL_TASK_KEY = "eval.task_id"

# 落盘行里的 level 是不受信数据：白名单外一律降级 info（mypy 收窄 Literal）
_LOG_LEVELS: frozenset[str] = frozenset({"debug", "info", "warn", "error"})


def _log_level(raw: object) -> LogLevel:
    lvl = str(raw) if raw else "info"
    return cast(LogLevel, lvl if lvl in _LOG_LEVELS else "info")


def _iter_trace_files(base: Path) -> list[Path]:
    """扫 traces/ 下全部 JSONL（布局兼容：session 目录或直接放 traces/ 下）。"""
    files: list[Path] = []
    for entry in sorted(base.iterdir()):
        if entry.is_dir():
            files.extend(p for p in sorted(entry.iterdir()) if p.name.endswith(".jsonl"))
        elif entry.name.endswith(".jsonl"):
            files.append(entry)
    return files


def _atomic_write(path: Path, lines: list[str]) -> None:
    """整文件原子重写（同目录 .tmp → replace），防写中断留半行。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def backfill_verdict(
    obs_dir: Path,
    run_id: str,
    task_id: str,
    passed: bool,
    kind: str | None = None,
    reason: str | None = None,
    f2p: list[str] | None = None,
    p2p_failed: list[str] | None = None,
) -> int:
    """打分后回填 trace 判定（07 §3.8 验收 276 / 11 §5 225）。

    遍历 obs/traces/ 下全部 JSONL，把 eval.run_id/eval.task_id 命中的 trace
    header attributes 追加 `eval.passed`（bool）+ `eval.kind`/`eval.reason`
    （失败归类）——判分结果除 report.json 外写进 trace 本体，trace 成为自包含
    产物（/metrics 聚合、复核阅读等直接读 trace 的消费者不用 join 报告）。
    D4 v052：f2p/p2p_failed 可选——Docker 判分有逐题 F2P/P2P 明细时写
    `eval.f2p`/`eval.p2p_failed`（None 不写，非 Docker 路径 trace 保持干净）。

    原子写防半行；返回改写的 trace 数（0 = 无命中）。幂等：重复回填覆盖上次
    判定。读/写失败（OSError/脏行）跳过该文件不抛——回填是加分项，不阻断判分。
    """
    base = obs_dir / "traces"
    if not base.is_dir():
        return 0
    patched = 0
    for path in _iter_trace_files(base):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        if not lines:
            continue
        try:
            header = json.loads(lines[0])
        except json.JSONDecodeError:
            continue  # 头行脏 → 跳过（不阻断）
        if header.get("_type") != "trace":
            continue
        attrs = header.get("attributes") or {}
        if attrs.get(_EVAL_RUN_KEY) != run_id or attrs.get(_EVAL_TASK_KEY) != task_id:
            continue
        new_attrs = dict(attrs)
        new_attrs["eval.passed"] = passed
        if kind is not None:
            new_attrs["eval.kind"] = kind
        if reason is not None:
            new_attrs["eval.reason"] = reason
        if f2p is not None:
            new_attrs["eval.f2p"] = f2p
        if p2p_failed is not None:
            new_attrs["eval.p2p_failed"] = p2p_failed
        header["attributes"] = new_attrs
        lines[0] = json.dumps(header, ensure_ascii=False)
        try:
            _atomic_write(path, lines)
        except OSError:
            continue  # 写失败跳过（回填不阻断判分）
        patched += 1
    return patched


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
                    # D89：logs 一并读回（llm.call span 的 prompt 摘要/全文在这里——
                    # 此前只读 attributes 把 logs 丢了，HTML/复核界面看不到 LLM 输入）。
                    logs=[
                        SpanLog(
                            level=_log_level(log.get("level")),
                            message=str(log.get("message", "")),
                            ts=int(log.get("ts", 0)),
                            attributes=dict(log.get("attributes") or {}),
                        )
                        for log in (row.get("logs") or [])
                    ],
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
    for path in _iter_trace_files(base):
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
