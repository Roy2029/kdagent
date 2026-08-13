"""ReAct Loop 真实端到端（KDAAGENT_LIVE=1 才跑，规格 02 §5 能跑档 demo 冒烟）。

宽松断言：真实 DeepSeek 跑通「用户输入 → Agent 循环 → 历史落库」，不强制模型行为。
完整的「自主完成 5-6 步任务」由 M1 验收 demo 手动演示。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from kdagent.config import Config
from kdagent.engine.agent import Agent
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.events import CancelledEvent, LoopCompleteEvent, MaxIterationsReachedEvent
from kdagent.engine.llm.base import ProviderConfig
from kdagent.engine.llm.openai import OpenAICompatClient
from kdagent.tools import build_default_registry


def _load_env_key() -> str | None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None


@pytest.mark.skipif(os.getenv("KDAAGENT_LIVE") != "1", reason="KDAAGENT_LIVE=1 才跑真实调用")
async def test_deepseek_live_agent_loop(tmp_path: Path) -> None:
    api_key = _load_env_key()
    assert api_key, ".env 缺少 DEEPSEEK_API_KEY"
    config = Config()
    config.extra["max_tokens"] = 512
    llm = OpenAICompatClient(
        ProviderConfig(
            protocol="openai",
            model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
            api_key=api_key,
        )
    )
    collected: list[Any] = []
    conv = ConversationManager()
    agent = Agent(
        config=config,
        llm=llm,
        conversation=conv,
        tools=build_default_registry(),
        events=collected.append,
        work_dir=tmp_path,
    )
    await agent.run("请用一句中文自我介绍，并可调用 TodoWrite 工具记录一个两步任务计划。")
    # 正常终止：要么模型主动完成，要么触达迭代上限
    assert collected, "Agent 未产出任何事件"
    assert any(isinstance(e, (LoopCompleteEvent, MaxIterationsReachedEvent)) for e in collected)
    assert not any(isinstance(e, CancelledEvent) for e in collected)
    # 历史合法：最后一条是 assistant 回复（完整落库）
    assert conv.messages[-1].role == "assistant"
    assert [m.role for m in conv.messages]  # 交替链非空
