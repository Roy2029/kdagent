"""统一埋点 sink：Telemetry（规格 07 §3.2）。

埋点不散落各模块——收敛到一个 `Telemetry`，用 **contextvars** 传递当前 trace/span
（Python 3.11，避免显式参数穿层，侵入最小）。

- **自动父子**：嵌套 `span()` 自动串 parent，埋点方不关心链路。
- **trace 上下文**：02 `Agent.run()` 进入时 `begin_trace()`，退出时 `end_trace()`。
- **实时落盘**：span 一结束就写一行（崩溃不丢已产生数据，与 04 JSONL 同思路）。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from kdagent.obs.exporters import JsonlExporter, SpanExporter
from kdagent.obs.log import make_rules
from kdagent.obs.model import LogLevel, Span, SpanLog, Trace, gen_id, now_ms

_current_span_id: ContextVar[str | None] = ContextVar("_current_span_id", default=None)
_current_trace: ContextVar[Trace | None] = ContextVar("_current_trace", default=None)


class Telemetry:
    """全局埋点入口。默认 JsonlExporter 落盘；可插拔 exporter（D2）。

    `enabled=False` 时一切为 no-op（测试/无 obs 环境零开销）。
    """

    def __init__(
        self,
        obs_dir: Path,
        exporter: SpanExporter | None = None,
        *,
        sanitize_rules: dict[str, Any] | None = None,
        log_full_prompt: bool = False,
        enabled: bool = True,
    ) -> None:
        self._exporter = exporter or JsonlExporter(
            obs_dir, sanitize_rules=make_rules(sanitize_rules)
        )
        self.log_full_prompt = log_full_prompt
        self._enabled = enabled
        self._trace_token: Any | None = None
        self._preset_attributes: dict[str, Any] = {}  # 实例级预置（07 §3.8 eval 标记）

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_trace_attributes(self, attributes: dict[str, Any]) -> None:
        """预置本次 begin_trace 的 trace attributes（07 §3.8：eval.run_id/task_id 关联）。

        eval runner 每任务前调用，子 Agent 内部 begin_trace 时自动带上；串行跑批安全，
        并发子代理共享实例会竞争（评估并发跑批留待后续，记待决）。
        """
        self._preset_attributes = dict(attributes)

    def begin_trace(
        self,
        session_id: str,
        user_input_snapshot: str,
        attributes: dict[str, Any] | None = None,
    ) -> Trace | None:
        """02 Agent.run() 进入时调用：创建 Trace 并设为当前，落头行。"""
        if not self._enabled:
            return None
        merged = {**self._preset_attributes, **(attributes or {})}
        trace = Trace(
            trace_id=gen_id(),
            session_id=session_id,
            user_input_snapshot=user_input_snapshot,
            root_span_id="",
            ts=now_ms(),
            attributes=merged,
        )
        self._trace_token = _current_trace.set(trace)
        self._exporter.export_trace_header(trace)
        return trace

    def end_trace(self) -> None:
        """02 Agent.run() 退出时调用：恢复 trace 上下文。"""
        if self._trace_token is not None:
            _current_trace.reset(self._trace_token)
            self._trace_token = None

    @contextmanager
    def span(
        self, name: str, kind: str, attributes: dict[str, Any] | None = None
    ) -> Iterator[Span | None]:
        """嵌套自动父子；异常 → status=error + 堆栈入 logs → 重抛；结束即落盘。

        无活动 trace 时返回 None（埋点容错：游离 span 丢弃）。
        """
        if not self._enabled:
            yield None
            return
        trace = _current_trace.get()
        if trace is None:
            yield None
            return
        parent = _current_span_id.get()
        span = Span(
            span_id=gen_id(),
            trace_id=trace.trace_id,
            parent_span_id=parent,
            name=name,
            kind=kind,
            start_ts=now_ms(),
            attributes=dict(attributes or {}),
        )
        trace.spans.append(span)
        if not trace.root_span_id and parent is None:
            trace.root_span_id = span.span_id
        token = _current_span_id.set(span.span_id)
        try:
            yield span
        except Exception as exc:
            span.status = "error"
            self.add_log(span.span_id, "error", f"{type(exc).__name__}: {exc}")
            raise
        finally:
            span.end_ts = now_ms()
            span.duration_ms = span.end_ts - span.start_ts
            self._exporter.export(trace, span)
            _current_span_id.reset(token)

    def add_log(
        self,
        span_id: str,
        level: LogLevel,
        message: str,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """向 span 追加日志行（脱敏在 exporter 出口做，此处本地完整记录）。"""
        trace = _current_trace.get()
        if trace is None:
            return
        for span in trace.spans:
            if span.span_id == span_id:
                span.logs.append(
                    SpanLog(
                        level=level,
                        message=message,
                        ts=now_ms(),
                        attributes=dict(attributes or {}),
                    )
                )
                return
