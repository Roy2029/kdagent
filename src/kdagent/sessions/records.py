"""会话记录：协议无关的内部表示（规格 04 §3.2）。

关键原则：不落盘任何厂商的原始线格式。落盘的是内部表示（调用 ID / 工具名 /
参数 / 输出），恢复时由 `02` 的 adapter 翻译成当前厂商的线格式——换 provider
历史不失效。

todos 是会话级状态（非消息），随最新一条记录落盘，供 `12` 检查点时点④重灌快照。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Literal, TypeAlias

from kdagent.engine.messages import (
    ContentBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)


@dataclass(frozen=True, slots=True)
class ToolUseRecord:
    tool_use_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResultRecord:
    tool_use_id: str  # 指回 ToolUseRecord
    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class ThinkingRecord:
    content: str
    signature: str | None = None  # 带签名必须原样回传（D12）


@dataclass(frozen=True, slots=True)
class StepRecord:
    description: str
    accept_criteria: str | None = None  # 完成型必写 / 探索型可省（12 T36）


@dataclass(frozen=True, slots=True)
class TodoItemRecord:
    content: str
    status: str = "pending"  # pending / in_progress / completed
    active_form: str = ""
    steps: list[StepRecord] | None = None
    group: str = ""  # 所属 todo 目标（TodoWrite 三层 todo→task→steps 的顶层 content）


RawTodo: TypeAlias = dict[str, Any]  # TodoWrite 归一化后的三层结构（03 §3.6）


def todo_items_from_raw(raw_todos: list[RawTodo]) -> list[TodoItemRecord]:
    """`03` TodoWrite 归一化结构（todo→task→steps）→ `04` 会话级 TodoItemRecord。

    TodoItemRecord 的 content/steps 表示 **task 层**（含完成状态），group 记所属 todo
    目标——三层在落盘前压成两层，渲染时按 group 还原三层（05 §3.2b）。
    映射在 M1-f 落地（04 §3.2：TodoWrite → SessionRecord.todos）。
    """
    items: list[TodoItemRecord] = []
    for todo in raw_todos:
        group = str(todo.get("content", "")).strip()
        for task in todo.get("tasks", []) or []:#遍历任务
            steps = None
            raw_steps = task.get("steps")
            if raw_steps:#遍历任务中的步骤
                steps = [
                    StepRecord(
                        description=str(s.get("description", "")).strip(),
                        accept_criteria=str(s.get("accept_criteria", "")).strip() or None,
                    )
                    for s in raw_steps
                ]
            items.append(
                TodoItemRecord(
                    content=str(task.get("content", "")).strip(),
                    status=str(task.get("status", "pending")),
                    steps=steps,
                    group=group,
                )
            )
    return items


@dataclass(frozen=True, slots=True)
class SessionRecord:
    role: Literal["user", "assistant"]
    content: str = ""
    tool_uses: list[ToolUseRecord] | None = None
    tool_results: list[ToolResultRecord] | None = None
    thinking: ThinkingRecord | None = None  # 空字段整体省略
    todos: list[TodoItemRecord] | None = None  # 当前 todo 列表（会话级状态）
    ts: int = 0  # Unix 秒（整数，紧凑）

    @classmethod
    def from_message(cls, msg: Message, ts: int) -> SessionRecord:
        """`02` Message → SessionRecord。

        assistant: content+tool_uses+thinking；user: content+tool_results。
        """
        text_parts: list[str] = []
        tool_uses: list[ToolUseRecord] = []
        tool_results: list[ToolResultRecord] = []
        thinking: ThinkingRecord | None = None
        for block in msg.content:
            if isinstance(block, TextBlock):
                text_parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                tool_uses.append(
                    ToolUseRecord(
                        tool_use_id=block.id, tool_name=block.name, arguments=block.input
                    )
                )
            elif isinstance(block, ToolResultBlock):
                tool_results.append(
                    ToolResultRecord(
                        tool_use_id=block.tool_use_id,
                        content=block.content,
                        is_error=block.is_error,
                    )
                )
            elif isinstance(block, ThinkingBlock):
                thinking = ThinkingRecord(content=block.thinking, signature=block.signature)
        return cls(
            role=msg.role,
            content="".join(text_parts),
            tool_uses=tool_uses or None,
            tool_results=tool_results or None,
            thinking=thinking,
            ts=ts,
        )

    def to_message(self) -> Message:
        """SessionRecord → `02` Message（供恢复映射）。"""
        blocks: list[ContentBlock] = []
        if self.content:
            blocks.append(TextBlock(self.content))
        if self.thinking is not None:
            blocks.append(
                ThinkingBlock(thinking=self.thinking.content, signature=self.thinking.signature)
            )
        for tu in self.tool_uses or []:
            blocks.append(ToolUseBlock(id=tu.tool_use_id, name=tu.tool_name, input=tu.arguments))
        for tr in self.tool_results or []:
            blocks.append(
                ToolResultBlock(
                    tool_use_id=tr.tool_use_id, content=tr.content, is_error=tr.is_error
                )
            )
        return Message(role=self.role, content=blocks)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> SessionRecord:
        """逐行解析；坏行由调用方捕获跳过。"""
        return cls.from_dict(json.loads(line))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionRecord:
        tool_uses = [ToolUseRecord(**tu) for tu in data.get("tool_uses") or []]
        tool_results = [ToolResultRecord(**tr) for tr in data.get("tool_results") or []]
        thinking = ThinkingRecord(**data["thinking"]) if data.get("thinking") else None
        todos: list[TodoItemRecord] | None = None
        raw_todos = data.get("todos")
        if raw_todos:
            todos = []
            for raw in raw_todos:
                raw_steps = raw.get("steps")
                steps = (
                    [StepRecord(**s) for s in raw_steps]
                    if isinstance(raw_steps, list)
                    else None
                )
                todos.append(
                    TodoItemRecord(
                        content=raw["content"],
                        status=raw.get("status", "pending"),
                        active_form=raw.get("active_form", ""),
                        steps=steps,
                        group=raw.get("group", ""),
                    )
                )
        return cls(
            role=data["role"],
            content=data.get("content", ""),
            tool_uses=tool_uses or None,
            tool_results=tool_results or None,
            thinking=thinking,
            todos=todos,
            ts=data.get("ts", 0),
        )
