"""工具结果入口处理（规格 01 §5.2 L1 / §5.3 L2）。

写在历史之前（01 P3）：单条结果 > `TOOL_RESULT_SAVE_THRESHOLD` 或一轮聚合
> `TOOL_RESULT_AGGREGATE_THRESHOLD` 时，完整内容写盘，历史中只放预览 + 路径。

决策 D6：原始内容默认落盘保留（L1/L2 共用）。

L2（M2-b）：单条未超 L1 阈值的结果，若 `should_online_compress` 判定通过
（中等大小 + 低密度 + 经济性），交给 `L2Compressor` 现场摘要，历史中放摘要 + 路径。
摘要失败回退原文（不打断 Agent 循环）。

读回闭环（§5.2）：ReadFile 读回落盘文件 → 工具层已置 `persist_exempt`，
本处理器跳过 L1 与 L2（否则永远读不到全文）。
"""

from __future__ import annotations

from pathlib import Path

from kdagent.context.compactor import (
    PREVIEW_CHARS,
    L2Compressor,
    write_persisted,
)
from kdagent.context.history import PersistedOutput, ProcessedToolResult
from kdagent.engine.llm.base import Payload
from kdagent.tools.base import ToolResult

# 01 §9.1：L1 单条落盘阈值 / 单轮聚合阈值 / 预览长度（全参数化，可调可标定）
TOOL_RESULT_SAVE_THRESHOLD = 50_000
TOOL_RESULT_AGGREGATE_THRESHOLD = 200_000


class ToolResultHandler:
    """L1 + L2 判定与处理：单条超限落盘、单轮聚合超限从最大的开始落盘。

    `persist_dir`：落盘根目录（`{sessions_dir}/{sid}/tool-results/`），
    由调用方（ContextManager）按会话装配。`l2` 为 None 时只做 L1（纯落盘模式）。
    """

    def __init__(
        self,
        persist_dir: Path,
        *,
        save_threshold: int = TOOL_RESULT_SAVE_THRESHOLD,
        aggregate_threshold: int = TOOL_RESULT_AGGREGATE_THRESHOLD,
        preview_chars: int = PREVIEW_CHARS,
        l2: L2Compressor | None = None,
    ) -> None:
        self._persist_dir = persist_dir
        self._save_threshold = save_threshold
        self._aggregate_threshold = aggregate_threshold
        self._preview_chars = preview_chars
        self._l2 = l2

    async def handle_single(
        self, result: ToolResult, p_tokens: int = 0, prefix: Payload | None = None
    ) -> ProcessedToolResult:
        """单条处理（§5.5 决策流程）：persist_exempt → L1 落盘 → L2 摘要 → 原样。

        `p_tokens`：已有上下文 token 数（L2 经济性判定用，01 §5.3）；
        `prefix`：主调用 payload 前缀（L2 摘要复用前缀缓存）。
        """
        if result.persist_exempt:
            return ProcessedToolResult(content=result.content)
        if len(result.content) > self._save_threshold:
            persisted = self._persist(result.tool_use_id, result.content)
            return ProcessedToolResult(content=_preview_text(persisted), persisted=persisted)
        l2 = self._l2
        if l2 is not None:
            decision = l2.decide(result, p_tokens)  # 07 T8：判定点落 context.l2_decide span
            if decision.accepted:
                try:
                    return await l2.compress(result, prefix)
                except Exception:
                    return ProcessedToolResult(content=result.content)  # 摘要失败回退原文
        return ProcessedToolResult(content=result.content)

    async def handle_batch(
        self, results: list[ToolResult], p_tokens: int = 0, prefix: Payload | None = None
    ) -> list[ProcessedToolResult]:
        """单轮聚合处理：先逐条（L1 单条 / L2 / 原样），再合计总量聚合 L1。"""
        processed = [await self.handle_single(r, p_tokens, prefix) for r in results]
        return self._aggregate(results, processed)

    def _aggregate(
        self, results: list[ToolResult], processed: list[ProcessedToolResult]
    ) -> list[ProcessedToolResult]:
        """从最大的未落盘结果开始落盘替换成预览，直到总量回到预算以内（§5.2）。

        单条已落盘/已摘要的结果 content 已是小文本，聚合只挑"未落盘"的。
        文件名沿用原始 tool_use_id（`results[i].tool_use_id`），保证可追溯。
        """
        result = list(processed)
        while sum(len(p.content) for p in result) > self._aggregate_threshold:
            candidates = [(i, p) for i, p in enumerate(result) if p.persisted is None]
            if not candidates:
                break  # 全部已落盘仍超限：异常边界，不再处理（L3 兜底会看总量）
            i, p = max(candidates, key=lambda cp: len(cp[1].content))
            persisted = self._persist(results[i].tool_use_id, p.content)
            result[i] = ProcessedToolResult(content=_preview_text(persisted), persisted=persisted)
        return result

    def _persist(self, key: str, content: str) -> PersistedOutput:
        return write_persisted(self._persist_dir, key, content, self._preview_chars)


def _preview_text(po: PersistedOutput) -> str:
    """历史中的预览块（01 §5.2 格式）。"""
    return (
        "<persisted-output>\n"
        f"输出太大（{po.full_size // 1024}KB），完整内容已保存到：\n"
        f"{po.path}\n\n"
        "预览（前 2KB）：\n"
        f"{po.preview}\n"
        "</persisted-output>"
    )
