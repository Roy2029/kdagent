"""trace_html 渲染测试（评估 trace 阅读工具，D87 增量）。

覆盖：单 trace → HTML（判定徽章/span 卡片/错误高亮/HTML 转义）、
单文件/目录 CLI 落盘、脏 jsonl 跳过。
"""

from __future__ import annotations

from pathlib import Path

from kdagent.eval.trace_html import (
    convert_dir,
    convert_file,
    main,
    render_trace_html,
)
from kdagent.obs.model import Span, SpanLog, Trace


def _make_trace(
    *,
    trace_id: str = "t1",
    attrs: dict | None = None,
    with_error: bool = False,
) -> Trace:
    spans = [
        Span(
            span_id="s1",
            trace_id=trace_id,
            parent_span_id=None,
            name="llm.call",
            kind="client",
            status="ok",
            duration_ms=100,
            attributes={"input_tokens": 10, "output_tokens": 5, "model": "m"},
        ),
        Span(
            span_id="s2",
            trace_id=trace_id,
            parent_span_id="s1",
            name="tool.exec",
            kind="tool",
            status="error" if with_error else "ok",
            duration_ms=50,
            attributes={"tool": "Bash", "command": "ls", "output": "<unclosed"},
        ),
    ]
    return Trace(
        trace_id=trace_id,
        session_id="",
        user_input_snapshot="问题描述：修复 bug",
        root_span_id="s1",
        spans=spans,
        attributes=attrs or {"eval.run_id": "r", "eval.task_id": "t1"},
    )


# ---- 单 trace 渲染 ----

def test_render_contains_trace_id_and_spans() -> None:
    html = render_trace_html(_make_trace())
    assert "t1" in html
    assert "llm.call" in html
    assert "tool:Bash" in html


def test_render_verdict_badges() -> None:
    passed = render_trace_html(_make_trace(attrs={"eval.run_id": "r", "eval.passed": True}))
    assert "通过" in passed and 'class="verdict pass"' in passed
    failed = render_trace_html(_make_trace(attrs={"eval.run_id": "r", "eval.passed": False}))
    assert "失败" in failed and 'class="verdict fail"' in failed
    unknown = render_trace_html(_make_trace(attrs={"eval.run_id": "r"}))
    assert "未判分" in unknown and 'class="verdict none"' in unknown


def test_render_error_span_highlighted() -> None:
    html = render_trace_html(_make_trace(with_error=True))
    assert 'class="span err"' in html
    assert "error" in html


def test_render_escapes_injection() -> None:
    """input/output 含 HTML/脚本不被注入，且完整保留。"""
    t = _make_trace(attrs={"eval.run_id": "r"})
    t.spans[1].attributes["output"] = "<script>alert(1)</script>"
    html = render_trace_html(t)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_token_badge() -> None:
    html = render_trace_html(_make_trace())
    assert "in 10" in html and "out 5" in html


def test_render_span_logs_shows_prompt() -> None:
    """D89：llm.call 的 prompt 内容（span debug 日志）渲染进卡片。"""
    t = _make_trace()
    t.spans[0].logs = [SpanLog(level="debug", message="[system]\n你是助手，负责评估")]
    html = render_trace_html(t)
    assert "log[debug]" in html
    assert "你是助手，负责评估" in html


def test_render_log_splits_into_dialog_blocks() -> None:
    """D90：日志按 [role] 标记切块渲染成对话历史（角色配色）。"""
    t = _make_trace()
    t.spans[0].logs = [
        SpanLog(
            level="debug",
            message="[user]\n题目：修 bug\n\n[assistant]\n我先读代码\n\n[user:tool_result:t1]\n读取结果",
        )
    ]
    html = render_trace_html(t)
    assert 'class="logblock user"' in html
    assert 'class="logblock assistant"' in html
    # tool_result 即便 role=user 也要染工具色（_log_role）
    assert 'class="logblock tool"' in html
    assert "题目：修 bug" in html
    assert "我先读代码" in html
    assert "读取结果" in html


