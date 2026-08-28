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
# begin_trace 的 token 栈（context-local）：一次 Agent.run() = 一次 push/pop。用**不可变
# tuple** 而非 list——asyncio.create_task 复制 context 时值是浅拷贝，list 原地改会跨任务
# 串改同一对象，tuple 每次 set 新建对象、子任务改不到父栈。修复 2026-08-28 f45c 实测：
# 并发子 Agent（各自 context 副本）共享同一 Telemetry 实例，原 `self._trace_token`
# 被后 begin 的子任务覆盖 → 先 end 的子任务拿别人 Context 的 token 去 reset →
# RuntimeError「Token ... was created in a different Context」击穿 Agent 主循环。
_trace_tokens: ContextVar[tuple[Any, ...]] = ContextVar("_trace_tokens", default=())
# 预置 trace attributes（07 §3.8 eval.run_id/task_id）：contextvar 隔离——每个
# asyncio.Task 独立上下文副本，并发子代理各自 set 互不覆盖（D60，替代实例级可变状态）。
# default=None 而非 {}（B039：ContextVar 默认值不可变，可变共享会跨上下文串改）。
_trace_attrs: ContextVar[dict[str, Any] | None] = ContextVar("_trace_attrs", default=None)


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

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_trace_attributes(self, attributes: dict[str, Any]) -> Any:
        """预置本次 begin_trace 的 trace attributes（07 §3.8：eval.run_id/task_id 关联）。

        写 contextvar（D60）：每个 asyncio.Task 有独立上下文副本，**并发子代理各自
        set 互不覆盖**（原实例级可变状态在并发下会互相串标记）。调用方应持返回的
        token 在作用域结束 reset（eval runner try/finally）；不 reset 时同 task 内
        多次 set 覆盖累积，串行语义与原实现一致。
        """
        return _trace_attrs.set(dict(attributes))

    def reset_trace_attributes(self, token: Any) -> None:
        """撤销一次 set_trace_attributes（恢复该作用域之前的标记，防跨任务残留）。"""
        _trace_attrs.reset(token)

    def begin_trace(
        self,
        session_id: str,
        user_input_snapshot: str,
        attributes: dict[str, Any] | None = None,
        parent_trace_id: str = "",
        parent_span_id: str = "",
    ) -> Trace | None:
        """02 Agent.run() 进入时调用：创建 Trace 并设为当前，落头行。

        `parent_trace_id`/`parent_span_id`（10 §5 342 D78）：子 Agent 委派点读父
        上下文传入——子 trace 记录调用方 id，落盘可重建「父 trace → 子 trace」调用链。
        """
        if not self._enabled:
            return None
        merged = {**(_trace_attrs.get() or {}), **(attributes or {})}
        trace = Trace(
            trace_id=gen_id(),
            session_id=session_id,
            user_input_snapshot=user_input_snapshot,
            root_span_id="",
            ts=now_ms(),
            parent_trace_id=parent_trace_id,
            parent_span_id=parent_span_id,
            attributes=merged,
        )
        _trace_tokens.set(_trace_tokens.get() + (_current_trace.set(trace),))
        self._exporter.export_trace_header(trace)
        return trace

    def current_context(self) -> tuple[str, str, str]:
        """读取调用方当前 trace 上下文（10 §5 342 D78）：返回 (trace_id, span_id, session_id)。

        委派点（SubAgentRunner 构造子 Agent 前）读父 trace 挂链 + 会话归属；无活动
        trace / 未启用 → ("", "", "") 表示无父。contextvar 任务局部：asyncio.create_task
        快照当前上下文，后台子 Agent 同样能读到父 trace。
        """
        if not self._enabled:
            return ("", "", "")
        trace = _current_trace.get()
        if trace is None:
            return ("", "", "")
        return (trace.trace_id, _current_span_id.get() or "", trace.session_id)

    def end_trace(self) -> None:
        """02 Agent.run() 退出时调用：恢复 trace 上下文（本 context 栈 pop）。

        token 栈是 context-local：并发子 Agent（create_task 复制 context）各自
        push/pop 互不干扰，父 trace 上下文不会被子任务覆盖（D60 同思路）。
        """
        stack = _trace_tokens.get()
        if stack:
            _current_trace.reset(stack[-1])
            _trace_tokens.set(stack[:-1])

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
