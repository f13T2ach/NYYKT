"""NyTech TUI —— 横向选单式交互界面。"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import state as sm
from . import term, ui
from .api import Api
from .booster import Pace, boost_leaf, boost_leaf_paced, fetch_course, fetch_ids
from .browser import Session
from .config import load
from .forum import get_topic, has_my_comment, run_for_leaf
from .term import (
    C_ACCENT, C_DIM, C_ERR, C_LABEL, C_OK, C_TITLE, C_WARN, RESET,
)
from .utils import truncate

RATE_CHOICES = [1.0, 1.25, 1.5, 2.0, 2.5, 3.0]
DELAY_CHOICES = [0.5, 1.0, 1.5, 2.0, 3.0, 5.0]
SEGMENT_CHOICES = [6, 10, 12, 15, 20, 30]
SERVERS = ["长江雨课堂", "荷塘雨课堂", "黄河雨课堂"]


# ================================================================== 数据层

def fetch_identity(api: Api) -> tuple[str, str]:
    r = api.call("GET", "/api/v3/user/basic-info")
    d = (r.get("json") or {}).get("data") or {}
    return str(d.get("id") or ""), str(d.get("name") or d.get("nickname") or "")


def collect_stats(cfg, classroom: str, want_disc: bool, cancel: threading.Event,
                  on_partial=None) -> dict:
    """开一个 headless 浏览器把状态拉全。"""
    out: dict = {"logged_in": False, "uid": "", "nickname": ""}
    s = Session(cfg)
    try:
        s.start(headless=True, mobile=True)
        api = Api(s.ctx, cfg)
        uid, nick = fetch_identity(api)
        out.update(logged_in=bool(uid), uid=uid, nickname=nick)
        if not uid or not classroom:
            return out

        _name, leaves = fetch_course(api, classroom)
        if not leaves:
            # 换到一门还没内容的课时，必须把旧统计清零，不然界面还显示上一门的数据
            out.update(
                course_name=_name or "",
                videos_total=0, videos_done=0,
                hw_total=0, hw_done=0, hw_pending=[], hw_names={},
                disc_total=0, disc_done=0,
                stats_ready=True, empty_course=True,
            )
            if on_partial:
                on_partial(dict(out))
            return out
        out["course_name"] = _name or ""
        vids = [l for l in leaves if l.kind == "video"]
        hws = [l for l in leaves if l.kind == "quiz"]
        out["videos_total"] = len(vids)
        out["videos_done"] = sum(1 for l in vids if l.done)
        out["hw_total"] = len(hws)
        out["hw_done"] = sum(1 for l in hws if l.done)
        out["hw_pending"] = [
            {"leaf_id": l.leaf_id, "name": l.name} for l in hws if not l.done
        ]
        # 所有作业的名字，进度条上显示用
        out["hw_names"] = {str(l.leaf_id): l.name for l in hws}

        # 顺手把课程列表也拿了 —— 免得切作用域时再开一次浏览器
        try:
            from . import courses as courses_mod

            out["courses"] = courses_mod.fetch_my_courses(api)
        except Exception:
            pass
        out["stats_ready"] = True
        if on_partial:
            on_partial(dict(out))

        if not want_disc or cancel.is_set():
            out["disc_scan_off"] = True
            return out

        ids = fetch_ids(api, classroom, leaves[0].leaf_id)
        total = done = 0
        for i, leaf in enumerate(leaves, 1):
            if cancel.is_set():
                break
            t = get_topic(api, ids, leaf.leaf_id)
            if not t:
                continue
            total += 1
            if has_my_comment(api, t):
                done += 1
            if i % 8 == 0:
                out["disc_total"] = total
                out["disc_done"] = done
                out["disc_scan"] = f"{i}/{len(leaves)}"
                if on_partial:
                    on_partial(dict(out))
        out["disc_total"] = total
        out["disc_done"] = done
        out["disc_ready"] = True
        return out
    finally:
        try:
            s.close()
        except Exception:
            pass


@contextmanager
def locked_session(cfg, lock, timeout: float = 20.0, **start_kw):
    """整个 Session 生命周期都握在锁里，避免两个浏览器抢同一个 .profile。

    带超时：统计线程正在跑的时候不会无限期卡住调用方。
    """
    if not lock.acquire(timeout=timeout):
        raise RuntimeError("浏览器被后台任务占用中，请稍后重试")
    try:
        s = Session(cfg)
        try:
            s.start(**start_kw)
            yield s
        finally:
            try:
                s.close()
            except Exception:
                pass
    finally:
        lock.release()


def parse_range(text: str, upper: int) -> list[int] | None:
    """'1-20' / '3,5,8' / '1-5,9' -> 0-based 索引列表。"""
    text = (text or "").strip().replace("－", "-")
    if not text:
        return None
    idx: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b or a)
            except ValueError:
                return None
            if lo > hi:
                lo, hi = hi, lo
            idx.extend(range(lo - 1, hi))
        else:
            try:
                idx.append(int(part) - 1)
            except ValueError:
                return None
    return [i for i in idx if 0 <= i < upper]


# ================================================================== 应用

class App:
    def __init__(self):
        self.cfg = load()
        self.state = sm.load_state()
        self.alive = True
        self.page = "main"
        self.cursor = 0
        self.set_cursor = 0
        self.out: list[str] = []
        self.input_text = ""
        self.input_prompt = ""
        self.input_action = ""
        self.input_back = "main"
        self.st: dict = {
            "server": self.state.get("server", "长江雨课堂"),
            "logged_in": False,
            "uid": "",
            "nickname": "",
            "classroom": self.state.get("classroom", ""),
            "stats_ready": False,
            "disc_ready": False,
            "checking": True,
        }
        self._pw_lock = threading.Lock()
        self._menu_overflow: tuple[int, int] = (0, 0)
        self.out_scroll = 0               # 输出区往上翻的行数
        self._out_span = (0, 0, 0)        # (start, end, total)
        self._hw_waiting = False          # 正在等用户确认提交
        self._hw_pending: dict = {}
        self._hw_choice: str = ""
        self._hw_gate = threading.Event()
        self.confirm_cursor = 0
        # ---- 考试辅助
        self.exam_problems: list[dict] = []
        self.exam_answers: dict[int, dict] = {}
        self.exam_status: dict[int, str] = {}
        self.exam_cursor = 0
        self.exam_info: dict = {}
        self.ai_models: list[str] = []     # 拉到的模型列表，←→ 在里面选
        self.ai_loading = False
        self.course_list: list[dict] = []  # 作用域候选
        self.course_loading = False
        self.course_error = ""
        self.exam_list: list[dict] = []    # 可用考试
        self.exam_loading = False
        self._pending_admin = False
        self._exam_runner = None          # 考试会话保持打开，答题完也不关
        self._exam_thread: threading.Thread | None = None
        self._exam_close = threading.Event()
        self.exam_scroll = 0              # 答案区往上翻的行数
        self._stat_thread: threading.Thread | None = None
        self._stat_cancel = threading.Event()
        self._task_thread: threading.Thread | None = None
        self._task_cancel = threading.Event()
        self._task_prog: dict = {}
        self._task_kind = ""
        self._task_done = False
        self._task_result: list[dict] = []

    # ------------------------------------------------------------- 生命周期
    def run(self) -> int:
        import os

        term.enable_vt()
        if os.environ.get("NYYKT_TUI_SELFTEST"):
            self.st.update({"logged_in": True, "uid": "self-test",
                            "classroom": "00000000", "stats_ready": True,
                            "videos_total": 102, "videos_done": 61,
                            "disc_total": 108, "disc_done": 103})
            self.render()
            print()
            return 0

        term.hide_cursor()
        term.clear()
        try:
            from .utils import set_log_sink

            set_log_sink(lambda m: self.log(str(m)))
            self.log("欢迎。↑↓ 移动，Enter 进入，Esc 返回，Q 退出。")
            # 载入后自动拉一次模型列表，省得用户先去设置里点
            try:
                from .ai import AI

                if AI(self.state.get("ai")).usable:
                    self._refresh_ai_caps()
                    self.fetch_models(quiet=True)
            except Exception:
                pass

            # 防监视在控制台里做不到，历史遗留的开关一律归零
            ui = self.state.setdefault("ui", {})
            if ui.get("hide_from_capture"):
                ui["hide_from_capture"] = False
                sm.save_state(self.state)

            self.refresh_stats(quick=False)
            self._loop()
        except KeyboardInterrupt:
            pass
        finally:
            from .utils import set_log_sink

            set_log_sink(None)
            self._task_cancel.set()
            self._stat_cancel.set()
            self._hw_gate.set()          # 放行可能还在等确认的作业线程
            self._exam_close.set()       # 收掉考试的浏览器
            for th in (self._task_thread, self._stat_thread):
                if th and th.is_alive():
                    try:
                        th.join(timeout=6)
                    except Exception:
                        pass
            term.show_cursor()
            term.clear()
            print("已退出 NyTech。")

            # 用户刚开了「防止监视」，需要管理员：这里提权重启
            if getattr(self, "_pending_admin", False):
                from . import protect

                if protect.relaunch_as_admin():
                    print("已在新窗口以管理员身份启动。")
                else:
                    print("提权被取消或失败，防监视没生效。")
                    try:
                        u = self.state.setdefault("ui", {})
                        u["hide_from_capture"] = False
                        sm.save_state(self.state)
                    except Exception:
                        pass
        return 0

    def _loop(self) -> None:
        """状态驱动重绘：只有画面会变时才画，但后台状态一变就能反映出来。"""
        sig = None
        while self.alive:
            cur = self._signature()
            if cur != sig or self.task_running:
                self.render()
                sig = cur
            # 一次把所有待处理按键吃掉
            while term.kbhit():
                self.handle(term.getkey())
                sig = None
                if not self.alive:
                    break
            time.sleep(0.05)

    def _signature(self) -> tuple:
        p = self._task_prog or {}
        return (
            self.page, self.cursor, self.set_cursor,
            self.st.get("logged_in"), self.st.get("checking"),
            self.st.get("stats_ready"), self.st.get("disc_ready"),
            self.st.get("videos_done"), self.st.get("disc_total"),
            self.st.get("disc_scan"), self.st.get("uid"),
            self.st.get("hw_done"), self.st.get("hw_total"),
            len(self.out),
            p.get("idx"), p.get("phase"), int(p.get("eta") or 0),
            self.input_text, self._task_kind,
            self.out_scroll, self.confirm_cursor, self._hw_waiting,
            self.exam_cursor, len(self.exam_problems), len(self.exam_answers),
            sum(1 for v in self.exam_status.values() if v in ("done", "thinking", "skipped")),
            self.cursor if self.page == "course" else -1,
            len(self.course_list), self.course_loading,
        )

    @property
    def task_running(self) -> bool:
        return bool(self._task_thread and self._task_thread.is_alive())

    @property
    def busy_login(self) -> bool:
        return bool(self._task_thread and self._task_thread.is_alive() and self._task_kind == "login")

    def log(self, text: str, color: str = "") -> None:
        stamp = time.strftime("%H:%M:%S")
        self.out.append(f"  {C_DIM}{stamp}{RESET} {color}{text}{RESET}")
        if len(self.out) > 2000:
            self.out = self.out[-1500:]

    # ------------------------------------------------------------- 菜单
    def menu_items(self) -> list[tuple[str, str, bool]]:
        logged = self.st.get("logged_in")
        if not logged:
            return [
                ("login", "登录", True),
                ("server", "选择服务器", True),
                ("settings", "设置", True),
                ("quit", "退出", True),
            ]

        autom = self.state["automation"]
        auto_txt = f"防封自动化  [{C_OK + '开' + RESET if autom['enabled'] else C_DIM + '关' + RESET}]"
        if autom["enabled"]:
            auto_txt += f"{C_DIM}  {autom['rate']}x + {autom['click_delay']}s{RESET}"
        disc = self.state["discussion"]
        disc_txt = f"自动讨论  [{C_OK + '开' + RESET if disc['auto'] else C_DIM + '关' + RESET}]"
        scan_txt = f"讨论统计  [{C_OK + '开' + RESET if disc.get('auto_scan') else C_DIM + '关' + RESET}]"

        return [
            ("exam", "辅助答题", True),
            ("boost_all", "刷课 · 全部", True),
            ("boost_range", "刷课 · 区间", True),
            ("disc_all", "完成讨论 · 全部", True),
            ("disc_range", "完成讨论 · 区间", True),
            ("cdisc", "学习讨论 · 全部", True),
            ("hw_all", "完成作业 · 全部", True),
            ("hw_range", "完成作业 · 区间", True),
            ("automation", auto_txt, True),
            ("disc_toggle", disc_txt, True),
            ("disc_scan", scan_txt, True),
            ("course", "作用域 / 切换课程", True),
            ("server", "选择服务器", True),
            ("settings", "设置", True),
            ("relogin", "重新登录", True),
            ("refresh", "刷新状态", True),
            ("quit", "退出", True),
        ]

    def settings_items(self) -> list[tuple[str, str]]:
        a = self.state["automation"]
        d = self.state["discussion"]
        ai = self.state.get("ai", {})
        onoff = lambda v: f"{C_OK}开{RESET}" if v else f"{C_DIM}关{RESET}"
        key_txt = (f"{C_OK}已配置{RESET}" if ai.get("api_key")
                   else f"{C_ERR}未配置{RESET}")
        return [
            ("-", "防封与讨论"),
            ("automation", f"防封自动化      {onoff(a['enabled'])}"),
            ("rate", f"播放倍速        {C_ACCENT}{a['rate']:.2f}x{RESET}"),
            ("click_delay", f"人手点击间隔    {C_ACCENT}{a['click_delay']:.1f}s{RESET}"),
            ("segment", f"节流粒度        {C_ACCENT}{a['segment']}s{RESET}"),
            ("disc_auto", f"自动讨论        {onoff(d['auto'])}"),
            ("strategy", f"讨论策略        {C_ACCENT}{sm.STRATEGY_NAMES.get(d['strategy'], d['strategy'])}{RESET}"),
            ("class_ai", f"学习讨论用 AI   {onoff(d.get('class_ai', True))}"),
            ("avoid_note", f"回避「笔记」贴  {onoff(d.get('avoid_note', True))}"),
            ("-", "AI 配置"),
            ("ai_enabled", f"AI 作答         {onoff(ai.get('enabled'))}"),
            ("ai_provider", f"AI 服务商       {C_ACCENT}{ai.get('provider', 'deepseek')}{RESET}"),
            ("ai_model", f"AI 模型         {C_ACCENT}{ai.get('model', '')}{RESET}"
                         + (f"  {C_DIM}(←→ 从列表选){RESET}" if self.ai_models else "")),
            ("ai_temp", f"模型温度        {C_ACCENT}{float(ai.get('temperature', 0.3)):.1f}{RESET}"
                        f"  {C_DIM}(低=稳，高=活){RESET}"),
            ("ai_refresh", f"重新获取模型    {C_DIM}{self.ai_model_hint()}{RESET}"),
            ("ai_base", f"服务商地址      {C_ACCENT}{(ai.get('base_url') or '')[:38]}{RESET}"),
            ("ai_key", f"API Key         {key_txt}"),
            ("ai_prompt", f"附加提示词      "
                          f"{C_DIM}{(ai.get('extra_prompt') or '（未设置）')[:30]}{RESET}"),
            ("ai_vision", f"看图能力        {C_ACCENT}{ai.get('vision', 'auto')}{RESET}"
                          f"  {C_DIM}(auto/on/off){RESET}"),
            ("ai_test", f"测试连接        {C_DIM}(Enter 测试){RESET}"),
            ("-", "作业与界面"),
            ("hw_auto_submit", f"作业自动提交    {onoff((self.state.get('homework') or {}).get('auto_submit'))}"),
            ("hw_show_browser", f"显示浏览器      {onoff((self.state.get('homework') or {}).get('show_browser'))}"
                                f"  {C_DIM}(开了会弹出窗口){RESET}"),
            ("ui_out_lines", f"输出行数        {C_ACCENT}{self.out_lines()}{RESET}"),
            ("ui_hide", f"防止监视此窗口  {C_DIM}关（控制台窗口不支持，已固定）{RESET}"),
            ("-", ""),
            ("back", "返回"),
        ]

    def _menu_window(self, items: list, budget: int, cols: int = 1) -> tuple[list, int, int]:
        """菜单超出可用高度时，只显示光标周围的一段。

        cols=2 时 budget 按「行」算，一次显示 budget*cols 项。
        """
        n = len(items)
        if cols <= 1:
            if n <= budget or budget <= 0:
                return items, self.cursor, 0
            start = max(0, min(self.cursor - budget // 2, n - budget))
            return items[start:start + budget], self.cursor - start, start

        rows = (n + cols - 1) // cols
        if rows <= budget or budget <= 0:
            return items, self.cursor, 0
        cur_row = self.cursor // cols
        start_row = max(0, min(cur_row - budget // 2, rows - budget))
        s = start_row * cols
        e = min(n, s + budget * cols)
        return items[s:e], self.cursor - s, s

    # ------------------------------------------------------------- 提交确认
    def confirm_items(self) -> list[tuple[str, str, bool]]:
        res = self._hw_pending or {}
        can = bool(res.get("can_submit", True))
        subj = res.get("subjective") or []
        n = len(res.get("answered") or [])
        submit_label = f"立即提交  {C_DIM}({n} 题已答){RESET}"
        if not can:
            submit_label = (f"立即提交  {C_ERR}（第 {','.join(map(str, subj))} 题是主观题，"
                            f"不允许提交）{RESET}")
        return [
            ("submit", submit_label, can),
            ("view", "查看作答情况", True),
            ("cancel", "取消任务（不提交）", True),
        ]

    def render_confirm(self, w: int) -> list[str]:
        res = self._hw_pending or {}
        out = [
            f"  {C_TITLE}答案提交{RESET}  {C_DIM}作业 {res.get('leaf_id', '')}"
            f" · 共 {res.get('total', '?')} 题{RESET}",
            "",
        ]
        items = self.confirm_items()
        for i, (_k, label, _en) in enumerate(items):
            out.append(ui.select_row(label, i == self.confirm_cursor, w))
        return out

    def handle_confirm(self, key: str) -> None:
        items = self.confirm_items()
        if key in ("UP", "k"):
            self.confirm_cursor = (self.confirm_cursor - 1) % len(items)
        elif key in ("DOWN", "j"):
            self.confirm_cursor = (self.confirm_cursor + 1) % len(items)
        elif key in ("ESC", "q", "Q"):
            self._hw_choice = "cancel"
            self._hw_waiting = False
            self._hw_gate.set()
        elif key == "ENTER":
            kind, _label, enabled = items[self.confirm_cursor]
            if not enabled:
                self.log("有主观题没答，不允许直接提交", C_WARN)
                return
            if kind == "view":
                self.show_hw_answers()
                self.confirm_cursor = 0
                return
            self._hw_choice = kind
            self._hw_waiting = False
            self._hw_gate.set()

    def show_hw_answers(self) -> None:
        res = self._hw_pending or {}
        rows = res.get("answered") or []
        if not rows:
            self.log("还没有作答记录", C_WARN)
            return
        for a in rows:
            flag = " [主观题]" if a.get("subjective") else ""
            self.log(f"  Q{a.get('index')}{flag}  {a.get('answer') or '-'}  "
                     f"{str(a.get('body'))[:48]}")

    # ------------------------------------------------------------- 渲染
    def ai_model_hint(self) -> str:
        if self.ai_models:
            return f"(已拉 {len(self.ai_models)} 个，Enter 重拉)"
        return "(Enter 拉取)"

    def out_lines(self) -> int:
        try:
            v = int((self.state.get("ui") or {}).get("out_lines", 8))
        except Exception:
            v = 8
        return max(8, min(30, v))

    def render(self) -> None:
        w, h = term.size()
        budget_h = max(10, h - 1)
        want_out = self.out_lines()
        # 视觉能力标记，给状态区显示警告用
        try:
            from .ai import AI

            self.st["ai_vision"] = AI(self.state.get("ai")).supports_vision
        except Exception:
            self.st["ai_vision"] = None
        # 终端矮就压缩 banner / 状态区，把行数让给输出和菜单
        compact = budget_h < 40 + max(0, want_out - 8)
        lines: list[str] = []
        lines += ui.banner_lines(w, compact=compact)
        if not compact:
            lines.append(ui.rule(w))
        lines += ui.status_block(self.st, w, compact=compact)
        lines.append("")

        if self.page == "exam":
            lines += self.render_exam(w)
        elif self.page == "settings":
            items = self.settings_items()
            n = len(items)
            # 按光标开窗，长列表也能翻
            avail = max(4, budget_h - len(lines) - 3)
            start = 0
            if n > avail:
                start = max(0, min(self.set_cursor - avail // 2, n - avail))
            end = min(n, start + avail)
            head_label = f"{start + 1}-{end}/{n}" if n > avail else ""
            lines.append(ui.rule(w, "┄", label=head_label))
            lines.append(f"  {C_TITLE}设置{RESET}  {C_DIM}（防封策略 / 自动讨论 / 作业）{RESET}")
            for i, (k, label) in enumerate(items[start:end], start):
                if not label:
                    continue
                if k == "-":
                    lines.append(f"  {C_DIM}{'─' * 6} {label} {'─' * 6}{RESET}")
                    continue
                lines.append(ui.select_row(label, i == self.set_cursor, w))
        elif self.page == "examlist":
            lines.append(f"  {C_TITLE}辅助答题{RESET}  "
                         f"{C_DIM}选一门考试；工具只读题给答案，作答和提交你自己来{RESET}")
            lines.append("")
            if self.exam_list:
                from . import exams as exams_mod

                shown = max(3, budget_h - len(lines) - 2)
                start = max(0, min(self.cursor - shown // 2, len(self.exam_list) - shown))
                for i, e in enumerate(self.exam_list[start:start + shown], start):
                    lines.append(ui.select_row(exams_mod.label_of(e, w - 8),
                                               i == self.cursor, w))
                if self.exam_loading:
                    lines.append(f"  {C_DIM}后台刷新中…{RESET}")
            elif self.exam_loading:
                lines.append(f"  {C_DIM}正在拉考试列表…{RESET}")
            else:
                lines.append(f"  {C_DIM}最近一周没有待考的考试{RESET}")
                lines.append("")
                lines.append(f"  {C_DIM}按 L 粘贴考试链接，R 重新拉取，Esc 返回{RESET}")
        elif self.page == "course":
            lines.append(f"  {C_TITLE}作用域{RESET}  {C_DIM}刷课 / 讨论 / 学习讨论都在这里选中的课程下进行{RESET}")
            lines.append("")
            if self.course_list:
                # 有缓存就立刻能选，后台刷新不挡界面
                from . import courses as courses_mod

                cur = str(self.st.get("classroom") or "")
                shown = max(3, budget_h - len(lines) - 2)
                start = max(0, min(self.cursor - shown // 2, len(self.course_list) - shown))
                for i, c in enumerate(self.course_list[start:start + shown], start):
                    mark = f"  {C_DIM}← 当前{RESET}" if str(c["classroom_id"]) == cur else ""
                    if c.get("teach"):
                        mark += f"  {C_DIM}（教）{RESET}"
                    label = courses_mod.label_of(c, w - 24) + mark
                    lines.append(ui.select_row(label, i == self.cursor, w))
                if self.course_loading:
                    lines.append(f"  {C_DIM}后台刷新中…{RESET}")
            elif self.course_loading:
                lines.append(f"  {C_DIM}正在拉课程列表…{RESET}")
            else:
                lines.append(f"  {C_WARN}没有课程{RESET}"
                             + (f"  {C_DIM}{self.course_error}{RESET}" if self.course_error else ""))
                lines.append(f"  {C_DIM}按 R 重新拉取，Esc 返回{RESET}")
        elif self.page == "input":
            lines.append(ui.rule(w, "┄"))
            lines.append(f"  {C_TITLE}{self.input_prompt}{RESET}")
            lines.append("")
            lines.append(f"  {C_ACCENT}> {RESET}{self.input_text}{C_ACCENT}_{RESET}")
        elif self.page == "server":
            lines.append(ui.rule(w, "┄"))
            lines.append(f"  {C_TITLE}选择服务器{RESET}")
            for i, name in enumerate(SERVERS):
                mark = f"  {C_DIM}← 当前{RESET}" if name == self.st.get("server") else ""
                lines.append(ui.select_row(f"{name}{mark}", i == self.cursor, w))
        else:
            # 主菜单两列，budget 是「行数」
            reserve = 1 + 1 + 1 + want_out + 1 + 1
            budget = max(2, budget_h - len(lines) - 1 - reserve)
            items = self.menu_items()
            rows_total = (len(items) + 1) // 2
            page_label = ""
            if rows_total > budget > 0:
                page_no = (self.cursor // 2) // budget + 1
                page_max = max(1, (rows_total + budget - 1) // budget)
                page_label = f"{page_no}/{page_max}"

            lines.append(ui.rule(w, "┄", label=page_label))
            lines.append("")
            if self.st.get("checking") and not self.task_running:
                lines.append(f"  {C_DIM}正在检查登录状态…{RESET}")
            elif self.task_running:
                if self._hw_waiting:
                    lines += self.render_confirm(w)
                else:
                    lines += self.render_progress(w)
            else:
                sub, cur, start = self._menu_window(items, budget, cols=2)
                self._menu_overflow = (start, max(0, len(items) - start - len(sub)))
                lines += ui.menu_lines(sub, cur, w, active=True, cols=2)

        # ---- 输出区
        lines.append("")
        lines.append(ui.rule(w, "┄"))
        used = len(lines)
        # 空间富裕就填满；不富裕（菜单已经让过位）也至少有 want_out 行
        room = max(2, budget_h - used - 1)
        n = len(self.out)
        end = max(0, n - self.out_scroll)
        start = max(0, end - room)
        self._out_span = (start, end, n)
        span = f"{start + 1}-{end}/{n}" if n else "0/0"
        scroll_hint = f"  {C_DIM}+/- 翻页{RESET}" if n > room else ""
        lines.append(f"  {C_LABEL}输出{RESET} {C_DIM}[{span}]{RESET}{scroll_hint}")
        tail = self.out[start:end] if n else [f"  {C_DIM}（暂无输出）{RESET}"]
        lines += tail

        hint = self.hint()
        lines.append(f"  {C_DIM}{hint}{RESET}")
        term.paint(lines)

    def render_progress(self, w: int) -> list[str]:
        p = self._task_prog or {}
        if self._task_kind == "login":
            return [
                f"  {C_TITLE}登录中{RESET}  {p.get('label', '等待扫码…')}",
                f"  {C_DIM}在浏览器窗口里扫码，成功后自动返回；Esc 可取消{RESET}",
            ]

        out: list[str] = []
        kind = {"boost": "刷课", "disc": "发讨论", "stats": "统计",
                "hw": "AI作答"}.get(self._task_kind, "任务")
        total = int(p.get("total") or 0)
        idx = int(p.get("idx") or 0)

        head = f"  {C_TITLE}{kind}中{RESET}  {C_ACCENT}{idx}/{total}{RESET}"
        if p.get("label"):
            head += f"  {truncate(str(p['label']), 44)}"
        out.append(head)
        out.append("  " + ui.progress_bar(idx, total, 28))

        if p.get("phase") == "pace":
            cp = float(p.get("cp") or 0)
            vlen = float(p.get("vlen") or 1)
            out.append("  " + ui.progress_bar(int(cp), int(vlen), 28)
                       + f"  {C_DIM}节流中 {p.get('seg')}/{p.get('nseg')}{RESET}")
            out.append(f"  {C_DIM}本节剩余 {ui.fmt_dur(p.get('eta'))}{RESET}")
        elif p.get("elapsed") is not None:
            el = float(p.get("elapsed") or 0)
            eta = (el / idx) * (total - idx) if idx else 0
            out.append(f"  {C_DIM}已用 {ui.fmt_dur(el)}  预计剩余 {ui.fmt_dur(eta)}{RESET}")
        return out

    def hint(self) -> str:
        if self._hw_waiting:
            return "↑↓ 选择   Enter 确认   Esc 取消   +/- 翻输出"
        if self.task_running or self._task_prog:
            return "Esc 中止任务   +/- 翻输出"
        if self.page == "exam":
            return "↑↓ 选题  Enter 重想  S 跳过  C 复制  [ ] 翻答案  Esc 返回"
        if self.page == "settings":
            return "↑↓ 选择   ←→ 调整   Enter 切换   Esc 返回"
        if self.page == "course":
            return "↑↓ 选课程   Enter 设为作用域   R 重新拉取   Esc 返回"
        if self.page == "examlist":
            return "↑↓ 选考试   Enter 开始   L 粘贴链接   R 重新拉取   Esc 返回"
        if self.page == "input":
            return "输入内容   Enter 确认   Esc 取消"
        if self.page == "server":
            return "↑↓ 选择   Enter 确认   Esc 返回"
        above, below = getattr(self, "_menu_overflow", (0, 0))
        extra = ""
        if above or below:
            bits = []
            if above:
                bits.append(f"上 {above}")
            if below:
                bits.append(f"下 {below}")
            extra = f"   （还有 {'/'.join(bits)} 项）"
        return f"↑↓←→ 选择   Enter 确认   Q 退出   +/- 翻输出{extra}"

    # ------------------------------------------------------------- 输入
    def handle(self, key: str) -> None:
        if key == "CTRL_C":
            self.alive = False
            return

        # 输出翻页：任何页面都能用
        if key in ("+", "=", "＋", "＝", "PGUP"):
            shown = max(5, self._out_span[1] - self._out_span[0])
            self.out_scroll = min(max(0, len(self.out) - 1), self.out_scroll + shown)
            return
        if key in ("-", "_", "－", "—", "PGDN"):
            self.out_scroll = max(0, self.out_scroll - 5)
            return

        # 提交确认选单
        if self._hw_waiting:
            self.handle_confirm(key)
            return

        # 首屏统计期间不接受菜单操作，避免"以为没登录"误触登录
        if self.st.get("checking") and not self.task_running:
            if key in ("q", "Q"):
                self.alive = False
            return

        if self.page == "exam":
            self.handle_exam(key)
            return

        if self.page == "course":
            self.handle_course(key)
            return

        if self.page == "examlist":
            self.handle_examlist(key)
            return

        if self.page == "input":
            self.handle_input(key)
            return
        if self.page == "settings":
            self.handle_settings(key)
            return
        if self.page == "server":
            self.handle_server(key)
            return

        # 考试页优先：任务还在跑也能看题目、切题、跳过
        if self.page == "exam":
            if key == "ESC" and (self.task_running or self._exam_thread):
                self._task_cancel.set()
                self._exam_close.set()
                self.log("已退出辅助答题，浏览器正在关闭…", C_WARN)
                self.page = "main"
                self.cursor = 0
                return
            self.handle_exam(key)
            return

        # 任务运行时只允许 Esc 中止
        if self.task_running:
            if key == "ESC":
                self._task_cancel.set()
                self.log("已请求中止…", C_WARN)
            return

        items = self.menu_items()
        n = len(items)
        # 两列布局：上下跨一行（±2），左右切列（±1）
        if key in ("UP", "k"):
            self.cursor = max(0, self.cursor - 2)
        elif key in ("DOWN", "j"):
            self.cursor = min(n - 1, self.cursor + 2)
        elif key in ("LEFT", "h"):
            self.cursor = max(0, self.cursor - 1)
        elif key in ("RIGHT", "l"):
            self.cursor = min(n - 1, self.cursor + 1)
        elif key == "ESC":
            pass
        elif key in ("q", "Q"):
            self.alive = False
        elif key == "ENTER" or (len(key) == 1 and key.isdigit()):
            if key.isdigit():
                num = int(key)
                if 1 <= num <= n:
                    self.cursor = num - 1
            self.activate(items)

    def _step(self, items: list, cur: int, d: int) -> int:
        n = len(items)
        i = cur
        for _ in range(n):
            i = (i + d) % n
            if items[i][0] != "-":
                return i
        return cur

    def _index_of_items(self, raw: list, nth: int) -> int:
        c = 0
        for i, it in enumerate(raw):
            if it[0] == "-":
                continue
            if c == nth:
                return i
            c += 1
        return 0

    def activate(self, raw: list) -> None:
        if not raw:
            return
        key = raw[self.cursor][0]
        fn = getattr(self, f"act_{key}", None)
        if fn:
            fn()
        elif key == "-":
            pass

    # ------------------------------------------------------------- 动作
    def act_quit(self) -> None:
        self.alive = False

    def act_login(self) -> None:
        self.start_login(force=False)

    def act_relogin(self) -> None:
        self.start_login(force=True)

    def act_server(self) -> None:
        self.page = "server"
        cur = self.st.get("server", SERVERS[0])
        self.cursor = SERVERS.index(cur) if cur in SERVERS else 0

    def handle_server(self, key: str) -> None:
        if key in ("UP", "k"):
            self.cursor = (self.cursor - 1) % len(SERVERS)
        elif key in ("DOWN", "j"):
            self.cursor = (self.cursor + 1) % len(SERVERS)
        elif key == "ENTER":
            name = SERVERS[self.cursor]
            if name == self.st.get("server"):
                self.log(f"当前服务器已是 {name}", C_OK)
            else:
                self.log("选择服务器：暂未支持 —— 目前仅接通「长江雨课堂」"
                         "(changjiang.yuketang.cn)，其余服务器接口不同", C_WARN)
            self.page = "main"
            self.cursor = 0
        elif key == "ESC":
            self.page = "main"
            self.cursor = 0

    def act_automation(self) -> None:
        a = self.state["automation"]
        a["enabled"] = not a["enabled"]
        sm.save_state(self.state)
        if a["enabled"]:
            self.log(f"防封自动化：开 · {a['rate']}x 倍速 + {a['click_delay']}s 人手点击间隔，"
                     f"按理论最快时间推送", C_OK)
        else:
            self.log("防封自动化：关（瞬间推送，最快）")

    def act_disc_toggle(self) -> None:
        d = self.state["discussion"]
        d["auto"] = not d["auto"]
        sm.save_state(self.state)
        self.log(f"自动讨论：{'开' if d['auto'] else '关'}")

    def act_disc_scan(self) -> None:
        """讨论数目自动统计。开了就立刻统计一次，关掉就不再扫全课程。"""
        d = self.state["discussion"]
        d["auto_scan"] = not d.get("auto_scan")
        sm.save_state(self.state)
        if d["auto_scan"]:
            self.log("讨论自动统计：开，立刻统计一次…", C_OK)
            self.st["disc_scan_off"] = False
            self.refresh_stats(quick=False)
        else:
            self.log("讨论自动统计：关（不再扫描，讨论数目显示为未统计）", C_DIM)
            self.st["disc_scan_off"] = True
            self.st["disc_ready"] = False

    def act_exam(self) -> None:
        """辅助答题：只读题目 + AI 给答案，不操作浏览器。"""
        self.ask_input("粘贴考试链接，或直接填考试 ID", self._exam_start,
                       prefill="", back="main")

    def _exam_start(self, text: str) -> None:
        t = (text or "").strip()
        import re as _re

        m = _re.search(r"examcover/(\d+)/(\d+)", t)
        if m:
            classroom, exam_id = m.group(1), m.group(2)
        elif t.isdigit():
            classroom = (self.st.get("classroom") or "").strip()
            exam_id = t
        else:
            self.log("看不懂这个输入，贴 examcover/{classroom}/{exam_id} 这种链接就行", C_ERR)
            return
        if not classroom:
            self.log("先绑定课程", C_WARN)
            return
        self.start_exam(classroom, exam_id)

    def start_exam(self, classroom: str, exam_id: str) -> None:
        """只读题目 + AI 答题，不碰浏览器里的任何作答控件。"""
        if self.task_running:
            return
        from .ai import AI
        from .exam import ExamRunner, ai_solve

        ai = AI(self.state.get("ai"))
        if not ai.usable:
            self.log("AI 没启用或没配 Key，先去「设置 → AI 作答」配置", C_WARN)
            return

        # 上一次的考试浏览器还开着会占住 profile，先收掉
        if self._exam_thread is not None and self._exam_thread.is_alive():
            self.log("先关掉上一次的考试浏览器…", C_DIM)
            self._exam_close.set()
            self._exam_thread.join(timeout=12)
        self._exam_close = threading.Event()

        # 注意：考试辅助不看「显示浏览器」设置，永远可见——按钮要用户自己点
        self.exam_problems = []
        self.exam_answers = {}
        self.exam_status = {}
        self.exam_cursor = 0
        self.exam_info = {"classroom": classroom, "exam_id": exam_id}
        self._begin_task("exam", f"辅助答题 {exam_id}")
        cancel = self._task_cancel

        def work():
            # 考试辅助必须看得见：按钮得用户自己点
            runner = ExamRunner(self.cfg, classroom, exam_id,
                                lambda m: self.log(str(m)), headless=False)
            try:
                res = runner.enter(
                    on_wait=lambda: self.log(
                        "请在弹出的浏览器里点「继续答题」，限时提示里也点继续。我在这等你…",
                        C_WARN),
                    cancel=cancel)
                if res != "ok":
                    msg = {"timeout": "等太久没进考试", "closed": "浏览器被关闭",
                           "cancelled": "你按了 Esc"}.get(res, res)
                    self.log(f"{msg}，辅助答题已取消", C_WARN)
                    self.page = "main"
                    self.cursor = 0
                    return
                probs = runner.read()
                if not probs:
                    self.log("没读到题目", C_ERR)
                    return
                self.exam_problems = probs
                for p in probs:
                    self.exam_status[p["index"]] = "pending"
                # 立刻切到题目列表，边答边看，不再单独显示进度页
                self.exam_cursor = 0
                self.exam_scroll = 0
                self.page = "exam"
                self._task_prog = {}

                # 题干含图片的：能看图就截图喂模型，看不了才退回 OCR
                for p in probs:
                    if not (p.get("imgs") or p.get("optImgs")):
                        continue
                    self._task_prog = {"total": len(probs), "idx": 0,
                                       "label": f"第 {p['no']} 题读图"}
                    if ai.supports_vision:
                        b64 = runner.image_b64(p)
                        if b64:
                            p["vision_b64"] = b64
                            self.log(f"  第 {p['no']} 题占了一屏，截图已附给模型")
                    if not p.get("vision_b64"):
                        txt = runner.ocr_problem(p)
                        if txt:
                            p["ocr"] = txt
                            self.log(f"  第 {p['no']} 题退回 OCR：{txt[:40]}")

                # 题干为空但有图的，先用 OCR 文本顶上，别直接判"读不到"
                for p in probs:
                    if not p.get("body") and p.get("ocr"):
                        p["body"] = p["ocr"][:900]
                        self.log(f"  第 {p['no']} 题题干靠 OCR 补上")

                self.log(f"读到 {len(probs)} 题，开始逐题作答")
                self._task_prog = {"total": len(probs), "idx": 0, "label": "准备中"}

                for i, p in enumerate(probs):
                    if cancel.is_set():
                        self.log("已中止", C_WARN)
                        break
                    if self.exam_status.get(p["index"]) == "skipped":
                        continue
                    if not p.get("body"):
                        self.exam_status[p["index"]] = "error"
                        self.exam_answers[p["index"]] = {"answer": "", "reason": "题干读不到"}
                        continue
                    self.exam_status[p["index"]] = "thinking"
                    self._task_prog = {"total": len(probs), "idx": i,
                                       "label": f"第 {p['no']} 题 {p['kindName']}"}
                    try:
                        imgs = [p["vision_b64"]] if p.get("vision_b64") else None
                        js = ai_solve(ai, p, images=imgs, exam_title=runner.title)
                        self.exam_answers[p["index"]] = js
                        self.exam_status[p["index"]] = "done"
                        self.log(f"  {p['no']}. {p['kindName']} → {js['answer'][:40]}")
                    except Exception as e:
                        self.exam_status[p["index"]] = "error"
                        self.exam_answers[p["index"]] = {"answer": "", "reason": f"失败：{e}"}
                        self.log(f"  第 {p['no']} 题失败：{e}", C_WARN)
                    self._task_prog = {"total": len(probs), "idx": i + 1,
                                       "label": f"第 {p['no']} 题"}
                self.log("答题完成，答案在下面的选单里")
                self.log("浏览器保持打开，你可以直接在里面作答；答完自己关窗口就行", C_DIM)
                self.page = "exam"
            except Exception as e:
                self.log(f"辅助答题出错：{e}", C_ERR)
            finally:
                self._task_prog = {}
                self._task_kind = ""
                self._exam_runner = runner
                # 逻辑上任务已结束（菜单可用），线程留着守 Session：
                # Playwright 对象跨线程不能用，浏览器要开着给你抄答案。
                self._task_thread = None
                self.log("浏览器保持打开，你可以在里面作答；关窗口后下次启动会自动清理", C_DIM)
                while not self._exam_close.is_set():
                    time.sleep(0.4)
                try:
                    runner.close()
                except Exception:
                    pass

        self._exam_thread = threading.Thread(target=work, daemon=True)
        self._task_thread = self._exam_thread
        self._exam_thread.start()

    def exam_current(self) -> dict | None:
        if 0 <= self.exam_cursor < len(self.exam_problems):
            return self.exam_problems[self.exam_cursor]
        return None

    def render_exam(self, w: int) -> list[str]:
        info = self.exam_info or {}
        probs = self.exam_problems
        done = sum(1 for v in self.exam_status.values() if v == "done")
        thinking = sum(1 for v in self.exam_status.values() if v == "thinking")
        skipped = sum(1 for v in self.exam_status.values() if v == "skipped")
        errs = sum(1 for v in self.exam_status.values() if v == "error")

        head = (f"  {C_TITLE}辅助答题{RESET}  "
                f"{C_DIM}考试 {info.get('exam_id', '')}{RESET}  "
                f"{C_OK}{done}{RESET}{C_DIM}/{len(probs)} 完成{RESET}")
        if thinking:
            head += f"  {C_WARN}思考中 {thinking}{RESET}"
        if skipped:
            head += f"  {C_DIM}跳过 {skipped}{RESET}"
        if errs:
            head += f"  {C_ERR}失败 {errs}{RESET}"
        out = [head, ""]
        marks = {
            "done": f"{C_OK}OK{RESET}",
            "thinking": f"{C_WARN}思考中{RESET}",
            "skipped": f"{C_DIM}已跳过{RESET}",
            "error": f"{C_ERR}失败{RESET}",
            "pending": f"{C_DIM}待答{RESET}",
        }
        for i, p in enumerate(self.exam_problems):
            st = self.exam_status.get(p["index"], "pending")
            ans = (self.exam_answers.get(p["index"]) or {}).get("answer") or ""
            label = f"{p['no']:2}. {p['kindName']:<4} {ans[:14]:<16} {marks.get(st, '')}"
            out.append(ui.select_row(label, i == self.exam_cursor, w))

        cur = self.exam_current()
        if cur:
            out.append("")
            out.append(ui.rule(w, "┄"))
            js = self.exam_answers.get(cur["index"]) or {}
            body_w = max(20, min(w - 10, 76))

            # 题目 + 选项 + 答案 + 思考 拼成一整块，整体分页
            block: list[str] = []

            def push(label: str, text: str, color: str) -> None:
                lines_ = ui.wrap_w(text or "", body_w) or ["—"]
                for i, line in enumerate(lines_):
                    if i == 0:
                        block.append(f"  {C_LABEL}{label}{RESET} {color}{line}{RESET}")
                    else:
                        block.append(f"       {color}{line}{RESET}")

            push("题目", cur["body"], "")
            for o in cur["opts"]:
                block.append(f"    {C_DIM}{o['key']}. {ui.trunc_w(o['text'], body_w - 4)}{RESET}")
            push("答案", js.get("answer") or "—", C_OK)
            push("思考", (js.get("reason") or "").strip(), C_DIM)

            n = len(block)
            page = 10                      # 一屏 10 行，超了整体翻页
            start = min(max(0, self.exam_scroll), max(0, n - 1))
            end = min(n, start + page)
            chunk = block[start:end]
            if n > page and chunk:
                chunk[0] = chunk[0] + f"  {C_DIM}[{start + 1}-{end}/{n}]{RESET}"
            out.extend(chunk)
        return out

    def handle_exam(self, key: str) -> None:
        n = len(self.exam_problems)
        if key in ("UP", "k"):
            self.exam_cursor = max(0, self.exam_cursor - 1)
            self.exam_scroll = 0
        elif key in ("DOWN", "j"):
            self.exam_cursor = min(max(0, n - 1), self.exam_cursor + 1)
            self.exam_scroll = 0
        elif key == "ESC":
            self.page = "main"
            self.cursor = 0
        elif key in ("s", "S"):
            cur = self.exam_current()
            if cur:
                self.exam_status[cur["index"]] = "skipped"
                self.log(f"第 {cur['no']} 题已跳过（Enter 可重新思考）", C_DIM)
        elif key in ("[", "{", "【", "〔"):
            self.exam_scroll = max(0, self.exam_scroll - 10)
        elif key in ("]", "}", "】", "〕"):
            self.exam_scroll += 10
        elif key in ("c", "C"):
            cur = self.exam_current()
            if not cur:
                return
            js = self.exam_answers.get(cur["index"]) or {}
            ans = (js.get("answer") or "").strip()
            if not ans:
                self.log(f"第 {cur['no']} 题还没有答案，按 Enter 先让它想想", C_WARN)
                return
            text = ans if key == "c" else f"{ans}\n\n【思路】{js.get('reason') or ''}"
            from .utils import to_clipboard

            if to_clipboard(text):
                self.log(f"第 {cur['no']} 题{'答案+思路' if key == 'C' else '答案'}已复制", C_OK)
            else:
                self.log("复制失败（剪贴板被别的程序占着？再试一次）", C_ERR)
        elif key == "ENTER":
            self.exam_rethink()

    def exam_rethink(self) -> None:
        """对当前题重新问一次 AI。"""
        cur = self.exam_current()
        if not cur:
            return
        from .ai import AI
        from .exam import ai_solve

        ai = AI(self.state.get("ai"))
        if not ai.usable:
            self.log("AI 没配置", C_WARN)
            return
        idx = cur["index"]
        self.exam_status[idx] = "thinking"
        self.log(f"重新思考第 {cur['no']} 题…", C_DIM)

        def work():
            try:
                js = ai_solve(ai, cur)
                self.exam_answers[idx] = js
                self.exam_status[idx] = "done"
                self.log(f"  第 {cur['no']} 题 → {js['answer'][:40]}")
            except Exception as e:
                self.exam_status[idx] = "error"
                self.exam_answers[idx] = {"answer": "", "reason": f"失败：{e}"}
                self.log(f"  重新思考失败：{e}", C_WARN)

        threading.Thread(target=work, daemon=True).start()

    def act_cdisc(self) -> None:
        """班级讨论区（学习讨论）：AI 写评论发进去。"""
        if self.task_running:
            return
        classroom = (self.st.get("classroom") or "").strip()
        if not classroom:
            self.log("先绑定课程", C_WARN)
            return
        from .ai import AI

        ai = AI(self.state.get("ai"))
        d = self.state.get("discussion") or {}
        use_ai = bool(d.get("class_ai", True))
        if use_ai and not ai.usable:
            self.log("AI 没启用或没配 Key，先去「设置 → AI 作答」配置", C_WARN)
            return

        avoid_note = bool(d.get("avoid_note", True))
        self._begin_task("cdisc", "开始完成学习讨论")
        cancel = self._task_cancel

        def work():
            from . import classdisc as cd

            try:
                with locked_session(self.cfg, self._pw_lock, headless=True, mobile=True) as s:
                    api = Api(s.ctx, self.cfg)
                    topics = cd.all_discussions(api, int(classroom), avoid_note=avoid_note)
                    if not topics:
                        self.log("这门课没有「学习讨论」类帖子")
                        return
                    self.log(f"学习讨论共 {len(topics)} 条", C_ACCENT)
                    done = skipped = failed = 0
                    for i, t in enumerate(topics, 1):
                        if cancel.is_set():
                            self.log("收到停止信号", C_WARN)
                            break
                        tid = t.get("id")
                        body = cd.html_to_text((t.get("content") or {}).get("text"))
                        self._task_prog = {"total": len(topics), "idx": i - 1,
                                           "label": f"第 {i} 条 {body[:24]}"}
                        if cd.my_comment_exists(api, tid):
                            skipped += 1
                            self.log(f"  [{i}/{len(topics)}] 已评过，跳过")
                            continue
                        try:
                            text = cd.ai_write(ai, body)
                            r = cd.publish(api, int(classroom), t, text)
                            ok, why = cd.verdict(r)

                            # 被内容审核拦了：换个写法重试，最多两次
                            tries = 0
                            while not ok and cd.is_audit_reject(why) and tries < 2:
                                tries += 1
                                if cancel.is_set():
                                    break
                                self.log(f"  [{i}/{len(topics)}] 被审核拦下，改写重试 {tries}/2", C_DIM)
                                try:
                                    text = cd.ai_write_safe(ai, body, tries=2)
                                except Exception as e:
                                    self.log(f"    重写失败：{e}", C_WARN)
                                    break
                                r = cd.publish(api, int(classroom), t, text)
                                ok, why = cd.verdict(r)

                            if ok:
                                done += 1
                                self.log(f"  [{i}/{len(topics)}] 已发表 {len(text)} 字：{text[:28]}…", C_OK)
                            else:
                                failed += 1
                                self.log(f"  [{i}/{len(topics)}] 失败：{why}", C_WARN)
                        except Exception as e:
                            failed += 1
                            self.log(f"  [{i}/{len(topics)}] 出错：{e}", C_WARN)
                    self.log(f"学习讨论完成：发 {done}，跳过 {skipped}，失败 {failed}", C_OK)
            except Exception as e:
                self.log(f"学习讨论出错：{e}", C_ERR)
            finally:
                self._task_prog = {}
                self._task_kind = ""

        self._task_thread = threading.Thread(target=work, daemon=True)
        self._task_thread.start()

    def _set_ai_base(self, text: str) -> None:
        t = (text or "").strip().rstrip("/")
        ai = self.state.setdefault("ai", {})
        if not t:
            from .ai import PRESETS

            preset = PRESETS.get(ai.get("provider") or "deepseek") or {}
            ai["base_url"] = preset.get("base_url", "")
            self.log(f"已恢复默认地址 {ai['base_url']}", C_OK)
        else:
            if not t.startswith("http"):
                t = "https://" + t
            ai["base_url"] = t
            self.log(f"服务商地址已设为 {t}", C_OK)
        sm.save_state(self.state)
        self._refresh_ai_caps()

    def _set_ai_prompt(self, text: str) -> None:
        ai = self.state.setdefault("ai", {})
        ai["extra_prompt"] = (text or "").strip()
        sm.save_state(self.state)
        if ai["extra_prompt"]:
            self.log(f"附加提示词已设置：{ai['extra_prompt'][:50]}", C_OK)
        else:
            self.log("附加提示词已清空", C_DIM)

    def _refresh_ai_caps(self) -> None:
        """换模型 / 换服务商后重新判定能力（看图等）。"""
        from . import ocr as ocr_mod
        from .ai import AI

        a = AI(self.state.get("ai"))
        self.st["ai_vision"] = a.supports_vision
        self.st["ocr_ready"] = ocr_mod.available()
        if a.supports_vision:
            self.log(f"{a.model} 能看图，含图片的题会走多模态", C_OK)
        else:
            self.log(f"{a.model} 不支持看图"
                     + ("，含图题退回 OCR" if self.st["ocr_ready"] else "，含图题只能人工看"),
                     C_DIM)

    def fetch_models(self, quiet: bool = False) -> None:
        """按公开接口拉服务商的模型列表。quiet=True 用于启动时静默预取。"""
        from .ai import AI

        ai = AI(self.state.get("ai"))
        if not quiet:
            self.log("正在拉取模型列表…", C_DIM)
        if self.ai_loading:
            return
        self.ai_loading = True

        def work():
            try:
                models = ai.list_models()
                self.ai_models = models
                if models:
                    if not quiet:
                        self.log(f"拉到 {len(models)} 个模型：{', '.join(models[:6])}"
                                 + ("…" if len(models) > 6 else ""), C_OK)
                        self.log("回到「AI 模型」上按 ←→ 就能切换", C_DIM)
                    if str(self.state["ai"].get("model") or "") not in models:
                        self.state["ai"]["model"] = models[0]
                        if not quiet:
                            self.log(f"当前模型不在列表里，先切到 {models[0]}", C_WARN)
                        sm.save_state(self.state)
                elif not quiet:
                    self.log("接口返回空列表，只能手填模型名", C_WARN)
            except Exception as e:
                if not quiet:
                    self.log(f"拉取失败：{str(e)[:150]}", C_ERR)
                    self.log("可以继续手填模型名（在「AI 模型」上按 Enter 输入）", C_DIM)
            finally:
                self.ai_loading = False

        threading.Thread(target=work, daemon=True).start()

    def act_exam(self) -> None:
        """辅助答题：先列可用考试，选一门进去；也可以直接粘链接。"""
        if self.task_running:
            return
        self.page = "examlist"
        self.cursor = 0
        if not self.exam_list and not self.exam_loading:
            self.load_exams()

    def load_exams(self) -> None:
        """后台拉「最近一周的考试」。"""
        if self.exam_loading:
            return
        self.exam_loading = True
        self.log("正在拉考试列表…", C_DIM)

        def work():
            try:
                from . import exams

                with locked_session(self.cfg, self._pw_lock, headless=True, mobile=True) as s:
                    api = Api(s.ctx, self.cfg)
                    self.exam_list = exams.fetch_exams(api)
                if self.exam_list:
                    self.log(f"找到 {len(self.exam_list)} 门可用考试", C_OK)
                else:
                    self.log("最近一周没有待考的考试（也可能都已交卷）", C_DIM)
            except Exception as e:
                self.log(f"拉考试列表失败：{e}", C_ERR)
            finally:
                self.exam_loading = False
        self._pending_admin = False

        threading.Thread(target=work, daemon=True).start()

    def handle_examlist(self, key: str) -> None:
        n = len(self.exam_list)
        if key in ("UP", "k"):
            self.cursor = max(0, self.cursor - 1)
        elif key in ("DOWN", "j"):
            self.cursor = min(max(0, n - 1), self.cursor + 1)
        elif key in ("r", "R"):
            if not self.exam_loading:
                self.exam_list = []
                self.load_exams()
        elif key in ("l", "L"):
            # 粘链接兜底
            self.ask_input("粘贴考试链接，或直接填考试 ID", self._exam_start,
                           prefill="", back="examlist")
        elif key == "ESC":
            self.page = "main"
            self.cursor = 0
        elif key == "ENTER" and 0 <= self.cursor < n:
            e = self.exam_list[self.cursor]
            self.start_exam(str(e["classroom_id"]), str(e["exam_id"]))

    def _apply_capture_protect(self) -> bool:
        """设防监视：先试当前窗口，被拒就切自建控制台再试。"""
        from . import protect

        ok, msg = protect.hide_from_capture()
        if ok:
            self.log(f"防监视已生效：{msg}", C_OK)
            return True

        self.log(f"当前窗口设不上（{msg}），切到自建控制台…", C_DIM)
        ok2, msg2 = protect.spawn_own_console()
        if not ok2:
            self.log(f"切换控制台失败：{msg2}", C_ERR)
            return False
        self.log(msg2, C_OK)
        term.clear()

        ok, msg = protect.hide_from_capture()
        if ok:
            self.log(f"防监视已生效：{msg}", C_OK)
            self.log("原终端现在是空的了，可以最小化；关掉这项会切回去", C_DIM)
            return True

        self.log(f"开启失败：{msg}", C_ERR)
        protect.attach_back()
        self.log("已切回原终端", C_DIM)
        return False

    def toggle_capture_protect(self) -> None:
        """开关「防止监视此窗口」。

        终端窗口属于 conhost，跨进程设不了防监视，所以要先 AllocConsole
        切到一个属于本进程的新控制台，再设 WDA_EXCLUDEFROMCAPTURE。
        """
        from . import protect

        u = self.state.setdefault("ui", {})
        want = not bool(u.get("hide_from_capture"))

        if not want:
            protect.show_in_capture()
            if protect.attach_back():
                self.log("已关闭防监视，切回原终端", C_DIM)
            else:
                self.log("已关闭防监视", C_DIM)
            u["hide_from_capture"] = False
            sm.save_state(self.state)
            term.clear()
            return

        if not protect.is_admin():
            self.log("这个功能需要管理员权限，正在申请提权…", C_WARN)
            self.log("当前窗口会退出，几秒后由管理员窗口接管", C_DIM)
            self._pending_admin = True
            self.alive = False
            return

        if self._apply_capture_protect():
            u["hide_from_capture"] = True
            sm.save_state(self.state)
        term.clear()

    def act_settings(self) -> None:
        self.page = "settings"
        self.set_cursor = 0

    def act_refresh(self) -> None:
        self.refresh_stats(quick=False)
        self.log("正在刷新状态…")

    def act_course(self) -> None:
        """作用域：从课程列表选。有缓存就直接用，没有才去拉。"""
        if self.task_running:
            return
        self.page = "course"
        self.cursor = 0
        cur = str(self.st.get("classroom") or "")

        if not self.course_list:
            cached = self.state.get("courses") or []
            if cached:
                self.course_list = [dict(c) for c in cached]

        for i, c in enumerate(self.course_list):
            if str(c["classroom_id"]) == cur:
                self.cursor = i
                break

        # 没有缓存才阻塞式拉；有缓存就静默刷新
        if not self.course_list and not self.course_loading:
            self.load_courses()
        elif self.course_list:
            self.load_courses(quiet=True)

    def load_courses(self, quiet: bool = False) -> None:
        """后台拉课程列表。quiet=True 时不打日志、不挡界面。"""
        if self.course_loading:
            return
        self.course_loading = True
        self.course_error = ""
        if not quiet:
            self.log("正在拉课程列表…", C_DIM)

        def work():
            try:
                from . import courses

                with locked_session(self.cfg, self._pw_lock, headless=True, mobile=True) as s:
                    api = Api(s.ctx, self.cfg)
                    got = courses.fetch_my_courses(api)
                if got:
                    self.course_list = got
                    self.state["courses"] = got
                    sm.save_state(self.state)
                    if not quiet:
                        self.log(f"拉到 {len(got)} 门课，选一门作为作用域", C_OK)
                else:
                    self.course_error = "没拉到课程"
                    if not quiet:
                        self.log("没拉到课程，可能是登录态失效", C_WARN)
            except Exception as e:
                self.course_error = str(e)[:80]
                if not quiet:
                    self.log(f"拉课程失败：{e}", C_ERR)
            finally:
                self.course_loading = False

        threading.Thread(target=work, daemon=True).start()

    def handle_course(self, key: str) -> None:
        n = len(self.course_list)
        if key in ("UP", "k"):
            self.cursor = max(0, self.cursor - 1)
        elif key in ("DOWN", "j"):
            self.cursor = min(max(0, n - 1), self.cursor + 1)
        elif key in ("r", "R"):
            if not self.course_loading:
                self.course_list = []
                self.load_courses()
        elif key == "ESC":
            self.page = "main"
            self.cursor = 0
        elif key == "ENTER" and 0 <= self.cursor < n:
            c = self.course_list[self.cursor]
            self._apply_course(str(c["classroom_id"]), c.get("course_name") or "")

    def _apply_course(self, classroom: str, name: str = "") -> None:
        self.state["classroom"] = classroom
        self.st["classroom"] = classroom
        if name:
            self.st["course_name"] = name
        sm.save_state(self.state)
        self.log(f"作用域已切到 {name or classroom}（{classroom}）", C_OK)
        self.page = "main"
        self.cursor = 0
        self.refresh_stats(quick=False)

    def act_boost_all(self) -> None:
        if not self._require_course():
            return
        self.start_boost(None)

    def act_boost_range(self) -> None:
        if not self._require_course():
            return
        self.ask_input("刷课区间（基于「未完成」列表的序号，如 1-10 或 1,3,5）",
                       lambda t: self._start_range(t, "boost"))

    def act_disc_all(self) -> None:
        if not self._require_course():
            return
        self.start_disc(None)

    def act_disc_range(self) -> None:
        if not self._require_course():
            return
        self.ask_input("发讨论区间（基于单元序号，如 1-10）",
                       lambda t: self._start_range(t, "disc"))

    def _start_range(self, text: str, kind: str) -> None:
        idx = parse_range(text, 9999)
        if idx is None:
            self.log(f"区间格式看不懂：{text}", C_ERR)
            return
        if kind == "boost":
            self.start_boost(idx)
        else:
            self.start_disc(idx)

    # ------------------------------------------------------------- 作业
    def act_hw_all(self) -> None:
        if not self._require_course():
            return
        pend = self.st.get("hw_pending") or []
        if not self.st.get("stats_ready"):
            self.log("状态还没统计完，稍等一下", C_WARN)
            return
        if not pend:
            self.log("作业全部已完成，没有待做的", C_OK)
            return
        self.start_hw([h["leaf_id"] for h in pend])

    def act_hw_range(self) -> None:
        if not self._require_course():
            return
        self.ask_input("作业区间（按待做列表序号，如 1-3 或 2,4）",
                       self._hw_range)

    def _hw_range(self, text: str) -> None:
        pend = self.st.get("hw_pending") or []
        idx = parse_range(text, len(pend))
        if idx is None:
            self.log(f"区间格式看不懂：{text}", C_ERR)
            return
        ids = [pend[i]["leaf_id"] for i in idx if 0 <= i < len(pend)]
        if not ids:
            self.log("区间里没有对应的作业", C_WARN)
            return
        self.start_hw(ids)

    def show_hw_info(self, rng: str | None) -> None:
        pend = self.st.get("hw_pending") or []
        if not pend:
            self.log("作业全部已完成", C_OK)
            return
        for i, h in enumerate(pend, 1):
            self.log(f"  {i}. {h['leaf_id']}  {truncate(h['name'], 30)}", C_DIM)

    def start_hw(self, leaf_ids: list[int]) -> None:
        """AI 作答：截图 → OCR → AI → 点选项 → 提交选单。"""
        if self.task_running:
            return
        from . import ocr as ocr_mod
        from .ai import AI
        from .homework import HomeworkRunner

        ai = AI(self.state.get("ai"))
        if not ai.usable:
            self.log("AI 没启用或没配 Key，先去「设置 → AI 作答」配置", C_WARN)
            return
        if not ocr_mod.available():
            self.log("系统没有中文 OCR 引擎（Windows 10+ 应该自带）", C_ERR)
            return

        auto = bool((self.state.get("homework") or {}).get("auto_submit"))
        show = bool((self.state.get("homework") or {}).get("show_browser"))
        classroom = self.st["classroom"]
        self._begin_task("hw", f"AI 作答 {len(leaf_ids)} 份作业" +
                         ("（自动提交）" if auto else "") +
                         ("（显示浏览器）" if show else ""))
        if show:
            self.log("注意：会弹出浏览器窗口。别去手动操作，会和自动化打架", C_WARN)
        cancel = self._task_cancel

        # 作业名字（在进度条上显示，比光秃秃的 leaf_id 好认）
        hw_names = {int(k): v for k, v in (self.st.get("hw_names") or {}).items()}

        def work():
            total = len(leaf_ids)
            for i, lid in enumerate(leaf_ids, 1):
                if cancel.is_set():
                    break
                title = hw_names.get(lid) or f"作业 {lid}"
                self._task_prog = {"total": total, "idx": i - 1, "label": title}
                if not self._pw_lock.acquire(timeout=30):
                    self.log("浏览器被占用，跳过", C_WARN)
                    continue
                runner = None
                try:
                    runner = HomeworkRunner(self.cfg, ai, classroom, lid,
                                            lambda m: self.log(str(m)),
                                            headless=not show)
                    res = runner.solve(
                        progress=lambda n, tot, lab: self._task_prog.update(
                            {"total": total, "idx": i - 1, "label": f"{title} 第{n}题"}),
                        cancel=self._task_cancel)
                    if not res.get("ok"):
                        self.log(f"{title} 失败：{res.get('reason')}", C_WARN)
                        continue

                    res["leaf_id"] = lid
                    n = len(res.get("answered") or [])
                    self.log(f"{title}：答完 {n}/{res.get('total')} 题", C_OK)

                    if auto:
                        if res.get("can_submit"):
                            okv, msg = runner.submit()
                            self.log(("提交成功：" if okv else "提交失败：") + msg,
                                     C_OK if okv else C_WARN)
                        else:
                            self.log("含主观题，自动提交已跳过", C_WARN)
                    else:
                        # 停下等用户在选单里确认
                        self._hw_pending = res
                        self._hw_choice = ""
                        self.confirm_cursor = 0
                        self._hw_gate.clear()
                        self._hw_waiting = True
                        while True:
                            self._hw_gate.wait(timeout=1800)
                            self._hw_gate.clear()
                            if self._hw_choice == "view":
                                continue
                            break
                        self._hw_waiting = False
                        if self._hw_choice == "submit":
                            okv, msg = runner.submit()
                            self.log(("提交成功：" if okv else "提交失败：") + msg,
                                     C_OK if okv else C_WARN)
                        else:
                            self.log("已取消提交", C_WARN)
                except Exception as e:
                    self.log(f"{title} 出错：{e}", C_ERR)
                finally:
                    if runner:
                        runner.close()
                    try:
                        self._pw_lock.release()
                    except Exception:
                        pass
                self._task_prog = {"total": total, "idx": i, "label": title}
            # 循环跑完（正常结束 / 取消 / 异常）都要收掉进度条，否则一直卡在进度页
            self._task_prog = {}
            self._task_kind = ""

        self._task_thread = threading.Thread(target=work, daemon=True)
        self._task_thread.start()

    def _require_course(self) -> bool:
        if not self.st.get("logged_in"):
            self.log("还没登录，先选「登录」", C_WARN)
            return False
        if not self.st.get("classroom"):
            self.log("还没绑定课程，先选「切换课程」", C_WARN)
            return False
        return True

    # ------------------------------------------------------------- 输入页
    def ask_input(self, prompt: str, action, prefill: str = "", back: str = "main") -> None:
        self.page = "input"
        self.input_prompt = prompt
        self.input_action = action
        self.input_text = prefill or ""
        self.input_back = back

    def handle_input(self, key: str) -> None:
        back = getattr(self, "input_back", "main") or "main"
        if key == "ENTER":
            text = self.input_text.strip()
            self.page = back
            self.cursor = 0
            if text:
                self.input_action(text)
            return
        if key == "ESC":
            self.page = back
            self.cursor = 0
            return
        if key == "BACKSPACE":
            self.input_text = self.input_text[:-1]
            return
        if len(key) == 1 and key.isprintable():
            self.input_text += key

    def _set_classroom(self, text: str) -> None:
        digits = "".join(ch for ch in text if ch.isdigit())
        if not digits:
            self.log(f"课程 ID 应该是数字：{text}", C_ERR)
            return
        self.state["classroom"] = digits
        self.st["classroom"] = digits
        sm.save_state(self.state)
        self.log(f"课程已设为 {digits}", C_OK)
        self.refresh_stats(quick=False)

    # ------------------------------------------------------------- 设置页
    def handle_settings(self, key: str) -> None:
        items = self.settings_items()
        if key in ("UP", "k"):
            self.set_cursor = self._step_settings(items, self.set_cursor, -1)
        elif key in ("DOWN", "j"):
            self.set_cursor = self._step_settings(items, self.set_cursor, +1)
        elif key == "ESC":
            self.page = "main"
            self.cursor = 0
        elif key in ("LEFT", "RIGHT"):
            self.adjust_setting(items[self.set_cursor][0], +1 if key == "RIGHT" else -1)
        elif key == "ENTER":
            k = items[self.set_cursor][0]
            if k == "back":
                self.page = "main"
                self.cursor = 0
            elif k in ("automation", "disc_auto", "ai_enabled", "hw_auto_submit",
                       "hw_show_browser", "class_ai", "avoid_note"):
                self.adjust_setting(k, +1)
            elif k == "ai_refresh":
                self.fetch_models()
            elif k == "ai_key":
                cur = self.state["ai"].get("api_key") or ""
                hint = f"当前 {cur[:6]}…{cur[-4:]}" if cur else "当前未配置"
                self.ask_input(f"输入 API Key（{hint}，只存 state.json，不进代码）",
                               self._set_ai_key, prefill="", back="settings")
            elif k == "ai_base":
                self.ask_input("输入服务商地址（如 https://api.deepseek.com，留空恢复默认）",
                               self._set_ai_base,
                               prefill=self.state["ai"].get("base_url", ""), back="settings")
            elif k == "ai_prompt":
                self.ask_input("附加提示词（拼在每次 system 后面；留空清除）",
                               self._set_ai_prompt,
                               prefill=self.state["ai"].get("extra_prompt", ""),
                               back="settings")
            elif k == "ai_model":
                self.ask_input("输入模型名（deepseek-chat / gpt-4o-mini / claude-3-5-sonnet-latest）",
                               self._set_ai_model,
                               prefill=self.state["ai"].get("model", ""), back="settings")
            elif k == "ai_test":
                self._test_ai()

    def _step_settings(self, items: list, cur: int, d: int) -> int:
        n = len(items)
        i = cur
        for _ in range(n):
            i = (i + d) % n
            if items[i][0] != "-":
                return i
        return cur

    def adjust_setting(self, key: str, d: int) -> None:
        a = self.state["automation"]
        g = self.state["discussion"]
        ai = self.state.setdefault("ai", {})
        if key == "automation":
            a["enabled"] = not a["enabled"]
        elif key == "rate":
            i = RATE_CHOICES.index(a["rate"]) if a["rate"] in RATE_CHOICES else 3
            a["rate"] = RATE_CHOICES[max(0, min(len(RATE_CHOICES) - 1, i + d))]
        elif key == "click_delay":
            i = DELAY_CHOICES.index(a["click_delay"]) if a["click_delay"] in DELAY_CHOICES else 3
            a["click_delay"] = DELAY_CHOICES[max(0, min(len(DELAY_CHOICES) - 1, i + d))]
        elif key == "segment":
            i = SEGMENT_CHOICES.index(a["segment"]) if a["segment"] in SEGMENT_CHOICES else 2
            a["segment"] = SEGMENT_CHOICES[max(0, min(len(SEGMENT_CHOICES) - 1, i + d))]
        elif key == "disc_auto":
            g["auto"] = not g["auto"]
        elif key == "strategy":
            i = sm.STRATEGIES.index(g["strategy"]) if g["strategy"] in sm.STRATEGIES else 0
            g["strategy"] = sm.STRATEGIES[(i + d) % len(sm.STRATEGIES)]
        elif key == "class_ai":
            g["class_ai"] = not g.get("class_ai", True)
        elif key == "avoid_note":
            g["avoid_note"] = not g.get("avoid_note", True)
        elif key == "ai_model":
            if not self.ai_models:
                self.log("还没拉过模型列表，先在「重新获取模型」上按 Enter", C_WARN)
                return
            cur = str(ai.get("model") or "")
            i = self.ai_models.index(cur) if cur in self.ai_models else -1
            ai["model"] = self.ai_models[(i + d) % len(self.ai_models)]
            self._refresh_ai_caps()
        elif key == "ai_enabled":
            ai["enabled"] = not ai.get("enabled")
        elif key == "ai_provider":
            from .ai import PRESETS

            cur = ai.get("provider", "deepseek")
            i = sm.PROVIDERS.index(cur) if cur in sm.PROVIDERS else 0
            nxt = sm.PROVIDERS[(i + d) % len(sm.PROVIDERS)]
            ai["provider"] = nxt
            preset = PRESETS.get(nxt) or {}
            if preset:
                ai["base_url"] = preset.get("base_url", "")
                ai["model"] = preset.get("model", "")
                ai["protocol"] = preset.get("protocol", "openai")
            self.ai_models = []          # 换服务商，旧列表作废
            self._refresh_ai_caps()
            self.fetch_models(quiet=True)
        elif key == "ai_temp":
            cur = float(ai.get("temperature", 0.3))
            ai["temperature"] = round(max(0.0, min(1.5, cur + 0.1 * d)), 1)
        elif key == "ai_vision":
            order = ["auto", "on", "off"]
            cur = str(ai.get("vision") or "auto")
            i = order.index(cur) if cur in order else 0
            ai["vision"] = order[(i + d) % len(order)]
        elif key == "hw_auto_submit":
            h = self.state.setdefault("homework", {})
            h["auto_submit"] = not h.get("auto_submit")
        elif key == "hw_show_browser":
            h = self.state.setdefault("homework", {})
            h["show_browser"] = not h.get("show_browser")
            if h["show_browser"]:
                self.log("已开启「显示浏览器」：跑作业时会弹出受控窗口", C_WARN)
                self.log("  如果你在窗口里手动点、滑、关页面，会和自动化互相干扰", C_WARN)
                self.log("  那会产生意料外的后果（答错题、漏题、提交内容不对）", C_WARN)
                self.log("  只想观察的话别碰鼠标键盘；不想看就关掉这项", C_DIM)
            else:
                self.log("已切回静默模式，作业在后台跑，不弹窗口", C_OK)
        elif key == "ui_out_lines":
            u = self.state.setdefault("ui", {})
            u["out_lines"] = max(8, min(30, int(u.get("out_lines", 8)) + d))
        elif key == "ui_hide":
            self.log("控制台窗口设不了防监视（SetWindowDisplayAffinity 对 conhost 无效）", C_DIM)
            self.log("要防录屏只能改成 GUI 窗口，当前版本不做，这项固定在「关」", C_DIM)
        sm.save_state(self.state)

    # ------------------------------------------------------------- AI
    def _set_ai_key(self, text: str) -> None:
        key = text.strip()
        if len(key) < 8:
            self.log("Key 太短，未保存", C_ERR)
            return
        self.state["ai"]["api_key"] = key
        self.state["ai"]["enabled"] = True
        sm.save_state(self.state)
        self.log(f"API Key 已保存（{key[:6]}…{key[-4:]}），AI 已启用", C_OK)

    def _set_ai_model(self, text: str) -> None:
        m = text.strip()
        if not m:
            return
        self.state["ai"]["model"] = m
        sm.save_state(self.state)
        self.log(f"模型已设为 {m}", C_OK)

    def _test_ai(self) -> None:
        from .ai import AI

        ai = AI(self.state.get("ai"))
        if not ai.usable:
            self.log("AI 没启用或缺 Key / 模型", C_WARN)
            return
        self.log(f"测试 {ai.provider} · {ai.model} …", C_DIM)

        def work():
            try:
                out = ai.ping()
                self.log(f"AI 连接正常：{out[:40]}", C_OK)
            except Exception as e:
                self.log(f"AI 连接失败：{e}", C_ERR)

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------- 后台：登录
    def start_login(self, force: bool = False) -> None:
        if self.task_running:
            return
        if self.st.get("logged_in") and not force:
            self.log("已经登录了。要换账号请用「重新登录」", C_OK)
            return

        # 手动登录前先丢掉外部注入的那份，别让它盖掉这次登录的结果
        try:
            injected = self.cfg.profile_dir / "login.json"
            if injected.exists():
                injected.unlink()
                self.log("已清除外部注入的登录态，这次登录结果会生效", C_DIM)
        except Exception:
            pass

        # 关键：先叫停后台统计，否则 profile 锁被占着，浏览器起不来
        busy_stat = bool(self._stat_thread and self._stat_thread.is_alive())
        self._stat_cancel.set()
        if busy_stat:
            self.log("已中断后台统计，腾出浏览器…", C_DIM)

        self.page = "main"
        self._task_kind = "login"
        self._task_cancel = threading.Event()
        self._task_prog = {"label": "准备浏览器…"}
        self.log("正在打开浏览器，" + ("已清除旧登录态，" if force else "") + "请扫码…", C_ACCENT)

        def work():
            ok = False
            try:
                with locked_session(self.cfg, self._pw_lock, timeout=25.0,
                                    headless=False, mobile=False) as s:
                    self._task_prog = {"label": "等待浏览器扫码…"}
                    if force:
                        try:
                            s.ctx.clear_cookies()
                        except Exception:
                            pass
                    page = s.page()
                    page.goto(self.cfg.base_url, wait_until="domcontentloaded", timeout=45000)
                    deadline = time.time() + 420
                    while time.time() < deadline and not self._task_cancel.is_set():
                        try:
                            r = page.evaluate(
                                """async () => { const r = await fetch('/api/v3/user/basic-info',{credentials:'include'});
                                   return await r.text(); }"""
                            )
                        except Exception:
                            r = ""
                        if r and "UNAUTHENTICATED" not in r and '"code":0' in r.replace(" ", ""):
                            ok = True
                            break
                        time.sleep(2.5)
                if ok:
                    self.log("登录成功", C_OK)
                else:
                    self.log("未检测到登录（超时或已取消）", C_WARN)
            except Exception as e:
                self.log(f"登录出错：{e}", C_ERR)
            finally:
                self._task_prog = {}
                self._task_kind = ""
            if ok:
                # 锁已释放，再起统计线程
                self.refresh_stats(quick=False)

        self._task_thread = threading.Thread(target=work, daemon=True)
        self._task_thread.start()
        self._task_done = False

    # ------------------------------------------------------------- 后台：统计
    def refresh_stats(self, quick: bool = False) -> None:
        classroom = self.st.get("classroom") or ""
        self._stat_cancel = threading.Event()
        cancel = self._stat_cancel

        def on_partial(d: dict) -> None:
            self.st.update(d)
            if d.get("stats_ready"):
                # 视频统计一出来就放开菜单；讨论在后台慢慢算
                self.st["checking"] = False

        def work():
            try:
                if not self._pw_lock.acquire(timeout=20):
                    self.log("浏览器被任务占用，统计跳过", C_WARN)
                    return
                try:
                    want_disc = bool(self.state["discussion"].get("auto_scan"))
                    res = collect_stats(self.cfg, classroom,
                                        want_disc=(not quick) and want_disc,
                                        cancel=cancel,
                                        on_partial=on_partial)
                finally:
                    self._pw_lock.release()
                self.st.update(res)
                if res.get("empty_course"):
                    self.log(f"「{res.get('course_name') or classroom}」还没有课程内容", C_WARN)
                if res.get("logged_in"):
                    self.log(f"状态已更新：登录 {res.get('uid')}，"
                             f"视频 {res.get('videos_done')}/{res.get('videos_total')}，"
                             f"作业 {res.get('hw_done')}/{res.get('hw_total')}")
            except Exception as e:
                self.log(f"统计失败：{e}", C_ERR)
            finally:
                self.st["checking"] = False

        self._stat_thread = threading.Thread(target=work, daemon=True)
        self._stat_thread.start()

    # ------------------------------------------------------------- 后台：任务
    def _posted_path(self):
        return self.cfg.out_dir / "posted.json"

    def _load_posted(self) -> set[str]:
        import json

        p = self._posted_path()
        if p.exists():
            try:
                return set(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
        return set()

    def _save_posted(self, posted: set[str]) -> None:
        import json

        try:
            self._posted_path().write_text(
                json.dumps(sorted(posted), ensure_ascii=False, indent=0), encoding="utf-8")
        except Exception:
            pass

    def start_boost(self, indices: list[int] | None) -> None:
        if self.task_running:
            return
        a = self.state["automation"]
        pace = Pace(enabled=bool(a["enabled"]), rate=float(a["rate"]),
                    click_delay=float(a["click_delay"]), segment=float(a["segment"]))
        classroom = self.st["classroom"]
        self._begin_task("boost", f"开始刷课{'' if pace.enabled else '（极速）'}")
        cancel = self._task_cancel

        def work():
            try:
                with locked_session(self.cfg, self._pw_lock, headless=True, mobile=True) as s:
                    api = Api(s.ctx, self.cfg)
                    page = s.page()          # 探测时长要用
                    disc_auto = bool(self.state["discussion"]["auto"])
                    strategy = self.state["discussion"]["strategy"]
                    posted = self._load_posted()
                    _name, leaves = fetch_course(api, classroom)
                    ids = fetch_ids(api, classroom, leaves[0].leaf_id)
                    vids = [l for l in leaves if l.kind == "video"]
                    todo = [l for l in vids if not l.done]
                    if indices is not None:
                        todo = [todo[i] for i in indices if 0 <= i < len(todo)]
                    total = len(todo)
                    self._task_prog = {"total": total, "idx": 0}
                    if total == 0:
                        self.log("没有待刷的视频", C_OK)
                        return
                    self.log(f"待刷 {total} 个视频" + (f"，节流 {pace.rate}x" if pace.enabled else ""))

                    okn = 0
                    discn = 0
                    for i, leaf in enumerate(todo, 1):
                        if cancel.is_set():
                            break
                        self._task_prog = {"total": total, "idx": i - 1, "label": leaf.name}
                        try:
                            if pace.enabled:
                                from .booster import boost_leaf_paced
                                r = boost_leaf_paced(api, ids, leaf, pace,
                                                     on_tick=lambda t: self._task_prog.update(
                                                         {"phase": "pace", **t}),
                                                     cancel=cancel)
                            else:
                                r = boost_leaf(api, ids, leaf, page=page)
                        except Exception as e:
                            r = {"ok": False, "reason": f"异常: {e}"}
                        self._task_prog = {"total": total, "idx": i, "label": leaf.name}
                        if r.get("ok"):
                            okn += 1
                            if not r.get("reason", "").startswith("已完成"):
                                self.log(f"{leaf.leaf_id} {truncate(leaf.name, 30)} 完成", C_OK)
                            # 自动讨论：刷完这一节顺手发
                            if disc_auto and not cancel.is_set():
                                try:
                                    out = run_for_leaf(api, self.cfg, ids, leaf, posted,
                                                       strategy=strategy)
                                    if out.skipped:
                                        pass
                                    elif out.ok:
                                        discn += 1
                                        self.log(f"    + 讨论（{out.source}）{truncate(out.text, 26)}")
                                    else:
                                        self.log(f"    - 讨论：{out.reason}", C_DIM)
                                    self._save_posted(posted)
                                except Exception as e:
                                    self.log(f"    - 讨论异常：{e}", C_DIM)
                        else:
                            self.log(f"{leaf.leaf_id} {truncate(leaf.name, 30)} 失败：{r.get('reason')}", C_WARN)
                    tail = f"，顺带发讨论 {discn} 条" if disc_auto else ""
                    self.log(f"刷课结束：{okn}/{total} 成功{tail}",
                             C_OK if okn == total else C_WARN)
            except Exception as e:
                self.log(f"刷课出错：{e}", C_ERR)
            finally:
                self._task_prog = {}
                self._task_kind = ""
            try:
                self.refresh_stats(quick=False)
            except Exception:
                pass

        self._task_thread = threading.Thread(target=work, daemon=True)
        self._task_thread.start()

    def start_disc(self, indices: list[int] | None) -> None:
        if self.task_running:
            return
        classroom = self.st["classroom"]
        strategy = self.state["discussion"]["strategy"]
        self._begin_task("disc", "开始发讨论")

        posted = self._load_posted()
        cancel = self._task_cancel

        def work():
            try:
                with locked_session(self.cfg, self._pw_lock, headless=True, mobile=True) as s:
                    api = Api(s.ctx, self.cfg)
                    _name, leaves = fetch_course(api, classroom)
                    ids = fetch_ids(api, classroom, leaves[0].leaf_id)
                    targets = [l for l in leaves if l.kind != "quiz"]
                    if indices is not None:
                        targets = [targets[i] for i in indices if 0 <= i < len(targets)]
                    total = len(targets)
                    self._task_prog = {"total": total, "idx": 0}
                    self.log(f"目标 {total} 个单元，策略：{sm.STRATEGY_NAMES.get(strategy, strategy)}")

                    okn = skip = 0
                    for i, leaf in enumerate(targets, 1):
                        if cancel.is_set():
                            break
                        self._task_prog = {"total": total, "idx": i - 1, "label": leaf.name}
                        try:
                            out = run_for_leaf(api, self.cfg, ids, leaf, posted,
                                               strategy=strategy)
                        except Exception as e:
                            out = None
                            self.log(f"{leaf.leaf_id} 异常：{e}", C_ERR)
                        self._task_prog = {"total": total, "idx": i, "label": leaf.name}
                        if out is None:
                            continue
                        if out.skipped:
                            skip += 1
                        elif out.ok:
                            okn += 1
                            self.log(f"{truncate(leaf.name, 26)} ← {out.source}：{truncate(out.text, 34)}")
                        else:
                            self.log(f"{truncate(leaf.name, 26)} 失败：{out.reason}", C_WARN)
                        try:
                            self._save_posted(posted)
                        except Exception:
                            pass
                        time.sleep(1.2)
                    self.log(f"讨论结束：新发 {okn}，跳过 {skip}，共 {total}", C_OK)
            except Exception as e:
                self.log(f"讨论出错：{e}", C_ERR)
            finally:
                self._task_prog = {}
                self._task_kind = ""
            try:
                self.refresh_stats(quick=False)
            except Exception:
                pass

        self._task_thread = threading.Thread(target=work, daemon=True)
        self._task_thread.start()

    def _begin_task(self, kind: str, msg: str) -> None:
        # 先叫停统计线程，释放 profile 锁
        self._stat_cancel.set()
        self._task_cancel = threading.Event()
        self._task_kind = kind
        self._task_done = False
        self.st["checking"] = False
        self.log(msg, C_ACCENT)


def main() -> int:
    return App().run()


if __name__ == "__main__":
    raise SystemExit(main())
