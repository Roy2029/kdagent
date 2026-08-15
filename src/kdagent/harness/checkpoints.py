"""双层检查点（规格 12 §3.3，T33 已决，M5 遗留第二块）。

长任务微小的路径偏移累积成灾难——检查点在步骤边界对照完成判据核验产出、自检目标
一致性。双层结构：

- 第一层 声明驱动（主检查点，agent 主动报站）：TodoWrite 更新到步骤边界（task 标
  completed 且有 accept_criteria）→ 注入「产出 vs 判据」验证 + 目标一致性自检
- 第二层 行为观察（兜底，不依赖 agent 自觉）：
  ① 声明 vs 行为不一致 —— 见遗留（完成判据可机械验证的自动核验）
  ② 遗忘更新：todo 长期滞后于实际行为 → 强制刷新快照 + 提示补更新
  ③ 安全类信号：断路器（02 已实现）+ 权限拒绝（06 已实现）+ 跨文件大改（本模块）

纯函数可单测；agent 接线在 engine/agent.py（_observe_todos）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from kdagent.tools.todo import format_todos

# 行为观察②：连续 N 轮工具活动但 todo 未更新 → 强制刷新（§3.3 第二层②）
STALE_TODO_THRESHOLD = 5
# 行为观察③：单轮 write/edit 目标数超过此值 → 跨文件大改警告
LARGE_CHANGE_THRESHOLD = 5
# 告警冷却：同类 reminder 距上次注入至少 N 轮才再注入（防刷屏）
REINJECT_COOLDOWN = 3
# Replan 判定信号（§3.3 Replan 机制，D57）：断路器连续触发 ≥ N 次 =
# 路径反复受阻不可行 → 注入 Replan 引导（整体重写 todo 而非硬着头皮）
REPLAN_TRIGGER_COUNT = 2

# ---- 行为观察①：声明 vs 行为不一致的自动核验（§3.3 第二层①，D58） ----

# 机械判据类型（§3.2 引导的「可机械验证」判据形式）
class VerificationKind(StrEnum):
    TEST = "test"  # 判据要求测试通过 → 证据 = 会话跑过测试
    FILE = "file"  # 判据要求文件存在 → 证据 = 文件系统当前状态
    SOFT = "soft"  # 软判据 → 不自动核验，靠自评（D54 既有路径）


# 测试类判据关键词（小写匹配；英文不取裸 test——「改 test_x.py」会误判）
_TEST_KEYWORDS = ("测试", "用例", "单测", "pytest", "tox", "nosetests", "跑通")
# file 类判据提取目标文件路径：判据里第一个类路径 token（容忍 ./ 与 \ 分隔）
_FILE_PATTERN = re.compile(r"[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,8}")


def classify_criteria(criteria: str) -> VerificationKind:
    """判据文本 → 自动核验类型。

    test：含测试关键词（「测试全绿」/「pytest 通过」）→ 证据 = 会话跑过测试。
    file：要求文件存在/创建且含路径 → 证据 = 文件系统当前状态。
    其余（探索型自评判据）→ soft，不自动核验。
    """
    text = criteria.lower()
    if any(k in text for k in _TEST_KEYWORDS):
        return VerificationKind.TEST
    if ("存在" in text or "创建" in text) and _FILE_PATTERN.search(criteria):
        return VerificationKind.FILE
    return VerificationKind.SOFT


def file_target(criteria: str) -> str | None:
    """file 类判据 → 目标文件路径（判据里第一个类路径 token，无则 None）。"""
    m = _FILE_PATTERN.search(criteria)
    return m.group(0) if m else None


def has_test_evidence(tool_names: list[str], bash_commands: list[str]) -> bool:
    """行为证据（第二层① test 类）：会话是否跑过测试。

    TestRunner 工具调用（12 §3.1）直接算证据；Bash 命令含 pytest/tox/nosetests
    也算。其余（只 ReadFile / 没跑过测试）→ 无证据 = 声明与行为不一致。
    """
    if "TestRunner" in tool_names:
        return True
    return any(
        re.search(r"\b(pytest|tox|nosetests)\b", cmd, re.IGNORECASE)
        for cmd in bash_commands
    )


@dataclass(frozen=True, slots=True)
class CheckpointEvent:
    """步骤边界事件（§3.3 第一层）：刚完成的 task + 完成判据。"""

    todo_content: str
    task_content: str
    step_description: str
    accept_criteria: str


def _completed_tasks(todos: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """todo → completed task 的 (todo_content, task_content) 集合。"""
    return {
        (str(t.get("content", "")), str(tk.get("content", "")))
        for t in todos
        for tk in t.get("tasks", []) or []
        if tk.get("status") == "completed"
    }


def todo_progress(
    before: list[dict[str, Any]] | None, after: list[dict[str, Any]]
) -> CheckpointEvent | None:
    """前后快照对比，找刚标 completed 且有判据的 task → 步骤边界事件。

    边界取该 task 最后一个 step 的 accept_criteria（完成型判据，§3.2 T36）。
    无 before（首次 TodoWrite）或无新增 completed 任务 → None。
    """
    if before is None:
        return None
    newly_done = _completed_tasks(after) - _completed_tasks(before)
    if not newly_done:
        return None
    for todo in after:
        for task in todo.get("tasks", []) or []:
            key = (str(todo.get("content", "")), str(task.get("content", "")))
            if key not in newly_done:
                continue
            steps = task.get("steps", []) or []
            step = steps[-1] if steps else {}
            description = str(step.get("description", "")) if isinstance(step, dict) else ""
            criteria = str(step.get("accept_criteria", "") or "") if isinstance(step, dict) else ""
            return CheckpointEvent(
                todo_content=key[0],
                task_content=key[1],
                step_description=description,
                accept_criteria=criteria,
            )
    return None


def build_checkpoint_reminder(event: CheckpointEvent, todos: list[dict[str, Any]]) -> str:
    """第一层检查点 reminder：判据验证 + 目标一致性自检 + 最新 todo 快照（§3.3 绑定）。"""
    lines = [
        "<system-reminder>",
        f"步骤边界检查点：刚完成任务「{event.task_content}」（目标：{event.todo_content}）。",
    ]
    if event.accept_criteria:
        lines.append(f"完成判据：{event.accept_criteria}")
    else:
        lines.append("该任务无机械判据，请简要自评产出是否达成目标。")
    lines.append("请对照判据核验产出（跑测试/检查文件），确认目标一致性。当前 todo 快照：")
    lines.append(format_todos(todos))
    lines.append("</system-reminder>")
    return "\n".join(lines)


def build_stale_todo_reminder(todos: list[dict[str, Any]]) -> str:
    """行为观察②：todo 落后于行为 → 强制刷新快照 + 提示补更新（§3.3 第二层②）。"""
    return (
        "<system-reminder>\n"
        f"todo 已连续 {STALE_TODO_THRESHOLD} 轮工具操作未更新，落后于实际行为。"
        "请更新 todo（报站），保持规划与进度一致。当前快照：\n"
        + format_todos(todos)
        + "\n</system-reminder>"
    )


def build_large_change_warning(paths: list[str]) -> str:
    """行为观察③：单轮跨文件大改警告（安全类信号，始终生效）。"""
    shown = "\n".join(f"- {p}" for p in paths[:10])
    more = f"  … 等 {len(paths)} 个" if len(paths) > 10 else ""
    return (
        "<system-reminder>\n"
        f"检测到单轮跨文件大改（{len(paths)} 个文件），请确认变更范围、评估破坏风险，"
        f"并在 todo 中反映进展：\n{shown}{more}\n</system-reminder>"
    )


def build_replan_reminder(todos: list[dict[str, Any]]) -> str:
    """Replan 引导（§3.3 Replan 机制，D57）：路径反复受阻 → 整体重写 todo。

    断路器已连续 REPLAN_TRIGGER_COUNT 次触发 = 当前路径不可行。引导模型 Replan：
    废弃旧列表整体重写（不修补），重新拆步骤；含最新 todo 快照对表。
    """
    return (
        "<system-reminder>\n"
        f"路径反复受阻：工具已连续 {REPLAN_TRIGGER_COUNT} 次触发失败熔断，当前做法走不通。"
        "请 Replan——整体重写 todo（废弃旧列表，不修补），重新拆解步骤换路径，"
        "不要在同一方向上硬着头皮重试。当前 todo 快照：\n"
        + format_todos(todos)
        + "\n</system-reminder>"
    )


def build_mismatch_reminder(
    event: CheckpointEvent, todos: list[dict[str, Any]], reason: str
) -> str:
    """行为观察①：声明 vs 行为不一致 → 拦截警告（§3.3 第二层①，D58）。

    步骤边界 agent 声称完成（task 标 completed），但机械判据的证据缺失——
    判据要求测试通过却未跑测试 / 判据要求文件存在却不存在。比自检 reminder
    更尖锐：拦截完成声明，要求补证据后重新报站；含完整 todo 快照对表。
    """
    return (
        "<system-reminder>\n"
        f"声明与行为不一致：刚标记完成「{event.task_content}」（目标：{event.todo_content}），"
        f"完成判据「{event.accept_criteria}」的证据缺失——{reason}。"
        "请补足证据后重新核验，再在 todo 中报站。当前快照：\n"
        + format_todos(todos)
        + "\n</system-reminder>"
    )
