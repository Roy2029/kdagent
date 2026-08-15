"""M2-a L1 大结果落盘测试（规格 01 §5.2 / §11 第 1、2 项）。"""

from __future__ import annotations

from pathlib import Path

from conftest import FakeLLM, done

from kdagent.config import Config
from kdagent.context.context_manager import ContextManager
from kdagent.context.history import ProcessedToolResult
from kdagent.context.tool_result_handler import (
    PREVIEW_CHARS,
    ToolResultHandler,
)
from kdagent.engine.agent import Agent
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import LLMStreamEvent
from kdagent.engine.messages import ToolResultBlock, ToolUseBlock
from kdagent.tools import build_default_registry
from kdagent.tools.base import ToolResult


def _big_result(content: str, tool_use_id: str = "t1") -> ToolResult:
    return ToolResult(tool_use_id=tool_use_id, name="Bash", content=content)


async def test_single_result_over_threshold_persisted(tmp_path: Path) -> None:
    """单条 60K 结果 → 历史只有预览+路径，无原文；落盘文件保留完整内容。"""
    handler = ToolResultHandler(tmp_path)
    big = _big_result("x" * 60_000)
    processed = await handler.handle_single(big)

    assert isinstance(processed, ProcessedToolResult)
    assert processed.persisted is not None
    assert processed.persisted.full_size == 60_000
    # 历史中不含原文，含路径与预览
    assert "x" * 60_000 not in processed.content
    assert "<persisted-output>" in processed.content
    assert processed.persisted.path in processed.content
    # 落盘文件完整保留
    written = Path(processed.persisted.path).read_text(encoding="utf-8")
    assert written == "x" * 60_000


async def test_single_result_within_threshold_passthrough(tmp_path: Path) -> None:
    """单条 20K 结果 → 原样写入，不落盘。"""
    handler = ToolResultHandler(tmp_path)
    ok = _big_result("y" * 20_000)
    processed = await handler.handle_single(ok)
    assert processed.persisted is None
    assert processed.content == "y" * 20_000
    assert not list(tmp_path.glob("*.txt"))


async def test_preview_is_limited_to_2kb(tmp_path: Path) -> None:
    """预览只取前 2KB（§5.2），不把全文塞进历史。"""
    handler = ToolResultHandler(tmp_path)
    processed = await handler.handle_single(_big_result("z" * 80_000))
    assert processed.persisted is not None
    assert processed.persisted.preview == "z" * PREVIEW_CHARS


async def test_readfile_readback_not_re_persisted(tmp_path: Path) -> None:
    """ReadFile 读回落盘文件（persist_exempt）→ 跳过 L1，不再二次落盘（§5.2 读回闭环）。"""
    handler = ToolResultHandler(tmp_path)
    big = _big_result("w" * 60_000)
    first = await handler.handle_single(big)
    assert first.persisted is not None

    # 模拟 ReadFile 读回落盘文件：内容仍是 60K，但带 persist_exempt=True
    readback = ToolResult(
        tool_use_id="t2", name="ReadFile", content="w" * 60_000, persist_exempt=True
    )
    second = await handler.handle_single(readback)
    assert second.persisted is None  # 不再落盘
    assert second.content == "w" * 60_000  # 全文直接进历史
    # 只有第一个落盘文件
    files = list(tmp_path.glob("*.txt"))
    assert len(files) == 1


async def test_aggregate_over_threshold_persists_biggest(tmp_path: Path) -> None:
    """一轮并行 5 个结果合计 209K → 最大的 45K 落盘，总量回到 200K 预算内（§5.2）。"""
    handler = ToolResultHandler(tmp_path)
    sizes = [42_000, 38_000, 45_000, 40_000, 44_000]
    results = [_big_result(chr(65 + i) * size, tool_use_id=f"t{i}") for i, size in enumerate(sizes)]
    processed = await handler.handle_batch(results)

    # 总量回到预算内
    total = sum(len(p.content) for p in processed)
    assert total <= 200_000
    # 只有最大的 45K 那条被落盘替换成预览
    persisted = [p for p in processed if p.persisted is not None]
    assert len(persisted) == 1
    assert persisted[0].persisted is not None
    assert persisted[0].persisted.full_size == 45_000
    # 其余四条保留原文
    rest = [p for p in processed if p.persisted is None]
    assert sorted(len(p.content) for p in rest) == [38_000, 40_000, 42_000, 44_000]
    # 落盘文件名沿用 tool_use_id
    assert Path(persisted[0].persisted.path).name == "t2.txt"


