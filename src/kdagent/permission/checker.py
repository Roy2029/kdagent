"""权限五层裁决核心（规格 06 §3.1-3.2）。

裁决链：L1 黑名单 → 敏感路径禁写（§3.8，硬性）→ bypassPermissions 提前放行
→ L2 路径沙箱 → L3 权限规则 → L4 模式矩阵。L5 HITL 由调用方（Agent loop）
在 `Decision.effect == "ask"` 时触发。

`PermissionChecker.check` 是无 UI 依赖的纯函数——MCP wrapper 与 SubAgent
复用同一裁决链（09/10）。
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kdagent.permission.blacklist import CommandBlacklist
from kdagent.permission.modes import MODE_MATRIX, Mode, tool_class
from kdagent.permission.rules import Effect, RuleEngine
from kdagent.permission.sandbox import PathSandbox
from kdagent.tools.base import Tool

# 工具输入 → 匹配内容 的提取表（规格 06 §3.5）。
_CONTENT_FIELDS: dict[str, str] = {
    "Bash": "command",
    "ReadFile": "path",
    "WriteFile": "path",
    "EditFile": "path",
    "Glob": "pattern",
    "Grep": "pattern",
}

# §3.8 敏感禁写：kdagent 目录内的系统配置/权限/技能文件，绝对禁写。
_SENSITIVE_FILENAMES = frozenset(
    {"config.yaml", "config.local.yaml", "permissions.yaml", "permissions.local.yaml"}
)
_SENSITIVE_DIRNAMES = frozenset({"skills"})

# D10 命令级只读判断（N3）：纯只读命令集合。文件工具只读类（Glob/Grep/ReadFile）
# 在 L4 已是 read=allow；但 Bash 执行 `grep/find/ls/cat` 一律归 shell → ask，
# 用户在 acceptEdits 下看到"只读命令也弹窗"。这里对**单条**只读命令放行。
# 只收录明确无写变体的命令（排除 sed -i、awk、tr 等有破坏性变体的）；
# 复杂形态（重定向/管道/命令组合）不走此判断，仍按 L4 矩阵 ask，安全优先。
# 🔴 review 修复（2026-08-31）：移除 `env`——它是通用命令执行器
# （`env python -c "..."` 免审批执行任意代码），不属于只读。
_READONLY_COMMANDS = frozenset(
    {
        "ls", "find", "grep", "cat", "head", "tail", "wc", "pwd", "echo",
        "printf", "which", "type", "diff", "file", "strings", "du", "df",
        "free", "whoami", "dirname", "basename", "date", "stat", "realpath",
        "tree", "sort", "uniq", "cut", "printenv",
    }
)

# 只读命令的危险变体：参数命中排除 token 即不放行（find -delete/-exec 可写盘）。
_READONLY_EXCLUDE_TOKENS: dict[str, frozenset[str]] = {
    "find": frozenset(
        {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fls", "-fprint", "-fprintf"}
    ),
}


def _is_readonly_bash(command: str) -> bool:
    """单条只读命令（无重定向/管道/命令替换）→ True。

    首 token 经 ``Path(name).name`` 容忍 `/usr/bin/grep` 全路径；含
    ``> | < ; & $() ` `` 任一即不视为纯只读（可能改写外部状态）。
    白名单命令若命中 `_READONLY_EXCLUDE_TOKENS` 的危险变体（如 `find -delete`）
    也不放行（🔴 review 修复）。
    """
    if not command or not command.strip():
        return False
    for ch in ">|<;&":
        if ch in command:
            return False
    if "$(" in command or "`" in command:
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False
    first = Path(tokens[0]).name
    if first not in _READONLY_COMMANDS:
        return False
    exclude = _READONLY_EXCLUDE_TOKENS.get(first)
    return not (exclude is not None and any(tok in exclude for tok in tokens[1:]))


def _bash_pipeline_readonly(command: str) -> bool:
    """命令全只读 → True（bash_readonly Agent 门禁用）。

    与 `_is_readonly_bash`（单条保守口径，acceptEdits 自动放行）不同：允许纯只读
    管道/组合（`grep x src | wc -l`、`ls && git log`）——按 ``&& || ; |`` 分段后
    逐段过只读白名单；任一段非只读（rm/sed -i/重定向/命令替换）即 False。
    """
    for seg in re.split(r"&&|\|\||;|\|", command):
        seg = seg.strip()
        if seg and not _is_readonly_bash(seg):
            return False
    return True


# 敏感路径「出现即拦」兜底（🔴 review 修复）：写语法提取（rm/mv/cp/重定向）之外，
# 命令文本同时满足「引用 kdagent 目录」+「点名敏感文件/skills」→ 升级 ask。
# 覆盖 `cd .kdagent && rm permissions.local.yaml`、`sed -i`、`python -c "os.remove(...)"`
# 等所有写形态——不变量是「命令文本出现敏感路径即需确认」，不再穷举写命令语法。
# 拦的是 ask 不是 deny（用户可放行）；误伤面 = 读写敏感路径多一次确认，可接受。
_SENSITIVE_SUBSTRINGS: tuple[str, ...] = (
    "config.yaml", "config.local.yaml", "permissions.yaml", "permissions.local.yaml",
    "skills",
)


