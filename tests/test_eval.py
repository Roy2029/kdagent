"""评估体系 MVP（规格 11，M5-e）：封史 / 判分 / 归类 / 流水线。"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeLLM, done, tool_call

from kdagent.config import Config
from kdagent.eval import (
    EvalReport,
    EvalRunner,
    EvalTask,
    classify,
    extract_patch,
    gold_check,
    gold_similarity,
    seal_copy,
)
from kdagent.eval.cli import load_tasks_file
from kdagent.eval.docker_judge import DockerJudgeConfig
from kdagent.eval.runner import _force_rmtree
from kdagent.subagent import BUILTIN_AGENTS_DIR
from kdagent.subagent.manager import AgentManager
from kdagent.subagent.runner import SubAgentRunner
from kdagent.tools import build_default_registry

_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """含两个 commit 的临时 git 仓库（base = 第一次 commit）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@test.local")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("# t\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    (repo / "bug.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add bug")
    return repo


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, env=_GIT_ENV, capture_output=True, text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout


def _base_commit(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD~1").strip()


# ---- 封史副本（防作弊） ----

def test_seal_copy_single_commit(repo: Path) -> None:
    dest = repo.parent / "sealed"
    seal_copy(repo, _base_commit(repo), dest)
    log = _git(dest, "log", "--oneline")
    assert len(log.splitlines()) == 1  # 单提交：翻 git log 找不到答案
    assert "base" in log
    assert not (dest / "bug.py").exists()  # base 版本没有 bug.py
    assert (dest / "README.md").exists()


def test_seal_copy_origin_isolation(repo: Path) -> None:
    dest = repo.parent / "sealed"
    seal_copy(repo, _base_commit(repo), dest)
    assert _git(dest, "remote", "-v").strip() == ""  # 无 remote，无上游痕迹


def test_force_rmtree_removes_readonly_files(tmp_path: Path) -> None:
    """Windows 只读文件（git objects 同款属性）普通 rmtree 删不掉 → _force_rmtree 先 chmod 再删。"""
    target = tmp_path / "readonly"
    sub = target / "sub"
    sub.mkdir(parents=True)
    ro = sub / "ro.txt"
    ro.write_text("x", encoding="utf-8")
    os.chmod(ro, stat.S_IREAD)  # 只读属性，等同 git init 后的 objects
    assert ro.exists()
    _force_rmtree(target)
    assert not target.exists()


# ---- 补丁提取 ----

def test_extract_patch_excludes_runtime_dirs(repo: Path) -> None:
    dest = repo.parent / "sealed"
    seal_copy(repo, _base_commit(repo), dest)
    (dest / "fix.txt").write_text("ok\n", encoding="utf-8")
    (dest / ".kdagent").mkdir()
    (dest / ".kdagent" / "meta.json").write_text("{}", encoding="utf-8")
    _git(dest, "add", "-A")
    patch = extract_patch(dest)
    assert "fix.txt" in patch
    assert ".kdagent" not in patch  # 运行时目录不进补丁


def test_extract_patch_excludes_venv(repo: Path) -> None:
    """D96 治理③+回归：preinstall 建在副本内的 .venv 不进补丁。

    `git add -A` 会把 .venv 全量 stage（python.exe/Activate.ps1 等），若进 patch，
    容器 git apply 报 `git diff header lacks filename` / trailing whitespace
    （B5 全 5 题 harness_fault 实测，patch 混入 .venv/Scripts/Activate.ps1）。
    """
    dest = repo.parent / "sealed-venv"
    seal_copy(repo, _base_commit(repo), dest)
    (dest / "fix.txt").write_text("ok\n", encoding="utf-8")
    venv_script = dest / ".venv" / "Scripts"
    venv_script.mkdir(parents=True)
    (venv_script / "Activate.ps1").write_text(
        "<#\n.Synopsis\nActivate a Python virtual environment.\n#>",
        encoding="utf-8",
    )
    (venv_script / "python.exe").write_bytes(b"\x4d\x5a")  # 模拟二进制
    _git(dest, "add", "-A")
    patch = extract_patch(dest)
    assert "fix.txt" in patch
    assert ".venv" not in patch
    assert "Activate.ps1" not in patch
    assert "python.exe" not in patch


# ---- gold 相似度 ----

def test_gold_similarity() -> None:
    assert gold_similarity("", "a") == 0.0
    assert gold_similarity("a", "") == 0.0
    assert gold_similarity("same patch", "same patch") == 1.0
    assert gold_similarity("hello world", "hello world!") > 0.8


# ---- 失败归类五类 ----

def _task(**kw) -> EvalTask:
    base = dict(
        instance_id="t1",
        problem_statement="修 bug",
        fail_to_pass=["test_f.py"],
        pass_to_pass=["test_p.py"],
        gold_patch="",
    )
    base.update(kw)
    return EvalTask(**base)


def test_classify_harness_fault_on_error() -> None:
    task = _task()
    kind, reason = classify(task, "", None, "provider 挂了")
    assert kind == "harness_fault"
    assert "provider 挂了" in reason


def test_classify_empty_patch_kind() -> None:
    """D4 v052：模型未产出补丁（无基础设施故障）→ 独立 empty_patch，不占 harness_fault。"""
    kind, reason = classify(_task(), "", None, "")
    assert kind == "empty_patch"
    assert "未产出补丁" in reason


def test_classify_not_located() -> None:
    task = _task(gold_patch="+++ src/a.py\n+fixed\n")
    kind, _ = classify(task, "+++ src/other.py\n+x\n", None, "")
    assert kind == "not_located"


def test_classify_wrong_fix() -> None:
    task = _task(gold_patch="+++ src/a.py\n+right\n")
    kind, _ = classify(task, "+++ src/a.py\n+wrong\n", False, "")
    assert kind == "wrong_fix"


def test_classify_regression_touches_test() -> None:
    kind, _ = classify(_task(), "+++ tests/test_bug.py\n+- bad\n", False, "")
    assert kind == "regression"


def test_classify_constraint_conflict() -> None:
    task = _task(constraint="不要改测试文件")
    kind, _ = classify(task, "+++ tests/test_bug.py\n+x\n", False, "")
    assert kind == "constraint_conflict"


# ---- D4 v052：测试文件判定收紧（_is_test_path） ----

def test_is_test_path_patterns() -> None:
    """falcon/testing/client.py 不再误判为测试文件（旧 _TEST_FILE_HINTS 子串 bug）。"""
    from kdagent.eval.runner import _is_test_path

    # 真测试文件
    assert _is_test_path("tests/test_bug.py")  # tests/ 目录
    assert _is_test_path("test_utils.py")  # test_ 前缀
    assert _is_test_path("pkg/helper_test.py")  # _test 后缀
    assert _is_test_path("tests/conftest.py")  # conftest.py
    assert _is_test_path("src/tests/whatever.py")  # 任意深度 tests 段
    # 非测试文件（旧实现子串命中的回归点）
    assert not _is_test_path("falcon/testing/client.py")  # testing/ ≠ tests/
    assert not _is_test_path("src/contest.py")  # 子串 "test" 不命中
    assert not _is_test_path("README.md")


def test_classify_testing_dir_not_regression() -> None:
    """改动 falcon/testing/client.py 不算碰测试 → 走定位启发式（D4 收紧回归点）。"""
    kind, _ = classify(_task(), "+++ falcon/testing/client.py\n+fix\n", False, "")
    assert kind == "not_located"  # gold 为空无交集，不误判 regression


# ---- 流水线端到端 ----

def _runner(tmp_path: Path, llm: FakeLLM) -> SubAgentRunner:
    return SubAgentRunner(
        llm=llm,
        tools=build_default_registry(),
        config=Config(),
        work_dir=tmp_path,
    )


@pytest.mark.asyncio
async def test_eval_runner_resolves_via_test_cmd(repo: Path, tmp_path: Path) -> None:
    """Bash 在封史副本里建 flag 文件 → test_cmd 通过 → resolved。"""
    llm = FakeLLM([tool_call("Bash", {"command": "echo fixed > flag.txt"}), done("改完了")])
    runner = _runner(tmp_path, llm)
    manager = AgentManager([BUILTIN_AGENTS_DIR])
    manager.scan()
    definition = manager.get("general-purpose")
    assert definition is not None
    task = EvalTask(
        instance_id="t1",
        base_commit=_base_commit(repo),
        problem_statement="制造一个 flag.txt",
        test_cmd="test -f flag.txt",
    )
    ev = EvalRunner(
        runner, definition=definition, source_repo=repo,
        work_dir=tmp_path / "eval", task_loader=lambda: [task],
    )
    report = await ev.run("run-1")
    assert report.resolved == ["t1"]
    assert report.metrics.resolved == 1
    assert report.metrics.total == 1
    assert report.failed == []


@pytest.mark.asyncio
async def test_eval_runner_empty_patch_on_no_patch(repo: Path, tmp_path: Path) -> None:
    """Agent 只回文本不改动 → 空补丁 → empty_patch（D4 v052，非 harness_fault）。"""
    llm = FakeLLM([done("我看了下，没问题")])
    runner = _runner(tmp_path, llm)
    manager = AgentManager([BUILTIN_AGENTS_DIR])
    manager.scan()
    definition = manager.get("general-purpose")
    assert definition is not None
    task = EvalTask(
        instance_id="t2",
        base_commit=_base_commit(repo),
        problem_statement="修 bug",
        gold_patch="+++ README.md\n+fixed\n",
    )
    ev = EvalRunner(
        runner, definition=definition, source_repo=repo,
        work_dir=tmp_path / "eval", task_loader=lambda: [task],
    )
    report = await ev.run("run-2")
    assert report.resolved == []
    assert len(report.failed) == 1
    assert report.failed[0].kind == "empty_patch"


@pytest.mark.asyncio
async def test_eval_runner_gold_similarity_resolve(repo: Path, tmp_path: Path) -> None:
    """无 test_cmd：gold 相似度 ≥ 阈值 → resolved。"""
    gold = "+++ README.md\n+# fixed the bug\n+- done\n"
    llm = FakeLLM([tool_call("Bash", {"command": "printf '\\n# fixed the bug\\n' >> README.md"}), done("ok")])
    runner = _runner(tmp_path, llm)
    manager = AgentManager([BUILTIN_AGENTS_DIR])
    manager.scan()
    definition = manager.get("general-purpose")
    assert definition is not None
    task = EvalTask(
        instance_id="t3",
        base_commit=_base_commit(repo),
        problem_statement="修 bug",
        gold_patch=gold,
    )
    ev = EvalRunner(
        runner, definition=definition, source_repo=repo,
        work_dir=tmp_path / "eval", task_loader=lambda: [task], similarity_threshold=0.1,
    )
    report = await ev.run("run-3")
    assert "t3" in report.resolved


def test_summary_format(repo: Path) -> None:
    task = EvalTask(instance_id="t1", base_commit=_base_commit(repo))
    from kdagent.eval.model import EvalReport, RunMetrics

    report = EvalReport(
        run_id="run-x",
        tasks=[task],
        resolved=["t1"],
        metrics=RunMetrics(total=1, resolved=1),
    )
    s = report.summary()
    assert "run-x" in s
    assert "1/1" in s
    assert "通过" in s


# ---- tasks.json 解析 ----

def test_load_tasks_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f").write_text("x", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "i")
    cfg = tmp_path / "tasks.json"
    cfg.write_text(
        json.dumps(
            {
                "run_id": "eval-cfg",
                "repo_dir": str(repo),
                "tasks": [
                    {
                        "instance_id": "a1",
                        "base_commit": "HEAD",
                        "problem_statement": "修",
                        "fail_to_pass": ["test_a.py"],
                        "gold_patch": "+++ x",
                        "test_cmd": "python -m pytest",
                        "constraint": "别改测试",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id, repo_dir, work_dir, tasks = load_tasks_file(cfg)
    assert run_id == "eval-cfg"
    assert repo_dir == repo.resolve()
    assert len(tasks) == 1
    t = tasks[0]
    assert t.instance_id == "a1"
    assert t.fail_to_pass == ["test_a.py"]
    assert t.test_cmd == "python -m pytest"
    assert t.constraint == "别改测试"
    assert work_dir.is_dir()


# ---- PASS_TO_PASS 保护判分（11 §3.2 单题判定 + §5 223，D81） -----------------


def _repo_with_keep(tmp_path: Path) -> Path:
    """base commit 含 keep.txt 的仓库（P2P 测试保护的对象）。"""
    repo = tmp_path / "repo-keep"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@test.local")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("# t\n", encoding="utf-8")
    (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    (repo / "bug.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add bug")
    return repo


async def _run_eval(repo: Path, tmp_path: Path, llm: FakeLLM, *tasks: EvalTask) -> EvalReport:
    runner = _runner(tmp_path, llm)
    manager = AgentManager([BUILTIN_AGENTS_DIR])
    manager.scan()
    definition = manager.get("general-purpose")
    assert definition is not None
    ev = EvalRunner(
        runner, definition=definition, source_repo=repo,
        work_dir=tmp_path / "eval", task_loader=lambda: list(tasks),
    )
    return await ev.run("run-p2p")


@pytest.mark.asyncio
async def test_eval_runner_p2p_protection_passes(tmp_path: Path) -> None:
    """F2P 过 + P2P 过（未破坏 keep.txt）→ resolved + passed_to_passed 计入。"""
    repo = _repo_with_keep(tmp_path)
    llm = FakeLLM([tool_call("Bash", {"command": "echo fixed > flag.txt"}), done("改完了")])
    task = EvalTask(
        instance_id="p1",
        base_commit=_base_commit(repo),
        problem_statement="制造一个 flag.txt",
        test_cmd="test -f flag.txt",   # F2P：必须全过
        p2p_cmd="test -f keep.txt",    # P2P：原通过资源不能被破坏
    )
    report = await _run_eval(repo, tmp_path, llm, task)
    assert report.resolved == ["p1"]
    assert report.metrics.resolved == 1
    assert report.metrics.passed_to_passed == 1  # P2P 实测确认无损坏
    assert report.failed == []


@pytest.mark.asyncio
async def test_eval_runner_p2p_broken_is_regression(tmp_path: Path) -> None:
    """F2P 过但 P2P 被破坏（rm keep.txt）→ 不 resolved，归 regression。"""
    repo = _repo_with_keep(tmp_path)
    llm = FakeLLM(
        [tool_call("Bash", {"command": "echo fixed > flag.txt && rm -f keep.txt"}), done("改完了")]
    )
    task = EvalTask(
        instance_id="p2",
        base_commit=_base_commit(repo),
        problem_statement="制造一个 flag.txt",
        test_cmd="test -f flag.txt",   # F2P 过
        p2p_cmd="test -f keep.txt",    # P2P 破坏 → 不算 resolved
    )
    report = await _run_eval(repo, tmp_path, llm, task)
    assert report.resolved == []
    assert report.metrics.resolved == 0
    assert len(report.failed) == 1
    assert report.failed[0].kind == "regression"
    assert "PASS_TO_PASS" in report.failed[0].reason
    assert report.failed[0].patch  # 失败题带补丁供复查


@pytest.mark.asyncio
async def test_eval_runner_no_p2p_cmd_keeps_behavior(tmp_path: Path) -> None:
    """p2p_cmd 未给 → 原行为：F2P 过即 resolved，passed_to_passed 不计。"""
    repo = _repo_with_keep(tmp_path)
    llm = FakeLLM([tool_call("Bash", {"command": "echo fixed > flag.txt"}), done("改完了")])
    task = EvalTask(
        instance_id="p3",
        base_commit=_base_commit(repo),
        problem_statement="制造一个 flag.txt",
        test_cmd="test -f flag.txt",
        # 无 p2p_cmd
    )
    report = await _run_eval(repo, tmp_path, llm, task)
    assert report.resolved == ["p3"]
    assert report.metrics.passed_to_passed == 0


def test_load_tasks_parses_p2p_cmd(tmp_path: Path) -> None:
    """tasks.json 解析 p2p_cmd 字段。"""
    (tmp_path / "repo").mkdir()
    _git(tmp_path / "repo", "init")
    tasks = tmp_path / "tasks.json"
    tasks.write_text(
        json.dumps(
            {
                "repo_dir": str(tmp_path / "repo"),
                "tasks": [
                    {
                        "instance_id": "a1",
                        "base_commit": "HEAD",
                        "problem_statement": "修",
                        "fail_to_pass": ["test_a.py"],
                        "test_cmd": "python -m pytest",
                        "p2p_cmd": "python -m pytest tests/test_b.py",
                        "constraint": "别改测试",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _, _, _, tasks_list = load_tasks_file(tasks)
    assert tasks_list[0].p2p_cmd == "python -m pytest tests/test_b.py"
    assert tasks_list[0].constraint == "别改测试"


def test_load_tasks_file_empty(tmp_path: Path) -> None:
    cfg = tmp_path / "tasks.json"
    cfg.write_text('{"repo_dir": ".", "tasks": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="tasks 为空"):
        load_tasks_file(cfg)


# ---- gold 校验（11 §3.2 步骤 3 + §5 222，D82：环境失效题剔除） ----------------

_PATCH_README = (
    "--- a/README.md\n+++ b/README.md\n"
    "@@ -1 +1 @@\n-# t\n+# t fixed\n"
)
_PATCH_MISSING = (
    "--- a/bug.py\n+++ b/bug.py\n"
    "@@ -1 +1 @@\n-def f():\n+def g():\n"
)


def test_gold_check_cleanly_applies(repo: Path) -> None:
    """gold 补丁（真 diff，改 base 里存在的文件）能应用到 base commit → 环境有效。"""
    assert gold_check(repo, _base_commit(repo), _PATCH_README) is True


def test_gold_check_unapplicable_is_env_invalid(repo: Path) -> None:
    """gold 补丁改 base 里不存在的文件（bug.py 在 base commit 之后才加）→ 环境失效。"""
    assert gold_check(repo, _base_commit(repo), _PATCH_MISSING) is False


def test_gold_check_simplified_text_is_valid(repo: Path) -> None:
    """无 hunk 的简化文本（相似度兜底用）无法 apply 校验 → 放行，防误伤。"""
    assert gold_check(repo, _base_commit(repo), "+++ README.md\n+# fixed\n") is True


def test_gold_check_empty_patch_is_valid(repo: Path) -> None:
    """无 gold_patch → 无法校验，视为有效（保持原行为）。"""
    assert gold_check(repo, _base_commit(repo), "") is True


@pytest.mark.asyncio
async def test_eval_runner_gold_valid_filters_env_invalid(repo: Path, tmp_path: Path) -> None:
    """gold 可应用题正常判分；gold 不可应用题剔除（invalid），metrics.total 只计 valid。"""
    llm = FakeLLM([tool_call("Bash", {"command": "echo fixed > flag.txt"}), done("改完了")])
    a = EvalTask(
        instance_id="g1",
        base_commit=_base_commit(repo),
        problem_statement="制造 flag.txt",
        gold_patch=_PATCH_README,
        test_cmd="test -f flag.txt",
    )
    b = EvalTask(
        instance_id="g2",
        base_commit=_base_commit(repo),
        problem_statement="修 bug",
        gold_patch=_PATCH_MISSING,  # base 无 bug.py → 环境失效
        test_cmd="test -f flag.txt",
    )
    report = await _run_eval(repo, tmp_path, llm, a, b)
    assert report.resolved == ["g1"]
    assert report.invalid == ["g2"]
    assert report.metrics.total == 1  # 只计 valid 题
    assert report.metrics.resolved == 1
    assert report.failed == []
    assert [t.env_valid for t in report.tasks] == [True, False]
    assert "g2" in report.summary()  # 报表标注剔除


# ---- D94：跑批异常题不重复归类（B3 实测：异常题被 Docker 判分二次归类） -----


@pytest.mark.asyncio
async def test_eval_runner_exception_task_not_reclassified(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跑批异常题从 report.tasks 摘除 → 不再进 Docker 判分（同题只记一次，防二次归类）。"""
    llm = FakeLLM([done("ok")])
    runner = _runner(tmp_path, llm)
    manager = AgentManager([BUILTIN_AGENTS_DIR])
    manager.scan()
    definition = manager.get("general-purpose")
    assert definition is not None
    task = EvalTask(
        instance_id="x1",
        base_commit=_base_commit(repo),
        problem_statement="修 bug",
    )
    ev = EvalRunner(
        runner, definition=definition, source_repo=repo,
        work_dir=tmp_path / "eval", task_loader=lambda: [task],
        docker=DockerJudgeConfig(harness_script=Path("run_harness.py"), python=Path("python")),
    )

    async def _boom(*_a: object, **_kw: object) -> object:
        raise KeyError("status")  # 复现并发偶发崩溃

    monkeypatch.setattr(ev._runner, "run_to_completion", _boom)
    judged: list[list[EvalTask]] = []

    def _fake_judge(
        config: object, tasks: list[EvalTask], *, eval_work_dir: Path, eval_run_id: str
    ) -> list[object]:
        judged.append(tasks)
        return []

    monkeypatch.setattr("kdagent.eval.runner.judge", _fake_judge)

    report = await ev.run("run-x")
    # 异常只记一次（跑批异常），不因 model_patch 空被 Docker 判分再归 empty_patch
    assert len(report.failed) == 1
    assert report.failed[0].kind == "harness_fault"
    assert "跑批异常" in report.failed[0].reason
    assert report.metrics.total == 1
    # judge 未被调用：异常题已摘除 + D96 预检对空 tasks 短路（更彻底，不再空转一轮）
    assert judged == []


# ---- D96 治理①：评测 prompt 环境说明 ----

def test_build_prompt_env_note_with_work_dir() -> None:
    """传 work_dir → prompt 含 Windows 环境说明（路径 / 可 pip install / 无 /testbed）。"""
    from kdagent.eval.runner import EvalRunner

    prompt = EvalRunner._build_prompt(_task(), Path(r"D:\eval\falconry__falcon-2404"))
    assert "问题描述" in prompt
    assert "修 bug" in prompt
    assert r"D:\eval\falconry__falcon-2404" in prompt
    assert "Windows" in prompt
    assert "/testbed" in prompt  # 明示不存在容器路径
    assert "pip install -e ." in prompt  # 引导自装依赖，防 pip download 病
    assert "必须基于工作目录内的源码" in prompt


def test_build_prompt_no_env_note_without_work_dir() -> None:
    """work_dir=None → 无环境说明段（向后兼容，纯问题描述）。"""
    from kdagent.eval.runner import EvalRunner

    prompt = EvalRunner._build_prompt(_task())
    assert "问题描述" in prompt
    assert "Windows" not in prompt
    assert "/testbed" not in prompt


# ---- D96 治理②：patch 前置预检 + error 细分 ----

_VALID_PATCH = (
    "diff --git a/README.md b/README.md\n"
    "--- a/README.md\n"
    "+++ b/README.md\n"
    "@@ -1 +1 @@\n"
    "-# t\n"
    "+# t2\n"
)


def test_patch_applies_valid(repo: Path) -> None:
    from kdagent.eval.runner import patch_applies

    assert patch_applies(repo, _base_commit(repo), _VALID_PATCH) is None


def test_patch_applies_crlf_normalized(repo: Path) -> None:
    """CRLF 行尾 patch 归一化后应可应用（D95 同款处理）。"""
    from kdagent.eval.runner import patch_applies

    crlf = _VALID_PATCH.replace("\n", "\r\n")
    assert patch_applies(repo, _base_commit(repo), crlf) is None


def test_patch_applies_missing_file(repo: Path) -> None:
    """引用 base 树不存在的文件（如临时文件 hunk）→ 拦截报错。"""
    from kdagent.eval.runner import patch_applies

    bad = (
        "diff --git a/bug.py b/bug.py\n"
        "--- a/bug.py\n"
        "+++ b/bug.py\n"
        "@@ -1 +1 @@\n"
        "-def f():\n"
        "+def g():\n"
    )
    err = patch_applies(repo, _base_commit(repo), bad)
    assert err is not None  # bug.py 只在 HEAD，base 树无此文件


def test_patch_applies_context_mismatch(repo: Path) -> None:
    from kdagent.eval.runner import patch_applies

    bad = _VALID_PATCH.replace("-# t", "-# 不存在的上下文")
    assert patch_applies(repo, _base_commit(repo), bad) is not None


def test_patch_applies_empty(repo: Path) -> None:
    from kdagent.eval.runner import patch_applies

    assert "空补丁" in patch_applies(repo, _base_commit(repo), "")


@pytest.mark.asyncio
async def test_docker_judge_report_precheck_intercepts(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """坏 patch 被前置预检拦截 → 不进 Docker 判分（judge 不调用）。"""
    llm = FakeLLM([done("ok")])
    runner = _runner(tmp_path, llm)
    manager = AgentManager([BUILTIN_AGENTS_DIR])
    manager.scan()
    definition = manager.get("general-purpose")
    assert definition is not None
    task = EvalTask(
        instance_id="x1",
        base_commit=_base_commit(repo),
        problem_statement="修 bug",
    )
    ev = EvalRunner(
        runner, definition=definition, source_repo=repo,
        work_dir=tmp_path / "eval", task_loader=lambda: [task],
        docker=DockerJudgeConfig(harness_script=Path("run_harness.py"), python=Path("python")),
    )
    judged: list[list[EvalTask]] = []

    def _fake_judge(
        config: object, tasks: list[EvalTask], *, eval_work_dir: Path, eval_run_id: str
    ) -> list[object]:
        judged.append(tasks)
        return []

    monkeypatch.setattr("kdagent.eval.runner.judge", _fake_judge)

    report = await ev.run("run-x")

    # 跑批产空 patch（agent 无改动）→ 预检拦截为「空补丁」，不调 judge
    assert judged == []
    assert len(report.failed) == 1
    assert "patch 前置预检不通过" in report.failed[0].reason


def test_error_reason_subdivides_patch_apply(tmp_path: Path) -> None:
    """日志含 Patch Apply Failed → 细分 patch 应用失败（B4 2322 实录）。"""
    from kdagent.eval.docker_judge import _error_reason

    out = tmp_path / "out"
    logdir = out / "logs" / "run_evaluation" / "judge-x" / "kdagent" / "i1"
    logdir.mkdir(parents=True)
    (logdir / "run_instance.log").write_text(
        ">>> Patch Apply Failed:\nmalformed patch at line 9\n"
    )
    assert "patch 应用失败" in _error_reason(out, "judge-x", "kdagent", "i1")


def test_error_reason_subdivides_container(tmp_path: Path) -> None:
    """日志含 daemon 错误 → 细分容器启动失败。"""
    from kdagent.eval.docker_judge import _error_reason

    out = tmp_path / "out"
    logdir = out / "logs" / "run_evaluation" / "judge-x" / "kdagent" / "i2"
    logdir.mkdir(parents=True)
    (logdir / "run_instance.log").write_text(
        "Error response from daemon: pull access denied for image\n"
    )
    assert "容器启动失败" in _error_reason(out, "judge-x", "kdagent", "i2")


def test_error_reason_fallback_generic(tmp_path: Path) -> None:
    """无日志 → 回退通用描述。"""
    from kdagent.eval.docker_judge import _error_reason

    out = tmp_path / "out"
    assert "容器启动/依赖安装失败" in _error_reason(out, "judge-x", "kdagent", "i3")


def test_extract_patch_ends_with_newline(repo: Path, tmp_path: Path) -> None:
    """extract_patch 重建补丁补回末尾换行 → git apply 可接受（预检不拦）。"""
    from kdagent.eval.runner import extract_patch, patch_applies, seal_copy

    dest = seal_copy(repo, _base_commit(repo), tmp_path / "sealed")
    (dest / "README.md").write_text("# t2\n", encoding="utf-8")
    patch = extract_patch(dest)
    assert patch.endswith("\n")
    assert patch_applies(repo, _base_commit(repo), patch) is None


# ---- D96 治理③：封史副本依赖预装（venv + pip install -e .） --------------------


def _fake_subprocess_run(*codes: int) -> tuple[Any, list[list[str]]]:
    """返回 (替身, 捕获的命令列表)。依次返回给定 returncode。"""
    calls: list[list[str]] = []
    it = iter(codes)

    def run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)
        code = next(it, 0)
        return subprocess.CompletedProcess(cmd, code, stdout="", stderr="boom")

    return run, calls


def test_install_repo_deps_creates_venv_and_pip_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命令序列正确：先 `python -m venv`，再 `.venv/Scripts/pip install -e`。"""
    import sys

    from kdagent.eval.runner import install_repo_deps

    sealed = tmp_path / "sealed"
    sealed.mkdir()
    fake, calls = _fake_subprocess_run(0, 0)
    monkeypatch.setattr("kdagent.eval.runner.subprocess.run", fake)
    ok, msg = install_repo_deps(sealed, python=sys.executable)
    assert ok
    assert "已预装" in msg
    venv = sealed / ".venv"
    pip = venv / "Scripts" / "pip.exe" if os.name == "nt" else venv / "bin" / "pip"
    assert calls == [
        [sys.executable, "-m", "venv", str(venv)],
        [str(pip), "install", "-e", str(sealed)],
    ]


def test_install_repo_deps_venv_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """venv 创建失败 → 返回 (False, 原因)，不阻断跑批。"""
    from kdagent.eval.runner import install_repo_deps

    sealed = tmp_path / "sealed"
    sealed.mkdir()
    fake, _ = _fake_subprocess_run(1)
    monkeypatch.setattr("kdagent.eval.runner.subprocess.run", fake)
    ok, msg = install_repo_deps(sealed)
    assert not ok
    assert "venv 创建失败" in msg


def test_install_repo_deps_pip_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pip install -e 失败 → 返回 (False, 原因)，模型可自装。"""
    from kdagent.eval.runner import install_repo_deps

    sealed = tmp_path / "sealed"
    sealed.mkdir()
    fake, _ = _fake_subprocess_run(0, 1)
    monkeypatch.setattr("kdagent.eval.runner.subprocess.run", fake)
    ok, msg = install_repo_deps(sealed)
    assert not ok
    assert "pip install -e 失败" in msg


def test_install_repo_deps_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """子进程超时 → 返回 (False, 预装异常)，不抛。"""
    from kdagent.eval.runner import install_repo_deps

    sealed = tmp_path / "sealed"
    sealed.mkdir()

    def boom(cmd: list[str], **_: object) -> object:
        raise subprocess.TimeoutExpired(" ".join(cmd), timeout=180)

    monkeypatch.setattr("kdagent.eval.runner.subprocess.run", boom)
    ok, msg = install_repo_deps(sealed)
    assert not ok
    assert "预装异常" in msg


@pytest.mark.asyncio
async def test_eval_runner_preinstall_gates_install_repo_deps(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """preinstall=True 时 _run_task 调 install_repo_deps；False 不调。"""
    installed: list[Path] = []
    monkeypatch.setattr(
        "kdagent.eval.runner.install_repo_deps",
        lambda sealed, python=None: installed.append(Path(sealed)) or (True, "ok"),
    )
    llm = FakeLLM([tool_call("Bash", {"command": "echo fixed > flag.txt"}), done("改完了")])
    runner = _runner(tmp_path, llm)
    manager = AgentManager([BUILTIN_AGENTS_DIR])
    manager.scan()
    definition = manager.get("general-purpose")
    assert definition is not None
    task = EvalTask(
        instance_id="t-pre",
        base_commit=_base_commit(repo),
        problem_statement="制造一个 flag.txt",
        test_cmd="test -f flag.txt",
    )
    ev = EvalRunner(
        runner, definition=definition, source_repo=repo,
        work_dir=tmp_path / "eval", task_loader=lambda: [task], preinstall=True,
    )
    report = await ev.run("run-pre")
    assert report.resolved == ["t-pre"]
    assert len(installed) == 1
    assert (installed[0] / "README.md").is_file()  # 传给 install 的是封史副本

    # preinstall=False：不触发预装
    ev2 = EvalRunner(
        runner, definition=definition, source_repo=repo,
        work_dir=tmp_path / "eval2", task_loader=lambda: [task], preinstall=False,
    )
    await ev2.run("run-pre2")
    assert len(installed) == 1  # 仍只有一次
