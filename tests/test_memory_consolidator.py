"""Dreaming 治理测试（08 §3.6）：门控 + 锁 + LLM 整理。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from kdagent.engine.llm.base import LLMStreamEvent, Payload
from kdagent.memory.consolidator import (
    CONSOLIDATE_MIN_INTERVAL,
    MemoryConsolidator,
)
from kdagent.memory.model import MemoryFile
from kdagent.memory.store import MemoryStore


class _FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.calls = 0

    async def stream_chat(self, payload: Payload) -> AsyncIterator[LLMStreamEvent]:
        self.calls += 1
        text = self._responses.pop(0) if self._responses else '{"ops": []}'
        yield LLMStreamEvent(type="text_delta", text=text)


class _Clock:
    """可控时钟：默认对齐真实时间（锁文件 mtime 是真实 epoch，须可比）。"""

    def __init__(self, start: float | None = None) -> None:
        import time

        self.now = time.time() if start is None else start

    def __call__(self) -> float:
        return self.now


def _store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "user-memory", tmp_path / "proj" / ".kdagent" / "memory")


def _mk_sessions(tmp_path: Path, n: int = 5) -> Path:
    d = tmp_path / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"s{i}.jsonl").write_text('{"role": "user", "content": "信号"}', encoding="utf-8")
    return d


def _consolidator(
    tmp_path: Path,
    llm: _FakeLLM,
    *,
    sessions: int = 5,
    clock: _Clock | None = None,
    pid_alive=None,
    min_interval: float = CONSOLIDATE_MIN_INTERVAL,
) -> MemoryConsolidator:
    return MemoryConsolidator(
        _store(tmp_path),
        llm,
        sessions_dir=_mk_sessions(tmp_path, sessions),
        min_interval=min_interval,
        min_sessions=5,
        clock=clock or _Clock(),
        pid_alive=pid_alive or (lambda pid: True),
    )


# ---- 门控 ----

def test_gate_ok_when_fresh(tmp_path: Path) -> None:
    """全新记忆目录 + ≥5 会话 + 无锁 → 门控通过。"""
    llm = _FakeLLM(['{"ops": []}'])
    c = _consolidator(tmp_path, llm)
    s = _store(tmp_path)
    s.ensure()
    assert c.gate_ok()


def test_gate_skips_without_memory_dir(tmp_path: Path) -> None:
    llm = _FakeLLM(['{"ops": []}'])
    c = _consolidator(tmp_path, llm)
    assert not c.gate_ok()  # 目录未建


def test_gate_skips_when_few_sessions(tmp_path: Path) -> None:
    llm = _FakeLLM(['{"ops": []}'])
    c = _consolidator(tmp_path, llm, sessions=3)
    _store(tmp_path).ensure()
    assert not c.gate_ok()  # 会话数 < 5


def test_gate_skips_within_interval(tmp_path: Path) -> None:
    llm = _FakeLLM(['{"ops": []}'])
    clock = _Clock()
    c = _consolidator(tmp_path, llm, clock=clock)
    s = _store(tmp_path)
    s.ensure()
    assert c.gate_ok()
    # 写锁文件 mtime = now → 24h 内不再整理
    lock = tmp_path / "proj" / ".kdagent" / "memory" / ".consolidate-lock"
    lock.write_text("0")
    assert not c.gate_ok()
    clock.now += CONSOLIDATE_MIN_INTERVAL + 1
    assert c.gate_ok()


def test_scan_throttle(tmp_path: Path) -> None:
    llm = _FakeLLM(['{"ops": []}'])
    clock = _Clock()
    c = _consolidator(tmp_path, llm, clock=clock)
    _store(tmp_path).ensure()
    assert c.gate_ok()
    assert not c.gate_ok()  # 10 分钟内已扫描


# ---- 锁 ----

def test_lock_acquire_and_concurrency(tmp_path: Path) -> None:
    llm = _FakeLLM(['{"ops": []}'])
    c = _consolidator(tmp_path, llm)
    _store(tmp_path).ensure()
    assert c._acquire_lock()
    # 存活 PID + 新 mtime → 非 stale → 放弃
    assert not c._acquire_lock()


def test_lock_reclaims_stale_dead_pid(tmp_path: Path) -> None:
    llm = _FakeLLM(['{"ops": []}'])
    # PID 死 → stale → 回收
    c = _consolidator(tmp_path, llm, pid_alive=lambda pid: False)
    _store(tmp_path).ensure()
    assert c._acquire_lock()


def test_lock_reclaims_old_mtime(tmp_path: Path) -> None:
    llm = _FakeLLM(['{"ops": []}'])
    clock = _Clock()
    c = _consolidator(tmp_path, llm, clock=clock)
    _store(tmp_path).ensure()
    assert c._acquire_lock()
    # 时间流逝 > 1h：即使 PID 存活也视为过期 → 回收
    clock.now += 2 * 3600.0
    assert c._acquire_lock()


# ---- 整理 ----

@pytest.mark.asyncio
async def test_consolidate_applies_ops(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.ensure()
    s.create(MemoryFile(name="dup", description="不要 push", type="feedback", content="v1"))
    llm = _FakeLLM([
        '{"ops": ['
        '{"action": "update", "name": "dup", "type": "feedback", "description": "合并后", "content": "统一规则"},'
        '{"action": "delete", "name": "dup2"}'
        "]}"
    ])
    c = _consolidator(tmp_path, llm)
    await c._run_consolidation()
    f = s.read("dup")
    assert f is not None and f.description == "合并后"


@pytest.mark.asyncio
async def test_consolidate_empty_ops_noop(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.ensure()
    llm = _FakeLLM(['{"ops": []}'])
    c = _consolidator(tmp_path, llm)
    await c._run_consolidation()
    assert s.list_all() == []


@pytest.mark.asyncio
async def test_consolidate_llm_error_safe(tmp_path: Path) -> None:
    class _Boom:
        async def stream_chat(self, payload: Payload) -> AsyncIterator[LLMStreamEvent]:
            yield LLMStreamEvent(type="text_delta", text='{"ops": []}')
            raise RuntimeError("上游挂了")

    s = _store(tmp_path)
    s.ensure()
    c = _consolidator(tmp_path, _Boom())  # type: ignore[arg-type]
    await c._run_consolidation()  # 不抛，静默跳过
    assert s.list_all() == []


# ---- 会话信号 ----

def test_session_signals_reads_recent(tmp_path: Path) -> None:
    llm = _FakeLLM(['{"ops": []}'])
    c = _consolidator(tmp_path, llm)
    signals = c._session_signals()
    assert "s4.jsonl" in signals  # 最近一个会话在信号里
    assert "s0.jsonl" not in signals  # 只取最近 3 个（_SIGNAL_FILES）
