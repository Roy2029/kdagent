"""07 日志脱敏与摘要测试（规格 07 §3.4）。"""

from __future__ import annotations

from kdagent.engine.llm.base import Payload
from kdagent.engine.messages import Message as EngineMessage
from kdagent.engine.messages import TextBlock, ToolResultBlock
from kdagent.obs.log import incremental_payload_text, make_rules, redact, redact_dict, snapshot


def _payload(*blocks: tuple[str, object]) -> Payload:
    """(role, block) → Payload（一条消息一个 block）。"""
    return Payload(
        system="SYS", messages=[EngineMessage(role=role, content=[block]) for role, block in blocks]
    )


def test_snapshot_short_text_unchanged() -> None:
    assert snapshot("你好 world") == "你好 world"


def test_snapshot_long_text_has_tail_and_hint() -> None:
    text = "a" * 1000
    result = snapshot(text, limit=100)
    assert "仅摘要" in result
    assert "共 1000 字符" in result
    assert result.startswith("a" * 50)
    assert result.endswith("a" * 50)


def test_redact_no_rules_returns_same() -> None:
    assert redact("api_key=secret123", []) == "api_key=secret123"


def test_redact_applies_rules() -> None:
    rules = [(r"api_key=(\w+)", "api_key=***")]
    assert redact("api_key=secret123", rules) == "api_key=***"


def test_make_rules_skips_bad_regex() -> None:
    rules = make_rules({"[unclosed": "x", "a+": "b"})
    assert rules == [("a+", "b")]


def test_redact_dict_strings_only() -> None:
    rules = [(r"token", "***")]
    out = redact_dict({"k": "my token here", "n": 42, "nested": {"x": "token"}}, rules)
    assert out["k"] == "my *** here"
    assert out["n"] == 42
    assert out["nested"] == {"x": "token"}  # 仅一层：嵌套 dict 字符串不脱敏（设计取舍）


def test_redact_dict_no_rules_returns_same_object() -> None:
    d = {"a": "b"}
    assert redact_dict(d, []) is d


# ---- D90：增量 prompt 日志（每轮 llm.call 只记新增消息） ----


def test_incremental_first_turn_has_system_snapshot_and_all_messages() -> None:
    p = _payload(("user", TextBlock("题目A")), ("assistant", TextBlock("回复B")))
    text = incremental_payload_text(p, 0)
    assert text.startswith("[system]\nSYS")
    assert "[user]\n题目A" in text
    assert "[assistant]\n回复B" in text


def test_incremental_second_turn_only_delta() -> None:
    p = _payload(
        ("user", TextBlock("题目A")),
        ("assistant", TextBlock("回复B")),
        ("user", TextBlock("题目C")),
    )
    text = incremental_payload_text(p, 2)  # 前两条已记录 → 只出新增
    assert "[system]" not in text
    assert "题目A" not in text and "回复B" not in text
    assert "[user]\n题目C" in text


def test_incremental_tool_result_rendered() -> None:
    p = _payload(("user", ToolResultBlock(tool_use_id="t1", content="工具输出")))
    text = incremental_payload_text(p, 0)
    assert "[user:tool_result:t1]\n工具输出" in text


def test_incremental_long_block_truncated() -> None:
    p = _payload(("user", ToolResultBlock(tool_use_id="t1", content="x" * 20000)))
    text = incremental_payload_text(p, 0)
    assert "已截断" in text
    assert len(text) < 20000


def test_incremental_no_system_when_include_false() -> None:
    p = _payload(("user", TextBlock("A")))
    text = incremental_payload_text(p, 0, include_system=False)
    assert "[system]" not in text
