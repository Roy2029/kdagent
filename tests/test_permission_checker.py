"""权限五层裁决（规格 06 §3.2）：L1/L2/L3/L4 + 敏感禁写 + bypass + learn。"""

from __future__ import annotations

from pathlib import Path

import pytest

from kdagent.permission.blacklist import CommandBlacklist
from kdagent.permission.checker import PermissionChecker
from kdagent.permission.rules import PermissionRule, RuleEngine
from kdagent.permission.sandbox import PathSandbox
from kdagent.tools.filesystem import ReadFile, WriteFile
from kdagent.tools.shell import Bash
from kdagent.tools.todo import TodoWrite


def _checker(
    tmp_path: Path,
    *,
    mode: str = "default",
    rules: RuleEngine | None = None,
    kdagent_dirs: list[Path] | None = None,
    blacklist: CommandBlacklist | None = None,
) -> PermissionChecker:
    """装配最小裁决器：沙箱仅 work_dir（不含系统临时目录，避免 tmp 误判）。"""
    sb = PathSandbox([tmp_path], work_dir=tmp_path, include_tempdir=False)
    return PermissionChecker(
        mode=mode,
        blacklist=blacklist,
        sandbox=sb,
        rules=rules,
        work_dir=tmp_path,
        kdagent_dirs=kdagent_dirs,
    )


def test_l1_blacklist_hard_deny(tmp_path: Path) -> None:
    ck = _checker(tmp_path, blacklist=CommandBlacklist())
    d = ck.check(Bash(), {"command": "rm -rf /"})
    assert d.effect == "deny"
    assert "危险命令" in d.reason


def test_bypass_allows_but_blacklist_still_denies(tmp_path: Path) -> None:
    ck = _checker(tmp_path, mode="bypassPermissions", blacklist=CommandBlacklist())
    d = ck.check(Bash(), {"command": "ls -la"})
    assert d.effect == "allow"
    d = ck.check(Bash(), {"command": "rm -rf /"})
    assert d.effect == "deny"  # 黑名单不豁免


def test_l2_sandbox_ask_outside(tmp_path: Path) -> None:
    ck = _checker(tmp_path, mode="acceptEdits")  # write 本应 allow
    outside = tmp_path.parent / "outside.txt"
    d = ck.check(WriteFile(), {"path": str(outside), "content": "x"})
    assert d.effect == "ask"
    assert "超出沙箱" in d.reason


def test_l2_sandbox_inside_allowed(tmp_path: Path) -> None:
    ck = _checker(tmp_path, mode="acceptEdits")
    d = ck.check(WriteFile(), {"path": str(tmp_path / "src" / "a.py"), "content": "x"})
    assert d.effect == "allow"


def test_l3_rule_deny_beats_matrix(tmp_path: Path) -> None:
    """规则 deny 压过矩阵（default 下 Bash 本为 ask，规则 deny 更严）。"""
    rules = RuleEngine()
    rules.add(PermissionRule.parse("Bash(git push --force*)", "deny"))
    ck = _checker(tmp_path, mode="default", rules=rules)
    d = ck.check(Bash(), {"command": "git push --force origin main"})
    assert d.effect == "deny"
    d = ck.check(Bash(), {"command": "git commit -m ok"})
    assert d.effect == "ask"  # 未命中规则 → 矩阵 shell=ask


def test_bypass_skips_l3_rules_but_not_blacklist(tmp_path: Path) -> None:
    """规格 §3.6：bypass 跳过 L2-L5（含规则），黑名单仍生效。"""
    rules = RuleEngine()
    rules.add(PermissionRule.parse("Bash(git push --force*)", "deny"))
    ck = _checker(tmp_path, mode="bypassPermissions", rules=rules, blacklist=CommandBlacklist())
    assert ck.check(Bash(), {"command": "git push --force origin main"}).effect == "allow"
    assert ck.check(Bash(), {"command": "rm -rf /"}).effect == "deny"


def test_l3_rule_ask(tmp_path: Path) -> None:
    rules = RuleEngine()
    rules.add(PermissionRule.parse("ReadFile(*.env*)", "ask"))
    ck = _checker(tmp_path, mode="default", rules=rules)
    d = ck.check(ReadFile(), {"path": str(tmp_path / ".env")})
    assert d.effect == "ask"
    d = ck.check(ReadFile(), {"path": str(tmp_path / "src" / "main.py")})
    assert d.effect == "allow"  # 只读 + 无规则 → 矩阵 allow


@pytest.mark.parametrize(
    ("mode", "write_effect", "bash_effect"),
    [
        ("default", "ask", "ask"),
        # N3：acceptEdits 下 Bash 只读命令（ls）经 D10 命令级判断放行。
        ("acceptEdits", "allow", "allow"),
        ("plan", "ask", "ask"),
        ("bypassPermissions", "allow", "allow"),
    ],
)
def test_l4_mode_matrix(tmp_path: Path, mode: str, write_effect: str, bash_effect: str) -> None:
    ck = _checker(tmp_path, mode=mode)
    d = ck.check(ReadFile(), {"path": str(tmp_path / "a.py")})
    assert d.effect == "allow"  # 只读恒 allow
    d = ck.check(WriteFile(), {"path": str(tmp_path / "a.py"), "content": "x"})
    assert d.effect == write_effect
    d = ck.check(Bash(), {"command": "ls"})
    assert d.effect == bash_effect
    # N3 兜底：破坏性 Bash（非只读，如 rm -rf）不因只读判断放行——
    # default/acceptEdits/plan 按矩阵 shell=ask，bypass 全放行。
    d = ck.check(Bash(), {"command": "rm -rf /tmp/foo"})
    assert d.effect == ("allow" if mode == "bypassPermissions" else "ask")


def test_planning_tool_always_allow(tmp_path: Path) -> None:
    for mode in ("default", "acceptEdits", "plan", "bypassPermissions"):
        ck = _checker(tmp_path, mode=mode)
        d = ck.check(TodoWrite(), {"todo": "write code", "task": "t", "steps": []})
        assert d.effect == "allow"


def test_sensitive_path_denied_even_bypass(tmp_path: Path) -> None:
    kd = tmp_path / ".kdagent"
    ck = _checker(tmp_path, mode="bypassPermissions", kdagent_dirs=[kd])
    for p in ("config.yaml", "permissions.local.yaml", "skills/agent.md"):
        d = ck.check(WriteFile(), {"path": str(kd / p), "content": "x"})
        assert d.effect == "deny", p
        assert "敏感路径禁写" in d.reason
    # 正常文件仍放行
    d = ck.check(WriteFile(), {"path": str(tmp_path / "src" / "main.py"), "content": "x"})
    assert d.effect == "allow"


def test_learn_allows_next_time(tmp_path: Path) -> None:
    rules = RuleEngine()
    rules.load_many([], local_path=tmp_path / "permissions.local.yaml")
    ck = _checker(tmp_path, mode="default", rules=rules)
    d = ck.check(Bash(), {"command": "git commit -m fix"})
    assert d.effect == "ask"  # 默认模式 Bash → ask
    ck.learn("Bash", "git commit -m fix")
    d = ck.check(Bash(), {"command": "git commit -m fix"})
    assert d.effect == "allow"  # 已「始终允许」


def test_set_mode_validates(tmp_path: Path) -> None:
    ck = _checker(tmp_path)
    ck.set_mode("plan")
    assert ck.mode == "plan"
    with pytest.raises(ValueError):
        ck.set_mode("superuser")
