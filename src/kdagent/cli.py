"""KDAgent 命令行入口。

M0：仅 `--version`；M1-e 起接入 Textual TUI（规格 05）。
无参启动 → 组装真实依赖（DeepSeek key 缺失时 UI 仍可启动，对话时报错引导）。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from kdagent import __version__
from kdagent.compat import patch_windows_input
from kdagent.config import load_api_key, load_config
from kdagent.context.compactor import estimate_messages_tokens, estimate_tokens
from kdagent.context.context_manager import ContextManager
from kdagent.engine.agent import DEFAULT_SYSTEM_PROMPT
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import LLMClient, LLMStreamEvent, Payload, ProviderConfig
from kdagent.engine.llm.openai import OpenAICompatClient
from kdagent.hooks.engine import HookEngine
from kdagent.memory.consolidator import MemoryConsolidator
from kdagent.memory.extractor import MemoryExtractor
from kdagent.memory.store import build_memory_store
from kdagent.permission.checker import build_permission_checker
from kdagent.tools import build_default_registry
from kdagent.ui.app import KDApp


class _MissingKeyClient:
    """无 DEEPSEEK_API_KEY 时的占位 client：对话时报错引导，UI 仍可启动。"""

    async def stream_chat(self, payload: Payload) -> AsyncIterator[LLMStreamEvent]:
        async def _boom() -> None:
            raise RuntimeError("未配置 DEEPSEEK_API_KEY：请在项目根 .env 设置后重试")

        await _boom()
        yield LLMStreamEvent(type="stop")  # mypy 认为可达；运行时在 _boom 抛错


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kdagent",
        description="KDAgent - 类 Claude Code 的 Coding Agent",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"kdagent {__version__}",
    )
    parser.add_argument(
        "-d",
        "--dir",
        help="工作目录（默认当前目录）",
    )
    return parser


def build_kdapp(work_dir: Path | None = None) -> KDApp:
    """组装真实依赖：DeepSeek（OpenAI 兼容 adapter）+ 7 工具 + 会话目录 + 07 obs。"""
    config = load_config()
    work_dir = (work_dir or Path.cwd()).resolve()
    api_key = load_api_key()
    model = config.model or "deepseek-chat"
    if api_key:
        llm: LLMClient = OpenAICompatClient(
            ProviderConfig(
                protocol="openai",
                model=model,
                base_url="https://api.deepseek.com/v1",
                api_key=api_key,
            )
        )
    else:
        llm = _MissingKeyClient()
    kd_dir = work_dir / (config.kdagent_dir or ".kdagent")
    # 06 M3 可控档：五层裁决器（默认模式来自 config.permissions.mode）+ Hook 引擎
    # （config.hooks 列表）。本地规则 learn 目标 = 项目级 permissions.local.yaml。
    permission_mode = config.permissions.get("mode", "default")
    permission_checker = build_permission_checker(
        work_dir,
        mode=permission_mode if isinstance(permission_mode, str) else "default",
        kdagent_dirs=[kd_dir],
    )
    hooks = HookEngine()
    if config.hooks:
        hooks.load({"hooks": config.hooks}, source="config.yaml")
    # 08 M4 好用档：记忆（静默读注入 + 静默写提取，双门槛节流）。提取用同主对话
    # 模型；token 估算口径与 agent 一致（消息 + system）。
    memory_store = build_memory_store(work_dir, kdagent_dir=config.kdagent_dir or ".kdagent")
    memory_extractor = MemoryExtractor(
        memory_store,
        llm,
        estimate=lambda conv: estimate_messages_tokens(conv.messages)
        + estimate_tokens(DEFAULT_SYSTEM_PROMPT),
    )
    # 08 §3.6 Dreaming 治理：门控（时间/扫描/会话数）+ 锁 + 后台 LLM 整理。
    memory_consolidator = MemoryConsolidator(
        memory_store,
        llm,
        sessions_dir=kd_dir / "sessions",
    )
    return KDApp(
        config=config,
        llm=llm,
        conversation=ConversationManager(),
        tools=build_default_registry(),
        work_dir=work_dir,
        sessions_dir=kd_dir / "sessions",
        model_name=model,
        obs_dir=kd_dir / "obs",
        context_manager=ContextManager(
            kd_dir / "sessions", llm=llm, system_prompt=DEFAULT_SYSTEM_PROMPT
        ),
        permission_checker=permission_checker,
        hooks=hooks,
        memory_store=memory_store,
        memory_extractor=memory_extractor,
        memory_consolidator=memory_consolidator,
    )


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    work_dir = Path(args.dir) if args.dir else None
    # 方案 A：Windows 终端不支持 Kitty 时禁用（对齐 Claude Code 回退传统流），
    # 恢复中文 IME；非 win32 为 no-op（compat.patch_windows_input）。
    patch_windows_input()
    build_kdapp(work_dir=work_dir).run()


if __name__ == "__main__":
    main(sys.argv[1:])
