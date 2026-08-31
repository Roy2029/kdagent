"""L2 在线压缩标定报告（01 §9.2 T8 / 07 §3.6）。

扫描 `{obs_dir}/traces/` 下 trace JSONL，聚合 L2 经济性模型标定所需指标：
- P 上下文增长率（llm.call.input_tokens 序列每轮增量）
- X 工具结果长度分布（tool.exec.output_tokens/output_chars，全量）
- 实际压缩率 α by type（context.l2_compress.x_tokens/s_tokens）
- L2 决策与触发分布（context.l2_decide.reason，含经济性中间量）
- 按模型计价成本（内置计价表 PROVIDER_COST_TABLE，D104）

纯函数、零外部依赖；复用 `obs/metrics._read_rows` 逐行解析（坏行防御已有）。
只读不写 config；建议值仅供参考，人工核对后回填 `01` §9.1 参数表。
"""

from __future__ import annotations

import statistics
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kdagent.context.compactor import (
    EXPECTED_RATIO_BY_TYPE,
    ONLINE_COMPRESS_MIN,
    TOOL_RESULT_SAVE_THRESHOLD_TOKENS,
    cost_params_from_table,
    estimate_token_cost,
)
from kdagent.obs.metrics import _read_rows

# L2 判定 reason 里「走过经济性评估」的集合（触发率分母 = eligible）
_ELIGIBLE_REASONS = {"compress", "econ_fail"}

# X 直方图分桶（token）：桶 i 覆盖 [bounds[i], bounds[i+1])，末桶 [40000, ∞)
_X_BOUNDS = [0, 1_000, 2_000, 4_000, 8_000, 12_000, 16_000, 20_000, 40_000]
_X_LABELS = [
    "<1K", "1-2K", "2-4K", "4-8K", "8-12K", "12-16K", "16-20K", "20-40K", "40K+",
]


@dataclass(slots=True)
class CalibrationReport:
    """标定报告聚合结果（各节原始样本 + 覆盖率标注）。"""

    trace_files: int = 0
    # P
    p_trace_turns: list[int] = field(default_factory=list)  # 每 trace 的 llm.call 轮数
    p_deltas: list[int] = field(default_factory=list)  # 每轮 input_tokens 增量
    # X
    x_tokens: list[int] = field(default_factory=list)
    x_chars: list[int] = field(default_factory=list)
    x_tool_counts: dict[str, int] = field(default_factory=dict)
    x_total: int = 0  # 全部 tool.exec span
    x_with_size: int = 0  # 带 output_tokens 的 span
    # α
    alpha_by_type: dict[str, list[float]] = field(default_factory=dict)
    # 决策
    decide_reasons: dict[str, int] = field(default_factory=dict)
    econ_fail_pairs: list[tuple[float, int]] = field(default_factory=list)  # (break_even, expected)
    # 成本（按模型分桶）
    cost_tokens: dict[str, list[int]] = field(default_factory=dict)  # model → [in, out, cache]
    cost_cny: float = 0.0


def _int_attr(attrs: dict[str, object], key: str) -> int:
    v = attrs.get(key, 0)
    return int(v) if isinstance(v, (int, float)) else 0


