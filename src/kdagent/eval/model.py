"""评测数据模型（规格 11 §3.1，M5-e MVP）。

判分口径（§3.2 单题判定）：FAIL_TO_PASS 全过 且 PASS_TO_PASS 无损坏 → resolved。
MVP 判分双轨：task 提供 test_cmd 跑真实测试；否则回退 gold_patch 文本相似度。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# 失败归类五类（11 §3.4）
FailureKind = Literal[
    "not_located",      # 1 没定位到该改的文件
    "wrong_fix",        # 2 定位对但修法不对
    "regression",       # 3 修对但碰坏别的测试
    "harness_fault",    # 4 改动越滚越大/超时/工具报错/中途放弃
    "constraint_conflict",  # 5 约束冲突主动放弃
]


@dataclass(slots=True)
class EvalTask:
    """一道评测题（11 §3.1 EvalTask）。"""

    instance_id: str  # 题号
    repo: str = ""
    base_commit: str = ""  # bug 对应版本
    problem_statement: str = ""  # 只给「要解决什么问题」，不给「怎么解决」
    fail_to_pass: list[str] = field(default_factory=list)  # 原失败测试必须全过
    pass_to_pass: list[str] = field(default_factory=list)  # 原通过测试不能被碰坏
    gold_patch: str = ""  # 官方参考补丁（gold 校验用）
    test_cmd: str = ""  # 判分测试命令（可选；给了才跑真实测试）
    p2p_cmd: str = ""  # PASS_TO_PASS 判分测试命令（D81：给了则跑，破坏 → 不 resolved）
    constraint: str = ""  # 任务约束（如「不要改测试文件」）→ 类 5 标记


@dataclass(slots=True)
class FailureCase:
    """失败题 + 归类（11 §3.1/§3.4）。"""

    instance_id: str
    kind: FailureKind
    reason: str  # 一句话失败原因
    patch: str = ""  # model_patch（复查用）


@dataclass(slots=True)
class RunMetrics:
    """单版本指标（§3.6 三件事之一：质量/效率）。"""

    total: int = 0
    resolved: int = 0
    passed_to_passed: int = 0  # PASS_TO_PASS 无损坏的题数
    total_turns: int = 0
    total_tokens: int = 0  # 输入+输出合计（向后兼容：旧报告只读它）
    wall_s: float = 0.0
    input_tokens: int = 0  # 计价明细：输入
    output_tokens: int = 0  # 计价明细：输出
    cache_tokens: int = 0  # 计价明细：缓存命中（前缀缓存，D67）
    cost_cny: float = 0.0  # 估算成本（元；CostParams 计价，D67 补齐 §3.8「成本需计价表」）

    @property
    def resolve_rate(self) -> float:
        return self.resolved / self.total if self.total else 0.0


@dataclass(slots=True)
class EvalReport:
    """一次跑批的报告（11 §3.1 EvalReport）。"""

    run_id: str
    tasks: list[EvalTask] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    failed: list[FailureCase] = field(default_factory=list)
    metrics: RunMetrics = field(default_factory=RunMetrics)

    def summary(self) -> str:
        """文本报表（CLI 输出）。"""
        lines = [
            f"评测报告 run={self.run_id}：{self.metrics.resolved}/{self.metrics.total} 通过 "
            f"（{self.metrics.resolve_rate:.0%}）",
            f"轮次 {self.metrics.total_turns} · token {self.metrics.total_tokens} · "
            f"耗时 {self.metrics.wall_s:.1f}s",
        ]
        if self.metrics.cost_cny > 0:
            lines.append(
                f"估算成本 {self.metrics.cost_cny:.4f} 元"
                f"（入 {self.metrics.input_tokens} · 出 {self.metrics.output_tokens} · "
                f"缓存 {self.metrics.cache_tokens}）"
            )
        if self.resolved:
            lines.append("\n通过：" + "、".join(self.resolved))
        if self.failed:
            lines.append("\n失败归类：")
            for f in self.failed:
                lines.append(f"- {f.instance_id} [{f.kind}] {f.reason}")
        return "\n".join(lines)
