"""Harness 测试闭环测试（规格 12，M5 遗留补齐）。

TestRunner 工具（隔离沙箱 / 结构化 TestingEvent / 回归检测 / 失败归因解析）、
规则量化追踪（§3.6 四规则）、测试基建探测（§3.1 激活条件 2）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kdagent.config import Config
from kdagent.engine.events import AgentEvent, TestingEvent
from kdagent.engine.messages import ToolUseBlock
from kdagent.harness.detect import detect_test_infra
from kdagent.harness.rules import (
    RuleStats,
    ToolCallRecord,
    accept_criteria_written,
    list_rules,
    no_test_file_edits,
    read_before_edit,
    records_from_blocks,
    records_from_spans,
    rule_adherence,
)

# 别名避免 pytest 误收集：rules.test_failed_rerun 以 test_ 开头会被当测试函数
from kdagent.harness.rules import test_failed_rerun as rules_test_failed_rerun
from kdagent.obs.model import Span
from kdagent.tools import TestRunner, parse_failed_tests
from kdagent.tools.base import ToolContext

PY = sys.executable


def _ctx(tmp_path: Path, events: list[AgentEvent] | None = None) -> ToolContext:
    return ToolContext(
        work_dir=tmp_path,
        config=Config(),
        tool_use_id="t1",
        events=(lambda e: events.append(e)) if events is not None else None,
    )


def _probe(tmp_path: Path, code: str) -> Path:
    """探针脚本文件：避免 shell 引号地狱，跨平台执行 python 断言。"""
    path = tmp_path / "probe.py"
    path.write_text(code, encoding="utf-8")
    return path


# ---- parse_failed_tests：失败测试名归因解析 ----


def test_parse_failed_tests_pytest_summary() -> None:
    out = (
        "============================= short test summary info ==============================\n"
        "FAILED tests/test_math.py::test_add - AssertionError: 1 != 2\n"
        "FAILED tests/test_math.py::test_sub - TypeError: unsupported operand\n"
    )
    assert parse_failed_tests(out) == [
        "tests/test_math.py::test_add",
        "tests/test_math.py::test_sub",
    ]


def test_parse_failed_tests_inline_and_header() -> None:
    inline = "tests/test_math.py::test_add FAILED   [ 50%]\n"
    assert parse_failed_tests(inline) == ["tests/test_math.py::test_add"]
    header = "____ test_add ____\n"
    assert parse_failed_tests(header) == ["test_add"]


def test_parse_failed_tests_dedup_and_empty() -> None:
    out = (
        "FAILED tests/test_math.py::test_add - AssertionError\n"
        "tests/test_math.py::test_add FAILED\n"
    )
    assert parse_failed_tests(out) == ["tests/test_math.py::test_add"]
    assert parse_failed_tests("all passed\n") == []


# ---- TestRunner：passed / failed / regression_detected ----


async def test_testrunner_passed_on_exit_zero(tmp_path: Path) -> None:
    events: list[AgentEvent] = []
    result = await TestRunner().execute(
        _ctx(tmp_path, events), {"command": f'"{PY}" -c "print(1)"'}
    )
    assert "[TestRunner] status=passed" in result.content
    assert result.is_error is False
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, TestingEvent)
    assert ev.status == "passed"
    assert ev.failed_tests == ()


async def test_testrunner_failed_sets_is_error(tmp_path: Path) -> None:
    result = await TestRunner().execute(
        _ctx(tmp_path), {"command": f'"{PY}" -c "import sys; sys.exit(2)"'}
    )
    assert "[TestRunner] status=failed" in result.content
    assert result.is_error is True


async def test_testrunner_regression_detected(tmp_path: Path) -> None:
    """主测试过但回归命令挂 → regression_detected（Pass2Pass 被碰坏，12 §3.1）。"""
    result = await TestRunner().execute(
        _ctx(tmp_path),
        {
            "command": f'"{PY}" -c "print(1)"',
            "regression_command": f'"{PY}" -c "import sys; sys.exit(1)"',
        },
    )
    assert "[TestRunner] status=regression_detected" in result.content
    assert result.is_error is True


async def test_testrunner_sandbox_current_uses_work_dir(tmp_path: Path) -> None:
    """sandbox=current（默认）：在 ctx.work_dir 目录执行命令。"""
    (tmp_path / "marker.txt").write_text("hello", encoding="utf-8")
    probe = _probe(
        tmp_path,
        "import pathlib\n"
        "assert pathlib.Path('marker.txt').read_text() == 'hello'\n"
        "print('read ok')",
    )
    result = await TestRunner().execute(
        _ctx(tmp_path), {"command": f'"{PY}" "{probe}"'}
    )
    assert "[TestRunner] status=passed" in result.content


async def test_testrunner_sandbox_temp_excludes_git(tmp_path: Path) -> None:
    """temp 沙箱：复制工作目录副本（排除 .git/.venv 等），跑挂不污染当前目录。"""
    (tmp_path / "marker.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("fake", encoding="utf-8")
    probe = _probe(
        tmp_path,
        "import pathlib\n"
        "assert pathlib.Path('marker.txt').is_file()\n"
        "assert not pathlib.Path('.git').exists()\n"
        "print('isolated ok')",
    )
    result = await TestRunner().execute(
        _ctx(tmp_path), {"command": f'"{PY}" "{probe}"', "sandbox": "temp"}
    )
    assert "[TestRunner] status=passed" in result.content
    # 当前目录不被污染：.git 原样保留（temp 是隔离副本）
    assert (tmp_path / ".git" / "config").is_file()


async def test_testrunner_sandbox_worktree(tmp_path: Path) -> None:
    """worktree 沙箱：resolve_worktree 注入解析名 → 路径（cli 传 worktree_manager.path）。"""
    wt_dir = tmp_path / "wt"
    wt_dir.mkdir()
    (wt_dir / "marker.txt").write_text("wt", encoding="utf-8")
    probe = _probe(
        wt_dir,
        "import pathlib\n"
        "assert pathlib.Path('marker.txt').read_text() == 'wt'\n"
        "print('wt ok')",
    )
    tool = TestRunner(resolve_worktree=lambda name: wt_dir if name == "abc" else None)
    result = await tool.execute(
        _ctx(tmp_path), {"command": f'"{PY}" "{probe}"', "sandbox": "worktree:abc"}
    )
    assert "[TestRunner] status=passed" in result.content


async def test_testrunner_sandbox_worktree_missing_is_error(tmp_path: Path) -> None:
    tool = TestRunner(resolve_worktree=lambda name: None)
    result = await tool.execute(
        _ctx(tmp_path), {"command": f'"{PY}" -c "print(1)"', "sandbox": "worktree:nope"}
    )
    assert result.is_error is True
    assert "worktree" in result.content


async def test_testrunner_sandbox_unknown_is_error(tmp_path: Path) -> None:
    result = await TestRunner().execute(
        _ctx(tmp_path), {"command": f'"{PY}" -c "print(1)"', "sandbox": "bogus"}
    )
    assert result.is_error is True


async def test_testrunner_timeout_is_error(tmp_path: Path) -> None:
    result = await TestRunner().execute(
        _ctx(tmp_path),
        {"command": f'"{PY}" -c "import time; time.sleep(5)"', "timeout": 1},
    )
    assert result.is_error is True
    assert "超时" in result.content


def test_testrunner_validate_input(tmp_path: Path) -> None:
    tool = TestRunner()
    assert tool.validate_input({})  # command 缺失
    assert tool.validate_input({"command": "  "})  # command 空白
    assert tool.validate_input({"command": "x", "timeout": 0})  # timeout 非法
    assert tool.validate_input({"command": "x", "timeout": 30}) == []


def test_testrunner_meta() -> None:
    tool = TestRunner()
    assert tool.name == "TestRunner"
    assert tool.is_read_only() is False
    assert tool.is_concurrency_safe({}) is False  # 测试有副作用，串行


# ---- rule_adherence：四规则聚合（§3.6） ----


def _block(name: str, input: dict[str, object]) -> ToolUseBlock:
    return ToolUseBlock(id=f"t{hash(name)}", name=name, input=input)


def test_read_before_edit_adhered_and_violated() -> None:
    records = records_from_blocks(
        [
            _block("ReadFile", {"path": "src/a.py"}),
            _block("EditFile", {"path": "src/a.py"}),
            _block("EditFile", {"path": "src/b.py"}),  # 无 read → 违反
            _block("Grep", {"pattern": "x"}),
            _block("WriteFile", {"path": "src/c.py"}),  # grep 后 → 遵守
        ]
    )
    stats = read_before_edit(records)
    assert stats.total == 3
    assert stats.adhered == 2
    assert stats.adherence() == pytest.approx(2 / 3)


def test_test_failed_rerun() -> None:
    records = records_from_blocks(
        [
            _block("TestRunner", {"command": "pytest"}),
            _block("EditFile", {"path": "src/a.py"}),  # 修复动作
            _block("TestRunner", {"command": "pytest"}),
        ]
    )
    stats = rules_test_failed_rerun(records)
    assert stats.total == 0  # 无 is_error 失败样本（records_from_blocks 默认 False）
    assert stats.adhered == 0


def test_test_failed_rerun_detects_rerun() -> None:
    records: list[ToolCallRecord] = [
        ToolCallRecord(name="TestRunner", input={}, is_error=True, order=1),  # 失败
        ToolCallRecord(name="EditFile", input={"path": "src/a.py"}, is_error=False, order=2),
        ToolCallRecord(name="TestRunner", input={}, is_error=False, order=3),  # 重跑
    ]
    stats = rules_test_failed_rerun(records)
    assert stats.total == 1
    assert stats.adhered == 1


def test_no_test_file_edits() -> None:
    records = records_from_blocks(
        [
            _block("WriteFile", {"path": "src/a.py"}),
            _block("EditFile", {"path": "tests/test_a.py"}),  # 碰测试 → 违反
            _block("EditFile", {"path": "src/b.py"}),
        ]
    )
    stats = no_test_file_edits(records)
    assert stats.total == 3
    assert stats.adhered == 2


def test_records_from_spans_uses_input_for_path_rules() -> None:
    spans = [
        Span(
            span_id="s1",
            trace_id="t",
            parent_span_id=None,
            name="tool.exec",
            kind="tool",
            status="ok",
            attributes={"tool": "WriteFile", "input": {"path": "src/a.py"}},
        ),
        Span(
            span_id="s2",
            trace_id="t",
            parent_span_id=None,
            name="tool.exec",
            kind="tool",
            status="ok",
            attributes={"tool": "EditFile", "input": {"path": "tests/test_a.py"}},
        ),
        Span(
            span_id="s3",
            trace_id="t",
            parent_span_id=None,
            name="tool.exec",
            kind="tool",
            status="error",
            attributes={"tool": "EditFile", "input": {"path": "src/b.py"}, "is_error": True},
        ),
    ]
    records = records_from_spans(spans)
    assert [r.name for r in records] == ["WriteFile", "EditFile", "EditFile"]
    assert records[1].input == {"path": "tests/test_a.py"}
    assert records[2].is_error is True
    # 路径判定规则直接消费真实 trace（不再依赖 records_from_blocks）
    stats = no_test_file_edits(records)
    assert stats.total == 3
    assert stats.adhered == 2


def test_records_from_spans_skips_non_tool_and_missing_input() -> None:
    spans = [
        Span(span_id="s1", trace_id="t", parent_span_id=None, name="llm.call", kind="client"),
        Span(
            span_id="s2",
            trace_id="t",
            parent_span_id=None,
            name="tool.exec",
            kind="tool",
            attributes={"tool": "ReadFile"},  # 旧 span 无 input → 兜底空 dict
        ),
    ]
    records = records_from_spans(spans)
    assert len(records) == 1
    assert records[0].name == "ReadFile"
    assert records[0].input == {}


def test_accept_criteria_written() -> None:
    records = records_from_blocks(
        [
            _block(
                "TodoWrite",
                {
                    "todos": [
                        {
                            "content": "目标",
                            "tasks": [
                                {
                                    "content": "任务",
                                    "steps": [
                                        {"description": "修 bug", "accept_criteria": "测试通过"},
                                        {"description": "调研", "accept_criteria": ""},
                                    ],
                                }
                            ],
                        }
                    ]
                },
            )
        ]
    )
    stats = accept_criteria_written(records)
    assert stats.total == 2
    assert stats.adhered == 1
    assert stats.adherence() == 0.5


def test_rule_adherence_dispatch_and_unknown() -> None:
    assert list_rules() == [
        "accept_criteria_written",
        "no_test_file_edits",
        "read_before_edit",
        "test_failed_rerun",
    ]
    stats = rule_adherence("read_before_edit", [])
    assert isinstance(stats, RuleStats)
    with pytest.raises(ValueError, match="未知规则"):
        rule_adherence("not_a_rule", [])


def test_rule_stats_no_sample_fully_adhered() -> None:
    assert RuleStats("r", 0, 0).adherence() == 1.0


# ---- detect_test_infra：激活条件 2 探测 ----


def test_detect_pytest_ini(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    reminder = detect_test_infra(tmp_path)
    assert reminder is not None
    assert "pytest.ini" in reminder
    assert "TestRunner" in reminder


def test_detect_pyproject_pytest_marker(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n", encoding="utf-8"
    )
    assert detect_test_infra(tmp_path) is not None


def test_detect_test_files(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_math.py").write_text("x", encoding="utf-8")
    reminder = detect_test_infra(tmp_path)
    assert reminder is not None
    assert "test_math.py" in reminder


def test_detect_none(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x", encoding="utf-8")
    assert detect_test_infra(tmp_path) is None
