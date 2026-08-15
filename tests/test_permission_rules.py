"""L3 权限规则引擎（规格 06 §3.5）：glob 匹配、deny>ask>allow、三份合并。"""

from __future__ import annotations

from pathlib import Path

import pytest

from kdagent.permission.rules import PermissionRule, RuleEngine


def test_parse_rule() -> None:
    r = PermissionRule.parse("Bash(git *)", "allow")
    assert r.tool_pattern == "Bash"
    assert r.content_pattern == "git *"
    assert r.effect == "allow"


def test_parse_invalid_raises() -> None:
    with pytest.raises(ValueError):
        PermissionRule.parse("no-parens", "allow")
    with pytest.raises(ValueError):
        PermissionRule.parse("Bash(git *)", "maybe")


def test_glob_matching() -> None:
    r = PermissionRule.parse("Bash(git push --force*)", "deny")
    assert r.matches("Bash", "git push --force origin main")
    assert r.matches("Bash", "git push --force-with-lease origin main")
    assert not r.matches("Bash", "git push origin main")
    assert not r.matches("EditFile", "git push --force origin main")


def test_deny_beats_allow() -> None:
    eng = RuleEngine()
    for r in [
        PermissionRule.parse("Bash(git *)", "allow"),
        PermissionRule.parse("Bash(git push --force*)", "deny"),
    ]:
        eng.add(r)
    effect, rule_str = eng.evaluate("Bash", "git push --force origin main")
    assert effect == "deny"
    assert rule_str == "Bash(git push --force*)"
    effect, _ = eng.evaluate("Bash", "git commit -m x")
    assert effect == "allow"


def test_ask_over_allow() -> None:
    eng = RuleEngine()
    for r in [
        PermissionRule.parse("ReadFile(*.env*)", "ask"),
        PermissionRule.parse("ReadFile(*)", "allow"),
    ]:
        eng.add(r)
    effect, rule_str = eng.evaluate("ReadFile", "/proj/.env.local")
    assert effect == "ask"
    assert rule_str == "ReadFile(*.env*)"


def test_no_match_returns_unknown() -> None:
    eng = RuleEngine()
    effect, rule_str = eng.evaluate("Bash", "ls -la")
    assert effect is None
    assert rule_str is None


def test_missing_file_is_empty_set(tmp_path: Path) -> None:
    eng = RuleEngine()
    eng.load_many([tmp_path / "permissions.yaml"])  # 不存在 → 空集
    assert len(eng) == 0


def test_load_yaml_file(tmp_path: Path) -> None:
    f = tmp_path / "permissions.yaml"
    f.write_text(
        "- rule: Bash(git push --force*)\n  effect: deny\n- rule: EditFile(*.py)\n  effect: allow\n",
        encoding="utf-8",
    )
    eng = RuleEngine()
    eng.load(f)
    assert len(eng) == 2
    effect, _ = eng.evaluate("EditFile", "/proj/main.py")
    assert effect == "allow"


def test_learn_appends_local_rule(tmp_path: Path) -> None:
    local = tmp_path / "permissions.local.yaml"
    eng = RuleEngine()
    eng.load_many([], local_path=local)
    eng.learn("Bash", "git commit -m fix")
    assert local.is_file()
    # 内存同步：本进程内立即放行。
    effect, _ = eng.evaluate("Bash", "git commit -m fix")
    assert effect == "allow"
    # 落盘内容可重载。
    eng2 = RuleEngine()
    eng2.load(local)
    assert len(eng2) == 1
