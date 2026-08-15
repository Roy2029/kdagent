"""记忆存储测试（08 §3.2）：四类目录 + MEMORY.md 索引维护 + 路径安全。"""

from __future__ import annotations

from pathlib import Path

from kdagent.memory.model import (
    INDEX_MAX_BYTES,
    INDEX_MAX_LINES,
    MemoryFile,
    parse_memory,
    serialize_memory,
)
from kdagent.memory.store import MemoryStore


def _store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "user-memory", tmp_path / "proj" / ".kdagent" / "memory")


def _mem(name: str, type_: str = "project", desc: str = "测试记忆", content: str = "正文") -> MemoryFile:
    return MemoryFile(name=name, description=desc, type=type_, content=content)  # type: ignore[arg-type]


# ---- model：序列化/解析 ----

def test_serialize_roundtrip() -> None:
    f = _mem("proj-notes", "project", "项目笔记", "**Why:** 保持简单\n**How:** 复用")
    text = serialize_memory(f)
    assert text.startswith("---\nname: proj-notes")
    assert "type: project" in text
    parsed = parse_memory(text)
    assert parsed is not None
    assert parsed.name == "proj-notes"
    assert parsed.description == "项目笔记"
    assert parsed.type == "project"
    assert "复用" in parsed.content


def test_parse_invalid_returns_none() -> None:
    assert parse_memory("没有 frontmatter 的纯文本") is None
    assert parse_memory("---\nnot: yaml: [\n---\n正文") is None
    assert parse_memory("---\nname: x\ntype: nope\n---\n正文") is None


# ---- store：四类落盘目录 ----

def test_user_types_go_to_user_root(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.create(_mem("user-prefers-any", "user"))
    assert s.create(_mem("feedback-typo", "feedback"))
    assert s.create(_mem("proj-lang", "project"))
    assert s.create(_mem("ref-link", "reference"))
    assert (tmp_path / "user-memory" / "user-prefers-any.md").is_file()
    assert (tmp_path / "user-memory" / "feedback-typo.md").is_file()
    assert (tmp_path / "proj" / ".kdagent" / "memory" / "proj-lang.md").is_file()
    assert (tmp_path / "proj" / ".kdagent" / "memory" / "ref-link.md").is_file()


def test_list_combines_both_roots(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.create(_mem("a", "user"))
    s.create(_mem("b", "project"))
    names = {f.name for f in s.list_all()}
    assert names == {"a", "b"}


def test_create_dedup_and_update(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.create(_mem("x", "project"))
    assert not s.create(_mem("x", "project"))  # 重名拒绝
    assert s.update(MemoryFile(name="x", description="改", type="project", content="新正文"))
    f = s.read("x")
    assert f is not None and f.content == "新正文"
    assert not s.update(_mem("nope", "project"))  # 不存在拒绝


def test_delete_removes_file_and_index_line(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.create(_mem("x", "project", "要删的"))
    assert s.delete("x")
    assert s.read("x") is None
    assert not (tmp_path / "proj" / ".kdagent" / "memory" / "x.md").exists()
    index = (tmp_path / "proj" / ".kdagent" / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    assert "x" not in index
    assert not s.delete("x")


# ---- store：MEMORY.md 索引维护 ----

def test_index_appends_and_dedups(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.create(_mem("a", "project", "记忆 A"))
    s.update(MemoryFile(name="a", description="记忆 A", type="project", content="v2"))
    index = (tmp_path / "proj" / ".kdagent" / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    assert index.count("a.md") == 1


def test_index_markdown_joins_both_scopes(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.create(_mem("ua", "user", "用户偏好"))
    s.create(_mem("pa", "project", "项目知识"))
    md = s.index_markdown()
    assert "用户偏好" in md and "项目知识" in md
    assert md.startswith("## MEMORY.md")


def test_index_truncates_to_line_limit(tmp_path: Path) -> None:
    s = _store(tmp_path)
    root = tmp_path / "proj" / ".kdagent" / "memory"
    s.ensure()
    lines = [f"- [m{i}](m{i}.md) — 第{i}条" for i in range(INDEX_MAX_LINES + 50)]
    s._write_index(root, lines)
    assert len((root / "MEMORY.md").read_text(encoding="utf-8").splitlines()) <= INDEX_MAX_LINES


def test_index_truncates_to_byte_limit(tmp_path: Path) -> None:
    s = _store(tmp_path)
    root = tmp_path / "proj" / ".kdagent" / "memory"
    s.ensure()
    lines = [f"- [m{i}](m{i}.md) — {'x' * 400}" for i in range(100)]  # 每行 ~430B，超 25KB
    s._write_index(root, lines)
    assert (root / "MEMORY.md").stat().st_size <= INDEX_MAX_BYTES + 1


# ---- store：路径安全 + apply_ops ----

def test_name_traversal_blocked(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert not s.create(_mem("../../evil", "project"))
    assert not (tmp_path / "evil.md").exists()


def test_apply_ops_create_update_delete(tmp_path: Path) -> None:
    s = _store(tmp_path)
    report = s.apply_ops(
        [
            {"action": "create", "name": "a", "type": "feedback", "description": "纠正", "content": "用 X"},
            {"action": "create", "name": "b", "type": "project", "description": "知识", "content": "Y"},
            {"action": "update", "name": "a", "type": "feedback", "description": "纠正", "content": "用 Z"},
            {"action": "delete", "name": "b"},
            {"action": "create", "name": "../evil", "type": "project"},
        ]
    )
    assert report.created == ["a", "b"]
    assert report.updated == ["a"]
    assert report.deleted == ["b"]
    assert report.skipped == ["../evil"]
    a = s.read("a")
    assert a is not None and a.content == "用 Z"
    assert s.read("b") is None
