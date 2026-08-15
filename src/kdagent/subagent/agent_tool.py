"""Agent 工具：Agent ≈ Tool（规格 10 §3.1）。

主 Agent 眼里 `Agent` 和 `ReadFile` 没有区别——子任务的中间过程在独立上下文里，
不污染主上下文。统一一个 Agent 工具（`subagent_type` 选型）而非每类型一个：
Agent 类型动态加载（用户新建定义文件即用），工具列表保持稳定。

两种创建模式（10 §3.2）：
- 定义式：`subagent_type: "explore"` → 空白对话（只装任务），前台同步 / 可后台
- Fork 式：不指定 `subagent_type` → 继承父完整对话历史 + Fork Boilerplate，
  无条件后台异步；再嵌套被第 1 层全局禁拦截（Fork 子 Agent 看不到 Agent 工具）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kdagent.engine.conversation import ConversationManager
from kdagent.subagent.manager import AgentManager
from kdagent.subagent.model import AgentDef
from kdagent.subagent.named import NamedAgentError, NamedAgentManager
from kdagent.subagent.runner import FORK_SYSTEM_PROMPT, SubAgentResult, SubAgentRunner
from kdagent.subagent.task import BackgroundTask, TaskManager
from kdagent.subagent.worktree import Worktree, WorktreeError, WorktreeManager
from kdagent.tools.base import ToolContext, ToolResult

# Fork 定义（不落盘、运行时构造）：继承对话 + Boilerplate 覆盖父默认行为。
_FORK_DEF = AgentDef(
    name="fork",
    description="继承父对话的临时工作进程（无条件后台）",
    system_prompt=FORK_SYSTEM_PROMPT,
    max_turns=20,
    permission_mode="dontAsk",
)

# 上下文通知（10 §3.12）：告诉子 Agent 三件事——继承了父对话；当前在独立
# Worktree；父传路径指向主目录、需翻译成本地路径并重新读文件（否则它读到
# worktree 文件却按主目录版本来理解，产生认知偏差）。
_WORKTREE_NOTICE = (
    "\n[note] 你在独立 Git Worktree 中工作（隔离目录，不碰主工作区）。\n"
    "1. 所有相对路径基于该 Worktree 目录。\n"
    "2. 父对话传入的路径指向主目录——需翻译成本地路径并重新读文件。\n"
    "3. 你的改动不会影响主目录；完成后有变更则保留供 review。"
)


class Agent:
    """统一子 Agent 工具：委派任务给独立上下文的子 Agent（10 §3.1）。"""

    name = "Agent"
    description = (
        "把子任务委派给独立上下文的子 Agent，返回其结果文本。"
        "何时使用：任务需要隔离上下文（避免污染主对话）、并行分发、或复用预定义专家类型。"
        "何时不使用：简单操作直接自己做；需要主对话状态的任务不要委派。"
        "参数约束：subagent_type 可选（留空 = Fork 继承当前对话的无条件后台助手）；"
        "run_in_background=true 时立即返回 task id（用 TaskList/TaskGet 查结果）。"
        "可用 Agent 类型会随可用清单动态变化，选择标准见各类型 description。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "子任务描述"},
            "description": {
                "type": "string",
                "description": "给模型的决策信息：此子任务要做什么、为何委派",
            },
            "subagent_type": {
                "type": "string",
                "description": "预定义 Agent 类型；留空 = Fork（继承当前对话历史的临时助手）",
            },
            "model": {
                "type": "string",
                "description": "覆盖定义文件的模型（独立上下文换模型不破坏主缓存）",
            },
            "run_in_background": {
                "type": "boolean",
                "description": "true = 后台异步执行，立即返回 task id",
            },
            "name": {
                "type": "string",
                "description": (
                    "命名该 Agent 以便 SendMessage 引用（M5-d 接线；给 name 即注册"
                    "命名 Agent，存活到会话结束，可反复投递新任务）"
                ),
            },
            "isolation": {"type": "string", "description": "worktree（独立工作目录，M5-b 落地）"},
        },
        "required": ["prompt", "description"],
    }
    category = "system"
    require_confirm = True  # 子任务可能写文件；主 Agent 经权限裁决授权

    def __init__(
        self,
        runner: SubAgentRunner,
        manager: AgentManager,
        task_manager: TaskManager,
        worktree_manager: WorktreeManager | None = None,
        named_manager: NamedAgentManager | None = None,
    ) -> None:
        self._runner = runner
        self._manager = manager
        self._task_manager = task_manager
        self._worktree_manager = worktree_manager
        self._named_manager = named_manager
        self._parent_conversation: ConversationManager | None = None

    def set_parent_conversation(self, conversation: ConversationManager) -> None:
        """延迟绑定父对话（KDApp 构造 Agent 后注入）：Fork 继承源 + 后台通知目标。"""
        self._parent_conversation = conversation
        self._task_manager.set_parent_conversation(conversation)

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return True

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return False

    def validate_input(self, input: dict[str, Any]) -> list[str]:
        errors = []
        for field_name in ("prompt", "description"):
            if not isinstance(input.get(field_name), str) or not input[field_name].strip():
                errors.append(f"{field_name} 必填且非空")
        return errors

    async def execute(self, ctx: ToolContext, input: dict[str, Any]) -> ToolResult:
        prompt = str(input.get("prompt", "")).strip()
        subagent_type = input.get("subagent_type")
        model = str(input.get("model", "") or "")
        background = bool(input.get("run_in_background", False))
        isolation = str(input.get("isolation", "") or "")
        name = str(input.get("name", "") or "")
        start = time.perf_counter()

        # 命名 Agent（M5-d）：给 name 即注册，存活到会话结束，SendMessage 可反复投递。
        if name:
            return await self._execute_named(
                ctx, name, prompt, subagent_type=subagent_type, model=model,
                isolation=isolation,
            )

        # 不指定 subagent_type → Fork（10 §3.2）：继承父对话，无条件后台。
        if not subagent_type:
            if self._parent_conversation is None:
                return ToolResult(
                    tool_use_id=ctx.tool_use_id,
                    name=self.name,
                    content="Fork 模式不可用：父对话未接线（无主 Agent 上下文可继承）",
                    is_error=True,
                )
            task = self._task_manager.launch(_FORK_DEF, prompt, model=model)
            return self._started_result(ctx, task.id, "fork", background=True)
        type_name = str(subagent_type)
        if not self._manager.validate_type(type_name):
            return ToolResult(
                tool_use_id=ctx.tool_use_id,
                name=self.name,
                content=(
                    f"未知 Agent 类型：{type_name}\n"
                    f"可用类型：\n{self._manager.types_markdown()}"
                ),
                is_error=True,
            )
        definition = self._manager.get(type_name)
        assert definition is not None

        if isolation == "worktree":
            return await self._execute_worktree(
                ctx, definition, prompt, model=model, background=background, start=start
            )
        if isolation:
            # 未知 isolation 值：安全默认回共享目录（不阻断委派），提示即可。
            prompt = prompt + "\n[note] isolation 值未知，本次在共享目录执行。"

        if background:
            task = self._task_manager.launch(definition, prompt, model=model)
            return self._started_result(ctx, task.id, type_name, background=True)
        # 前台：超时/主 Agent 取消 → adopt 转后台继续（10 §3.7 ②③，adoptRunning D79）。
        result, adopted_id = await self._foreground_or_adopt(definition, prompt, model=model)
        if adopted_id:
            return self._started_result(ctx, adopted_id, type_name, background=True)
        assert result is not None  # adopted_id 为空 = 前台正常完成，result 必有值
        duration_ms = int((time.perf_counter() - start) * 1000)
        if result.is_error:
            content = f"[Agent {type_name} 失败] {result.error or result.text}"
        elif result.text:
            content = f"[Agent {type_name} 完成，{result.turns} 轮]\n{result.text}"
        else:
            content = f"[Agent {type_name} 返回空结果]"
        return ToolResult(
            tool_use_id=ctx.tool_use_id,
            name=self.name,
            content=content,
            is_error=result.is_error,
            duration_ms=duration_ms,
        )

    async def _foreground_or_adopt(
        self,
        definition: AgentDef,
        prompt: str,
        *,
        model: str,
        work_dir: Path | None = None,
        on_complete: Callable[[BackgroundTask], None] | None = None,
    ) -> tuple[SubAgentResult | None, str]:
        """前台跑 run_to_completion；超时或主 Agent 取消 → adopt 转后台（10 §3.7 ②③）。

        返回 `(result, "")` = 前台正常完成；`(None, task_id)` = 已转后台继续跑，
        调用方据此返回「已后台启动 + task id」结果。adopt 不取消任务——实例/事件流/
        部分结果无损移交 TaskManager 继续消费，完成时 `<task-notification>` 注入主对话。
        """
        timeout_s = self._task_manager.get_auto_background_ms() / 1000.0
        fg = asyncio.create_task(
            self._runner.run_to_completion(definition, prompt, model=model, work_dir=work_dir)
        )
        try:
            done, _ = await asyncio.wait({fg}, timeout=timeout_s)
        except asyncio.CancelledError:
            # ③ 主 Agent 取消（用户 Esc）：子 Agent 不杀掉，转后台继续。
            self._task_manager.adopt(
                fg, definition, prompt, work_dir=work_dir, on_complete=on_complete
            )
            raise
        if fg in done:
            return fg.result(), ""
        # ② 前台超时：转后台继续，立即返回 task id 供 TaskList/TaskGet 查询。
        adopted = self._task_manager.adopt(
            fg, definition, prompt, work_dir=work_dir, on_complete=on_complete
        )
        return None, adopted.id

    async def _execute_worktree(
        self,
        ctx: ToolContext,
        definition: AgentDef,
        prompt: str,
        *,
        model: str,
        background: bool,
        start: float,
    ) -> ToolResult:
        """§3.12 execute_with_worktree：创建 → 注入通知 → 子 Agent 独立目录跑 → 自动清理。

        后台模式（M5-c）：创建后交 TaskManager 后台执行，`on_complete` 钩子在任务
        终态清理——有变更保留并把保留信息追加进任务结果（经 TaskGet/通知可见）。
        """
        wm = self._worktree_manager
        if wm is None:
            return ToolResult(
                tool_use_id=ctx.tool_use_id,
                name=self.name,
                content="worktree 隔离不可用：WorktreeManager 未接线",
                is_error=True,
            )
        wt_name = "agent-" + uuid.uuid4().hex[:8]
        try:
            wt = wm.create(wt_name, "HEAD")
        except WorktreeError as exc:
            return ToolResult(
                tool_use_id=ctx.tool_use_id,
                name=self.name,
                content=f"worktree 创建失败：{exc}",
                is_error=True,
            )
        if background:
            return self._start_worktree_background(
                ctx, definition, prompt, model=model, wt=wt, wt_name=wt_name
            )
        # 前台：超时/取消转后台（adoptRunning D79），后台路径同样经 _cleanup 清理 worktree。
        def _cleanup(bt: BackgroundTask) -> None:
            try:
                kept = wm.auto_cleanup(wt_name)
            except WorktreeError:
                kept = True  # 清理失败保守保留
            if kept:
                bt.result = (bt.result or "") + (
                    f"\n[Worktree 保留于 {wt.path}，分支 {wt.branch}]"
                )

        result, adopted_id = await self._foreground_or_adopt(
            definition,
            _WORKTREE_NOTICE + "\n\n" + prompt,
            model=model,
            work_dir=Path(wt.path),
            on_complete=_cleanup,
        )
        if adopted_id:
            return ToolResult(
                tool_use_id=ctx.tool_use_id,
                name=self.name,
                content=(
                    f"[Agent {definition.name} 已后台启动（独立 worktree），task id={adopted_id}]\n"
                    f"worktree：{wt.path}（分支 {wt.branch}）\n"
                    "用 TaskList/TaskGet 查询状态与结果；完成后有变更则保留供 review。"
                ),
            )
        assert result is not None  # adopted_id 为空 = 前台正常完成，result 必有值
        duration_ms = int((time.perf_counter() - start) * 1000)
        kept = False
        try:
            kept = wm.auto_cleanup(wt_name)
        except WorktreeError:
            kept = True  # 清理失败保守保留，供主 Agent 处理
        suffix = (
            f"\n[Worktree 保留于 {wt.path}，分支 {wt.branch}]"
            if kept
            else f"\n[Worktree {wt_name} 无变更，已自动清理]"
        )
        if result.is_error:
            content = f"[Agent {definition.name} 失败] {result.error or result.text}{suffix}"
        elif result.text:
            content = f"[Agent {definition.name} 完成，{result.turns} 轮]{suffix}\n{result.text}"
        else:
            content = f"[Agent {definition.name} 返回空结果]{suffix}"
        return ToolResult(
            tool_use_id=ctx.tool_use_id,
            name=self.name,
            content=content,
            is_error=result.is_error,
            duration_ms=duration_ms,
        )

    def _start_worktree_background(
        self,
        ctx: ToolContext,
        definition: AgentDef,
        prompt: str,
        *,
        model: str,
        wt: Worktree,
        wt_name: str,
    ) -> ToolResult:
        """后台 + worktree（M5-c）：TaskManager 后台执行，终态钩子清理 worktree。"""
        wm = self._worktree_manager
        assert wm is not None

        def _cleanup(bt: Any) -> None:
            try:
                kept = wm.auto_cleanup(wt_name)
            except WorktreeError:
                kept = True  # 清理失败保守保留
            if kept:
                bt.result = (bt.result or "") + (
                    f"\n[Worktree 保留于 {wt.path}，分支 {wt.branch}]"
                )

        task = self._task_manager.launch(
            definition,
            _WORKTREE_NOTICE + "\n\n" + prompt,
            model=model,
            work_dir=Path(wt.path),
            on_complete=_cleanup,
        )
        return ToolResult(
            tool_use_id=ctx.tool_use_id,
            name=self.name,
            content=(
                f"[Agent {definition.name} 已后台启动（独立 worktree），task id={task.id}]\n"
                f"worktree：{wt.path}（分支 {wt.branch}）\n"
                "用 TaskList/TaskGet 查询状态与结果；完成后有变更则保留供 review。"
            ),
        )

    async def _execute_named(
        self,
        ctx: ToolContext,
        name: str,
        prompt: str,
        *,
        subagent_type: Any,
        model: str,
        isolation: str,
    ) -> ToolResult:
        """命名 Agent 注册（10 §3.15 M5-d）：给 name 即注册，消息循环后台常驻。

        命名 Agent 无视 run_in_background（语义上恒为后台）；worktree 隔离时创建
        worktree 但**不自动清理**——命名 Agent 与会话同生命周期，worktree 由用户
        `/worktree` 管理。
        """
        nm = self._named_manager
        if nm is None:
            return ToolResult(
                tool_use_id=ctx.tool_use_id,
                name=self.name,
                content="命名 Agent 不可用：NamedAgentManager 未接线",
                is_error=True,
            )
        work_dir: Path | None = None
        suffix = ""
        if isolation == "worktree":
            wm = self._worktree_manager
            if wm is None:
                return ToolResult(
                    tool_use_id=ctx.tool_use_id,
                    name=self.name,
                    content="worktree 隔离不可用：WorktreeManager 未接线",
                    is_error=True,
                )
            wt_name = "agent-" + uuid.uuid4().hex[:8]
            try:
                wt = wm.create(wt_name, "HEAD")
            except WorktreeError as exc:
                return ToolResult(
                    tool_use_id=ctx.tool_use_id,
                    name=self.name,
                    content=f"worktree 创建失败：{exc}",
                    is_error=True,
                )
            work_dir = Path(wt.path)
            suffix = (
                f"\nworktree：{wt.path}（分支 {wt.branch}，命名 Agent 生命周期内保留）"
            )
        elif isolation:
            prompt += "\n[note] isolation 值未知，本次在共享目录执行。"

        try:
            if not subagent_type:
                # Fork 命名：继承父对话，SendMessage 在继承基础上继续。
                if self._parent_conversation is None:
                    return ToolResult(
                        tool_use_id=ctx.tool_use_id,
                        name=self.name,
                        content="Fork 模式不可用：父对话未接线（无主 Agent 上下文可继承）",
                        is_error=True,
                    )
                agent = nm.register(
                    _FORK_DEF,
                    name,
                    prompt,
                    fork=True,
                    parent_conversation=self._parent_conversation,
                )
            else:
                type_name = str(subagent_type)
                if not self._manager.validate_type(type_name):
                    return ToolResult(
                        tool_use_id=ctx.tool_use_id,
                        name=self.name,
                        content=(
                            f"未知 Agent 类型：{type_name}\n"
                            f"可用类型：\n{self._manager.types_markdown()}"
                        ),
                        is_error=True,
                    )
                definition = self._manager.get(type_name)
                assert definition is not None
                agent = nm.register(
                    definition, name, prompt, model=model, work_dir=work_dir
                )
        except NamedAgentError as exc:
            return ToolResult(
                tool_use_id=ctx.tool_use_id,
                name=self.name,
                content=str(exc),
                is_error=True,
            )
        return ToolResult(
            tool_use_id=ctx.tool_use_id,
            name=self.name,
            content=(
                f"[命名 Agent {name} 已注册并后台启动，type={agent.definition.name}]\n"
                f"用 SendMessage 投递新任务给 {name}（to={name}）。{suffix}"
            ),
        )

    def _started_result(
        self, ctx: ToolContext, task_id: str, type_name: str, *, background: bool
    ) -> ToolResult:
        return ToolResult(
            tool_use_id=ctx.tool_use_id,
            name=self.name,
            content=(
                f"[Agent {type_name} 已后台启动，task id={task_id}]\n"
                "用 TaskList/TaskGet 查询状态与结果；完成时通知会注入主对话。"
            ),
        )
