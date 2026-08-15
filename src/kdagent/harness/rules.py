"""Harness 规则量化追踪（规格 12 §3.6，M5 遗留补齐）。

度量「约束有效性」自身：从 07 tool.exec trace 聚合某条规则在一个 run 中的遵守率。
数据源全在 07 span（工具调用序 / is_error），本模块只定义判定规则，不新增埋点。

四规则（§3.6）：
- read_before_edit    先读后编辑：edit/write 前同窗口有 read（ReadFile/Grep/Glob）
- test_failed_rerun   测试失败必重跑：TestRunner 失败后跟了再次运行
- no_test_file_edits  禁止碰测试文件：write/edit 目标不命中 *_test.* 白名单外
- accept_criteria_written 完成型步骤必写判据：todo step 带 accept_criteria 的占比

纯函数可单测；调用方从「07 span / 对话 ToolUseBlock 序列」构造 ToolCallRecord。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from kdagent.engine.messages import ToolUseBlock
from kdagent.obs.model import Span

# 判定所需的最小工具集合（其余工具调用不参与对应规则判定）
_READ_TOOLS = {"ReadFile", "Grep", "Glob"}
_WRITE_TOOLS = {"EditFile", "WriteFile"}
_TEST_HINTS = ("test_", "_test", "tests/", "spec.", ".test.")


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """07 tool.exec span 的最小投影（rules 纯函数的数据源）。"""

    name: str
    input: dict[str, Any]
    is_error: bool
    order: int  # 调用序号（trace 内单调递增）


@dataclass(frozen=True, slots=True)
class RuleStats:
    """一条规则的遵守率度量（§3.6）。"""

    rule_id: str
    total: int
    adhered: int

    def adherence(self) -> float:
        """遵守率 = adhered / total；无观测样本视为完全遵守（无违反证据）。"""
        if self.total <= 0:
            return 1.0
        return self.adhered / self.total


# ---- 四规则（纯函数） ----


def read_before_edit(records: Iterable[ToolCallRecord]) -> RuleStats:
    """「先读后编辑」：每个 write/edit 前、自上一个 write/edit 以来的窗口内必须有 read。

    窗口语义（§3.6「edit 前同轮有 read」）：read 置窗口位，write/edit 消费判定后清位。
    连续 edit 之间无 read → 第二个违反。
    """
    total = adhered = 0
    window_has_read = False
    for rec in records:
        if rec.name in _WRITE_TOOLS:
            total += 1
            if window_has_read:
                adhered += 1
            window_has_read = False
        elif rec.name in _READ_TOOLS:
            window_has_read = True
    return RuleStats("read_before_edit", total, adhered)


def test_failed_rerun(records: Iterable[ToolCallRecord]) -> RuleStats:
    """「测试失败必重跑」（§3.6 / §3.1）：TestRunner 失败（is_error）后跟了再次运行。

    中间允许修复动作（edit/bash/write）——那正是归因→定向重试的中间环节；
    只要失败后有任一后续 TestRunner 调用即算重跑。
    """
    failures = rerun = 0
    pending = False
    for rec in records:
        if rec.name == "TestRunner":
            if rec.is_error:
                failures += 1
                pending = True
            elif pending:
                rerun += 1
                pending = False
    return RuleStats("test_failed_rerun", failures, rerun)


def no_test_file_edits(records: Iterable[ToolCallRecord]) -> RuleStats:
    """「禁止碰测试文件」（§3.6）：write/edit 目标路径不命中测试 hint。

    判分环境由 harness 注入测试（11），Agent 碰测试文件会与判分冲突——回归防护。
    """
    total = adhered = 0
    for rec in records:
        if rec.name not in _WRITE_TOOLS:
            continue
        path = str(rec.input.get("path") or rec.input.get("new_path") or "")
        if not path:
            continue  # input 未埋点/缺失：不可判定，不计入
        total += 1
        if not any(hint in path for hint in _TEST_HINTS):
            adhered += 1
    return RuleStats("no_test_file_edits", total, adhered)


def accept_criteria_written(records: Iterable[ToolCallRecord]) -> RuleStats:
    """「完成型步骤必写判据」（§3.2 T36）：todo step 带 accept_criteria 的占比。

    MVP 启发式：完成型 vs 探索型不可从工具调用机械区分，按「写了判据的 step 占比」
    度量——判据写得越全遵守率越高（引导写可机械验证形式）。
    """
    total = adhered = 0
    for rec in records:
        if rec.name != "TodoWrite":
            continue
        # TodoWrite 三层结构（03 §3.6）：todos → tasks → steps
        for todo in rec.input.get("todos") or []:
            for task in todo.get("tasks") or []:
                for step in task.get("steps") or []:
                    total += 1
                    if str(step.get("accept_criteria", "") or "").strip():
                        adhered += 1
    return RuleStats("accept_criteria_written", total, adhered)


_RULES: dict[str, Callable[[Iterable[ToolCallRecord]], RuleStats]] = {
    "read_before_edit": read_before_edit,
    "test_failed_rerun": test_failed_rerun,
    "no_test_file_edits": no_test_file_edits,
    "accept_criteria_written": accept_criteria_written,
}


def rule_adherence(
    rule_id: str, records: Iterable[ToolCallRecord]
) -> RuleStats:
    """§3.6 入口：按规则 id 聚合遵守率；未知规则抛 ValueError。"""
    fn = _RULES.get(rule_id)
    if fn is None:
        raise ValueError(f"未知规则：{rule_id}（可用：{sorted(_RULES)}）")
    return fn(records)


def list_rules() -> list[str]:
    return sorted(_RULES)


# ---- 数据源 helpers ----


def records_from_spans(spans: Iterable[Span]) -> list[ToolCallRecord]:
    """从 07 tool.exec span 序列投影工具调用记录（input 未埋点，留空 dict）。

    用于只需时序 + 成败的规则（read_before_edit / test_failed_rerun）；
    需要目标路径的规则（no_test_file_edits）请用 records_from_blocks。
    """
    records: list[ToolCallRecord] = []
    order = 0
    for span in spans:
        if span.name != "tool.exec":
            continue
        order += 1
        records.append(
            ToolCallRecord(
                name=str(span.attributes.get("tool", "")),
                input={},
                is_error=span.status == "error",
                order=order,
            )
        )
    return records


def records_from_blocks(blocks: Iterable[ToolUseBlock]) -> list[ToolCallRecord]:
    """从 ToolUseBlock 序列（含 input）构造记录——规则单测主入口。"""
    return [
        ToolCallRecord(name=b.name, input=dict(b.input), is_error=False, order=i)
        for i, b in enumerate(blocks, 1)
    ]
