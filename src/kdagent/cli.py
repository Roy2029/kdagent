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
from kdagent.config import load_api_key, load_config
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import LLMClient, LLMStreamEvent, Payload, ProviderConfig
from kdagent.engine.llm.openai import OpenAICompatClient
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
    """组装真实依赖：DeepSeek（OpenAI 兼容 adapter）+ 7 工具 + 会话目录。"""
    config = load_config()
    work_dir = (work_dir or Path.cwd()).resolve()
    api_key = load_api_key()
    if api_key:
        llm: LLMClient = OpenAICompatClient(
            ProviderConfig(
                protocol="openai",
                model=config.model or "deepseek-chat",
                base_url="https://api.deepseek.com/v1",
                api_key=api_key,
            )
        )
    else:
        llm = _MissingKeyClient()
    return KDApp(
        config=config,
        llm=llm,
        conversation=ConversationManager(),
        tools=build_default_registry(),
        work_dir=work_dir,
        sessions_dir=work_dir / ".kdagent" / "sessions",
    )


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    work_dir = Path(args.dir) if args.dir else None
    build_kdapp(work_dir=work_dir).run()


if __name__ == "__main__":
    main(sys.argv[1:])
