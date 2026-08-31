"""静默写：双门槛节流 + 主上下文前缀缓存复用（08 §3.4）。

**触发 = 廉价检查**（每次 `Agent.run()` 结束，非每个 ReAct iteration）：
```
if 距上次提取 < EXTRACT_MIN_INTERVAL（10 分钟）: return        # 时间节流
if 距上次提取的累计增量 < EXTRACT_MIN_DELTA（20K tokens）: return # 量级节流
执行提取（主上下文前缀缓存命中 + 追加提取指令，同主对话模型）
```

**增量是滑动窗口**：从上次静默写点累计到当前，跨多次 run 累积（01 estimate_tokens
差值）。大多数 run 在第一道门就返回、零 LLM 调用。

**提取输出** = 结构化操作集（JSON），本地执行 create/update/delete + 更新 MEMORY.md。
去重由 LLM 完成（拿到全部现有记忆清单）；「没有值得记忆的就什么都不做」。
失败只记日志、不抛——记忆是后台辅助，不能打断主流程。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import LLMClient
from kdagent.memory.model import normalize_ops
from kdagent.memory.ops import as_user_message, collect_ops_json
from kdagent.memory.store import MemoryStore

EXTRACT_MIN_INTERVAL = 10 * 60.0  # 秒：时间节流
EXTRACT_MIN_DELTA = 20_000  # tokens：量级节流（滑动窗口累计）

# 提取指令（08 §3.4）：追加为最后一条 user 消息；交给 LLM 判断，不写规则引擎。
_EXTRACT_INSTRUCTION = """下面是当前的记忆目录清单和最近对话的结尾部分。
分析对话，提取值得长期记忆的信息。

操作（输出 JSON，不要其他文字）：
{"ops": [{"action": "create|update|delete", "name": "文件名字节段",
"type": "user|feedback|project|reference", "description": "一行概述",
"content": "记忆正文（markdown）",
"index_line": "- [标题](文件名字节段.md) — 一行概述"}]}

规则：
- 分类：user（用户偏好/风格）/ feedback（纠正反馈）/ project（项目知识）/ reference（外部资料）
- 已有相同含义的记忆不要重复创建——读清单判断，信息有变化用 update
- 过时/被证伪的记忆用 delete
- 没有值得记忆的内容输出 {"ops": []}，不要硬造
"""

class MemoryExtractor:
    """双门槛节流 + 提取。`estimate` 注入 token 估算（默认 estimate_tokens 包装）。"""

    def __init__(
        self,
        store: MemoryStore,
        llm: LLMClient,
        *,
        estimate: Callable[[ConversationManager], int],
        min_interval: float = EXTRACT_MIN_INTERVAL,
        min_delta: int = EXTRACT_MIN_DELTA,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._estimate = estimate
        self._min_interval = min_interval
        self._min_delta = min_delta
        self._clock = clock or time.time
        self._last_run: float = 0.0  # 上次提取时间戳
        self._last_token_mark: int | None = None  # 上次提取点的 token 标记

    # ---- 触发 ----

    def should_extract(self, conversation: ConversationManager) -> bool:
        """双门槛判定（廉价，无 LLM 调用）。"""
        elapsed = self._clock() - self._last_run
        if self._last_run > 0 and elapsed < self._min_interval:
            return False
        if self._last_token_mark is None:
            # 首轮（从未提取过）：时间门槛豁免，但量级门槛保留——首轮 mark
            # 视为 0，会话累计增量 ≥ min_delta 才触发。会话刚建只有「你好」
            # 级别的增量不该惊动 LLM（D5 v052 review 修复首跑即提取）。
            return self._estimate(conversation) >= self._min_delta
        delta = self._estimate(conversation) - self._last_token_mark
        return delta >= self._min_delta

    async def maybe_extract(self, conversation: ConversationManager) -> None:
        """Agent.run() 结束后调用；节流不过直接返回（零成本）。"""
        if not self.should_extract(conversation):
            return
        await self._extract(conversation)

    # ---- 提取 ----

    async def _extract(self, conversation: ConversationManager) -> None:
        self._store.ensure()
        instruction = self._build_instruction(self._store.list_all())
        try:
            ops_json = await self._collect_json(conversation, instruction)
            ops = normalize_ops(ops_json)
            self._store.apply_ops(ops)
            self._last_run = self._clock()
            self._last_token_mark = self._estimate(conversation)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 记忆提取是后台辅助：失败只跳过本轮，不打断主流程。
            self._last_run = self._clock()  # 避免紧接重试
            return

    async def _collect_json(
        self, conversation: ConversationManager, instruction: str
    ) -> Any:
        """流式收集 LLM 输出 → 解析 JSON；失败返回空结构（安全）。"""
        # 提取指令追加为最后一条 user 消息（主上下文前缀缓存命中）。
        return await collect_ops_json(
            self._llm,
            system="你是 KDAgent 的记忆提取代理。只输出一个 JSON 对象。",
            messages=[*conversation.messages, as_user_message(instruction)],
        )

    def _build_instruction(self, files: list[Any]) -> str:
        listing = "\n".join(f"- {f.name}（{f.type}）：{f.description}" for f in files)
        listing = listing or "（记忆目录为空）"
        return _EXTRACT_INSTRUCTION + f"\n\n记忆目录清单：\n{listing}"
