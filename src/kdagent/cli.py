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
from kdagent.context.compactor import (
    cost_params_from_table,
    estimate_messages_tokens,
    estimate_tokens,
)
from kdagent.context.context_manager import ContextManager
from kdagent.engine.agent import DEFAULT_SYSTEM_PROMPT
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import LLMClient, LLMStreamEvent, Payload, ProviderConfig
from kdagent.engine.llm.openai import OpenAICompatClient
from kdagent.harness import detect_test_infra
from kdagent.hooks.engine import HookEngine
from kdagent.mcp.manager import MCPManager
from kdagent.mcp.search import ToolSearch
from kdagent.memory.consolidator import MemoryConsolidator
from kdagent.memory.extractor import MemoryExtractor
from kdagent.memory.store import build_memory_store
from kdagent.permission.checker import build_permission_checker
from kdagent.permission.modes import MODE_MATRIX
from kdagent.skill import BUILTIN_SKILLS_DIR, LoadSkill, SkillCreator, SkillManager
from kdagent.subagent import (
    BUILTIN_AGENTS_DIR,
    AgentManager,
    NamedAgentManager,
    SendMessage,
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
from kdagent.tools import GitRevert, TestRunner, build_default_registry
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
    sub = parser.add_subparsers(dest="command")
    eval_p = sub.add_parser("eval", help="跑一轮评测（11 评估体系）")
    eval_p.add_argument("tasks", help="评测配置 JSON 文件（tasks.json 结构见 kdagent.eval.cli）")
    eval_p.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="并发跑批数（11 §3.7 可并行；默认 1 顺序跑）",
    )
    eval_p.add_argument(
        "--report",
        metavar="RUN_ID",
        help="只读复核（11 §3.4）：失败题 → span 树 → 事件流阅读，不需要 api_key",
    )
    eval_p.add_argument(
        "--annotate",
        nargs=3,
        metavar=("RUN_ID", "TASK_ID", "KIND"),
        help="批注：人工修正失败归类（not_located/wrong_fix/regression/harness_fault/constraint_conflict）",
    )
    eval_p.add_argument(
        "--note",
        default="",
        help="--annotate 的备注文本",
    )
    eval_p.add_argument(
        "--diff",
        nargs=2,
        metavar=("RUN_A", "RUN_B"),
        help="复测对比（11 §3.5）：两轮 run 题级变化（fail2pass/pass2fail/fail2fail/pass2pass）",
    )
    eval_p.add_argument(
        "--metrics",
        metavar="RUN_ID",
        help="单版本报表（11 §3.8 metrics_by_run）：通过率/token/耗时，免 api_key",
    )
    eval_p.add_argument(
        "--docker-harness",
        metavar="RUN_HARNESS_PY",
        help="11 §5 224 Docker 判分：run_harness.py 路径（官方 harness Windows 启动器）。"
        "给了则跑批只产 patch，判分统一走官方容器（F2P/P2P 实测）",
    )
    eval_p.add_argument(
        "--docker-python",
        metavar="VENV_PYTHON",
        default=None,
        help="装了官方 swebench 包的 venv python（默认 --docker-harness 同目录 .venv/Scripts/python.exe）",
    )
    eval_p.add_argument(
        "--docker-namespace",
        default="starryzhang",
        help="DockerHub 预构建镜像命名空间（默认 starryzhang）",
    )
    eval_p.add_argument(
        "--preinstall",
        action="store_true",
        help="D96 治理③：封史副本内建 venv + pip install -e .（模拟容器环境构建）。"
        "对轻依赖 repo 开——模型可直接 import 工作区源码验证，防 pip download 病。"
        "C 扩展/重依赖 repo 预装可能失败（不阻断跑批，模型可自装）",
    )
    obs_p = sub.add_parser("obs", help="可观测性工具（07）")
    obs_sub = obs_p.add_subparsers(dest="obs_command")
    cal_p = obs_sub.add_parser("calibrate", help="L2 在线压缩标定报告（01 §9.2 T8）")
    cal_p.add_argument(
        "--obs-dir",
        default=None,
        help="obs 根目录（默认 {work_dir}/.kdagent/obs）",
    )
    cal_p.add_argument(
        "--run-id",
        default=None,
        help="只统计指定 eval 轮（trace 头 eval.run_id）",
    )
    cal_p.add_argument(
        "--json",
        action="store_true",
        help="输出结构化 JSON（机器可读）",
    )
    cal_p.add_argument(
        "--output",
        default=None,
        help="写文件（默认 stdout）",
    )
    return parser


