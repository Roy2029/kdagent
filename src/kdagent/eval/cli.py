"""kdagent eval CLI 子命令（规格 11 §3.9：长任务后台执行，不走 TUI）。

用法：kdagent eval <tasks.json>

tasks.json 结构：
{
  "run_id": "eval-1",
  "repo_dir": "path/to/source/git/repo",   # 含 base_commit 的原始仓库（封史来源）
  "work_dir": "path/to/eval/workspace",    # 封史副本的存放目录（可选，默认 repo_dir/.kdagent/eval）
  "tasks": [ { "instance_id", "base_commit", "problem_statement",
               "fail_to_pass", "pass_to_pass", "gold_patch", "test_cmd", "constraint" } ]
}
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from kdagent.config import load_api_key, load_config
from kdagent.engine.llm.base import ProviderConfig
from kdagent.engine.llm.openai import OpenAICompatClient
from kdagent.eval.model import EvalReport, EvalTask
from kdagent.eval.runner import EvalRunner
from kdagent.subagent import BUILTIN_AGENTS_DIR, AgentManager, SubAgentRunner
from kdagent.tools import build_default_registry


def load_tasks_file(path: Path) -> tuple[str, Path, Path, list[EvalTask]]:
    """解析 tasks.json → (run_id, repo_dir, work_dir, tasks)。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取评测配置 {path}：{exc}") from exc
    repo_dir = Path(data.get("repo_dir", "")).resolve()
    if not repo_dir.is_dir():
        raise ValueError(f"repo_dir 不存在：{repo_dir}")
    work_dir = Path(data.get("work_dir", str(repo_dir / ".kdagent" / "eval"))).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    tasks: list[EvalTask] = []
    for raw in data.get("tasks", []):
        tasks.append(
            EvalTask(
                instance_id=str(raw.get("instance_id", "")),
                repo=str(raw.get("repo", "")),
                base_commit=str(raw.get("base_commit", "")),
                problem_statement=str(raw.get("problem_statement", "")),
                fail_to_pass=[str(x) for x in raw.get("fail_to_pass", [])],
                pass_to_pass=[str(x) for x in raw.get("pass_to_pass", [])],
                gold_patch=str(raw.get("gold_patch", "")),
                test_cmd=str(raw.get("test_cmd", "")),
                constraint=str(raw.get("constraint", "")),
            )
        )
    if not tasks:
        raise ValueError("tasks 为空")
    return str(data.get("run_id", "eval-default")), repo_dir, work_dir, tasks


def run_eval_cli(tasks_file: Path) -> int:
    """跑一轮评测并打印报告（退出码：0 全过 / 1 有失败 / 2 配置错误）。"""
    try:
        run_id, repo_dir, work_dir, tasks = load_tasks_file(tasks_file)
    except ValueError as exc:
        print(f"评测配置错误：{exc}", file=sys.stderr)
        return 2

    config = load_config()
    api_key = load_api_key()
    if not api_key:
        print("未配置 DEEPSEEK_API_KEY：评测需要真实 LLM，请在项目根 .env 设置", file=sys.stderr)
        return 2
    llm = OpenAICompatClient(
        ProviderConfig(
            protocol="openai",
            model=config.model or "deepseek-chat",
            base_url="https://api.deepseek.com/v1",
            api_key=api_key,
        )
    )
    registry = build_default_registry()
    agent_manager = AgentManager([BUILTIN_AGENTS_DIR])
    agent_manager.scan()
    runner = SubAgentRunner(
        llm=llm,
        tools=registry,
        config=config,
        work_dir=repo_dir,
    )
    definition = agent_manager.get("general-purpose")
    if definition is None:
        print("内置 general-purpose Agent 缺失", file=sys.stderr)
        return 2
    eval_runner = EvalRunner(
        runner,
        definition=definition,
        source_repo=repo_dir,
        work_dir=work_dir,
        task_loader=lambda: tasks,
    )
    report: EvalReport = asyncio.run(eval_runner.run(run_id))
    print(report.summary())
    return 0 if not report.failed else 1
