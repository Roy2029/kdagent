"""复核界面数据/渲染层（规格 11 §3.4 T30 增量，M5 遗留）。

三类交互支撑「定位 / 阅读 / 批注」——不让人肉翻 jsonl：
- 定位：失败题 → 展开 trace span 树 → 按「报错工具调用 / 压缩点 / 权限拒绝」过滤跳转
- 阅读：原始事件流可读渲染（工具调用参数/返回/报错逐事件挑出）
- 批注：人工修正归类 + 备注 → 写回 `{kdagent_dir}/eval/<run_id>/annotations.json`

本模块**纯函数 + 文件读写**，可单测；CLI 编排在 eval/cli.py。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kdagent.eval.model import FailureCase, FailureKind
from kdagent.obs.model import Span, Trace

# span 三类定位过滤（11 §3.4 定位交互）
_FOCUS_ERROR = "error"  # 报错工具调用（status=error / is_error）
_FOCUS_COMPACT = "compact"  # 压缩点（kind=context）
_FOCUS_PERMISSION = "permission"  # 权限拒绝（kind=permission）
_FOCUS_KINDS = (_FOCUS_ERROR, _FOCUS_COMPACT, _FOCUS_PERMISSION)

# 批注文件：{kdagent_dir}/eval/<run_id>/annotations.json（obs_dir 上一级即 kdagent_dir）
_ANNOTATION_JSON = "annotations.json"

# 渲染配置
_MAX_JSON_INLINE = 80  # attributes 单行渲染超长截断
_OUTPUT_CAP = 1000  # 工具返回进 span 的截断长度（agent.py 同值）


@dataclass(slots=True)
class Annotation:
    """一道失败题的人工批注（11 §3.4 批注交互）。"""

    kind: FailureKind | None = None
    note: str = ""


# ---- span 树：定位 ----

@dataclass(slots=True)
class SpanNode:
    """span 树的节点（以 parent_span_id 恢复父子关系）。"""

    span: Span
    children: list[SpanNode] = field(default_factory=list)


def build_span_tree(trace: Trace) -> list[SpanNode]:
    """把 flat spans 按 parent_span_id 重组为树（深拷贝结构，不改原对象）。

    返回森林（多个根时）。根 = root_span_id 匹配的 span 或 parent 为空的 span；
    parent 指向不存在/已丢行的 span 时挂到该 parent 名下的孤儿不强制归位，
    但本实现只把「能对上 parent 的」挂树，其余按原序并入根列表（脏行不阻断排查）。
    """
    by_id: dict[str, SpanNode] = {s.span_id: SpanNode(s) for s in trace.spans}
    roots: list[SpanNode] = []
    for node in by_id.values():
        parent = node.span.parent_span_id
        if parent and parent in by_id:
            by_id[parent].children.append(node)
        else:
            roots.append(node)
    return roots


def _walk(node: SpanNode, depth: int, out: list[tuple[int, Span]]) -> None:
    out.append((depth, node.span))
    for child in node.children:
        _walk(child, depth + 1, out)


def spans_in_tree_order(trace: Trace) -> list[tuple[int, Span]]:
    """深度优先展平 span 树 → (depth, span) 列表（渲染/阅读共用的遍历序）。"""
    out: list[tuple[int, Span]] = []
    for root in build_span_tree(trace):
        _walk(root, 0, out)
    return out


def focus_spans(trace: Trace, focus: str) -> list[Span]:
    """按定位过滤取目标 span 集（11 §3.4 过滤跳转）。"""
    if focus == _FOCUS_ERROR:
        return [
            s for s in trace.spans if s.status == "error" or s.attributes.get("is_error") is True
        ]
    if focus == _FOCUS_COMPACT:
        return [
            s
            for s in trace.spans
            if s.kind == "context" or s.name == "context.compact" or "compact" in s.name
        ]
    if focus == _FOCUS_PERMISSION:
        return [
            s
            for s in trace.spans
            if s.kind == "permission" or s.name.startswith("permission.")
        ]
    raise ValueError(f"未知过滤类型：{focus}（可选 {_FOCUS_KINDS}）")


def focus_labels() -> tuple[tuple[str, str], ...]:
    """过滤类型 → 中文标签（CLI 提示用）。"""
    return (
        (_FOCUS_ERROR, "报错工具调用"),
        (_FOCUS_COMPACT, "压缩点"),
        (_FOCUS_PERMISSION, "权限拒绝"),
    )


# ---- 渲染：定位 + 阅读 ----

def _short_json(value: Any, cap: int = _MAX_JSON_INLINE) -> str:
    """属性值单行渲染：JSON 序列化 + 超长截断。"""
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= cap else text[: cap - 1] + "…"


def span_summary(span: Span) -> str:
    """单行 span 摘要（树节点行）：`llm.call(client) 123ms [error]`。"""
    name = span.name
    if span.kind == "tool":
        name = f"tool:{span.attributes.get('tool', name)}"
    elif span.kind == "permission":
        name = f"perm:{span.attributes.get('tool', name)}"
    dur = f"{span.duration_ms}ms" if span.duration_ms else ""
    mark = " [error]" if span.status == "error" else ""
    return f"{name}({span.kind}) {dur}{mark}".strip()


def render_span_tree(trace: Trace, *, mark_ids: set[str] | None = None) -> str:
    """树形渲染 span 树；mark_ids 命中的节点行尾标 `←`（定位跳转高亮）。"""
    mark_ids = mark_ids or set()
    lines: list[str] = []
    for depth, span in spans_in_tree_order(trace):
        prefix = "  " * depth
        marker = " ←" if span.span_id in mark_ids else ""
        lines.append(f"{prefix}- {span_summary(span)}{marker}")
    return "\n".join(lines) if lines else "(无 span)"


def span_detail(span: Span) -> str:
    """单事件可读渲染（阅读交互）：参数/返回/报错/裁决逐项挑出。"""
    lines = [
        f"{span.name}（{span.kind}）{span.duration_ms}ms"
        + ("" if span.status == "ok" else f" [{span.status}]")
    ]
    attrs = dict(span.attributes)
    for key in ("tool", "is_error", "duration_ms", "model"):
        attrs.pop(key, None)
    for key in ("input", "output", "effect", "verdict", "reason", "event"):
        if key in attrs:
            lines.append(f"  {key}: {_short_json(attrs.pop(key), cap=600)}")
    if attrs:
        lines.append(f"  attrs: {_short_json(attrs)}")
    for log in span.logs:
        lines.append(f"  log[{log.level}]: {log.message}")
    if len(lines) == 1:
        lines.append(f"{span.name}（无可见属性）")
    return "\n".join(lines)


# ---- 批注：人工修正归类 + 备注 ----

def _annotations_path(obs_dir: Path, run_id: str) -> Path:
    """批注文件路径：{kdagent_dir}/eval/<run_id>/annotations.json。"""
    return obs_dir.parent / "eval" / run_id / _ANNOTATION_JSON


def load_annotations(obs_dir: Path, run_id: str) -> dict[str, Annotation]:
    """读批注文件（不存在/脏行 → 空，不阻断复核）。"""
    path = _annotations_path(obs_dir, run_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    result: dict[str, Annotation] = {}
    for task_id, item in raw.items():
        if isinstance(item, dict):
            result[str(task_id)] = Annotation(
                kind=item.get("kind") if item.get("kind") else None,
                note=str(item.get("note", "")),
            )
    return result


def save_annotation(
    obs_dir: Path, run_id: str, task_id: str, kind: FailureKind, note: str = ""
) -> Path:
    """写一条批注（原子写：先落 .tmp 再替换，防半写）。返回文件路径。"""
    path = _annotations_path(obs_dir, run_id)
    annotations = load_annotations(obs_dir, run_id)
    annotations[task_id] = Annotation(kind=kind, note=note)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        tid: {"kind": a.kind, "note": a.note}
        for tid, a in sorted(annotations.items())
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(path)
    return path


def merged_kind(
    obs_dir: Path, run_id: str, task_id: str, auto_kind: FailureKind
) -> tuple[FailureKind, Annotation]:
    """归类合并：人工批注优先，否则自动归类（11 §3.4 人工可复核修正）。"""
    annotation = load_annotations(obs_dir, run_id).get(task_id, Annotation())
    return (annotation.kind if annotation.kind else auto_kind), annotation


# ---- 失败题索引 ----

def render_failure_index(
    case: FailureCase, annotation: Annotation, bad_count: int
) -> str:
    """失败题一行（复核列表）：`- <id> [<归类>] <reason>（N 失败事件，备注…）`。"""
    suffix = ""
    if annotation.kind and annotation.kind != case.kind:
        suffix += "  [人工改判]"
    if annotation.note:
        suffix += f"  备注：{annotation.note}"
    return (
        f"- {case.instance_id} [{case.kind}] {case.reason}"
        f"（{bad_count} 失败事件）{suffix}".rstrip()
    )
