"""Windows 终端兼容层（方案 A：对齐 Claude Code 的 Kitty 降级路径）。

问题根因（M1-i 第三轮验收定位）：
- Textual 8.2.8 的 Windows 驱动（windows_driver.py:99）**无条件**发送 ``\\x1b[>1u``
  启用 Kitty 键盘协议，而该协议要求终端支持（Windows Terminal **≥1.25 Preview**，
  2026-03 才加入）。稳定版 1.24 及更早不支持。
- 终端不支持时协议错配 → IME 组合输入被按精确键序列解析 → 中文输入失败；
  SGR 鼠标序列因缺 ESC 前缀漏出 ``[<35;56;28m`` 灌进输入框。
- Claude Code 用能力探测（``CSI ? u`` 握手），终端不支持则**回退传统输入流**，
  故同终端中文正常。

对齐方案：monkeypatch Windows 驱动，过滤掉 Kitty 启用序列，让终端走传统
VT 输入路径（ReadConsoleInputW 直读 Unicode 字符 + XTermParser 传统序列解析）。

注：Textual 自带 ``constants.DISABLE_KITTY_KEY``（``TEXTUAL_DISABLE_KITTY_KEY``
环境变量），但 8.2.8 中仅 linux_driver.py:285 读取，Windows 驱动无此检查——
故需在此 monkeypatch。未来升级 WT ≥1.25 后可用环境变量或移除本 patch 恢复 Kitty。
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.drivers.windows_driver import WindowsDriver

# Kitty 键盘协议启用序列：\x1b[>1u 的 flag=1 仅 DISAMBIGUATE_ESCAPE_CODES，
# 且未含 IME 所需的 REPORT_ASSOCIATED_TEXT；对不支持终端的负面影响大于收益。
_KITTY_ENABLE = "\x1b[>1u"


def filter_kitty_enable(data: str) -> str:
    """从写入数据中剔除 Kitty 启用序列（保留其余字节不变）。"""
    if _KITTY_ENABLE in data:
        data = data.replace(_KITTY_ENABLE, "")
    return data


def patch_windows_input() -> bool:
    """禁用 Textual Windows 驱动的 Kitty 启用序列。

    仅 win32 生效；其他平台直接返回 ``False``（无需 patch）。
    返回是否实际执行了 patch。
    """
    if sys.platform != "win32":
        return False
    from textual.drivers.windows_driver import WindowsDriver

    orig_write = WindowsDriver.write

    def _write_filtered(self: WindowsDriver, data: str) -> None:
        orig_write(self, filter_kitty_enable(data))

    # 幂等：重复调用不再叠加 wrapper
    if WindowsDriver.write is not _write_filtered:
        WindowsDriver.write = _write_filtered  # type: ignore[method-assign]
    return True
