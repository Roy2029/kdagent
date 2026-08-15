"""CLI argparse 层测试（kdagent.cli.build_parser）。

D61-D65 给 eval 子命令加了一堆参数（--report/--annotate/--diff/--metrics/--workers），
此前无 parser 层测试——补上：参数解析、默认值、缺参报错。
"""

from __future__ import annotations

import pytest

from kdagent.cli import build_parser


def test_eval_requires_tasks() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["eval"])


def test_eval_workers_default_and_value() -> None:
    args = build_parser().parse_args(["eval", "tasks.json"])
    assert args.workers == 1  # 默认顺序跑

    args = build_parser().parse_args(["eval", "tasks.json", "--workers", "4"])
    assert args.workers == 4
    assert args.tasks == "tasks.json"


def test_eval_report_readonly_flag() -> None:
    args = build_parser().parse_args(["eval", "tasks.json", "--report", "run-1"])
    assert args.report == "run-1"


def test_eval_annotate_three_args() -> None:
    args = build_parser().parse_args(
        ["eval", "tasks.json", "--annotate", "run-1", "t1", "not_located", "--note", "定位错"]
    )
    assert args.annotate == ["run-1", "t1", "not_located"]
    assert args.note == "定位错"


def test_eval_diff_two_runs() -> None:
    args = build_parser().parse_args(["eval", "tasks.json", "--diff", "run-a", "run-b"])
    assert args.diff == ["run-a", "run-b"]


def test_eval_metrics_flag() -> None:
    args = build_parser().parse_args(["eval", "tasks.json", "--metrics", "run-1"])
    assert args.metrics == "run-1"


def test_eval_annotate_requires_exact_three() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["eval", "tasks.json", "--annotate", "run-1", "t1"])