def _mentions_sensitive_path(command: str) -> bool:
    """命令引用 kdagent 目录且点名敏感文件/目录 → True（大小写不敏感，子串匹配）。"""
    lowered = command.casefold()
    if ".kdagent" not in lowered:
        return False  # 未引用 kd 目录（如根目录 `cat config.yaml`）不构成写风险
    return any(s in lowered for s in _SENSITIVE_SUBSTRINGS)


@dataclass(frozen=True, slots=True)
class Decision:
    """一次裁决结果。effect=ask 时由调用方转 HITL。"""

    effect: Effect
    reason: str
    rule: str | None = None  # 命中的规则串（L3），None = 未命中规则


class PermissionChecker:
    """五层裁决器。`mode` 运行时可切换（/permissions 命令）。"""

    def __init__(
        self,
        mode: Mode = "default",
        blacklist: CommandBlacklist | None = None,
        sandbox: PathSandbox | None = None,
        rules: RuleEngine | None = None,
        work_dir: Path | None = None,
        kdagent_dirs: list[Path] | None = None,
    ) -> None:
        self._mode = mode
        self._blacklist = blacklist
        self._sandbox = sandbox
        self._rules = rules
        self._work_dir = Path.cwd() if work_dir is None else work_dir
        self._kdagent_dirs = [d.resolve() for d in (kdagent_dirs or [])]

    # ---- 模式 ----

    @property
    def mode(self) -> Mode:
        return self._mode

    def set_mode(self, mode: Mode) -> None:
        if mode not in MODE_MATRIX:
            raise ValueError(f"未知权限模式：{mode}")
        self._mode = mode

    def with_mode(self, mode: Mode) -> PermissionChecker:
        """同组件派生指定模式的新 checker（子 Agent permissionMode 收口用）。

        黑名单/沙箱/规则/工作目录/kd 目录全部共享同一实例，仅 L4 矩阵模式不同——
        子 Agent 不绕过 L1-L3（review 修复 2026-08-31）。
        """
        return PermissionChecker(
            mode=mode,
            blacklist=self._blacklist,
            sandbox=self._sandbox,
            rules=self._rules,
            work_dir=self._work_dir,
            kdagent_dirs=self._kdagent_dirs,
        )

    # ---- 裁决 ----

    def check(self, tool: Tool, input: dict[str, Any]) -> Decision:
        content = self._extract_content(tool.name, input)

        # L1 危险命令黑名单（仅 Bash，硬拦截，先于一切）。
        if tool.name == "Bash" and self._blacklist is not None:
            hit = self._blacklist.match(content)
            if hit is not None:
                return Decision(
                    "deny", f"检测到危险命令（{hit}），已被安全策略硬拦截"
                )

        # §3.8 敏感路径禁写（filesystem 写工具；绝对禁写，bypass 也不豁免——
        # 模型自改 config/permissions/skills = 提权或自改指令）。
        if tool.category == "filesystem" and not tool.is_read_only():
            reason = self._sensitive_write(content)
            if reason is not None:
                return Decision("deny", reason)

        # B3+B4 修复（2026-08-29）：Bash 命令的写目标同样受 §3.8 敏感禁写。
        # 此前只拦 filesystem 写工具，Agent 实测 `rm .kdagent/permissions.local.yaml`
        # （相对路径普通删除，不命中 L1 黑名单）成功绕过删掉权限文件。黑名单只兜底
        # 递归删根/挂载点/盘符，此处补上"删除/移动/覆盖写 kdagent 系统文件"的通道。
        if tool.name == "Bash":
            for tgt in self._bash_write_targets(content):
                reason = self._sensitive_write(tgt)
                if reason is not None:
                    return Decision("deny", reason)

        # bypassPermissions：跳过 L2-L5（黑名单与敏感禁写仍生效）。
        if self._mode == "bypassPermissions":
            return Decision("allow", "bypassPermissions 模式")

        # planning 类（TodoWrite）恒放行——规划是心智产物，无破坏面。
        if tool.category == "planning":
            return Decision("allow", "planning 类工具恒放行")

        # L2 路径沙箱（仅 filesystem 类）。
        if (
            tool.category == "filesystem"
            and self._sandbox is not None
            and not self._sandbox.contains(content)
        ):
            return Decision("ask", f"路径超出沙箱范围：{content}")

        # L3 权限规则。
        if self._rules is not None:
            effect, rule_str = self._rules.evaluate(tool.name, content)
            if effect is not None:
                return Decision(effect, "权限规则命中", rule=rule_str)

        # 敏感路径「出现即拦」兜底（🔴 review 修复）：置于 L3 之后（显式规则优先）、
        # 只读免审批与 L4 之前——读敏感文件也值得一次确认。
        if tool.name == "Bash" and _mentions_sensitive_path(content):
            return Decision(
                "ask", "命令引用 kdagent 敏感路径（出现即拦兜底），需确认"
            )

        # D10 命令级只读判断（N3）：acceptEdits 下 Bash 单条只读命令免审批。
        # 置于 L3 之后（规则优先：用户显式 deny 的仍拦）、L4 之前（只读不落矩阵 ask）。
        if tool.name == "Bash" and self._mode == "acceptEdits" and _is_readonly_bash(content):
            return Decision("allow", "只读命令（acceptEdits 免审批）")

        # L4 模式矩阵。
        cls = tool_class(tool)
        return Decision(
            MODE_MATRIX[self._mode][cls],
            f"权限模式 {self._mode} · {cls} 类工具矩阵",
        )

    def learn(self, tool_name: str, content: str) -> None:
        """「始终允许」→ 追加本地规则，同类操作下次直接放行（§3.7）。"""
        if self._rules is not None:
            self._rules.learn(tool_name, content)

    def extract_content(self, tool: Tool, input: dict[str, Any]) -> str:
        """工具输入 → 匹配内容（Agent 侧 learn 复用；规格 §3.5 提取表）。"""
        return self._extract_content(tool.name, input)

    # ---- 内部 ----

    def _extract_content(self, tool_name: str, input: dict[str, Any]) -> str:
        field = _CONTENT_FIELDS.get(tool_name, "")
        value = input.get(field)
        return str(value) if isinstance(value, str) else ""

    def _bash_write_targets(self, command: str) -> list[str]:
        """提取 Bash 写操作的目标路径（rm/mv/cp 参数 + 重定向目标），供敏感禁写判定。

        只做保守提取——多提取几个路径交给 `_sensitive_write` 精确判断（命中才 DENY），
        不会误伤只读命令（无 rm/mv/cp/`>` 就返回空）。`rm -rf x` 的 flag 不参与。
        """
        targets: list[str] = []
        for m in re.finditer(r"\b(?:rm|mv|cp)\s+", command):
            rest = command[m.end():]
            for token in rest.split():
                tok = token.strip("\"'").rstrip(";&|")
                if tok and not tok.startswith("-"):
                    targets.append(tok)
        for m in re.finditer(r"[0-9]*>+\s*(\S+)", command):
            # `echo x > /path` 的 `>` 后常带空格（非 `>path`），`\S+` 从 token 首非空
            # 开始捕获；尾部可能粘命令分隔符（`config.yaml; rm x`）→ rstrip。
            tok = m.group(1).strip("\"'").rstrip(";&|")
            if tok:
                targets.append(tok)
        return targets

    def _sensitive_write(self, raw_path: str) -> str | None:
        """命中 kdagent 目录内敏感路径 → 返回禁写原因，否则 None。"""
        if not raw_path or not self._kdagent_dirs:
            return None
        p = Path(raw_path)
        if not p.is_absolute():
            p = self._work_dir / p
        try:
            real = p.resolve(strict=False)
        except OSError:
            real = p.absolute()
        real_str = str(real)
        # 🔴 review 修复：name/parts 与 containment 统一用 resolved 路径——
        # 原 name/parts 取未解析的 p，work_dir 内 symlink 指向 .kdagent/skills/
        # 时（real 落在 kd 目录内进入分支）parts 无 skills → 绕过禁写。
        name = real.name.casefold()
        for kd in self._kdagent_dirs:
            kd_str = str(kd)
            sep = "\\" if kd_str.find("\\") >= 0 else "/"
            if not (real_str == kd_str or real_str.startswith(kd_str + sep)):
                continue
            if name in _SENSITIVE_FILENAMES:
                return f"敏感路径禁写：{raw_path}（{real.name} 属系统配置文件）"
            if any(part.casefold() in _SENSITIVE_DIRNAMES for part in real.parts):
                return f"敏感路径禁写：{raw_path}（skills/ 可注入系统提示词）"
        return None


