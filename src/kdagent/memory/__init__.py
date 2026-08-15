"""记忆系统（08）：静默读注入 + 双门槛静默写 + 索引 + Dreaming 治理。

- `store.MemoryStore`：文件生态唯一真相源 + MEMORY.md 索引维护
- `extractor.MemoryExtractor`：静默写（时间/量级双门槛 + 主上下文前缀缓存复用）
- `consolidator.MemoryConsolidator`：Dreaming 治理（门控/锁/LLM 四阶段整理）
- `prompt.MEMORY_USAGE_INSTRUCTION`：主动线提示词（§3.5）
- `model`：四类记忆 + frontmatter 解析/序列化
"""

from __future__ import annotations

from kdagent.memory.consolidator import (
    CONSOLIDATE_MIN_INTERVAL,
    CONSOLIDATE_MIN_SESSIONS,
    MemoryConsolidator,
)
from kdagent.memory.extractor import (
    EXTRACT_MIN_DELTA,
    EXTRACT_MIN_INTERVAL,
    MemoryExtractor,
)
from kdagent.memory.model import (
    INDEX_MAX_BYTES,
    INDEX_MAX_LINES,
    MemoryFile,
    MemoryType,
    parse_memory,
    serialize_memory,
)
from kdagent.memory.prompt import MEMORY_USAGE_INSTRUCTION
from kdagent.memory.store import ApplyReport, MemoryStore, build_memory_store

__all__ = [
    "ApplyReport",
    "CONSOLIDATE_MIN_INTERVAL",
    "CONSOLIDATE_MIN_SESSIONS",
    "EXTRACT_MIN_DELTA",
    "EXTRACT_MIN_INTERVAL",
    "INDEX_MAX_BYTES",
    "INDEX_MAX_LINES",
    "MEMORY_USAGE_INSTRUCTION",
    "MemoryConsolidator",
    "MemoryExtractor",
    "MemoryFile",
    "MemoryStore",
    "MemoryType",
    "build_memory_store",
    "parse_memory",
    "serialize_memory",
]
