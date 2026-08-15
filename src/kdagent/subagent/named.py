"""命名 Agent 与 SendMessage 消息投递（规格 10 §3.15，M5-d）。

后台 Agent 跑完初始任务即结束（TaskManager 一次性 RunToCompletion）；SendMessage
让命名 Agent **存活到会话结束**——主 Agent 随时投递新消息/新任务唤醒继续。

生命周期：

    Agent {name, prompt, run_in_background: true}
       └─ name slug 校验（复用 worktree validate_name，防注入）→ 注册 → 队列投初始任务
       └─ NamedAgentManager._message_loop：逐条消费队列 → run_to_completion(消息)
          → 结果追加进 result_history → 队列空回到 idle（阻塞在 queue.get，不退出）
       └─ SendMessage 投递 → 唤醒 → 继续处理；无显式销毁（进程退出自然回收）

Fork 命名（无 subagent_type + name）：继承父对话 + 命名，SendMessage 给它的消息在
继承的对话基础上继续——最贴近 Agent SDK「发消息给子代理继续它」的协作语义。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import Usage
from kdagent.subagent.model import AgentDef
from kdagent.subagent.runner import SubAgentRunner
from kdagent.subagent.worktree import validate_name
from kdagent.tools.base import ToolContext, ToolResult


class NamedAgentError(Exception):
    """命名 Agent 生命周期异常（重名 / name 非法）。"""


@dataclass(slots=True)
class NamedAgent:
    """一个命名 Agent：注册 + 消息队列 + 处理循环（10 §3.15）。"""

    name: str
    definition: AgentDef
    model: str = ""
    work_dir: Path | None = None  # worktree 隔离时子 Agent 的 cwd（10 §3.12）
    fork: bool = False  # Fork 命名：继承父对话（build_forked_messages）
    parent_conversation: ConversationManager | None = None  # Fork 继承源
    status: str = "running"  # running 处理中 / idle 等消息
    result_history: str = ""  # 各条消息的处理结果追加（SendMessage/TaskGet 可见）
    messages: list[tuple[str, str]] = field(default_factory=list)  # (sender, text)
    turns: int = 0
    usage: Usage = field(default_factory=Usage)
    start_time: float = field(default_factory=time.perf_counter)
    _queue: asyncio.Queue[tuple[str, str] | None] = field(
        default_factory=asyncio.Queue, repr=False
    )
    _loop: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def alive_s(self) -> float:
        return time.perf_counter() - self.start_time

    def summary(self) -> str:
        """清单行：`name [status] 定义类型 存活s 消息N 轮次`。"""
        return (
            f"- {self.name} [{self.status}] {self.definition.name} "
            f"存活 {self.alive_s:.0f}s 消息 {len(self.messages)} 轮次 {self.turns}"
        )


def _sum_usage(a: Usage, b: Usage) -> Usage:
    """两条消息的 token 用量累加（命名 Agent 全程累计）。"""
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        cache_creation_tokens=a.cache_creation_tokens + b.cache_creation_tokens,
    )


class NamedAgentManager:
    """命名 Agent 注册表 + 消息循环（10 §3.15）。与 TaskManager 并存：普通后台任务
    一次性 RunToCompletion；命名 Agent 多消息续跑。"""

    def __init__(self, runner: SubAgentRunner) -> None:
        self._runner = runner
        self._agents: dict[str, NamedAgent] = {}

    def register(
        self,
        definition: AgentDef,
        name: str,
        prompt: str,
        *,
        model: str = "",
        work_dir: Path | None = None,
        fork: bool = False,
        parent_conversation: ConversationManager | None = None,
    ) -> NamedAgent:
        """校验 name → 注册 → 队列投初始任务 → 启动处理循环。

        Fork 命名须带 parent_conversation（继承源）；定义式命名传 None。
        """
        try:
            validate_name(name)  # 复用 slug 白名单（防 SendMessage 目标注入）
        except Exception as exc:
            raise NamedAgentError(str(exc)) from exc  # 包装成子系统错误，调用方统一 catch
        if name in self._agents:
            raise NamedAgentError(f"命名 Agent 已存在：{name}")
        agent = NamedAgent(
            name=name,
            definition=definition,
            model=model,
            work_dir=work_dir,
            fork=fork,
            parent_conversation=parent_conversation,
        )
        self._agents[name] = agent
        agent._queue.put_nowait(("main", prompt))
        agent._loop = asyncio.create_task(self._message_loop(agent))
        return agent

    async def _message_loop(self, agent: NamedAgent) -> None:
        """串行消费队列：一条消息一轮 RunToCompletion，FIFO 不并行（同一 Agent 上下文
        单线程推进）；队列空回到 idle 阻塞等待，不退出——命名 Agent 存活到会话结束。"""
        try:
            while True:
                item = await agent._queue.get()
                if item is None:
                    break  # 终止信号（M5 无显式 kill，进程退出自然回收，保留扩展点）
                sender, message = item
                agent.status = "running"
                try:
                    result = await self._runner.run_to_completion(
                        agent.definition,
                        message,
                        parent_conversation=agent.parent_conversation,
                        fork=agent.fork,
                        model=agent.model,
                        background=True,
                        work_dir=agent.work_dir,
                    )
                    agent.turns += result.turns
                    agent.usage = _sum_usage(agent.usage, result.usage)
                    preview = message if len(message) <= 200 else message[:200] + "…"
                    if result.is_error:
                        agent.result_history += (
                            f"\n— 来自 {sender}：{preview}\n✗ {result.error or result.text}"
                        )
                    else:
                        agent.result_history += f"\n— 来自 {sender}：{preview}\n→ {result.text}"
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    agent.result_history += f"\n— 来自 {sender}：处理异常 {exc}"
                agent.status = "idle"
        except asyncio.CancelledError:
            pass
        finally:
            agent._loop = None

    def send(self, to: str, message: str, *, sender: str = "main") -> bool:
        """投递消息给命名 Agent；未注册返回 False（不抛，SendMessage 工具转 is_error）。"""
        agent = self._agents.get(to)
        if agent is None:
            return False
        agent.messages.append((sender, message))
        agent._queue.put_nowait((sender, message))
        return True

    def get(self, name: str) -> NamedAgent | None:
        return self._agents.get(name)

    def list(self) -> list[NamedAgent]:
        return sorted(self._agents.values(), key=lambda a: a.start_time)


class SendMessage:
    """给命名 Agent 投递新消息/新任务（10 §3.15，M5-d）。"""

    name = "SendMessage"
    description = (
        "给命名 Agent 投递新消息/新任务，唤醒它继续处理（结果追加进该 Agent 的结果历史）。"
        "何时使用：后台子任务完成后，需要让同一 Agent 接着干活；或给 Fork 出的子代理发后续指令。"
        "参数约束：to 必须是之前 Agent 调用里传的 name（命名 Agent）；message 为新任务文本。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "命名 Agent 的 name"},
            "message": {"type": "string", "description": "新消息/新任务文本"},
            "summary": {
                "type": "string",
                "description": "消息预览（UI/通知用，默认取 message 截断）",
            },
        },
        "required": ["to", "message"],
    }
    category = "system"
    require_confirm = False

    def __init__(self, manager: NamedAgentManager) -> None:
        self._manager = manager

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return False

    def validate_input(self, input: dict[str, Any]) -> list[str]:
        errors = []
        if not input.get("to"):
            errors.append("to 必填")
        if not input.get("message"):
            errors.append("message 必填")
        return errors

    async def execute(self, ctx: ToolContext, input: dict[str, Any]) -> ToolResult:
        to = str(input.get("to", ""))
        message = str(input.get("message", ""))
        sent = self._manager.send(to, message)
        if not sent:
            names = self._manager.list()
            hint = "当前无命名 Agent" if not names else "可用：" + "、".join(a.name for a in names)
            return ToolResult(
                tool_use_id=ctx.tool_use_id,
                name=self.name,
                content=f"命名 Agent 不存在：{to}。{hint}",
                is_error=True,
            )
        agent = self._manager.get(to)
        assert agent is not None
        return ToolResult(
            tool_use_id=ctx.tool_use_id,
            name=self.name,
            content=(
                f"已投递给命名 Agent {to}（当前状态 {agent.status}，"
                f"累计消息 {len(agent.messages)} 条，处理轮次 {agent.turns}）"
            ),
        )
