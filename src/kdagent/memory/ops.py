"""LLM 输出 → 结构化操作集（JSON）的共享收集逻辑。

extractor（静默写）与 consolidator（Dreaming 治理）共用：流式收集 text_delta →
正则提取首个 JSON → json 解析；失败重试（追加小调用）；再失败安全返回空结构。
"""

from __future__ import annotations

import json
import re
from typing import Any

from kdagent.engine.llm.base import LLMClient, Payload

MAX_APPEND_CALLS = 2  # 单次调用不够时最多追加小调用次数
_JSON_RE = re.compile(r"\{.*\}", re.S)


def as_user_message(text: str) -> Any:
    from kdagent.engine.messages import Message, TextBlock

    return Message(role="user", content=[TextBlock(text)])


async def collect_ops_json(
    llm: LLMClient,
    *,
    system: str,
    messages: list[Any],
    max_tokens: int = 4096,
) -> Any:
    """流式收集 LLM 输出 → 解析 JSON；失败返回空结构（安全，不抛异常）。"""
    payload = Payload(system=system, messages=[*messages], max_tokens=max_tokens)
    buf: list[str] = []
    for _ in range(MAX_APPEND_CALLS):
        buf.clear()
        try:
            async for ev in llm.stream_chat(payload):
                if ev.type == "text_delta" and ev.text:
                    buf.append(ev.text)
            text = "".join(buf)
            m = _JSON_RE.search(text)
            if m is not None:
                parsed = json.loads(m.group(0))
                if isinstance(parsed, (dict, list)):
                    return parsed
            if not buf:  # 空输出：什么都不做
                return {"ops": []}
        except Exception:
            return {"ops": []}
        # 追加小调用：把上一次的 raw 输出作为修正输入（08 §3.4）。
        retry_msg = f"上次输出无法解析：{text}。请重新输出合法 JSON。"
        payload.messages.append(as_user_message(retry_msg))
    return {"ops": []}