def test_render_log_no_tag_plain_block() -> None:
    """无 [role] 标记的日志整段返回（不炸）。"""
    t = _make_trace()
    t.spans[0].logs = [SpanLog(level="debug", message="纯文本日志")]
    html = render_trace_html(t)
    assert "纯文本日志" in html
    assert 'class="logblock "' in html


# ---- 文件/目录落盘 ----

def test_convert_file_writes_html(tmp_path: Path) -> None:
    jsonl = tmp_path / "t.jsonl"
    jsonl.write_text(_trace_lines(), encoding="utf-8")
    out = convert_file(jsonl, open_browser=False)
    assert out == jsonl.with_suffix(".html")
    text = out.read_text(encoding="utf-8")
    assert "llm.call" in text and "trace" in text


def test_convert_file_invalid_rejects(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"_type": "span", "name": "x"}\n', encoding="utf-8")  # 无 header
    try:
        convert_file(bad, open_browser=False)
    except ValueError as exc:
        assert "不是有效 trace" in str(exc)
    else:
        raise AssertionError("应拒绝缺 header 的 jsonl")


def test_convert_dir_writes_index_and_skips_dirty(tmp_path: Path) -> None:
    (tmp_path / "a.jsonl").write_text(_trace_lines("a"), encoding="utf-8")
    (tmp_path / "b.jsonl").write_text(_trace_lines("b"), encoding="utf-8")
    (tmp_path / "dirty.jsonl").write_text("{{{ 坏行\n", encoding="utf-8")
    index = convert_dir(tmp_path, open_browser=False)
    assert index == tmp_path / "index.html"
    assert (tmp_path / "a.html").is_file() and (tmp_path / "b.html").is_file()
    # 脏文件跳过，不阻塞
    assert not (tmp_path / "dirty.html").exists()
    assert "2 条" in index.read_text(encoding="utf-8")


# ---- CLI ----

def test_main_renders_file(tmp_path: Path, monkeypatch) -> None:
    jsonl = tmp_path / "t.jsonl"
    jsonl.write_text(_trace_lines(), encoding="utf-8")
    monkeypatch.setattr("kdagent.eval.trace_html.webbrowser", _NoOpBrowser())
    rc = main([str(jsonl), "--no-open"])
    assert rc == 0
    assert jsonl.with_suffix(".html").is_file()


def test_main_missing_path_rejects(tmp_path: Path) -> None:
    rc = main([str(tmp_path / "nope.jsonl"), "--no-open"])
    assert rc == 2


class _NoOpBrowser:
    def open(self, *_: object) -> bool:
        return True


# ---- 帮助 ----

def _trace_lines(trace_id: str = "t1") -> str:
    """一条合法 trace jsonl（header + 2 span 行）。"""
    return "\n".join(
        [
            json_dumps(
                {
                    "_type": "trace",
                    "trace_id": trace_id,
                    "session_id": "",
                    "user_input_snapshot": "问题描述：修复 bug",
                    "root_span_id": "s1",
                    "attributes": {"eval.run_id": "r", "eval.task_id": trace_id},
                }
            ),
            json_dumps(
                {
                    "_type": "span",
                    "span_id": "s1",
                    "trace_id": trace_id,
                    "parent_span_id": None,
                    "name": "llm.call",
                    "kind": "client",
                    "status": "ok",
                    "duration_ms": 100,
                    "attributes": {"input_tokens": 10, "output_tokens": 5},
                }
            ),
            json_dumps(
                {
                    "_type": "span",
                    "span_id": "s2",
                    "trace_id": trace_id,
                    "parent_span_id": "s1",
                    "name": "tool.exec",
                    "kind": "tool",
                    "status": "ok",
                    "duration_ms": 50,
                    "attributes": {"tool": "Bash", "output": "ls 输出"},
                }
            ),
        ]
    )


def json_dumps(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
