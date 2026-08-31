"""L3 权限规则引擎（规格 06 §3.5）。

语法 `ToolName(pattern)`，pattern 为 glob 通配，匹配从工具输入提取的「内容」：

    ```yaml
    - rule: Bash(git *)
    - rule: Bash(git push --force*)
      effect: deny
    ```

三份规则文件（用户级/项目级/本地级）无优先级、合并裁决，`deny > ask > allow`。
想禁死一个操作，写在哪一层都禁得死；放宽权限只能改/删 deny。
规则文件不存在 → 按空规则集，新项目零配置可用。
"""

from __future__ import annotations

import fnmatch
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import yaml

Effect = Literal["allow", "deny", "ask"]

# `Bash(git push --force*)` → ("Bash", "git push --force*")
_RULE_RE = re.compile(r"^([^(]+)\(([^)]*)\)\s*$")

_EFFECTS: set[str] = {"allow", "deny", "ask"}

# 本地规则文件文件名（「始终允许」自动追加）与标记头。
LOCAL_RULES_FILENAME = "permissions.local.yaml"


def _warn_corrupt(rule_file: Path, detail: str) -> None:
    """规则文件损坏警告（降级为空规则集，不阻断启动——🔴 review 修复）。"""
    print(
        f"[kdagent] 警告：权限规则文件损坏已忽略：{rule_file}（{detail}）",
        file=sys.stderr,
    )


