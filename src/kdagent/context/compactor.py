"""上下文压缩（规格 01 §5.3 L2 在线摘要 / §5.4 L3 Auto-Compact）。

L2 是用户原创增量：中等大小、低信息密度、经济性通过的**工具结果**，在写入历史前
现场压缩为语义摘要（一次 LLM 调用，复用主对话 provider + 前缀缓存），降低噪声、
延长窗口寿命、减少 L3 触发频率。

L3 Auto-Compact：累积 token 逼近窗口上限时的全量摘要——9 部分结构化摘要 + 保留近期
原文（tool_use/tool_result 配对完整）+ 文件/todo 快照重灌（12 §3.2）；触发与
熔断/强制/紧急兜底逻辑在 `ContextManager`（§6.1 独立预算）与主循环（`engine.agent`）。

流程（§5.5）：L1 落盘判定之后、原样写入之前。
- `should_online_compress()`：三门槛决策 —— 作用域（X 范围）→ 信息密度 → 经济性盈亏平衡。
- `L2Compressor.compress()`：原文落盘（D6）+ 调用 LLM 两阶段生成摘要。

经济性模型（用户建模，已校验；`01` §5.3）：
  break_even_N = (C_in*(I+S) + C_out*S + C_hit*P) / (C_hit*(X-S))
  expected_N = 剩余窗口 / 每轮平均增长 → expected_N > break_even_N 才压缩。
"""

from __future__ import annotations