def build_permission_checker(
    work_dir: Path,
    *,
    mode: str = "default",
    blacklist_shell: str = "bash",
    kdagent_dirs: list[Path] | None = None,
    extra_roots: list[Path] | None = None,
) -> PermissionChecker:
    """装配完整五层裁决器（用户级 + 项目级规则文件 + 沙箱根）。

    规则文件：`~/.kdagent/permissions.yaml`（用户级）、
    `{work_dir}/.kdagent/permissions.yaml`（项目级）、
    `{work_dir}/.kdagent/permissions.local.yaml`（本地，learn 目标）。
    """
    kdagent_dirs = kdagent_dirs or []
    user_kd = Path.home() / ".kdagent"
    proj_kd = work_dir / ".kdagent"
    all_kd = [*kdagent_dirs, user_kd, proj_kd]

    rules = RuleEngine()
    rules.load_many(
        [user_kd / "permissions.yaml", proj_kd / "permissions.yaml"],
        local_path=proj_kd / "permissions.local.yaml",
    )

    roots = [*extra_roots] if extra_roots else []
    sandbox = PathSandbox(roots, work_dir=work_dir)

    if mode not in MODE_MATRIX:
        raise ValueError(f"未知权限模式：{mode}")
    return PermissionChecker(
        mode=mode,
        blacklist=CommandBlacklist(blacklist_shell),
        sandbox=sandbox,
        rules=rules,
        work_dir=work_dir,
        kdagent_dirs=all_kd,
    )
