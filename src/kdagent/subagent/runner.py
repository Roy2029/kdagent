"""SubAgentRunner：子 Agent 执行（规格 10 §3.5-3.6）。

核心公式 `Agent ≈ Tool`（10 §1）：Agent 与 Tool 接口同构——有名字、有描述、
收参数、返结果。子 Agent = 一个新 Agent 实例（复用 02 ReAct Loop），独立
ConversationManager + 过滤后的 ToolRegistry，跑完返回最后文本。

RunToCompletion（10 §3.5）：任务注入 → 无工具调用即返回最后文本，与主 Loop
共用四类停止条件与事件流，仅两点差异：任务直接注入不等用户输入；权限由
permissionMode 决定（dontAsk + disallowedTools 锁死能力边界 → 全自动无弹窗）。

工具过滤四层（10 §3.6）：
  第 1 层 全局禁 ALL_AGENT_DISALLOWED_TOOLS
    （Agent 防递归 / AskUserQuestion 防阻塞 / Task* 防后台嵌套）
  第 2 层 自定义 Agent disallowedTools 额外禁止（定义式走工具过滤）
  第 3 层 后台白名单 ASYNC_AGENT_ALLOWED_TOOLS（后台 Agent 只用基础读写/搜索/Bash）
  第 4 层 Agent 定义 tools 白名单定范围 + disallowedTools 黑名单从中排除

Fork（10 §3.2）：继承父完整对话历史 + Fork Boilerplate 覆盖父默认行为；无条件
后台；再嵌套被第 1 层全局禁（Fork 子 Agent 看不到 Agent 工具）拦截。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kdagent.config import Config
from kdagent.engine.agent import Agent
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.events import (
    ErrorEvent,
    PermissionRequestEvent,
    StreamTextEvent,
    UsageEvent,
)
from kdagent.engine.llm.base import LLMClient, Usage
from kdagent.engine.messages import Message, TextBlock, ToolResultBlock, ToolUseBlock
from kdagent.permission.checker import PermissionChecker
from kdagent.subagent.model import AgentDef
from kdagent.tools.registry import ToolRegistry

# 第 1 层：全局禁（10 §3.6）。AskUserQuestion 尚未实现，预留防阻塞。
ALL_AGENT_DISALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "Agent",
        "AskUserQuestion",
        "TaskStop",
        "TaskList",
        "TaskGet",
        "TaskCreate",
        "TaskUpdate",
    }
)

# 第 3 层：后台白名单（10 §3.6）。网络工具尚未实现，留注释扩展位。
ASYNC_AGENT_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {"ReadFile", "WriteFile", "EditFile", "Glob", "Grep", "Bash"}
)

# Fork Boilerplate（10 §3.2）：注入子 Agent，覆盖父系统提示的默认行为。
FORK_BOILERPLATE = """<fork_boilerplate>
你是一个 Fork 出来的工作进程。你不是主 Agent。
1. 不能再 Fork。
2. 不要对话、不要提问、不要请求确认。
3. 直接使用工具：读文件、搜索代码、做修改。
4. 严格限制在被分配的任务范围内。
5. 最终报告 ≤500 字，以「Scope:」开头。
</fork_boilerplate>"""

FORK_SYSTEM_PROMPT = (
    "你是 KDAgent 的一个 Fork 工作进程，继承父对话上下文。"
    "按任务指令独立工作，完成后返回结果文本。"
)


@dataclass(frozen=True, slots=True)
class SubAgentResult:
    """子 Agent 运行结果（Agent 工具的返回载荷）。"""

    text: str
    usage: Usage
    is_error: bool
    turns: int
    error: str = ""  # is_error 时的错误描述（provider 异常 / ErrorEvent）


def filter_tools(
    source: ToolRegistry,
    definition: AgentDef | None = None,
    *,
    background: bool = False,
    fork: bool = False,
) -> ToolRegistry:
    """工具过滤四层（10 §3.6）→ 子 Agent 实际工具集。

    `fork=True` 跳过第 2/4 层（Fork 继承全部工具不过滤），第 1 层全局禁始终生效
    （Fork 内再 Fork 被拦截）；后台模式叠加第 3 层白名单。
    """
    filtered = ToolRegistry()
    for tool in source.all():
        name = tool.name
        # 第 1 层：全局禁（所有子 Agent，含 Fork）。
        if name in ALL_AGENT_DISALLOWED_TOOLS:
            continue
        # 第 3 层：后台白名单。
        if background and name not in ASYNC_AGENT_ALLOWED_TOOLS:
            continue
        if definition is not None and not fork:
            # 第 4 层：tools 白名单定范围（非空则只留清单内）。
            if definition.tools and name not in definition.tools:
                continue
            # 第 2/4 层：disallowedTools 黑名单从中排除。
            if name in definition.disallowed_tools:
                continue
        filtered.register(tool)
    return filtered


def build_forked_messages(parent_messages: list[Message], task: str) -> list[Message]:
    """Fork 继承实现（10 §3.2 build_forked_messages 三步）：

    1. 复制父完整对话；
    2. 把最后一条 assistant 里未完成的 tool_use 包成 placeholder tool_result
       （保证消息链交替合法，OpenAI 兼容 API 会因悬空 tool_call 拒收）；
    3. 末尾追加任务指令（含 Fork Boilerplate）为 user 消息。
    """
    messages = [Message(role=m.role, content=[*m.content]) for m in parent_messages]
    last = messages[-1] if messages else None
    if last is not None and last.role == "assistant":
        blocks: list[Any] = []
        for block in last.content:
            if isinstance(block, ToolUseBlock):
                blocks.append(
                    ToolResultBlock(
                        tool_use_id=block.id,
                        content="[system-reminder] Fork 父对话的未完成工具调用，不执行",
                        is_error=True,
                    )
                )
            else:
                blocks.append(block)
        messages[-1] = Message(role="assistant", content=blocks)
    messages.append(
        Message(
            role="user",
            content=[TextBlock(FORK_BOILERPLATE), TextBlock(f"\n\n任务：{task}")],
        )
    )
    return messages


class _SubSink:
    """子 Agent 事件收集器：文本累积 + usage + 自动批准 ask（headless 无 HITL）。

    `PermissionRequestEvent` 自动 allow：子 Agent 的能力边界已被工具过滤四层锁死，
    父 Agent 调用 Agent 工具即完成「人级授权」——子 Agent 内不再逐次弹窗。
    deny 类安全规则仍由 PermissionChecker 裁决（M3 五层，子 Agent 不绕过）。
    """

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.usage = Usage()
        self.error: str | None = None

    def __call__(self, ev: Any) -> None:
        if isinstance(ev, StreamTextEvent):
            self.text_parts.append(ev.text or "")
        elif isinstance(ev, UsageEvent):
            self.usage = ev.usage
        elif isinstance(ev, PermissionRequestEvent):
            ev.future.set_result("allow")
        elif isinstance(ev, ErrorEvent):
            self.error = ev.error


class SubAgentRunner:
    """子 Agent 工厂：构造独立 Agent 实例 + 过滤工具集，跑 RunToCompletion。"""

    def __init__(
        self,
        *,
        llm: LLMClient,
        tools: ToolRegistry,
        config: Config,
        work_dir: Path,
        permission_checker: PermissionChecker | None = None,
        make_client: Callable[[str], LLMClient] | None = None,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._config = config
        self._work_dir = work_dir
        self._permission_checker = permission_checker
        self._make_client = make_client  # model 覆盖时新建 client（换模型不破坏主缓存）

    async def run_to_completion(
        self,
        definition: AgentDef,
        task: str,
        *,
        parent_conversation: ConversationManager | None = None,
        fork: bool = False,
        model: str = "",
        background: bool = False,
        work_dir: Path | None = None,
    ) -> SubAgentResult:
        """任务注入 → 独立 Agent 跑完 → 返回最后文本（10 §3.5）。

        `fork=True` 继承父对话历史（build_forked_messages）而非空白对话，且无条件后台
        （Fork 继承全部工具但叠加第 3 层后台白名单，只用基础读写/搜索/Bash）；
        `model` 非空且 ≠ 主模型时新建 LLMClient；`background` 套后台白名单。
        `work_dir` 覆盖子 Agent 工作目录（explicit cwd 模式，10 §3.11：Worktree
        隔离时工具显式取 worktree 路径作本次调用 cwd，无全局 chdir）。
        """
        background = background or fork  # Fork 无条件后台（10 §3.2）
        registry = filter_tools(self._tools, definition, background=background, fork=fork)
        conversation = ConversationManager()
        if fork and parent_conversation is not None:
            conversation.restore(build_forked_messages(parent_conversation.messages, task))
        sink = _SubSink()
        llm = self._llm
        if model and model != self._config.model and self._make_client is not None:
            llm = self._make_client(model)
        agent = Agent(
            config=self._config,
            llm=llm,
            conversation=conversation,
            tools=registry,
            events=sink,
            work_dir=work_dir or self._work_dir,
            system_prompt=(
                definition.system_prompt or FORK_SYSTEM_PROMPT if fork
                else definition.system_prompt
            ),
            max_iterations=definition.max_turns,
            permission_checker=self._permission_checker,
            telemetry=None,
        )
        await agent.run(task if not fork else "")
        return self._extract_result(agent, conversation, sink)

    def _extract_result(
        self, agent: Agent, conversation: ConversationManager, sink: _SubSink
    ) -> SubAgentResult:
        """RunToCompletion 结果 = 最后一条 assistant 消息文本（10 §3.5 纯文本 → 完成）。"""
        text = "".join(sink.text_parts).strip()
        last_text = ""
        for msg in reversed(conversation.messages):
            if msg.role == "assistant":
                last_text = "".join(b.text for b in msg.content if isinstance(b, TextBlock)).strip()
                break
        final = last_text or text
        is_error = sink.error is not None
        turns = agent.turns if hasattr(agent, "turns") else 0
        return SubAgentResult(
            text=final,
            usage=sink.usage,
            is_error=is_error,
            turns=turns,
            error=sink.error or "",
        )
