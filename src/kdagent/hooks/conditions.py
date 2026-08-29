"""Hook 条件语法（规格 06 §3.10）。

支持操作符：`==` 精确 / `!=` 反向 / `=~` 正则 / `~=` glob；
组合：`&&` 与、`||` 或，**两者不可混用**（避免引入表达式引擎）；
字段：`tool` 工具名；`event` 事件名；`args.xxx` 工具参数（未知字段返回空串不报错）。
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

from kdagent.hooks.engine_types import HookContext  # 仅类型引用，规避成环见 __init__

_OP_RE = re.compile(r"^(?P<field>[A-Za-z_][\w.]*)\s*(?P<op>==|!=|=~|~=)\s*(?P<value>.+)$")

_OP_AND = "&&"
_OP_OR = "||"


class ConditionError(ValueError):
    """条件解析错误。"""


@dataclass(frozen=True, slots=True)
class _Expr:
    field: str
    op: str
    value: str

    def matches(self, ctx: HookContext) -> bool:
        got = _field_value(ctx, self.field)
        if self.op == "==":
            return got == self.value
        if self.op == "!=":
            return got != self.value
        if self.op == "=~":
            try:
                return re.search(self.value, got) is not None
            except re.error:
                return False
        return fnmatch.fnmatchcase(got, self.value)


@dataclass(frozen=True, slots=True)
class Condition:
    """一个布尔条件：`A && B` 或 `A || B`（同一种连接符，不可混用）。"""

    exprs: tuple[_Expr, ...]
    combine: str  # "&&" | "||"

    def matches(self, ctx: HookContext) -> bool:
        results = [e.matches(ctx) for e in self.exprs]
        if self.combine == "&&":
            return all(results)
        return any(results)


def _field_value(ctx: HookContext, field: str) -> str:
    if field == "tool":
        return ctx.tool_name
    if field == "event":
        return ctx.event
    if field.startswith("args."):
        key = field[5:]
        value = ctx.tool_args.get(key)
        return str(value) if value is not None else ""
    return ""  # 未知字段返回空串，不报错


def parse_condition(text: str) -> Condition:
    """解析条件串；非法（混用连接符/缺操作符/空表达式）抛 ConditionError。"""
    stripped = text.strip()
    if not stripped:
        raise ConditionError("空条件")
    if _OP_AND in stripped and _OP_OR in stripped:
        raise ConditionError("&& 与 || 不可混用")
    combine = _OP_AND if _OP_AND in stripped else _OP_OR
    parts = [p.strip() for p in stripped.split(combine) if p.strip()]
    if not parts:
        raise ConditionError("空条件")
    exprs: list[_Expr] = []
    for part in parts:
        m = _OP_RE.match(part)
        if m is None:
            raise ConditionError(f"条件片段非法：{part!r}")
        exprs.append(
            _Expr(
                field=m.group("field").strip(),
                op=m.group("op"),
                value=_unquote(m.group("value").strip()),
            )
        )
    return Condition(tuple(exprs), combine)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def expand_variables(text: str, ctx: HookContext) -> str:
    """上下文变量替换：$EVENT/$TOOL_NAME/$FILE_PATH/$MESSAGE/$ERROR/$TOOL_ARGS.xxx；
    未定义变量替换为空串，不报错（规格 §3.10）。"""
    def _lookup(name: str) -> str:
        if name == "EVENT":
            return ctx.event
        if name == "TOOL_NAME":
            return ctx.tool_name
        if name == "FILE_PATH":
            return ctx.file_path
        if name == "MESSAGE":
            return ctx.message
        if name == "ERROR":
            return ctx.error
        if name == "PAYLOAD_PATH":
            return ctx.payload_path
        if name.startswith("TOOL_ARGS."):
            key = name[len("TOOL_ARGS.") :]
            value = ctx.tool_args.get(key)
            return str(value) if value is not None else ""
        return ""

    return _VAR_RE.sub(lambda m: _lookup(m.group(1)), text)


_VAR_RE = re.compile(r"\$([A-Z][A-Z0-9_]*(\.[A-Za-z_][\w]*)?)")
