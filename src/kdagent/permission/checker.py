"""权限五层裁决核心（规格 06 §3.1-3.2）。

裁决链：L1 黑名单 → 敏感路径禁写（§3.8，硬性）→ bypassPermissions 提前放行
→ L2 路径沙箱 → L3 权限规则 → L4 模式矩阵。L5 HITL 由调用方（Agent loop）
在 `Decision.effect == "ask"` 时触发。

`PermissionChecker.check` 是无 UI 依赖的纯函数——MCP wrapper 与 SubAgent
复用同一裁决链（09/10）。
"""

from __future__ import annotations

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
_READONLY_COMMANDS = frozenset(
    {
        "ls", "find", "grep", "cat", "head", "tail", "wc", "pwd", "echo",
        "printf", "which", "type", "diff", "file", "strings", "du", "df",
        "free", "whoami", "dirname", "basename", "date", "stat", "realpath",
        "tree", "sort", "uniq", "cut", "env", "printenv",
    }
)


def _is_readonly_bash(command: str) -> bool:
    """单条只读命令（无重定向/管道/命令替换）→ True。

    首 token 经 ``Path(name).name`` 容忍 `/usr/bin/grep` 全路径；含
    ``> | < ; & $() ` `` 任一即不视为纯只读（可能改写外部状态）。
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
    return Path(tokens[0]).name in _READONLY_COMMANDS


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
        name = p.name.casefold()
        for kd in self._kdagent_dirs:
            kd_str = str(kd)
            sep = "\\" if kd_str.find("\\") >= 0 else "/"
            if not (real_str == kd_str or real_str.startswith(kd_str + sep)):
                continue
            if name in _SENSITIVE_FILENAMES:
                return f"敏感路径禁写：{raw_path}（{p.name} 属系统配置文件）"
            if any(part.casefold() in _SENSITIVE_DIRNAMES for part in p.parts):
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
