"""评估体系（规格 11）：数据模型 / 评测流水线 / 失败归类 / CLI 子命令（M5-e MVP）。"""

from kdagent.eval.cli import load_tasks_file, run_annotate_cli, run_eval_cli, run_review_cli
from kdagent.eval.model import (
    EvalReport,
    EvalTask,
    FailureCase,
    FailureKind,
    RunMetrics,
)
from kdagent.eval.runner import (
    EvalError,
    EvalRunner,
    classify,
    extract_patch,
    gold_similarity,
    seal_copy,
)

__all__ = [
    "EvalError",
    "EvalReport",
    "EvalRunner",
    "EvalTask",
    "FailureCase",
    "FailureKind",
    "RunMetrics",
    "classify",
    "extract_patch",
    "gold_similarity",
    "load_tasks_file",
    "run_annotate_cli",
    "run_eval_cli",
    "run_review_cli",
    "seal_copy",
]
