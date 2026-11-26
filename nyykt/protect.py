"""窗口防监视。

Windows 的 SetWindowDisplayAffinity 能让窗口从截图/录屏里排除掉
（WDA_EXCLUDEFROMCAPTURE，Win10 2004+）。实测非管理员调用会直接失败，
所以开了这项但当前不是管理员时，需要重启自己到提权进程。
"""

from __future__ import annotations

import ctypes
import sys

WDA_NONE = 0x0
WDA_MONITOR = 0x1
WDA_EXCLUDEFROMCAPTURE = 0x11


def _user32():
    u = ctypes.windll.user32
    u.GetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    u.GetWindowDisplayAffinity.restype = ctypes.c_bool
    u.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    u.SetWindowDisplayAffinity.restype = ctypes.c_bool
    return u


def _console_hwnd() -> int:
    """找真正可见的那个窗口。

    传统 conhost：GetConsoleWindow 直接可用。
    Windows Terminal：拿到的是隐藏的 proxy，得往上找父窗口
    （CASCADIA_HOSTING_WINDOW_CLASS 才是屏幕上那个）。
    """
    try:
        k32 = ctypes.windll.kernel32
        u32 = ctypes.windll.user32
        hwnd = int(k32.GetConsoleWindow() or 0)
        if not hwnd:
            return 0
        # 顺着父窗口链往上爬，第一个可见的就是要操作的目标
        cur = hwnd
        for _ in range(8):
            if u32.IsWindowVisible(cur):
                return cur
            nxt = int(u32.GetParent(cur) or 0)
            if not nxt:
                nxt = int(u32.GetAncestor(cur, 2) or 0)   # GA_ROOT
            if not nxt or nxt == cur:
                break
            cur = nxt
        return hwnd      # 都不行就交回原来的，让 API 自己报错
    except Exception:
        return 0


def window_info() -> dict:
    """诊断用：当前拿到的窗口是什么。"""
    if not sys.platform.startswith("win"):
        return {}
    hwnd = _console_hwnd()
    if not hwnd:
        return {"hwnd": 0}
    u32 = ctypes.windll.user32
    cls = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(hwnd, cls, 256)
    title = ctypes.create_unicode_buffer(512)
    u32.GetWindowTextW(hwnd, title, 512)
    raw = int(ctypes.windll.kernel32.GetConsoleWindow() or 0)
    return {
        "hwnd": hwnd,
        "class": cls.value,
        "title": title.value,
        "visible": bool(u32.IsWindowVisible(hwnd)),
        "console_hwnd": raw,
        "same": hwnd == raw,
    }


def is_admin() -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def current_affinity() -> int:
    hwnd = _console_hwnd()
    if not hwnd:
        return -1
    val = ctypes.c_uint32()
    if not _user32().GetWindowDisplayAffinity(hwnd, ctypes.byref(val)):
        return -1
    return int(val.value)


_own_hwnd = 0        # AllocConsole 出来的窗口，属于本进程


def hide_from_capture() -> tuple[bool, str]:
    """把当前控制台窗口从截图中排除。返回 (成功?, 说明)。"""
    if not sys.platform.startswith("win"):
        return False, "只有 Windows 支持"

    # 优先用自己 AllocConsole 出来的那个窗口，别往上爬（会跑到别的窗口上）
    hwnd = int(globals().get("_own_hwnd") or 0)
    if not hwnd:
        info = window_info()
        hwnd = info.get("hwnd") or 0
        if not hwnd:
            return False, "找不到控制台窗口（可能不是从终端启动的）"
        if info.get("class") == "PseudoConsoleWindow":
            return False, ("Windows Terminal 的伪控制台不支持，"
                           "请把默认终端改成「Windows 控制台主机」再试")

    if not is_admin():
        return False, "需要管理员权限（错误码 5 = 拒绝访问）"

    u = _user32()
    u.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    u.SetWindowDisplayAffinity.restype = ctypes.c_bool
    ctypes.windll.kernel32.SetLastError(0)

    if u.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
        return True, "已从截图/录屏中排除"
    # 老系统退回「截图变黑」
    if u.SetWindowDisplayAffinity(hwnd, WDA_MONITOR):
        return True, "已隐藏（旧系统：截图显示为黑块）"

    err = ctypes.windll.kernel32.GetLastError()
    if err == 5:
        return False, "访问被拒绝（错误码 5），确认是管理员终端"
    return False, f"系统不支持或调用被拒绝（错误码 {err}）"