import json
import re
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from kdagent.context.history import PersistedOutput, ProcessedToolResult
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import LLMClient, Payload, Usage
from kdagent.engine.messages import (
    ContentBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from kdagent.sessions.records import TodoItemRecord
from kdagent.tools.base import ToolResult

# 运行时零 obs 依赖（D69 曾直接 import 触发 obs→compactor→obs 循环：obs/metrics 聚合
# 消费 estimate_token_cost，compactor 又反向 import obs/telemetry）。类型注解经
# `from __future__ import annotations` 惰性求值，TYPE_CHECKING 足够，span 走 duck type。
if TYPE_CHECKING:
    from kdagent.obs.model import Span
    from kdagent.obs.telemetry import Telemetry

_CHARS_PER_TOKEN = 4  # 字符/token 启发式（对齐 L1 的 50K 字符 ≈ 12.5K token 注释）

# 01 §9.1：L2 参数（全部参数化；T8 待实测标定）
ONLINE_COMPRESS_MIN = 8_000  # L2 在线压缩下限（token 估算）
TOOL_RESULT_SAVE_THRESHOLD_TOKENS = 50_000 // _CHARS_PER_TOKEN  # 12.5K，镜像 L1 落盘阈值
COMPRESS_INSTRUCTION_TOKENS = 500  # 压缩指令 prompt 长度（token 估算）
WINDOW_SIZE = 200_000  # 上下文窗口基准（token）
AVG_GROWTH_PER_TURN = 2_000  # 每轮平均上下文增长估算（token）
SUMMARY_MAX_TOKENS = 2_048  # L2 摘要输出上限

PREVIEW_CHARS = 2_000  # 落盘预览长度（L1/L2 共用）

# 01 §9.1：各类型压缩率先验（待实测校准）。代码 0.9 仅作参考——实际走 info_density HIGH 提前返回。
EXPECTED_RATIO_BY_TYPE: dict[str, float] = {
    "stack": 0.2,
    "log": 0.3,
    "web": 0.3,
    "search": 0.4,
    "test": 0.4,
    "code": 0.9,
}

# 01 §9.1：L3 参数（200K 窗口为基准，全部参数化；T7 待实测标定）
SUMMARY_OUTPUT_RESERVE = 20_000  # L3 摘要输出预留（9 部分 + analysis）
SAFETY_MARGIN = 13_000  # 安全余量（挡单轮并行工具结果波动）
FORCE_EXTRA_MARGIN = 3_000  # 强制线比自动线更靠近窗口顶部的余量
RECENT_KEEP_TOKENS = 10_000  # 压缩后保留近期原文（token）
RECENT_KEEP_MIN_MESSAGES = 5  # 或至少 5 条消息
FILES_TO_RESTORE = 5  # 重附最近访问文件（个）
FILE_BUDGET_TOKENS = 5_000  # 每个文件快照 ≤ 5K token
SKILL_BUDGET_TOKENS = 25_000  # Skill 快照预算（09 M4 落地后生效）
L3_SUMMARY_MAX_TOKENS = 6_000  # L3 摘要输出上限
COMPACT_FAILURE_LIMIT = 3  # auto/force 独立熔断次数（01 §6）
SUMMARY_RETRY_LIMIT = 4  # 摘要请求超长重试：丢最旧组×2 + 丢 20%×1

# 01 §5.4/§5.5/§6：触发线（200K → 自动 167K / 强制 177K）。
# 注意 §6 表中「窗口-33K-3K」为笔误——177K = window - 20K(摘要预留) - 3K，高于自动线。
AUTO_COMPACT_TRIGGER = WINDOW_SIZE - SUMMARY_OUTPUT_RESERVE - SAFETY_MARGIN  # 167K
FORCE_COMPACT_LINE = WINDOW_SIZE - SUMMARY_OUTPUT_RESERVE - FORCE_EXTRA_MARGIN  # 177K


@dataclass(frozen=True, slots=True)
class CostParams:
    """token 计价（元/百万 token；01 T5-1 待按 provider 标定）。

    默认值落在"典型区间"（(C_in+C_out)/C_hit ≈ 50）；DeepSeek 命中折扣更深
    （比值 ≈ 150-160，L2 决策更保守），随 M2 收尾的前缀缓存实测校准。
    """

    c_in: float = 2.0
    c_out: float = 8.0
    c_hit: float = 0.2


DEFAULT_COST = CostParams()


def estimate_token_cost(
    input_tokens: int,
    output_tokens: int,
    cache_tokens: int,
    cost: CostParams | None = None,
) -> float:
    """token → 估算成本（元）：输入×c_in + 输出×c_out + 缓存命中×c_hit，除百万。

    纯函数、取 int 不依赖 Usage 类型（解耦）；01 T5-1 标定前用 DEFAULT_COST 典型区间。
    评测 `metrics_by_run` 补成本（11 §3.8，D67）与 L2 压缩决策共用同一计价模型。
    """
    c = DEFAULT_COST if cost is None else cost
    return (
        input_tokens * c.c_in + output_tokens * c.c_out + cache_tokens * c.c_hit
    ) / 1_000_000


@dataclass(frozen=True, slots=True)
class CompressedOutput:
    """L2 在线摘要结果（01 §8）：语义摘要 + 原文落盘路径 + 分类元信息。"""

    summary: str
    path: str  # 原文落盘路径（决策 D6）
    preview: str
    original_type: str
    info_density: Literal["LOW", "HIGH"]


def estimate_tokens(text: str) -> int:
    """字符数 → token 粗略估算（~4 字符/token；T8 待实测校准）。"""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def estimate_messages_tokens(messages: list[Message]) -> int:
    """消息列表 → token 估算（01 §5.3 经济性模型里的 P = 已有上下文）。"""
    return sum(estimate_tokens(_render_blocks(m.content)) for m in messages)


def _render_blocks(blocks: list[ContentBlock]) -> str:
    """消息块 → 文本（tool_use 序列化为 JSON），供 token 估算。"""
    parts: list[str] = []
    for b in blocks:
        if isinstance(b, TextBlock):
            parts.append(b.text)
        elif isinstance(b, ThinkingBlock):
            parts.append(b.thinking)
        elif isinstance(b, ToolResultBlock):
            parts.append(b.content)
        elif isinstance(b, ToolUseBlock):
            parts.append(json.dumps({"name": b.name, "input": b.input}, ensure_ascii=False))
    return "\n".join(parts)


# ---- 内容分类（启发式；T8 实测校准） --------------------------------------

_STACK_RE = re.compile(r'Traceback \(most recent call last\)|File "[^"]+\.py", line \d+')
_TEST_RE = re.compile(r"\d+ passed|\d+ failed|PASSED|FAILED|Ran \d+ tests?")
_LOG_RE = re.compile(r"\[\w+\]\s|^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|^\d{2}:\d{2}:\d{2}\.\d+", re.M)


def classify_content(result: ToolResult) -> str:
    """工具结果内容分类：stack / log / web / search / test / code / other。

    以内容标记为主、工具名为辅（Grep/Glob 结果天然是路径列表）。
    """
    name = result.name
    head = result.content[:4_000]
    if _STACK_RE.search(head):
        return "stack"
    if _TEST_RE.search(head):
        return "test"
    if name in {"Grep", "Glob"}:
        return "search"
    if "<html" in head or "<body" in head or "<div" in head:
        return "web"
    if _LOG_RE.search(head):
        return "log"
    if name in {"ReadFile", "WriteFile", "EditFile"} and _code_like(result.content):
        return "code"
    return "other"


def _code_like(content: str) -> bool:
    """内容像源代码：代码关键字命中 ≥3，或长文本强缩进（防御日志被误判）。"""
    sample = content[:8_000]
    keywords = ("def ", "class ", "import ", "from ", "return ", "function ", "=>", "if __name__")
    if sum(1 for k in keywords if k in sample) >= 3:
        return True
    lines = [ln for ln in sample.splitlines() if ln.strip()]
    if len(lines) < 10:
        return False
    indented = sum(1 for ln in lines if ln[:1] in (" ", "\t"))
    return indented / len(lines) > 0.4


def info_density(result: ToolResult) -> Literal["LOW", "HIGH"]:
    """信息密度门槛：高密度（源代码/配置/文档/todo 快照）不压缩，避免丢信息。

    `other`（无法判断）也按 HIGH 处理——不确定时不压缩（§5.3"避免丢失信息"）。
    """
    if result.name == "TodoWrite":
        return "HIGH"  # 12：todo 快照必须保真，L2 不得摘（检查点对表依赖）
    if classify_content(result) in {"code", "other"}:
        return "HIGH"
    return "LOW"


# ---- 经济性决策（01 §5.3） -------------------------------------------------


def estimate_remaining_turns(
    p_tokens: int,
    window_size: int = WINDOW_SIZE,
    avg_growth_per_turn: int = AVG_GROWTH_PER_TURN,
) -> int:
    """剩余窗口还能复用几轮（expected_N）：预算 / 每轮平均增长，最低 1。"""
    budget = window_size - p_tokens
    if budget <= 0:
        return 1
    return max(1, budget // max(1, avg_growth_per_turn))


def should_online_compress(
    result: ToolResult,
    p_tokens: int,
    *,
    expected_remaining: int | None = None,
    cost: CostParams | None = None,
    window_size: int = WINDOW_SIZE,
    avg_growth_per_turn: int = AVG_GROWTH_PER_TURN,
    online_compress_min: int = ONLINE_COMPRESS_MIN,
    save_threshold_tokens: int = TOOL_RESULT_SAVE_THRESHOLD_TOKENS,
    expected_ratio: dict[str, float] | None = None,
    compress_instruction_tokens: int = COMPRESS_INSTRUCTION_TOKENS,
) -> bool:
    """L2 三门槛决策（01 §5.3 决策函数）。

    1) 作用域：X 太小不值得、太大走 L1 落盘；
    2) 信息密度：高密度文本不压缩；
    3) 经济性：压缩后还需复用 `expected_N` 轮才能回本，否则不值得。
    """
    X = estimate_tokens(result.content)
    if online_compress_min > X or save_threshold_tokens <= X:
        return False
    if info_density(result) == "HIGH":
        return False
    alpha = (EXPECTED_RATIO_BY_TYPE if expected_ratio is None else expected_ratio).get(
        classify_content(result), 0.5
    )
    S = max(1, int(X * alpha))
    c = DEFAULT_COST if cost is None else cost
    denominator = c.c_hit * (X - S)
    if denominator <= 0:
        return False
    break_even = (
        c.c_in * (compress_instruction_tokens + S) + c.c_out * S + c.c_hit * p_tokens
    ) / denominator
    if expected_remaining is None:
        expected_remaining = estimate_remaining_turns(p_tokens, window_size, avg_growth_per_turn)
    return expected_remaining > break_even


# ---- 落盘 + L2 摘要执行 ----------------------------------------------------


async def _llm_text(llm: LLMClient, payload: Payload) -> tuple[str, Usage | None]:
    """流式调用，只收文本（同时收 usage 供 T8 标定）；error 事件抛错。

    L2/L3 摘要共用（摘要调用保留 tools 参数命中缓存，但代码只读文本）。
    返回 (文本, usage)——usage 由调用方决定是否消费。
    """
    parts: list[str] = []
    usage: Usage | None = None
    async for ev in llm.stream_chat(payload):
        if ev.type == "text_delta" and ev.text:
            parts.append(ev.text)
        elif ev.type == "usage" and ev.usage is not None:
            usage = ev.usage
        elif ev.type == "error" and ev.error is not None:
            raise ev.error
    return "".join(parts), usage


def write_persisted(
    persist_dir: Path, key: str, content: str, preview_chars: int = PREVIEW_CHARS
) -> PersistedOutput:
    """完整内容写盘（L1/L2 共用，01 §5.2/§5.3），返回 PersistedOutput。"""
    persist_dir.mkdir(parents=True, exist_ok=True)
    path = persist_dir / f"{key}.txt"
    path.write_text(content, encoding="utf-8")
    return PersistedOutput(preview=content[:preview_chars], path=str(path), full_size=len(content))


_L2_INSTRUCTION = """\
请对上面的工具输出做信息压缩，生成浓缩摘要。要求：
1. 保留关键可回溯信息：文件路径、行号、错误码、断言/预期对比、URL、关键数字与结论。
2. 两阶段生成：先输出 <analysis> 草稿（梳理信息结构、识别冗余与关键锚点），再输出 <summary> 正文。
3. <summary> 为纯文本，禁止代码块与标题；只输出摘要正文。
4. 禁止调用任何工具（工具调用会被拒绝），只输出纯文本。"""


def _two_phase_extract(text: str) -> str:
    """提取两阶段生成的 <summary> 正文；无标签时剥掉 <analysis> 草稿取余下。"""
    m = re.search(r"<summary>(.*?)</summary>", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return re.sub(r"<analysis>.*?</analysis>", "", text, flags=re.DOTALL).strip()


def _compressed_text(co: CompressedOutput, full_size: int) -> str:
    """历史中的 L2 结果块（01 §5.3 格式）：摘要 + 路径，不含原文。"""
    return (
        "<compressed-output>\n"
        f"工具结果已在线摘要。完整内容（{full_size // 1024}KB）已保存到：\n"
        f"{co.path}\n\n"
        f"摘要：\n{co.summary}\n\n"
        "如需完整内容，用 ReadFile 按 offset/limit 读取。\n"
        "</compressed-output>"
    )


class L2Compressor:
    """L2 在线摘要执行器：判定 + 原文落盘 + LLM 两阶段摘要。

    复用主对话 provider（`llm`）与主 payload 前缀（`prefix`）以命中前缀缓存
    （01 §5.3 经济性模型假设）；`system_prompt` 仅在无 prefix（独立调用）时兜底。
    """

    def __init__(
        self,
        llm: LLMClient,
        *,
        persist_dir: Path,
        system_prompt: str = "",
        online_compress_min: int = ONLINE_COMPRESS_MIN,
        save_threshold_tokens: int = TOOL_RESULT_SAVE_THRESHOLD_TOKENS,
        expected_ratio: dict[str, float] | None = None,
        compress_instruction_tokens: int = COMPRESS_INSTRUCTION_TOKENS,
        cost: CostParams | None = None,
        window_size: int = WINDOW_SIZE,
        avg_growth_per_turn: int = AVG_GROWTH_PER_TURN,
        preview_chars: int = PREVIEW_CHARS,
        telemetry: Telemetry | None = None,
    ) -> None:
        self._llm = llm
        self._persist_dir = persist_dir
        self._system_prompt = system_prompt
        self._online_compress_min = online_compress_min
        self._save_threshold_tokens = save_threshold_tokens
        self._expected_ratio = expected_ratio
        self._compress_instruction_tokens = compress_instruction_tokens
        self._cost = cost
        self._window_size = window_size
        self._avg_growth_per_turn = avg_growth_per_turn
        self._preview_chars = preview_chars
        self._telemetry = telemetry  # 07 T8 标定：L2 压缩成本 span

    def should_online_compress(self, result: ToolResult, p_tokens: int) -> bool:
        """用本压缩器参数调用模块级决策函数（01 §5.3）。"""
        return should_online_compress(
            result,
            p_tokens,
            cost=self._cost,
            window_size=self._window_size,
            avg_growth_per_turn=self._avg_growth_per_turn,
            online_compress_min=self._online_compress_min,
            save_threshold_tokens=self._save_threshold_tokens,
            expected_ratio=self._expected_ratio,
            compress_instruction_tokens=self._compress_instruction_tokens,
        )

    async def compress(
        self, result: ToolResult, prefix: Payload | None = None
    ) -> ProcessedToolResult:
        """原文落盘（D6）→ 摘要调用 → 返回历史形态（<compressed-output> + 元信息）。

        摘要失败时抛异常，由调用方（ToolResultHandler）回退原文。
        每次压缩产出 context.l2_compress span（07 §3.6 T8 标定数据源：X/S token +
        usage；异常标 error，对齐 _stream_llm 风格）。
        """
        po = write_persisted(
            self._persist_dir, result.tool_use_id, result.content, self._preview_chars
        )
        telemetry = self._telemetry
        span_cm: AbstractContextManager[Span | None] = nullcontext(None)
        if telemetry is not None:
            span_cm = telemetry.span(
                "context.l2_compress", "context", {"tool_use_id": result.tool_use_id}
            )
        with span_cm as span:
            try:
                text, usage = await self._call_llm_text(
                    self._build_summary_payload(result, prefix)
                )
            except Exception as exc:
                if span is not None and telemetry is not None:
                    span.status = "error"
                    telemetry.add_log(
                        span.span_id, "error", f"{type(exc).__name__}: {exc}"
                    )
                raise
            summary = _two_phase_extract(text) or f"(在线摘要未生成，完整内容见 {po.path})"
            co = CompressedOutput(
                summary=summary,
                path=po.path,
                preview=po.preview,
                original_type=classify_content(result),
                info_density=info_density(result),
            )
            if span is not None:
                span.attributes.update(
                    original_type=classify_content(result),
                    info_density=info_density(result),
                    x_tokens=estimate_tokens(result.content),
                    s_tokens=estimate_tokens(summary),
                )
                if usage is not None:
                    span.attributes.update(
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cache_read_tokens=usage.cache_read_tokens,
                        cache_creation_tokens=usage.cache_creation_tokens,
                    )
            return ProcessedToolResult(
                content=_compressed_text(co, po.full_size), persisted=po, compressed=co
            )

    def _build_summary_payload(self, result: ToolResult, prefix: Payload | None) -> Payload:
        """摘要调用载荷：主前缀（复用前缀缓存）+ 追加[原文 + 压缩指令]。

        带 prefix 时 system/messages/tools 与主调用逐字节一致（命中缓存）；
        独立调用（测试）用构造时的 system_prompt 兜底。
        """
        block = Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id=result.tool_use_id, content=result.content, is_error=result.is_error
                ),
                TextBlock(_L2_INSTRUCTION),
            ],
        )
        if prefix is not None:
            return Payload(
                system=prefix.system,
                messages=[*prefix.messages, block],
                tools=prefix.tools,
                max_tokens=SUMMARY_MAX_TOKENS,
            )
        return Payload(
            system=self._system_prompt,
            messages=[block],
            max_tokens=SUMMARY_MAX_TOKENS,
        )

    async def _call_llm_text(self, payload: Payload) -> tuple[str, Usage | None]:
        """流式调用，只收文本 + usage；error 事件抛错；tool_use 事件忽略。"""
        return await _llm_text(self._llm, payload)


