"""Hook 条件语法（规格 06 §3.10）：操作符 / 组合 / 字段 / 变量替换。"""

from __future__ import annotations

import pytest

from kdagent.hooks.conditions import ConditionError, expand_variables, parse_condition
from kdagent.hooks.engine_types import HookContext


def _ctx(**kw) -> HookContext:
    return HookContext(event="post_tool_use", **kw)


def test_exact_operators() -> None:
    c = parse_condition('tool == "WriteFile"')
    assert c.matches(_ctx(tool_name="WriteFile"))
    assert not c.matches(_ctx(tool_name="Bash"))

    c = parse_condition('tool != "WriteFile"')
    assert c.matches(_ctx(tool_name="Bash"))
    assert not c.matches(_ctx(tool_name="WriteFile"))


def test_glob_and_regex() -> None:
    c = parse_condition('args.path ~= "*.py"')
    assert c.matches(_ctx(tool_args={"path": "/proj/main.py"}))
    assert not c.matches(_ctx(tool_args={"path": "/proj/main.md"}))

    c = parse_condition(r'args.path =~ "\.env$"')
    assert c.matches(_ctx(tool_args={"path": "/proj/.env"}))
    assert not c.matches(_ctx(tool_args={"path": "/proj/.env.example"}))


def test_and_combine() -> None:
    c = parse_condition('tool == "WriteFile" && args.path ~= "*.py"')
    assert c.matches(_ctx(tool_name="WriteFile", tool_args={"path": "a.py"}))
    assert not c.matches(_ctx(tool_name="WriteFile", tool_args={"path": "a.md"}))
    assert not c.matches(_ctx(tool_name="Bash", tool_args={"path": "a.py"}))


def test_or_combine() -> None:
    c = parse_condition('tool == "Bash" || args.path ~= "*.env"')
    assert c.matches(_ctx(tool_name="Bash"))
    assert c.matches(_ctx(tool_args={"path": "x.env"}))
    assert not c.matches(_ctx(tool_name="ReadFile", tool_args={"path": "x.py"}))


def test_mixed_combine_raises() -> None:
    with pytest.raises(ConditionError):
        parse_condition('tool == "Bash" && args.path ~= "*.py" || event == "turn_end"')


def test_unknown_field_returns_empty() -> None:
    c = parse_condition('args.nonexistent == "x"')
    assert not c.matches(_ctx())
    # 未知字段返回空串 → 与空串 == 匹配（不报错语义）
    c2 = parse_condition('args.nonexistent == ""')
    assert c2.matches(_ctx())


def test_invalid_syntax_raises() -> None:
    with pytest.raises(ConditionError):
        parse_condition("tool WriteFile")  # 缺操作符
    with pytest.raises(ConditionError):
        parse_condition("   ")


def test_expand_variables() -> None:
    ctx = _ctx(
        tool_name="Bash",
        file_path="/proj/main.py",
        message="编译失败",
        error="boom",
        tool_args={"cmd": "git push", "n": 3},
    )
    text = "tool=$TOOL_NAME path=$FILE_PATH msg=$MESSAGE err=$ERROR arg=$TOOL_ARGS.cmd n=$TOOL_ARGS.n missing=$NOPE"
    out = expand_variables(text, ctx)
    assert "tool=Bash" in out
    assert "path=/proj/main.py" in out
    assert "msg=编译失败" in out
    assert "err=boom" in out
    assert "arg=git push" in out
    assert "n=3" in out
    assert "$NOPE" not in out  # 未定义替换为空串，不报错
