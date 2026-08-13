"""TUI 真实端到端（KDAAGENT_LIVE=1 才跑）：Textual run_test + 真实 DeepSeek。

覆盖规格 05 §5 能跑档 demo 的核心链路：TUI 输入 → Agent Loop → 事件渲染。
宽松断言：不强制模型行为，只验证 UI→Agent→LLM→UI 全链路跑通。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from kdagent.config import Config, load_api_key
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import ProviderConfig
from kdagent.engine.llm.openai import OpenAICompatClient
from kdagent.tools import build_default_registry
from kdagent.ui.app import KDApp
from kdagent.ui.chat import ChatView


@pytest.mark.skipif(os.getenv("KDAAGENT_LIVE") != "1", reason="KDAAGENT_LIVE=1 才跑真实调用")
async def test_ui_live_agent_turn(tmp_path: Path) -> None:
    api_key = load_api_key()
    assert api_key, ".env 缺少 DEEPSEEK_API_KEY"
    llm = OpenAICompatClient(
        ProviderConfig(
            protocol="openai",
            model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
            api_key=api_key,
        )
    )
    app = KDApp(
        config=Config(),
        llm=llm,
        conversation=ConversationManager(),
        tools=build_default_registry(),
        work_dir=tmp_path,
        sessions_dir=tmp_path / ".kdagent" / "sessions",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        app.send_user_message("用一句话自我介绍")
        # 等待 agent worker 完成（真实网络，带超时；先 pause 一拍让 worker 启动）
        deadline = time.time() + 60
        await pilot.pause()
        while time.time() < deadline:
            worker = app._agent_worker
            if worker is None or not worker.is_running:
                break
            await pilot.pause()
        chat = app.query_one("#chat", ChatView)
        assert any("介绍" in m or "KDAgent" in m for m in chat.messages)
        # 全链路落库：最后一条是 assistant 回复
        assert app._agent.conversation.messages[-1].role == "assistant"
