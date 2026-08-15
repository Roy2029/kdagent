"""记忆系统（08）：静默读注入 + 双门槛静默写 + 索引。

- `store.MemoryStore`：文件生态唯一真相源 + MEMORY.md 索引维护
- `extractor.MemoryExtractor`：静默写（时间/量级双门槛 + 主上下文前缀缓存复用）
- `model`：四类记忆 + frontmatter 解析/序列化
"""

from __future__ import annotations

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
from kdagent.memory.store import ApplyReport, MemoryStore, build_memory_store

__all__ = [
    "ApplyReport",
    "EXTRACT_MIN_DELTA",
    "EXTRACT_MIN_INTERVAL",
    "INDEX_MAX_BYTES",
    "INDEX_MAX_LINES",
    "MemoryExtractor",
    "MemoryFile",
    "MemoryStore",
    "MemoryType",
    "build_memory_store",
    "parse_memory",
    "serialize_memory",
]
