"""Docker 判分集成（11 §5 224，D91）：predictions/dataset 组装 + runner 集成 + harness 调用。

不真跑 Docker：judge() 的 subprocess 调用 mock 掉；端到端测 runner 集成用 FakeLLM。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from conftest import FakeLLM, done, tool_call

from kdagent.config import Config
from kdagent.eval.docker_judge import (
    DockerJudgeConfig,
    DockerJudgeError,
    JudgeOutcome,
    build_dataset,
    build_predictions,
    judge,
)
from kdagent.eval.model import EvalReport, EvalTask
from kdagent.eval.runner import EvalRunner
from kdagent.subagent import BUILTIN_AGENTS_DIR
from kdagent.subagent.manager import AgentManager
from kdagent.subagent.runner import SubAgentRunner
from kdagent.tools import build_default_registry

_GOLD = "+++ src/a.py\n+right\n"


def _task(**kw: object) -> EvalTask:
    base: dict[str, object] = dict(
        instance_id="t1",
        repo="org/repo",
        base_commit="abc",
        problem_statement="修 bug",
        fail_to_pass=["test_f.py"],
        pass_to_pass=["test_p.py"],
        gold_patch=_GOLD,
        test_cmds=["pytest test_f.py"],
        test_patch="diff --git a/tests/test_f.py b/tests/test_f.py",
        log_parser="pytest",
    )
    base.update(kw)
    return EvalTask(**base)


def _docker_config() -> DockerJudgeConfig:
    return DockerJudgeConfig(
        harness_script=Path("D:/harness/run_harness.py"),
        python=Path("D:/harness/.venv/Scripts/python.exe"),
    )


# ---- 组装纯函数 ----

def test_build_predictions_format() -> None:
    task = _task()
    task.model_patch = "--- a/b.py\n+++ b/b.py\n+fixed\n"
    rows = build_predictions([task])
    assert rows == [
        {"instance_id": "t1", "model_patch": task.model_patch, "model_name_or_path": "kdagent"}
    ]


def test_build_dataset_format() -> None:
    task = _task()
    row = build_dataset([task])[0]
    # 官方 harness 反序列化要求：全大写 F2P/P2P、log_parser、test_cmds 保留
    assert row["instance_id"] == "t1"
    assert row["repo"] == "org/repo"
    assert row["FAIL_TO_PASS"] == ["test_f.py"]
    assert row["PASS_TO_PASS"] == ["test_p.py"]
    assert row["test_cmds"] == ["pytest test_f.py"]
    assert row["log_parser"] == "pytest"
    assert row["patch"] == _GOLD


def test_build_dataset_log_parser_default() -> None:
    row = build_dataset([_task(log_parser="")])[0]
    assert row["log_parser"] == "pytest"


# ---- D4 v052：harness report 逐题 F2P/P2P 明细解析 ----

def test_test_details_parses_f2p_p2p_and_failed() -> None:
    """logs[instance_id] 的 F2P/P2P/tests_status → (f2p_tests, p2p_tests, p2p_failed)。"""
    from kdagent.eval.docker_judge import _test_details

    logs = {
        "i1": {
            "FAIL_TO_PASS": ["test_f.py::test_a"],
            "PASS_TO_PASS": ["test_p.py::test_b", "test_p.py::test_c"],
            "tests_status": {
                "test_f.py::test_a": "PASSED",
                "test_p.py::test_b": "PASSED",
                "test_p.py::test_c": "FAILED",
            },
        }
    }
    f2p, p2p, p2p_failed = _test_details(logs, "i1")
    assert f2p == ["test_f.py::test_a"]
    assert p2p == ["test_p.py::test_b", "test_p.py::test_c"]
    assert p2p_failed == ["test_p.py::test_c"]  # 非 PASSED → 被碰坏


def test_test_details_missing_entry_returns_empty() -> None:
    """结构缺失/题不在 logs 里 → 全空（判分结果仍由 resolved/error 承载，不阻断）。"""
    from kdagent.eval.docker_judge import _test_details

    assert _test_details(None, "i1") == ([], [], [])
    assert _test_details({"i2": {"FAIL_TO_PASS": []}}, "i1") == ([], [], [])
    assert _test_details({"i1": "not-a-dict"}, "i1") == ([], [], [])


# ---- runner 集成 ----

@pytest.mark.asyncio
async def test_docker_mode_skips_local_judging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker 模式：跑批只产 patch，本地判分不做（resolved/failed 由 docker judge 填）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    task = _task(base_commit=base, instance_id="d1")
    runner = SubAgentRunner(
        llm=FakeLLM([tool_call("Bash", {"command": "echo fixed > flag.txt"}), done("ok")]),
        tools=build_default_registry(),
        config=Config(),
        work_dir=tmp_path,
    )
    manager = AgentManager([BUILTIN_AGENTS_DIR])
    manager.scan()
    definition = manager.get("general-purpose")
    assert definition is not None
    ev = EvalRunner(
        runner,
        definition=definition,
        source_repo=repo,
        work_dir=tmp_path / "eval",
        task_loader=lambda: [task],
        docker=_docker_config(),
    )
    # 不真跑 harness：mock judge 返回 resolved
    monkeypatch.setattr(
        "kdagent.eval.runner.judge",
        lambda *a, **kw: [JudgeOutcome(instance_id="d1", resolved=True)],
    )
    report = await ev.run("dr1")
    assert report.resolved == ["d1"]
    assert report.metrics.resolved == 1
    # 本地双轨没跑（test_cmd 有但被 docker 模式跳过）
    assert report.failed == []


def _bare_runner(**attrs: object) -> EvalRunner:
    """轻量构造：绕过 __init__，只设 _docker_judge_report 依赖的属性（单测私有方法）。"""
    ev = EvalRunner.__new__(EvalRunner)  # type: ignore[misc]
    ev._docker = _docker_config()
    ev._work_dir = Path("D:/work")
    ev._obs_dir = None
    ev._source_repo = None  # 单测归类逻辑，预检由 monkeypatch 放行
    for k, v in attrs.items():
        setattr(ev, k, v)
    return ev


def test_docker_judge_report_resolved_and_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kdagent.eval.runner.patch_applies", lambda *a, **kw: None)  # 预检放行
    """resolved 移入报告；失败走 classify 归类（wrong_fix / not_located）。"""
    ok = _task(instance_id="a")
    bad = _task(instance_id="b", gold_patch=_GOLD, model_patch="+++ src/a.py\n+wrong\n")  # 同文件 → wrong_fix
    far = _task(instance_id="c", gold_patch=_GOLD, model_patch="+++ src/other.py\n+x\n")  # 异文件 → not_located
    for t in (ok, bad, far):
        t.model_patch = t.model_patch or "+++ src/a.py\n+right\n"
    outcomes = [
        JudgeOutcome("a", True),
        JudgeOutcome("b", False),
        JudgeOutcome("c", False),
    ]
    monkeypatch.setattr("kdagent.eval.runner.judge", lambda *a, **kw: outcomes)
    ev = _bare_runner()
    report = EvalReport(run_id="r1", tasks=[ok, bad, far])
    report.metrics.total = 3
    ev._docker_judge_report(report)
    assert report.resolved == ["a"]
    assert report.metrics.resolved == 1
    kinds = {c.instance_id: c.kind for c in report.failed}
    assert kinds["b"] == "wrong_fix"
    assert kinds["c"] == "not_located"


def test_docker_judge_report_error_harness_fault_and_empty_patch_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kdagent.eval.runner.patch_applies", lambda *a, **kw: None)  # 预检放行
    """D4 v052 账目拆分：harness 环境错误 → harness_fault；模型空补丁 → empty_patch。"""
    err = _task(instance_id="e")
    empty = _task(instance_id="f", model_patch="")
    outcomes = [
        JudgeOutcome("e", False, error=True, reason="容器失败"),
        JudgeOutcome("f", False, empty_patch=True, reason="模型补丁为空"),
    ]
    monkeypatch.setattr("kdagent.eval.runner.judge", lambda *a, **kw: outcomes)
    ev = _bare_runner()
    report = EvalReport(run_id="r1", tasks=[err, empty])
    report.metrics.total = 2
    ev._docker_judge_report(report)
    assert report.resolved == []
    kinds = {c.instance_id: c.kind for c in report.failed}
    assert kinds["e"] == "harness_fault"  # 容器/环境问题仍是基础设施故障
    assert kinds["f"] == "empty_patch"  # 模型没产出 → 独立账目


def test_docker_judge_report_persists_f2p_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kdagent.eval.runner.patch_applies", lambda *a, **kw: None)  # 预检放行
    """D4 v052：harness 逐题 F2P/P2P 明细写回 EvalTask（report.json asdict 落盘）。"""
    ok = _task(instance_id="a")
    bad = _task(instance_id="b", gold_patch=_GOLD, model_patch="+++ src/a.py\n+wrong\n")
    for t in (ok, bad):
        t.model_patch = t.model_patch or "+++ src/a.py\n+right\n"
    outcomes = [
        JudgeOutcome(
            "a", True,
            f2p_tests=["test_f.py::test_x"], p2p_tests=["test_p.py::test_y"], p2p_failed=[],
        ),
        JudgeOutcome(
            "b", False,
            f2p_tests=["test_f.py::test_x"], p2p_tests=["test_p.py::test_y"],
            p2p_failed=["test_p.py::test_y"],
        ),
    ]
    monkeypatch.setattr("kdagent.eval.runner.judge", lambda *a, **kw: outcomes)
    ev = _bare_runner()
    report = EvalReport(run_id="r1", tasks=[ok, bad])
    report.metrics.total = 2
    ev._docker_judge_report(report)
    # resolved 题附 F2P 明细（目标测试全过）；失败题附 P2P 被碰坏明细
    assert ok.f2p_tests == ["test_f.py::test_x"]
    assert ok.p2p_failed == []
    assert bad.p2p_failed == ["test_p.py::test_y"]


def test_docker_judge_report_harness_error_all_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kdagent.eval.runner.patch_applies", lambda *a, **kw: None)  # 预检放行
    """judge 整体抛错（镜像缺/Docker 没起/harness 超时）→ 全部 harness_fault，报告不为空。"""
    monkeypatch.setattr(
        "kdagent.eval.runner.judge",
        lambda *a, **kw: (_ for _ in ()).throw(DockerJudgeError("harness 判分失败")),
    )
    tasks = [_task(instance_id="a"), _task(instance_id="b")]
    ev = _bare_runner()
    report = EvalReport(run_id="r1", tasks=tasks)
    report.metrics.total = 2
    ev._docker_judge_report(report)
    assert report.resolved == []
    assert [c.instance_id for c in report.failed] == ["a", "b"]
    assert all(c.kind == "harness_fault" for c in report.failed)
    assert "Docker 判分失败" in report.failed[0].reason


# ---- harness 调用（mock subprocess）----

def test_judge_invokes_harness_and_parses_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """judge 组 predictions/dataset → 调 run_harness.py → 读 report → 逐题判定。"""
    config = _docker_config()
    tasks = [_task(instance_id="t1"), _task(instance_id="t2")]
    tasks[0].model_patch = "--- a/b.py\n+++ b/b.py\n+fixed\n"
    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], **kw: object) -> object:
        captured["cmd"] = cmd
        captured["cwd"] = kw.get("cwd")
        # 写 harness report（文件名 = {model_name_or_path}.{run_id}.json，reporting.py:131）
        out_dir: Path = kw["cwd"]  # type: ignore[assignment]
        harness_run_id = cmd[cmd.index("--run_id") + 1]
        report = out_dir / f"kdagent.{harness_run_id}.json"
        report.write_text(
            json.dumps(
                {
                    "resolved_ids": ["t1"],
                    "error_ids": [],
                    "empty_patch_ids": [],
                    "logs": {  # D4 v052：逐题 F2P/P2P 明细
                        "t1": {
                            "FAIL_TO_PASS": ["test_f.py::test_a"],
                            "PASS_TO_PASS": ["test_p.py::test_b"],
                            "tests_status": {
                                "test_f.py::test_a": "PASSED",
                                "test_p.py::test_b": "PASSED",
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return type("P", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcomes = judge(config, tasks, eval_work_dir=tmp_path, eval_run_id="r1")
    # 命令构造正确
    assert "--predictions_path" in captured["cmd"]
    assert "--namespace" in captured["cmd"] and "starryzhang" in captured["cmd"]
    assert "--run_id" in captured["cmd"] and captured["cmd"][captured["cmd"].index("--run_id") + 1] == "judge-r1"
    # report 解析
    assert len(outcomes) == 2
    assert outcomes[0].resolved is True
    assert outcomes[1].resolved is False
    # D4 v052：明细随 outcomes 带出
    assert outcomes[0].f2p_tests == ["test_f.py::test_a"]
    assert outcomes[0].p2p_failed == []
    # predictions/dataset 已落盘
    out = config.resolve_out(tmp_path, "r1")
    assert (out / "predictions.jsonl").is_file()
    assert (out / "judge-dataset.json").is_file()


def test_judge_raises_on_missing_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """harness 正常退出但 report 缺失（异常路径）→ DockerJudgeError。"""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: type("P", (), {"returncode": 0, "stdout": ""})(),
    )
    with pytest.raises(DockerJudgeError):
        judge(_docker_config(), [_task()], eval_work_dir=tmp_path, eval_run_id="r2")