async def test_aggregate_multiple_rounds(tmp_path: Path) -> None:
    """聚合 10 个各 45K（合计 450K）→ 需要多次落盘，最大的若干条落盘。"""
    handler = ToolResultHandler(tmp_path)
    results = [_big_result("a" * 45_000, tool_use_id=f"t{i}") for i in range(10)]
    processed = await handler.handle_batch(results)
    total = sum(len(p.content) for p in processed)
    assert total <= 200_000
    persisted = [p for p in processed if p.persisted is not None]
    # 45K*10=450K，单条 45K<50K 不触发单条落盘；聚合需落盘足够多条到预算内。
    # 每条落盘后 content 是预览（~2K），10 条全落盘也才 20K < 200K，
    # 因此聚合会反复落盘直到全部落盘或总量达标。
    assert len(persisted) >= 5


async def test_context_manager_wires_session_dir(tmp_path: Path) -> None:
    """ContextManager 落盘目录 = {sessions_dir}/{sid}/tool-results/（01 §5.2）。"""
    sessions_dir = tmp_path / "sessions"
    cm = ContextManager(sessions_dir, session_id="s-1")
    processed = await cm.on_tool_result(_big_result("q" * 60_000))
    assert processed.persisted is not None
    path = Path(processed.persisted.path)
    assert path.parent == sessions_dir / "s-1" / "tool-results"


async def test_context_manager_session_switch(tmp_path: Path) -> None:
    """/session 切换 → set_session_id 后落盘目录随 sid 变。"""
    sessions_dir = tmp_path / "sessions"
    cm = ContextManager(sessions_dir, session_id="s-1")
    cm.set_session_id("s-2")
    processed = await cm.on_tool_result(_big_result("q" * 60_000))
    assert processed.persisted is not None
    assert Path(processed.persisted.path).parent == sessions_dir / "s-2" / "tool-results"


async def test_agent_writes_preview_to_history(tmp_path: Path) -> None:
    """Agent 端到端：ReadFile 读 60K 文件 → 历史中只有预览+路径，无原文。"""
    work_dir = tmp_path / "proj"
    work_dir.mkdir()
    big_file = work_dir / "big.txt"
    big_file.write_text("L" * 60_000, encoding="utf-8")

    sessions_dir = tmp_path / "sessions"
    llm = FakeLLM(
        [
            [
                LLMStreamEvent(
                    type="tool_use",
                    tool_use=ToolUseBlock(id="t1", name="ReadFile", input={"path": str(big_file)}),
                ),
                LLMStreamEvent(type="stop", stop_reason="tool_use"),
            ],
            done("完成"),
        ]
    )
    conversation = ConversationManager()
    agent = Agent(
        config=Config(),
        llm=llm,
        conversation=conversation,
        tools=build_default_registry(),
        events=lambda _ev: None,  # type: ignore[arg-type]
        work_dir=work_dir,
        session_id="s-l1",
        context_manager=ContextManager(sessions_dir, session_id="s-l1"),
    )
    await agent.run("读 big.txt")

    # 历史里只有预览（<persisted-output>），无原文
    tool_blocks = [
        block
        for msg in conversation.messages
        for block in msg.content
        if isinstance(block, ToolResultBlock)
    ]
    assert len(tool_blocks) == 1
    content = tool_blocks[0].content
    assert "<persisted-output>" in content
    assert "L" * 60_000 not in content
    # 落盘文件存在且完整（ReadFile 带行号前缀，落盘的是带行号的完整内容）
    persisted_files = list((sessions_dir / "s-l1" / "tool-results").glob("*.txt"))
    assert len(persisted_files) == 1
    persisted = persisted_files[0].read_text(encoding="utf-8")
    assert persisted.startswith("1: ")
    assert "L" * 60_000 in persisted
