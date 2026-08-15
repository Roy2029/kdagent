"""Hook 引擎（规格 06 §3.10）：配置校验 / 匹配 / pre_tool 拦截 / once / 错误兜底。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kdagent.hooks.engine import HookEngine
from kdagent.hooks.engine_types import HookContext


def _hooks_yaml(items: list[dict]) -> dict:
    return {"hooks": items}


def test_validation_errors() -> None:
    cases = [
        # (错误 hook 配置, 错误片段)
        ({"id": "a", "event": "nope", "action": {"type": "command", "command": "x"}}, "未知事件"),
        ({"id": "a", "event": "post_tool_use", "action": {"type": "command", "command": "x"}, "reject": True}, "reject 仅限"),
        ({"id": "a", "event": "pre_tool_use", "action": {"type": "prompt", "prompt": "x"}, "async": True}, "async 禁用于"),
        ({"id": "a", "event": "turn_end", "action": {"type": "command"}}, "缺必填字段"),
        ({"id": "a", "event": "turn_end", "action": {"type": "ssh"}}, "未知 action.type"),
        ({"id": "a", "event": "turn_end", "action": {"type": "http"}}, "缺必填字段"),
    ]
    for item, fragment in cases:
        eng = HookEngine()
        with pytest.raises(ValueError, match=fragment):
            eng.load(_hooks_yaml([item]))


def test_load_ok_and_order() -> None:
    eng = HookEngine()
    eng.load(
        _hooks_yaml(
            [
                {"id": "a", "event": "turn_end", "action": {"type": "prompt", "prompt": "A"}},
                {"id": "b", "event": "turn_end", "action": {"type": "prompt", "prompt": "B"}},
            ]
        )
    )
    assert [h.id for h in eng.hooks] == ["a", "b"]


def test_prompt_action_injects_with_vars() -> None:
    injected: list[str] = []
    eng = HookEngine(prompt_inject=injected.append)
    eng.load(
        _hooks_yaml(
            [
                {
                    "id": "remind",
                    "event": "post_tool_use",
                    "if": 'tool == "WriteFile" && args.path ~= "*.py"',
                    "action": {"type": "prompt", "prompt": "[system-reminder] 已修改 $FILE_PATH"},
                }
            ]
        )
    )
    ctx = HookContext(event="post_tool_use", tool_name="WriteFile", tool_args={"path": "/p/a.py"}, file_path="/p/a.py")
    eng.run("post_tool_use", ctx)
    assert injected == ["[system-reminder] 已修改 /p/a.py"]
    # 条件不命中 → 不注入
    ctx2 = HookContext(event="post_tool_use", tool_name="WriteFile", tool_args={"path": "/p/a.md"})
    eng.run("post_tool_use", ctx2)
    assert len(injected) == 1


def test_condition_filter() -> None:
    calls: list[str] = []
    eng = HookEngine(prompt_inject=lambda _: calls.append("fired"))
    eng.load(_hooks_yaml([{"id": "a", "event": "turn_start", "if": 'tool == "Bash"', "action": {"type": "prompt", "prompt": "x"}}]))
    eng.run("turn_start", HookContext(event="turn_start", tool_name="ReadFile"))
    assert calls == []
    eng.run("turn_start", HookContext(event="turn_start", tool_name="Bash"))
    assert calls == ["fired"]


def test_pre_tool_reject_short_circuits() -> None:
    injected: list[str] = []
    eng = HookEngine(prompt_inject=injected.append)
    eng.load(
        _hooks_yaml(
            [
                {"id": "pre1", "event": "pre_tool_use", "action": {"type": "prompt", "prompt": "pre1"}},
                {
                    "id": "block",
                    "event": "pre_tool_use",
                    "reject": True,
                    "action": {"type": "prompt", "prompt": "禁止 Bash"},
                },
                {"id": "pre2", "event": "pre_tool_use", "action": {"type": "prompt", "prompt": "pre2"}},
            ]
        )
    )
    reject = eng.run_pre_tool(HookContext(event="pre_tool_use", tool_name="Bash"))
    assert reject is not None
    assert reject.reason == "禁止 Bash"
    # 短路：pre2 未跑；pre1 在 reject 前已调度（无事件循环时同步跑完）。
    assert injected == ["pre1"]


def test_pre_tool_no_reject() -> None:
    eng = HookEngine()
    eng.load(_hooks_yaml([{"id": "a", "event": "pre_tool_use", "action": {"type": "prompt", "prompt": "x"}}]))
    reject = eng.run_pre_tool(HookContext(event="pre_tool_use", tool_name="Bash"))
    assert reject is None


def test_once_fires_only_once() -> None:
    injected: list[str] = []
    eng = HookEngine(prompt_inject=injected.append)
    eng.load(_hooks_yaml([{"id": "once", "event": "startup", "once": True, "action": {"type": "prompt", "prompt": "hi"}}]))
    for _ in range(3):
        eng.run("startup", HookContext(event="startup"))
    assert injected == ["hi"]


def test_command_action_runs(tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    forward = str(out).replace("\\", "/")
    eng = HookEngine()
    eng.load(_hooks_yaml([{"id": "c", "event": "turn_end", "action": {"type": "command", "command": f"echo ok > {forward}"}}]))
    eng.run("turn_end", HookContext(event="turn_end"))
    assert out.read_text(encoding="utf-8").strip() == "ok"


def test_command_error_logs_not_raises() -> None:
    errors: list[str] = []
    eng = HookEngine(on_error=errors.append)
    eng.load(_hooks_yaml([{"id": "bad", "event": "turn_end", "action": {"type": "command", "command": "exit 1"}}]))
    eng.run("turn_end", HookContext(event="turn_end"))
    # exit 1 不抛异常；hook 正常结束。
    assert errors == []


def test_run_async_deterministic() -> None:
    injected: list[str] = []
    eng = HookEngine(prompt_inject=injected.append)
    eng.load(_hooks_yaml([{"id": "a", "event": "error", "action": {"type": "prompt", "prompt": "err: $ERROR"}}]))
    asyncio.run(eng.run_async("error", HookContext(event="error", error="boom")))
    assert injected == ["err: boom"]


def test_load_file_missing_is_empty(tmp_path: Path) -> None:
    eng = HookEngine()
    eng.load_file(tmp_path / "nope.yaml")
    assert eng.hooks == []
