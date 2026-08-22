"""会话管理器测试（规格 04 §3.3-3.6：生命周期 / 恢复四步 / 过期清理）。"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pytest

from kdagent.engine.messages import TextBlock, ToolUseBlock
from kdagent.sessions.manager import SessionManager, make_session_id
from kdagent.sessions.records import SessionRecord, todo_items_from_raw
from kdagent.tools.base import ToolResult


def _write_ts(file: Path, ts: int) -> None:
    file.write_text(SessionRecord(role="user", content="x", ts=ts).to_json() + "\n", encoding="utf-8")


def _make_manager(tmp_path: Path) -> SessionManager:
    return SessionManager(tmp_path / ".kdagent" / "sessions")


def test_make_session_id_format() -> None:
    sid = make_session_id(datetime(2026, 8, 11, 14, 30, 0))
    assert sid.startswith("20260811-143000-")
    assert len(sid) == 20  # 15 时间戳 + '-' + 4 后缀


def test_create_creates_file(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s = mgr.create()
    assert s.file.exists()
    assert s.id


def test_append_persists_each_message_one_line(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s = mgr.create()
    s.append_user("你好")
    s.append_assistant([TextBlock("收到")])
    s.append_tool_results([ToolResult(tool_use_id="t1", name="Bash", content="out")])
    lines = s.file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3  # 每条消息一行，流式实时落盘（04 §5）
    data = json.loads(lines[2])
    assert data["role"] == "user"
    assert data["tool_results"][0]["tool_use_id"] == "t1"


def test_resume_roundtrip_conversation(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s = mgr.create()
    s.append_user("问题")
    s.append_assistant([TextBlock("回答"), ToolUseBlock(id="t1", name="ReadFile", input={"path": "a.py"})])
    s.append_tool_results([ToolResult(tool_use_id="t1", name="ReadFile", content="内容")])
    resumed = mgr.resume(s.id)
    assert [m.role for m in resumed.conversation.messages] == ["user", "assistant", "user"]
    assert resumed.conversation.messages[1].content[1].id == "t1"
    assert resumed.conversation.messages[2].content[0].tool_use_id == "t1"


def test_resume_skips_corrupt_last_line(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s = mgr.create()
    s.append_user("完好消息")
    with s.file.open("a", encoding="utf-8") as f:
        f.write('{"role":"assistant","content":"截断的半行\n')
    resumed = mgr.resume(s.id)
    assert len(resumed.conversation.messages) == 1  # 坏行跳过，前序完好
    assert resumed.conversation.messages[0].content[0].text == "完好消息"


def test_resume_stale_injects_reminder(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s = mgr.create()
    s.append_user("旧消息")
    # 篡改 ts 为 3 天前
    data = json.loads(s.file.read_text(encoding="utf-8").splitlines()[0])
    data["ts"] = int(time.time()) - 3 * 86400
    s.file.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
    resumed = mgr.resume(s.id)
    texts = [
        b.text for m in resumed.conversation.messages for b in m.content if isinstance(b, TextBlock)
    ]
    assert any("上次活跃" in t for t in texts)


def test_resume_unknown_session_raises(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    with pytest.raises(FileNotFoundError):
        mgr.resume("no-such-session")


def test_list_sorted_by_last_active_desc(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s_old = mgr.create()
    s_new = mgr.create()
    # 用确定性 ts 覆写（避免同秒 ts 相等的竞态）
    _write_ts(s_old.file, ts=1000)
    _write_ts(s_new.file, ts=2000)
    metas = mgr.list()
    assert [m.sid for m in metas] == [s_new.id, s_old.id]  # 后活跃的先排


def test_delete_removes_file_and_dir(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s = mgr.create()
    (tmp_path / ".kdagent" / "sessions" / s.id).mkdir()  # 同名目录（tool-results）
    mgr.delete(s.id)
    assert not s.file.exists()
    assert not (tmp_path / ".kdagent" / "sessions" / s.id).exists()


def test_cleanup_expired(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s = mgr.create()
    s.append_user("旧")
    data = json.loads(s.file.read_text(encoding="utf-8").splitlines()[0])
    data["ts"] = int(time.time()) - 31 * 86400
    s.file.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
    removed = mgr.cleanup_expired(days=30)
    assert s.id in removed
    assert not s.file.exists()


def test_cleanup_disabled_keeps_all(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    s = mgr.create()
    assert mgr.cleanup_expired(days=30, enabled=False) == []
    assert s.file.exists()


def test_todos_persist_with_latest_record_and_resume(tmp_path: Path) -> None:
    """M1-f 链路：TodoWrite 归一化 → Session.set_todos → 随最新记录落盘 → resume 恢复。"""
    mgr = _make_manager(tmp_path)
    s = mgr.create()
    raw = [
        {
            "content": "目标",
            "tasks": [
                {"content": "任务", "status": "completed", "steps": [{"description": "步骤"}]}
            ],
        }
    ]
    s.set_todos(todo_items_from_raw(raw))
    s.append_user("下一轮输入")  # 新消息落盘时携带 todos（04 §3.2）
    resumed = mgr.resume(s.id)
    assert resumed.todos is not None
    assert resumed.todos[0].content == "任务"
    assert resumed.todos[0].status == "completed"
    assert resumed.todos[0].group == "目标"
    assert resumed.todos[0].steps[0].description == "步骤"


def test_create_with_existing_conversation(tmp_path: Path) -> None:
    """M1-f：App 启动时把已在运行的 Agent 会话包装为 Session。"""
    mgr = _make_manager(tmp_path)
    from kdagent.engine.conversation import ConversationManager

    conv = ConversationManager()
    conv.add_user_message("已有输入")
    s = mgr.create(conversation=conv)
    assert s.conversation is conv
    assert s.conversation.messages[0].content[0].text == "已有输入"


def test_set_title_persists_to_meta_and_list_reads_back(tmp_path: Path) -> None:
    """U3：set_title 写 `.meta.json` 伴生文件，`title()`/`list()` 能读回。"""
    mgr = _make_manager(tmp_path)
    s = mgr.create()
    assert s.title == ""  # 初始无标题
    s.set_title("重构权限模块")
    assert s.title == "重构权限模块"
    assert mgr.title(s.id) == "重构权限模块"
    metas = mgr.list()
    assert metas[0].sid == s.id
    assert metas[0].title == "重构权限模块"
    # 伴生文件确实落盘（不污染 JSONL 逐行格式）
    meta_file = s.file.with_suffix(".meta.json")
    assert meta_file.exists()
    data = json.loads(meta_file.read_text(encoding="utf-8"))
    assert data["title"] == "重构权限模块"
    assert data["updated"] > 0


def test_title_missing_or_corrupt_meta_returns_empty(tmp_path: Path) -> None:
    """U3：meta 缺失 / 损坏（非 JSON）时 title 返回空串，不崩。"""
    mgr = _make_manager(tmp_path)
    s = mgr.create()
    s.file.with_suffix(".meta.json").write_text("{not-json", encoding="utf-8")
    assert mgr.title(s.id) == ""
    assert mgr.list()[0].title == ""