# ---- L3 Auto-Compact（01 §5.4/§6） -----------------------------------------


class ContextFullError(RuntimeError):
    """上下文已满且强制压缩失败（01 §6）：交给用户手动处理（/compact 或清理会话）。"""


@dataclass(frozen=True, slots=True)
class CompactResult:
    """L3 Auto-Compact 结果（01 §8）：摘要消息 + 保留的近期原文 + 会话路径。

    压缩后的历史 = `[summary_message, *kept_recent]`（摘要在前、近期原文随后）。
    """

    summary_message: Message
    kept_recent: list[Message]
    session_path: str


def render_todo_snapshot(todos: list[TodoItemRecord] | None) -> str:
    """todo 快照纯文本（12 §3.2：快照保真、L3 压缩后重灌 / 恢复时点④共用）。"""
    if not todos:
        return ""
    lines: list[str] = []
    for t in todos:
        mark = {"completed": "[x]", "in_progress": "[→]", "pending": "[ ]"}.get(t.status, "[ ]")
        group = f"（{t.group}）" if t.group else ""
        lines.append(f"- {mark} {group}{t.content}")
        for step in t.steps or []:
            lines.append(f"    - {step.description}")
    return "\n".join(lines)


def _is_tool_result_message(msg: Message) -> bool:
    """user 消息且含 tool_result 块（配对边界判定用）。"""
    return msg.role == "user" and any(isinstance(b, ToolResultBlock) for b in msg.content)


