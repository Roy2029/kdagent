"""记忆提取器测试（08 §3.4）：双门槛节流 + JSON 操作集 + 失败兜底。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from kdagent.engine.conversation import ConversationManager
from kdagent.engine.llm.base import LLMStreamEvent, Payload
from kdagent.memory.extractor import MemoryExtractor
from kdagent.memory.store import MemoryStore


class _FakeLLM:
    """可编排：按次弹出响应；可注入抛错。"""

    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = list(responses or [])
        self.calls = 0

    async def stream_chat(self, payload: Payload) -> AsyncIterator[LLMStreamEvent]:
        self.calls += 1
        if not self._responses:
            yield LLMStreamEvent(type="text_delta", text='{"ops": []}')
            return
        text = self._responses.pop(0)
        yield LLMStreamEvent(type="text_delta", text=text)


class _Clock:
    """可控时钟。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "user-memory", tmp_path / "proj" / ".kdagent" / "memory")


def _extractor(
    tmp_path: Path,
    llm: _FakeLLM,
    *,
    clock: _Clock | None = None,
    min_interval: float = 600.0,
    min_delta: int = 20_000,
    estimate: int = 10_000,
) -> MemoryExtractor:
    clock = clock or _Clock()
    return MemoryExtractor(
        _store(tmp_path),
        llm,
        estimate=lambda c: estimate,  # 固定 token 估算
        min_interval=min_interval,
        min_delta=min_delta,
        clock=clock,
    )


# ---- 触发门槛 ----

@pytest.mark.asyncio
async def test_first_run_small_delta_skips(tmp_path: Path) -> None:
    """首跑增量门（D5 v052）：首轮 mark 视为 0，会话增量 <20K（如「你好」）不提取。"""
    llm = _FakeLLM(['{"ops": []}'])
    ex = _extractor(tmp_path, llm, clock=_Clock(), estimate=10_000)
    conv = ConversationManager()
    await ex.maybe_extract(conv)
    assert llm.calls == 0  # 零 LLM 调用，会话刚建不值得惊动提取


@pytest.mark.asyncio
async def test_first_run_large_delta_extracts(tmp_path: Path) -> None:
    """首跑增量门：会话已有 ≥20K 增量（说明是实质工作会话）→ 首轮即提取。"""
    llm = _FakeLLM(['{"ops": []}'])
    ex = _extractor(tmp_path, llm, clock=_Clock(), estimate=25_000)
    conv = ConversationManager()
    await ex.maybe_extract(conv)
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_time_throttle_skips(tmp_path: Path) -> None:
    llm = _FakeLLM(['{"ops": []}'])
    clock = _Clock()
    ex = _extractor(tmp_path, llm, clock=clock, min_interval=600.0, estimate=25_000)
    conv = ConversationManager()
    await ex.maybe_extract(conv)  # 首轮提取（增量够）
    assert llm.calls == 1
    clock.now += 60.0  # 未到 10 分钟
    await ex.maybe_extract(conv)
    assert llm.calls == 1  # 被时间门槛拦截，零 LLM 调用


@pytest.mark.asyncio
async def test_delta_throttle_skips(tmp_path: Path) -> None:
    llm = _FakeLLM(['{"ops": []}'])
    clock = _Clock()
    ex = _extractor(tmp_path, llm, clock=clock, min_interval=0.0, min_delta=20_000, estimate=25_000)
    conv = ConversationManager()

    # 首轮提取（记录 token mark = 25_000）
    await ex.maybe_extract(conv)
    assert llm.calls == 1

    # 时间门过了（interval=0），但 delta < 20K → 量级门槛拦截
    clock.now += 600.0
    await ex.maybe_extract(conv)
    assert llm.calls == 1


# ---- 提取执行 ----

@pytest.mark.asyncio
async def test_extract_applies_ops(tmp_path: Path) -> None:
    llm = _FakeLLM([
        '{"ops": ['
        '{"action": "create", "name": "lang-python", "type": "project", "description": "项目语言", "content": "Python 3.11", "index_line": "- [语言](lang-python.md) — 项目语言"},'
        '{"action": "create", "name": "use-tabs", "type": "user", "description": "用户偏好", "content": "喜欢 tab"}'
        "]}"
    ])
    ex = _extractor(tmp_path, llm, estimate=25_000)  # 首轮增量够 → 触发提取
    await ex.maybe_extract(ConversationManager())
    s = _store(tmp_path)
    assert s.read("lang-python") is not None
    assert s.read("use-tabs") is not None
    # 索引注入能看到新记忆
    md = s.index_markdown()
    assert "项目语言" in md and "用户偏好" in md


@pytest.mark.asyncio
async def test_empty_ops_creates_nothing(tmp_path: Path) -> None:
    llm = _FakeLLM(['{"ops": []}'])
    ex = _extractor(tmp_path, llm, estimate=25_000)  # 首轮增量够 → 触发提取
    await ex.maybe_extract(ConversationManager())
    assert _store(tmp_path).list_all() == []


@pytest.mark.asyncio
async def test_parse_failure_is_safe(tmp_path: Path) -> None:
    llm = _FakeLLM(["这不是 JSON，模型胡言乱语了"])  # 首轮失败 → 追加小调用
    ex = _extractor(tmp_path, llm, min_interval=0.0, min_delta=0)
    await ex.maybe_extract(ConversationManager())
    # 不抛异常、不建任何记忆
    assert _store(tmp_path).list_all() == []
    assert llm.calls == 2  # 重试了一次（MAX_APPEND_CALLS）


@pytest.mark.asyncio
async def test_llm_error_is_safe(tmp_path: Path) -> None:
    class _Boom:
        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, payload: Payload) -> AsyncIterator[LLMStreamEvent]:
            self.calls += 1
            yield LLMStreamEvent(type="text_delta", text='{"ops": []}')
            raise RuntimeError("上游挂了")

    boom = _Boom()
    ex = _extractor(tmp_path, boom, min_interval=0.0, min_delta=0)  # type: ignore[arg-type]
    await ex.maybe_extract(ConversationManager())  # 不抛，静默跳过
    assert _store(tmp_path).list_all() == []
