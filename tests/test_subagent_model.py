"""Agent 定义 frontmatter 解析（规格 10 §3.4）。"""

from __future__ import annotations

import pytest

from kdagent.subagent.model import (
    AGENT_NAME_RE,
    DEFAULT_MAX_TURNS,
    parse_agent_file,
    parse_agent_text,
    yaml_scalar,
)


def _text(**overrides: object) -> str:
    front = {
        "name": "explore",
        "description": "只读探索",
        **overrides,
    }
    lines = ["---"] + [f"{k}: {v}" for k, v in front.items()] + ["---", "", "正文", "指令"]
    return "\n".join(lines)


def test_parse_valid_minimal() -> None:
    agent = parse_agent_text(_text())
    assert agent is not None
    assert agent.name == "explore"
    assert agent.description == "只读探索"
    assert agent.tools == ()
    assert agent.disallowed_tools == ()
    assert agent.model == "inherit"
    assert agent.max_turns == DEFAULT_MAX_TURNS
    assert agent.permission_mode == ""  # 未声明 = 继承父 checker（v052 review 收口语义）
    assert agent.system_prompt == "正文\n指令"


def test_parse_full_fields() -> None:
    text = """---
name: security-reviewer
description: 专注安全审查
disallowedTools: [Agent, EditFile, WriteFile]
model: deepseek-chat
maxTurns: 15
permissionMode: dontAsk
isolation: worktree
---

你是一个安全审查 Agent。
"""
    agent = parse_agent_text(text)
    assert agent is not None
    assert agent.disallowed_tools == ("Agent", "EditFile", "WriteFile")
    assert agent.model == "deepseek-chat"
    assert agent.max_turns == 15
    assert agent.permission_mode == "dontAsk"
    assert agent.isolation == "worktree"
    assert agent.system_prompt == "你是一个安全审查 Agent。"


def test_parse_tools_whitelist() -> None:
    agent = parse_agent_text(_text(tools="[Glob, Grep, ReadFile]"))
    assert agent is not None
    assert agent.tools == ("Glob", "Grep", "ReadFile")


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "空文本"),
        ("没有 frontmatter", "缺 frontmatter"),
        ("---\nfoo: bar\n---\n", "缺 name"),
        ("---\nname: Bad-Name\n---\n", "非法 name（大写）"),
        ("---\nname: ok\n---\n", "缺 description"),
        ("---\nname: ok\ndescription:  \n---\n", "description 空白"),
        ("---\nname: ok\ndescription: {a: b}\n---\n", "description 非字符串"),
    ],
)
def test_parse_invalid(text: str, reason: str) -> None:
    assert parse_agent_text(text) is None, f"应返回 None：{reason}"


def test_parse_invalid_yaml() -> None:
    assert parse_agent_text("---\nname: [unclosed\n---\n") is None


def test_parse_max_turns_invalid_fallback() -> None:
    for bad in (0, -3, "abc"):
        agent = parse_agent_text(_text(maxTurns=bad))
        assert agent is not None
        assert agent.max_turns == DEFAULT_MAX_TURNS


def test_parse_permission_mode_invalid_fallback() -> None:
    """非法 permissionMode → 未声明语义（""），不误落到 default 自动拒绝。"""
    agent = parse_agent_text(_text(permissionMode="anything"))
    assert agent is not None
    assert agent.permission_mode == ""


def test_parse_file(tmp_path) -> None:
    p = tmp_path / "agent.md"
    p.write_text(_text(name="explore"), encoding="utf-8")
    agent = parse_agent_file(p)
    assert agent is not None
    assert agent.name == "explore"
    assert agent.path == p


def test_parse_file_missing(tmp_path) -> None:
    assert parse_agent_file(tmp_path / "nope.md") is None


def test_name_re() -> None:
    assert AGENT_NAME_RE.fullmatch("explore")
    assert AGENT_NAME_RE.fullmatch("general-purpose")
    assert not AGENT_NAME_RE.fullmatch("General")
    assert not AGENT_NAME_RE.fullmatch("-explore")
    assert not AGENT_NAME_RE.fullmatch("a b")


def test_yaml_scalar_escapes_colon_space() -> None:
    """含 ': ' 的 description 双引号包裹后 YAML 可回读（踩坑 M4 同款）。"""
    scalar = yaml_scalar("输出 VERDICT: PASS/FAIL 判定")
    import yaml

    assert yaml.safe_load(scalar) == "输出 VERDICT: PASS/FAIL 判定"
