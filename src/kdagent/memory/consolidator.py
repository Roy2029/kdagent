"""Dreaming 治理（08 §3.6）：单阶段 LLM 整理 + 门控 + 锁文件。

**定位**：只管写不管整理，记忆目录会变成垃圾场（三条"不要 push"、两条矛盾的
项目截止日期、过期信息占满索引配额）。治理 = 低频批量整理（vs 提取的高频小写）。

**触发门控**（懒检查，挂在 Agent.run() 末，命令行工具无常驻 cron，只能在活跃时顺带检查）：
```
if 记忆目录不存在: return
if 距上次整理 < 24h: return          # 时间门（锁文件 mtime）
if 10 分钟内已扫描: return            # 扫描节流
if 累积会话数 < 5: return             # 会话门
if 获取锁失败: return                # 防并发
后台 asyncio task 执行整理（不阻塞主流程）
```

**锁文件**（`memory/.consolidate-lock`，一个文件两个用途）：内容存 PID，mtime 在
整理完成时刷新 ≈ 上次整理完成时间。获取：文件存在 + mtime < 1h + PID 存活 → 有人在
整理，放弃；否则回收写入 PID + 回读确认。崩溃残留 PID 由"进程已死"回收；>1h 视为
过期。整理失败用 utimes 回退 mtime（下次门控能过）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Callable
from pathlib import Path

from kdagent.engine.llm.base import LLMClient
from kdagent.memory.model import normalize_ops
from kdagent.memory.ops import as_user_message, collect_ops_json
from kdagent.memory.store import MemoryStore

CONSOLIDATE_MIN_INTERVAL = 24 * 60 * 60.0  # 时间门：距上次整理 ≥ 24h
CONSOLIDATE_SCAN_THROTTLE = 10 * 60.0  # 扫描节流：10 分钟内不重复扫描
CONSOLIDATE_MIN_SESSIONS = 5  # 会话门：累积会话数 ≥ 5
_LOCK_FILENAME = ".consolidate-lock"
_LOCK_STALE_SECONDS = 60 * 60.0  # 锁内 PID 存活 + mtime < 1h → 判定"正在整理"
_SIGNAL_FILES = 3  # 收集信号：最近 N 个会话
_SIGNAL_LINES = 40  # 每个会话尾部取 N 行

# 整理指令（08 §3.6 四阶段：定位→收集信号→整理→修剪索引，LLM 驱动）。
_CONSOLIDATE_INSTRUCTION = """你是 KDAgent 的记忆治理代理。下面是记忆目录和最近会话信号。

整理记忆（合并重复、删被证伪、修矛盾、相对日期转绝对日期），输出 JSON 操作集（不要其他文字）：
{"ops": [{"action": "create|update|delete", "name": "文件名字节段",
"type": "user|feedback|project|reference", "description": "一行概述",
"content": "记忆正文（markdown）",
"index_line": "- [标题](文件名字节段.md) — 一行概述"}]}