def _recent_keep(
    messages: list[Message],
    recent_keep_tokens: int = RECENT_KEEP_TOKENS,
    min_messages: int = RECENT_KEEP_MIN_MESSAGES,
) -> list[Message]:
    """尾部近期原文（01 §5.4）：≥10K token 或 ≥5 条，且不在 tool_use/tool_result 配对中间切断。

    保留的是**连续后缀**：从尾部往回数，达到预算即停；若后缀首条是 tool_result 消息
    （其 tool_use 在更早处），向前扩展把配对一并收进。
    """
    if not messages:
        return []
    kept: list[Message] = []
    tokens = 0
    for msg in reversed(messages):
        kept.append(msg)
        tokens += estimate_messages_tokens([msg])
        if tokens >= recent_keep_tokens and len(kept) >= min_messages:
            break
    kept.reverse()
    idx = len(messages) - len(kept)
    while idx > 0 and _is_tool_result_message(messages[idx]):
        idx -= 1  # 边界是 tool_result 消息 → 前扩包含其 tool_use
    return messages[idx:]


def _recent_files(
    messages: list[Message],
    max_files: int = FILES_TO_RESTORE,
    file_budget_tokens: int = FILE_BUDGET_TOKENS,
) -> list[tuple[str, str]]:
    """从被摘要的早期历史提取最近访问文件（路径+内容），最多 `max_files` 个。

    内容来自 ReadFile 的 tool_result（带行号前缀，与历史一致）；每个 ≤ `file_budget`，
    供压缩后重附（01 §5.4「最近访问的文件」）。
    """
    name_by_id: dict[str, str] = {}
    path_by_id: dict[str, str] = {}
    for msg in messages:
        for block in msg.content:
            if isinstance(block, ToolUseBlock):
                name_by_id[block.id] = block.name
                if block.name == "ReadFile":
                    path_by_id[block.id] = str(block.input.get("path", ""))
    content_by_id: dict[str, str] = {}
    for msg in messages:
        for block in msg.content:
            if (
                isinstance(block, ToolResultBlock)
                and name_by_id.get(block.tool_use_id) == "ReadFile"
            ):
                content_by_id[block.tool_use_id] = block.content
    limit = file_budget_tokens * _CHARS_PER_TOKEN
    files: list[tuple[str, str]] = []
    seen: set[str] = set()
    for msg in reversed(messages):
        for block in msg.content:
            if isinstance(block, ToolUseBlock) and block.name == "ReadFile":
                path = path_by_id.get(block.id, "")
                if path and path not in seen:
                    seen.add(path)
                    files.append((path, content_by_id.get(block.id, "")[:limit]))
                    if len(files) >= max_files:
                        return files
    return files


