"""Harness：测试驱动自闭环 / 规则量化追踪 / 测试基建探测（规格 12）。

M5 遗留补齐（v0.5.0 之后）：TestRunner 工具 + TestingEvent（tools/test_runner.py +
engine/events.py）实现 §3.1 自测闭环；rules.py 实现 §3.6 规则量化；detect.py 实现
§3.1 激活条件 2 的测试基建探测；checkpoints.py 实现 §3.3 双层检查点（声明驱动 +
行为观察兜底）。错误模式沉淀（08）/ Replan 接入标注遗留。
"""

from kdagent.harness.checkpoints import (
    LARGE_CHANGE_THRESHOLD,
    REINJECT_COOLDOWN,
    STALE_TODO_THRESHOLD,
    CheckpointEvent,
    build_checkpoint_reminder,
    build_large_change_warning,
    build_stale_todo_reminder,
    todo_progress,
)
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
    "LARGE_CHANGE_THRESHOLD",
    "REINJECT_COOLDOWN",
    "STALE_TODO_THRESHOLD",
    "CheckpointEvent",
    "RuleStats",
    "build_checkpoint_reminder",
    "build_large_change_warning",
    "build_stale_todo_reminder",
    "todo_progress",
    "accept_criteria_written",
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