规则：
- 重复记忆合并：多条相同含义 → 保留一条最全的（用 update 补全，其余 delete）
- 被证伪/过时 → delete；相对日期（"昨天"）→ 转绝对日期
- 会话信号里新且重要的信息 → create
- 索引行保持在单行概述；没有要改的 → {"ops": []}
"""


def _pid_alive(pid: int) -> bool:
    """Windows/Linux 通用：os.kill(pid, 0) 探测进程存活。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class MemoryConsolidator:
    """单阶段 Dreaming 治理：门控 + 锁 + LLM 四阶段整理。"""

    def __init__(
        self,
        store: MemoryStore,
        llm: LLMClient,
        *,
        sessions_dir: Path,
        min_interval: float = CONSOLIDATE_MIN_INTERVAL,
        min_sessions: int = CONSOLIDATE_MIN_SESSIONS,
        clock: Callable[[], float] | None = None,
        pid_alive: Callable[[int], bool] = _pid_alive,
    ) -> None:
        self._store = store
        self._llm = llm
        self._sessions_dir = Path(sessions_dir)
        self._min_interval = min_interval
        self._min_sessions = min_sessions
        self._clock = clock or time.time
        self._pid_alive = pid_alive
        self._last_scan: float = 0.0  # 扫描节流时间戳

    # ---- 门控（廉价，同步） ----

    def gate_ok(self) -> bool:
        """五道门：目录存在 / 时间门 / 扫描节流 / 会话门 / 锁。返回 True 才整理。"""
        if not self._store.user_root.is_dir() or not self._store.project_root.is_dir():
            return False
        now = self._clock()
        if now - self._last_scan < CONSOLIDATE_SCAN_THROTTLE:
            return False
        self._last_scan = now
        lock = self._lock_path()
        if lock.is_file() and now - lock.stat().st_mtime < self._min_interval:
            return False  # 时间门：距上次整理 < 24h
        return self._session_count() >= self._min_sessions  # 会话门

    def maybe_consolidate(self) -> None:
        """Agent.run() 结束调用：门控通过则后台调度整理（不阻塞主流程）。"""
        if not self.gate_ok():
            return
        asyncio.create_task(self._consolidate())

    # ---- 锁 ----

    def _lock_path(self) -> Path:
        return self._store.project_root / _LOCK_FILENAME

    def _acquire_lock(self) -> bool:
        """防并发；占用返回 False。过期残留（PID 死或 >1h）被回收。"""
        lock = self._lock_path()
        try:
            if lock.is_file():
                stale = self._lock_is_stale(lock)
                if not stale:
                    return False  # 正在整理
                lock.unlink(missing_ok=True)  # 过期残留 → 回收
            lock.write_text(str(os.getpid()), encoding="utf-8")
            return lock.read_text(encoding="utf-8").strip() == str(os.getpid())
        except OSError:
            return False

    def _lock_is_stale(self, lock: Path) -> bool:
        """mtime < 1h + PID 存活 → 非 stale（有人在整理）；否则视为过期。"""
        try:
            pid = int(lock.read_text(encoding="utf-8").strip() or "0")
        except (ValueError, OSError):
            pid = 0
        try:
            fresh = self._clock() - lock.stat().st_mtime < _LOCK_STALE_SECONDS
        except OSError:
            fresh = False
        return not fresh or not self._pid_alive(pid)

    def _release_lock(self) -> None:
        """保留锁文件作为「上次整理时间」标记；整理失败 utimes 回退 mtime。"""
        with contextlib.suppress(OSError):
            self._lock_path().touch()  # mtime = 完成时刻（时间门的基准）

    # ---- 整理 ----

    async def _consolidate(self) -> None:
        if not self._acquire_lock():
            return
        try:
            await self._run_consolidation()
            self._release_lock()
        except asyncio.CancelledError:
            raise
        except Exception:
            # 治理是后台辅助：失败只跳过本轮，回退 mtime 让下次门控能过。
            self._rollback_lock_mtime()

    def _rollback_lock_mtime(self) -> None:
        lock = self._lock_path()
        try:
            past = self._clock() - self._min_interval
            os.utime(lock, (past, past))
        except OSError:
            pass

    async def _run_consolidation(self) -> None:
        self._store.ensure()
        listing = "\n".join(
            f"- {f.name}（{f.type}）：{f.description}" for f in self._store.list_all()
        )
        listing = listing or "（记忆目录为空）"
        instruction = (
            _CONSOLIDATE_INSTRUCTION
            + f"\n\n记忆目录清单：\n{listing}"
            + f"\n\n最近会话信号：\n{self._session_signals() or '（无）'}"
        )
        ops_json = await collect_ops_json(
            self._llm,
            system="你是 KDAgent 的记忆治理代理。只输出一个 JSON 对象。",
            messages=[as_user_message(instruction)],
        )
        self._store.apply_ops(normalize_ops(ops_json))

    # ---- 会话信号 ----

    def _session_count(self) -> int:
        try:
            return len(list(self._sessions_dir.glob("*.jsonl")))
        except OSError:
            return 0

    def _session_signals(self) -> str:
        """最近 N 个会话的尾部信号（针对性搜，不全量读）。"""
        try:
            files = sorted(self._sessions_dir.glob("*.jsonl"))[-_SIGNAL_FILES:]
        except OSError:
            return ""
        blocks: list[str] = []
        for f in files:
            try:
                lines = f.read_text(encoding="utf-8").splitlines()[-_SIGNAL_LINES:]
            except OSError:
                continue
            if lines:
                blocks.append(f"--- {f.name} ---\n" + "\n".join(lines))
        return "\n\n".join(blocks)
