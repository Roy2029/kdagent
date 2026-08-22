"""对话管理器（规格 02 §3.3）。

写路径唯一入口：引擎层任何产生消息的动作都必须经由它，消息构造规矩只在一处维护。

M1 阶段自持 history；M2 起委托 `01` 的 ConversationHistory / ContextManager
（读路径），本模块保留写路径的语义化 add 方法。
"""

from __future__ import annotations

from kdagent.engine.messages import (
    ContentBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from kdagent.tools.base import ToolResult


class ConversationManager:
    """语义化 add 方法；内部保证结构合法（交替 / 配对 / 合并相邻同角色）。"""

    def __init__(self) -> None:
        self._messages: list[Message] = []

    @property
    def messages(self) -> list[Message]:
        return list(self._messages)

    def add_user_message(
        self, text: str, extra_blocks: list[ContentBlock] | None = None
    ) -> None:
        """用户输入 + 动态注入（system-reminder）合并为一条 user 消息。"""
        blocks: list[ContentBlock] = [TextBlock(text)]
        if extra_blocks:
            blocks.extend(extra_blocks)
        self._append(Message(role="user", content=blocks))

    def add_assistant_message(self, blocks: list[ContentBlock]) -> None:
        """模型完整回复（text + tool_use + thinking）作为一条 assistant 消息，append-only。"""
        self._append(Message(role="assistant", content=list(blocks)))

    def add_tool_results(self, results: list[ToolResult]) -> None:
        """工具结果全部挂在一条 user 消息下 append（is_error=True 也照常写入）。"""
        blocks: list[ContentBlock] = [
            ToolResultBlock(
                tool_use_id=r.tool_use_id, content=r.content, is_error=r.is_error
            )
            for r in results
        ]
        self._append(Message(role="user", content=blocks))

    def restore(self, messages: list[Message]) -> None:
        """`04` 会话恢复：整批替换历史（调用方保证消息链已映射为领域 Message）。"""
        self._messages = list(messages)

    def last_tool_use_ids(self) -> list[str]:
        """返回最近一条 assistant 消息里的 tool_use id 集合（校验配对用）。"""
        for msg in reversed(self._messages):
            if msg.role == "assistant":
                return [b.id for b in msg.content if isinstance(b, ToolUseBlock)]
        return []

    def repair_chain(self) -> None:
        """`04` 会话恢复时调用：剔除孤立 tool_result、补齐悬空 tool_use 的错误结果。

        规格 02 §3.3 要求"补缺失的 tool_result、剔除孤立块、保证交替合法"。
        悬空 tool_use（assistant 请求了工具但从未返回结果，如会话中断/取消）若不补，
        发请求时 OpenAI 兼容 API 会因 tool_call 无对应 `role=tool` 响应而拒收（HTTP 400）。

        另处理两类实测损坏（2026-08 用户核验发现，长对话/resume 触发）：
        - 同 id 重复：assistant 内重复 tool_call_id、user 内重复 tool_use_id
          （含跨消息重复）——流式响应厂商偶发重复 → 保第一个。
        - 孤立 tool_result：tool_use_id 无对应 assistant tool_call → 剔除。
        """
        # 1) 全量 tool_use id（孤立 tool_result 判定基准）
        tool_use_ids = {
            block.id
            for msg in self._messages
            for block in msg.content
            if isinstance(block, ToolUseBlock)
        }
        seen_tool_use: set[str] = set()
        seen_tool_result: set[str] = set()
        repaired: list[Message] = []
        for msg in self._messages:
            blocks: list[ContentBlock] = []
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    if block.id in seen_tool_use:
                        continue  # 重复 tool_call → 保第一个
                    seen_tool_use.add(block.id)
                    blocks.append(block)
                elif isinstance(block, ToolResultBlock):
                    if block.tool_use_id not in tool_use_ids:
                        continue  # 孤立 tool_result → 剔除
                    if block.tool_use_id in seen_tool_result:
                        continue  # 重复 tool_result（含跨消息）→ 保第一个
                    seen_tool_result.add(block.tool_use_id)
                    blocks.append(block)
                else:
                    blocks.append(block)
            if blocks:
                repaired.append(Message(role=msg.role, content=blocks))
        self._messages = repaired
        # 2) 悬空 tool_use → 补一条 errorResult（合并进最后一条 user 消息或追加一条）
        missing = seen_tool_use - seen_tool_result
        if missing:
            blocks = [
                ToolResultBlock(
                    tool_use_id=bid,
                    content="[system-reminder] 该工具调用在上次会话中断，未实际执行，请重新评估",
                    is_error=True,
                )
                for bid in sorted(missing)
            ]
            self._append(Message(role="user", content=blocks))

    def _append(self, msg: Message) -> None:
        """交替规则兜底：相邻同角色自动合并（Anthropic API 自动合并；OpenAI 需自行合并）。"""
        if self._messages and self._messages[-1].role == msg.role:
            last = self._messages[-1]
            self._messages[-1] = Message(
                role=last.role, content=[*last.content, *msg.content]
            )
        else:
            self._messages.append(msg)
