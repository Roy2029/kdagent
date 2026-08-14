"""ReAct Loop（规格 02 §3.4-3.9）。

ReAct = 推理（text）→ 行动（tool_use）→ 观察（tool_result），Claude API 原生映射。
一个 while 循环持续调 LLM 并拼接上下文，直到模型不再请求工具。

M1-c 能跑档范围：
- 流式消费 → 聚合成完整响应（text + 多个 tool_use 一条 assistant 消息）
- 工具分批执行（is_concurrency_safe 划并发批 / 串行批）
- 四种停止条件：end_turn / MAX_ITERATIONS / 用户取消（CancelledError）/
  工具不存在→errorResult 不终止
- 断路器（§3.5 工具失败部分）：连续 3 次失败注入 system-reminder

后续接入：01 ContextManager（M2，payload 组装与压缩）、06 权限确认（M3）、12 TestingEvent（M5）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kdagent.config import Config
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.events import (
    AgentEventSink,
    CancelledEvent,
    ErrorEvent,
    LoopCompleteEvent,
    MaxIterationsReachedEvent,
    StreamTextEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnCompleteEvent,
    UsageEvent,
)
from kdagent.engine.llm.base import LLMClient, Payload, Usage
from kdagent.engine.messages import ContentBlock, TextBlock, ToolUseBlock
from kdagent.tools.base import AsyncConfirm, TodosCallback, ToolContext, ToolResult
from kdagent.tools.registry import ToolRegistry

AgentStatus = Literal["CONTINUE", "TERMINAL"]

MAX_ITERATIONS = 50  # T11：安全网，正常编码任务很少超过
CONCURRENCY_LIMIT = 5  # T11：并发批上限初值，待实测校准
CIRCUIT_BREAK_LIMIT = 3  # 连续失败挂起阈值（规格 02 §3.5）

# M1-c 用默认 system prompt；`01` assemble_system_prompt 组装管线 M2 接入
DEFAULT_SYSTEM_PROMPT = "你是 KDAgent，一个终端编码助手。自主完成任务，按需调用工具。"

_CIRCUIT_REMINDER = "[system-reminder] 已连续失败 3 次，需重新评估策略再继续"


@dataclass(slots=True)
class Batch:
    """一批可同时执行的工具调用。"""

    is_concurrency_safe: bool
    calls: list[ToolUseBlock]


def partition_tool_calls(tool_uses: list[ToolUseBlock], registry: ToolRegistry) -> list[Batch]:
    """按 is_concurrency_safe 划分并发批与串行批（规格 02 §3.7）。

    连续的安全工具同批并发，写操作隔离成独立串行批。
    例：[Read, Read, Edit, Read, Read] → [Read,Read]并发 | [Edit]串行 | [Read,Read]并发
    """
    batches: list[Batch] = []
    for tc in tool_uses:
        tool = registry.get(tc.name)
        safe = tool is not None and tool.is_concurrency_safe(tc.input)
        if safe and batches and batches[-1].is_concurrency_safe:
            batches[-1].calls.append(tc)
        else:
            batches.append(Batch(is_concurrency_safe=safe, calls=[tc]))
    return batches


class Agent:
    """ReAct Loop 主体。产出 AgentEvent 流（events sink），不感知 UI。"""

    def __init__(
        self,
        config: Config,
        llm: LLMClient,
        conversation: ConversationManager,
        tools: ToolRegistry,
        events: AgentEventSink,
        work_dir: Path | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        confirm: AsyncConfirm | None = None,
        todos: TodosCallback | None = None,
        on_conversation_change: Callable[[], None] | None = None,
    ) -> None:
        self._config = config
        self._llm = llm
        self._conversation = conversation
        self._tools = tools
        self._events = events
        self._work_dir = Path.cwd() if work_dir is None else work_dir
        self._system_prompt = system_prompt
        self._confirm = confirm  # 05 UI 确认钩子，None = 非交互环境直接执行
        self._todos = todos  # 03 TodoWrite 归一化回调 → 会话状态 + UI 渲染
        self._on_conversation_change = on_conversation_change  # 每次落一条消息后回调
        self._usage: Usage | None = None
        self._consecutive_failures = 0
        self._turn = 0
        self._pending_text: list[str] = []
        self._pending_tool_uses: list[ToolUseBlock] = []

    def set_system_prompt(self, text: str) -> None:
        """运行时切换 system prompt（05 /plan 模式切换用）。"""
        self._system_prompt = text

    @property
    def tool_count(self) -> int:
        """已注册工具数（05 /status 展示用）。"""
        return len(self._tools.all())

    @property
    def conversation(self) -> ConversationManager:
        """当前对话历史（05 命令 / 会话接线用）。"""
        return self._conversation

    def set_conversation(self, conversation: ConversationManager) -> None:
        """切换到另一个会话的对话历史（`04` /session new/resume 接线，M1-f）。

        保留 events/tools/confirm 等依赖，只换对话上下文。
        """
        self._conversation = conversation
        self._pending_text = []
        self._pending_tool_uses = []
        self._usage = None

    def _notify_conversation_change(self) -> None:
        """每次对话落一条消息后触发（UI 层由此把最新一条实时落盘，04 §3.2）。"""
        if self._on_conversation_change is not None:
            self._on_conversation_change()

    async def run(self, user_input: str) -> None:
        """跑完整循环直到终止；用户取消或超限也干净返回。"""
        self._conversation.add_user_message(user_input)
        self._notify_conversation_change()
        for turn in range(MAX_ITERATIONS):
            self._turn = turn
            try:
                status = await self._loop_iteration()
            except asyncio.CancelledError:
                # 停止条件 3：用户取消。已收部分落成一条消息，不碎不丢。
                self._flush_partial()
                self._events(CancelledEvent())
                return
            if status == "TERMINAL":
                return
        # 停止条件 2：迭代上限强制停止，提示用户。
        self._events(MaxIterationsReachedEvent(limit=MAX_ITERATIONS))

    async def _loop_iteration(self) -> AgentStatus:
        # 恢复四步②（04 §3.4）：链修复守在"发请求前"这一个出口——悬空 tool_use
        # 补错误结果、孤儿 tool_result 剔除，保证交替/配对合法。
        self._conversation.repair_chain()
        payload = self._assemble_payload()
        self._pending_text = []
        self._pending_tool_uses = []
        try:
            async for ev in self._llm.stream_chat(payload):
                if ev.type == "text_delta":
                    self._pending_text.append(ev.text or "")
                    self._events(StreamTextEvent(ev.text or ""))
                elif ev.type == "tool_use" and ev.tool_use is not None:
                    self._pending_tool_uses.append(ev.tool_use)
                    self._events(
                        ToolUseEvent(
                            id=ev.tool_use.id, name=ev.tool_use.name, input=ev.tool_use.input
                        )
                    )
                elif ev.type == "usage" and ev.usage is not None:
                    self._usage = ev.usage
                    self._events(UsageEvent(ev.usage))
                elif ev.type == "error" and ev.error is not None:
                    raise ev.error
        except Exception as exc:
            # 停止条件 4（provider 异常）：上报并终止，不无限重试（M1-c 简化）。
            self._flush_partial()
            self._events(ErrorEvent(error=str(exc)))
            return "TERMINAL"

        tool_uses = self._pending_tool_uses
        blocks = self._assemble_blocks(self._pending_text, tool_uses)
        self._pending_text = []
        self._pending_tool_uses = []
        if not blocks:
            return "TERMINAL"  # 空回复，防死循环
        self._conversation.add_assistant_message(blocks)
        self._notify_conversation_change()

        if tool_uses:
            batches = partition_tool_calls(tool_uses, self._tools)
            for batch in batches:
                results = await self._execute_batch(batch)
                self._conversation.add_tool_results(results)
                self._notify_conversation_change()
                self._update_circuit_breaker(results)
            self._events(TurnCompleteEvent(turn=self._turn))
            return "CONTINUE"
        # 停止条件 1：模型主动完成（无 tool_use）。
        self._events(LoopCompleteEvent(turns=self._turn + 1, usage=self._usage))
        return "TERMINAL"

    def _assemble_payload(self) -> Payload:
        max_tokens = self._config.extra.get("max_tokens")
        if not isinstance(max_tokens, int):
            max_tokens = 4096
        return Payload(
            system=self._system_prompt,
            messages=self._conversation.messages,
            tools=self._tools.schemas(),
            max_tokens=max_tokens,
        )

    def _assemble_blocks(
        self, text_parts: list[str], tool_uses: list[ToolUseBlock]
    ) -> list[ContentBlock]:
        """流式缓冲 → 完整 assistant 消息块（text 在前，tool_use 在后）。"""
        blocks: list[ContentBlock] = []
        text = "".join(text_parts)
        if text:
            blocks.append(TextBlock(text))
        blocks.extend(tool_uses)
        return blocks

    def _flush_partial(self) -> None:
        """中断时把已收部分落成一条 assistant 消息（不碎不丢，规格 02 §3.6）。"""
        blocks = self._assemble_blocks(self._pending_text, self._pending_tool_uses)
        if blocks:
            self._conversation.add_assistant_message(blocks)
            self._notify_conversation_change()
        self._pending_text = []
        self._pending_tool_uses = []

    async def _execute_batch(self, batch: Batch) -> list[ToolResult]:
        """并发批 gather（带 CONCURRENCY_LIMIT 信号量）；串行批逐个执行。"""
        if batch.is_concurrency_safe:
            sem = asyncio.Semaphore(CONCURRENCY_LIMIT)

            async def run_one(tc: ToolUseBlock) -> ToolResult:
                async with sem:
                    return await self._exec_one(tc)

            return list(await asyncio.gather(*[run_one(tc) for tc in batch.calls]))
        results: list[ToolResult] = []
        for tc in batch.calls:
            results.append(await self._exec_one(tc))
        return results

    async def _exec_one(self, tool_use: ToolUseBlock) -> ToolResult:
        """单个工具执行：统一捕获异常 → errorResult，不让单个失败打断整轮（§3.7）。

        停止条件 4：工具不存在 → errorResult 进历史（不终止循环），模型下轮自行调整。
        """
        tool = self._tools.get(tool_use.name)
        if tool is None:
            return self._error_result(
                tool_use, f"工具不存在：{tool_use.name}（可用工具：{self._tools.all()} 的名字）"
            )
        errors = tool.validate_input(tool_use.input)
        if errors:
            return self._error_result(tool_use, "参数校验失败：\n" + "\n".join(errors))
        ctx = ToolContext(
            work_dir=self._work_dir,
            config=self._config,
            tool_use_id=tool_use.id,
            confirm=self._confirm,
            todos=self._todos,
        )
        # 规格 05 §3.4：Y/N 前置——选 no 返回 is_error=True 结果，模型下轮自行调整。
        if (
            tool.require_confirm
            and ctx.confirm is not None
            and not await ctx.confirm(tool_use.name, tool_use.input)
        ):
            result = self._error_result(tool_use, "执行已被用户拒绝")
            self._events(
                ToolResultEvent(
                    name=result.name,
                    content=result.content,
                    is_error=True,
                    duration_ms=0,
                )
            )
            return result
        try:
            result = await tool.execute(ctx, tool_use.input)
        except Exception as exc:
            result = self._error_result(tool_use, f"执行异常：{exc}")
        self._events(
            ToolResultEvent(
                name=result.name,
                content=result.content,
                is_error=result.is_error,
                duration_ms=result.duration_ms,
            )
        )
        return result

    def _error_result(self, tool_use: ToolUseBlock, message: str) -> ToolResult:
        return ToolResult(
            tool_use_id=tool_use.id, name=tool_use.name, content=message, is_error=True
        )

    def _update_circuit_breaker(self, results: list[ToolResult]) -> None:
        """连续失败计数；达阈值注入 system-reminder 后复位（规格 02 §3.5）。

        TestingEvent（12）触发部分留 M5；此处只覆盖工具执行失败。
        """
        if results and all(not r.is_error for r in results):
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += sum(1 for r in results if r.is_error)
        if self._consecutive_failures >= CIRCUIT_BREAK_LIMIT:
            self._consecutive_failures = 0
            self._conversation.add_user_message(
                "", extra_blocks=[TextBlock(_CIRCUIT_REMINDER)]
            )
            self._notify_conversation_change()
