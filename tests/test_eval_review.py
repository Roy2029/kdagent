"""复核界面（11 §3.4 T30 增量）数据/渲染层测试。

span 树构建/三类过滤定位/事件流阅读/批注读写/归类合并——纯函数 + 文件读写。
"""

from __future__ import annotations

from pathlib import Path

from kdagent.eval.model import FailureCase
from kdagent.eval.review import (
    Annotation,
    build_span_tree,
    focus_labels,
    focus_spans,
    load_annotations,
    merged_kind,
    render_failure_index,
    render_span_tree,
    save_annotation,
    span_detail,
    span_summary,
    spans_in_tree_order,
)
from kdagent.obs.model import Span, Trace

# ---- fixtures ----

def _span(
    span_id: str,
    name: str,
    kind: str,
    *,
    parent: str | None = None,
    status: str = "ok",
    attrs: dict | None = None,
    duration_ms: int = 0,
) -> Span:
    return Span(
        span_id=span_id,
        trace_id="t",
        parent_span_id=parent,
        name=name,
        kind=kind,
        status=status,  # type: ignore[arg-type]
        duration_ms=duration_ms,
        attributes=dict(attrs or {}),
    )


def _trace(*spans: Span) -> Trace:
    return Trace(
        trace_id="t",
        session_id="s",
        user_input_snapshot="题目",
        root_span_id="",
        spans=list(spans),
    )


def _review_trace() -> Trace:
    """典型复核 trace：root llm → 权限 → 工具成功 → 工具失败 → 压缩点。"""
    return _trace(
        _span("r", "llm.call", "client"),
        _span("p", "permission.check", "permission", parent="r", attrs={"tool": "Bash", "effect": "allow"}),
        _span("t1", "tool.exec", "tool", parent="r", attrs={"tool": "ReadFile", "input": {"path": "a.py"}}, duration_ms=3),
        _span("t2", "tool.exec", "tool", parent="r", status="error", attrs={"tool": "EditFile", "input": {"path": "a.py"}, "is_error": True, "output": "未在文件中找到"}, duration_ms=12),
        _span("c", "context.compact", "context", parent="r"),
    )


# ---- span 树 ----

def test_build_span_tree_restores_parent_child() -> None:
    trace = _review_trace()
    roots = build_span_tree(trace)
    assert len(roots) == 1
    assert roots[0].span.span_id == "r"
    assert [c.span.span_id for c in roots[0].children] == ["p", "t1", "t2", "c"]


def test_build_span_tree_orphan_becomes_root() -> None:
    trace = _trace(_span("a", "x", "tool"), _span("o", "orphan", "tool", parent="ghost"))
    roots = build_span_tree(trace)
    assert {r.span.span_id for r in roots} == {"a", "o"}  # 脏 parent 不阻断排查


def test_spans_in_tree_order_depth_first() -> None:
    ordered = spans_in_tree_order(_review_trace())
    assert [(d, s.span_id) for d, s in ordered] == [
        (0, "r"), (1, "p"), (1, "t1"), (1, "t2"), (1, "c"),
    ]


def test_spans_in_tree_order_empty() -> None:
    assert spans_in_tree_order(_trace()) == []


# ---- 三类定位过滤 ----

def test_focus_spans_error_finds_failed_tool() -> None:
    hits = focus_spans(_review_trace(), "error")
    assert [s.span_id for s in hits] == ["t2"]


def test_focus_spans_compact_finds_compaction() -> None:
    hits = focus_spans(_review_trace(), "compact")
    assert [s.span_id for s in hits] == ["c"]


def test_focus_spans_permission_finds_permission_check() -> None:
    hits = focus_spans(_review_trace(), "permission")
    assert [s.span_id for s in hits] == ["p"]


def test_focus_spans_unknown_raises() -> None:
    try:
        focus_spans(_review_trace(), "nope")
    except ValueError as exc:
        assert "未知过滤类型" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应抛 ValueError")


def test_focus_labels_three_kinds() -> None:
    assert len(focus_labels()) == 3
    assert all(len(t) == 2 for t in focus_labels())


# ---- 渲染 ----

def test_span_summary_tool_shows_name_and_error() -> None:
    assert "tool:EditFile" in span_summary(_span("s", "tool.exec", "tool", status="error", attrs={"tool": "EditFile"}, duration_ms=12))
    assert "[error]" in span_summary(_span("s", "tool.exec", "tool", status="error", attrs={"tool": "EditFile"}))


