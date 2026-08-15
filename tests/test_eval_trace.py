"""评估失败定位 trace 测试（07 §3.8 消费方，M5 遗留）。

Telemetry 预置 eval 标记 → 子代理 run 全程产 trace → 落盘 → trace_store 过滤/定位。
"""

from __future__ import annotations

from pathlib import Path

from conftest import FakeLLM, done, tool_call

from kdagent.config import Config
from kdagent.eval.trace_store import failed_events, load_traces
from kdagent.obs.telemetry import Telemetry
from kdagent.subagent import SubAgentRunner
from kdagent.subagent.model import AgentDef
from kdagent.tools import build_default_registry

# ---- Telemetry 预置属性合并 ----


def test_telemetry_preset_attributes_merged(tmp_path: Path) -> None:
    telemetry = Telemetry(tmp_path)
    telemetry.set_trace_attributes({"eval.run_id": "run-1"})
    trace = telemetry.begin_trace("s", "snap", attributes={"eval.task_id": "task-a"})
    assert trace is not None
    assert trace.attributes == {"eval.run_id": "run-1", "eval.task_id": "task-a"}
    telemetry.end_trace()


def test_telemetry_preset_persists_across_traces(tmp_path: Path) -> None:
    telemetry = Telemetry(tmp_path)
    telemetry.set_trace_attributes({"eval.run_id": "run-1"})
    t1 = telemetry.begin_trace("s1", "a")
    telemetry.end_trace()
    t2 = telemetry.begin_trace("s2", "b")
    assert t1 is not None and t2 is not None
    assert t2.attributes.get("eval.run_id") == "run-1"  # 同实例多任务共享标记
    telemetry.end_trace()


# ---- trace_store：过滤 + 失败定位 ----


def _export_fixture(obs_dir: Path) -> None:
    """JsonlExporter 落盘一条带 eval 标记的 trace：1 个 is_error tool span + 1 ok span。"""
    telemetry = Telemetry(obs_dir)
    telemetry.set_trace_attributes({"eval.run_id": "run-1", "eval.task_id": "task-b"})
    trace = telemetry.begin_trace("sess-1", "题目")
    assert trace is not None
    with telemetry.span("tool.exec", "tool", {"tool": "EditFile", "input": {"path": "src/a.py"}}) as s1:
        if s1 is not None:
            s1.attributes["is_error"] = True
            s1.status = "error"
    with telemetry.span("tool.exec", "tool", {"tool": "ReadFile", "input": {"path": "src/a.py"}}):
        pass  # ok span
    telemetry.end_trace()


def test_load_traces_filters_by_run_and_task(tmp_path: Path) -> None:
    _export_fixture(tmp_path)
    assert len(load_traces(tmp_path)) == 1  # 无过滤全量
    assert len(load_traces(tmp_path, run_id="run-1")) == 1
    assert len(load_traces(tmp_path, run_id="run-1", task_id="task-b")) == 1
    assert load_traces(tmp_path, run_id="run-1", task_id="nope") == []
    assert load_traces(tmp_path, run_id="other") == []


def test_failed_events_locates_error_spans(tmp_path: Path) -> None:
    _export_fixture(tmp_path)
    traces = load_traces(tmp_path, run_id="run-1", task_id="task-b")
    assert len(traces) == 1
    bad = failed_events(traces[0])
    assert len(bad) == 1
    assert bad[0].name == "tool.exec"
    assert bad[0].attributes.get("tool") == "EditFile"  # 定位到失败工具调用


def test_load_traces_empty_dir(tmp_path: Path) -> None:
    assert load_traces(tmp_path) == []  # obs 目录不存在 → 空


def test_failed_events_empty_trace() -> None:
    # 无 spans 的 trace → 无失败事件（不误报）
    from kdagent.obs.model import Trace

    assert failed_events(
        Trace(trace_id="t", session_id="s", user_input_snapshot="", root_span_id="")
    ) == []


# ---- subagent telemetry 透传：子代理产 trace 落盘 ----


async def test_run_to_completion_emits_eval_trace(tmp_path: Path) -> None:
    definition = AgentDef(name="d", description="d", system_prompt="p", max_turns=5)
    obs_dir = tmp_path / "obs"
    telemetry = Telemetry(obs_dir)
    telemetry.set_trace_attributes({"eval.run_id": "run-9", "eval.task_id": "task-x"})
    runner = SubAgentRunner(
        llm=FakeLLM(
            [tool_call("TodoWrite", {"todos": [{"content": "目标", "tasks": []}]}), done("完成")]
        ),
        tools=build_default_registry(),
        config=Config(),
        work_dir=tmp_path,
    )
    result = await runner.run_to_completion(definition, "任务", telemetry=telemetry)
    assert "完成" in result.text
    traces = load_traces(obs_dir, run_id="run-9", task_id="task-x")
    assert len(traces) == 1
    names = {s.name for s in traces[0].spans}
    assert "tool.exec" in names  # TodoWrite 执行进了 trace


async def test_run_to_completion_without_telemetry_no_trace(tmp_path: Path) -> None:
    definition = AgentDef(name="d", description="d", system_prompt="p", max_turns=5)
    obs_dir = tmp_path / "obs"
    runner = SubAgentRunner(
        llm=FakeLLM([done("完成")]),
        tools=build_default_registry(),
        config=Config(),
        work_dir=tmp_path,
    )
    await runner.run_to_completion(definition, "任务")
    assert load_traces(obs_dir) == []  # 无 telemetry 不落盘（默认行为不变）
