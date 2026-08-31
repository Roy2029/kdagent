"""ReAct Loop（规格 02 §3.4-3.9）。

ReAct = 推理（text）→ 行动（tool_use）→ 观察（tool_result），Claude API 原生映射。
一个 while 循环持续调 LLM 并拼接上下文，直到模型不再请求工具。

M1-c 能跑档范围：
- 流式消费 → 聚合成完整响应（text + 多个 tool_use 一条 assistant 消息）
- 工具分批执行（is_concurrency_safe 划并发批 / 串行批）
- 四种停止条件：end_turn / MAX_ITERATIONS / 用户取消（CancelledError）/
  工具不存在→errorResult 不终止
- 断路器（§3.5 工具失败部分）：连续 3 次失败注入 system-reminder

后续接入：01 ContextManager（M2，payload 组装与压缩）、06 权限确认（M3）、
12 TestingEvent（M5 遗留补齐）。
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext, suppress
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from kdagent.config import Config
from kdagent.context.compactor import ContextFullError, estimate_messages_tokens, estimate_tokens
from kdagent.context.context_manager import ContextManager
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.events import (
    AgentEventSink,
    CancelledEvent,
    ErrorEvent,
    LoopCompleteEvent,
    MaxIterationsReachedEvent,
    PermissionRequestEvent,
    PermissionVerdict,
    StreamTextEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnCompleteEvent,
    UsageEvent,
)
from kdagent.engine.llm.base import (
    LLMClient,
    Payload,
    PromptTooLongError,
    ToolTruncatedError,
    Usage,
    is_transient_llm_error,
)
from kdagent.engine.messages import ContentBlock, TextBlock, ToolUseBlock
from kdagent.harness.checkpoints import (
    LARGE_CHANGE_THRESHOLD,
    REINJECT_COOLDOWN,
    REPLAN_TRIGGER_COUNT,
    STALE_TODO_THRESHOLD,
    CheckpointEvent,
    VerificationKind,
    build_checkpoint_reminder,
    build_large_change_warning,
    build_mismatch_reminder,
    build_replan_reminder,
    build_stale_todo_reminder,
    classify_criteria,
    file_target,
    has_test_evidence,
    todo_progress,
)
from kdagent.harness.error_patterns import diagnose_failure, pattern_memory
from kdagent.hooks.engine import HookEngine
from kdagent.hooks.engine_types import HookContext
from kdagent.memory.consolidator import MemoryConsolidator
from kdagent.memory.extractor import MemoryExtractor
from kdagent.memory.prompt import MEMORY_USAGE_INSTRUCTION
from kdagent.memory.store import MemoryStore
from kdagent.obs.log import incremental_payload_text, payload_text
from kdagent.obs.model import Span
from kdagent.obs.telemetry import Telemetry
from kdagent.permission.checker import PermissionChecker
from kdagent.skill.manager import SkillManager, build_skills_reminder
from kdagent.tools.base import AsyncConfirm, TodosCallback, ToolContext, ToolResult
from kdagent.tools.registry import ToolRegistry

AgentStatus = Literal["CONTINUE", "TERMINAL"]

MAX_ITERATIONS = 50  # T11：安全网，正常编码任务很少超过
CONCURRENCY_LIMIT = 5  # T11：并发批上限初值，待实测校准
CIRCUIT_BREAK_LIMIT = 3  # 连续失败挂起阈值（规格 02 §3.5）
# 瞬态 LLM 错误重试（02 §3.9，review 修复 2026-08-31）：429/5xx/连接/超时指数
# 退避 ≤3 次（1s→2s→4s），重试不推进轮次计数；一次网络抖动不再废掉整个 50 轮 run。
TRANSIENT_RETRY_MAX = 3
TRANSIENT_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)


async def _retry_sleep(seconds: float) -> None:
    """重试退避休眠（模块级便于测试 monkeypatch）。"""
    await asyncio.sleep(seconds)
# 07 tool.exec span 工具返回截断上限（11 §3.4 复核界面「阅读」返回；防大输出撑爆 span）
_TRACE_OUTPUT_CAP = 1000

# M1-c 用默认 system prompt；`01` assemble_system_prompt 组装管线 M2 接入
# 12 §3.4 常驻核心铁律（便宜，每轮在）：测试自测与修复规矩，防「看起来干完」。
DEFAULT_SYSTEM_PROMPT = (
    "你是 KDAgent，一个终端编码助手。自主完成任务，按需调用工具。"
    "修改代码后应运行测试自测（TestRunner 工具）；测试失败必须基于失败信息"
    "修复后重跑，不得绕开测试或伪造通过。"
    "你的名字与用户对你的称呼以记忆中的用户偏好为准（可能不叫 KDAgent）；"
    "被问及身份/名字时先查记忆，不要自称 Claude 或其他通用身份。"
)

_CIRCUIT_REMINDER = "[system-reminder] 已连续失败 3 次，需重新评估策略再继续"

# B：输出截断反馈（写文件场景实测死循环：模型把整个 HTML 塞进 WriteFile 参数被
# max_tokens 截断，旧实现静默解析失败成空参数 → 误导性「参数校验失败」→ 反复重试
# 同样超长输出）。显式告知截断 + 引导分段写，打破死循环。
_TRUNCATED_FEEDBACK = (
    "[system] 你的上一条回复的工具参数不完整（JSON 解析失败，通常因单次输出达到 "
    "max_tokens 上限被截断），已丢弃。\n"
    "解决办法：把长内容拆小输出。写大文件（完整 HTML/长代码）时先用 WriteFile "
    "写入开头一段，再用 Edit 逐段追加其余内容，不要一次性把整个文件塞进 "
    "WriteFile 参数。重新尝试。\n"
    "（原因：{reason}）"
)

# B2：空回复截断反馈（2026-08-28 21da 会话实测：模型对「全部都做」先输出大量
# reasoning 思考吃满 max_tokens，content 为空 → parser 零事件 → 旧逻辑静默
# TERMINAL「没报错但也没反应了」）。引导模型别过度思考、直接输出，与拆小输出
# （_TRUNCATED_FEEDBACK）是不同场景的两种引导。
_EMPTY_TRUNCATED_FEEDBACK = (
    "[system] 你上一条回复未产生任何可见内容（无文本、无工具调用），且输出达到 "
    "max_tokens 上限被截断——常见原因是思考过程过长、占满输出预算。\n"
    "解决办法：直接给出简洁文本或直接调用工具，不要输出冗长的推理过程。重新尝试。\n"
    "（原因：{reason}）"
)

# B5 修复（2026-08-29）：prompt 型 hook 注入提示词的 user 消息标记。HookEngine 的
# `prompt_inject` 回调（engine.py 预留、此前从未接线）由 Agent 构造时接上——prompt
# 内容作为 user 消息进 conversation，随本轮 payload 发送（turn_start 在 assemble
# 之前触发，本论生效）。
HOOK_PROMPT_MARKER = "[system-reminder] Hook 注入提示词"


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
        session_id: str = "",
        model_name: str = "",
        telemetry: Telemetry | None = None,
        # 10 §5 342（D78）：子 Agent 挂父 trace——begin_trace 时关联调用方 id。
        # 主 Agent 构造不传（默认空=根 trace）；SubAgentRunner 委派点读父上下文传入。
        parent_trace_id: str = "",
        parent_span_id: str = "",
        context_manager: ContextManager | None = None,
        permission_checker: PermissionChecker | None = None,
        hooks: HookEngine | None = None,
        memory_store: MemoryStore | None = None,
        memory_extractor: MemoryExtractor | None = None,
        memory_consolidator: MemoryConsolidator | None = None,
        skills: SkillManager | None = None,
        # 10 M5-a SubAgent：实例级迭代上限（子 Agent 用定义文件 maxTurns；None =
        # 运行时读模块级 MAX_ITERATIONS=50，保证测试 monkeypatch 生效）。
        max_iterations: int | None = None,
        # B5：prompt 型 hook 注入接线开关。主 Agent 接线（注入自己的 conversation）；
        # 子 Agent（SubAgentRunner）共享主 HookEngine，传 False 不覆盖主注入目标。
        wire_hook_prompt: bool = True,
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
        self._session_id = session_id  # 07 trace 关联（04 sid，/session 切换时更新）
        self._model_name = model_name or config.model  # llm.call span 记录 model（D9）
        self._telemetry = telemetry  # 07 埋点 sink，None = 无 obs 环境
        self._parent_trace_id = parent_trace_id  # 10 §5 342：子 Agent trace 挂父
        self._parent_span_id = parent_span_id
        self._context_manager = context_manager  # 01 工具结果入口分发（M2-a L1 落盘）
        # 06 M3 可控档：五层裁决器 + Hook 引擎。checker 存在时接管 require_confirm
        # （升级为完整裁决系统，规格 06 §1）；hooks None = 无自动化（M1/M2 行为不变）。
        self._permission_checker = permission_checker
        self._hooks = hooks
        # 08 M4 好用档：静默读注入（memory_store） + 静默写提取（memory_extractor）
        # + Dreaming 治理（memory_consolidator，门控通过则后台整理）。
        self._memory_store = memory_store
        self._memory_extractor = memory_extractor
        self._memory_consolidator = memory_consolidator
        # 09 M4-d Skill 两阶段加载：system-reminder 注入「可用 Skill」清单（轻量），
        # 完整 SOP 经 LoadSkill 工具按需加载（agent 只持清单，正文在 manager）。
        self._skills = skills
        # 10 M5-a SubAgent：子 Agent 迭代上限（RunToCompletion 防失控，10 §3.5）。
        self._max_iterations = max_iterations if max_iterations is not None else MAX_ITERATIONS
        self._stop_reason = "completed"
        self._usage: Usage | None = None
        self._consecutive_failures = 0
        self._turn = 0
        # D90 增量 prompt 日志：上一轮已记录的消息数（llm.call span 只记新增段）。
        self._payload_msg_offset = 0
        self._pending_text: list[str] = []
        self._pending_tool_uses: list[ToolUseBlock] = []
        # 12 §3.3 双层检查点（M5 遗留第二块）：todo 前后快照 + 行为观察冷却计数。
        self._last_todos: list[dict[str, Any]] | None = None
        self._turns_since_todo_update = 0
        self._stale_inject_turn = -REINJECT_COOLDOWN - 1
        self._large_inject_turn = -REINJECT_COOLDOWN - 1
        self._replan_inject_turn = -REINJECT_COOLDOWN - 1
        self._round_write_paths: list[str] = []  # 本轮 write/edit 目标（跨批累计）
        # 12 §3.3 Replan（D57）：断路器触发累计次数；达阈值=路径反复受阻不可行。
        self._circuit_breaker_triggers = 0
        # 08 §3.3 错误模式沉淀（T33-3）：本会话已沉淀的根因 name（防同轮重复写）。
        self._seen_patterns: set[str] = set()
        # B5 接线：prompt 型 hook 注入本 Agent conversation（主 Agent 开启、子 Agent
        # 关）。hooks None / 关闭 → 跳过；构造时已自定义 prompt_inject（测试/用户
        # 传入）→ 不覆盖，保留外部回调优先。engine.py `prompt_inject` 靠此 setter
        # 在 Agent 装配后接上注入目标。
        if (
            self._hooks is not None
            and wire_hook_prompt
            and self._hooks.prompt_inject is None
        ):
            self._hooks.set_prompt_inject(self._inject_hook_prompt)

    def set_session_id(self, sid: str) -> None:
        """切换会话时更新 trace 关联的 session_id + 上下文落盘目录（04 /session new/resume）。"""
        self._session_id = sid
        if self._context_manager is not None:
            self._context_manager.set_session_id(sid)

    def set_system_prompt(self, text: str) -> None:
        """运行时切换 system prompt（05 /plan 模式切换用）。"""
        self._system_prompt = text

    def set_config(self, config: Config) -> None:
        """/session new 重载配置：换 Config 引用（改完 config.yaml 不必重启进程）。

        Agent 读 `self._config` 是惰性的——`extra.max_tokens` 在 `_assemble_payload`
        组装 payload 时才读（2026-08-28 953e 实测：改配置后 `/session new` 不重载，
        进程一直顶格 4096，写大文件 WriteFile 参数被截断致任务失败）。本方法只换
        引用不读盘，读盘在 UI 层 `reload_config`。
        """
        self._config = config

    @property
    def system_prompt(self) -> str:
        """当前 system prompt（05 状态栏/上下文窗口估算用）。"""
        return self._system_prompt

    @property
    def tool_count(self) -> int:
        """已注册工具数（05 /status 展示用）。"""
        return len(self._tools.all())

    @property
    def tool_names(self) -> list[str]:
        """已注册工具名（09 /mcp 查看工具列表用）。"""
        return sorted(t.name for t in self._tools.all())

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

    def _inject_hook_prompt(self, text: str) -> None:
        """prompt 型 hook 注入回调（B5）：提示词作为 user 消息进 conversation。

        HookEngine `_dispatch` 同步调用（prompt 分支）。带 HOOK_PROMPT_MARKER 前缀，
        与既有 system-reminder 注入（TODO 快照/时间跨度）同通道；notify 让 UI 落盘。
        空文本跳过（hook 配置 prompt 为空串时 `_inject_prompt` 仍会调用）。hook 是
        辅助机制，注入失败按「尾巴摇狗」不抛（错误兜底见 06 §3.10）。
        """
        if not text:
            return
        try:
            self._conversation.add_user_message(
                "", extra_blocks=[TextBlock(f"{HOOK_PROMPT_MARKER}\n{text}")]
            )
            self._notify_conversation_change()
        except Exception:
            return

    async def run(self, user_input: str) -> None:
        """跑完整循环直到终止；用户取消或超限也干净返回。"""
        self._conversation.add_user_message(user_input)
        self._notify_conversation_change()
        self._stop_reason = "completed"
        self._run_hook("session_start", HookContext(event="session_start", message=user_input))
        telemetry = self._telemetry
        # 07：一次 Agent.run() = 一条 Trace（根 span=trace.run，记停止原因）。
        root_cm: AbstractContextManager[Span | None] = nullcontext(None)
        if telemetry is not None:
            telemetry.begin_trace(
                self._session_id,
                user_input[:200],
                parent_trace_id=self._parent_trace_id,
                parent_span_id=self._parent_span_id,
            )
            root_cm = telemetry.span(
                "trace.run", "session", {"user_input_snapshot": user_input[:200]}
            )
        with root_cm as root_span:
            try:
                await self._run_loop()
            finally:
                if root_span is not None:
                    root_span.attributes["stop_reason"] = self._stop_reason
                if telemetry is not None:
                    telemetry.end_trace()
                self._run_hook("session_end", HookContext(event="session_end"))
        # 08 §3.4 静默写：每轮 run 结束提取（双门槛节流，多数直接返回零 LLM 调用）。
        # 辅助机制故障不打断主流程（与 hook 同一哲学：尾巴摇狗反向禁止）。
        if self._memory_extractor is not None:
            try:
                await self._memory_extractor.maybe_extract(self._conversation)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        # 08 §3.6 Dreaming 治理：门控通过则后台调度整理（懒检查，同步返回零阻塞）。
        if self._memory_consolidator is not None:
            with suppress(Exception):
                self._memory_consolidator.maybe_consolidate()

    async def _run_loop(self) -> None:
        """ReAct 主循环（从 run 提取，供 trace.run span 包住）。"""
        for turn in range(self._max_iterations):
            self._turn = turn
            try:
                status = await self._loop_iteration()
            except asyncio.CancelledError:
                # 停止条件 3：用户取消。已收部分落成一条消息，不碎不丢。
                self._stop_reason = "cancelled"
                self._flush_partial()
                self._events(CancelledEvent())
                return
            if status == "TERMINAL":
                return
        # 停止条件 2：迭代上限强制停止，提示用户。
        self._stop_reason = "max-iterations"
        self._events(MaxIterationsReachedEvent(limit=self._max_iterations))

    @property
    def turns(self) -> int:
        """已执行轮数（10 M5-a SubAgentRunner 结果统计用）。"""
        return self._turn + 1

    async def _loop_iteration(self) -> AgentStatus:
        """一轮 LLM 调用 + 工具执行。包 turn_start/turn_end 生命周期 hook（06 §3.10）。"""
        self._run_hook("turn_start", HookContext(event="turn_start"))
        try:
            return await self._loop_iteration_inner()
        finally:
            self._run_hook("turn_end", HookContext(event="turn_end"))

    async def _loop_iteration_inner(self) -> AgentStatus:
        # 恢复四步②（04 §3.4）：链修复守在"发请求前"这一个出口——悬空 tool_use
        # 补错误结果、孤儿 tool_result 剔除，保证交替/配对合法。
        self._conversation.repair_chain()
        # 阶段 A（01 §6.1）：每轮 API 前预防性压缩。FORCE 超限先压（失败即终止），
        # AUTO 尽力而为（失败只熔断自动路径，不终止）。压缩成功已由 ContextManager
        # 整体重写会话文件（04 §3.5），不触发 _notify_conversation_change。
        if (
            self._context_manager is not None
            and await self._precompact_if_needed() == "TERMINAL"
        ):
            return "TERMINAL"
        payload = self._assemble_payload()
        self._pending_text = []
        self._pending_tool_uses = []
        # 阶段 B（01 §6 ③）：prompt_too_long 撞墙 → 紧急压缩 → 重建 payload 重试一次。
        retry = 0  # PromptTooLong 重试计数（输入侧，紧急压缩后限重试一次）
        truncate_retry = 0  # 输出截断重试计数（B：反馈模型拆小输出，限 2 次防死循环）
        transient_retry = 0  # 瞬态错误重试计数（02 §3.9：429/5xx/连接/超时，≤3 次）
        while True:
            err = await self._stream_llm(payload)
            if err is None:
                break
            if isinstance(err, ToolTruncatedError):
                # B：截断已反馈模型（_stream_llm 内写入 conversation）。重建 payload
                # 让反馈进入下一轮；连续截断 2 次视为模型无法收敛 → 终止防死循环。
                truncate_retry += 1
                if truncate_retry >= 2:
                    self._stop_reason = "error"
                    self._flush_partial()
                    self._events(ErrorEvent(error=str(err)))
                    self._run_hook("error", HookContext(event="error", error=str(err)))
                    return "TERMINAL"
                payload = self._assemble_payload()
                continue
            # 瞬态重试（02 §3.9）：重试不推进轮次；超上限落到下方 TERMINAL 分支。
            if is_transient_llm_error(err) and transient_retry < TRANSIENT_RETRY_MAX:
                transient_retry += 1
                delay = TRANSIENT_RETRY_DELAYS[transient_retry - 1]
                self._pending_text = []
                self._pending_tool_uses = []
                retry_note = (
                    f"LLM 瞬态错误（{err}），"
                    f"正在重试 {transient_retry}/{TRANSIENT_RETRY_MAX}（{delay:.0f}s 后）"
                )
                self._events(ErrorEvent(error=retry_note))
                await _retry_sleep(delay)
                continue
            if (
                not isinstance(err, PromptTooLongError)
                or retry >= 1
                or self._context_manager is None
            ):
                # 停止条件 4（provider 异常）：上报并终止，不无限重试（M1-c 简化）。
                self._stop_reason = "error"
                self._flush_partial()
                self._events(ErrorEvent(error=str(err)))
                self._run_hook("error", HookContext(event="error", error=str(err)))
                return "TERMINAL"
            # 清半截缓冲，走 force 预算压一次；失败（含预算耗尽）须终止。
            self._pending_text = []
            self._pending_tool_uses = []
            if await self._emergency_compact(payload) == "TERMINAL":
                return "TERMINAL"
            payload = self._assemble_payload()
            retry += 1

        tool_uses = self._pending_tool_uses
        blocks = self._assemble_blocks(self._pending_text, tool_uses)
        self._pending_text = []
        self._pending_tool_uses = []
        if not blocks:
            return "TERMINAL"  # 空回复，防死循环
        self._conversation.add_assistant_message(blocks)
        self._notify_conversation_change()

        if tool_uses:
            self._round_write_paths = []
            batches = partition_tool_calls(tool_uses, self._tools)
            for batch in batches:
                results = await self._execute_batch(batch)
                results = await self._process_results(results)
                self._conversation.add_tool_results(results)
                self._notify_conversation_change()
                self._update_circuit_breaker(results)
                self._observe_todos(batch, results)
                self._observe_error_patterns(batch, results)
            self._checkpoint_round_end()
            self._events(TurnCompleteEvent(turn=self._turn))
            return "CONTINUE"
        # 停止条件 1：模型主动完成（无 tool_use）。
        self._events(LoopCompleteEvent(turns=self._turn + 1, usage=self._usage))
        return "TERMINAL"

    async def _precompact_if_needed(self) -> AgentStatus:
        """阶段 A（01 §6.1）：每轮 API 前预防性压缩判定。

        FORCE_COMPACT 走 force 预算，失败（含预算耗尽 → ContextFullError）必须终止；
        AUTO_COMPACT 尽力而为，失败只熔断自动路径，不终止。
        每次压缩产出 context.compact span（07 §3.6 T7 标定数据源）。
        """
        cm = self._context_manager
        assert cm is not None
        check = cm.check_before_call(self._estimate_context_tokens())
        if check == "FORCE_COMPACT":
            with self._compact_span("force") as span:
                try:
                    await cm.force_compact(self._conversation, prefix=self._assemble_payload())
                except Exception as exc:
                    self._mark_span_error(span, exc)
                    self._stop_reason = (
                        "context-full" if isinstance(exc, ContextFullError) else "error"
                    )
                    self._events(ErrorEvent(error=str(exc)))
                    return "TERMINAL"
                self._record_after_tokens(span)
            self._run_hook("compact", HookContext(event="compact", message="force"))
        elif check == "AUTO_COMPACT":
            with self._compact_span("auto") as span:
                try:
                    await cm.auto_compact(self._conversation, prefix=self._assemble_payload())
                except Exception as exc:
                    self._mark_span_error(span, exc)
                    raise
                self._record_after_tokens(span)
            self._run_hook("compact", HookContext(event="compact", message="auto"))
        return "CONTINUE"

    async def _emergency_compact(self, prefix: Payload) -> AgentStatus:
        """阶段 B（01 §6 ③）：prompt_too_long 撞墙后走 force 预算压一次再重试。"""
        cm = self._context_manager
        assert cm is not None
        with self._compact_span("emergency") as span:
            try:
                await cm.emergency_compact(self._conversation, prefix=prefix)
            except Exception as exc:
                self._mark_span_error(span, exc)
                self._stop_reason = "context-full" if isinstance(exc, ContextFullError) else "error"
                self._events(ErrorEvent(error=str(exc)))
                return "TERMINAL"
            self._record_after_tokens(span)
        self._run_hook("compact", HookContext(event="compact", message="emergency"))
        return "CONTINUE"

    async def _stream_llm(self, payload: Payload) -> Exception | None:
        """流式调一次 LLM（llm.call span + prompt 日志）；异常返回，由调用方决定重试/终止。

        prompt 日志（07 §3.4 + D9 `debug.log_full_prompt`）：默认只落摘要（长度 + 首尾
        片段），开关打开才落全文。全文含本地业务代码，防误导出泄露。
        """
        self._fire_pre_send(payload)
        telemetry = self._telemetry
        # 07：一次 LLM 调用 = 一个 llm.call span（model/耗时/tokens）。
        llm_cm: AbstractContextManager[Span | None] = nullcontext(None)
        if telemetry is not None:
            llm_cm = telemetry.span("llm.call", "client", {"model": self._model_name})
        with llm_cm as llm_span:
            log_full = bool(self._config.debug.get("log_full_prompt", False))
            if llm_span is not None and telemetry is not None:
                # D98 埋点：记录本次请求配置的 max_tokens——与 usage.output_tokens 对照，
                # 一眼可辨「配置未生效」vs「输出真的撞上限」（配合 llm.call span 落盘）。
                llm_span.attributes["max_tokens"] = payload.max_tokens
                # D90：默认记**本轮增量**（新增消息，正文可读）；log_full_prompt 才每轮全量。
                text = (
                    payload_text(payload)
                    if log_full
                    else incremental_payload_text(payload, self._payload_msg_offset)
                )
                self._payload_msg_offset = len(payload.messages)
                telemetry.add_log(
                    llm_span.span_id,
                    "debug",
                    text,
                    {"full": log_full},
                )
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
                        if llm_span is not None:
                            llm_span.attributes.update(
                                input_tokens=ev.usage.input_tokens,
                                output_tokens=ev.usage.output_tokens,
                                cache_read_tokens=ev.usage.cache_read_tokens,
                                cache_creation_tokens=ev.usage.cache_creation_tokens,
                            )
                        self._events(UsageEvent(ev.usage))
                    elif ev.type == "error" and ev.error is not None:
                        if isinstance(ev.error, ToolTruncatedError):
                            # B：输出侧截断——残缺 tool_use 不可执行。清半截缓冲，
                            # 把截断原因反馈模型（写入 conversation），返回异常让
                            # _loop_iteration_inner 重建 payload 重试（限次）。
                            # 不落 assistant 消息，避免悬空 tool_use。
                            # B2：empty 标记区分「纯思考截断」（引导别过度思考）与
                            # 「工具参数截断」（引导拆小输出）。
                            self._pending_text = []
                            self._pending_tool_uses = []
                            feedback = (
                                _EMPTY_TRUNCATED_FEEDBACK
                                if getattr(ev.error, "empty", False)
                                else _TRUNCATED_FEEDBACK
                            )
                            self._conversation.add_user_message(
                                feedback.format(reason=ev.error)
                            )
                            self._notify_conversation_change()
                            return ev.error
                        raise ev.error
            except Exception as exc:
                if llm_span is not None and telemetry is not None:
                    llm_span.status = "error"
                    telemetry.add_log(llm_span.span_id, "error", f"{type(exc).__name__}: {exc}")
                return exc
        # B2 兜底：流正常结束（无 error 事件）但空回复、且输出打满 max_tokens——
        # parser 未报截断（如 provider 返回 finish_reason=stop 但 usage 显示打满）
        # 时仍应反馈重试，不再落到 `not blocks: return TERMINAL` 静默吞掉。
        if (
            not self._pending_text
            and not self._pending_tool_uses
            and self._usage is not None
            and payload.max_tokens > 0
            and self._usage.output_tokens >= payload.max_tokens
        ):
            err = ToolTruncatedError(
                "输出达到 max_tokens 上限被截断，且未产生任何文本或工具调用",
                empty=True,
            )
            self._conversation.add_user_message(
                _EMPTY_TRUNCATED_FEEDBACK.format(reason=err)
            )
            self._notify_conversation_change()
            return err
        return None

    def _estimate_context_tokens(self) -> int:
        """当前上下文 token 估算（01 §5.4 窗口口径）：system + 全部消息。"""
        return estimate_messages_tokens(self._conversation.messages) + estimate_tokens(
            self._system_prompt
        )

    def _fire_pre_send(self, payload: Payload) -> None:
        """pre_send hook（06 §3.10 预留事件转正）：payload 组装后、LLM 调用前。

        完整上下文（payload_text 渲染 + 工具清单头）先落临时文件，路径经
        $PAYLOAD_PATH 展开给 hook 命令——内容超长，不走命令行参数/环境变量。
        每次 fire 独立临时文件：command 动作在事件循环内是后台调度（ensure_future），
        复用同名文件会被下一次调用的落盘覆盖。无 pre_send hook 注册时零开销直接返回。
        """
        hooks = self._hooks
        if hooks is None or not hooks.has_event("pre_send"):
            return
        try:
            tools = ", ".join(t.name for t in payload.tools) or "(无)"
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            header = (
                f"===== {stamp} | turn {self._turn} | tools: {tools} "
                f"| max_tokens: {payload.max_tokens} =====\n"
            )
            path = Path(tempfile.gettempdir()) / f"kdagent-payload-{uuid4().hex[:12]}.txt"
            path.write_text(header + payload_text(payload), encoding="utf-8")
        except Exception:
            return  # hook 辅助机制故障不拖垮主流程（06 §3.10 错误兜底同思路）
        self._run_hook("pre_send", HookContext(event="pre_send", payload_path=str(path)))

    def _compact_span(self, trigger: str) -> AbstractContextManager[Span | None]:
        """context.compact trace span（07 §3.6 T7 标定数据源：触发类型/压缩前 token）。

        telemetry 未开启返回 nullcontext——不改变任何压缩行为。before_tokens 在
        span 创建时估算（尚未压缩），after_tokens 由 `_record_after_tokens` 压缩后回填。
        """
        telemetry = self._telemetry
        if telemetry is None:
            return nullcontext(None)
        return telemetry.span(
            "context.compact",
            "compact",
            {"trigger": trigger, "before_tokens": self._estimate_context_tokens()},
        )

    def _record_after_tokens(self, span: Span | None) -> None:
        """压缩成功后回填 after_tokens——与 before_tokens 齐，渲染层可算压缩比（T7）。"""
        if span is not None:
            span.attributes["after_tokens"] = self._estimate_context_tokens()

    def _mark_span_error(self, span: Span | None, exc: Exception) -> None:
        """压缩失败标记 span error + 落错误日志（对齐 _stream_llm 风格）。"""
        if span is None or self._telemetry is None:
            return
        span.status = "error"
        self._telemetry.add_log(span.span_id, "error", f"{type(exc).__name__}: {exc}")

    def _assemble_payload(self) -> Payload:
        max_tokens = self._config.extra.get("max_tokens")
        if not isinstance(max_tokens, int):
            max_tokens = 100_000  # 全局默认（用户拍板：长任务不受 4k 截断）
        # 08 §3.3 静默读：记忆索引随 CLAUDE.md 走同一管线注入（索引是动态增长的——
        # build 时注入会过期，payload 组装时现取）。索引用 `<system-reminder>` 醒目注入
        # （与 09 §3.5 延迟工具/§3.9 Skill 同机制：改 reminder 不改 system → 前缀缓存
        # 不受影响）。只注入索引指针不注入全文——模型按需 ReadFile 取详情，省 token。
        # 08 §3.5 主动线：记忆使用说明随索引一起注入，让模型知道何时翻记忆。
        system = self._system_prompt
        if self._memory_store is not None:
            index = self._memory_store.index_markdown()
            if index:
                # 08 §3.3 静默读增强：user/feedback 类记忆（称呼/身份/偏好）全文注入
                # ——模型被问「你叫什么」时凭基础身份自我介绍不读文件，全文直接
                # 进上下文才可靠（store.user_memory_markdown）。
                prefs = self._memory_store.user_memory_markdown()
                memory_reminder = (
                    "<system-reminder>\n记忆索引已随初始上下文加载（KDAgent 四类 "
                    "Markdown 记忆）。涉及过往工作/决策/待办时，第一轮就直"
                    "接用 ReadFile 读取下方指针指向的 `.md` 文件取详情（指针已是"
                    "绝对路径，直接作为 ReadFile 的 path 参数即可），无需在文件系统"
                    "里重新探索记忆目录。\n"
                    + index
                    + prefs
                    + "\n</system-reminder>"
                )
                system = f"{system}\n\n{memory_reminder}\n\n{MEMORY_USAGE_INSTRUCTION}"
        # 09 §3.5 延迟加载：MCP 工具不进 tools 字段（数量不可控、token 省 ~85%），
        # 名字进 system-reminder 提示用 ToolSearch 加载。改 reminder 不改 system
        # → 前缀缓存不受影响。
        tools, deferred_names = self._tools.payload_schemas()
        if deferred_names:
            listing = "\n".join(deferred_names)
            reminder = (
                "<system-reminder>\n以下工具可通过 ToolSearch 加载（名称精确指定 "
                "或关键词搜索）：\n" + listing + "\n</system-reminder>"
            )
            system = f"{system}\n\n{reminder}"
        # 09 §3.9 两阶段加载：Skill 轻量清单注入 system-reminder（只列 name+description，
        # 渐进式披露）；完整 SOP 经 LoadSkill 按需加载。改 reminder 不改 system → 前缀缓存
        # 不受影响（与延迟工具同机制）。清单变化随 payload 组装现取，build 时注入会过期。
        if self._skills is not None:
            skills_reminder = build_skills_reminder(self._skills.list())
            if skills_reminder:
                system = f"{system}\n\n{skills_reminder}"
        return Payload(
            system=system,
            messages=self._conversation.messages,
            tools=tools,
            max_tokens=max_tokens,
        )

    async def _process_results(self, results: list[ToolResult]) -> list[ToolResult]:
        """01 入口处理（L1 落盘 + L2 在线摘要，M2）：历史只放预览/摘要+路径。

        处理发生在写入历史之前（01 P3：预处理必须在写入前）。content 被替换为
        最终形态，`persisted`/`compressed` 元信息由 ContextManager 内部持有，Agent 不感知。

        `p_tokens`：已有上下文 token（L2 经济性判定，01 §5.3）——system + 当前消息
        （含本轮 tool_use，尚未写入本批 tool_result）；`prefix`：主调用 payload，
        L2 摘要复用其前缀命中缓存。
        """
        if self._context_manager is None:
            return results
        prefix = self._assemble_payload()
        processed = await self._context_manager.on_tool_results(
            results, self._estimate_context_tokens(), prefix=prefix
        )
        return [replace(r, content=p.content) for r, p in zip(results, processed, strict=True)]

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
        """并发批 gather（带 CONCURRENCY_LIMIT 信号量）；串行批逐个执行。

        单个调用异常（HITL/权限/hook 等非工具本体抛）兜底为 errorResult——否则
        gather 整批抛、`for batch in batches:` 中断，剩余批 tool_result 缺失，
        历史留下 assistant(tool_calls) 无对应 tool_result（发请求 HTTP 400）。
        """
        async def run_one_safe(tc: ToolUseBlock) -> ToolResult:
            try:
                return await self._exec_one(tc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return self._error_result(tc, f"工具执行异常：{exc}")

        if batch.is_concurrency_safe:
            sem = asyncio.Semaphore(CONCURRENCY_LIMIT)

            async def run_one(tc: ToolUseBlock) -> ToolResult:
                async with sem:
                    return await run_one_safe(tc)

            return list(await asyncio.gather(*[run_one(tc) for tc in batch.calls]))
        results: list[ToolResult] = []
        for tc in batch.calls:
            results.append(await run_one_safe(tc))
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
            events=self._events,  # 12 测试闭环：TestRunner 结构化结果 → 事件流
        )
        telemetry = self._telemetry
        # 06 M3 可控档：五层裁决。checker 存在时接管 require_confirm（升级为完整
        # 裁决系统）；拒绝不终止 Loop——is_error 结果进历史，模型下轮自行调整。
        permission = self._permission_checker
        if permission is not None:
            # 07：一次裁决 = 一个 permission.check span（effect/verdict 入属性）。
            perm_cm: AbstractContextManager[Span | None] = nullcontext(None)
            if telemetry is not None:
                perm_cm = telemetry.span(
                    "permission.check", "security", {"tool": tool_use.name}
                )
            with perm_cm as perm_span:
                decision = permission.check(tool, tool_use.input)
                if perm_span is not None:
                    perm_span.attributes["effect"] = decision.effect
                if decision.effect == "deny":
                    return self._denied_result(tool_use, "权限拒绝：" + decision.reason)
                if decision.effect == "ask":
                    verdict = await self._ask_permission(tool_use.name, tool_use.input)
                    if perm_span is not None:
                        perm_span.attributes["verdict"] = verdict
                    if verdict == "deny":
                        return self._denied_result(tool_use, "已被用户拒绝")
                    if verdict == "allow_always":
                        # §3.7「始终允许」→ 追加本地规则，同类操作下次直接放行。
                        content = permission.extract_content(tool, tool_use.input)
                        permission.learn(tool_use.name, content)
        else:
            # 规格 05 §3.4（无 checker 时保留原 Y/N 前置）：选 no 返回 is_error 结果。
            if (
                tool.require_confirm
                and ctx.confirm is not None
                and not await ctx.confirm(tool_use.name, tool_use.input)
            ):
                return self._denied_result(tool_use, "执行已被用户拒绝")
        # 06 §3.9/§3.10：pre_tool_use hooks（唯一可拦截事件）——在权限基线之上做
        # 参数内容级动态拦截；reject 短路，理由作为错误结果进历史。
        if self._hooks is not None:
            hook_ctx = HookContext(
                event="pre_tool_use",
                tool_name=tool_use.name,
                tool_args=tool_use.input,
                file_path=self._hook_file_path(tool_use.input),
            )
            hook_cm: AbstractContextManager[Span | None] = nullcontext(None)
            if telemetry is not None:
                hook_cm = telemetry.span("hook.run", "security", {"event": "pre_tool_use"})
            with hook_cm:
                reject = self._hooks.run_pre_tool(hook_ctx)
            if reject is not None:
                return self._denied_result(tool_use, reject.reason)
        # 07：一次工具执行 = 一个 tool.exec span（复用 ToolResult.duration_ms/is_error）。
        # input 入 attributes（12 §3.6 规则量化数据源；本地完整内容，exporter 出口脱敏）。
        tool_cm: AbstractContextManager[Span | None] = nullcontext(None)
        if telemetry is not None:
            tool_cm = telemetry.span(
                "tool.exec", "tool", {"tool": tool_use.name, "input": tool_use.input}
            )
        with tool_cm as tool_span:
            try:
                result = await tool.execute(ctx, tool_use.input)
            except Exception as exc:
                result = self._error_result(tool_use, f"执行异常：{exc}")
            # 必须在 span 关闭（__exit__ 落盘）前写入，否则属性丢失。
            if tool_span is not None:
                tool_span.attributes["is_error"] = result.is_error
                tool_span.attributes["duration_ms"] = result.duration_ms
                # 11 §3.4 复核「阅读」返回：output 进 span（截断），供 span_detail 逐事件挑出
                tool_span.attributes["output"] = result.content[: _TRACE_OUTPUT_CAP]
                # 01 §9.2 T8：X 分布标定数据源——原始长度/估算 token/tool_use_id
                # （output 已截断，output_chars/output_tokens 记完整原始大小）
                tool_span.attributes["output_chars"] = len(result.content)
                tool_span.attributes["output_tokens"] = estimate_tokens(result.content)
                tool_span.attributes["tool_use_id"] = tool_use.id
                if result.is_error:
                    tool_span.status = "error"
        if self._hooks is not None:
            self._run_hook(
                "post_tool_use",
                HookContext(
                    event="post_tool_use",
                    tool_name=tool_use.name,
                    tool_args=tool_use.input,
                    file_path=self._hook_file_path(tool_use.input),
                    message=result.content,
                ),
            )
        self._events(
            ToolResultEvent(
                name=result.name,
                content=result.content,
                is_error=result.is_error,
                duration_ms=result.duration_ms,
            )
        )
        return result

    # ---- 06 M3 可控档：权限 HITL / hook 辅助 ----

    async def _ask_permission(self, tool_name: str, input: dict[str, Any]) -> PermissionVerdict:
        """L5 HITL（06 §3.7）：emit PermissionRequestEvent，阻塞等 UI 回传裁决。

        UI 消费方 set_result(allow/deny/allow_always)；未来不 resolved 时调用方悬挂
        （交互环境总有 UI 消费；headless 测试注入自动裁决 sink）。
        """
        summary = self._permission_summary(tool_name, input)
        future: asyncio.Future[PermissionVerdict] = asyncio.get_running_loop().create_future()
        self._events(PermissionRequestEvent(tool_name=tool_name, summary=summary, future=future))
        return await future

    def _permission_summary(self, tool_name: str, input: dict[str, Any]) -> str:
        """审批对话框摘要：`Bash git commit -m "fix"`（超长截断）。"""
        text = f"{tool_name} {dict(input)}"
        return text if len(text) <= 200 else text[:200] + "…"

    def _hook_file_path(self, input: dict[str, Any]) -> str:
        """hook 上下文的 FILE_PATH：filesystem 工具取 path 参数，其余空串。"""
        path = input.get("path")
        return str(path) if isinstance(path, str) else ""

    def _denied_result(self, tool_use: ToolUseBlock, message: str) -> ToolResult:
        """权限拒绝/pre_tool 拦截结果：is_error 进历史 + 发 UI 事件展示原因。"""
        result = self._error_result(tool_use, message)
        self._events(
            ToolResultEvent(
                name=result.name,
                content=result.content,
                is_error=True,
                duration_ms=0,
            )
        )
        return result

    def _run_hook(self, event: str, ctx: HookContext) -> None:
        """生命周期/副作用 hook（06 §3.10）；hooks None = 无自动化，静默。"""
        if self._hooks is None:
            return
        telemetry = self._telemetry
        # 07：一次 hook 匹配 = 一个 hook.run span（event 入属性）。
        hook_cm: AbstractContextManager[Span | None] = nullcontext(None)
        if telemetry is not None:
            hook_cm = telemetry.span("hook.run", "security", {"event": event})
        with hook_cm:
            self._hooks.run(event, ctx)

    def _error_result(self, tool_use: ToolUseBlock, message: str) -> ToolResult:
        return ToolResult(
            tool_use_id=tool_use.id, name=tool_use.name, content=message, is_error=True
        )

    def _update_circuit_breaker(self, results: list[ToolResult]) -> None:
        """连续失败计数；达阈值注入 system-reminder 后复位（规格 02 §3.5）。

        TestingEvent（12）触发部分留 M5；此处只覆盖工具执行失败。
        Replan（12 §3.3，D57）：一批全部成功 = 路径有进展 → Replan 触发计数复位；
        断路器反复触发 ≥ REPLAN_TRIGGER_COUNT = 路径反复受阻 → 注入 Replan 引导
        （整体重写 todo，废弃旧列表，冷却防刷屏）。
        """
        if results and all(not r.is_error for r in results):
            self._consecutive_failures = 0
            self._circuit_breaker_triggers = 0  # 有进展，受阻不是持续不可行
        else:
            self._consecutive_failures += sum(1 for r in results if r.is_error)
        if self._consecutive_failures >= CIRCUIT_BREAK_LIMIT:
            self._consecutive_failures = 0
            self._circuit_breaker_triggers += 1
            self._conversation.add_user_message("", extra_blocks=[TextBlock(_CIRCUIT_REMINDER)])
            self._notify_conversation_change()
            if (
                self._circuit_breaker_triggers >= REPLAN_TRIGGER_COUNT
                and self._turn - self._replan_inject_turn >= REINJECT_COOLDOWN
                and self._last_todos is not None
            ):
                self._replan_inject_turn = self._turn
                self._circuit_breaker_triggers = 0  # 注入后复位，防 Replan 后连发
                self._conversation.add_user_message(
                    "", extra_blocks=[TextBlock(build_replan_reminder(self._last_todos))]
                )
                self._notify_conversation_change()

    def _observe_todos(self, batch: Batch, results: list[ToolResult]) -> None:
        """12 §3.3 双层检查点：声明驱动主检查点 + 行为观察兜底（每批工具结果后）。

        第一层：TodoWrite 更新到步骤边界（task 标 completed 且有判据）→ 注入
        「产出 vs 判据」自检 reminder（含完整 todo 快照，§3.2 时点①）。
        第二层②：todo 长期滞后于行为（工具活跃但未更新规划）→ 强制刷新快照。
        第二层③：单轮跨文件大改（安全类信号，始终生效）→ 变更范围警告。
        注入走 user 消息 extra_blocks（§3.2 硬约束：todo 不进 system prompt 常驻区）。
        """
        todo_names = {r.name for r in results}
        if "TodoWrite" in todo_names:
            todos = self._latest_todo_input()
            if todos is not None:
                event = todo_progress(self._last_todos, todos)
                self._last_todos = todos
                self._turns_since_todo_update = 0
                if event is not None:
                    # 行为观察①（D58）：机械判据自动核验——证据缺失则声明 vs 行为
                    # 不一致，拦截警告替代自检；软判据走 D54 既有自评路径。
                    mismatch = self._verify_step_boundary(event)
                    if mismatch is not None:
                        reminder = mismatch
                    else:
                        reminder = build_checkpoint_reminder(event, todos)
                    self._conversation.add_user_message("", extra_blocks=[TextBlock(reminder)])
                    self._notify_conversation_change()
        else:
            self._turns_since_todo_update += 1
        if (
            self._last_todos is not None
            and self._turns_since_todo_update >= STALE_TODO_THRESHOLD
            and self._turn - self._stale_inject_turn >= REINJECT_COOLDOWN
        ):
            self._stale_inject_turn = self._turn
            self._conversation.add_user_message(
                "", extra_blocks=[TextBlock(build_stale_todo_reminder(self._last_todos))]
            )
            self._notify_conversation_change()
        # 行为观察③：累计本轮 write/edit 目标，轮末统一检查（§3.3 第二层③）。
        self._round_write_paths.extend(
            str(tc.input.get("path", ""))
            for tc in batch.calls
            if tc.name in ("WriteFile", "EditFile") and tc.input.get("path")
        )

    def _checkpoint_round_end(self) -> None:
        """本轮汇总的跨文件大改检查（§3.3 第二层③，单轮口径跨批累计）。"""
        if (
            len(self._round_write_paths) >= LARGE_CHANGE_THRESHOLD
            and self._turn - self._large_inject_turn >= REINJECT_COOLDOWN
        ):
            self._large_inject_turn = self._turn
            self._conversation.add_user_message(
                "", extra_blocks=[TextBlock(build_large_change_warning(self._round_write_paths))]
            )
            self._notify_conversation_change()

    def _observe_error_patterns(self, batch: Batch, results: list[ToolResult]) -> None:
        """错误模式沉淀（08 §3.3 feedback 消费方，T33-3）：写工具失败 → 诊断 → 沉淀。

        事件驱动的客观事实记录（`diagnose_failure` 纯函数归类，不依赖 LLM）——
        失败是硬事实，不等静默写（08 §3.4 LLM 提炼）稀释。沉淀到用户级 feedback
        记忆，新会话经 MEMORY.md 索引自动加载（08 §3.3 静默读）。无 memory_store
        （08 未启用）→ 静默跳过。
        """
        if self._memory_store is None:
            return
        for tc, r in zip(batch.calls, results, strict=True):
            if tc.name not in ("EditFile", "WriteFile") or not r.is_error:
                continue
            pattern = diagnose_failure(tc.name, r.content)
            if pattern is None:
                continue
            mf = pattern_memory(pattern)
            if mf.name in self._seen_patterns:
                continue  # 本会话已沉淀过同类根因
            if self._memory_store.create(mf):
                self._seen_patterns.add(mf.name)

    def _latest_todo_input(self) -> list[dict[str, Any]] | None:
        """从会话找最近一次 TodoWrite 的结构化 input（读会话，不改工具路径）。"""
        for msg in reversed(self._conversation.messages):
            if msg.role != "assistant":
                continue
            for block in reversed(msg.content):
                if isinstance(block, ToolUseBlock) and block.name == "TodoWrite":
                    todos = block.input.get("todos")
                    if isinstance(todos, list):
                        return todos
        return None

    def _verify_step_boundary(self, event: CheckpointEvent) -> str | None:
        """行为观察①（D58）：机械判据自动核验步骤边界产出。

        classify_criteria 定类型 → 收集本会话行为证据 → 证据缺失返回拦截警告
        （声明 vs 行为不一致，替代 D54 自检）；证据齐全/软判据 → None 走既有路径。
        """
        kind = classify_criteria(event.accept_criteria)
        if kind == VerificationKind.TEST:
            tool_names, bash_commands = self._evidence_from_conversation()
            if not has_test_evidence(tool_names, bash_commands):
                return build_mismatch_reminder(
                    event,
                    self._last_todos or [],
                    "判据要求测试通过，但本会话未跑过测试",
                )
        elif kind == VerificationKind.FILE:
            target = file_target(event.accept_criteria)
            if target is not None and not (self._work_dir / target).exists():
                return build_mismatch_reminder(
                    event,
                    self._last_todos or [],
                    f"判据要求文件 {target} 存在，但该文件不存在",
                )
        return None

    def _evidence_from_conversation(self) -> tuple[list[str], list[str]]:
        """收集本会话工具调用名 + Bash 命令（行为观察① 的证据源，读会话）。"""
        tool_names: list[str] = []
        bash_commands: list[str] = []
        for msg in self._conversation.messages:
            if msg.role != "assistant":
                continue
            for block in msg.content:
                if not isinstance(block, ToolUseBlock):
                    continue
                tool_names.append(block.name)
                if block.name == "Bash":
                    cmd = block.input.get("command")
                    if isinstance(cmd, str):
                        bash_commands.append(cmd)
        return tool_names, bash_commands
