"""Agent + Telemetry 端到端测试（07 §5：一次 Agent.run() 产一条 trace，span 树完整落盘）。"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import FakeLLM, done

from kdagent.config import Config
from kdagent.context.compactor import estimate_tokens
from kdagent.engine.agent import Agent
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import LLMStreamEvent, Usage
from kdagent.engine.messages import ToolUseBlock
from kdagent.obs.telemetry import Telemetry
from kdagent.tools import build_default_registry


def _trace_file(obs_dir: Path, sid: str) -> Path:
    files = list((obs_dir / "traces" / sid).glob("*.jsonl"))
    assert len(files) == 1, f"期望 1 个 trace 文件，实际 {len(files)}"
    return files[0]


async def test_agent_run_produces_complete_trace(tmp_path: Path) -> None:
    work_dir = tmp_path / "proj"
    work_dir.mkdir()
    file = work_dir / "a.txt"
    file.write_text("hello", encoding="utf-8")

    obs_dir = tmp_path / ".kdagent" / "obs"
    telemetry = Telemetry(obs_dir)
    llm = FakeLLM(
        [
            [
                LLMStreamEvent(
                    type="usage",
                    usage=Usage(
                        input_tokens=10,
                        output_tokens=5,
                        cache_read_tokens=2,
                        cache_creation_tokens=8,
                    ),
                ),
                LLMStreamEvent(
                    type="tool_use",
                    tool_use=ToolUseBlock(id="t1", name="ReadFile", input={"path": str(file)}),
                ),
                LLMStreamEvent(type="stop", stop_reason="tool_use"),
            ],
            done("完成"),
        ]
    )
    collected: list[object] = []
    agent = Agent(
        config=Config(),
        llm=llm,
        conversation=ConversationManager(),
        tools=build_default_registry(),
        events=collected.append,  # type: ignore[arg-type]
        work_dir=work_dir,
        session_id="s-test",
        model_name="deepseek-chat",
        telemetry=telemetry,
    )
    await agent.run("读一下 a.txt")

    rows = [
        json.loads(line)
        for line in _trace_file(obs_dir, "s-test").read_text(encoding="utf-8").splitlines()
    ]
    header = rows[0]
    spans = [r for r in rows[1:] if r["_type"] == "span"]
    assert header["_type"] == "trace"
    assert header["session_id"] == "s-test"
    assert "读一下 a.txt" in header["user_input_snapshot"]

    names = [s["name"] for s in spans]
    assert "trace.run" in names
    assert "llm.call" in names
    assert "tool.exec" in names
    # 1 次工具轮 + 1 次完成轮 = 2 次 LLM 调用
    assert names.count("llm.call") == 2
    assert names.count("tool.exec") == 1

    root = next(s for s in spans if s["name"] == "trace.run")
    assert root["parent_span_id"] is None
    assert root["attributes"]["stop_reason"] == "completed"
    llm_row = next(s for s in spans if s["name"] == "llm.call")
    assert llm_row["attributes"]["model"] == "deepseek-chat"
    assert llm_row["attributes"]["input_tokens"] == 10
    assert llm_row["attributes"]["cache_read_tokens"] == 2
    tool_row = next(s for s in spans if s["name"] == "tool.exec")
    assert tool_row["attributes"]["tool"] == "ReadFile"
    assert tool_row["attributes"]["is_error"] is False
    assert tool_row["attributes"]["tool_use_id"] == "t1"  # 01 T8：X 分布数据源
    # ReadFile 输出带行号前缀（"1\thello\n" = 8 字符）；output_chars 记完整原始大小，
    # 与未截断的 output 一致（内容 < _TRACE_OUTPUT_CAP）
    assert tool_row["attributes"]["output_chars"] == len(tool_row["attributes"]["output"])
    assert tool_row["attributes"]["output_tokens"] == estimate_tokens(
        tool_row["attributes"]["output"]
    )
    assert tool_row["parent_span_id"] == root["span_id"]  # 父是 trace.run


async def test_agent_tool_error_span_status(tmp_path: Path) -> None:
    """工具失败 → tool.exec span status=error，is_error=True。"""
    work_dir = tmp_path / "proj"
    work_dir.mkdir()
    obs_dir = tmp_path / "obs"
    telemetry = Telemetry(obs_dir)
    llm = FakeLLM(
        [
            [
                LLMStreamEvent(
                    type="tool_use",
                    tool_use=ToolUseBlock(
                        id="t1", name="ReadFile", input={"path": str(work_dir / "missing.txt")}
                    ),
                ),
                LLMStreamEvent(type="stop", stop_reason="tool_use"),
            ],
            done(),
        ]
    )
    agent = Agent(
        config=Config(),
        llm=llm,
        conversation=ConversationManager(),
        tools=build_default_registry(),
        events=lambda _ev: None,  # type: ignore[arg-type]
        work_dir=work_dir,
        session_id="s-err",
        telemetry=telemetry,
    )
    await agent.run("读不存在文件")

    rows = [
        json.loads(line)
        for line in _trace_file(obs_dir, "s-err").read_text(encoding="utf-8").splitlines()
    ]
    tool_row = next(r for r in rows if r.get("name") == "tool.exec")
    assert tool_row["status"] == "error"
    assert tool_row["attributes"]["is_error"] is True
