"""评测数据模型（规格 11 §3.1，M5-e MVP）。

判分口径（§3.2 单题判定）：FAIL_TO_PASS 全过 且 PASS_TO_PASS 无损坏 → resolved。
MVP 判分双轨：task 提供 test_cmd 跑真实测试；否则回退 gold_patch 文本相似度。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# 失败归类六类（11 §3.4；D4 v052 拆出 empty_patch，harness_fault 仅留基础设施故障）
FailureKind = Literal[
    "empty_patch",      # 0 模型未产出补丁（中途退出/未产出改动）
    "not_located",      # 1 没定位到该改的文件
    "wrong_fix",        # 2 定位对但修法不对
    "regression",       # 3 修对但碰坏别的测试
    "harness_fault",    # 4 基础设施故障（封史/判分/环境/工具报错）
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
    env_valid: bool = False  # gold 校验通过才 True（11 §3.2 步骤 3，D82：runner 校验后置位）
    test_cmd: str = ""  # 判分测试命令（可选；给了才跑真实测试）
    p2p_cmd: str = ""  # PASS_TO_PASS 判分测试命令（D81：给了则跑，破坏 → 不 resolved）
    constraint: str = ""  # 任务约束（如「不要改测试文件」）→ 类 5 标记
    # ---- 官方 Docker harness 判分信息（swebench.py loader 保留，224 落地读取）----
    test_cmds: list[str] = field(default_factory=list)  # 官方 test_cmds（Docker 判分用）
    test_patch: str = ""  # 官方测试补丁（harness 注入环境）
    log_parser: str = ""  # 官方日志解析器（pytest 等）
    # 判分后回填：模型补丁（Docker 判分 / 复核展示用）
    model_patch: str = ""
    # 判分后回填：Docker harness 逐题 F2P/P2P 明细（D4 v052，report.json 落盘）
    f2p_tests: list[str] = field(default_factory=list)  # 该题 FAIL_TO_PASS 测试
    p2p_tests: list[str] = field(default_factory=list)  # 该题 PASS_TO_PASS 测试
    p2p_failed: list[str] = field(default_factory=list)  # 被补丁碰坏的 P2P 测试（失败时非空）


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
    invalid: list[str] = field(default_factory=list)  # 环境失效被剔除的题（11 §3.2 步骤 3，D82）
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
        if self.invalid:
            lines.append(
                f"\n剔除环境失效 {len(self.invalid)} 道（gold 补丁无法应用，不计分）："
                + "、".join(self.invalid)
            )
        return "\n".join(lines)
