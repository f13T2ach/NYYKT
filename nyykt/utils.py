"""通用工具：日志、文本清洗、句子切分、重试。"""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime
from pathlib import Path

RESET = "\033[0m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[36m"
MAGENTA = "\033[35m"

_USE_COLOR = sys.stdout.isatty()

# TUI 模式下把日志接管走，避免 print 把整屏布局打烂
_log_sink = None


def set_log_sink(fn) -> None:
    """把 log 输出重定向到回调（TUI 用）。传 None 恢复 stdout。"""
    global _log_sink
    _log_sink = fn


def _c(code: str, text: str) -> str:
    return f"{code}{text}{RESET}" if _USE_COLOR else text


def log(msg: str, tag: str = "*") -> None:
    if _log_sink is not None:
        try:
            _log_sink(str(msg))
            return
        except Exception:
            pass
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{_c(DIM, ts)} {_c(BLUE, '[' + tag + ']')} {msg}", flush=True)


def ok(msg: str) -> None:
    log(_c(GREEN, msg), "OK")


def warn(msg: str) -> None:
    log(_c(YELLOW, msg), "!!")


def err(msg: str) -> None:
    log(_c(RED, msg), "XX")


def step(msg: str) -> None:
    log(_c(MAGENTA, msg), ">>")


def die(msg: str, code: int = 1):
    err(msg)
    raise SystemExit(code)


# ---------------------------------------------------------------- 文本处理

_WS = re.compile(r"[ \t\u00a0\u3000]+")
_MULTI_NL = re.compile(r"\n{2,}")
_INVISIBLE = re.compile(r"[\u200b-\u200f\ufeff\u2060]")


def clean(text: str) -> str:
    """压缩空白、去零宽字符，保留单换行。"""
    if not text:
        return ""
    t = _INVISIBLE.sub("", text)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = _WS.sub(" ", t)
    t = _MULTI_NL.sub("\n", t)
    return t.strip()


def squeeze(text: str) -> str:
    """彻底单行化，用于比对去重。"""
    return re.sub(r"\s+", "", clean(text))


SENT_SPLIT = re.compile(r"[。！？；!?;\n]+")


def split_sentences(text: str) -> list[str]:
    """按中英文句号/分号/问号/叹号切句，返回非空句子。"""
    return [s.strip() for s in SENT_SPLIT.split(clean(text)) if s.strip()]


def pick_sentence(text: str, lo: int = 20, hi: int = 90) -> str | None:
    """从大纲里挑一句长度合适的。先挑落在 [lo, hi] 的，再退而求其次。"""
    sents = split_sentences(text)
    good = [s for s in sents if lo <= len(s) <= hi]
    if good:
        return max(good, key=len)
    mid = [s for s in sents if len(s) >= 12]
    if mid:
        return max(mid, key=lambda s: -abs(len(s) - (lo + hi) // 2))
    return None


def cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk / len(text)


def truncate(text: str, n: int = 60) -> str:
    t = clean(text).replace("\n", " ")
    return t if len(t) <= n else t[: n - 1] + "…"


def slugify_name(text: str, n: int = 40) -> str:
    keep = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", text).strip("_")
    return (keep or "page")[:n]


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def human_wait(page, lo: float = 0.4, hi: float = 1.1) -> None:
    """拟人化小停顿，避免动作过于机械。"""
    import random

    time.sleep(random.uniform(lo, hi))

def to_clipboard(text: str) -> bool:
    """把文本复制到 Windows 剪贴板（UTF-16，中文不会乱码）。"""
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        u32 = ctypes.windll.user32
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002

        # 64 位下必须声明，否则 HANDLE/LPVOID 会被截断成 32 位
        k32.GlobalAlloc.restype = ctypes.c_void_p
        k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalLock.argtypes = [ctypes.c_void_p]
        k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        k32.GlobalFree.argtypes = [ctypes.c_void_p]
        u32.SetClipboardData.restype = ctypes.c_void_p
        u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

        data = (text or "").encode("utf-16-le") + b"\x00\x00"
        h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        ptr = k32.GlobalLock(h)
        if not ptr:
            k32.GlobalFree(h)
            return False
        ctypes.memmove(ptr, data, len(data))
        k32.GlobalUnlock(h)

        if not u32.OpenClipboard(None):
            k32.GlobalFree(h)
            return False
        try:
            u32.EmptyClipboard()
            # 成功后所有权转移给系统，不能再 GlobalFree
            return bool(u32.SetClipboardData(CF_UNICODETEXT, h))
        finally:
            u32.CloseClipboard()
    except Exception:
        return False
