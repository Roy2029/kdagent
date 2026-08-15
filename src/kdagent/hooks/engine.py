"""Hook 引擎（规格 06 §3.10）：事件 + 条件 + 动作。

三要素：什么时候触发（event）/ 什么情况下（if 条件）/ 做什么（action）。
`pre_tool_use` 是唯一能「说不」的事件——`reject: true` → 工具调用取消，
拒绝原因作为错误结果返回模型，模型调整策略（反馈循环）。

**错误兜底**：Hook 执行出错只记日志、不中断 Agent 主流程——辅助机制的故障
不能反过来搞崩核心流程（尾巴摇狗）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml

from kdagent.hooks.conditions import Condition, expand_variables, parse_condition
from kdagent.hooks.engine_types import HookContext, HookReject

ActionType = Literal["command", "prompt", "http", "agent"]

# 可控档实现集（规格 §3.10 表 ✅ 行）：11 个事件，其余（pre_send/post_receive、
# file_change/command_execute）预留。
EVENT_SET: frozenset[str] = frozenset(
    {
        "session_start",
        "session_end",
        "turn_start",
        "turn_end",
        "pre_tool_use",
        "post_tool_use",
        "permission_request",
        "startup",
        "shutdown",
        "error",
        "compact",
    }
)

# action 必填字段（规格 §3.10 配置校验）。
_ACTION_REQUIRED: dict[ActionType, tuple[str, ...]] = {
    "command": ("command",),
    "prompt": ("prompt",),
    "http": ("url",),
    "agent": (),
}


@dataclass(slots=True)
class HookConfig:
    """一条 Hook 配置（解析并校验后的形态）。"""

    id: str
    event: str
    action_type: ActionType
    action: dict[str, Any]
    condition: Condition | None = None
    once: bool = False
    async_: bool = False
    reject: bool = False
    timeout: float = 10.0


class HookEngine:
    """Hook 注册 + 匹配 + 执行。`prompt_inject` 注入提示词（Agent 侧接线为追加 user 消息）。"""

    def __init__(
        self,
        prompt_inject: Callable[[str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._hooks: list[HookConfig] = []
        self._fired: set[str] = set()  # once 钩子：本进程内只触发一次
        self._prompt_inject = prompt_inject
        self._error_cb = on_error
        self._errors: list[str] = []

    # ---- 配置 ----

    def load(self, data: list[dict[str, Any]] | dict[str, Any], *, source: str = "") -> None:
        """从 YAML 结构加载 hooks 列表（config.yaml 的 `hooks:` 节或裸列表）。"""
        items: Any = data
        if isinstance(data, dict):
            items = data.get("hooks", [])
        if not isinstance(items, list):
            raise ValueError(f"hooks 配置格式非法（{source}）：应为列表")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError(f"hook 配置条目非法（{source}）：{item}")
            self._hooks.append(self._parse_hook(item, source))

    def load_file(self, hook_file: Path) -> None:
        """从 YAML 文件加载；文件不存在按空集跳过（零配置可用）。"""
        if not hook_file.is_file():
            return
        data = yaml.safe_load(hook_file.read_text(encoding="utf-8"))
        if data is None:
            return
        self.load(data, source=str(hook_file))

    @property
    def hooks(self) -> list[HookConfig]:
        return list(self._hooks)

    @property
    def error_log(self) -> list[str]:
        """历史执行错误（诊断用）；`on_error` 回调是主动通知通道。"""
        return list(self._errors)

    # ---- 执行 ----

    def run(self, event: str, ctx: HookContext) -> None:
        """同步入口：非拦截事件。

        prompt 注入**同步执行**（生命周期提示词需在下一轮前生效）；
        command/http 后台调度（有事件循环则 ensure_future，无则同步跑完）。
        """
        ctx.event = event
        for hook in self._hooks:
            if hook.event != event or not self._matches(hook, ctx):
                continue
            if self._already_fired(hook):
                continue
            self._mark_fired(hook)
            try:
                self._dispatch(hook, ctx)
            except Exception as exc:
                self._report_error(hook, exc)

    def run_pre_tool(self, ctx: HookContext) -> HookReject | None:
        """pre_tool_use：可拦截。任一匹配 reject → 短路返回，后续 Hook 不再跑。"""
        ctx.event = "pre_tool_use"
        for hook in self._hooks:
            if hook.event != "pre_tool_use" or not self._matches(hook, ctx):
                continue
            if self._already_fired(hook):
                continue
            if hook.reject:
                return HookReject(self._reject_reason(hook, ctx))  # 短路：后续不再跑
            self._mark_fired(hook)
            try:
                self._dispatch(hook, ctx)
            except Exception as exc:
                self._report_error(hook, exc)
        return None

    async def run_async(self, event: str, ctx: HookContext) -> None:
        """异步执行全部匹配 Hook（测试与内部调度共用）。"""
        ctx.event = event
        for hook in self._hooks:
            if hook.event != event or not self._matches(hook, ctx):
                continue
            if self._already_fired(hook):
                continue
            self._mark_fired(hook)
            try:
                await self._run_action(hook, ctx)
            except Exception as exc:
                self._report_error(hook, exc)

    # ---- 内部 ----

    def _parse_hook(self, item: dict[str, Any], source: str) -> HookConfig:
        hid = item.get("id")
        event = item.get("event")
        if not isinstance(hid, str) or not hid:
            raise ValueError(f"hook 缺 id（{source}）")
        if not isinstance(event, str) or event not in EVENT_SET:
            raise ValueError(f"hook {hid}: 未知事件 {event!r}（可选：{sorted(EVENT_SET)}）")
        action = item.get("action")
        if not isinstance(action, dict) or not isinstance(action.get("type"), str):
            raise ValueError(f"hook {hid}: 缺 action.type")
        action_type = action["type"]
        if action_type not in _ACTION_REQUIRED:
            raise ValueError(f"hook {hid}: 未知 action.type {action_type!r}")
        action_type_c: ActionType = action_type
        for required in _ACTION_REQUIRED[action_type_c]:
            if not isinstance(action.get(required), str):
                raise ValueError(f"hook {hid}: {action_type_c} 执行器缺必填字段 {required}")
        # 拦截/后台约束（规格 §3.10 配置校验）。
        reject = bool(item.get("reject", False))
        if reject and event != "pre_tool_use":
            raise ValueError(f"hook {hid}: reject 仅限 pre_tool_use 事件")
        async_ = bool(item.get("async", False))
        if async_ and event == "pre_tool_use":
            raise ValueError(f"hook {hid}: async 禁用于 pre_tool_use")
        timeout = _parse_timeout(item.get("timeout"), default=10.0)
        condition = None
        if item.get("if"):
            condition = parse_condition(str(item["if"]))
        return HookConfig(
            id=hid,
            event=event,
            action_type=action_type_c,
            action=action,
            condition=condition,
            once=bool(item.get("once", False)),
            async_=async_,
            reject=reject,
            timeout=timeout,
        )

    def _matches(self, hook: HookConfig, ctx: HookContext) -> bool:
        return hook.condition is None or hook.condition.matches(ctx)

    def _reject_reason(self, hook: HookConfig, ctx: HookContext) -> str:
        """拒绝原因：action.reason 显式 → prompt 模板 → 默认（均展开变量）。"""
        explicit = hook.action.get("reason")
        if isinstance(explicit, str):
            return expand_variables(explicit, ctx)
        template = hook.action.get("prompt")
        if isinstance(template, str):
            return expand_variables(template, ctx)
        return f"被 Hook {hook.id} 拦截"

    def _already_fired(self, hook: HookConfig) -> bool:
        return hook.once and hook.id in self._fired

    def _mark_fired(self, hook: HookConfig) -> None:
        if hook.once:
            self._fired.add(hook.id)

    def _dispatch(self, hook: HookConfig, ctx: HookContext) -> None:
        """分发执行：prompt 同步注入；command/http 后台调度（async 语义）。"""
        if hook.action_type == "prompt":
            self._inject_prompt(hook, ctx)
        elif _loop_running():
            asyncio.ensure_future(self._run_action(hook, ctx))
        else:
            asyncio.run(self._run_action(hook, ctx))

    def _inject_prompt(self, hook: HookConfig, ctx: HookContext) -> None:
        message = expand_variables(str(hook.action.get("prompt", "")), ctx)
        if self._prompt_inject is not None:
            self._prompt_inject(message)

    async def _run_action(self, hook: HookConfig, ctx: HookContext) -> None:
        if hook.action_type == "prompt":
            self._inject_prompt(hook, ctx)
            return
        if hook.action_type == "command":
            command = expand_variables(str(hook.action["command"]), ctx)
            await _run_command(command, hook.timeout)
            return
        if hook.action_type == "http":
            url = expand_variables(str(hook.action["url"]), ctx)
            payload = hook.action.get("json")
            async with httpx.AsyncClient(timeout=hook.timeout) as client:
                await client.post(url, json=payload if isinstance(payload, dict) else None)
            return
        # agent 执行器：预留（依赖 10 SubAgent 运行时，仅接口骨架）。

    def _report_error(self, hook: HookConfig, exc: Exception | str) -> None:
        msg = f"Hook {hook.id} 执行出错：{exc}"
        self._errors.append(msg)
        if self._error_cb is not None:
            self._error_cb(msg)


async def _run_command(command: str, timeout: float) -> None:
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=timeout)


def _parse_timeout(raw: Any, *, default: float) -> float:
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        text = raw.strip().lower()
        try:
            if text.endswith("s"):
                return float(text[:-1])
            return float(text)
        except ValueError:
            return default
    return default


def _loop_running() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False
