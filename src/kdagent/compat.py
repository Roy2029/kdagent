"""Windows 终端兼容层（M1-i2 第三轮：对齐 Claude Code 传统输入路径）。

**环境事实**：用户终端 = conhost（cmd 窗口），未安装 Windows Terminal。
Textual 8.2.8 的 Windows 驱动（windows_driver.py / drivers/win32.py）有三处
与 conhost 不兼容：

1. 无条件发 ``\\x1b[>1u`` 启用 Kitty 键盘协议（windows_driver.py:99）。
   conhost **永远不支持** Kitty（那是 WT 1.25 Preview 2026-03 才加入的功能），
   该序列被 conhost 忽略——本补丁直接过滤，消除无意义输出。
2. ``enable_application_mode`` 把 stdin 强制设为 ``ENABLE_VIRTUAL_TERMINAL_INPUT``
   （win32.py:157）。**conhost 在 VT 输入模式下对中文 IME 的组合提交处理有缺陷**
   → 中文输入失败。这是第三轮实测中文失败的环境根因。
3. EventMonitor 只读 ``uChar.UnicodeChar``——依赖 VT 输入把特殊键编码为
   逐字符 KEY_EVENT；传统模式下方向键/功能键的 UnicodeChar 为 0，需 VK 码翻译。

**对齐方案**（用户选定「坚持 cmd，改 Textual 输入层」）：
- monkeypatch ``enable_application_mode`` → **传统事件输入模式**：即时读键
  （无 LINE/ECHO/PROCESSED）+ 窗口/鼠标事件（对齐 Node/ink 传统 ReadConsoleInput
  直读，Claude Code 同路径 → IME 提交的 Unicode 字符直读，中文恢复）。
- monkeypatch ``EventMonitor.run``：KEY_EVENT 用 VK 码→xterm 序列翻译
  （UnicodeChar 优先）；MOUSE_EVENT 翻译为 SGR 鼠标序列。

注：Textual 自带 ``constants.DISABLE_KITTY_KEY``（TEXTUAL_DISABLE_KITTY_KEY
环境变量），但 8.2.8 仅 linux_driver 读取，Windows 驱动无检查——故需 monkeypatch。
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from textual.drivers.windows_driver import WindowsDriver

# ---------------------------------------------------------------------------
# Kitty 键盘协议启用序列过滤
# ---------------------------------------------------------------------------

# \x1b[>1u 的 flag=1 仅 DISAMBIGUATE_ESCAPE_CODES，未含 IME 所需的
# REPORT_ASSOCIATED_TEXT；conhost 不支持，过滤无害。
_KITTY_ENABLE = "\x1b[>1u"


def filter_kitty_enable(data: str) -> str:
    """从写入数据中剔除 Kitty 启用序列（保留其余字节不变）。"""
    if _KITTY_ENABLE in data:
        data = data.replace(_KITTY_ENABLE, "")
    return data


# ---------------------------------------------------------------------------
# 传统输入模式（KEY_EVENT / MOUSE_EVENT → xterm 序列翻译）
# ---------------------------------------------------------------------------

# dwControlKeyState 位（windows.h）
_SHIFT_PRESSED = 0x0001
_RIGHT_ALT_PRESSED = 0x0002
_LEFT_ALT_PRESSED = 0x0004
_RIGHT_CTRL_PRESSED = 0x0008
_LEFT_CTRL_PRESSED = 0x0010

# 虚拟键码（windows.h）
VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_ESCAPE = 0x1B
VK_PRIOR = 0x21  # PageUp
VK_NEXT = 0x22  # PageDown
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_INSERT = 0x2D
VK_DELETE = 0x2E
VK_F1 = 0x70
VK_F12 = 0x7B

_ARROW_BASE: dict[int, str] = {
    VK_LEFT: "D",
    VK_UP: "A",
    VK_RIGHT: "C",
    VK_DOWN: "B",
}
_EDIT_KEYS: dict[int, str] = {
    VK_INSERT: "\x1b[2~",
    VK_DELETE: "\x1b[3~",
    VK_HOME: "\x1b[H",
    VK_END: "\x1b[F",
    VK_PRIOR: "\x1b[5~",
    VK_NEXT: "\x1b[6~",
}
_FUNC_KEYS: dict[int, str] = {
    code: seq
    for code, seq in (
        (VK_F1, "\x1bOP"),
        (VK_F1 + 1, "\x1bOQ"),
        (VK_F1 + 2, "\x1bOR"),
        (VK_F1 + 3, "\x1bOS"),
        (VK_F1 + 4, "\x1b[15~"),
        (VK_F1 + 5, "\x1b[17~"),
        (VK_F1 + 6, "\x1b[18~"),
        (VK_F1 + 7, "\x1b[19~"),
        (VK_F1 + 8, "\x1b[20~"),
        (VK_F1 + 9, "\x1b[21~"),
        (VK_F1 + 10, "\x1b[23~"),
        (VK_F1 + 11, "\x1b[24~"),
    )
}

# MOUSE_EVENT_RECORD.dwEventFlags
_MOUSE_MOVED = 0x0001
_MOUSE_WHEELED = 0x0004

# MOUSE_EVENT_RECORD.dwButtonState 低位按钮 → SGR 按钮码（0=left 1=middle 2=right）
_BUTTON_SGR = {0x1: 0, 0x4: 1, 0x2: 2}


def _modifier_code(control_state: int) -> int:
    """xterm 修饰符码：1=无 2=Shift 3=Alt 4=Shift+Alt 5=Ctrl 6=Shift+Ctrl …"""
    mod = 1
    if control_state & _SHIFT_PRESSED:
        mod += 1
    if control_state & (_LEFT_ALT_PRESSED | _RIGHT_ALT_PRESSED):
        mod += 2
    if control_state & (_LEFT_CTRL_PRESSED | _RIGHT_CTRL_PRESSED):
        mod += 4
    return mod


def translate_key_event(vkey: int, control_state: int, unicode_char: str) -> str | None:
    """把 KEY_EVENT 翻译为喂给 XTermParser 的序列。

    UnicodeChar 优先（普通字符/IME 提交的 Unicode 字符/Ctrl 组合的控制字符）；
    特殊键（UnicodeChar 为空）按 VK 码 → xterm 序列；修饰键本身无输出。
    返回 ``None`` 表示无输出（丢弃）。
    """
    if unicode_char not in ("", "\x00"):
        return unicode_char
    mod = _modifier_code(control_state)
    if vkey == VK_ESCAPE:
        return "\x1b"
    if vkey == VK_TAB:
        return "\t"
    if vkey == VK_RETURN:
        return "\r"
    if vkey == VK_BACK:
        return "\x08"
    if vkey in (VK_SHIFT, VK_CONTROL, VK_MENU):
        return None  # 修饰键按下/释放，无输出
    base = _ARROW_BASE.get(vkey)
    if base:
        return f"\x1b[{base}" if mod == 1 else f"\x1b[1;{mod}{base}"
    return _EDIT_KEYS.get(vkey) or _FUNC_KEYS.get(vkey)


def translate_mouse_event(
    pos_x: int,
    pos_y: int,
    button_state: int,
    control_state: int,
    event_flags: int,
) -> str | None:
    """MOUSE_EVENT → SGR 鼠标序列 ``\\x1b[<b;x;yM``（按下/移动）或 ``m``（释放）。"""
    x, y = pos_x + 1, pos_y + 1
    mod = 0
    if control_state & _SHIFT_PRESSED:
        mod += 4
    if control_state & (_LEFT_ALT_PRESSED | _RIGHT_ALT_PRESSED):
        mod += 8
    if control_state & (_LEFT_CTRL_PRESSED | _RIGHT_CTRL_PRESSED):
        mod += 16
    if event_flags & _MOUSE_WHEELED:
        b = 65 if button_state & 0x8000_0000 else 64  # 下/上滚
        return f"\x1b[<{b + mod};{x};{y}M"
    if event_flags & _MOUSE_MOVED:
        pressed = _BUTTON_SGR.get(button_state & 0x7)
        b = 32 + pressed if pressed is not None else 35  # 拖动/无按钮移动
        return f"\x1b[<{b + mod};{x};{y}M"
    pressed = _BUTTON_SGR.get(button_state & 0x7)
    if pressed is not None:
        return f"\x1b[<{pressed + mod};{x};{y}M"  # 按下
    return f"\x1b[<{3 + mod};{x};{y}m"  # 释放


# ---------------------------------------------------------------------------
# 驱动 monkeypatch（仅 win32 生效）
# ---------------------------------------------------------------------------

# 幂等标志：避免重复 patch 叠加
_patched = False


def _compat_enable_application_mode() -> Any:
    """替代 win32.enable_application_mode：stdin 用传统事件输入模式。

    - stdout 保持 VT 处理（Textual 渲染依赖）。
    - stdin 不设 ENABLE_VIRTUAL_TERMINAL_INPUT（conhost 的 VT 输入对中文 IME
      有缺陷）→ 改传统模式：即时读键（无 LINE/ECHO/PROCESSED）+ 窗口/鼠标事件。
      Ctrl+C 因无 PROCESSED_INPUT 以 KEY_EVENT('\\x03') 进入，Textual 正常解析。
    """
    from textual.drivers import win32

    assert sys.__stdin__ is not None and sys.__stdout__ is not None
    terminal_in = sys.__stdin__
    terminal_out = sys.__stdout__
    current_in = win32.get_console_mode(terminal_in)
    current_out = win32.get_console_mode(terminal_out)

    def restore() -> None:
        win32.set_console_mode(terminal_in, current_in)
        win32.set_console_mode(terminal_out, current_out)

    win32.set_console_mode(
        terminal_out,
        current_out | win32.ENABLE_VIRTUAL_TERMINAL_PROCESSING,
    )
    win32.set_console_mode(
        terminal_in,
        win32.ENABLE_WINDOW_INPUT | win32.ENABLE_MOUSE_INPUT | win32.ENABLE_EXTENDED_FLAGS,
    )
    return restore


def _compat_event_monitor_run(self: Any) -> None:
    """替代 EventMonitor.run：传统模式 KEY_EVENT（VK 翻译）+ MOUSE_EVENT（SGR）。

    结构与 Textual 8.2.8 的 EventMonitor.run 保持一致，仅把 KEY_EVENT 的
    UnicodeChar 直读替换为 translate_key_event，并新增 MOUSE_EVENT 翻译。
    """
    from ctypes import byref, wintypes

    from textual import constants
    from textual._xterm_parser import XTermParser
    from textual.drivers import win32

    exit_requested = self.exit_event.is_set
    parser = XTermParser(debug=constants.DEBUG)

    try:
        read_count = wintypes.DWORD(0)
        hIn = win32.GetStdHandle(win32.STD_INPUT_HANDLE)

        MAX_EVENTS = 1024
        KEY_EVENT = 0x0001
        MOUSE_EVENT = 0x0002
        WINDOW_BUFFER_SIZE_EVENT = 0x0004

        arrtype = win32.INPUT_RECORD * MAX_EVENTS
        input_records = arrtype()
        ReadConsoleInputW = win32.KERNEL32.ReadConsoleInputW
        keys: list[str] = []
        append_key = keys.append

        while not exit_requested():
            for event in parser.tick():
                self.process_event(event)

            # 等待新事件
            if win32.wait_for_handles([hIn], 100) is None:
                continue

            ReadConsoleInputW(
                hIn, byref(input_records), MAX_EVENTS, byref(read_count)
            )
            read_input_records = input_records[: read_count.value]

            del keys[:]
            new_size: tuple[int, int] | None = None

            for input_record in read_input_records:
                event_type = input_record.EventType
                if event_type == KEY_EVENT:
                    key_event = input_record.Event.KeyEvent
                    if key_event.bKeyDown:
                        if key_event.dwControlKeyState and key_event.wVirtualKeyCode == 0:
                            continue  # IME 辅助事件，无字符
                        translated = translate_key_event(
                            key_event.wVirtualKeyCode,
                            key_event.dwControlKeyState,
                            key_event.uChar.UnicodeChar,
                        )
                        if translated:
                            append_key(translated)
                elif event_type == MOUSE_EVENT:
                    mouse_event = input_record.Event.MouseEvent
                    translated = translate_mouse_event(
                        mouse_event.dwMousePosition.X,
                        mouse_event.dwMousePosition.Y,
                        mouse_event.dwButtonState,
                        mouse_event.dwControlKeyState,
                        mouse_event.dwEventFlags,
                    )
                    if translated:
                        append_key(translated)
                elif event_type == WINDOW_BUFFER_SIZE_EVENT:
                    size = input_record.Event.WindowBufferSizeEvent.dwSize
                    new_size = (size.X, size.Y)

            if keys:
                # 与 Textual 原逻辑一致：UTF-16 surrogatepass 处理 IME 代理对
                for event in parser.feed(
                    "".join(keys).encode("utf-16", "surrogatepass").decode("utf-16")
                ):
                    self.process_event(event)
            if new_size is not None:
                self.on_size_change(*new_size)

    except Exception as error:
        self.app.log.error("EVENT MONITOR ERROR", error)


def patch_windows_input() -> bool:
    """禁用 Textual Windows 驱动的 Kitty 启用序列 + 改传统输入模式。

    仅 win32 生效；其他平台直接返回 ``False``（无需 patch）。
    返回是否实际执行了 patch。
    """
    global _patched
    if sys.platform != "win32":
        return False
    if _patched:
        return True

    from textual.drivers import win32
    from textual.drivers.windows_driver import WindowsDriver

    # 1. Kitty 启用序列过滤（conhost 不支持，无害清除）
    orig_write = WindowsDriver.write

    def _write_filtered(self: WindowsDriver, data: str) -> None:
        orig_write(self, filter_kitty_enable(data))

    WindowsDriver.write = _write_filtered  # type: ignore[method-assign]

    # 2. 传统输入模式（对齐 Claude Code：不设 ENABLE_VIRTUAL_TERMINAL_INPUT）
    win32.enable_application_mode = _compat_enable_application_mode

    # 3. EventMonitor.run：VK 码 → xterm 序列 + SGR 鼠标翻译
    win32.EventMonitor.run = _compat_event_monitor_run  # type: ignore[method-assign]

    _patched = True
    return True
