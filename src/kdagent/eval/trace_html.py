"""trace jsonl → 易读 HTML（评估 trace 阅读工具）。

把单条/整目录的 trace jsonl 渲染成零依赖、双击即开的静态 HTML（深色主题，
span 卡片可折叠、可搜索过滤、错误高亮），让人不用手翻 jsonl 也能看
「每道题怎么解的 + 判定结论」。复用 `review.py` 的树遍历/摘要逻辑。

用法：

    # 单文件（拖拽/命令行均可）
    python -m kdagent.eval.trace_html path/to/trace.jsonl

    # 目录（每个 trace 一个 html + index.html 索引）
    python -m kdagent.eval.trace_html path/to/obs/traces

    # 多个/混合、不自动开浏览器
    python -m kdagent.eval.trace_html a.jsonl b.jsonl --no-open

输出：与输入同目录、同名 `.html`（目录模式附带 `index.html`）。
拖拽入口：`scripts/trace2html.bat`（Windows 把 jsonl 拖上去即可）。
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import webbrowser
from pathlib import Path
from typing import Any

from kdagent.eval.review import span_summary, spans_in_tree_order
from kdagent.eval.trace_store import _load_one
from kdagent.obs.model import Span, Trace

# 判定徽章：trace 头 attributes 里的 eval 标记
_PASS_KEY = "eval.passed"
_RUN_KEY = "eval.run_id"
_TASK_KEY = "eval.task_id"
_KIND_KEY = "eval.kind"
_REASON_KEY = "eval.reason"


# ---- 单 span 卡片 ----

def _esc(value: Any) -> str:
    """任意值 → HTML 转义文本（dict 用 JSON 排版）。"""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, indent=2)
    else:
        text = str(value)
    return html.escape(text)


def _token_badge(attrs: dict[str, Any]) -> str:
    """LLM 调用的 token 计量条（有 input_tokens 才渲染）。"""
    if "input_tokens" not in attrs:
        return ""
    parts = [
        f"in {attrs.get('input_tokens', '?')}",
        f"out {attrs.get('output_tokens', '?')}",
    ]
    if attrs.get("cache_read_tokens"):
        parts.append(f"cache↩ {attrs['cache_read_tokens']}")
    if attrs.get("cache_creation_tokens"):
        parts.append(f"cache→ {attrs['cache_creation_tokens']}")
    return f'<span class="tokens">{" · ".join(parts)}</span>'


_LOG_PART_RE = re.compile(r"(?m)^(\[[^\]]+\])\n")


def _split_log_parts(message: str) -> list[tuple[str, str]]:
    """日志按 `[role]\n` 起点切块 → [(tag, body)]；无标记整段返回 ("", 原文)。

    payload_text/incremental_payload_text 的格式：每块 `[role[:sub]][:name]` + 换行 + 正文。
    """
    matches = list(_LOG_PART_RE.finditer(message))
    if not matches:
        return [("", message)]
    parts: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(message)
        parts.append((m.group(1), message[m.end() : end]))
    return parts


def _log_role(tag: str) -> str:
    """日志标记 → 对话角色（tool 结果/调用虽然 role=user/assistant 也要单独染工具色）。"""
    clean = tag.strip("[]")
    if "tool_result" in clean or "tool_use" in clean:
        return "tool"
    return clean.split(":")[0]


def _render_log(message: str) -> str:
    """日志消息 → 对话块（按 [role] 标记切分、角色配色），近似对话历史。"""
    blocks: list[str] = []
    for tag, body in _split_log_parts(message):
        blocks.append(
            f'<div class="logblock {_log_role(tag)}"><div class="lt">{html.escape(tag)}</div>'
            f'<pre class="v">{html.escape(body)}</pre></div>'
        )
    return "".join(blocks)


def _render_span_card(depth: int, span: Span) -> str:
    """一个 span → 卡片 HTML：标题行 + 可折叠正文。"""
    attrs = dict(span.attributes)
    status = span.status
    state_cls = "err" if status == "error" else ("warn" if status != "ok" else "ok")

    # 标题行：缩进 + 摘要 + 状态徽章 + token 计量
    title = span_summary(span)
    badge = f'<span class="st {state_cls}">{html.escape(status)}</span>' if status != "ok" else ""
    head = (
        f'<span class="depth" style="--d:{depth}"></span>'
        f'<span class="title">{html.escape(title)}</span>'
        f"{badge}{_token_badge(attrs)}"
    )

    # 正文：逐属性渲染（工具输入输出/LLM 决策等，完整不截断）
    body_parts: list[str] = []
    for key in ("tool", "is_error", "duration_ms", "model", "input_tokens",
                "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
        attrs.pop(key, None)
    for key, value in attrs.items():
        cls = "attr-input" if key in ("input", "output", "prompt", "result") else "attr"
        body_parts.append(
            f'<div class="row {cls}"><div class="k">{html.escape(key)}</div>'
            f'<pre class="v">{_esc(value)}</pre></div>'
        )
    # D90：span 日志（llm.call 的 prompt 增量/全量）按 [role] 块切分渲染成对话历史。
    for log in span.logs:
        body_parts.append(
            f'<div class="row"><div class="k">log[{html.escape(log.level)}]</div>'
            f'<div class="logbody">{_render_log(log.message)}</div></div>'
        )
    body = "".join(body_parts) or '<div class="row"><pre class="v muted">(无可见属性)</pre></div>'

    # data-text 同时含可读标题与原始 name，保证「tool:Bash」和「tool.exec」都能搜到
    searchable = f"{title} {span.name}".lower()
    return (
        f'<details class="span {state_cls}" data-text="{html.escape(searchable)}">'
        f"<summary>{head}</summary>{body}</details>"
    )


# ---- 整条 trace → HTML ----

def _trace_header(trace: Trace) -> str:
    """trace 头信息：id + 判定徽章 + eval 标记。"""
    attrs = trace.attributes
    passed = attrs.get(_PASS_KEY)
    if passed is None:
        verdict = '<span class="verdict none">未判分</span>'
    elif passed is True:
        verdict = '<span class="verdict pass">通过 ✓</span>'
    else:
        verdict = '<span class="verdict fail">失败 ✗</span>'

    meta: list[str] = [f"trace_id: <code>{html.escape(trace.trace_id)}</code>"]
    if attrs.get(_RUN_KEY):
        meta.append(f"run: <code>{html.escape(str(attrs[_RUN_KEY]))}</code>")
    if attrs.get(_TASK_KEY):
        meta.append(f"task: <code>{html.escape(str(attrs[_TASK_KEY]))}</code>")
    meta.append(f"{len(trace.spans)} spans")

    extra = ""
    if attrs.get(_KIND_KEY) or attrs.get(_REASON_KEY):
        kind = html.escape(str(attrs.get(_KIND_KEY, "")))
        reason = html.escape(str(attrs.get(_REASON_KEY, "")))
        extra = f'<div class="verdict-line">归类 {kind} — {reason}</div>'
    return f"{verdict} {' · '.join(meta)}{extra}"


def render_trace_html(trace: Trace) -> str:
    """单条 trace → 完整 HTML 文档（内联 CSS/JS，零依赖）。"""
    cards = [_render_span_card(d, s) for d, s in spans_in_tree_order(trace)]
    cards_html = "\n".join(cards) if cards else '<div class="muted">(无 span)</div>'
    user_input = html.escape(trace.user_input_snapshot)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trace {html.escape(trace.trace_id)}</title>
<style>
:root {{ color-scheme: dark; }}
body {{ font-family: "Segoe UI", "Microsoft YaHei", sans-serif; background: #0f1117; color: #d5d8e0; margin: 0; padding: 24px 32px 80px; }}
h1 {{ font-size: 18px; margin: 0 0 8px; }}
.verdict {{ font-weight: 700; padding: 2px 10px; border-radius: 10px; margin-right: 8px; font-size: 13px; }}
.verdict.pass {{ background: #13331f; color: #5cd98a; }}
.verdict.fail {{ background: #3b1416; color: #ff7d82; }}
.verdict.none {{ background: #2a2d38; color: #9aa1b5; }}
.verdict-line {{ color: #ffd27a; margin: 6px 0; font-size: 13px; }}
code {{ background: #1c2029; padding: 1px 6px; border-radius: 4px; }}
.meta {{ color: #8b91a3; font-size: 13px; margin-bottom: 16px; }}
.toolbar {{ position: sticky; top: 0; background: #0f1117; padding: 8px 0 12px; z-index: 5; }}
.toolbar input {{ width: 300px; background: #1c2029; border: 1px solid #333a4a; color: #d5d8e0; padding: 6px 10px; border-radius: 6px; }}
.toolbar button {{ background: #232837; border: 1px solid #333a4a; color: #c7cbd8; padding: 6px 12px; border-radius: 6px; cursor: pointer; margin-left: 6px; }}
.user-input {{ white-space: pre-wrap; background: #1c2029; border-left: 3px solid #4a6cf7; padding: 10px 14px; border-radius: 4px; margin: 8px 0 18px; font-size: 13px; max-height: 140px; overflow: auto; }}
.span {{ margin: 0 0 4px; background: #171a22; border-radius: 6px; border: 1px solid #262b37; }}
.span summary {{ list-style: none; display: flex; align-items: center; gap: 8px; padding: 6px 12px; cursor: pointer; }}
.span summary::-webkit-details-marker {{ display: none; }}
.span.err {{ border-color: #5c2a2e; }}
.span.err summary {{ background: #23181b; }}
.span.warn summary {{ background: #232019; }}
.depth {{ display: inline-block; width: calc(var(--d) * 18px); flex: none; }}
.title {{ font-family: Consolas, monospace; font-size: 13px; }}
.st {{ font-size: 11px; padding: 1px 7px; border-radius: 8px; font-weight: 700; }}
.st.ok {{ background: #13331f; color: #5cd98a; }}
.st.err {{ background: #3b1416; color: #ff7d82; }}
.st.warn {{ background: #3b3016; color: #ffd27a; }}
.tokens {{ margin-left: auto; font-size: 11px; color: #8b91a3; font-family: Consolas, monospace; }}
.span > .row {{ padding: 8px 14px 8px 22px; border-top: 1px solid #262b37; }}
.row {{ display: flex; gap: 12px; }}
.k {{ flex: none; width: 90px; color: #7d84a0; font-family: Consolas, monospace; font-size: 12px; padding-top: 4px; }}
.v {{ margin: 0; font-family: Consolas, monospace; font-size: 12.5px; white-space: pre-wrap; word-break: break-word; flex: 1; }}
.logbody {{ flex: 1; max-height: 320px; overflow: auto; }}
.logblock {{ margin: 4px 0; padding: 2px 0 2px 10px; border-left: 2px solid #333a4a; }}
.logblock .lt {{ font-size: 11px; color: #8b91a3; font-family: Consolas, monospace; margin-bottom: 2px; }}
.logblock.user .lt {{ color: #7aa2ff; }}
.logblock.assistant .lt {{ color: #5cd98a; }}
.logblock.system .lt {{ color: #9aa1b5; }}
.logblock.tool .lt {{ color: #ffd27a; }}
.attr-input .k {{ color: #5cd98a; }}
.muted {{ color: #6a7084; }}
footer {{ margin-top: 24px; color: #5c6172; font-size: 12px; }}
</style>
</head>
<body>
<h1>Trace {html.escape(trace.trace_id)}</h1>
<div class="meta">{_trace_header(trace)}</div>
<div class="toolbar">
  <input id="q" placeholder="过滤 span（名称/工具/关键字）…" oninput="filterSpans(this.value)">
  <button onclick="toggleAll()">全部展开/收起</button>
</div>
<div class="user-input">{user_input}</div>
{cards_html}
<footer>kdagent eval · trace_html 渲染 · 共 {len(trace.spans)} 个 span</footer>
<script>
function filterSpans(q) {{
  q = q.toLowerCase();
  document.querySelectorAll(".span").forEach(el => {{
    el.style.display = (q === "" || el.dataset.text.includes(q)) ? "" : "none";
  }});
}}
function toggleAll() {{
  const all = document.querySelectorAll(".span");
  const open = all[0] && all[0].open;
  all.forEach(el => el.open = !open);
}}
</script>
</body>
</html>
"""


