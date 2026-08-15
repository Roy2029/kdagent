"""Agent 压缩路径 context.compact span 埋点（07 §3.6 T7 标定数据源，D68）。

T7 验收 268「压缩成本数据可聚合」的数据源：每次压缩（force/auto/emergency）产出
`context.compact` span，attributes 含触发类型（trigger）/压缩前 token（before_tokens）/
压缩后 token（after_tokens）——聚合层据此算压缩比与触发分布。
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import FakeLLM, done

from kdagent.config import Config
from kdagent.context.compactor import Compactor
from kdagent.context.context_manager import ContextManager
from kdagent.engine.agent import DEFAULT_SYSTEM_PROMPT, Agent
from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import PromptTooLongError
from kdagent.obs.telemetry import Telemetry
from kdagent.tools import build_default_registry


def _trace_file(obs_dir: Path, sid: str) -> Path:
    files = list((obs_dir / "traces" / sid).glob("*.jsonl"))
    assert len(files) == 1, f"期望 1 个 trace 文件，实际 {len(files)}"
    return files[0]


def _compact_rows(obs_dir: Path, sid: str) -> list[dict]:
    rows = [
        json.loads(line)
        for line in _trace_file(obs_dir, sid).read_text(encoding="utf-8").splitlines()
    ]
    return [r for r in rows if r["_type"] == "span" and r["name"] == "context.compact"]


def _tiny_compactor(llm: FakeLLM) -> Compactor:
    """极低窗口：第一轮上下文即超阈值 → FORCE_COMPACT。

    摘要源空（kept 覆盖全部消息，recent_keep 阈值远超窗口）→ compact 快速返回，
    不消费 LLM 响应——正常轮响应对应唯一一条即可。
    """
    return Compactor(
        llm,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        window_size=20,  # system(23 token) 已超阈值，第一轮必 FORCE
        summary_output_reserve=0,
        safety_margin=0,
        force_extra_margin=0,
    )


def _make_agent(
    llm: FakeLLM,
    *,
    session_id: str,
    work_dir: Path,
    cm: ContextManager,
    telemetry: Telemetry | None,
) -> Agent:
    return Agent(
        config=Config(),
        llm=llm,
        conversation=ConversationManager(),
        tools=build_default_registry(),
        events=lambda _ev: None,
        work_dir=work_dir,
        session_id=session_id,
        telemetry=telemetry,
        context_manager=cm,
    )


async def test_force_compact_emits_context_compact_span(tmp_path: Path) -> None:
    """FORCE 分支（阶段 A）：span 带 trigger=force + before/after_tokens，status=ok。"""
    work_dir = tmp_path / "proj"
    work_dir.mkdir()
    obs_dir = tmp_path / "obs"
    telemetry = Telemetry(obs_dir)
    llm = FakeLLM([done("完成")])  # 空源压缩不消费响应，唯一响应给正常轮
    cm = ContextManager(
        sessions_dir=tmp_path / "sessions",
        llm=llm,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        compactor=_tiny_compactor(llm),
    )
    agent = _make_agent(
        llm, session_id="s-t7f", work_dir=work_dir, cm=cm, telemetry=telemetry
    )
    await agent.run("触发压缩")

    rows = _compact_rows(obs_dir, "s-t7f")
    assert len(rows) == 1  # 首轮开头 FORCE 一次，之后正常结束
    attrs = rows[0]["attributes"]
    assert attrs["trigger"] == "force"
    assert isinstance(attrs["before_tokens"], int) and attrs["before_tokens"] > 0
    assert isinstance(attrs["after_tokens"], int) and attrs["after_tokens"] > 0
    assert rows[0]["status"] == "ok"


class _PromptTooLongLLM(FakeLLM):
    """首次调用抛 PromptTooLongError → 触发 emergency_compact，之后正常弹预设响应。"""

    async def stream_chat(self, payload):  # type: ignore[override]
        if self.call_count == 0:
            self.call_count += 1
            raise PromptTooLongError("prompt too long")
        async for ev in super().stream_chat(payload):
            yield ev


async def test_emergency_compact_emits_context_compact_span(tmp_path: Path) -> None:
    """emergency 分支（阶段 B）：span 带 trigger=emergency + before/after_tokens。"""
    work_dir = tmp_path / "proj"
    work_dir.mkdir()
    obs_dir = tmp_path / "obs"
    telemetry = Telemetry(obs_dir)
    llm = _PromptTooLongLLM([done("完成")])
    cm = ContextManager(
        sessions_dir=tmp_path / "sessions",
        llm=llm,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        compactor=_tiny_compactor(llm),
    )
    agent = _make_agent(
        llm, session_id="s-t7e", work_dir=work_dir, cm=cm, telemetry=telemetry
    )
    await agent.run("触发紧急压缩")

    rows = _compact_rows(obs_dir, "s-t7e")
    emergency = [r for r in rows if r["attributes"]["trigger"] == "emergency"]
    assert len(emergency) == 1  # 首轮 LLM 撞墙后走一次 emergency（首轮开头另有 FORCE）
    attrs = emergency[0]["attributes"]
    assert attrs["before_tokens"] > 0
    assert attrs["after_tokens"] > 0


async def test_no_telemetry_compact_unchanged(tmp_path: Path) -> None:
    """telemetry=None 时压缩路径行为不变（nullcontext 兜底，不产 span 不报错）。"""
    work_dir = tmp_path / "proj"
    work_dir.mkdir()
    llm = FakeLLM([done("完成")])
    cm = ContextManager(
        sessions_dir=tmp_path / "sessions",
        llm=llm,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        compactor=_tiny_compactor(llm),
    )
    agent = _make_agent(
        llm, session_id="s-plain", work_dir=work_dir, cm=cm, telemetry=None
    )
    await agent.run("触发压缩")
    assert not list((tmp_path / "obs").glob("**/*"))  # 未开启 obs，无任何产出