def _percentile(vals: list[int], p: float) -> float | None:
    """线性插值百分位；空列表返回 None。"""
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    if lo == hi:
        return float(s[lo])
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _collect_trace(rep: CalibrationReport, rows: list[dict[str, object]]) -> None:
    """把单条 trace 的 span 行聚合进报告桶（只认新埋点属性，旧 trace 缺则跳过）。"""
    calls: list[tuple[int, dict[str, object]]] = []
    for row in rows:
        if row.get("_type") != "span":
            continue
        name = row.get("name")
        attrs = row.get("attributes")
        attrs = attrs if isinstance(attrs, dict) else {}
        if name == "llm.call":
            calls.append((int(row.get("start_ts") or 0), attrs))
            model = attrs.get("model")
            key = model if isinstance(model, str) and model else "<unknown>"
            bucket = rep.cost_tokens.setdefault(key, [0, 0, 0])
            bucket[0] += _int_attr(attrs, "input_tokens")
            bucket[1] += _int_attr(attrs, "output_tokens")
            bucket[2] += _int_attr(attrs, "cache_read_tokens")
        elif name == "tool.exec":
            rep.x_total += 1
            tool = attrs.get("tool")
            if isinstance(tool, str) and tool:
                rep.x_tool_counts[tool] = rep.x_tool_counts.get(tool, 0) + 1
            ot = attrs.get("output_tokens")
            if isinstance(ot, (int, float)):
                rep.x_with_size += 1
                rep.x_tokens.append(int(ot))
            oc = attrs.get("output_chars")
            if isinstance(oc, (int, float)):
                rep.x_chars.append(int(oc))
        elif name == "context.l2_decide":
            reason = attrs.get("reason")
            if isinstance(reason, str):
                rep.decide_reasons[reason] = rep.decide_reasons.get(reason, 0) + 1
                if reason == "econ_fail":
                    be, en = attrs.get("break_even_n"), attrs.get("expected_n")
                    if isinstance(be, (int, float)) and isinstance(en, (int, float)):
                        rep.econ_fail_pairs.append((float(be), int(en)))
        elif name == "context.l2_compress":
            xt, st = attrs.get("x_tokens"), attrs.get("s_tokens")
            if isinstance(xt, (int, float)) and isinstance(st, (int, float)):
                otype = attrs.get("original_type")
                t = otype if isinstance(otype, str) else "other"
                rep.alpha_by_type.setdefault(t, []).append(
                    min(1.0, float(st) / max(1, float(xt)))
                )

    # P 序列：按 start_ts 排序，input_tokens 增量 = 每轮新增上下文（P4 口径近似）
    calls.sort(key=lambda c: c[0])
    prev: int | None = None
    for _, attrs in calls:
        it = _int_attr(attrs, "input_tokens")
        if prev is not None:
            rep.p_deltas.append(max(0, it - prev))
        prev = it
    rep.p_trace_turns.append(len(calls))


def analyze(obs_dir: Path, run_id: str | None = None) -> CalibrationReport:
    """扫描 trace 目录聚合标定报告；`run_id` 非空时只统计该 eval 轮。

    返回的 `cost_cny` 已按模型查内置计价表求和（未知模型回退 DEFAULT_COST）。
    """
    rep = CalibrationReport()
    traces_dir = obs_dir / "traces"
    if not traces_dir.is_dir():
        return rep
    # 兼容两种落盘布局：标准 `traces/{session_id}/{trace_id}.jsonl`，以及 eval/脚本
    # 产出的平铺 `traces/{trace_id}.jsonl`（无 session 分目录，文件即 trace）。
    trace_files: list[Path] = []
    for item in sorted(traces_dir.iterdir()):
        if item.is_dir():
            trace_files += sorted(item.glob("*.jsonl"))
        elif item.suffix == ".jsonl":
            trace_files.append(item)
    for file in trace_files:
        rows = _read_rows(file)
        trace_attrs: dict[str, Any] = {}
        for row in rows:
            if row.get("_type") == "trace":
                a = row.get("attributes")
                if isinstance(a, dict):
                    trace_attrs = a
                break
        if run_id and trace_attrs.get("eval.run_id") != run_id:
            continue
        rep.trace_files += 1
        _collect_trace(rep, rows)
    for model, bucket in rep.cost_tokens.items():
        rep.cost_cny += estimate_token_cost(
            bucket[0], bucket[1], bucket[2],
            cost=cost_params_from_table({}, model=model),
        )
    return rep