# ---- 目录索引 ----

def render_index_html(entries: list[tuple[Path, Trace]]) -> str:
    """多条 trace 的索引页：链接 + 判定 + task 摘要。"""
    rows: list[str] = []
    for path, trace in entries:
        attrs = trace.attributes
        passed = attrs.get(_PASS_KEY)
        if passed is None:
            v = '<span class="v none">未判分</span>'
        else:
            v = f'<span class="v {"pass" if passed else "fail"}">{"通过" if passed else "失败"}</span>'
        task = html.escape(str(attrs.get(_TASK_KEY, path.stem)))
        rows.append(
            f'<div class="row"><a href="{html.escape(path.name)}">{html.escape(path.name)}</a>'
            f"{v}<span class='task'>{task}</span>"
            f"<span class='n'>{len(trace.spans)} spans</span></div>"
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Trace 索引</title>
<style>
body {{ font-family: "Segoe UI", "Microsoft YaHei", sans-serif; background: #0f1117; color: #d5d8e0; padding: 32px; }}
.row {{ display: flex; gap: 12px; align-items: center; padding: 8px 0; border-bottom: 1px solid #262b37; }}
a {{ color: #7aa2ff; text-decoration: none; }}
.task {{ color: #8b91a3; }}
.n {{ color: #5c6172; font-size: 12px; margin-left: auto; }}
.v {{ font-weight: 700; padding: 1px 8px; border-radius: 8px; font-size: 12px; }}
.v.pass {{ background: #13331f; color: #5cd98a; }}
.v.fail {{ background: #3b1416; color: #ff7d82; }}
.v.none {{ background: #2a2d38; color: #9aa1b5; }}
</style></head>
<body><h1>Trace 索引（{len(entries)} 条）</h1>{"".join(rows)}</body></html>
"""


# ---- 文件 I/O ----

def trace_to_html_file(trace: Trace, out: Path) -> Path:
    """写单条 trace 的 HTML（原子写：.tmp → replace）。返回输出路径。"""
    tmp = out.with_suffix(".html.tmp")
    tmp.write_text(render_trace_html(trace), encoding="utf-8")
    tmp.replace(out)
    return out


def convert_file(jsonl: Path, *, open_browser: bool) -> Path:
    """单个 jsonl → 同名 html。返回输出路径。"""
    trace = _load_one(jsonl)
    if trace is None:
        raise ValueError(f"不是有效 trace jsonl（缺 header 行）：{jsonl}")
    out = jsonl.with_suffix(".html")
    trace_to_html_file(trace, out)
    if open_browser:
        webbrowser.open(out.resolve().as_uri())
    return out


def convert_dir(directory: Path, *, open_browser: bool) -> Path:
    """目录下全部 jsonl → 每 trace 一个 html + index.html。返回 index 路径。"""
    entries: list[tuple[Path, Trace]] = []
    for jsonl in sorted(directory.glob("*.jsonl")):
        try:
            trace = _load_one(jsonl)
        except (OSError, ValueError):
            continue  # 脏文件跳过，不阻断
        if trace is not None:
            trace_to_html_file(trace, jsonl.with_suffix(".html"))
            entries.append((jsonl.with_suffix(".html"), trace))
    index = directory / "index.html"
    index.write_text(render_index_html(entries), encoding="utf-8")
    if open_browser:
        webbrowser.open(index.resolve().as_uri())
    return index


# ---- CLI ----

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m kdagent.eval.trace_html",
        description="trace jsonl → 易读 HTML（文件或目录，输出同名 .html）",
    )
    p.add_argument("paths", nargs="*", help="jsonl 文件或目录（无参数时弹文件选择框）")
    p.add_argument("--no-open", action="store_true", help="生成后不自动打开浏览器")
    return p


def _pick_file_gui() -> Path | None:
    """无参数时弹文件选择框（tkinter 内置，失败则提示用法）。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return None
    root = tk.Tk()
    root.withdraw()
    path = filedialog.askopenfilename(title="选择 trace jsonl", filetypes=[("trace jsonl", "*.jsonl")])
    root.destroy()
    return Path(path) if path else None


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    open_browser = not args.no_open
    paths = [Path(p) for p in args.paths]
    if not paths:
        picked = _pick_file_gui()
        if picked is not None:
            paths = [picked]
    if not paths:
        print("用法：python -m kdagent.eval.trace_html <trace.jsonl|目录> [--no-open]", file=sys.stderr)
        return 2
    for p in paths:
        if p.is_dir():
            out = convert_dir(p, open_browser=open_browser)
            print(f"目录 {p} → {out}（含全部 trace）")
        elif p.is_file():
            out = convert_file(p, open_browser=open_browser)
            print(f"{p} → {out}")
        else:
            print(f"路径不存在：{p}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
