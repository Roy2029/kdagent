"""compat 模块测试（M1-i2：conhost 传统输入层对齐 Claude Code）。

纯翻译函数任意平台可测；驱动 patch 生效仅 win32 断言。
"""

from __future__ import annotations

import sys

import pytest

from kdagent import compat

# ---- Kitty 启用序列过滤 --------------------------------------------------


def test_filter_kitty_enable() -> None:
    """过滤逻辑：剔除 \\x1b[>1u，其余字节不动。"""
    assert compat.filter_kitty_enable("\x1b[>1u") == ""
    assert compat.filter_kitty_enable("\x1b[?1006h\x1b[>1u") == "\x1b[?1006h"
    assert compat.filter_kitty_enable("hello \x1b[?1049h") == "hello \x1b[?1049h"


# ---- KEY_EVENT → xterm 序列翻译（传统模式） ------------------------------


def test_translate_unicode_char_wins() -> None:
    """UnicodeChar 优先：IME 提交的汉字 / 普通字符 / Ctrl 组合控制字符。"""
    assert compat.translate_key_event(0, 0, "你") == "你"
    assert compat.translate_key_event(0x43, 0, "A") == "A"  # shift+a 大写
    assert compat.translate_key_event(0x43, 0, "\x03") == "\x03"  # Ctrl+C


def test_translate_arrows() -> None:
    """方向键：无修饰符 → \\x1b[D；Shift → \\x1b[1;2D。"""
    assert compat.translate_key_event(compat.VK_LEFT, 0, "") == "\x1b[D"
    assert compat.translate_key_event(compat.VK_UP, 0, "") == "\x1b[A"
    assert compat.translate_key_event(compat.VK_RIGHT, 0, "") == "\x1b[C"
    assert compat.translate_key_event(compat.VK_DOWN, 0, "") == "\x1b[B"
    assert compat.translate_key_event(compat.VK_LEFT, compat._SHIFT_PRESSED, "") == "\x1b[1;2D"
    assert compat.translate_key_event(
        compat.VK_LEFT, compat._LEFT_CTRL_PRESSED, ""
    ) == "\x1b[1;5D"


def test_translate_editing_and_function_keys() -> None:
    """编辑键/功能键 → xterm 序列。"""
    assert compat.translate_key_event(compat.VK_BACK, 0, "") == "\x08"
    assert compat.translate_key_event(compat.VK_RETURN, 0, "") == "\r"
    assert compat.translate_key_event(compat.VK_TAB, 0, "") == "\t"
    assert compat.translate_key_event(compat.VK_ESCAPE, 0, "") == "\x1b"
    assert compat.translate_key_event(compat.VK_DELETE, 0, "") == "\x1b[3~"
    assert compat.translate_key_event(compat.VK_HOME, 0, "") == "\x1b[H"
    assert compat.translate_key_event(compat.VK_PRIOR, 0, "") == "\x1b[5~"
    assert compat.translate_key_event(compat.VK_F1, 0, "") == "\x1bOP"
    assert compat.translate_key_event(compat.VK_F1 + 4, 0, "") == "\x1b[15~"


def test_translate_modifier_keys_dropped() -> None:
    """修饰键本身无输出。"""
    assert compat.translate_key_event(compat.VK_SHIFT, 0, "") is None
    assert compat.translate_key_event(compat.VK_CONTROL, 0, "") is None
    assert compat.translate_key_event(compat.VK_MENU, 0, "") is None


def test_should_drop_key_event_ime_submission() -> None:
    """传统模式下 VK=0 的 IME 汉字事件必须保留（修复中文误杀）。

    conhost 把 IME 确认的汉字以 VK=0 + UnicodeChar 提交；原 VT 模式的
    "dwControlKeyState and vk==0" 过滤会把中文丢进黑名单。
    """
    assert not compat._should_drop_key_event(0, "你")  # IME 确认提交的汉字
    assert not compat._should_drop_key_event(0, "好")
    assert not compat._should_drop_key_event(0, "A")  # 普通字符
    assert compat._should_drop_key_event(0, "")  # 无 VK 且无字符 → 丢弃
    assert compat._should_drop_key_event(0, "\x00")
    assert not compat._should_drop_key_event(compat.VK_RETURN, "")  # 特殊键不丢


# ---- MOUSE_EVENT → SGR 鼠标序列 ------------------------------------------


def test_translate_mouse_press_release() -> None:
    """左键按下 → \\x1b[<0;x;yM；释放 → \\x1b[<3;x;ym（坐标 1 基）。"""
    assert compat.translate_mouse_event(0, 0, 0x1, 0, 0) == "\x1b[<0;1;1M"
    assert compat.translate_mouse_event(4, 9, 0x1, 0, 0) == "\x1b[<0;5;10M"
    assert compat.translate_mouse_event(0, 0, 0x2, 0, 0) == "\x1b[<2;1;1M"  # 右键
    assert compat.translate_mouse_event(0, 0, 0x4, 0, 0) == "\x1b[<1;1;1M"  # 中键
    assert compat.translate_mouse_event(0, 0, 0, 0, 0) == "\x1b[<3;1;1m"  # 释放


def test_translate_mouse_modifier_and_motion() -> None:
    """Shift 修饰 → 按钮码 +4；移动 → +32。"""
    assert compat.translate_mouse_event(0, 0, 0x1, compat._SHIFT_PRESSED, 0) == "\x1b[<4;1;1M"
    assert compat.translate_mouse_event(
        0, 0, 0x1, 0, compat._MOUSE_MOVED
    ) == "\x1b[<32;1;1M"


# ---- 驱动 patch 生效（仅 win32） -----------------------------------------


def test_patch_non_windows_noop() -> None:
    """非 win32：patch 为 no-op，返回 False。"""
    if sys.platform == "win32":
        pytest.skip("win32 上此分支由 test_patch_windows_effective 覆盖")
    assert compat.patch_windows_input() is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 驱动仅 win32")
def test_patch_windows_effective(monkeypatch: pytest.MonkeyPatch) -> None:
    """win32：三处 patch 全部生效（Kitty 过滤 / 传统输入模式 / EventMonitor.run）。"""
    import textual.drivers.win32 as win32_mod
    from textual.drivers.windows_driver import WindowsDriver

    compat._patched = False  # 幂等标志重置，确保本次真正执行
    orig_write = WindowsDriver.write
    orig_enable = win32_mod.enable_application_mode
    orig_run = win32_mod.EventMonitor.run
    try:
        assert compat.patch_windows_input() is True
        assert compat.patch_windows_input() is True  # 幂等
        # 1) write 被过滤 Kitty
        assert WindowsDriver.write is not orig_write
        driver = object.__new__(WindowsDriver)
        driver._writer_thread = _NullWriter()  # type: ignore[attr-defined]
        driver.write("\x1b[>1u\x1b[?1006h")  # type: ignore[arg-type]
        assert driver._writer_thread.data == "\x1b[?1006h"  # type: ignore[attr-defined]
        # 2) enable_application_mode 被替换为传统模式
        assert win32_mod.enable_application_mode is not orig_enable
        # 3) EventMonitor.run 被替换
        assert win32_mod.EventMonitor.run is not orig_run
    finally:
        WindowsDriver.write = orig_write
        win32_mod.enable_application_mode = orig_enable
        win32_mod.EventMonitor.run = orig_run
        compat._patched = False


class _NullWriter:
    """替代 WriterThread：write 时记录数据，供驱动直写验证。"""

    def __init__(self) -> None:
        self.data = ""

    def write(self, data: str) -> None:
        self.data += data
