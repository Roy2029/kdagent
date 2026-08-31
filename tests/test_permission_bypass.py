"""权限绕过面负向测试组（v052-review-remediation / permission-hardening）。

覆盖 review 发现的已知绕过面：learn 写坏 YAML 致启动崩溃、acceptEdits 只读
白名单提权（env / find -delete）、cd 组合敏感删除、symlink 绕 skills 禁写、
大小写绕 deny 规则、子 Agent HITL 收口（permissionMode 裁决）、Explore
bash_readonly 门禁。同时验证正向不误伤（非敏感路径仍按原矩阵）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from conftest import FakeLLM, done, tool_call

from kdagent.config import Config
from kdagent.engine.events import PermissionRequestEvent
from kdagent.permission.blacklist import CommandBlacklist
from kdagent.permission.checker import PermissionChecker, build_permission_checker
from kdagent.permission.rules import PermissionRule, RuleEngine
from kdagent.permission.sandbox import PathSandbox
from kdagent.subagent import SubAgentRunner
from kdagent.subagent.model import AgentDef, parse_agent_file
from kdagent.subagent.runner import _SubSink, filter_tools
from kdagent.tools import build_default_registry
from kdagent.tools.filesystem import WriteFile
from kdagent.tools.registry import ToolRegistry
from kdagent.tools.shell import Bash


def _checker(
    tmp_path: Path,
    *,
    mode: str = "default",
    rules: RuleEngine | None = None,
    kdagent_dirs: list[Path] | None = None,
) -> PermissionChecker:
    sb = PathSandbox([tmp_path], work_dir=tmp_path, include_tempdir=False)
    return PermissionChecker(
        mode=mode,
        blacklist=CommandBlacklist(),
        sandbox=sb,
        rules=rules,
        work_dir=tmp_path,
        kdagent_dirs=kdagent_dirs,
    )


# ---- learn / load 安全化（🔴 启动崩溃修复）---------------------------------


def test_learn_quoted_command_yaml_safe(tmp_path: Path) -> None:
    """含引号命令「始终允许」→ YAML 结构不破坏，重启 load 后命中放行。"""
    eng = RuleEngine()
    local = tmp_path / ".kdagent" / "permissions.local.yaml"
    eng.load(local, local=True)
    eng.learn("Bash", 'git commit -m "fix \'x\'"')
    eng2 = RuleEngine()
    eng2.load(local)  # 模拟重启：不抛异常
    effect, _ = eng2.evaluate("Bash", 'git commit -m "fix \'x\'"')
    assert effect == "allow"


def test_learn_multiline_command_yaml_safe(tmp_path: Path) -> None:
    """含换行命令 learn → safe_dump 块标量承载，回读不崩且可匹配。"""
    eng = RuleEngine()
    local = tmp_path / ".kdagent" / "permissions.local.yaml"
    eng.load(local, local=True)
    cmd = "echo line1\nline2"
    eng.learn("Bash", cmd)
    eng2 = RuleEngine()
    eng2.load(local)
    assert eng2.evaluate("Bash", cmd)[0] == "allow"


def test_load_corrupt_yaml_degrades(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """规则文件 YAML 损坏 → 降级空集 + stderr 警告，不抛异常阻断启动。"""
    f = tmp_path / "permissions.yaml"
    f.write_text("rule: [unclosed", encoding="utf-8")
    eng = RuleEngine()
    eng.load(f)
    assert len(eng) == 0
    assert "损坏已忽略" in capsys.readouterr().err


def test_load_malformed_entries_degrade(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """条目结构非法（rule 非字符串）→ 同样降级，不抛异常。"""
    f = tmp_path / "permissions.yaml"
    f.write_text("- rule: 123\n  effect: allow\n", encoding="utf-8")
    eng = RuleEngine()
    eng.load(f)
    assert len(eng) == 0
    assert "损坏已忽略" in capsys.readouterr().err


def test_startup_with_corrupt_rules(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """🔴 场景回归：损坏的项目规则文件不再让 build_permission_checker 崩溃。"""
    kd = tmp_path / ".kdagent"
    kd.mkdir()
    (kd / "permissions.yaml").write_text("{{{{not yaml", encoding="utf-8")
    ck = build_permission_checker(tmp_path)
    assert ck is not None
    assert "损坏已忽略" in capsys.readouterr().err


# ---- acceptEdits 只读白名单收紧（🔴 env / find 提权修复）--------------------


def test_acceptEdits_env_not_readonly(tmp_path: Path) -> None:
    """`env` 是通用命令执行器：`env python -c` 不放行，落矩阵 ask。"""
    ck = _checker(tmp_path, mode="acceptEdits")
    d = ck.check(Bash(), {"command": 'env python -c "import os; os.remove(\'x\')"'})
    assert d.effect == "ask"
    assert "只读命令" not in d.reason


def test_acceptEdits_find_delete_not_readonly(tmp_path: Path) -> None:
    """`find -delete` 有写副作用：不放行。"""
    ck = _checker(tmp_path, mode="acceptEdits")
    d = ck.check(Bash(), {"command": 'find . -name "*.pyc" -delete'})
    assert d.effect == "ask"
    assert "只读命令" not in d.reason


def test_acceptEdits_find_exec_not_readonly(tmp_path: Path) -> None:
    """`find -exec` 可执行任意命令：不放行。"""
    ck = _checker(tmp_path, mode="acceptEdits")
    d = ck.check(Bash(), {"command": "find . -name '*.log' -exec rm {} \\;"})
    assert d.effect == "ask"


def test_acceptEdits_find_pure_readonly_still_allowed(tmp_path: Path) -> None:
    """正向：纯查找 `find` 仍免审批（N3 语义不回退）。"""
    ck = _checker(tmp_path, mode="acceptEdits")
    d = ck.check(Bash(), {"command": 'find . -name "*.py"'})
    assert d.effect == "allow"


def test_acceptEdits_env_plain_query_ask(tmp_path: Path) -> None:
    """`env` 本身（查环境变量）也不再白名单——保守口径。"""
    ck = _checker(tmp_path, mode="acceptEdits")
    assert ck.check(Bash(), {"command": "env"}).effect == "ask"


# ---- Bash 敏感路径「出现即拦」兜底 -----------------------------------------


def test_bash_cd_compose_sensitive_delete_asks(tmp_path: Path) -> None:
    """`cd .kdagent && rm permissions.local.yaml` → 兜底 ask（写语法提取抓不到）。"""
    kd = tmp_path / ".kdagent"
    kd.mkdir()
    ck = _checker(tmp_path, mode="acceptEdits", kdagent_dirs=[kd])
    d = ck.check(Bash(), {"command": "cd .kdagent && rm permissions.local.yaml"})
    assert d.effect == "ask"
    assert "敏感路径" in d.reason


def test_bash_sed_i_sensitive_asks(tmp_path: Path) -> None:
    """`sed -i` 改敏感文件：写语法提取面外，兜底拦。"""
    kd = tmp_path / ".kdagent"
    kd.mkdir()
    ck = _checker(tmp_path, mode="acceptEdits", kdagent_dirs=[kd])
    d = ck.check(Bash(), {"command": f"sed -i 's/a/b/' {kd}/config.yaml"})
    assert d.effect == "ask"


def test_bash_python_c_sensitive_asks(tmp_path: Path) -> None:
    """`python -c "os.remove(...)"` 内嵌敏感路径：兜底拦。"""
    kd = tmp_path / ".kdagent"
    kd.mkdir()
    ck = _checker(tmp_path, mode="acceptEdits", kdagent_dirs=[kd])
    d = ck.check(Bash(), {"command": 'python -c "import os; os.remove(\'.kdagent/config.yaml\')"',
    })
    assert d.effect == "ask"


def test_bash_nonsensitive_rm_not_upgraded(tmp_path: Path) -> None:
    """正向不误伤：非敏感路径 `rm build/temp.log` 按矩阵走，非兜底 reason。"""
    kd = tmp_path / ".kdagent"
    kd.mkdir()
    ck = _checker(tmp_path, kdagent_dirs=[kd])
    d = ck.check(Bash(), {"command": "rm build/temp.log"})
    assert d.effect == "ask"
    assert "敏感路径" not in d.reason


def test_bash_read_kdagent_notes_no_extra_ask(tmp_path: Path) -> None:
    """读 kd 目录内非敏感文件不触发兜底（引用 kd 但未点名敏感项）。"""
    kd = tmp_path / ".kdagent"
    kd.mkdir()
    ck = _checker(tmp_path, mode="acceptEdits", kdagent_dirs=[kd])
    d = ck.check(Bash(), {"command": "cat .kdagent/notes.txt"})
    assert d.effect == "allow"  # acceptEdits 只读免审批语义保持


def test_bypass_still_skips_fallback_ask(tmp_path: Path) -> None:
    """bypassPermissions 跳过兜底 ask（对齐 L2-L5 跳过语义；L1/敏感 deny 不豁免）。"""
    kd = tmp_path / ".kdagent"
    kd.mkdir()
    ck = _checker(tmp_path, mode="bypassPermissions", kdagent_dirs=[kd])
    d = ck.check(Bash(), {"command": "cd .kdagent && rm permissions.local.yaml"})
    assert d.effect == "allow"  # bypass 用户显式选择；deny 层（写语法提取）仍生效


# ---- symlink 绕 skills 禁写（containment 对称修复）--------------------------


def test_symlink_into_skills_denied(tmp_path: Path) -> None:
    """work_dir 内 symlink 指向 .kdagent/skills/ → resolved 判定命中禁写 deny。"""
    kd = tmp_path / ".kdagent"
    skills = kd / "skills"
    skills.mkdir(parents=True)
    link = tmp_path / "link"
    try:
        link.symlink_to(skills, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境无 symlink 权限（Windows 需管理员/开发者模式）")
    ck = _checker(tmp_path, kdagent_dirs=[kd])
    d = ck.check(WriteFile(), {"path": str(link / "x.md"), "content": "x"})
    assert d.effect == "deny"
    assert "敏感路径禁写" in d.reason


# ---- L3 deny 大小写绕过 -----------------------------------------------------


def test_deny_rule_matches_uppercase_extension(tmp_path: Path) -> None:
    """deny `ReadFile(*.env*)` 必须命中 `.ENV`（Windows 文件系统大小写语义）。"""
    eng = RuleEngine()
    eng.add(PermissionRule.parse("ReadFile(*.env*)", "deny"))
    effect, _ = eng.evaluate("ReadFile", str(tmp_path / "config.ENV"))
    assert effect == "deny"


# ---- 子 Agent HITL 收口（permissionMode 裁决）-------------------------------


def test_subsink_permission_mode_verdicts() -> None:
    """dontAsk/未声明 → allow；default/acceptEdits → deny（fail-closed）。"""
    async def scenario() -> None:
        for mode, expect in (
            ("dontAsk", "allow"),
            ("", "allow"),  # 未声明 = 向后兼容
            ("default", "deny"),
            ("acceptEdits", "deny"),
        ):
            fut = asyncio.get_running_loop().create_future()
            sink = _SubSink(permission_mode=mode)
            sink(PermissionRequestEvent(tool_name="Bash", summary="s", future=fut))
            assert fut.result() == expect, mode

    asyncio.run(scenario())


def test_subsink_default_missing_allow_regression() -> None:
    """默认构造（无 permissionMode 参数）= 未声明语义 → allow（旧用法不破坏）。"""
    async def scenario() -> None:
        fut = asyncio.get_running_loop().create_future()
        sink = _SubSink()
        sink(PermissionRequestEvent(tool_name="Bash", summary="s", future=fut))
        assert fut.result() == "allow"

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_runner_declared_default_deny_flows_to_history(tmp_path: Path) -> None:
    """声明 default 的子 Agent：HITL deny → is_error 进历史 → 重决策后收尾。"""
    llm = FakeLLM(
        [
            tool_call("Bash", {"command": "git push"}, id_="r1"),
            done("push 被拒，已停止"),
        ]
    )
    runner = SubAgentRunner(
        llm=llm,
        tools=build_default_registry(),
        config=Config(),
        work_dir=tmp_path,
    )
    definition = AgentDef(
        name="strict", description="d", permission_mode="default", system_prompt="s"
    )
    result = await runner.run_to_completion(definition, "push")
    assert not result.is_error  # 拒绝进历史重决策，子 Agent 正常收尾（D23）
    assert llm.call_count == 2  # 第二轮基于拒绝反馈继续
    assert "push 被拒" in result.text


@pytest.mark.asyncio
async def test_runner_declared_dontask_auto_allows(tmp_path: Path) -> None:
    """声明 dontAsk 的子 Agent：HITL 自动放行（10 §3.7 全自动语义保持）。"""
    llm = FakeLLM(
        [
            tool_call("Bash", {"command": "echo done"}, id_="r1"),
            done("ok"),
        ]
    )
    runner = SubAgentRunner(
        llm=llm,
        tools=build_default_registry(),
        config=Config(),
        work_dir=tmp_path,
    )
    definition = AgentDef(
        name="auto", description="d", permission_mode="dontAsk", system_prompt="s"
    )
    result = await runner.run_to_completion(definition, "run")
    assert "ok" in result.text
    assert llm.call_count == 2


# ---- Explore bash_readonly 门禁（机制级只读）--------------------------------


def _bash_readonly_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for tool in build_default_registry().all():
        reg.register(tool)
    return filter_tools(
        reg, AgentDef(name="x", description="d", bash_readonly=True)
    )


def test_bash_readonly_guard_rejects_writes() -> None:
    """bashReadonly 定义：rm / sed -i / find -delete / 重定向均被拒。"""
    bash = _bash_readonly_registry().get("Bash")
    assert bash is not None
    assert bash.validate_input({"command": "rm -rf build"})
    assert bash.validate_input({"command": "sed -i 's/a/b/' f.txt"})
    assert bash.validate_input({"command": 'find . -name "*.pyc" -delete'})
    assert bash.validate_input({"command": "echo x > out.txt"})


def test_bash_readonly_guard_allows_readonly_pipelines() -> None:
    """只读命令与其管道/组合放行（`grep | wc`、`ls && cat`）。"""
    bash = _bash_readonly_registry().get("Bash")
    assert bash is not None
    assert bash.validate_input({"command": "grep -r foo src | wc -l"}) == []
    assert bash.validate_input({"command": "ls -la && cat setup.py"}) == []
    assert bash.validate_input({"command": "cat a.txt b.txt"}) == []


def test_bash_readonly_guard_rejects_git_write_variants() -> None:
    """git 不在只读白名单：机制无法区分 git log 与 git push，整体拒绝（保守）。"""
    bash = _bash_readonly_registry().get("Bash")
    assert bash is not None
    assert bash.validate_input({"command": "git log --oneline -5"})


def test_explore_builtin_declares_bash_readonly() -> None:
    """内置 Explore 声明 bashReadonly: true（机制级，非仅提示词）。"""
    import kdagent.subagent.manager as mgr_mod

    builtin_dir = Path(mgr_mod.__file__).parent / "builtin"
    d = parse_agent_file(builtin_dir / "explore.md")
    assert d is not None
    assert d.bash_readonly is True


def test_bash_readonly_false_by_default() -> None:
    """未声明 bashReadonly → 不包装（其余 Agent 行为不变，原实例直通）。"""
    reg = ToolRegistry()
    bash_tool = Bash()
    reg.register(bash_tool)
    out = filter_tools(reg, AgentDef(name="y", description="d"))
    assert out.get("Bash") is bash_tool
