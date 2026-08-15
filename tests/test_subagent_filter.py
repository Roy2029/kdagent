"""工具过滤四层（规格 10 §3.6）。"""

from __future__ import annotations

from kdagent.subagent import (
    ALL_AGENT_DISALLOWED_TOOLS,
    ASYNC_AGENT_ALLOWED_TOOLS,
    filter_tools,
)
from kdagent.subagent.model import AgentDef
from kdagent.tools import build_default_registry
from kdagent.tools.registry import ToolRegistry


class _Fake:
    name = "Agent"
    description = "fake"
    input_schema = {"type": "object", "properties": {}}
    category = "system"
    require_confirm = True

    def is_read_only(self) -> bool:
        return False

    def is_destructive(self) -> bool:
        return True

    def is_concurrency_safe(self, input: dict) -> bool:
        return False

    def validate_input(self, input: dict) -> list[str]:
        return []

    async def execute(self, ctx, input):
        return None


def _add_task_tools(r: ToolRegistry) -> None:
    from kdagent.subagent.task import TaskCreate, TaskGet, TaskList, TaskUpdate

    class _M:
        def __init__(self) -> None:
            self._tasks = {}
            self._counter = 0

        def get(self, id: str):
            return None

        def list(self):
            return []

    m = _M()
    for tool in (TaskList(m), TaskGet(m), TaskCreate(m), TaskUpdate(m)):
        r.register(tool)


def _full_registry() -> ToolRegistry:
    r = build_default_registry()
    r.register(_Fake())
    _add_task_tools(r)
    return r


def test_layer1_global_disallowed() -> None:
    """第 1 层：所有子 Agent（含 Fork）看不到 Agent/AskUserQuestion/Task*。"""
    r = _full_registry()
    filtered = filter_tools(r)
    names = {t.name for t in filtered.all()}
    assert "Agent" not in names
    assert "TaskList" not in names
    assert "TaskGet" not in names
    assert "TaskCreate" not in names
    assert "TaskUpdate" not in names
    assert "ReadFile" in names  # 正常工具不受影响


def test_layer1_covers_all_fork() -> None:
    """Fork 继承全部工具（除第 1 层）：TodoWrite 仍在、Agent/Task 被禁。"""
    r = _full_registry()
    filtered = filter_tools(r, fork=True)
    names = {t.name for t in filtered.all()}
    assert "TodoWrite" in names  # Fork 继承全部（不过滤）
    assert "Agent" not in names  # 第 1 层防递归
    assert "TaskList" not in names


def test_layer2_disallowed_tools() -> None:
    """第 2/4 层：disallowedTools 黑名单排除（定义式走工具过滤）。"""
    definition = AgentDef(
        name="explore", description="readonly", disallowed_tools=("EditFile", "WriteFile")
    )
    filtered = filter_tools(_full_registry(), definition)
    names = {t.name for t in filtered.all()}
    assert "EditFile" not in names
    assert "WriteFile" not in names
    assert "ReadFile" in names
    assert "Glob" in names


def test_layer4_whitelist_then_blacklist() -> None:
    """第 4 层：tools 白名单定范围，disallowedTools 黑名单从中排除。"""
    definition = AgentDef(
        name="plan",
        description="readonly",
        tools=("Glob", "Grep", "ReadFile", "EditFile"),
        disallowed_tools=("EditFile",),
    )
    filtered = filter_tools(_full_registry(), definition)
    names = {t.name for t in filtered.all()}
    assert names == {"Glob", "Grep", "ReadFile"}


def test_layer3_background_whitelist() -> None:
    """第 3 层：后台 Agent 只用基础读写/搜索/Bash（不含 Agent/Task*/TodoWrite）。"""
    r = _full_registry()
    filtered = filter_tools(r, background=True)
    names = {t.name for t in filtered.all()}
    assert names == ASYNC_AGENT_ALLOWED_TOOLS
    assert "TodoWrite" not in names
    assert "Agent" not in names


def test_background_with_definition() -> None:
    """后台 + 定义式：第 3 层白名单 ∩ 第 2/4 层 def 过滤叠加。"""
    definition = AgentDef(
        name="explore", description="readonly", disallowed_tools=("Bash",)
    )
    filtered = filter_tools(_full_registry(), definition, background=True)
    names = {t.name for t in filtered.all()}
    # 后台白名单内，且 Bash 被 def 排除
    assert names == ASYNC_AGENT_ALLOWED_TOOLS - {"Bash"}


def test_fork_background_only_layer1_and_3() -> None:
    """Fork + 后台：继承全部工具但不限 def（第 2/4 跳过），套第 1+3 层。"""
    filtered = filter_tools(_full_registry(), fork=True, background=True)
    names = {t.name for t in filtered.all()}
    assert names == ASYNC_AGENT_ALLOWED_TOOLS  # 后台白名单 = 基础工具


def test_constants() -> None:
    assert "Agent" in ALL_AGENT_DISALLOWED_TOOLS
    assert "AskUserQuestion" in ALL_AGENT_DISALLOWED_TOOLS
    assert "TodoWrite" not in ASYNC_AGENT_ALLOWED_TOOLS