_L3_INSTRUCTION = """\
请对以上对话历史做结构化压缩，生成 <summary>。要求：
1. 按 9 个部分组织：
   ①主要请求和意图
   ②关键技术概念
   ③文件和代码段（关键代码片段原样保留）
   ④错误和修复
   ⑤问题解决过程
   ⑥所有用户消息（原文逐条保留，不得改写）
   ⑦待办任务
   ⑧当前工作（最详细）
   ⑨可能的下一步
2. 两阶段生成：先输出 <analysis> 草稿（梳理信息结构、识别冗余与关键锚点），再输出 <summary> 正文。
3. <summary> 为纯文本，禁止代码块与标题；只输出摘要正文。
4. 禁止调用任何工具（工具调用会被拒绝），只输出纯文本。"""


def _build_summary_message(
    summary: str,
    *,
    session_path: str,
    files: list[tuple[str, str]] | None = None,
    skills: tuple[str, ...] = (),
    todos: list[TodoItemRecord] | None = None,
) -> Message:
    """摘要 + 会话路径 + 恢复快照拼成同一条 user 消息（01 §5.4，用 `---` 分隔）。"""
    parts = [
        "<context-summary>",
        summary,
        "",
        "--- 会话记录 ---",
        f"完整历史已归档：{session_path}",
    ]
    if files:
        parts += ["", "--- 最近访问文件快照 ---"]
        parts += [f"{path}\n{content}" for path, content in files]
    if skills:
        parts += ["", "--- 已激活 Skill ---", *skills]
    todo_text = render_todo_snapshot(todos)
    if todo_text:
        parts += ["", "--- 当前 todo 快照 ---", todo_text]
    parts.append("</context-summary>")
    return Message(role="user", content=[TextBlock("\n".join(parts))])


