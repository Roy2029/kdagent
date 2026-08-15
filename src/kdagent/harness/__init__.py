"""Harness：测试驱动自闭环 / 规则量化追踪 / 测试基建探测（规格 12）。

M5 遗留补齐（v0.5.0 之后）：TestRunner 工具 + TestingEvent（tools/test_runner.py +
engine/events.py）实现 §3.1 自测闭环；rules.py 实现 §3.6 规则量化；detect.py 实现
§3.1 激活条件 2 的测试基建探测。双层检查点 / 错误模式沉淀（08）/ Replan 接入标注
遗留。
"""

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
    test_failed_rerun,
)

__all__ = [
    "RuleStats",
    "ToolCallRecord",
    "accept_criteria_written",
    "detect_test_infra",
    "list_rules",
    "no_test_file_edits",
    "read_before_edit",
    "records_from_blocks",
    "records_from_spans",
    "rule_adherence",
    "test_failed_rerun",
]
