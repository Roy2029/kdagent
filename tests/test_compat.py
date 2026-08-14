"""compat 模块测试（M1-i2：禁用 Windows 驱动 Kitty 启用序列，对齐 Claude Code）。

跨平台：纯过滤逻辑任意平台可测；patch 生效仅 win32 断言。
"""

from __future__ import annotations

import sys

import pytest

from kdagent import compat


def test_filter_kitty_enable() -> None:
    """过滤逻辑：剔除 \\x1b[>1u，其余字节不动。"""
    assert compat.filter_kitty_enable("\x1b[>1u") == ""
    # 与其它序列合并时只删 Kitty 段
    assert compat.filter_kitty_enable("\x1b[?1006h\x1b[>1u") == "\x1b[?1006h"
    # 无关内容原样
    assert compat.filter_kitty_enable("hello \x1b[?1049h") == "hello \x1b[?1049h"


def test_patch_non_windows_noop() -> None:
    """非 win32：patch 为 no-op，返回 False。"""
    if sys.platform == "win32":
        pytest.skip("win32 上此分支由 test_patch_windows_effective 覆盖")
    assert compat.patch_windows_input() is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 驱动仅 win32")
def test_patch_windows_effective() -> None:
    """win32：patch 生效 → 类 write 被替换，写入过滤 Kitty 序列；幂等；可恢复。"""
    from textual.drivers.windows_driver import WindowsDriver

    orig = WindowsDriver.write
    try:
        assert compat.patch_windows_input() is True
        # 幂等：第二次调用不叠加
        assert compat.patch_windows_input() is True
        assert WindowsDriver.write is not orig

        # 用最小实例验证写入被过滤：start_application_mode 前的直写路径
        driver = object.__new__(WindowsDriver)
        driver._writer_thread = _NullWriter()  # type: ignore[attr-defined]
        driver.write("\x1b[>1u")  # type: ignore[arg-type]
        driver.write("\x1b[?1006h")  # type: ignore[arg-type]
        assert driver._writer_thread.data == "\x1b[?1006h"  # type: ignore[attr-defined]
    finally:
        WindowsDriver.write = orig


class _NullWriter:
    """替代 WriterThread：write 时记录数据，供驱动直写验证。"""

    def __init__(self) -> None:
        self.data = ""

    def write(self, data: str) -> None:
        self.data += data
