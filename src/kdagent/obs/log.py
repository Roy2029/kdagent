"""日志脱敏与摘要（规格 07 §3.4）。

- Prompt 默认只落**摘要**（长度 + 首尾片段），`debug.log_full_prompt: true` 才落全文——
  本地日志常含业务代码，防误导出泄露。
- 工具参数本地完整记录（调试必需）；**脱敏钩子在 exporter 出口生效**（正则规则可配置，
  如 `api_key` 值打码），默认空规则。
"""

from __future__ import annotations

import json
import re
from typing import Any

from kdagent.engine.llm.base import Payload
from kdagent.engine.messages import Message, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock

SNAPSHOT_LIMIT = 300  # prompt 摘要默认：首尾各半，超限截断
INCREMENT_BLOCK_CAP = 8000  # 增量日志单条 block 上限（防单轮 tool 输出爆炸）


def payload_text(payload: Payload) -> str:
    """payload → 纯文本（system + 消息块），供 prompt 日志（全文或摘要）。"""
    parts = [f"[system]\n{payload.system}"]
    for msg in payload.messages:
        for block in msg.content:
            part = _block_text(msg, block)
            if part:
                parts.append(part)
    return "\n\n".join(parts)


def _block_text(msg: Message, block: object) -> str:
    """单消息块 → 纯文本（role 标记 + 内容），超长截断（INCREMENT_BLOCK_CAP）。"""
    if isinstance(block, TextBlock):
        head, body = f"[{msg.role}]\n", block.text
    elif isinstance(block, ThinkingBlock):
        head, body = f"[{msg.role}:thinking]\n", block.thinking
    elif isinstance(block, ToolUseBlock):
        head, body = (
            f"[{msg.role}:tool_use:{block.name}]\n",
            json.dumps(block.input, ensure_ascii=False),
        )
    elif isinstance(block, ToolResultBlock):
        head, body = f"[{msg.role}:tool_result:{block.tool_use_id}]\n", block.content
    else:
        return ""
    if len(body) > INCREMENT_BLOCK_CAP:
        body = body[:INCREMENT_BLOCK_CAP] + f"\n…[已截断，超出 {INCREMENT_BLOCK_CAP} 字符]…"
    return head + body


def incremental_payload_text(
    payload: Payload, offset: int, *, include_system: bool = True
) -> str:
    """本轮**新增**消息（messages[offset:]）纯文本（D90 增量日志）。

    每轮 llm.call 只记「相对上一轮新加了什么」（用户输入/工具结果/助手回复/反馈），
    不再每轮记全量上下文再摘要成「共 N 字符，仅摘要」。首轮 offset=0 时
    `include_system` 把 system 以摘要形式带头（system 静态且常大，看一次即可）；
    每条 block 截断上限（INCREMENT_BLOCK_CAP），整体不摘要——增量通常小、正文可读。
    """
    parts: list[str] = []
    if include_system and offset == 0:
        parts.append(f"[system]\n{snapshot(payload.system) if payload.system else ''}")
    for msg in payload.messages[offset:]:
        for block in msg.content:
            part = _block_text(msg, block)
            if part:
                parts.append(part)
    return "\n\n".join(parts)


def snapshot(text: str, limit: int = SNAPSHOT_LIMIT) -> str:
    """超长文本 → 首尾片段 + 长度提示（零成本预览，L1 同思路）。"""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n…[共 {len(text)} 字符，仅摘要]…\n{text[-half:]}"


def make_rules(config_rules: dict[str, Any] | None) -> list[tuple[str, str]]:
    """从配置构建脱敏规则：`{正则: 替换}` 有序列表（默认空）。

    例：``{"api[_\\-]?key\\s*[=:](\\w+)": "api_key=***"}``。坏正则跳过不阻塞。
    """
    rules: list[tuple[str, str]] = []
    for pattern, replacement in (config_rules or {}).items():
        try:
            re.compile(pattern)
        except re.error:
            continue
        rules.append((pattern, str(replacement)))
    return rules


def redact(text: str, rules: list[tuple[str, str]]) -> str:
    """对一段文本应用脱敏规则。无规则时原样返回。"""
    for pattern, replacement in rules:
        text = re.sub(pattern, replacement, text)
    return text


def redact_dict(value: dict[str, Any], rules: list[tuple[str, str]]) -> dict[str, Any]:
    """对 dict 中字符串值应用脱敏；非字符串值原样。无规则时原样返回（不拷贝）。"""
    if not rules:
        return value
    out: dict[str, Any] = {}
    for k, v in value.items():
        out[k] = redact(v, rules) if isinstance(v, str) else v
    return out
