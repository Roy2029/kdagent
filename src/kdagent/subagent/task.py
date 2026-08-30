"""TaskManager：后台任务生命周期（规格 10 §3.7）。

后台运行模式四种进入路径（10 §3.7）：① 调用传 run_in_background: true；
② 前台超 120s 自动切换（get_auto_background_ms）；③ 用户 Esc 手动切换；
④ Fork 无条件后台。M5-a 落地 ① 与 ④（Fork），②③ 留 M5 后续。

TaskManager.launch 在后台协程跑 run_to_completion，完成推 notify 回调 → 主对话
注入 `<task-notification>`（不打断当前对话）。

Task 工具（4 个内置）：TaskList / TaskGet / TaskCreate（给 Hook 用）/ TaskUpdate。
不给后台任务做 slash command 栈（10 §3.7）：用户问「后台任务咋样了」→ 主 Agent
自己用 TaskList/TaskGet 查 → 自然语言回答；`/tasks` 命令仅作便捷列表。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import Usage
from kdagent.engine.messages import TextBlock
from kdagent.obs.telemetry import Telemetry
from kdagent.subagent.manager import AgentManager
from kdagent.subagent.model import AgentDef
from kdagent.subagent.runner import SubAgentResult, SubAgentRunner
from kdagent.tools.base import ToolContext, ToolResult

TaskStatus = Literal["running", "completed", "failed"]


@dataclass(slots=True)
class BackgroundTask:
    """一个后台子任务（10 §3.7 BackgroundTask）。"""

    id: str
    definition: AgentDef
    task: str
    status: TaskStatus = "running"
    result: str = ""
    is_error: bool = False
    usage: Usage = field(default_factory=Usage)
    turns: int = 0
    start_time: float = 0.0
    end_time: float = 0.0
    cancel: Callable[[], None] = lambda: None
    work_dir: str | None = None  # worktree 隔离时子 Agent 的 cwd（10 §3.12，M5-c）
    on_complete: Callable[[BackgroundTask], None] | None = None  # 完成/失败/取消后回调

    @property
    def duration_s(self) -> float:
        end = self.end_time or time.perf_counter()
        return max(0.0, end - self.start_time)

    def summary(self) -> str:
        """列表行：`id [status] type 耗时s`。"""
        return f"- {self.id} [{self.status}] {self.definition.name} 耗时 {self.duration_s:.1f}s"


class TaskManager:
    """后台任务注册表 + 调度。launch 后台跑 run_to_completion，完成回调注入主对话。"""

    def __init__(
        self,
        runner: SubAgentRunner,
        *,
        parent_conversation: ConversationManager | None = None,
        auto_background_ms: int = 120_000,
    ) -> None:
        self._runner = runner
        self._parent_conversation = parent_conversation
        self._tasks: dict[str, BackgroundTask] = {}
        self._counter = 0
        # 10 §3.7 ②（D79）：前台 Agent 工具超时自动转后台阈值 ms，cli 传 config 值。
        self._auto_background_ms = auto_background_ms

    def set_parent_conversation(self, conversation: ConversationManager) -> None:
        """延迟绑定主对话（KDApp 构造 Agent 后注入）：完成通知的注入目标。"""
        self._parent_conversation = conversation

    def set_telemetry(self, telemetry: Telemetry) -> None:
        """延迟注入 telemetry（KDApp 装配后）：转发 runner——子 Agent trace 挂父链。"""
        self._runner.set_telemetry(telemetry)

    def get_auto_background_ms(self) -> int:
        """前台 Agent 工具超时自动转后台阈值 ms（10 §3.7 ②）。"""
        return self._auto_background_ms

    def _finalize(
        self,
        bt: BackgroundTask,
        result: SubAgentResult | None,
        *,
        cancelled: bool = False,
        error: str = "",
    ) -> None:
        """任务终态统一收尾：填状态/结果 + 清理钩子 + 注入通知（launch/adopt 共用）。"""
        if result is not None:
            bt.status = "completed" if not result.is_error else "failed"
            bt.result = result.error or result.text
            bt.is_error = result.is_error
            bt.usage = result.usage
            bt.turns = result.turns
        else:
            bt.status = "failed"
            bt.result = error or "（任务被取消）"
            bt.is_error = True
        bt.end_time = time.perf_counter()
        if bt.on_complete is not None:
            with contextlib.suppress(Exception):
                bt.on_complete(bt)  # 清理钩子失败不阻断任务终态
        self._notify(bt)

    def launch(
        self,
        definition: AgentDef,
        task: str,
        *,
        model: str = "",
        background: bool = True,
        work_dir: Path | None = None,
        on_complete: Callable[[BackgroundTask], None] | None = None,
    ) -> BackgroundTask:
        """在后台协程跑 RunToCompletion；立即返回 task 供轮询。

        `cancel` 绑定到 asyncio.Task.cancel——用户/主 Agent 可随时中断。
        `work_dir` 覆盖子 Agent cwd（worktree 隔离，10 §3.12 M5-c）；`on_complete`
        在任务终态（完成/失败/取消）回调——Worktree 清理钩子，可往 bt.result 追加信息。
        """
        self._counter += 1
        task_id = f"task-{self._counter}"
        bt = BackgroundTask(
            id=task_id,
            definition=definition,
            task=task,
            start_time=time.perf_counter(),
            work_dir=str(work_dir) if work_dir is not None else None,
            on_complete=on_complete,
        )
        self._tasks[task_id] = bt

        started = False  # 哨兵：协程体是否已开始执行（取消竞态判定基准）

        async def _run() -> None:
            nonlocal started
            started = True
            try:
                result: SubAgentResult = await self._runner.run_to_completion(
                    definition,
                    task,
                    model=model,
                    background=background,
                    work_dir=Path(bt.work_dir) if bt.work_dir else None,
                )
                self._finalize(bt, result)
            except asyncio.CancelledError:
                self._finalize(bt, None, cancelled=True)
            except Exception as exc:
                self._finalize(bt, None, error=f"执行异常：{exc}")

        runner_task = asyncio.create_task(_run())

        def _cancel() -> None:
            runner_task.cancel()
            if not started:
                # 协程体尚未开始即被取消：_run 的 finally 不会执行（M5-c 竞态），
                # 同步补终态 + 钩子 + 通知，避免 status 永久卡在 running。
                self._finalize(bt, None, cancelled=True)

        bt.cancel = _cancel
        return bt

    def adopt(
        self,
        task: asyncio.Task[SubAgentResult],
        definition: AgentDef,
        task_desc: str,
        *,
        work_dir: Path | None = None,
        on_complete: Callable[[BackgroundTask], None] | None = None,
    ) -> BackgroundTask:
        """adoptRunning（10 §3.7）：接管运行中的前台任务，不杀掉重来。

        前台 Agent 工具超时（②）/ 主 Agent 取消（③ Esc）时把运行中的
        `run_to_completion` Task 移交本管理器继续消费——任务实例/事件流/取消
        函数/部分结果天然保留（task 未取消、conversation 已产内容不丢），主对话
        不再 await，完成时经 `_notify` 注入 `<task-notification>`。
        """
        self._counter += 1
        task_id = f"task-{self._counter}"
        bt = BackgroundTask(
            id=task_id,
            definition=definition,
            task=task_desc,
            start_time=time.perf_counter(),
            work_dir=str(work_dir) if work_dir is not None else None,
            on_complete=on_complete,
        )
        self._tasks[task_id] = bt
        cancel_requested = False  # 取消请求标志：agent 吞取消时 watcher 据此标终态

        async def _watch() -> None:
            try:
                result = await task
            except asyncio.CancelledError:
                self._finalize(bt, None, cancelled=True)
                return
            except Exception as exc:
                self._finalize(bt, None, error=f"执行异常：{exc}")
                return
            if cancel_requested:
                # fg 已跑完（agent 把取消吞成正常返回，M1 停止条件 3），但用户已请求
                # 取消 → 标「任务被取消」（取消语义由 adopt 明确传达，不掩盖事实）。
                self._finalize(bt, None, cancelled=True)
                return
            self._finalize(bt, result)

        # watcher 由事件循环持引用（pending task），无需外部变量保活；取消只作用于
        # fg（task），watcher 恒为收尾者不取消（D79 坑：提前 cancel 丢终态）。
        asyncio.create_task(_watch())

        def _cancel() -> None:
            nonlocal cancel_requested
            cancel_requested = True
            task.cancel()
            # 不取消 watcher——它是唯一收尾者（D79 坑）：watcher 在首次调度前被
            # cancel 会直接 cancelled，协程体（含 _finalize）根本不执行 → 终态丢失。

        bt.cancel = _cancel
        return bt

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    def list(self) -> list[BackgroundTask]:
        """按 id 升序（创建顺序）。"""
        return [self._tasks[t] for t in sorted(self._tasks)]

    def _notify(self, task: BackgroundTask) -> None:
        """完成 → 主对话注入 `<task-notification>`（10 §3.7，不打断当前对话）。"""
        conv = self._parent_conversation
        if conv is None:
            return
        text = (
            "<task-notification>\n"
            f"后台任务 {task.id}（{task.definition.name}）已完成，耗时 {task.duration_s:.1f}s。\n"
            f"结果摘要：\n{(task.result or '（无结果）')[:500]}\n"
            "</task-notification>"
        )
        conv.add_user_message("", extra_blocks=[TextBlock(text)])


class TaskList:
    """列出后台任务（id/status/耗时/类型）。"""

    name = "TaskList"
    description = (
        "列出当前所有后台任务（状态、耗时、类型）。"
        "何时使用：主 Agent 被问「后台任务进行得怎么样」时先查清单。"
    )
    input_schema = {"type": "object", "properties": {}}
    category = "system"
    require_confirm = False

    def __init__(self, manager: TaskManager) -> None:
        self._manager = manager

    def is_read_only(self) -> bool:
        return True

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return False

    def validate_input(self, input: dict[str, Any]) -> list[str]:
        return []

    async def execute(self, ctx: ToolContext, input: dict[str, Any]) -> ToolResult:
        tasks = self._manager.list()
        if not tasks:
            content = "当前无后台任务"
        else:
            content = f"后台任务 {len(tasks)} 个：\n" + "\n".join(t.summary() for t in tasks)
        return ToolResult(tool_use_id=ctx.tool_use_id, name=self.name, content=content)


class TaskGet:
    """查单个后台任务的状态与结果。"""

    name = "TaskGet"
    description = (
        "查询单个后台任务的状态与结果。"
        "何时使用：TaskList 发现任务后，用 id 查它的结果文本。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "后台任务 id（如 task-1）"},
        },
        "required": ["id"],
    }
    category = "system"
    require_confirm = False

    def __init__(self, manager: TaskManager) -> None:
        self._manager = manager

    def is_read_only(self) -> bool:
        return True

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return False

    def validate_input(self, input: dict[str, Any]) -> list[str]:
        if not input.get("id"):
            return ["id 必填"]
        return []

    async def execute(self, ctx: ToolContext, input: dict[str, Any]) -> ToolResult:
        task = self._manager.get(str(input.get("id", "")))
        if task is None:
            return ToolResult(
                tool_use_id=ctx.tool_use_id,
                name=self.name,
                content=f"后台任务不存在：{input.get('id')}",
                is_error=True,
            )
        content = (
            f"任务 {task.id} [{task.status}] {task.definition.name} "
            f"耗时 {task.duration_s:.1f}s 轮次 {task.turns}\n"
            f"{task.result or '（尚无结果）'}"
        )
        return ToolResult(tool_use_id=ctx.tool_use_id, name=self.name, content=content)


class TaskCreate:
    """登记一个外部管理的后台任务条目（10 §3.7：给 Hook 用）。"""

    name = "TaskCreate"
    description = (
        "登记一个由外部（Hook/流程）管理的后台任务条目，返回 task id 供后续 TaskUpdate。"
        "何时使用：Hook 触发长任务需要可查询条目时（一般不用模型主动创建）。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "description": "任务类型标签（如 build / test）"},
            "task": {"type": "string", "description": "任务描述"},
        },
        "required": ["type", "task"],
    }
    category = "system"
    require_confirm = False

    def __init__(self, manager: TaskManager, agent_manager: AgentManager | None = None) -> None:
        self._manager = manager
        self._agent_manager = agent_manager

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return False

    def validate_input(self, input: dict[str, Any]) -> list[str]:
        errors = []
        if not input.get("type"):
            errors.append("type 必填")
        if not input.get("task"):
            errors.append("task 必填")
        return errors

    async def execute(self, ctx: ToolContext, input: dict[str, Any]) -> ToolResult:
        manager = self._manager
        manager._counter += 1
        task_id = f"task-{manager._counter}"
        type_name = str(input.get("type", ""))
        # type 若匹配已注册 Agent 类型 → 用其定义（description/system_prompt 有意义）；
        # 否则通用外部任务条目（M5-c：definition 校验落在 agent_manager.validate_type）。
        definition = AgentDef(
            name=type_name,
            description="外部登记任务",
            system_prompt="",
            max_turns=1,
        )
        if self._agent_manager is not None and self._agent_manager.validate_type(type_name):
            registered = self._agent_manager.get(type_name)
            if registered is not None:
                definition = registered
        bt = BackgroundTask(
            id=task_id,
            definition=definition,
            task=str(input.get("task", "")),
            start_time=time.perf_counter(),
        )
        manager._tasks[task_id] = bt
        return ToolResult(
            tool_use_id=ctx.tool_use_id,
            name=self.name,
            content=f"已登记后台任务 {task_id}（外部管理，用 TaskUpdate 更新状态）",
        )


class TaskUpdate:
    """更新外部登记任务的 status/result（10 §3.7：与 TaskCreate 配对）。"""

    name = "TaskUpdate"
    description = (
        "更新一个已登记后台任务的状态与结果（与 TaskCreate 配对）。"
        "何时使用：外部流程完成任务后回填状态。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "任务 id"},
            "status": {"type": "string", "enum": ["completed", "failed"], "description": "新状态"},
            "result": {"type": "string", "description": "结果文本"},
        },
        "required": ["id", "status"],
    }
    category = "system"
    require_confirm = False

    def __init__(self, manager: TaskManager) -> None:
        self._manager = manager

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return False

    def validate_input(self, input: dict[str, Any]) -> list[str]:
        errors = []
        if not input.get("id"):
            errors.append("id 必填")
        status = input.get("status")
        if status not in ("completed", "failed"):
            errors.append("status 必填且为 completed/failed")
        return errors

    async def execute(self, ctx: ToolContext, input: dict[str, Any]) -> ToolResult:
        task = self._manager.get(str(input.get("id", "")))
        if task is None:
            return ToolResult(
                tool_use_id=ctx.tool_use_id,
                name=self.name,
                content=f"后台任务不存在：{input.get('id')}",
                is_error=True,
            )
        # D95 防御：validate 已保证 status，但并发/异常输入下防 KeyError
        # （偶发并发 bug KeyError: 'status' 候选点之一）。
        task.status = input.get("status") or "completed"
        if input.get("result"):
            task.result = str(input.get("result"))
        task.end_time = time.perf_counter()
        return ToolResult(
            tool_use_id=ctx.tool_use_id,
            name=self.name,
            content=f"任务 {task.id} 已更新为 {task.status}",
        )
