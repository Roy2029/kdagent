"""上下文总管家（规格 01 §8）。

组装、演进、压缩的统一入口。M2 范围：
- `on_tool_results`：工具结果入口分发（§5.5 决策流程）——L1 落盘 → L2 在线摘要 → 原样。
- L3 Auto-Compact（§5.4/§6/§6.1）：`check_before_call` 每轮 API 前判定（AUTO/FORCE），
  `auto_compact`/`force_compact`/`emergency_compact`/`manual_compact` 执行压缩并维护
  **独立预算**（auto/force 各 3 次；auto 熔断只关自动路径，force 耗尽抛 ContextFullError）。

落盘根目录：`{sessions_dir}/{sid}/tool-results/`（01 §5.2）；L3 压缩后整体重写会话
JSONL（04 §3.5：压缩后内存与文件一致）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from kdagent.context.compactor import (
    COMPACT_FAILURE_LIMIT,
    WINDOW_SIZE,
    Compactor,
    CompactResult,
    ContextFullError,
    CostParams,
    L2Compressor,
)
from kdagent.context.history import ProcessedToolResult
from kdagent.context.tool_result_handler import ToolResultHandler
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import LLMClient, Payload
from kdagent.obs.telemetry import Telemetry
from kdagent.sessions.records import SessionRecord, TodoItemRecord
from kdagent.tools.base import ToolResult

CheckResult = Literal["NORMAL", "AUTO_COMPACT", "FORCE_COMPACT"]


class ContextManager:
    """上下文入口：工具结果分发 → L1/L2/原样；L3 压缩与兜底预算（M2-c）。"""

    def __init__(
        self,
        sessions_dir: Path,
        session_id: str = "",
        *,
        handler: ToolResultHandler | None = None,
        llm: LLMClient | None = None,
        system_prompt: str = "",
        compactor: Compactor | None = None,
        todos_provider: Callable[[], list[TodoItemRecord] | None] | None = None,
        window_size: int = WINDOW_SIZE,
        telemetry: Telemetry | None = None,
        cost: CostParams | None = None,
    ) -> None:
        self._sessions_dir = sessions_dir
        self._session_id = session_id
        self._llm = llm
        self._system_prompt = system_prompt
        self._todos_provider = todos_provider  # 04 Session.todos（12 时点② L3 重灌快照）
        self._window_size = window_size
        self._telemetry = telemetry  # 07 T8 标定：透传给 L2 压缩器产 span
        self._cost = cost  # 01 T5-1 计价（D83：config cost 段按 provider 注入，None 用默认）
        # persist_dir 随会话切换而变（`04` /session new/resume），延迟到首次 use 时装配
        self._handler: ToolResultHandler | None = None
        self._handler_factory = handler  # 测试注入自定义 handler（含自定义阈值）
        self._compactor: Compactor | None = compactor or self._build_compactor()
        # 01 §6：auto/force 独立失败预算 + auto 熔断（D8）
        self._auto_fail = 0
        self._force_fail = 0
        self._circuit_open = False

    def _build_compactor(self) -> Compactor | None:
        """无注入 compactor 时按主模型装配（复用主对话 provider，前缀缓存命中）。"""
        if self._llm is None:
            return None
        return Compactor(
            self._llm, system_prompt=self._system_prompt, window_size=self._window_size
        )

    @property
    def sessions_dir(self) -> Path:
        return self._sessions_dir

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def auto_fail(self) -> int:
        """auto 连续失败计数（测试/观测）。"""
        return self._auto_fail

    @property
    def force_fail(self) -> int:
        """force 连续失败计数（测试/观测）。"""
        return self._force_fail

    @property
    def circuit_open(self) -> bool:
        """auto 熔断状态（只关自动路径，01 §6）。"""
        return self._circuit_open

    def set_session_id(self, sid: str) -> None:
        """`04` 切换会话时更新：落盘目录随 sid 变。"""
        self._session_id = sid
        self._handler = None  # 惰性重建（persist_dir 已变）

    def set_telemetry(self, telemetry: Telemetry | None) -> None:
        """运行时接线 telemetry（App 装配后调用）：惰性重建 L2 压缩器承接 span。

        ContextManager 在 cli.py 装配、Telemetry 在 App 内部构建（时序后于 CM），
        故用 setter 后补而非构造参数硬绑（07 T8）。
        """
        self._telemetry = telemetry
        self._handler = None  # 惰性重建（L2Compressor 需携带 telemetry）

    def _tool_result_handler(self) -> ToolResultHandler:
        if self._handler is None:
            if self._handler_factory is not None:
                self._handler = self._handler_factory
            else:
                persist_dir = self._sessions_dir / self._session_id / "tool-results"
                l2 = None
                if self._llm is not None:
                    l2 = L2Compressor(
                        self._llm,
                        persist_dir=persist_dir,
                        system_prompt=self._system_prompt,
                        telemetry=self._telemetry,  # 07 T8：L2 压缩成本 span
                        cost=self._cost,  # T5-1：配置计价表按 provider 注入（None = 默认）
                    )
                self._handler = ToolResultHandler(persist_dir, l2=l2)
        return self._handler

    async def on_tool_result(
        self, result: ToolResult, p_tokens: int = 0, prefix: Payload | None = None
    ) -> ProcessedToolResult:
        """单条入口处理（01 §8 on_tool_result）：L1 落盘 → L2 摘要 → 原样。"""
        return await self._tool_result_handler().handle_single(result, p_tokens, prefix)

    async def on_tool_results(
        self, results: list[ToolResult], p_tokens: int = 0, prefix: Payload | None = None
    ) -> list[ProcessedToolResult]:
        """批量入口处理（一轮并行结果，聚合 L1 判定）。"""
        return await self._tool_result_handler().handle_batch(results, p_tokens, prefix)

    # ---- L3 Auto-Compact 与兜底（01 §6/§6.1） --------------------------------

    def check_before_call(self, tokens: int) -> CheckResult:
        """01 §6.1 阶段 A：FORCE 无视熔断；AUTO 需未熔断。无 LLM（无法压缩）→ NORMAL。"""
        c = self._compactor
        if c is None:
            return "NORMAL"
        if tokens >= c.force_compact_line:
            return "FORCE_COMPACT"
        if tokens >= c.auto_compact_trigger and not self._circuit_open:
            return "AUTO_COMPACT"
        return "NORMAL"

    async def auto_compact(
        self, conversation: ConversationManager, *, prefix: Payload | None = None
    ) -> CompactResult | None:
        """常规预防（§6.1 auto_compact）：auto 独立预算，连续失败 3 次熔断自动路径。"""
        try:
            result = await self._compact(conversation, prefix)
            self._reset_budgets()
            return result
        except Exception:
            self._auto_fail += 1
            if self._auto_fail >= COMPACT_FAILURE_LIMIT:
                self._circuit_open = True
            return None

    async def force_compact(
        self, conversation: ConversationManager, *, prefix: Payload | None = None
    ) -> CompactResult:
        """保底（§6.1 force_compact）：force 独立预算，耗尽抛 ContextFullError。"""
        c = self._compactor
        if c is None:
            raise ContextFullError("未配置 LLM，无法压缩上下文")
        if self._force_fail >= COMPACT_FAILURE_LIMIT:
            raise ContextFullError(
                "强制压缩连续失败，上下文仍超限——请 /compact 手动处理或清理会话"
            )
        try:
            result = await self._compact(conversation, prefix)
            self._reset_budgets()
            return result
        except ContextFullError:
            raise
        except Exception:
            self._force_fail += 1
            raise

    async def emergency_compact(
        self, conversation: ConversationManager, *, prefix: Payload | None = None
    ) -> CompactResult:
        """prompt_too_long 撞墙后自救（§6 ③）：走 force 预算，压完重试原请求。"""
        return await self.force_compact(conversation, prefix=prefix)

    async def manual_compact(
        self,
        conversation: ConversationManager,
        *,
        prefix: Payload | None = None,
        focus: str = "",
    ) -> CompactResult:
        """手动 /compact（01 §7 + 05 §3.6）：与自动共用同一套 L3 逻辑，仅触发方式不同。

        `focus`：/compact 带参指定保留重点，透传给 Compactor 注入摘要指令。
        走 force 预算（与强制压缩同保底口径）。
        """
        c = self._compactor
        if c is None:
            raise ContextFullError("未配置 LLM，无法压缩上下文")
        try:
            result = await self._compact(conversation, prefix, focus)
            self._reset_budgets()
            return result
        except ContextFullError:
            raise
        except Exception:
            self._force_fail += 1
            raise

    async def _compact(
        self, conversation: ConversationManager, prefix: Payload | None, focus: str = ""
    ) -> CompactResult:
        """执行 L3 压缩 → 替换历史 → 整体重写会话文件（04 §3.5：内存与文件一致）。"""
        c = self._compactor
        if c is None:
            raise ContextFullError("未配置 LLM，无法压缩上下文")
        todos = self._todos_provider() if self._todos_provider is not None else None
        session_path = str(self._sessions_dir / f"{self._session_id}.jsonl")
        result = await c.compact(
            conversation, session_path=session_path, todos=todos, prefix=prefix, focus=focus
        )
        conversation.restore([result.summary_message, *result.kept_recent])
        self._persist_history(conversation)
        return result

    def _persist_history(self, conversation: ConversationManager) -> None:
        """压缩后整体重写会话 JSONL（摘要消息落盘；todos 随下次 flush 附回）。

        目录懒创建：真实路径由 `04` SessionManager.create 建好，这里兜底（测试/首次压缩）。
        """
        if not self._session_id:
            return
        file = self._sessions_dir / f"{self._session_id}.jsonl"
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open("w", encoding="utf-8") as f:
            for msg in conversation.messages:
                f.write(SessionRecord.from_message(msg, int(time.time())).to_json() + "\n")

    def _reset_budgets(self) -> None:
        """任何一次压缩成功 → 两条计数清零、熔断复位（01 §6.1）。"""
        self._auto_fail = 0
        self._force_fail = 0
        self._circuit_open = False
