"""AgentManager：三级搜索 / Agent 类型注册（规格 10 §3.4）。

三级优先级，同名高优先级覆盖低优先级（同 Skill 搜索路径）：
1. 项目级 {work_dir}/.kdagent/agents/   # 可提交 git、团队共享
2. 用户级 ~/.kdagent/agents/            # 个人通用
3. 内置级 程序包内 subagent/builtin/     # 开箱即用 4 类

Verification 默认关（规格 10 T27）：内置 verification.md 只有
`enable_verification_agent: true` 时才扫描进结果。
"""

from __future__ import annotations

import builtins
from pathlib import Path

from kdagent.subagent.model import AGENT_NAME_RE, AgentDef, parse_agent_file


class AgentManager:
    """扫描 agents 目录注册 AgentDef；按名查询；提供类型清单。"""

    def __init__(
        self,
        dirs: list[Path],
        *,
        enable_verification: bool = False,
    ) -> None:
        self._dirs = list(dirs)  # 高优先级在前：[项目, 用户, 内置]
        self._enable_verification = enable_verification
        self._agents: dict[str, AgentDef] = {}

    @property
    def enable_verification(self) -> bool:
        return self._enable_verification

    def scan(self) -> None:
        """重扫三个目录（启动时 + 用户新建 Agent 后刷新）。高优先级覆盖低优先级。"""
        found: dict[str, AgentDef] = {}
        for d in self._dirs:
            if not d.is_dir():
                continue
            for definition in _iter_agent_files(d):
                # 内置 verification 受配置开关控制（规格 10 T27 默认关）。
                if definition.name == "verification" and not self._enable_verification:
                    continue
                if definition.name not in found:
                    found[definition.name] = definition
        self._agents = found

    def get(self, name: str) -> AgentDef | None:
        return self._agents.get(name)

    def list(self) -> list[AgentDef]:
        """按名排序的定义列表（/skills 式查看 / Agent 工具提示用）。"""
        return sorted(self._agents.values(), key=lambda d: d.name)

    def types_markdown(self) -> str:
        """可用 Agent 类型清单（喂给 Agent 工具的 description 动态展示）。"""
        if not self._agents:
            return "（无可用 Agent 类型，可新建 {work_dir}/.kdagent/agents/<name>.md）"
        lines = [f"- {d.name}：{d.description}" for d in self.list()]
        return "\n".join(lines)

    def type_names(self) -> builtins.list[str]:
        return [d.name for d in self.list()]

    def validate_type(self, name: str) -> bool:
        """subagent_type 参数合法性校验（Agent 工具调用前）。"""
        return bool(AGENT_NAME_RE.fullmatch(name or "")) and name in self._agents


def _iter_agent_files(d: Path) -> list[AgentDef]:
    """扫描目录内 Agent 定义：单文件顶层 *.md + 目录型 */AGENT.md（入口）。

    顶层 AGENT.md（无名字上下文）跳过。坏文件（无 frontmatter）跳过。
    """
    definitions: list[AgentDef] = []
    for p in sorted(d.glob("*.md")):
        if p.name == "AGENT.md":
            continue
        definition = parse_agent_file(p)
        if definition is not None:
            definitions.append(definition)
    for p in sorted(d.glob("*/AGENT.md")):
        definition = parse_agent_file(p)
        if definition is not None:
            definitions.append(definition)
    return definitions
