"""内置计价表测试（01 T5-1 / D104：PROVIDER_COST_TABLE + config 覆盖）。

覆盖：内置三档取值、config 按 model/provider/单一价目覆盖、未配回退内置、
未知模型回退 DEFAULT、旧签名（仅 provider）兼容、`aggregate_metrics` 按模型计价。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kdagent.context.compactor import (
    DEFAULT_COST,
    PROVIDER_COST_TABLE,
    CostParams,
    cost_params_from_table,
    estimate_token_cost,
)
from kdagent.obs.metrics import aggregate_metrics


# ---- 内置三档 ---------------------------------------------------------------


def test_builtin_three_tiers_exact_values() -> None:
    """内置三档价目逐字段核对（元/百万 token：命中-未命中-输出）。"""
    assert PROVIDER_COST_TABLE["deepseek-v4-flash"] == CostParams(c_in=1.5, c_out=4.5, c_hit=0.05)
    assert PROVIDER_COST_TABLE["deepseek-v4-pro"] == CostParams(c_in=4.5, c_out=13.5, c_hit=0.15)
    assert PROVIDER_COST_TABLE["glm-5.3-flash"] == CostParams(c_in=0.8, c_out=2.8, c_hit=0.23)


# ---- config 覆盖优先级 ------------------------------------------------------


def test_config_model_overrides_builtin() -> None:
    """config 按 model 嵌套 → 覆盖内置表。"""
    got = cost_params_from_table(
        {"deepseek-v4-flash": {"c_in": 9.0, "c_out": 9.0, "c_hit": 0.1}},
        model="deepseek-v4-flash",
    )
    assert got == CostParams(c_in=9.0, c_out=9.0, c_hit=0.1)


def test_config_provider_entry_when_model_absent() -> None:
    """config 只配 provider 键、model 不在表内 → 用 provider 条目。"""
    got = cost_params_from_table(
        {"deepseek": {"c_in": 3.0, "c_out": 6.0, "c_hit": 0.5}},
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    assert got == CostParams(c_in=3.0, c_out=6.0, c_hit=0.5)


def test_config_single_price_form() -> None:
    """config 单一价目（无 model/provider 嵌套）→ 直接取。"""
    got = cost_params_from_table(
        {"c_in": 1.0, "c_out": 2.0, "c_hit": 0.5}, model="deepseek-v4-flash"
    )
    assert got == CostParams(c_in=1.0, c_out=2.0, c_hit=0.5)


def test_config_invalid_value_falls_back() -> None:
    """config 条目值非法（非数值，float() 抛 ValueError）→ 容错回退 DEFAULT_COST。"""
    got = cost_params_from_table(
        {"deepseek-v4-flash": {"c_in": "oops", "c_out": 4.5, "c_hit": 0.05}},
        model="deepseek-v4-flash",
    )
    assert got == DEFAULT_COST


# ---- 回退链 -----------------------------------------------------------------


def test_empty_config_falls_back_to_builtin() -> None:
    """未配 config → 内置表按 model 取价。"""
    got = cost_params_from_table({}, model="deepseek-v4-flash")
    assert got == CostParams(c_in=1.5, c_out=4.5, c_hit=0.05)


def test_unknown_model_falls_back_to_default() -> None:
    """内置表也没有的 model → DEFAULT_COST。"""
    assert cost_params_from_table({}, model="gpt-99") == DEFAULT_COST


def test_old_signature_provider_only_unchanged() -> None:
    """旧签名（仅 provider="deepseek"）不在内置表键内 → 仍回退默认，行为不变。"""
    assert cost_params_from_table({}, "deepseek") == DEFAULT_COST


def test_provider_arg_matching_model_name_resolves_builtin() -> None:
    """provider 字符串恰好等于内置表 model 名 → 命中内置（cost 段配 provider 复用）。"""
    got = cost_params_from_table({}, provider="deepseek-v4-flash", model="")
    assert got == CostParams(c_in=1.5, c_out=4.5, c_hit=0.05)


# ---- 成本计算 ---------------------------------------------------------------


def test_estimate_token_cost_uses_cost_params() -> None:
    """计价函数按传入 CostParams 计算（1M 输入/0.5M 输出/0.25M 缓存，deepseek-v4-flash）。"""
    cost = cost_params_from_table({}, model="deepseek-v4-flash")
    assert estimate_token_cost(1_000_000, 500_000, 250_000, cost=cost) == pytest.approx(3.7625)


def _write_trace(obs_dir: Path, sid: str, spans: list[dict]) -> None:
    """写单 session trace JSONL（仅 llm.call 计入 metrics 聚合）。"""
    d = obs_dir / "traces" / sid
    d.mkdir(parents=True, exist_ok=True)
    rows = [{"_type": "trace", "trace_id": "t", "session_id": sid, "attributes": {}}]
    rows += spans
    (d / "trace.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


def _llm_call(model: str, inp: int, out: int, cache: int) -> dict:
    return {
        "_type": "span",
        "name": "llm.call",
        "status": "ok",
        "attributes": {
            "model": model,
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_tokens": cache,
        },
    }


def test_aggregate_metrics_prices_by_model(tmp_path: Path) -> None:
    """/metrics 聚合按模型查内置计价表（不再是统一 DEFAULT 价）。"""
    obs_dir = tmp_path / "obs"
    _write_trace(
        obs_dir,
        "s-1",
        [_llm_call("deepseek-v4-flash", 1_000_000, 500_000, 250_000)],
    )
    sms = aggregate_metrics(obs_dir)
    assert len(sms) == 1
    sm = sms[0]
    assert sm.tokens_by_model["deepseek-v4-flash"] == [1_000_000, 500_000, 250_000]
    assert sm.cost_cny == pytest.approx(3.7625)  # 真实价；DEFAULT 价会是 6.05


def test_aggregate_metrics_unknown_model_falls_back(tmp_path: Path) -> None:
    """未知模型 → 回退 DEFAULT_COST（聚合不 crash，价格按默认典型区间）。"""
    obs_dir = tmp_path / "obs"
    _write_trace(obs_dir, "s-1", [_llm_call("unknown-model", 1_000_000, 500_000, 250_000)])
    sm = aggregate_metrics(obs_dir)[0]
    assert sm.tokens_by_model["unknown-model"] == [1_000_000, 500_000, 250_000]
    assert sm.cost_cny == pytest.approx(estimate_token_cost(1_000_000, 500_000, 250_000))
    assert sm.cost_cny == pytest.approx(6.05)
