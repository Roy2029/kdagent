"""Metrics 聚合纯函数（07 §3.5，D70）：读 trace JSONL 按 session 聚合健康度指标。

数据源用 Telemetry 真实落盘（格式天然对齐 exporter），聚合后断言各口径。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kdagent.obs import aggregate_metrics, session_metrics
from kdagent.obs.telemetry import Telemetry


def _emit_sample_trace(obs_dir: Path) -> None:
    """构造一个含全部 span 类型的 trace（llm/tool/compact/l2/permission/hook）。"""
    telemetry = Telemetry(obs_dir)
    telemetry.begin_trace("s-m", "metrics test")
    with telemetry.span("llm.call", "client", {"model": "deepseek-chat"}) as s:
        if s is not None:
            s.attributes.update(
                input_tokens=100, output_tokens=50, cache_read_tokens=20, cache_creation_tokens=10
            )
    with telemetry.span("llm.call", "client", {"model": "deepseek-chat"}) as s2:
        if s2 is not None:
            s2.attributes.update(input_tokens=200, output_tokens=100)
            s2.status = "error"
    with telemetry.span("tool.exec", "tool", {"tool": "ReadFile", "is_error": False}):
        pass
    with telemetry.span("tool.exec", "tool", {"tool": "ReadFile", "is_error": True}):
        pass
    with telemetry.span("tool.exec", "tool", {"tool": "Grep", "is_error": False}):
        pass
    with telemetry.span("context.compact", "context", {"trigger": "auto"}):
        pass
    with telemetry.span("context.l2_compress", "context", {"tool_use_id": "r1"}):
        pass
    with telemetry.span("permission.check", "security", {"tool": "WriteFile", "effect": "allow"}):
        pass
    with telemetry.span("hook.run", "security", {"event": "pre_tool_use"}):
        pass
    telemetry.end_trace()


def test_aggregate_metrics_by_session(tmp_path: Path) -> None:
    _emit_sample_trace(tmp_path / "obs")
    result = aggregate_metrics(tmp_path / "obs")
    assert len(result) == 1
    m = result[0]
    assert m.session_id == "s-m"
    assert m.traces == 1
    assert m.providers == {"deepseek-chat"}
    # token 分列
    assert m.input_tokens == 300
    assert m.output_tokens == 150
    assert m.cache_read_tokens == 20
    assert m.cache_creation_tokens == 10
    # LLM：2 次调用，1 次 error
    assert m.llm_calls == 2
    assert m.llm_errors == 1
    assert m.llm_p99_ms >= 0  # 耗时口径不精确断言，只保证可算
    # 工具：ReadFile 2 次 1 错，Grep 1 次
    assert m.tools["ReadFile"].calls == 2
    assert m.tools["ReadFile"].errors == 1
    assert m.tools["Grep"].calls == 1
    assert m.tools["ReadFile"].success_rate == pytest.approx(0.5)
    # 压缩触发分布
    assert m.compact["auto"] == 1
    assert m.compact["l2"] == 1
    # 权限 / hook
    assert m.permission["allow"] == 1
    assert m.hook_runs == 1
    # 成本：入×2 + 出×8 + 缓存×0.2（元/百万 token）
    expected = (300 * 2 + 150 * 8 + 20 * 0.2) / 1_000_000
    assert m.cost_cny == pytest.approx(expected)


def test_aggregate_metrics_multi_session(tmp_path: Path) -> None:
    _emit_sample_trace(tmp_path / "obs")
    telemetry = Telemetry(tmp_path / "obs")
    telemetry.begin_trace("s-other", "second")
    with telemetry.span("llm.call", "client", {"model": "deepseek-chat"}):
        pass
    telemetry.end_trace()
    result = aggregate_metrics(tmp_path / "obs")
    sids = {m.session_id for m in result}
    assert sids == {"s-m", "s-other"}
    by_id = {m.session_id: m for m in result}
    assert by_id["s-other"].llm_calls == 1
    assert by_id["s-m"].llm_calls == 2  # 互不串扰


def test_aggregate_empty_obs(tmp_path: Path) -> None:
    assert aggregate_metrics(tmp_path / "obs") == []


def test_session_metrics_lookup(tmp_path: Path) -> None:
    _emit_sample_trace(tmp_path / "obs")
    m = session_metrics(tmp_path / "obs", "s-m")
    assert m is not None and m.llm_calls == 2
    assert session_metrics(tmp_path / "obs", "ghost") is None
