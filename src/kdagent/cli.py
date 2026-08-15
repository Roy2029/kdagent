"""KDAgent 命令行入口。

M0：仅 `--version`；M1-e 起接入 Textual TUI（规格 05）。
无参启动 → 组装真实依赖（DeepSeek key 缺失时 UI 仍可启动，对话时报错引导）。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import AsyncIterator, Callable
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
from kdagent.mcp.manager import MCPManager
from kdagent.mcp.search import ToolSearch
from kdagent.memory.consolidator import MemoryConsolidator
from kdagent.memory.extractor import MemoryExtractor
from kdagent.memory.store import build_memory_store
from kdagent.permission.checker import build_permission_checker
from kdagent.skill import BUILTIN_SKILLS_DIR, LoadSkill, SkillCreator, SkillManager
from kdagent.subagent import (
    BUILTIN_AGENTS_DIR,
    AgentManager,
    SubAgentRunner,
    TaskCreate,
    TaskGet,
    TaskList,
    TaskManager,
    TaskUpdate,
    WorktreeManager,
)
from kdagent.subagent import (
    Agent as AgentTool,
)
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
    # 09 M4-c 工具生态：ToolSearch（延迟工具拉取）注册进 registry；
    # MCP Server 连接在 KDApp on_mount 后台异步发起（启动即连接，失败隔离）。
    registry = build_default_registry()
    registry.register(ToolSearch(registry))
    mcp_manager = MCPManager(registry)
    mcp_manager.load_configs(config.mcp_servers if isinstance(config.mcp_servers, dict) else {})
    # 09 M4-d Skill 两阶段加载：三级搜索（项目>用户>内置），启动只扫 frontmatter
    # （system-reminder 注入「可用 Skill」清单），完整 SOP 经 LoadSkill 按需加载。
    # skill-creator 写入用户级目录（~/.kdagent/skills/，个人通用）。
    skill_manager = SkillManager(
        [
            kd_dir / "skills",  # 项目级（可提交 git、团队共享）
            Path.home() / ".kdagent" / "skills",  # 用户级（个人通用）
            BUILTIN_SKILLS_DIR,  # 内置级（开箱即用）
        ]
    )
    skill_manager.scan()
    registry.register(LoadSkill(skill_manager))
    registry.register(SkillCreator(skill_manager))
    # 10 M5-a SubAgent：三级搜索（项目>用户>内置）+ 内置 4 Agent（Verification 默认关
    # T27）。AgentManager 供 Agent 工具选型；TaskManager 管理后台任务，完成注入主对话。
    # SubAgentRunner 持主 llm/registry/config，`make_client` 支持 model 覆盖换 client。
    agent_manager = AgentManager(
        [
            kd_dir / "agents",  # 项目级（可提交 git、团队共享）
            Path.home() / ".kdagent" / "agents",  # 用户级（个人通用）
            BUILTIN_AGENTS_DIR,  # 内置级（开箱即用 4 类）
        ],
        enable_verification=bool(config.agents.get("enable_verification_agent", False)),
    )
    agent_manager.scan()
    subagent_runner = SubAgentRunner(
        llm=llm,
        tools=registry,
        config=config,
        work_dir=work_dir,
        permission_checker=permission_checker,
        make_client=_make_client(api_key),
    )
    task_manager = TaskManager(subagent_runner)
    # 10 M5-b Worktree：空间隔离工作目录（§3.10-3.13）。目录在仓库内
    # `.kdagent/worktrees/`（已 .gitignore）；过期清理 fail-closed 不丢成果。
    worktree_manager = WorktreeManager(work_dir, kd_dir / "worktrees")
    registry.register(
        AgentTool(subagent_runner, agent_manager, task_manager, worktree_manager)
    )
    registry.register(TaskList(task_manager))
    registry.register(TaskGet(task_manager))
    registry.register(TaskCreate(task_manager))
    registry.register(TaskUpdate(task_manager))
    return KDApp(
        config=config,
        llm=llm,
        conversation=ConversationManager(),
        tools=registry,
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
        mcp_manager=mcp_manager,
        skill_manager=skill_manager,
        task_manager=task_manager,
        agent_manager=agent_manager,
        worktree_manager=worktree_manager,
    )


def _make_client(api_key: str) -> Callable[[str], LLMClient]:
    """model 覆盖工厂：子 Agent 指定 model 时新建 OpenAI 兼容 client（10 §3.3）。"""

    def factory(model: str) -> LLMClient:
        return OpenAICompatClient(
            ProviderConfig(
                protocol="openai",
                model=model,
                base_url="https://api.deepseek.com/v1",
                api_key=api_key,
            )
        )

    return factory


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    work_dir = Path(args.dir) if args.dir else None
    # 方案 A：Windows 终端不支持 Kitty 时禁用（对齐 Claude Code 回退传统流），
    # 恢复中文 IME；非 win32 为 no-op（compat.patch_windows_input）。
    patch_windows_input()
    build_kdapp(work_dir=work_dir).run()


if __name__ == "__main__":
    main(sys.argv[1:])
