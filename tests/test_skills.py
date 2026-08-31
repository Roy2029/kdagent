"""Skill 包测试（09 §3.7-3.12）：frontmatter / 三级优先级 / LoadSkill / skill-creator / 注入。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from kdagent.config import Config
from kdagent.engine.agent import Agent
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import LLMStreamEvent, Payload
from kdagent.skill.loadskill import LoadSkill
from kdagent.skill.manager import SkillManager, build_skills_reminder
from kdagent.skill.model import SkillMeta, parse_skill_text
from kdagent.skill.skill_creator import SkillCreator
from kdagent.tools.base import ToolContext, ToolResult
from kdagent.tools.registry import ToolRegistry

# ---- frontmatter 解析（model.py） ----

def test_parse_skill_text_valid() -> None:
    text = (
        "---\nname: commit\ndescription: 分析 git diff 并生成规范的 commit\n"
        "mode: fork\nmodel: deepseek-reasoner\ncontext: none\n---\n\n# 步骤\n1. do it\n"
    )
    meta = parse_skill_text(text)
    assert meta is not None
    assert meta.name == "commit"
    assert meta.mode == "fork"
    assert meta.model == "deepseek-reasoner"
    assert meta.context == "none"
    assert meta.path is None


def test_parse_skill_text_defaults() -> None:
    meta = parse_skill_text("---\nname: commit\ndescription: 生成 commit\n---\n\nbody")
    assert meta is not None
    assert meta.mode == "inline"
    assert meta.model == ""
    assert meta.context == "full"


@pytest.mark.parametrize(
    "text",
    [
        "no frontmatter at all",
        "---\nname: commit\n---\n\nbody",  # 缺 description
        "---\ndescription: 只有描述\n---\n\nbody",  # 缺 name
        "---\nname: Commit\ndescription: x\n---\n",  # 大写非法
        "---\nname: 提交\ndescription: x\n---\n",  # 中文非法
        "---\nname: com mit\ndescription: x\n---\n",  # 空格非法
        "---\nname: [bad, yaml\ndescription: x\n---\n",  # YAML 解析失败
    ],
)
def test_parse_skill_text_invalid(text: str) -> None:
    assert parse_skill_text(text) is None


# ---- SkillManager：三级搜索 / 两阶段加载（manager.py） ----

def _write(
    root: Path,
    name: str,
    description: str,
    *,
    mode: str = "inline",
    body: str = "# 步骤\n1. do it\n",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    p = root / f"{name}.md"
    p.write_text(
        f"---\nname: {name}\ndescription: {description}\nmode: {mode}\n---\n\n{body}",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def skill_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """返回 (项目级, 用户级, 内置级) 三个目录，各自塞同名/独有 Skill。"""
    project, user, builtin = tmp_path / "proj", tmp_path / "user", tmp_path / "builtin"
    _write(project / "skills", "commit", "项目级 commit")
    _write(user / "skills", "commit", "用户级 commit")
    _write(user / "skills", "interview", "模拟后端面试")
    _write(builtin, "commit", "内置级 commit")
    _write(builtin, "review", "审查代码变更")
    return project / "skills", user / "skills", builtin


def test_scan_three_level_priority(skill_tree: tuple[Path, Path, Path]) -> None:
    project, user, builtin = skill_tree
    mgr = SkillManager([project, user, builtin])
    mgr.scan()
    assert [s.name for s in mgr.list()] == ["commit", "interview", "review"]
    # 同名：项目级覆盖用户级覆盖内置级
    assert mgr.get("commit").description == "项目级 commit"
    assert mgr.get("review").description == "审查代码变更"  # 仅内置级


def test_scan_skips_missing_dirs_and_bad_files(tmp_path: Path) -> None:
    project = tmp_path / "proj" / "skills"
    project.mkdir(parents=True)
    _write(project, "ok", "正常 Skill")
    (project / "README.md").write_text("# 说明文档\n", encoding="utf-8")
    (project / "bad.md").write_text("没有 frontmatter\n", encoding="utf-8")
    mgr = SkillManager([tmp_path / "no_such_dir", project])
    mgr.scan()
    assert [s.name for s in mgr.list()] == ["ok"]


def test_scan_discovers_directory_skill(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    (root / "backend-interview" / "examples").mkdir(parents=True)
    (root / "backend-interview" / "SKILL.md").write_text(
        "---\nname: backend-interview\ndescription: 模拟后端面试\n---\n\n# 步骤\n读 examples\n",
        encoding="utf-8",
    )
    mgr = SkillManager([root])
    mgr.scan()
    meta = mgr.get("backend-interview")
    assert meta is not None
    assert meta.path is not None
    assert meta.path.name == "SKILL.md"


def test_load_returns_body_and_replaces_arguments(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write(root, "commit", "生成 commit", body="# 步骤\n1. git status\n用户参数：$ARGUMENTS\n")
    mgr = SkillManager([root])
    mgr.scan()
    skill = mgr.load("commit", "加个 type: docs")
    assert skill is not None
    assert "用户参数：加个 type: docs" in skill.body
    assert skill.mode == "inline"
    # 无参数 → $ARGUMENTS 替换为空（skill_body 已 strip 首尾空行）
    assert mgr.load("commit").body == "# 步骤\n1. git status\n用户参数："


def test_load_unknown_returns_none(tmp_path: Path) -> None:
    mgr = SkillManager([tmp_path / "skills"])
    mgr.scan()
    assert mgr.load("nope") is None


def test_load_reflects_file_edits(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    p = _write(root, "commit", "生成 commit", body="# v1\n")
    mgr = SkillManager([root])
    mgr.scan()
    assert mgr.load("commit").body == "# v1"
    p.write_text("---\nname: commit\ndescription: 生成 commit\n---\n\n# v2 已更新\n", encoding="utf-8")
    assert mgr.load("commit").body == "# v2 已更新"


# ---- skill-creator（manager.create） ----

def test_create_writes_file_and_registers(tmp_path: Path) -> None:
    project, user = tmp_path / "proj", tmp_path / "user"
    (project / "skills").mkdir(parents=True)
    mgr = SkillManager([project / "skills", user / "skills"])
    path = mgr.create("deploy", "部署到生产，含冒号：注意", "# 步骤\n1. build\n")
    assert path == user / "skills" / "deploy.md"
    assert path.exists()
    # 写盘后可回读（含冒号的 description 用 yaml_scalar 转义）
    assert "deploy" in mgr
    assert mgr.get("deploy").description == "部署到生产，含冒号：注意"
    assert mgr.load("deploy").body == "# 步骤\n1. build"


def test_create_validates_name_and_overwrite(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    mgr = SkillManager([root])
    with pytest.raises(ValueError):
        mgr.create("Bad Name", "x", "body")
    with pytest.raises(ValueError):
        mgr.create("提交", "x", "body")
    mgr.create("commit", "生成 commit", "body")
    with pytest.raises(FileExistsError):
        mgr.create("commit", "另一个", "body")


# ---- build_skills_reminder（system-reminder 注入） ----

def test_reminder_empty_and_cap() -> None:
    assert build_skills_reminder([]) is None
    metas = [SkillMeta(name=f"s{i:03d}", description="d" * 50) for i in range(120)]
    reminder = build_skills_reminder(metas)
    assert reminder is not None
    assert "<system-reminder>" in reminder
    assert len(reminder.encode("utf-8")) <= 8 * 1024 + 64  # 截断 + 包裹余量


# ---- LoadSkill 工具 ----

def _ctx() -> ToolContext:
    return ToolContext(work_dir=Path("."), config=Config())  # type: ignore[arg-type]


def _manager_with(tmp_path: Path) -> SkillManager:
    _write(tmp_path / "skills", "commit", "生成 commit", body="# 步骤\n1. git status\n参数：$ARGUMENTS\n")
    _write(tmp_path / "skills", "review", "审查", mode="fork", body="# 客观审查\n")
    mgr = SkillManager([tmp_path / "skills"])
    mgr.scan()
    return mgr


@pytest.mark.asyncio
async def test_loadskill_returns_sop(tmp_path: Path) -> None:
    tool = LoadSkill(_manager_with(tmp_path))
    assert tool.is_read_only()
    assert tool.validate_input({}) == ["name 必填且为字符串"]
    result: ToolResult = await tool.execute(_ctx(), {"name": "commit", "arguments": "加个类型"})
    assert not result.is_error
    assert "参数：加个类型" in result.content


@pytest.mark.asyncio
async def test_loadskill_fork_degrades_with_warning(tmp_path: Path) -> None:
    tool = LoadSkill(_manager_with(tmp_path))
    result: ToolResult = await tool.execute(_ctx(), {"name": "review"})
    assert not result.is_error
    assert "fork" in result.content and "降级" in result.content
    assert "# 客观审查" in result.content


@pytest.mark.asyncio
async def test_loadskill_unknown_fails(tmp_path: Path) -> None:
    tool = LoadSkill(_manager_with(tmp_path))
    result: ToolResult = await tool.execute(_ctx(), {"name": "nope"})
    assert result.is_error
    assert "nope" in result.content


# ---- skill-creator 工具 ----

@pytest.mark.asyncio
async def test_skill_creator_tool_execute(tmp_path: Path) -> None:
    mgr = SkillManager([tmp_path / "skills"])
    tool = SkillCreator(mgr)
    assert tool.validate_input({}) == [
        "name 必填且为非空字符串",
        "description 必填且为非空字符串",
        "instructions 必填且为非空字符串",
    ]
    result: ToolResult = await tool.execute(
        _ctx(), {"name": "deploy", "description": "部署", "instructions": "# 步骤\n1. build\n"}
    )
    assert not result.is_error
    assert "deploy" in result.content
    assert "deploy" in mgr

    # 重复创建 → 拒绝（错误结果，不抛）
    result2: ToolResult = await tool.execute(
        _ctx(), {"name": "deploy", "description": "部署", "instructions": "body"}
    )
    assert result2.is_error


# ---- Agent payload 注入 ----

class _FakeLLM:
    """最小假 LLM（满足 Agent 构造，payload 测试不真正调用）。"""

    async def stream_chat(self, payload: Payload) -> AsyncIterator[LLMStreamEvent]:
        raise NotImplementedError


def test_agent_payload_injects_skill_reminder(tmp_path: Path) -> None:
    _write(tmp_path / "skills", "commit", "分析 git 变更并生成 commit", body="# 步骤\n1. git status\n")
    mgr = SkillManager([tmp_path / "skills"])
    mgr.scan()
    agent = Agent(
        config=Config(),
        llm=_FakeLLM(),
        conversation=ConversationManager(),
        tools=ToolRegistry(),
        events=lambda ev: None,
        work_dir=tmp_path,
        skills=mgr,
    )
    payload = agent._assemble_payload()
    reminder = payload.messages[-1].content[-1].text  # 末尾临时 user 消息承载 reminder
    assert "<system-reminder>" in reminder
    assert "可用 Skill" in reminder
    assert "commit：分析 git 变更并生成 commit" in reminder
    # 只注入清单（name+description），不注入完整正文
    assert "git status" not in reminder
    # v052 review 迁移：system 恒静态，不含 reminder
    assert "<system-reminder>" not in payload.system
    assert "可用 Skill" not in payload.system


def test_agent_payload_without_skills(tmp_path: Path) -> None:
    agent = Agent(
        config=Config(),
        llm=_FakeLLM(),
        conversation=ConversationManager(),
        tools=ToolRegistry(),
        events=lambda ev: None,
        work_dir=tmp_path,
    )
    payload = agent._assemble_payload()
    # 无 skills/mcp/memory 时：system 静态且末尾不追加临时消息
    assert "可用 Skill" not in payload.system
    assert len(payload.messages) == len(agent._conversation.messages)