def _atomic_dump_yaml(path: Path, entries: list[dict[str, str]]) -> None:
    """yaml.safe_dump 整体原子重写（.tmp 替换，任意命令文本不破坏结构）。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        yaml.safe_dump(entries, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    tmp.replace(path)


@dataclass(frozen=True, slots=True)
class PermissionRule:
    """一条权限规则：工具名 glob + 内容 glob + 效果。"""

    tool_pattern: str
    content_pattern: str
    effect: Effect

    @classmethod
    def parse(cls, rule_str: str, effect_str: str, *, source: str = "") -> PermissionRule:
        """解析 `ToolName(pattern)` 行；格式非法抛 ValueError 并定位来源。"""
        loc = f"（{source}）" if source else ""
        m = _RULE_RE.match(rule_str)
        if m is None:
            raise ValueError(f"权限规则格式非法{loc}：{rule_str!r}，应为 ToolName(pattern)")
        effect = effect_str.strip().lower()
        if effect not in _EFFECTS:
            raise ValueError(f"权限规则效果非法{loc}：{effect_str!r}，应为 allow/deny/ask 之一")
        return cls(
            tool_pattern=m.group(1).strip(),
            content_pattern=m.group(2).strip(),
            effect=cast(Effect, effect),
        )

    def matches(self, tool_name: str, content: str) -> bool:
        # 大小写不敏感（🔴 review 修复）：Windows 文件系统语义，deny `*.env*`
        # 必须命中 `.ENV`；统一 casefold 后再 glob，跨平台一致。
        return fnmatch.fnmatch(tool_name.casefold(), self.tool_pattern.casefold()) and (
            fnmatch.fnmatch(content.casefold(), self.content_pattern.casefold())
        )


class RuleEngine:
    """规则装载 + 合并裁决（deny > ask > allow，未命中返回 unknown）。"""

    def __init__(self) -> None:
        self._rules: list[PermissionRule] = []
        self._local_path: Path | None = None

    @property
    def local_path(self) -> Path | None:
        """本地规则文件路径（「始终允许」追加目标）。"""
        return self._local_path

    def add(self, rule: PermissionRule) -> None:
        """直灌一条已解析规则（程序化配置 / 测试用）。"""
        self._rules.append(rule)

    def load(self, rule_file: Path, *, local: bool = False) -> None:
        """加载一份规则文件；文件不存在按空集跳过。

        文件损坏（YAML 非法/条目结构错）降级为空规则集 + stderr 警告，MUST NOT
        阻断启动（🔴 review 修复：learn 曾写坏文件导致应用起不来）。
        本地文件即使不存在也记录 `_local_path`（learn 目标），否则新建前 learn 会静默失效。
        """
        if local:
            self._local_path = rule_file
        if not rule_file.is_file():
            return
        try:
            self._load_entries(rule_file)
        except (yaml.YAMLError, ValueError) as exc:
            _warn_corrupt(rule_file, str(exc))

    def _load_entries(self, rule_file: Path) -> None:
        """解析并装载一份规则文件（损坏抛异常，由 load 降级捕获）。"""
        text = rule_file.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        if data is None:
            return
        if not isinstance(data, list):
            raise ValueError(f"应为 YAML 列表，实际 {type(data).__name__}")
        for item in data:
            if not isinstance(item, dict):
                raise ValueError(f"权限规则条目非法：{item}")
            rule_str = item.get("rule")
            effect_str = item.get("effect")
            if not isinstance(rule_str, str) or not isinstance(effect_str, str):
                raise ValueError(f"权限规则条目非法：需含 rule 与 effect 字符串字段，实际 {item}")
            self._rules.append(PermissionRule.parse(rule_str, effect_str, source=str(rule_file)))

    def load_many(self, rule_files: list[Path], local_path: Path | None = None) -> None:
        """按序加载多份规则（用户级 → 项目级 → 本地级），本地文件记录为 learn 目标。"""
        for rf in rule_files:
            self.load(rf)
        if local_path is not None:
            self.load(local_path, local=True)

    def evaluate(self, tool_name: str, content: str) -> tuple[Effect | None, str | None]:
        """合并裁决。返回 (effect, 命中的规则串)；未命中 (None, None)。"""
        hit: Effect | None = None
        rule_str: str | None = None
        for rule in self._rules:
            if not rule.matches(tool_name, content):
                continue
            if rule.effect == "deny":
                return "deny", f"{rule.tool_pattern}({rule.content_pattern})"
            if rule.effect == "ask":
                hit = "ask"
                rule_str = f"{rule.tool_pattern}({rule.content_pattern})"
            elif hit is None:
                hit = "allow"
                rule_str = f"{rule.tool_pattern}({rule.content_pattern})"
        return hit, rule_str

    def learn(self, tool_name: str, content: str) -> None:
        """「始终允许」：追加一条 allow 规则到本地文件（yaml.safe_dump 原子重写）。

        🔴 review 修复：原 f-string 手拼 YAML 不转义引号/换行，含引号命令会写坏
        文件导致下次启动解析失败。改为读现有条目（容错）→ 追加 → safe_dump 整体
        原子重写；时间戳落 `learned_at` 字段（safe_dump 不产注释）。
        """
        if self._local_path is None:
            return
        self._local_path.parent.mkdir(parents=True, exist_ok=True)
        # 内容过长截断，避免本地规则文件被单条撑爆。
        pattern = content if len(content) <= 200 else content[:200]
        rule_str = f"{tool_name}({pattern})"
        try:
            entries: list[dict[str, str]] = []
            data = yaml.safe_load(self._local_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                entries = [
                    {"rule": it["rule"], "effect": it["effect"]}
                    for it in data
                    if isinstance(it, dict) and isinstance(it.get("rule"), str)
                    and isinstance(it.get("effect"), str)
                ]
        except (OSError, yaml.YAMLError):
            entries = []  # 文件读不了/损坏 → 从头重建（learn 动作本身即用户确认）
        entries.append(
            {
                "rule": rule_str,
                "effect": "allow",
                "learned_at": time.strftime("%Y-%m-%d %H:%M"),
            }
        )
        _atomic_dump_yaml(self._local_path, entries)
        # 同步内存，同类操作本进程内立即放行。
        self._rules.append(PermissionRule.parse(
            f"{tool_name}({pattern})", "allow", source=str(self._local_path)
        ))

    def __len__(self) -> int:
        return len(self._rules)
