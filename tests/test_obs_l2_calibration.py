"""L2 标定报告聚合测试（01 §9.2 T8 / 07 §3.6：kdagent obs calibrate 数据源）。

构造含 llm.call / tool.exec / context.l2_decide / context.l2_compress 的 fixture
JSONL，断言报告各节指标：P 增长率、X 分布与覆盖率、实际 α、决策与触发率、
econ_fail 中间量、按模型成本、--run-id 过滤、无数据退出、JSON 输出。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kdagent.obs.l2_calibration import (
    CalibrationReport,
    _collect_trace,
    analyze,
    format_markdown,
    main,
    render,
    suggest_params,
)


def _row(typ: str, name: str | None = None, attrs: dict | None = None, start_ts: int = 0) -> dict:
    r: dict = {"_type": typ}
    if name is not None:
        r["name"] = name
    if attrs is not None:
        r["attributes"] = attrs
    if start_ts:
        r["start_ts"] = start_ts
    return r


def _write_trace(obs_dir: Path, sid: str, run_id: str, rows: list[dict]) -> None:
    d = obs_dir / "traces" / sid
    d.mkdir(parents=True, exist_ok=True)
    header = {
        "_type": "trace",
        "trace_id": f"tr-{sid}",
        "session_id": sid,
        "attributes": {"eval.run_id": run_id},
    }
    lines = [json.dumps(header, ensure_ascii=False)] + [
        json.dumps(r, ensure_ascii=False) for r in rows
    ]
    (d / "trace.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _llm_call(inp: int, out: int, cache: int, start_ts: int) -> dict:
    return _row(
        "span",
        "llm.call",
        {
            "model": "deepseek-v4-flash",
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_tokens": cache,
        },
        start_ts=start_ts,
    )


def _tool_exec(tool: str, output_tokens: int, output_chars: int) -> dict:
    return _row(
        "span",
        "tool.exec",
        {"tool": tool, "output_tokens": output_tokens, "output_chars": output_chars},
    )


def _l2_decide(reason: str, x: int, *, be: float | None = None, en: int | None = None) -> dict:
    attrs = {
        "tool_use_id": "r1",
        "tool": "Bash",
        "x_chars": x * 4,
        "x_tokens": x,
        "info_density": "LOW",
        "original_type": "log",
        "reason": reason,
    }
    if be is not None:
        attrs["break_even_n"] = be
    if en is not None:
        attrs["expected_n"] = en
    return _row("span", "context.l2_decide", attrs)


def build_fixture(obs_dir: Path) -> None:
    """两条 trace：
    - s-A (eval-1)：2 轮 llm.call（Δ=3000）、2 个 tool.exec、compress + econ_fail 决策、1 次压缩
    - s-B (eval-2)：2 轮 llm.call（Δ=2800）、3 个 tool.exec
    """
    _write_trace(
        obs_dir,
        "s-A",
        "eval-1",
        [
            _llm_call(10_000, 100, 10_000, start_ts=1),
            _llm_call(13_000, 100, 10_000, start_ts=2),  # Δ3000
            _tool_exec("Bash", 1_000, 4_000),
            _tool_exec("Grep", 18_000, 72_000),
            _l2_decide("compress", 10_000, be=2.0, en=10),
            _l2_decide("econ_fail", 11_000, be=25.0, en=5),
            _row(
                "span",
                "context.l2_compress",
                {"original_type": "log", "x_tokens": 9_000, "s_tokens": 3_600},
            ),
        ],
    )
    _write_trace(
        obs_dir,
        "s-B",
        "eval-2",
        [
            _llm_call(10_000, 80, 8_000, start_ts=1),
            _llm_call(12_800, 80, 8_000, start_ts=2),  # Δ2800
            _tool_exec("Bash", 5_000, 20_000),
            _tool_exec("Grep", 8_000, 32_000),
            _tool_exec("ReadFile", 12_000, 48_000),
        ],
    )


# ---- 聚合正确性 -------------------------------------------------------------


def test_analyze_p_growth(tmp_path: Path) -> None:
    """P 增长率：每 trace 内按 ts 排序取 input_tokens 增量 → [3000, 2800]。"""
    obs_dir = tmp_path / "obs"
    build_fixture(obs_dir)
    rep = analyze(obs_dir)
    assert rep.trace_files == 2
    assert rep.p_trace_turns == [2, 2]
    assert rep.p_deltas == [3_000, 2_800]
    assert sum(rep.p_deltas) / len(rep.p_deltas) == 2_900


def test_analyze_x_distribution_and_coverage(tmp_path: Path) -> None:
    """X 分布：全量聚合 + 覆盖率 100%，P50/P90 线性插值正确。"""
    obs_dir = tmp_path / "obs"
    build_fixture(obs_dir)
    rep = analyze(obs_dir)
    assert rep.x_total == 5
    assert rep.x_with_size == 5  # 覆盖率
    assert sorted(rep.x_tokens) == [1_000, 5_000, 8_000, 12_000, 18_000]
    assert rep.x_chars == [4_000, 72_000, 20_000, 32_000, 48_000]
    assert rep.x_tool_counts == {"Bash": 2, "Grep": 2, "ReadFile": 1}


def test_analyze_alpha_by_type(tmp_path: Path) -> None:
    """实际 α：compress span 的 s/x = 3600/9000 = 0.4，按 original_type 分组。"""
    obs_dir = tmp_path / "obs"
    build_fixture(obs_dir)
    assert analyze(obs_dir).alpha_by_type == {"log": [0.4]}


def test_analyze_decisions_and_trigger_rate(tmp_path: Path) -> None:
    """决策分布：compress 1 + econ_fail 1，触发率 50%；econ_fail 中间量记录。"""
    obs_dir = tmp_path / "obs"
    build_fixture(obs_dir)
    rep = analyze(obs_dir)
    assert rep.decide_reasons == {"compress": 1, "econ_fail": 1}
    assert rep.econ_fail_pairs == [(25.0, 5)]


def test_analyze_cost_by_model(tmp_path: Path) -> None:
    """按模型计价：deepseek-v4-flash 全量 token → 精确成本（非 DEFAULT 价）。"""
    obs_dir = tmp_path / "obs"
    build_fixture(obs_dir)
    rep = analyze(obs_dir)
    # in=10000+13000+10000+12800=45800, out=360, cache=10000+10000+8000+8000=36000
    assert rep.cost_tokens["deepseek-v4-flash"] == [45_800, 360, 36_000]
    expected = (45_800 * 1.5 + 360 * 4.5 + 36_000 * 0.05) / 1e6
    assert rep.cost_cny == pytest.approx(expected)
    assert rep.cost_cny == pytest.approx(0.07212)


def test_run_id_filter(tmp_path: Path) -> None:
    """--run-id 过滤：只统计该 eval 轮（trace 头 eval.run_id）。"""
    obs_dir = tmp_path / "obs"
    build_fixture(obs_dir)
    rep = analyze(obs_dir, run_id="eval-1")
    assert rep.trace_files == 1
    assert rep.p_deltas == [3_000]
    assert sorted(rep.x_tokens) == [1_000, 18_000]
    assert rep.decide_reasons == {"compress": 1, "econ_fail": 1}
    # 只 s-A 的 llm.call：10000+13000 in
    assert rep.cost_tokens["deepseek-v4-flash"] == [23_000, 200, 20_000]


def _write_trace_flat(obs_dir: Path, trace_id: str, run_id: str, rows: list[dict]) -> None:
    """eval 跑批的平铺布局：traces/{trace_id}.jsonl 直放（无 session 分目录）。"""
    d = obs_dir / "traces"
    d.mkdir(parents=True, exist_ok=True)
    header = {
        "_type": "trace",
        "trace_id": trace_id,
        "session_id": "",
        "attributes": {"eval.run_id": run_id},
    }
    lines = [json.dumps(header, ensure_ascii=False)] + [
        json.dumps(r, ensure_ascii=False) for r in rows
    ]
    (d / f"{trace_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_analyze_flat_layout_eval_traces(tmp_path: Path) -> None:
    """平铺布局（eval 跑批 traces/{trace_id}.jsonl）与 run_id 过滤同样生效。"""
    obs_dir = tmp_path / "obs"
    _write_trace_flat(
        obs_dir,
        "tr-flat",
        "eval-flat",
        [
            _llm_call(10_000, 100, 10_000, start_ts=1),
            _llm_call(13_000, 100, 10_000, start_ts=2),  # Δ3000
            _tool_exec("Bash", 1_000, 4_000),
        ],
    )
    rep = analyze(obs_dir)
    assert rep.trace_files == 1
    assert rep.p_deltas == [3_000]
    assert rep.x_tokens == [1_000]
    assert rep.cost_tokens["deepseek-v4-flash"] == [23_000, 200, 20_000]
    # run_id 过滤在平铺布局同样生效
    assert analyze(obs_dir, run_id="eval-other").trace_files == 0
    assert analyze(obs_dir, run_id="eval-flat").trace_files == 1


def test_no_data_empty_dir(tmp_path: Path) -> None:
    """空 obs → trace_files=0，各桶为空（不 crash）。"""
    empty = tmp_path / "nonexistent-obs"
    rep = analyze(empty)
    assert rep.trace_files == 0
    assert rep.p_deltas == []
    assert rep.x_tokens == []
    assert rep.cost_cny == 0.0


# ---- 建议值与渲染 -----------------------------------------------------------


def test_suggest_params_keeps_prior_for_few_samples(tmp_path: Path) -> None:
    """α 样本 <10 → 建议沿用先验（不回填 EXPECTED_RATIO_BY_TYPE）。"""
    obs_dir = tmp_path / "obs"
    build_fixture(obs_dir)
    sugg = suggest_params(analyze(obs_dir))
    assert sugg["EXPECTED_RATIO_BY_TYPE"] == {}  # log 只有 1 个样本
    # X 窗口 [8000,12500) 内有 8000/12000 → 维持现阈值
    assert sugg["ONLINE_COMPRESS_MIN"] == 8_000


def test_format_markdown_sections(tmp_path: Path) -> None:
    """markdown 报告含全部关键节与数字。"""
    obs_dir = tmp_path / "obs"
    build_fixture(obs_dir)
    rep = analyze(obs_dir)
    text = format_markdown(rep, suggest_params(rep))
    assert "## 1. P 上下文增长率" in text
    assert "2,900" in text
    assert "## 2. X 工具结果长度分布" in text
    assert "## 3. 实际压缩率 α by type" in text
    assert "| log | 1 | 0.400" in text
    assert "## 4. 决策与触发分布" in text
    assert "50.0%" in text  # compress/eligible
    assert "## 5. 按模型计价成本" in text
    assert "deepseek-v4-flash" in text
    assert "¥0.0721" in text


def test_render_json_shape(tmp_path: Path) -> None:
    """--json：结构化 payload（suggestions + report 全字段）。"""
    obs_dir = tmp_path / "obs"
    build_fixture(obs_dir)
    text, _rep = render(obs_dir, as_json=True)
    payload = json.loads(text)
    assert set(payload) == {"suggestions", "report"}
    assert payload["report"]["trace_files"] == 2
    assert payload["report"]["cost_cny"] == pytest.approx(0.07212)


# ---- CLI 退出码与写文件 -----------------------------------------------------


def test_main_no_data_exits_nonzero(tmp_path: Path) -> None:
    """无数据 → 退出码 1（供脚本判断采集不足）。"""
    empty = tmp_path / "empty-obs"
    empty.mkdir()
    assert main(["--obs-dir", str(empty)]) == 1


def test_main_writes_output_file(tmp_path: Path) -> None:
    """--output 写文件（含报告文本），退出码 0。"""
    obs_dir = tmp_path / "obs"
    build_fixture(obs_dir)
    out = tmp_path / "report.md"
    assert main(["--obs-dir", str(obs_dir), "--output", str(out)]) == 0
    assert "L2 标定报告" in out.read_text(encoding="utf-8")


def test_collect_trace_ignores_unknown_spans() -> None:
    """未知 span 名（如旧版本无新埋点的 trace）→ 跳过不 crash。"""
    rep = CalibrationReport()
    _collect_trace(
        rep,
        [
            _row("trace"),
            _row("span", "future.span_name", {"reason": "compress"}),
            _row("span", "llm.call", {"model": "m", "input_tokens": 100, "output_tokens": 1}, 1),
        ],
    )
    assert rep.decide_reasons == {}
    assert rep.cost_tokens["m"] == [100, 1, 0]
