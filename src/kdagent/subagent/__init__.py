"""SubAgent 子系统（规格 10）：Agent≈Tool / 后台任务 / 工具过滤四层。"""

from pathlib import Path

from kdagent.subagent.agent_tool import Agent
from kdagent.subagent.manager import AgentManager
from kdagent.subagent.model import (
    AGENT_NAME_RE,
    DEFAULT_MAX_TURNS,
    AgentDef,
    parse_agent_file,
    parse_agent_text,
)
from kdagent.subagent.named import (
    NamedAgent,
    NamedAgentError,
    NamedAgentManager,
    SendMessage,
)
from kdagent.subagent.runner import (
    ALL_AGENT_DISALLOWED_TOOLS,
    ASYNC_AGENT_ALLOWED_TOOLS,
    FORK_BOILERPLATE,
    FORK_SYSTEM_PROMPT,
    SubAgentResult,
    SubAgentRunner,
    build_forked_messages,
    filter_tools,
)
from kdagent.subagent.task import (
    BackgroundTask,
    TaskCreate,
    TaskGet,
    TaskList,
    TaskManager,
    TaskUpdate,
)
from kdagent.subagent.worktree import (
    DEFAULT_TEMP_PREFIX,
    Worktree,
    WorktreeError,
    WorktreeManager,
    validate_name,
)

BUILTIN_AGENTS_DIR = Path(__file__).parent / "builtin"

__all__ = [
    "AGENT_NAME_RE",
    "ALL_AGENT_DISALLOWED_TOOLS",
    "ASYNC_AGENT_ALLOWED_TOOLS",
    "Agent",
    "AgentDef",
    "AgentManager",
    "BUILTIN_AGENTS_DIR",
    "BackgroundTask",
    "DEFAULT_MAX_TURNS",
    "DEFAULT_TEMP_PREFIX",
    "FORK_BOILERPLATE",
    "FORK_SYSTEM_PROMPT",
    "NamedAgent",
    "NamedAgentError",
    "NamedAgentManager",
    "SendMessage",
    "SubAgentResult",
    "SubAgentRunner",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskManager",
    "TaskUpdate",
    "Worktree",
    "WorktreeError",
    "WorktreeManager",
    "build_forked_messages",
    "filter_tools",
    "parse_agent_file",
    "parse_agent_text",
    "validate_name",
]