def build_kdapp(work_dir: Path | None = None) -> KDApp:
    """组装真实依赖：DeepSeek（OpenAI 兼容 adapter）+ 7 工具 + 会话目录 + 07 obs。

    config 从 work_dir 加载（D98：`-d` 指定 work_dir 时配置必须跟 work_dir 走，
    而不是进程 cwd——否则 `uv run kdagent -d X` 从项目目录启动读到开发项目的
    config，X 的 config 完全被忽略）。
    """
    work_dir = (work_dir or Path.cwd()).resolve()
    config = load_config(work_dir)
    api_key = load_api_key()
    model = config.model or "deepseek-v4-flash"
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
    # 06 M3 可控档：五层裁决器 + Hook 引擎（config.hooks 列表）。本地规则
    # learn 目标 = 项目级 permissions.local.yaml。
    # N2：模式默认来自 config.permissions.mode，但上次 /permissions 切换已落盘
    # `{项目}/.kdagent/permissions.mode`——持久化文件优先，重启不重置回 default。
    permission_mode = config.permissions.get("mode", "default")
    if not isinstance(permission_mode, str) or permission_mode not in MODE_MATRIX:
        permission_mode = "default"
    saved_mode_path = kd_dir / "permissions.mode"
    if saved_mode_path.is_file():
        try:
            saved = saved_mode_path.read_text(encoding="utf-8").strip()
            if saved in MODE_MATRIX:
                permission_mode = saved
        except OSError:
            pass
    permission_checker = build_permission_checker(
        work_dir,
        mode=permission_mode,
        kdagent_dirs=[kd_dir],
        # M1：用户级记忆根进沙箱——索引注入的全局记忆绝对路径（~/.kdagent/
        # memory/…）在 work_dir 外，不加会被 L2 沙箱拦成 HITL 弹窗读不到。
        extra_roots=[Path.home() / ".kdagent" / "memory"],
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
        hooks=hooks,  # 10 §3.3：共享主 HookEngine，子 Agent hook 同生效
    )
    task_manager = TaskManager(
        subagent_runner,
        # 10 §3.7 ②（D79）：前台 Agent 工具超时自动转后台阈值（agents.auto_background_ms）。
        auto_background_ms=config.get_auto_background_ms(),
    )
    # 10 M5-b Worktree：空间隔离工作目录（§3.10-3.13）。目录在仓库内
    # `.kdagent/worktrees/`（已 .gitignore）；过期清理 fail-closed 不丢成果。
    worktree_manager = WorktreeManager(work_dir, kd_dir / "worktrees")
    # 12 Harness 测试闭环：TestRunner 工具（隔离沙箱跑测试 + 结构化 TestingEvent）。
    # resolve_worktree 注入 worktree_manager.path（解耦 tools→subagent 包循环）。
    registry.register(TestRunner(worktree_manager.path))
    # 12 §3.2 代码回滚：GitRevert 精确回退（worktree 变更保护下失败改动可恢复）。
    registry.register(GitRevert())
    # 10 M5-d SendMessage：命名 Agent 注册表 + 消息投递工具。命名 Agent 存活到
    # 会话结束（注册即常驻），SendMessage 投递新任务唤醒继续。
    named_manager = NamedAgentManager(subagent_runner)
    registry.register(
        AgentTool(
            subagent_runner,
            agent_manager,
            task_manager,
            worktree_manager,
            named_manager,
        )
    )
    registry.register(TaskList(task_manager))
    registry.register(TaskGet(task_manager))
    registry.register(TaskCreate(task_manager, agent_manager))
    registry.register(TaskUpdate(task_manager))
    registry.register(SendMessage(named_manager))
    # 12 §3.1 激活条件 2：探测到测试基建 → 启动注入「改后自测」提醒（T32 提示词
    # 引导，非强制门禁）。启动探测一次，低频变化不进 system 字段（前缀缓存友好）。
    infra_reminder = detect_test_infra(work_dir)
    system_prompt = DEFAULT_SYSTEM_PROMPT
    if infra_reminder:
        system_prompt = f"{system_prompt}\n\n{infra_reminder}"
    return KDApp(
        config=config,
        llm=llm,
        conversation=ConversationManager(),
        tools=registry,
        work_dir=work_dir,
        sessions_dir=kd_dir / "sessions",
        system_prompt=system_prompt,
        model_name=model,
        obs_dir=kd_dir / "obs",
        context_manager=ContextManager(
            kd_dir / "sessions",
            llm=llm,
            system_prompt=DEFAULT_SYSTEM_PROMPT,
            # T5-1：计价表按 model 取价（D104 内置三档 + config cost 段可覆盖；None = DEFAULT）
            cost=cost_params_from_table(
                config.get_cost_table(), config.provider, model=model
            ),
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
    # obs 子命令：可观测性工具（01 §9.2 T8 标定），纯读不碰 TUI/IME。
    if args.command == "obs":
        from kdagent.obs import l2_calibration

        if args.obs_command == "calibrate":
            raise SystemExit(
                l2_calibration.main(
                    [
                        *(("--obs-dir", args.obs_dir) if args.obs_dir else ()),
                        *(("--run-id", args.run_id) if args.run_id else ()),
                        *(["--json"] if args.json else []),
                        *(("--output", args.output) if args.output else ()),
                    ]
                )
            )
        build_parser().print_help()
        raise SystemExit(2)
    # eval 子命令：长任务后台执行（11 §3.9），不走 TUI/IME 补丁。
    if args.command == "eval":
        from kdagent.eval import (
            run_annotate_cli,
            run_diff_cli,
            run_eval_cli,
            run_metrics_cli,
            run_review_cli,
        )

        if args.report:
            raise SystemExit(run_review_cli(Path(args.tasks), args.report))
        if args.annotate:
            run_id, task_id, kind = args.annotate
            raise SystemExit(
                run_annotate_cli(Path(args.tasks), run_id, task_id, kind, args.note)
            )
        if args.diff:
            run_a, run_b = args.diff
            raise SystemExit(run_diff_cli(Path(args.tasks), run_a, run_b))
        if args.metrics:
            raise SystemExit(run_metrics_cli(Path(args.tasks), args.metrics))
        raise SystemExit(
            run_eval_cli(
                Path(args.tasks),
                workers=args.workers,
                docker_harness=args.docker_harness,
                docker_python=args.docker_python,
                docker_namespace=args.docker_namespace,
                preinstall=args.preinstall,
            )
        )
    work_dir = Path(args.dir) if args.dir else None
    # 方案 A：Windows 终端不支持 Kitty 时禁用（对齐 Claude Code 回退传统流），
    # 恢复中文 IME；非 win32 为 no-op（compat.patch_windows_input）。
    patch_windows_input()
    build_kdapp(work_dir=work_dir).run()


if __name__ == "__main__":
    main(sys.argv[1:])