def show_in_capture() -> bool:
    """恢复可见（能被截到）。"""
    hwnd = int(globals().get("_own_hwnd") or 0) or _console_hwnd()
    if not hwnd:
        return False
    try:
        return bool(_user32().SetWindowDisplayAffinity(hwnd, WDA_NONE))
    except Exception:
        return False


def console_owner_pid() -> int:
    """当前控制台窗口属于哪个进程（conhost 的 pid）。"""
    hwnd = _console_hwnd()
    if not hwnd:
        return 0
    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(
        ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return int(pid.value)


def _rebind_std() -> None:
    """FreeConsole/AllocConsole 之后重新绑定标准流。"""
    try:
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    except Exception:
        pass
    try:
        sys.stderr = sys.stdout
    except Exception:
        pass
    try:
        sys.stdin = open("CONIN$", "r", encoding="utf-8", buffering=1)
    except Exception:
        pass


def spawn_own_console() -> tuple[bool, str]:
    """脱离当前终端，开一个属于本进程的新控制台。

    终端窗口属于 conhost，跨进程设不了防监视；只有自己 AllocConsole
    出来的窗口才能调 SetWindowDisplayAffinity。
    返回 (成功?, 说明)，并记下原终端 pid 供 attach_back 用。
    """
    if not sys.platform.startswith("win"):
        return False, "只有 Windows 支持"
    try:
        k32 = ctypes.windll.kernel32
        prev = console_owner_pid()
        k32.FreeConsole()
        if not k32.AllocConsole():
            # 失败就把控制台接回去，别把用户晾在无控制台状态
            if prev:
                k32.AttachConsole(prev)
            _rebind_std()
            return False, "创建新控制台失败"
        _rebind_std()
        globals()["_prev_console_pid"] = prev
        # 重新开 VT；新窗口默认没有
        try:
            from .term import enable_vt

            enable_vt()
        except Exception:
            pass
        hwnd = int(k32.GetConsoleWindow() or 0)
        globals()["_own_hwnd"] = hwnd
        op = console_owner_pid()
        import os

        if hwnd and op != os.getpid():
            return True, f"新控制台已开，但归属仍是 {op}"
        return True, "已切换到独立控制台窗口"
    except Exception as e:
        return False, f"切换失败：{e}"


def attach_back() -> bool:
    """切回原来的终端（如果还记得是哪个）。"""
    if not sys.platform.startswith("win"):
        return False
    prev = int(globals().get("_prev_console_pid") or 0)
    try:
        k32 = ctypes.windll.kernel32
        k32.FreeConsole()
        if prev and k32.AttachConsole(prev):
            _rebind_std()
            try:
                from .term import enable_vt

                enable_vt()
            except Exception:
                pass
            globals()["_prev_console_pid"] = 0
            globals()["_own_hwnd"] = 0
            return True
        # 接不回去就自己开一个，至少还有输出
        if k32.AllocConsole():
            _rebind_std()
        return False
    except Exception:
        return False
    """恢复可见（能被截到）。"""
    hwnd = _console_hwnd()
    if not hwnd:
        return False
    try:
        return bool(_user32().SetWindowDisplayAffinity(hwnd, WDA_NONE))
    except Exception:
        return False


def relaunch_as_admin(argv: list[str] | None = None) -> bool:
    """用 runas 重新拉起自己。调用方应在成功后退出当前进程。

    参数从 sys.argv 还原成 `-m nyykt ...`，否则提权窗口只会开个空 REPL。
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        extra = list(sys.argv[1:] if argv is None else argv)
        args = ["-m", "nyykt", *extra]
        params = " ".join(f'"{a}"' if " " in a else a for a in args)
        r = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1)
        return int(r) > 32      # >32 表示成功
    except Exception:
        return False
