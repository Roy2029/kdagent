"""ToolRegistry 测试（规格 03 §3.4：重名 / schema 生成 / 并发 / 校验）。"""

from __future__ import annotations

import pytest

from kdagent.tools import ReadFile, build_default_registry


def test_register_duplicate_name_raises() -> None:
    reg = build_default_registry()
    with pytest.raises(ValueError, match="重名"):
        reg.register(ReadFile())


def test_build_default_registry_has_7_tools() -> None:
    reg = build_default_registry()
    names = {t.name for t in reg.all()}
    assert names == {"ReadFile", "WriteFile", "EditFile", "Glob", "Grep", "Bash", "TodoWrite"}
    assert len(reg.all()) == 7


def test_schemas_match_02_tool_schema() -> None:
    from kdagent.engine.llm.base import ToolSchema

    schemas = build_default_registry().schemas()
    assert all(isinstance(s, ToolSchema) for s in schemas)
    names = {s.name for s in schemas}
    assert "ReadFile" in names and "TodoWrite" in names
    # 每个 schema 的 input_schema 带 required
    read_file = next(s for s in schemas if s.name == "ReadFile")
    assert read_file.input_schema["required"] == ["path"]


def test_is_concurrency_safe_assignments() -> None:
    reg = build_default_registry()
    assert reg.is_concurrency_safe("ReadFile", {}) is True
    assert reg.is_concurrency_safe("Glob", {}) is True
    assert reg.is_concurrency_safe("Grep", {}) is True
    assert reg.is_concurrency_safe("WriteFile", {}) is False
    assert reg.is_concurrency_safe("EditFile", {}) is False
    assert reg.is_concurrency_safe("Bash", {}) is False
    assert reg.is_concurrency_safe("TodoWrite", {}) is False
    assert reg.is_concurrency_safe("不存在", {}) is False


def test_require_confirm_assignments() -> None:
    reg = build_default_registry()
    confirms = {t.name for t in reg.all() if t.require_confirm}
    assert confirms == {"WriteFile", "EditFile", "Bash"}


def test_validate_unknown_tool_returns_error() -> None:
    reg = build_default_registry()
    errors = reg.validate("不存在", {})
    assert errors and "不存在" in errors[0]


def test_validate_delegates_to_tool() -> None:
    reg = build_default_registry()
    assert reg.validate("ReadFile", {})  # path 缺失
    assert reg.validate("ReadFile", {"path": "相对路径.txt"})  # 非绝对路径
    assert reg.validate("ReadFile", {"path": "C:/tmp/a.txt"}) == []
