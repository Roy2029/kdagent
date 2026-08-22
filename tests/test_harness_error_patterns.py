"""错误模式沉淀测试（08 §3.3 feedback 消费方，T33-3）。

写工具失败 → 诊断分类（纯函数）→ 沉淀为 feedback 记忆 + agent 接线去重。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from kdagent.config import Config
from kdagent.engine.agent import Agent
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import LLMStreamEvent, Payload
from kdagent.engine.messages import ToolUseBlock
from kdagent.harness.error_patterns import (
    ErrorPatternKind,
    diagnose_failure,
    pattern_dedup_key,
    pattern_memory,
)
from kdagent.memory.model import MemoryFile
from kdagent.memory.store import MemoryStore
from kdagent.tools import build_default_registry


class FakeLLM:
    def __init__(self, responses: list[list[LLMStreamEvent]]) -> None:
        self._responses = responses
        self.call_count = 0

    async def stream_chat(self, payload: Payload) -> AsyncIterator[LLMStreamEvent]:
        self.call_count += 1
        for ev in self._responses.pop(0):
            yield ev


def _done(text: str = "完成") -> list[LLMStreamEvent]:
    return [
        LLMStreamEvent(type="text_delta", text=text),
        LLMStreamEvent(type="stop", stop_reason="end_turn"),
    ]


def _tool(name: str, input: dict[str, Any], id_: str = "t") -> list[LLMStreamEvent]:
    return [
        LLMStreamEvent(type="tool_use", tool_use=ToolUseBlock(id=id_, name=name, input=input)),
        LLMStreamEvent(type="stop", stop_reason="tool_use"),
    ]


def _make_agent(
    responses: list[list[LLMStreamEvent]], work_dir: Path, memory_store: MemoryStore | None = None
) -> Agent:
    conv = ConversationManager()
    return Agent(
        config=Config(),
        llm=FakeLLM(responses),
        conversation=conv,
        tools=build_default_registry(),
        events=lambda _: None,
        work_dir=work_dir,
        memory_store=memory_store,
    )


def _list_memory_names(store: MemoryStore) -> set[str]:
    return {f.name for f in store.list_all()}


# ---- 纯函数：diagnose_failure 根因分类 ----

_F = "EditFile"  # 简短别名


def test_diagnose_target_missing() -> None:
    p = diagnose_failure(_F, "文件不存在：D:/a.py")
    assert p is not None and p.kind == ErrorPatternKind.TARGET_MISSING


def test_diagnose_target_missing_read_error() -> None:
    p = diagnose_failure(_F, "读取失败：[Errno 2] No such file or directory: 'x.py'")
    assert p is not None and p.kind == ErrorPatternKind.TARGET_MISSING


def test_diagnose_no_match() -> None:
    p = diagnose_failure(_F, "old_string 未在文件中找到")
    assert p is not None and p.kind == ErrorPatternKind.NO_MATCH


def test_diagnose_ambiguous_match() -> None:
    p = diagnose_failure(_F, "old_string 出现 3 次，需唯一匹配才能替换")
    assert p is not None and p.kind == ErrorPatternKind.AMBIGUOUS_MATCH


def test_diagnose_permission_denied() -> None:
    p = diagnose_failure(_F, "无权限写入：/sys/x")
    assert p is not None and p.kind == ErrorPatternKind.PERMISSION_DENIED


def test_diagnose_invalid_input_validation() -> None:
    p = diagnose_failure(_F, "参数校验失败：\npath 必须是绝对路径")
    assert p is not None and p.kind == ErrorPatternKind.INVALID_INPUT


def test_diagnose_invalid_path_absolute_alone() -> None:
    p = diagnose_failure(_F, "路径非法：相对路径越界")
    assert p is not None and p.kind == ErrorPatternKind.INVALID_PATH


def test_diagnose_other_fallback() -> None:
    p = diagnose_failure(_F, "写入失败：[Errno 28] No space left on device")
    assert p is not None and p.kind == ErrorPatternKind.OTHER


def test_diagnose_non_write_tool_returns_none() -> None:
    assert diagnose_failure("Bash", "命令失败") is None
    assert diagnose_failure("ReadFile", "文件不存在") is None


def test_diagnose_write_tool_success_text_still_classified() -> None:
    # is_error=True 才调用本函数；即便内容看似成功描述也归 OTHER 兜底。
    p = diagnose_failure(_F, "已替换 1 处到 D:/a.py")
    assert p is not None and p.kind == ErrorPatternKind.OTHER


# ---- 纯函数：pattern_memory / pattern_dedup_key ----

_T = "EditFile"


def test_pattern_memory_is_feedback_type() -> None:
    mf = pattern_memory(diagnose_failure(_T, "old_string 未在文件中找到"))  # type: ignore[arg-type]
    assert mf is not None
    assert mf.type == "feedback"  # 用户级记忆，跨项目复用
    assert mf.name == "error-pattern-edit-no-match"
    assert mf.description == "EditFile 失败根因：编辑原文与文件内容不符"
    assert "old_string 未在文件中找到" in mf.content  # 失败内容原文进正文
    assert "**Why:**" in mf.content and "**How to apply:**" in mf.content
    assert "error-pattern-edit-no-match.md" in mf.index_line  # 索引指针


def test_pattern_dedup_key_is_kind() -> None:
    a = diagnose_failure(_T, "old_string 未在文件中找到")
    b = diagnose_failure(_T, "old_string 未在文件中找到：abc")
    assert a is not None and b is not None
    assert pattern_dedup_key(a) == pattern_dedup_key(b) == ErrorPatternKind.NO_MATCH.value


# ---- agent 接线：写工具失败沉淀 + 去重 ----

_MISSING_CRITERIA_F = "EditFile"


async def test_no_memory_store_skips_silently(tmp_path: Path) -> None:
    """无 memory_store（08 未启用）→ Edit 失败不沉淀也不崩。"""
    agent = _make_agent(
        [
            _tool(_MISSING_CRITERIA_F, {"path": str(tmp_path / "nope.py"), "old_string": "x", "new_string": "y"}),
            _done(),
        ],
        tmp_path,
    )
    await agent.run("任务")
    assert agent._seen_patterns == set()  # type: ignore[attr-defined]


async def test_edit_failure_creates_feedback_memory(tmp_path: Path) -> None:
    """Edit 不存在的文件 → 诊断 TARGET_MISSING → 沉淀 feedback 记忆 + 索引。"""
    store = MemoryStore(tmp_path / "user", tmp_path / "proj")
    agent = _make_agent(
        [
            _tool(_MISSING_CRITERIA_F, {"path": str(tmp_path / "nope.py"), "old_string": "x", "new_string": "y"}),
            _done(),
        ],
        tmp_path,
        memory_store=store,
    )
    await agent.run("任务")
    names = _list_memory_names(store)
    assert "error-pattern-edit-target-missing" in names
    idx = (store.user_root / "MEMORY.md").read_text(encoding="utf-8")
    assert "error-pattern-edit-target-missing.md" in idx  # 索引指针已挂


async def test_same_kind_failure_deduped(tmp_path: Path) -> None:
    """同类根因重复失败只沉淀一条（本会话 _seen_patterns + MemoryStore.create 双防）。"""
    store = MemoryStore(tmp_path / "user", tmp_path / "proj")
    agent = _make_agent(
        [
            _tool(_MISSING_CRITERIA_F, {"path": str(tmp_path / "a.py"), "old_string": "x", "new_string": "y"}),
            _tool(_MISSING_CRITERIA_F, {"path": str(tmp_path / "b.py"), "old_string": "x", "new_string": "y"}),
            _done(),
        ],
        tmp_path,
        memory_store=store,
    )
    await agent.run("任务")
    assert _list_memory_names(store) == {"error-pattern-edit-target-missing"}


async def test_successful_write_no_memory(tmp_path: Path) -> None:
    """写成功不沉淀（只有 is_error 才诊断）。"""
    store = MemoryStore(tmp_path / "user", tmp_path / "proj")
    agent = _make_agent(
        [
            _tool("WriteFile", {"path": str(tmp_path / "a.py"), "content": "x = 1"}),
            _done(),
        ],
        tmp_path,
        memory_store=store,
    )
    await agent.run("任务")
    assert _list_memory_names(store) == set()


async def test_different_kinds_each_precipitate(tmp_path: Path) -> None:
    """不同根因各沉淀一条。"""
    store = MemoryStore(tmp_path / "user", tmp_path / "proj")
    agent = _make_agent(
        [
            _tool("EditFile", {"path": str(tmp_path / "nope.py"), "old_string": "x", "new_string": "y"}),
            _tool("WriteFile", {"path": str(tmp_path / "other.py"), "content": "hello"}),
            _tool("EditFile", {"path": str(tmp_path / "other.py"), "old_string": "不存在的", "new_string": "y"}),
            _done(),
        ],
        tmp_path,
        memory_store=store,
    )
    await agent.run("任务")
    names = _list_memory_names(store)
    assert "error-pattern-edit-target-missing" in names
    assert "error-pattern-edit-no-match" in names


# ---- M1 静默读：记忆索引注入 system-reminder ----

def test_assemble_payload_injects_memory_index(tmp_path: Path) -> None:
    """新会话 payload：记忆索引以 `<system-reminder>` 注入 system，并提示直接 ReadFile。

    M1 修复：索引不再埋在 system 中间，而是醒目 system-reminder（与 09 §3.5 延迟工具
    同机制，改 reminder 不改 system → 前缀缓存不受影响）。只注索引指针不注全文。
    """
    store = MemoryStore(tmp_path / "user", tmp_path / "proj")
    store.create(MemoryFile(name="沙箱环境", description="记录沙箱约束", type="project", content="body"))
    agent = _make_agent([_done()], tmp_path, memory_store=store)
    system = agent._assemble_payload().system
    assert "<system-reminder>" in system
    assert "记忆索引已随初始上下文加载" in system
    assert "沙箱环境.md" in system  # 索引指针在
    assert "记录沙箱约束" in system  # 索引行描述在
    assert "body" not in system  # 不注全文（省 token）


def test_assemble_payload_skips_memory_without_store(tmp_path: Path) -> None:
    """无 memory_store（08 未启用）→ 不注入记忆索引。"""
    agent = _make_agent([_done()], tmp_path)
    system = agent._assemble_payload().system
    assert "记忆索引已随初始上下文加载" not in system
    assert "<system-reminder>" not in system  # 本用例无 skills/mcp 时无 reminder
