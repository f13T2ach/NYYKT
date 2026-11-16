"""界面组件：Banner、状态面板、横向菜单、进度条。"""

from __future__ import annotations

from . import term
from .term import (
    C_ACCENT, C_DIM, C_ERR, C_LABEL, C_OK, C_SEL_BG, C_SEL_FG, C_TITLE, C_WARN,
    BOLD, DIM, RESET, dwidth, pad_w, trunc_w,
)

VERSION = "1.0.0"
AUTHOR = "Redmaple"

BANNER = [
    r" _   _       _____         _     ",
    r"| \ | |_   _|_   _|__  ___| |__  ",
    r"|  \| | | | | | |/ _ \/ __| '_ \ ",
    r"| |\  | |_| | | |  __/ (__| | | |",
    r"|_| \_|\__, | |_|\___|\___|_| |_|",
    r"       |___/                    ",
]

# 终端矮的时候用这个，省两行
BANNER_SHORT = [
    r" _   _       _____         _     ",
    r"| \ | |_   _|_   _|__  ___| |__  ",
    r"|  \| | | | | | |/ _ \/ __| '_ \ ",
    r"|_| \_|\__, | |_|\___|\___|_| |_|",
]


def banner_lines(width: int, compact: bool = False) -> list[str]:
    """字符画 + 右侧版本信息。"""
    out: list[str] = []
    tag1 = f"Ver {VERSION} by {AUTHOR}"
    tag2 = "DeepSeek"
    for i, art in enumerate(BANNER_SHORT if compact else BANNER):
        line = f"{C_TITLE}{art}{RESET}"
        if i == 0:
            line += " " * 4 + f"{C_DIM}{tag1}{RESET}"
        elif i == 1:
            line += " " * 4 + f"{C_DIM}{tag2}{RESET}"
        out.append(line)
    return out


def wrap_w(text: str, width: int, max_lines: int = 0) -> list[str]:
    """按终端显示宽度折行（中文算 2 格）。max_lines>0 时超出用省略号收尾。"""
    if not text:
        return []
    if width <= 4:
        return [text]
    out: list[str] = []
    cur = ""
    for ch in text:
        if ch == "\n":
            out.append(cur)
            cur = ""
            continue
        if dwidth(cur + ch) > width:
            out.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        out.append(cur)
    if max_lines and len(out) > max_lines:
        out = out[:max_lines]
        out[-1] = trunc_w(out[-1] + "…", width)
    return out