def suggest_params(rep: CalibrationReport) -> dict[str, object]:
    """基于真实分布给建议值（启发式；人工核对后回填 01 §9.1，不回写 config）。"""
    sugg: dict[str, object] = {}
    # α：各类型实际均值，样本 ≥10 才给建议（否则沿用先验）
    alpha_sugg: dict[str, float] = {}
    for t, vals in sorted(rep.alpha_by_type.items()):
        if len(vals) >= 10:
            alpha_sugg[t] = round(statistics.mean(vals), 3)
    sugg["EXPECTED_RATIO_BY_TYPE"] = alpha_sugg
    # ONLINE_COMPRESS_MIN：L2 窗口 [8000, 12500) 内结果占比
    total = len(rep.x_tokens)
    in_window = sum(
        1
        for x in rep.x_tokens
        if ONLINE_COMPRESS_MIN <= x < TOOL_RESULT_SAVE_THRESHOLD_TOKENS
    )
    if total == 0:
        sugg["ONLINE_COMPRESS_MIN"] = ONLINE_COMPRESS_MIN
        sugg["ONLINE_COMPRESS_MIN_note"] = "无 X 样本，维持现阈值"
    elif in_window == 0:
        p90 = _percentile(rep.x_tokens, 0.9)
        suggested = int(min(max(1_000, ((p90 or 8_000) // 1_000) * 1_000), 8_000))
        sugg["ONLINE_COMPRESS_MIN"] = suggested
        sugg["ONLINE_COMPRESS_MIN_note"] = (
            f"L2 窗口内无样本（占比 0.0%），建议下调至 ~P90 扩大覆盖（供参考）"
        )
    else:
        sugg["ONLINE_COMPRESS_MIN"] = ONLINE_COMPRESS_MIN
        sugg["ONLINE_COMPRESS_MIN_note"] = (
            f"窗口内占比 {in_window / total:.1%}，维持现阈值"
        )
    return sugg


def _histogram(xs: list[int]) -> list[tuple[str, int]]:
    counts = [0] * len(_X_BOUNDS)
    for x in xs:
        idx = len(_X_BOUNDS) - 1
        for i, b in enumerate(_X_BOUNDS):
            if x < b:
                idx = i - 1
                break
        counts[max(0, idx)] += 1
    return list(zip(_X_LABELS, counts))


def _f_ratio(num: float, den: float) -> str:
    return f"{num / den:.1%}" if den else "—"


def format_markdown(rep: CalibrationReport, sugg: dict[str, object]) -> str:
    """报告 → markdown 表格（终端友好；`--json` 用 asdict 兜底）。"""
    L: list[str] = []
    A = L.append
    A("# L2 标定报告（01 §9.2 T8）")
    A("")
    A(f"- trace 文件：{rep.trace_files}；有 llm.call 的 trace：{sum(1 for n in rep.p_trace_turns if n)}")
    A("- 口径：eligible = reason ∈ {compress, econ_fail}；X = 工具结果原始 token 估算（估计值）")
    A("")

    # P 增长率
    A("## 1. P 上下文增长率")
    A("")
    n_traces = sum(1 for n in rep.p_trace_turns if n)
    if n_traces:
        p50 = _percentile(rep.p_deltas, 0.5)
        p90 = _percentile(rep.p_deltas, 0.9)
        A("| 指标 | 值 |")
        A("|---|---|")
        A(f"| 参与 trace 数 | {n_traces} |")
        A(f"| 每轮增长均值 | {statistics.mean(rep.p_deltas):,.0f} token（{len(rep.p_deltas)} 个样本） |")
        A(f"| P50 / P90 | {p50:,.0f} / {p90:,.0f} |")
        A(f"| 建议 AVG_GROWTH_PER_TURN | {int(statistics.mean(rep.p_deltas)):,} |")
        A("")
    else:
        A("样本不足（无多轮 llm.call 序列）。")
        A("")

    # X 分布
    A("## 2. X 工具结果长度分布")
    A("")
    if rep.x_tokens:
        p50, p90, p99 = (
            _percentile(rep.x_tokens, 0.5),
            _percentile(rep.x_tokens, 0.9),
            _percentile(rep.x_tokens, 0.99),
        )
        A("| 指标 | 值 |")
        A("|---|---|")
        A(f"| 样本数 | {len(rep.x_tokens)}（覆盖率 {rep.x_with_size}/{rep.x_total}） |")
        A(f"| P50 / P90 / P99 / max | {p50:,.0f} / {p90:,.0f} / {p99:,.0f} / {max(rep.x_tokens):,} token |")
        A(f"| Top 工具 | {'、'.join(f'{k}({v})' for k, v in sorted(rep.x_tool_counts.items(), key=lambda kv: -kv[1])[:5])} |")
        A("")
        A("| 分桶(token) | 计数 |")
        A("|---|---|")
        for label, cnt in _histogram(rep.x_tokens):
            A(f"| {label} | {cnt} |")
        A("")
    else:
        A("无带 output_tokens 的 tool.exec 样本（旧 trace 或未采集）。")
        A("")

    # 实际 α
    A("## 3. 实际压缩率 α by type")
    A("")
    if rep.alpha_by_type:
        A("| 类型 | 样本数 | 实际均值 | 先验 | 偏差 |")
        A("|---|---|---|---|---|")
        for t, vals in sorted(rep.alpha_by_type.items()):
            mean = statistics.mean(vals)
            prior = EXPECTED_RATIO_BY_TYPE.get(t)
            prior_s = f"{prior}" if prior is not None else "—"
            bias = f"{mean - prior:+.3f}" if prior is not None else "—"
            A(f"| {t} | {len(vals)} | {mean:.3f} | {prior_s} | {bias} |")
        A("")
    else:
        A("无 context.l2_compress 样本（L2 未触发或未采集）。")
        A("")

    # 决策与触发
    A("## 4. 决策与触发分布")
    A("")
    total_decide = sum(rep.decide_reasons.values())
    if total_decide:
        A("| reason | 计数 |")
        A("|---|---|")
        for r in ("compress", "size_too_small", "size_too_big", "high_density", "econ_fail"):
            A(f"| {r} | {rep.decide_reasons.get(r, 0)} |")
        eligible = sum(rep.decide_reasons.get(r, 0) for r in _ELIGIBLE_REASONS)
        compressed = rep.decide_reasons.get("compress", 0)
        A("")
        A(f"- eligible（compress + econ_fail）：{eligible}；触发率 = compress/eligible = {_f_ratio(compressed, eligible)}")
        if rep.econ_fail_pairs:
            missed = sum(1 for be, en in rep.econ_fail_pairs if en > be)
            A(f"- econ_fail {len(rep.econ_fail_pairs)} 例中 {missed} 例 expected_n > break_even（本可回本却跳过，潜在漏压）")
            A(f"- econ_fail break_even 均值 {statistics.mean(be for be, _ in rep.econ_fail_pairs):.1f} / expected 均值 {statistics.mean(en for _, en in rep.econ_fail_pairs):.1f}")
        A("")
    else:
        A("无 context.l2_decide 样本——决策数据未采集（需新版埋点采集后积累）。")
        A("")

    # 成本
    A("## 5. 按模型计价成本")
    A("")
    if rep.cost_tokens:
        A("| 模型 | 输入 | 输出 | 缓存 | 成本(元) |")
        A("|---|---|---|---|---|")
        for model, bucket in sorted(rep.cost_tokens.items()):
            c = estimate_token_cost(
                bucket[0], bucket[1], bucket[2], cost=cost_params_from_table({}, model=model)
            )
            A(f"| {model} | {bucket[0]:,} | {bucket[1]:,} | {bucket[2]:,} | {c:.4f} |")
        A(f"\n**合计：¥{rep.cost_cny:.4f}**")
        A("")
    else:
        A("无 llm.call 样本。")
        A("")

    # 建议
    A("## 6. 建议值（人工确认后回填 01 §9.1，本命令不回写）")
    A("")
    alpha = sugg.get("EXPECTED_RATIO_BY_TYPE")
    if isinstance(alpha, dict) and alpha:
        A("| 类型 | 建议 α |")
        A("|---|---|")
        for t, v in alpha.items():
            A(f"| {t} | {v} |")
    else:
        A("- EXPECTED_RATIO_BY_TYPE：样本不足，沿用先验。")
    A(f"- ONLINE_COMPRESS_MIN：**{sugg.get('ONLINE_COMPRESS_MIN')}**（{sugg.get('ONLINE_COMPRESS_MIN_note', '')}）")
    A("")
    return "\n".join(L)


def render(
    obs_dir: Path, run_id: str | None = None, as_json: bool = False
) -> tuple[str, CalibrationReport]:
    """跑标定分析并返回 (输出文本, 报告)。JSON 形态用 asdict 序列化。"""
    rep = analyze(obs_dir, run_id)
    if as_json:
        import json as _json

        payload = {
            "suggestions": suggest_params(rep),
            "report": asdict(rep),
        }
        return _json.dumps(payload, ensure_ascii=False, indent=2), rep
    return format_markdown(rep, suggest_params(rep)), rep


def main(argv: list[str] | None = None) -> int:
    """CLI 入口（`kdagent obs calibrate` 挂载点；也可 `python -m` 直跑）。

    退出码：0 正常；1 无数据（obs 目录不存在或空）。
    """
    import argparse

    parser = argparse.ArgumentParser(prog="kdagent obs calibrate", description="L2 标定报告")
    parser.add_argument("--obs-dir", default=None, help="obs 根目录（默认 {work_dir}/.kdagent/obs）")
    parser.add_argument("--run-id", default=None, help="只统计指定 eval 轮（trace 头 eval.run_id）")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON")
    parser.add_argument("--output", default=None, help="写文件（默认 stdout）")
    args = parser.parse_args(argv)

    obs_dir = Path(args.obs_dir) if args.obs_dir else (Path.cwd() / ".kdagent" / "obs")
    text, rep = render(obs_dir, args.run_id, as_json=args.json)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text)
    if rep.trace_files == 0:
        print("（无 trace 数据，请先积累会话或指定 --obs-dir）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
