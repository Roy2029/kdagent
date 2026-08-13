"""会话管理器（规格 04 §3.3-3.6）：创建/恢复/列表/删除/过期清理。

写路径顺序保证（§3.5）：先写文件、再更新内存——崩溃时从文件重建不丢消息。
恢复四步：① 逐行解析（坏行跳过）→ ② 链修复（出口，`02` repair_chain）→
③ token 检查（`01`，M1-d 占位）→ ④ 时间跨度提示。
"""

from __future__ import annotations

import json
import secrets
import shutil
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TypeAlias

from kdagent.engine.conversation import ConversationManager
from kdagent.engine.messages import ContentBlock, Message, TextBlock
from kdagent.sessions.records import SessionRecord, TodoItemRecord
from kdagent.tools.base import ToolResult

STALE_AFTER_SECONDS = 86400  # 恢复时间跨度阈值（>24h 提示）
STALE_REMINDER = "上次活跃于 {time}，期间代码可能已变更，建议重读相关文件"


def make_session_id(now: datetime) -> str:
    """会话 ID：YYYYMMDD-HHMMSS-xxxx（时间戳一眼看出创建时间，后缀防同秒冲突）。"""
    return f"{now.strftime('%Y%m%d-%H%M%S')}-{secrets.randbelow(0x10000):04x}"


@dataclass(frozen=True, slots=True)
class SessionMeta:
    """会话列表项（创建时间从文件名读，最后活跃读最后一行 ts）。"""

    sid: str
    created_ts: int
    last_active_ts: int


# 方法名 `list` 遮蔽 builtin，方法内不能用 `list[...]`，用模块级别名规避
SessionMetaList: TypeAlias = list[SessionMeta]
StrList: TypeAlias = list[str]


class Session:
    """一个会话：聚合 `02` ConversationManager + 文件句柄，写路径先落盘再入内存。"""

    def __init__(
        self,
        sid: str,
        file: Path,
        conversation: ConversationManager,
        todos: list[TodoItemRecord] | None = None,
    ) -> None:
        self.id = sid
        self.file = file
        self._conversation = conversation
        self._todos = todos

    @property
    def conversation(self) -> ConversationManager:
        return self._conversation

    @property
    def todos(self) -> list[TodoItemRecord] | None:
        return self._todos

    def set_todos(self, todos: list[TodoItemRecord]) -> None:
        """会话级 todo 状态（`03` TodoWrite 接线，M1-f 落地；`12` 时点④重灌快照）。"""
        self._todos = list(todos)

    def append_user(self, text: str, extra_blocks: list[ContentBlock] | None = None) -> None:
        self._conversation.add_user_message(text, extra_blocks)
        self._flush_last()

    def append_assistant(self, blocks: list[ContentBlock]) -> None:
        self._conversation.add_assistant_message(blocks)
        self._flush_last()

    def append_tool_results(self, results: list[ToolResult]) -> None:
        self._conversation.add_tool_results(results)
        self._flush_last()

    def _flush_last(self) -> None:
        """§3.5 顺序：先写文件、再更新内存计数。最新一条消息落盘为一行。"""
        msg = self._conversation.messages[-1]
        record = SessionRecord.from_message(msg, int(time.time()))
        if self._todos is not None:
            record = replace(record, todos=list(self._todos))
        with self.file.open("a", encoding="utf-8") as f:
            f.write(record.to_json() + "\n")


class SessionManager:
    def __init__(self, sessions_dir: Path) -> None:
        self._dir = sessions_dir

    @property
    def sessions_dir(self) -> Path:
        return self._dir

    def create(self) -> Session:
        """生成 id + 建 .jsonl（父目录懒创建）。"""
        sid = make_session_id(datetime.now())
        file = self._dir / f"{sid}.jsonl"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.touch()
        return Session(sid, file, ConversationManager())

    def resume(self, sid: str) -> Session:
        """恢复四步：①逐行解析 →（②链修复在出口）→ ③token 检查占位 → ④时间跨度提示。"""
        file = self._dir / f"{sid}.jsonl"
        if not file.exists():
            raise FileNotFoundError(f"会话不存在：{sid}")
        messages: list[Message] = []
        todos: list[TodoItemRecord] | None = None
        last_ts = 0
        with file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = SessionRecord.from_json(line)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue  # 坏行跳过（崩溃截断的半行），不放弃整个会话
                messages.append(record.to_message())
                if record.todos:
                    todos = record.todos
                if record.ts:
                    last_ts = max(last_ts, record.ts)
        conversation = ConversationManager()
        conversation.restore(messages)
        session = Session(sid, file, conversation, todos=todos)
        # 恢复④：>24h 时间跨度提示（system_reminder 注入）
        if last_ts and int(time.time()) - last_ts > STALE_AFTER_SECONDS:
            last_time = datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M")
            session.append_user(
                "", extra_blocks=[TextBlock(STALE_REMINDER.format(time=last_time))]
            )
        return session

    def list(self) -> SessionMetaList:
        """按最后活跃倒序（最近用过的排最前）。"""
        metas: SessionMetaList = []
        for file in self._dir.glob("*.jsonl"):
            sid = file.stem
            created_ts = _parse_created_ts(sid)
            metas.append(SessionMeta(sid=sid, created_ts=created_ts, last_active_ts=_last_ts(file)))
        return sorted(metas, key=lambda m: m.last_active_ts, reverse=True)

    def delete(self, sid: str) -> None:
        """删 .jsonl + 同名目录（如 tool-results）。"""
        file = self._dir / f"{sid}.jsonl"
        if file.exists():
            file.unlink()
        dir_ = self._dir / sid
        if dir_.is_dir():
            shutil.rmtree(dir_)

    def cleanup_expired(self, days: int = 30, enabled: bool = True) -> StrList:
        """启动时清理过期会话（D12：天数可配置、可开关）。返回删除的 sid 列表。"""
        if not enabled:
            return []
        cutoff = time.time() - days * 86400
        removed: StrList = []
        for file in self._dir.glob("*.jsonl"):
            last_active = _last_ts(file)
            if (last_active and last_active < cutoff) or (
                not last_active and file.stat().st_mtime < cutoff
            ):
                sid = file.stem
                self.delete(sid)
                removed.append(sid)
        return removed


def _parse_created_ts(sid: str) -> int:
    try:
        return int(datetime.strptime(sid[:15], "%Y%m%d-%H%M%S").timestamp())
    except ValueError:
        return 0


def _last_ts(file: Path) -> int:
    """最后一行记录的 ts（会话最后活跃时间）。"""
    ts = 0
    try:
        with file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = SessionRecord.from_json(line)
                    ts = record.ts or ts
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
    except OSError:
        pass
    return ts
