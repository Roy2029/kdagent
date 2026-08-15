"""可观测性 Metrics 聚合（规格 07 §3.5）。

读 `obs_dir/traces/` 下的 trace JSONL，按 session_id 聚合 token/LLM/工具/压缩/
权限/Hook 指标。纯函数、零外部依赖，供 T9 `/metrics` 面板与 T7/T8 校准消费；
成本复用 01 计价表（estimate_token_cost，D67 同源）。

JSONL 每行：trace header（_type=trace）或 span（_type=span），字段对齐 obs.model；
本模块只读不写，span 归属 session 以目录名（= sid）为准。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from kdagent.context.compactor import estimate_token_cost


@dataclass(slots=True)
class ToolStat:
    """单工具调用统计（07 §3.5 工具调用行）。"""

    calls: int = 0
    errors: int = 0
    total_ms: int = 0

    @property
    def success_rate(self) -> float:
        """成功率；无调用时为 1.0（不误导）。"""
        if self.calls == 0:
            return 1.0
        return (self.calls - self.errors) / self.calls

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.calls if self.calls else 0.0


@dataclass(slots=True)
class SessionMetrics:
    """一个 session 的聚合指标（07 §3.5 全表；成本在聚合收尾统一算）。"""

    session_id: str
    traces: int = 0
    providers: set[str] = field(default_factory=set)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    llm_calls: int = 0
    llm_errors: int = 0
    llm_total_ms: int = 0
    llm_latencies_ms: list[int] = field(default_factory=list)
    tools: dict[str, ToolStat] = field(default_factory=dict)
    compact: dict[str, int] = field(default_factory=dict)  # force/auto/emergency/l2
    permission: dict[str, int] = field(default_factory=dict)  # allow/deny/ask
    hook_runs: int = 0
    cost_cny: float = 0.0

    @property
    def llm_avg_ms(self) -> float:
        return self.llm_total_ms / self.llm_calls if self.llm_calls else 0.0

    @property
    def llm_p99_ms(self) -> int:
        """第 99 百分位延迟（ms）；调用 <100 次时取最大值。"""
        if not self.llm_latencies_ms:
            return 0
        s = sorted(self.llm_latencies_ms)
        return s[min(len(s) - 1, int(len(s) * 0.99))]


def _read_rows(path: Path) -> list[dict[str, object]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[dict[str, object]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 坏行防御性跳过（写盘中断的半行）
    return rows


def _int_attr(attrs: dict[str, object], key: str) -> int:
    v = attrs.get(key, 0)
    return int(v) if isinstance(v, (int, float)) else 0


def _accumulate(sm: SessionMetrics, span: dict[str, object]) -> None:
    """按 span 类型把一行累进 session 桶（07 §3.5 口径；属性缺失防御为 0）。"""
    name = span.get("name")
    if not isinstance(name, str):
        return
    raw_attrs = span.get("attributes")
    attrs: dict[str, object] = raw_attrs if isinstance(raw_attrs, dict) else {}
    ms = _int_attr(span, "duration_ms")
    if name == "llm.call":
        sm.llm_calls += 1
        if span.get("status") == "error":
            sm.llm_errors += 1
        sm.input_tokens += _int_attr(attrs, "input_tokens")
        sm.output_tokens += _int_attr(attrs, "output_tokens")
        sm.cache_read_tokens += _int_attr(attrs, "cache_read_tokens")
        sm.cache_creation_tokens += _int_attr(attrs, "cache_creation_tokens")
        model = attrs.get("model")
        if isinstance(model, str) and model:
            sm.providers.add(model)
        sm.llm_total_ms += ms
        sm.llm_latencies_ms.append(ms)
    elif name == "tool.exec":
        tool = attrs.get("tool")
        stat = sm.tools.setdefault(
            tool if isinstance(tool, str) and tool else "<unknown>", ToolStat()
        )
        stat.calls += 1
        if attrs.get("is_error"):
            stat.errors += 1
        stat.total_ms += ms
    elif name == "context.compact":
        trigger = attrs.get("trigger", "unknown")
        trigger_name = trigger if isinstance(trigger, str) else "unknown"
        sm.compact[trigger_name] = sm.compact.get(trigger_name, 0) + 1
    elif name == "context.l2_compress":
        sm.compact["l2"] = sm.compact.get("l2", 0) + 1
    elif name == "permission.check":
        effect = attrs.get("effect", "unknown")
        effect_name = effect if isinstance(effect, str) else "unknown"
        sm.permission[effect_name] = sm.permission.get(effect_name, 0) + 1
    elif name == "hook.run":
        sm.hook_runs += 1


def aggregate_metrics(obs_dir: Path) -> list[SessionMetrics]:
    """读全部 trace JSONL 按 session 聚合（07 §3.5）。

    成本行聚合后统一算（token × 01 计价表），避免逐 span 取整误差累计；
    cache 用 cache_read（D9：命中折扣价，与 D67 评测口径一致）。
    """
    traces_dir = obs_dir / "traces"
    if not traces_dir.is_dir():
        return []
    buckets: dict[str, SessionMetrics] = {}
    for sid_dir in sorted(traces_dir.iterdir()):
        if not sid_dir.is_dir():
            continue
        sm = buckets.setdefault(sid_dir.name, SessionMetrics(session_id=sid_dir.name))
        for file in sorted(sid_dir.glob("*.jsonl")):
            rows = _read_rows(file)
            sm.traces += sum(1 for r in rows if r.get("_type") == "trace")
            for span in rows:
                if span.get("_type") == "span":
                    _accumulate(sm, span)
    for sm in buckets.values():
        sm.cost_cny = estimate_token_cost(
            sm.input_tokens, sm.output_tokens, sm.cache_read_tokens
        )
    return list(buckets.values())


def session_metrics(obs_dir: Path, session_id: str) -> SessionMetrics | None:
    """按 sid 取聚合（/metrics 面板「当前 session」用）；无 trace 返回 None。"""
    for sm in aggregate_metrics(obs_dir):
        if sm.session_id == session_id:
            return sm
    return None