def test_span_summary_llm_plain() -> None:
    summary = span_summary(_span("s", "llm.call", "client"))
    assert summary == "llm.call(client)"


def test_render_span_tree_indents_and_marks() -> None:
    text = render_span_tree(_review_trace(), mark_ids={"t2"})
    assert "- llm.call(client)" in text
    assert "  - tool:EditFile(tool) 12ms [error] ←" in text  # 失败工具命中高亮
    assert "  - tool:ReadFile(tool) 3ms" in text


def test_render_span_tree_empty() -> None:
    assert render_span_tree(_trace()) == "(无 span)"


def test_span_detail_extracts_input_output_and_status() -> None:
    detail = span_detail(_span("s", "tool.exec", "tool", status="error", attrs={"tool": "EditFile", "input": {"path": "a.py"}, "output": "未在文件中找到", "is_error": True}))
    assert "[error]" in detail
    assert '"path": "a.py"' in detail  # input 逐项挑出
    assert "未在文件中找到" in detail  # output 逐项挑出


def test_span_detail_truncates_long_values() -> None:
    detail = span_detail(_span("s", "tool.exec", "tool", attrs={"output": "x" * 2000}))
    assert "…" in detail


def test_span_detail_no_visible_attrs() -> None:
    detail = span_detail(_span("s", "llm.call", "client"))
    assert "（无可见属性）" in detail


# ---- 批注 ----

def test_annotations_roundtrip(tmp_path: Path) -> None:
    obs_dir = tmp_path / "obs"
    path = save_annotation(obs_dir, "run-1", "task-a", "wrong_fix", note="人工复核过")
    assert path.exists()
    assert not path.with_suffix(".tmp").exists()  # 原子写不留 .tmp
    loaded = load_annotations(obs_dir, "run-1")
    assert loaded["task-a"].kind == "wrong_fix"
    assert loaded["task-a"].note == "人工复核过"


def test_annotations_multiple_and_overwrite(tmp_path: Path) -> None:
    obs_dir = tmp_path / "obs"
    save_annotation(obs_dir, "run-1", "task-a", "wrong_fix")
    save_annotation(obs_dir, "run-1", "task-b", "not_located")
    save_annotation(obs_dir, "run-1", "task-a", "regression")  # 覆盖同题
    loaded = load_annotations(obs_dir, "run-1")
    assert loaded["task-a"].kind == "regression"
    assert loaded["task-b"].kind == "not_located"
    assert len(loaded) == 2


def test_load_annotations_missing_file_empty(tmp_path: Path) -> None:
    assert load_annotations(tmp_path / "obs", "run-1") == {}


def test_load_annotations_corrupt_json_empty(tmp_path: Path) -> None:
    path = tmp_path / "obs" / "eval" / "run-1"
    path.mkdir(parents=True)
    (path / "annotations.json").write_text("{bad json", encoding="utf-8")
    assert load_annotations(tmp_path / "obs", "run-1") == {}


# ---- 归类合并 ----

def test_merged_kind_annotation_overrides_auto(tmp_path: Path) -> None:
    obs_dir = tmp_path / "obs"
    save_annotation(obs_dir, "run-1", "task-a", "wrong_fix")
    kind, annotation = merged_kind(obs_dir, "run-1", "task-a", "not_located")
    assert kind == "wrong_fix"
    assert annotation.kind == "wrong_fix"


def test_merged_kind_no_annotation_keeps_auto(tmp_path: Path) -> None:
    kind, annotation = merged_kind(tmp_path / "obs", "run-1", "task-a", "harness_fault")
    assert kind == "harness_fault"
    assert annotation.kind is None


# ---- 失败题索引 ----

def test_render_failure_index_plain() -> None:
    case = FailureCase(instance_id="task-a", kind="not_located", reason="没定位到该改的文件")
    line = render_failure_index(case, Annotation(), 1)
    assert "task-a [not_located] 没定位到该改的文件（1 失败事件）" in line


def test_render_failure_index_with_annotation(tmp_path: Path) -> None:
    obs_dir = tmp_path / "obs"
    save_annotation(obs_dir, "run-1", "task-a", "wrong_fix", note="修法不对，非定位问题")
    case = FailureCase(instance_id="task-a", kind="not_located", reason="没定位到该改的文件")
    _, annotation = merged_kind(obs_dir, "run-1", "task-a", case.kind)
    line = render_failure_index(case, annotation, 2)
    assert "[人工改判]" in line
    assert "备注：修法不对，非定位问题" in line