def rule(width: int, ch: str = "─", color: str = C_DIM, label: str = "") -> str:
    """分隔线。给了 label 就居中嵌进去（用来显示页码）。"""
    w = min(max(20, width - 2), 76)
    if not label:
        return f"{color}{ch * w}{RESET}"
    tag = f" {label} "
    left = max(0, (w - len(tag)) // 2)
    right = max(0, w - left - len(tag))
    return (f"{color}{ch * left}{RESET}"
            f"{C_LABEL}{tag}{RESET}"
            f"{color}{ch * right}{RESET}")


def select_row(label: str, selected: bool, width: int, indent: int = 2) -> str:
    """统一选中行样式：选中整条反显，用 ASCII > 保证列对齐。"""
    body = f"{' ' * indent}{'>' if selected else ' '} {label}"
    if selected:
        return f"{C_SEL_BG}{C_SEL_FG}{BOLD}{pad_w(body, min(width - 2, 72))}{RESET}"
    return body


def two_col(label: str, value: str, width: int) -> str:
    return f"  {C_LABEL}{label}{RESET} {value}"


def status_block(st: dict, width: int, compact: bool = False) -> list[str]:
    """当前服务器 / 登录ID / 课程数目 / 讨论数目 / 作业数目"""
    lines: list[str] = []

    server = st.get("server") or "长江雨课堂"
    uid = st.get("uid") or "?"
    nick = st.get("nickname") or ""
    who = f"{uid}" + (f"  {C_DIM}{nick}{RESET}" if nick else "")

    if compact:
        # 终端矮：服务器和登录ID挤一行
        if st.get("logged_in"):
            lines.append(f"  {C_LABEL}{server}{RESET}  {C_DIM}·{RESET}  {who}")
        else:
            lines.append(f"  {C_LABEL}{server}{RESET}  {C_DIM}·{RESET}  {C_ERR}未登录{RESET}")
    else:
        lines.append(f"  {C_LABEL}当前服务器：{RESET}{C_ACCENT}{server}{RESET}")
        if st.get("logged_in"):
            lines.append(f"  {C_LABEL}登录ID：{RESET}{who}")
        else:
            lines.append(f"  {C_LABEL}登录ID：{RESET}{C_ERR}未登录{RESET}")

    if not st.get("logged_in"):
        return lines

    if not st.get("classroom"):
        lines.append(f"  {C_LABEL}课程数目：{RESET}{C_DIM}未绑定课程{RESET}")
        lines.append(f"  {C_LABEL}作业数目：{RESET}{C_DIM}—{RESET}")
        return lines

    if not st.get("stats_ready"):
        lines.append(f"  {C_LABEL}课程数目：{RESET}{C_DIM}统计中…{RESET}")
        lines.append(f"  {C_LABEL}作业数目：{RESET}{C_DIM}统计中…{RESET}")
        return lines

    vt, vd = st.get("videos_total", 0), st.get("videos_done", 0)
    vleft = max(0, vt - vd)
    vcolor = C_OK if vleft == 0 else C_WARN

    # 作用域：当前这门课
    course = st.get("course_name") or ""
    if course:
        lines.append(f"  {C_LABEL}当前课程：{RESET}{C_TITLE}{course}{RESET}")
    lines.append(
        f"  {C_LABEL}课程数目：{RESET}{vcolor}{vleft} 未完成{RESET}"
        f"  {C_DIM}/{RESET}  {C_OK}{vd} 已完成{RESET}"
        f"  {C_DIM}（共 {vt}）{RESET}"
    )

    # 讨论：每个单元下面那个"说点什么"
    if st.get("disc_scan_off"):
        lines.append(f"  {C_LABEL}讨论数目：{RESET}{C_DIM}未统计（主页可开）{RESET}")
    elif not st.get("disc_ready"):
        scan = st.get("disc_scan")
        label = f"统计中… {scan}" if scan else "统计中…"
        lines.append(f"  {C_LABEL}讨论数目：{RESET}{C_DIM}{label}{RESET}")
    else:
        dt, dd = st.get("disc_total", 0), st.get("disc_done", 0)
        dleft = max(0, dt - dd)
        dcolor = C_OK if dleft == 0 else C_WARN
        lines.append(
            f"  {C_LABEL}讨论数目：{RESET}{dcolor}{dleft} 未完成{RESET}"
            f"  {C_DIM}/{RESET}  {C_OK}{dd} 已完成{RESET}"
            f"  {C_DIM}（共 {dt}）{RESET}"
        )

    # 作业：leaf_type=6 那种要交卷的
    ht, hd = st.get("hw_total", 0), st.get("hw_done", 0)
    if ht:
        hleft = max(0, ht - hd)
        hcolor = C_OK if hleft == 0 else C_WARN
        lines.append(
            f"  {C_LABEL}作业数目：{RESET}{hcolor}{hleft} 未完成{RESET}"
            f"  {C_DIM}/{RESET}  {C_OK}{hd} 已完成{RESET}"
            f"  {C_DIM}（共 {ht}）{RESET}"
        )

    # 多模态能力警告：模型不能看图时，考试里带图的题只能退回 OCR
    if st.get("ai_vision") is False:
        lines.append(
            f"  {C_WARN}⚠ 当前 AI 模型不支持看图，考试里含图片的题会退回 OCR{RESET}"
        )
    return lines


def menu_lines(items: list[tuple[str, str, bool]], cursor: int, width: int,
               active: bool = True, cols: int = 1) -> list[str]:
    """items: [(key, label, enabled)]。选中整条反显。

    cols=2 时排成两列，选中格高亮，光标按行/列走。
    """
    if cols <= 1:
        out: list[str] = []
        for i, (_key, label, enabled) in enumerate(items):
            if label.startswith("-") or label == "":
                out.append(f"  {C_DIM}{'─' * min(width - 6, 40)}{RESET}")
                continue
            selected = (i == cursor) and active
            if not enabled:
                out.append(f"{C_DIM}{pad_w(f'   {label}', min(width - 4, 70))}{RESET}")
                continue
            out.append(select_row(label, selected, width))
        return out

    n = len(items)
    if not n:
        return []
    total_w = min(width - 2, 76)
    colw = max(18, total_w // cols)
    rows = (n + cols - 1) // cols
    out = []
    for r in range(rows):
        line = ""
        for c in range(cols):
            i = r * cols + c
            if i >= n:
                line += " " * colw
                continue
            _key, label, enabled = items[i]
            sel = (i == cursor) and active and enabled
            body = f" {'>' if sel else ' '} {label}"
            if sel:
                line += f"{C_SEL_BG}{C_SEL_FG}{BOLD}{pad_w(body, colw - 1)}{RESET} "
            elif not enabled:
                line += f"{C_DIM}{pad_w(body, colw)}{RESET}"
            else:
                line += pad_w(body, colw)
        out.append(line.rstrip())
    return out


def progress_bar(done: int, total: int, width: int = 24) -> str:
    """纯 ASCII 进度条：填充用 #，未填充用空格。

    刻意避开 U+2580-259F 那一带块字符——PowerShell 默认字体缺字形，会渲染成问号。
    """
    if total <= 0:
        return f"{C_DIM}[{' ' * width}]{RESET}  {C_DIM}0/0{RESET}"
    ratio = max(0.0, min(1.0, done / total))
    filled = max(0, min(width, int(round(ratio * width))))
    if filled >= width:
        bar = f"{C_OK}{'#' * width}{RESET}"
    else:
        bar = f"{C_ACCENT}{'#' * filled}{RESET}" + " " * (width - filled)
    pct = f"{C_LABEL}{ratio * 100:5.1f}%{RESET}"
    return f"{C_DIM}[{RESET}{bar}{C_DIM}]{RESET} {pct}  {C_DIM}{done}/{total}{RESET}"


def fmt_dur(sec: float | int | None) -> str:
    if sec is None:
        return "--:--"
    s = int(max(0, sec))
    h, m, ss = s // 3600, (s % 3600) // 60, s % 60
    if h:
        return f"{h}:{m:02d}:{ss:02d}"
    return f"{m:02d}:{ss:02d}"


def boxed(lines: list[str], width: int, title: str = "") -> list[str]:
    w = min(width, 100)
    inner = w - 4
    top = f"  {C_DIM}┌─{RESET} {C_LABEL}{title}{RESET} " + f"{C_DIM}{'─' * max(0, inner - dwidth(title) - 1)}{RESET}"
    body = [f"  {C_DIM}│{RESET} " + pad_w(f"{trunc_w(l, inner)}", inner) for l in lines]
    bot = f"  {C_DIM}└{'─' * inner}{RESET}"
    return [top] + body + [bot]
