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
from kdagent.engine.messages import TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock

SNAPSHOT_LIMIT = 300  # prompt 摘要默认：首尾各半，超限截断


def payload_text(payload: Payload) -> str:
    """payload → 纯文本（system + 消息块），供 prompt 日志（全文或摘要）。"""
    parts = [f"[system]\n{payload.system}"]
    for msg in payload.messages:
        for block in msg.content:
            if isinstance(block, TextBlock):
                parts.append(f"[{msg.role}]\n{block.text}")
            elif isinstance(block, ThinkingBlock):
                parts.append(f"[{msg.role}:thinking]\n{block.thinking}")
            elif isinstance(block, ToolUseBlock):
                parts.append(
                    f"[{msg.role}:tool_use:{block.name}]\n"
                    f"{json.dumps(block.input, ensure_ascii=False)}"
                )
            elif isinstance(block, ToolResultBlock):
                parts.append(f"[{msg.role}:tool_result:{block.tool_use_id}]\n{block.content}")
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
