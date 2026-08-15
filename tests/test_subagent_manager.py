"""AgentManager：三级搜索 / verification 开关 / 坏文件容错（规格 10 §3.4）。"""

from __future__ import annotations

from pathlib import Path

from kdagent.subagent import BUILTIN_AGENTS_DIR, AgentManager


def _write(d: Path, name: str, description: str = "默认描述") -> Path:
    p = d / f"{name}.md"
    p.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n正文",
        encoding="utf-8",
    )
    return p


def test_builtin_agents_default_no_verification() -> None:
    """内置 3 类（Verification 默认关，规格 10 T27）。"""
    mgr = AgentManager([BUILTIN_AGENTS_DIR])
    mgr.scan()
    names = [d.name for d in mgr.list()]
    assert names == ["explore", "general-purpose", "plan"]


def test_builtin_agents_with_verification_enabled() -> None:
    mgr = AgentManager([BUILTIN_AGENTS_DIR], enable_verification=True)
    mgr.scan()
    assert "verification" in [d.name for d in mgr.list()]


def test_three_tier_priority(tmp_path) -> None:
    """同名高优先级覆盖低优先级（项目 > 用户 > 内置）。"""
    project = tmp_path / "project"
    user = tmp_path / "user"
    project.mkdir()
    user.mkdir()
    _write(project, "explore", "项目级 explore")
    _write(user, "explore", "用户级 explore")
    mgr = AgentManager([project, user, BUILTIN_AGENTS_DIR])
    mgr.scan()
    d = mgr.get("explore")
    assert d is not None
    assert d.description == "项目级 explore"
    # 项目级覆盖后，用户级 + 内置同名都不可见
    assert len([x for x in mgr.list() if x.name == "explore"]) == 1


def test_project_and_user_merge(tmp_path) -> None:
    """不同名混合：项目级一个、内置补全，无冲突。"""
    project = tmp_path / "project"
    project.mkdir()
    _write(project, "security-reviewer", "项目自定义")
    mgr = AgentManager([project, BUILTIN_AGENTS_DIR])
    mgr.scan()
    names = [d.name for d in mgr.list()]
    assert "security-reviewer" in names
    assert "explore" in names  # 内置仍可用
    assert "general-purpose" in names


def test_bad_file_skipped(tmp_path) -> None:
    """无 frontmatter 的 README 混入不阻断扫描。"""
    (tmp_path / "README.md").write_text("# 说明\n", encoding="utf-8")
    _write(tmp_path, "explore")
    mgr = AgentManager([tmp_path])
    mgr.scan()
    names = [d.name for d in mgr.list()]
    assert "explore" in names
    assert len(names) == 1


def test_directory_agent_skippable_entries(tmp_path) -> None:
    """目录型 AGENT.md 入口 + 顶层 AGENT.md（无名字上下文）跳过。"""
    sub = tmp_path / "team-refactor"
    sub.mkdir()
    (sub / "AGENT.md").write_text(
        "---\nname: team-refactor\ndescription: 目录型 Agent\n---\n\n正文", encoding="utf-8"
    )
    (tmp_path / "AGENT.md").write_text("---\ndescription: 无名字\n---\n\n正文", encoding="utf-8")
    mgr = AgentManager([tmp_path])
    mgr.scan()
    names = [d.name for d in mgr.list()]
    assert "team-refactor" in names
    assert "AGENT" not in names


def test_missing_dir_empty() -> None:
    mgr = AgentManager([Path(".") / "does-not-exist-xyz"])
    mgr.scan()
    assert mgr.list() == []


def test_validate_type() -> None:
    mgr = AgentManager([BUILTIN_AGENTS_DIR])
    mgr.scan()
    assert mgr.validate_type("explore")
    assert not mgr.validate_type("nope")
    assert not mgr.validate_type("../../etc")
