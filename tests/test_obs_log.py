"""07 日志脱敏与摘要测试（规格 07 §3.4）。"""

from __future__ import annotations

from kdagent.obs.log import make_rules, redact, redact_dict, snapshot


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
