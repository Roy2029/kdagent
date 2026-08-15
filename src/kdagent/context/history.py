"""上下文历史（规格 01 §8）。

M2-a 范围：`PersistedOutput`（L1 大结果落盘的元信息）+ `ConversationHistory`
（写路径包装，M2-a 复用 `02` ConversationManager 内部实现，L3 起才需要
estimate_tokens 等压缩侧能力）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from kdagent.engine.messages import Message

if TYPE_CHECKING:  # 仅类型解析；compactor 运行时依赖本模块，避免环导入
    from kdagent.context.compactor import CompressedOutput


@dataclass(frozen=True, slots=True)
class PersistedOutput:
    """L1 落盘结果（01 §5.2 / §8）：原始内容写盘，历史中只放预览 + 路径。"""

    preview: str
    path: str  # {sessions_dir}/{sid}/tool-results/{tool_id}.txt
    full_size: int


@dataclass(slots=True)
class ProcessedToolResult:
    """入口处理后的结果：content 是写入历史的最终形态（可能是预览/摘要+路径）。

    persisted：L1/L2 原文落盘元信息（`_aggregate` 据此识别已落盘项）；
    compressed：L2 在线摘要元信息（未触发 L2 时为 None）。
    """

    content: str
    persisted: PersistedOutput | None = None
    compressed: CompressedOutput | None = None


class ConversationHistory:
    """历史视图（01 §8）。

    M2-a 先作为 `02` ConversationManager 的读包装（append 即写入即终态，
    满足 P1/P2）；L3 压缩需要时再叠加 estimate_tokens 等压缩侧接口。
    """

    def __init__(self, messages: list[Message] | None = None) -> None:
        self._messages: list[Message] = list(messages) if messages else []

    @property
    def messages(self) -> list[Message]:
        return list(self._messages)

    def append(self, message: Message) -> None:
        """写入即终态（01 P1）：后续轮次不再改动。"""
        self._messages.append(message)
