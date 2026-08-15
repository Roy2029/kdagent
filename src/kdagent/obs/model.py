"""可观测性数据模型（规格 07 §3.1）。

Session/Trace/Span 三层：一次完整端到端交互（04 的 sid）→ 一次 Agent.run()（Trace）
→ 一个具体操作（Span）。字段语义对齐 OTel（id/name/kind/parent/status/attributes），
导出映射零转换（D2）。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal

SpanStatus = Literal["ok", "error"]
LogLevel = Literal["debug", "info", "warn", "error"]


def gen_id() -> str:
    """span/trace 唯一 id（16 hex 字符）。"""
    return secrets.token_hex(8)


def now_ms() -> int:
    """当前时间戳（毫秒）。"""
    return time.time_ns() // 1_000_000


@dataclass(slots=True)
class SpanLog:
    """span 的半结构化日志行（本地含完整内容，脱敏在 exporter 出口做）。"""

    level: LogLevel
    message: str
    ts: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Span:
    """一个具体操作的最小单元（07 §3.1）。"""

    span_id: str
    trace_id: str
    parent_span_id: str | None
    name: str  # llm.call / tool.exec / permission.check / context.compact / ...
    kind: str  # client / tool / permission / hook / context / session
    status: SpanStatus = "ok"
    start_ts: int = 0  # ms
    end_ts: int = 0
    duration_ms: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)  # model/tokens/decision/...
    logs: list[SpanLog] = field(default_factory=list)


@dataclass(slots=True)
class Trace:
    """一次用户输入 + 响应链路（02 一次 Agent.run()）。"""

    trace_id: str
    session_id: str
    user_input_snapshot: str  # 脱敏后的输入摘要
    root_span_id: str
    spans: list[Span] = field(default_factory=list)  # 以 parent_span_id 恢复树
    ts: int = 0
    # 10 §5 342（D78）：子 Agent trace 挂父——记录调用方（主 Agent/eval）的 trace。
    # 空串 = 根 trace（无父）；落盘 header 带这两个字段可重建跨 trace 调用链。
    parent_trace_id: str = ""
    parent_span_id: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)  # 版本指纹 / eval 关联（§3.8）
