"""TodoWrite：规划工具 · 语义报站（规格 03 §3.6 / 12 T35）。

结构 todo → task → steps（每步 description + 可选 accept_criteria，12 T36）。
M1-b：规范化 + 内存快照 + 完整快照文本回传（tool_result 主通道）；
会话状态接线（SessionRecord.todos → TodoRegion 渲染）留 M1-f。
"""

from __future__ import annotations

import time
from typing import Any

from kdagent.tools.base import ToolContext, ToolResult

_PENDING = "pending"
_COMPLETED = "completed"


def normalize_todos(raw_todos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """归一化 todo 结构：剔除无效项、补默认字段，返回规范结构。

    容忍模型产生的脏输入（空 content、缺 steps、status 非法），
    执行不因单个坏项整批失败。
    """
    todos: list[dict[str, Any]] = []
    for raw in raw_todos:
        content = str(raw.get("content", "")).strip()
        if not content:
            continue
        tasks: list[dict[str, Any]] = []
        for raw_task in raw.get("tasks", []) or []:
            task_content = str(raw_task.get("content", "")).strip()
            if not task_content:
                continue
            steps: list[dict[str, str]] = []
            for raw_step in raw_task.get("steps", []) or []:
                description = str(raw_step.get("description", "")).strip()
                if not description:
                    continue
                criteria = str(raw_step.get("accept_criteria", "")).strip() or ""
                steps.append({"description": description, "accept_criteria": criteria})
            status = raw_task.get("status", _PENDING)
            if status not in (_PENDING, _COMPLETED):
                status = _PENDING
            tasks.append({"content": task_content, "status": status, "steps": steps})
        todos.append({"content": content, "tasks": tasks})
    return todos


def format_todos(todos: list[dict[str, Any]]) -> str:
    """生成完整快照文本（回传主通道；`12` 检查点据此对照判据）。"""
    lines: list[str] = []
    for todo in todos:
        lines.append(f"- {todo['content']}")
        for task in todo["tasks"]:
            # D96 防御：format_todos 是公开纯函数（checkpoints 也会传半结构数据）；
            # get 兜底防脏输入 KeyError（正常路径 normalize 已保证字段）。
            mark = "x" if task.get("status", _PENDING) == _COMPLETED else " "
            lines.append(f"  [{mark}] {task['content']}")
            for step in task["steps"]:
                criteria = str(step.get("accept_criteria", "") or "")
                suffix = f" [判据: {criteria}]" if criteria else ""
                lines.append(f"    - {step.get('description', '')}{suffix}")
    return "\n".join(lines) or "（空 todo 列表）"


class TodoWrite:
    """写入/更新 todo 列表；顺序语义，必须单写（is_concurrency_safe=False）。"""

    name = "TodoWrite"
    description = (
        "写入或更新任务规划列表。结构为 todo → task → steps：todo 是整体目标，"
        "task 是可执行子任务，step 是任务的具体步骤（每步可带可选完成判据 accept_criteria）。"
        "何时使用：接到多步任务时先规划 todo，任务推进时更新状态并回传完整列表。"
        "何时不使用：一次性小请求无需规划；不要为单一操作创建 todo。"
        "参数约束：每次调用回传全部 todo（非增量）；status 仅 pending/completed；"
        "空 content 项会被忽略。"
        "返回格式：回传完整规范化快照（含每任务状态与判据）。"
        "配合：todo 状态驱动 UI 面板与检查点（12 T35）；完成型步骤必写判据、探索型可省（12 T36）。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "完整规划列表（每次调用回传全部 todo，非增量）",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "todo 描述"},
                        "tasks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "content": {"type": "string"},
                                    "status": {
                                        "type": "string",
                                        "enum": [_PENDING, _COMPLETED],
                                        "description": "任务状态",
                                    },
                                    "steps": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "description": {"type": "string"},
                                                "accept_criteria": {
                                                    "type": "string",
                                                    "description": "可选完成判据",
                                                },
                                            },
                                            "required": ["description"],
                                        },
                                    },
                                },
                                "required": ["content"],
                            },
                        },
                    },
                    "required": ["content"],
                },
            }
        },
        "required": ["todos"],
    }
    category = "planning"
    require_confirm = False

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict[str, Any]) -> bool:
        return False

    def validate_input(self, input: dict[str, Any]) -> list[str]:
        raw_todos = input.get("todos")
        if not isinstance(raw_todos, list) or not raw_todos:
            return ["todos 必填且非空"]
        return []

    async def execute(self, ctx: ToolContext, input: dict[str, Any]) -> ToolResult:
        # D96 治理④：无状态化——每次调用用本次 input 的规范化快照，不存跨调用实例
        # 字段。此前 TodoWrite 是 cli 全局 registry 单例，filter_tools 给并发子 Agent
        # 注册同一实例引用 → 5 个并发任务共享 self._todos，轮次间隙读到别的 Agent 的
        # 计划（KeyError 'status' 候选根因，B4 实测 20% 触发）。无状态后共享实例也
        # 无状态可共享；模型每次本就回传完整 todo 列表（非增量），行为不变。
        start = time.perf_counter()
        todos = normalize_todos(input["todos"])
        if ctx.todos is not None:
            ctx.todos(todos)  # 03 §3.6 数据流：→ 04 会话状态 + 05 面板渲染
        content = format_todos(todos)
        duration_ms = int((time.perf_counter() - start) * 1000)
        return ToolResult(
            tool_use_id=ctx.tool_use_id, name=self.name, content=content, duration_ms=duration_ms
        )