class Compactor:
    """L3 Auto-Compact 执行器（01 §8）：9 部分摘要 + 近期原文 + 恢复快照。

    复用主对话 provider（`llm`）与主 payload 前缀（`prefix`）命中前缀缓存；
    摘要调用保留 tools 参数（缓存前缀一致），但 Prompt 两头堵禁令 + 只读文本。
    """

    def __init__(
        self,
        llm: LLMClient,
        *,
        system_prompt: str = "",
        window_size: int = WINDOW_SIZE,
        summary_output_reserve: int = SUMMARY_OUTPUT_RESERVE,
        safety_margin: int = SAFETY_MARGIN,
        force_extra_margin: int = FORCE_EXTRA_MARGIN,
        recent_keep_tokens: int = RECENT_KEEP_TOKENS,
        recent_keep_min_messages: int = RECENT_KEEP_MIN_MESSAGES,
        files_to_restore: int = FILES_TO_RESTORE,
        file_budget_tokens: int = FILE_BUDGET_TOKENS,
        summary_max_tokens: int = L3_SUMMARY_MAX_TOKENS,
    ) -> None:
        self._llm = llm
        self._system_prompt = system_prompt
        self._auto_trigger = window_size - summary_output_reserve - safety_margin
        self._force_line = window_size - summary_output_reserve - force_extra_margin
        self._recent_keep_tokens = recent_keep_tokens
        self._recent_keep_min_messages = recent_keep_min_messages
        self._files_to_restore = files_to_restore
        self._file_budget_tokens = file_budget_tokens
        self._summary_max_tokens = summary_max_tokens

    @property
    def auto_compact_trigger(self) -> int:
        """自动压缩线（200K → 167K）。"""
        return self._auto_trigger

    @property
    def force_compact_line(self) -> int:
        """强制压缩线（200K → 177K），无视熔断。"""
        return self._force_line

    def should_compact(self, tokens: int) -> bool:
        """是否达到自动压缩阈值（01 §8 Compactor.should_compact）。"""
        return tokens >= self._auto_trigger

    async def compact(
        self,
        conversation: ConversationManager,
        *,
        session_path: str = "",
        todos: list[TodoItemRecord] | None = None,
        recent_files: list[tuple[str, str]] | None = None,
        skills: tuple[str, ...] = (),
        prefix: Payload | None = None,
        focus: str = "",
    ) -> CompactResult:
        """L3 全量压缩（01 §5.4）：摘要早期历史 + 保留近期原文 + 恢复快照重灌。

        `focus`：手动 /compact 带参时指定保留重点（05 §3.6），注入摘要指令让
        模型优先保留；自动压缩不传（""）。不直接修改 conversation——返回
        `CompactResult`，由调用方（ContextManager）`restore([summary_message, *kept_recent])` 生效。
        """
        messages = conversation.messages
        kept = _recent_keep(
            messages, self._recent_keep_tokens, self._recent_keep_min_messages
        )
        summarize_src = messages[: max(0, len(messages) - len(kept))]
        if not summarize_src:
            return CompactResult(
                summary_message=_build_summary_message(
                    "(上下文未超过可压缩阈值，保持原文)", session_path=session_path
                ),
                kept_recent=messages,
                session_path=session_path,
            )
        text = await self._call_summary(summarize_src, prefix, focus)
        summary = _two_phase_extract(text) or "(Auto-Compact 摘要为空)"
        files = (
            list(recent_files)
            if recent_files is not None
            else _recent_files(summarize_src, self._files_to_restore, self._file_budget_tokens)
        )
        summary_message = _build_summary_message(
            summary, session_path=session_path, files=files, skills=skills, todos=todos
        )
        return CompactResult(
            summary_message=summary_message, kept_recent=kept, session_path=session_path
        )

    async def _call_summary(
        self, summarize_src: list[Message], prefix: Payload | None, focus: str = ""
    ) -> str:
        """摘要生成（§6 摘要请求超长兜底）：失败丢最旧组重试（最多 3 次），仍不行丢 20% 再试。"""
        src = summarize_src
        for attempt in range(SUMMARY_RETRY_LIMIT):
            try:
                text, _usage = await _llm_text(
                    self._llm, self._build_summary_payload(src, prefix, focus)
                )
                return text
            except Exception:
                if attempt == SUMMARY_RETRY_LIMIT - 1:
                    raise
                # 丢最旧 1 组；仍超长再丢 20% 消息组（01 §9.1 摘要超长兜底）
                src = src[1:] if attempt < 2 else src[max(1, len(src) // 5) :]
        raise RuntimeError("摘要生成连续失败")

    def _build_summary_payload(
        self, summarize_src: list[Message], prefix: Payload | None, focus: str = ""
    ) -> Payload:
        """摘要载荷：主前缀（命中缓存）+ 早期历史 + 压缩指令；独立调用用 system_prompt 兜底。

        `focus`（手动 /compact 带参）：追加"保留重点"指令，其余与自动压缩逐字节一致。
        """
        instruction_text = _L3_INSTRUCTION
        if focus:
            instruction_text += f"\n\n用户指定本次压缩保留重点：{focus}"
        instruction = Message(role="user", content=[TextBlock(instruction_text)])
        if prefix is not None:
            return Payload(
                system=prefix.system,
                messages=[*summarize_src, instruction],
                tools=prefix.tools,
                max_tokens=self._summary_max_tokens,
            )
        return Payload(
            system=self._system_prompt,
            messages=[*summarize_src, instruction],
            max_tokens=self._summary_max_tokens,
        )
