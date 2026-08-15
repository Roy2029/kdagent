"""状态栏 token 显示测试（规格 05 §3.2 + 07 指标最初形态，M2-e）。"""

from __future__ import annotations

from kdagent.ui.statusbar import StatusBar


def test_statusbar_shows_context_over_window() -> None:
    """带 window_size：`tokens: 45,230/200k`（当前窗口占用/窗口上限）。"""
    sb = StatusBar()
    sb.update_status(
        mode="DEFAULT",
        token_count=45_230,
        tool_count=7,
        work_dir="/tmp/proj",
        window_size=200_000,
    )
    text = str(sb.render())
    assert "tokens: 45,230/200k" in text
    assert "工具 7" in text
    assert "/tmp/proj" in text


def test_statusbar_plain_count_compat() -> None:
    """不带 window_size（旧调用方）：兼容纯 count 显示。"""
    sb = StatusBar()
    sb.update_status(mode="PLAN", token_count=1234, tool_count=7, work_dir="/tmp")
    assert "tokens: 1,234" in str(sb.render())


def test_statusbar_session_id_suffix() -> None:
    """会话 id 附加在尾部。"""
    sb = StatusBar()
    sb.update_status(
        mode="DEFAULT",
        token_count=100,
        tool_count=7,
        work_dir="/tmp",
        session_id="s-abc",
        window_size=200_000,
    )
    text = str(sb.render())
    assert "s-abc" in text and "200k" in text
