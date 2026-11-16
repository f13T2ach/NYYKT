"""终端底层：VT 模式、清屏、光标、按键读取、显示宽度计算。"""

from __future__ import annotations

import os
import shutil
import sys
import unicodedata

IS_WIN = os.name == "nt"
CSI = "\x1b["

# ---------------------------------------------------------------- 颜色
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
ITALIC = "\x1b[3m"
REV = "\x1b[7m"
UNDER = "\x1b[4m"


def fg(n: int) -> str:
    return f"\x1b[38;5;{n}m"


def bg(n: int) -> str:
    return f"\x1b[48;5;{n}m"


# 配色（256 色）
C_ACCENT = fg(51)      # 青
C_TITLE = fg(45)
C_OK = fg(84)
C_WARN = fg(214)
C_ERR = fg(203)
C_DIM = fg(244)
C_LABEL = fg(110)
C_SEL_BG = bg(24)      # 选中行底色
C_SEL_FG = fg(231)


def enable_vt() -> None:
    if not IS_WIN:
        return
    try:
        import ctypes

        k = ctypes.windll.kernel32
        for std in (-11, -12):
            h = k.GetStdHandle(std)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


# ---------------------------------------------------------------- 尺寸
def size() -> tuple[int, int]:
    """窗口尺寸（不是缓冲区尺寸）。

    Windows 上 shutil.get_terminal_size 拿到的是 buffer 大小，比可见窗口高得多，
    按它画会因为多出来的换行触发滚动，导致每帧错位、内容重复。
    """
    if IS_WIN:
        try:
            import ctypes
            import struct

            handle = ctypes.windll.kernel32.GetStdHandle(-11)
            csbi = ctypes.create_string_buffer(22)
            if ctypes.windll.kernel32.GetConsoleScreenBufferInfo(handle, csbi):
                (_x, _y, _xl, _yt, _xa, left, top, right, bottom,
                 _mx, _my) = struct.unpack("hhhhHhhhhhh", csbi.raw)
                cols = right - left + 1
                rows = bottom - top + 1
                if cols > 0 and rows > 0:
                    return max(60, cols), max(18, rows)
        except Exception:
            pass
    try:
        s = shutil.get_terminal_size((100, 30))
    except Exception:
        return 100, 30
    return max(60, s.columns), max(18, s.lines)


# ---------------------------------------------------------------- 光标
def hide_cursor() -> None:
    sys.stdout.write(CSI + "?25l")


def show_cursor() -> None:
    sys.stdout.write(CSI + "?25h")


def move(row: int, col: int) -> None:
    sys.stdout.write(f"{CSI}{row};{col}H")


def clear() -> None:
    sys.stdout.write(CSI + "2J" + CSI + "H")


def flush() -> None:
    try:
        sys.stdout.flush()
    except Exception:
        pass


_overflow_rows = 0


def paint(lines: list[str], overflow: bool = False) -> None:
    """整屏重绘。

    通常裁到窗口高度、绝不滚动。overflow=True 且内容超屏时，
    允许写满整屏并向下溢出，用户自己往上滚就能看到全部。
    """
    global _overflow_rows
    w, h = size()
    h = max(10, h - 1)          # 留一行余量，最后一行写满也不会触发 scroll

    if overflow and len(lines) > h:
        # 溢出模式：先退回上次写过的位置，清掉旧内容，再整块重写
        out = []
        if _overflow_rows:
            out.append(f"{CSI}{_overflow_rows}F")   # 光标上移 N 行到起点
        out.append(CSI + "J")
        for raw in lines:
            if dwidth(raw) > w:
                raw = trunc_w(raw, w)
            out.append(raw + CSI + "K" + "\r\n")
        _overflow_rows = len(lines)
        sys.stdout.write("".join(out))
        flush()
        return

    _overflow_rows = 0
    body = lines[:h]
    # 尾部空行不写，避免最后一行反复换行
    while body and not strip_ansi(body[-1]).strip():
        body.pop()

    out = [CSI + "H"]
    for i, raw in enumerate(body):
        if dwidth(raw) > w:
            raw = trunc_w(raw, w)
        out.append(raw)
        out.append(CSI + "K")
        if i < len(body) - 1:
            out.append("\r\n")
    out.append(CSI + "J")       # 光标以下全清，抹掉上一帧残留
    sys.stdout.write("".join(out))
    flush()


# ---------------------------------------------------------------- 宽度
def char_w(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def dwidth(s: str) -> int:
    # 去掉 ANSI 转义再算
    return sum(char_w(c) for c in strip_ansi(s))


def strip_ansi(s: str) -> str:
    out = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "\x1b" and i + 1 < n and s[i + 1] == "[":
            j = i + 2
            while j < n and not ("@" <= s[j] <= "~"):
                j += 1
            i = j + 1
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


def trunc_w(s: str, w: int) -> str:
    """按显示宽度截断（保留 ANSI 序列）。"""
    out = []
    cur = 0
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "\x1b" and i + 1 < n and s[i + 1] == "[":
            j = i + 2
            while j < n and not ("@" <= s[j] <= "~"):
                j += 1
            out.append(s[i:j + 1])
            i = j + 1
            continue
        cw = char_w(s[i])
        if cur + cw > w:
            break
        out.append(s[i])
        cur += cw
        i += 1
    out.append(RESET)
    return "".join(out)


def pad_w(s: str, w: int) -> str:
    return s + " " * max(0, w - dwidth(s))


# ---------------------------------------------------------------- 键盘
_ARROWS = {
    "H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT",
    "I": "PGUP", "Q": "PGDN", "G": "HOME", "O": "END",
}


def kbhit() -> bool:
    if IS_WIN:
        try:
            import msvcrt

            return msvcrt.kbhit()
        except Exception:
            return False
    import select

    try:
        return bool(select.select([sys.stdin], [], [], 0)[0])
    except Exception:
        return False


def getkey() -> str:
    """读一个键，返回语义名：UP/DOWN/LEFT/RIGHT/ENTER/ESC/BACKSPACE/CTRL_C 或字符。"""
    if IS_WIN:
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            code = msvcrt.getwch()
            return _ARROWS.get(code, "")
        if ch == "\r":
            return "ENTER"
        if ch == "\x1b":
            return "ESC"
        if ch == "\x03":
            return "CTRL_C"
        if ch in ("\x08", "\x7f"):
            return "BACKSPACE"
        if ch == "\t":
            return "TAB"
        if ch == " ":
            return "SPACE"
        return ch

    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            seq = sys.stdin.read(2)
            return {"[A": "UP", "[B": "DOWN", "[C": "RIGHT", "[D": "LEFT"}.get(seq, "ESC")
        if ch in ("\r", "\n"):
            return "ENTER"
        if ch in ("\x7f", "\x08"):
            return "BACKSPACE"
        if ch == "\x03":
            return "CTRL_C"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
