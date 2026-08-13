"""工具分批执行测试（规格 02 §3.7：is_concurrency_safe 划分并发/串行批）。"""

from __future__ import annotations

from kdagent.engine.agent import partition_tool_calls
from kdagent.engine.messages import ToolUseBlock
from kdagent.tools import build_default_registry


def _use(id_: str, name: str) -> ToolUseBlock:
    return ToolUseBlock(id=id_, name=name, input={})


def test_partition_mixed_concurrency() -> None:
    reg = build_default_registry()
    uses = [
        _use("1", "ReadFile"),
        _use("2", "ReadFile"),
        _use("3", "EditFile"),
        _use("4", "ReadFile"),
        _use("5", "ReadFile"),
    ]
    batches = partition_tool_calls(uses, reg)
    assert len(batches) == 3
    assert batches[0].is_concurrency_safe is True
    assert [b.id for b in batches[0].calls] == ["1", "2"]
    assert batches[1].is_concurrency_safe is False
    assert batches[1].calls[0].id == "3"
    assert batches[2].is_concurrency_safe is True
    assert [b.id for b in batches[2].calls] == ["4", "5"]


def test_partition_unknown_tool_is_serial() -> None:
    reg = build_default_registry()
    batches = partition_tool_calls([_use("a", "NoSuchTool")], reg)
    assert len(batches) == 1
    assert batches[0].is_concurrency_safe is False  # 未知工具保守串行


def test_partition_keeps_each_tool_isolated_between_serial_groups() -> None:
    reg = build_default_registry()
    uses = [
        _use("1", "Grep"),
        _use("2", "WriteFile"),
        _use("3", "Grep"),
    ]
    batches = partition_tool_calls(uses, reg)
    assert len(batches) == 3  # 并发批不能跨越串行批合并
    assert [b.calls[0].id for b in batches] == ["1", "2", "3"]
