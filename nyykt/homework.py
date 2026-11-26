"""作业自动作答。

链路：封面页 → 点「开始作答」→ 展开答题卡跳题 → 逐题
      （截图 → Windows OCR → AI → 点选项）→ 等你确认 → 提交
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

from . import ocr as ocr_mod
from .ai import AI, AIError, parse_json_loose
from .browser import Session, goto

PROBLEM_SEL = ".container-problem"
BODY_SEL = ".problem-body"
TITLE_SEL = ".pro-title"
# 答题卡题号。注意：第 1 题自带 primary 类，别把它过滤掉
CARD_ITEM = ".problem-list .subject-item"

# 出现这些字样就不是纯客观题，不允许直接提交
SUBJECTIVE_KEYS = ("填空", "简答", "论述", "问答", "主观", "作文", "分析", "计算", "名词解释")

_BASE = "https://changjiang.yuketang.cn"


def cfg_base() -> str:
    try:
        from .config import load

        return load().base_url or _BASE
    except Exception:
        return _BASE


def cover_url(classroom: str, leaf_id: int) -> str:
    return f"{cfg_base()}/ai-workspace/lms-graph-mobile/{classroom}/exercise/{leaf_id}?is_chapter=1"


def is_subjective(title: str) -> bool:
    t = title or ""
    return any(k in t for k in SUBJECTIVE_KEYS)


def find_exam_frame(page):
    for f in page.frames:
        try:
            if f.evaluate(f"() => !!document.querySelector('{PROBLEM_SEL}')"):
                return f
        except Exception:
            continue
    return None


def _ensure_card_open(page, frame, tries: int = 4) -> bool:
    """保证答题卡展开。点完一题它会自动收起，必须重新点开。"""
    for _ in range(tries):
        if frame.evaluate("() => !!document.querySelector('.homework-aside-open')"):
            return True
        opened = False
        for sel in (".homework-aside-close", ".homework-aside", ".aside-header"):
            try:
                loc = frame.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=2500, force=True)
                    opened = True
                    break
            except Exception:
                continue
        if not opened:
            break
        page.wait_for_timeout(1300)
        if frame.evaluate(f"() => document.querySelectorAll('{CARD_ITEM}').length > 0"):
            return True
    return bool(frame.evaluate(f"() => document.querySelectorAll('{CARD_ITEM}').length > 0"))


def open_card(page, frame) -> int:
    try:
        if not _ensure_card_open(page, frame):
            return 0
        txt = frame.evaluate(
            """() => {
                const a = document.querySelector('.homework-aside-open') ||
                          document.querySelector('.homework-aside');
                return a ? a.innerText : '';
            }"""
        ) or ""
        for pat in (r"共\s*(\d+)\s*分", r"(\d+)\s*题", r"/\s*(\d+)\b"):
            m = re.search(pat, txt)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return 0


def fingerprint(info: dict) -> str:
    return (info.get("bodyText") or "")[:40] + "|" + ",".join(info.get("opts") or [])


def _ensure_card_closed(page, frame, tries: int = 3) -> bool:
    """收起答题卡。展开后是全屏覆盖，唯一的出口是「返回作业」。"""
    for _ in range(tries):
        if not frame.evaluate("() => !!document.querySelector('.homework-aside-open')"):
            return True
        clicked = False
        for label in ("返回作业",):
            try:
                loc = frame.get_by_text(label, exact=True)
                for i in range(min(loc.count(), 3)):
                    el = loc.nth(i)
                    try:
                        if el.is_visible():
                            el.click(timeout=2500, force=True)
                            clicked = True
                            break
                    except Exception:
                        continue
            except Exception:
                pass
            if clicked:
                break
        if not clicked:
            break
        page.wait_for_timeout(800)
    return not frame.evaluate("() => !!document.querySelector('.homework-aside-open')")


ARROW_LEFT = ".problem-fixedbar .el-icon-arrow-left"
ARROW_RIGHT = ".problem-fixedbar .el-icon-arrow-right"


def current_q(frame) -> int:
    """当前题号。从标题 "3.多选题" 里解析，最靠得住。"""
    try:
        t = frame.evaluate(
            f"""() => {{
                const e = document.querySelector('{PROBLEM_SEL} {TITLE_SEL}');
                return e ? (e.innerText || '').trim() : '';
            }}"""
        ) or ""
        m = re.match(r"\s*(\d+)", t)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


def total_questions(frame) -> int:
    """题目总数。答题卡收着的时候上面就有 "答题卡 1/7"。"""
    try:
        txt = frame.evaluate(
            """() => {
                const a = document.querySelector('.homework-aside, .homework-aside-open');
                return a ? a.innerText : '';
            }"""
        ) or ""
        for pat in (r"/\s*(\d+)", r"共\s*(\d+)\s*分", r"(\d+)\s*题"):
            m = re.search(pat, txt)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return 0


def wait_until(page, cond, timeout_ms: int = 4000, step: int = 120) -> bool:
    """轮询等条件成立，成立就立刻返回——别再死等固定毫秒。"""
    rounds = max(1, timeout_ms // step)
    for _ in range(rounds):
        try:
            if cond():
                return True
        except Exception:
            pass
        try:
            page.wait_for_timeout(step)
        except Exception:
            return False
    return False


def submit_current(frame, page) -> bool:
    """切题时弹出的「刚刚答完的习题还没有提交，需要提交吗？」→ 点「帮我提交」。

    **必须点「帮我提交」**：点「先不用了」等于把这道题的答案丢掉，不计成绩。
    点完立刻等弹窗消失，不等固定时间。
    """
    for _attempt in range(3):
        for label in ("帮我提交", "全部提交"):
            try:
                loc = frame.get_by_text(label, exact=True)
                if not loc.count():
                    continue
                for i in range(min(loc.count(), 3)):
                    el = loc.nth(i)
                    try:
                        if el.is_visible():
                            el.click(timeout=2500)
                            # 弹窗一消失就走，最多等 3 秒
                            wait_until(
                                page,
                                lambda: not any(
                                    frame.get_by_text(t, exact=True).count()
                                    and frame.get_by_text(t, exact=True).first.is_visible()
                                    for t in ("帮我提交", "全部提交")
                                ),
                                timeout_ms=3000,
                            )
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        page.wait_for_timeout(300)
    return False


def tidy_before_advance(frame, page) -> None:
    """切题前收拾页面。

    答错后页面会展开解析把底部切题栏挤出视口，Playwright 点不到就一直等到超时
    （这就是"答错后卡十秒"的原因）。这里先收起解析，再把按钮滚进视野。
    """
    for label in ("收起解析",):
        try:
            loc = frame.get_by_text(label, exact=True)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=1500)
                page.wait_for_timeout(400)
                break
        except Exception:
            pass
    for sel in (ARROW_RIGHT, ARROW_LEFT):
        try:
            frame.locator(sel).first.scroll_into_view_if_needed(timeout=1500)
            break
        except Exception:
            pass


def goto_q(page, frame, n: int, max_steps: int = 30) -> bool:
    """用底部左右箭头走到第 n 题。每步都核对题号，走不动就返回 False。"""
    for _ in range(max_steps):
        cur = current_q(frame)
        if cur == n:
            return True
        if cur == 0:
            return False
        tidy_before_advance(frame, page)
        sel = ARROW_RIGHT if n > cur else ARROW_LEFT
        moved = False
        for _attempt in range(3):
            ok = False
            # 底栏 fixed 定位，locator.click 的命中检测经常误判，直接按坐标点
            try:
                b = frame.locator(sel).first.bounding_box()
                if b:
                    page.mouse.click(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
                    ok = True
            except Exception:
                pass
            if not ok:
                try:
                    frame.locator(sel).first.click(timeout=2500, force=True)
                    ok = True
                except Exception:
                    pass
            if not ok:
                page.wait_for_timeout(900)
                continue
            # 切题会撞上「刚刚答完还没提交」的提示，点「帮我提交」把这一题交上去
            submit_current(frame, page)
            # 题号一变就走，最多等 5 秒，别死等
            moved = wait_until(page, lambda: current_q(frame) != cur, timeout_ms=5000, step=150)
            if not moved:
                submit_current(frame, page)
                moved = wait_until(page, lambda: current_q(frame) != cur, timeout_ms=2500, step=150)
            if moved:
                break
        if not moved:
            return False
    return False



def read_problem(frame) -> dict:
    try:
        return frame.evaluate(
            f"""() => {{
                const q = document.querySelector('{PROBLEM_SEL}');
                if (!q) return null;
                const title = (q.querySelector('{TITLE_SEL}') || {{}}).innerText || '';
                const body = q.querySelector('{BODY_SEL}');
                const bodyText = body ? body.innerText : '';
                const inputs = [...q.querySelectorAll('input')];
                const opts = inputs.map(e => e.value);
                // 已经勾上的选项：做过这题时页面会把它们标成选中
                const checked = inputs
                    .filter(e => e.checked || (e.closest('label') || {{}}).className
                                 && e.closest('label').className.toString().includes('is-checked'))
                    .map(e => e.value);
                const editable = q.querySelectorAll('textarea,[contenteditable=true]');
                const filledText = [...editable].some(e => (e.value || e.innerText || '').trim());
                return {{ title: title.trim(), bodyText, opts, checked, editable: editable.length, filledText }};
            }}"""
        ) or {}
    except Exception:
        return {}


def shoot_problem(frame, path: str) -> bool:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        frame.locator(PROBLEM_SEL).first.screenshot(path=path, timeout=15000)
        return True
    except Exception:
        return False


def click_options(frame, letters: list[str], page=None) -> int:
    """勾选选项：先连着点一遍，再统一校验补点。

    不再每个选项都死等 —— 那样一道四选能拖十几秒。
    """
    if not letters:
        return 0

    def checked(L: str) -> bool:
        try:
            inp = frame.locator(f"{PROBLEM_SEL} input[value='{L}']")
            return bool(inp.count()) and bool(inp.first.is_checked())
        except Exception:
            return False

    def click_one(L: str) -> None:
        try:
            inp = frame.locator(f"{PROBLEM_SEL} input[value='{L}']").first
            lab = frame.locator(f"{PROBLEM_SEL} label").filter(has=inp)
            if lab.count():
                lab.first.click(timeout=1500)
                return
        except Exception:
            pass
        try:
            frame.locator(f"{PROBLEM_SEL} input[value='{L}']").first.click(timeout=1500)
            return
        except Exception:
            pass
        try:
            frame.evaluate(
                """(L) => {
                    const i = document.querySelector(`.container-problem input[value="${L}"]`);
                    if (!i) return false;
                    const lab = i.closest('label') || i.parentElement;
                    for (const t of ['pointerdown','mousedown','mouseup','click']) {
                        lab.dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true, view:window}));
                    }
                    return i.checked;
                }""",
                L,
            )
        except Exception:
            pass

    # 第一轮：连着点，但每下之间留一点点时间给 Vue 反应
    todo = [L for L in letters if not checked(L)]
    for L in todo:
        click_one(L)
        if page:
            page.wait_for_timeout(150)
    if page and todo:
        page.wait_for_timeout(450)

    # 第二轮：只补没勾上的
    for L in letters:
        if checked(L):
            continue
        for _ in range(2):
            click_one(L)
            if page:
                page.wait_for_timeout(400)
            if checked(L):
                break

    return sum(1 for L in letters if checked(L))


def entry_submit(page, frame) -> tuple[bool, str]:
    """点「提交」→ 弹窗里点「帮我提交」。

    注意：页面上的「提交」外层是 <button>，里面那个是 <span>，
    点 span 不会触发事件，必须点 button。
    """
    if frame is None:
        return False, "没有作答页"
    try:
        # 逐题「帮我提交」生效后，底部按钮会变成「已提交」
        done = frame.locator("button").filter(has_text="已提交")
        if done.count():
            try:
                if done.first.is_visible():
                    return True, "已提交（作答时逐题提交已生效）"
            except Exception:
                pass

        btn = frame.locator("button").filter(has_text="提交").first
        if not btn.count():
            return False, "找不到提交按钮"
        if btn.is_disabled():
            return False, "提交按钮是灰的——还有题没答完"
        btn.click(timeout=4000)
        # 弹确认框就立刻处理，没有就短暂等一下就往下走
        wait_until(
            page,
            lambda: any(frame.get_by_text(t, exact=True).count()
                        and frame.get_by_text(t, exact=True).first.is_visible()
                        for t in ("帮我提交", "全部提交", "确定")),
            timeout_ms=1500, step=100,
        )

        for label in ("帮我提交", "全部提交", "确定"):
            for loc in (frame.locator("button").filter(has_text=label),
                        frame.get_by_text(label, exact=True)):
                try:
                    if not loc.count():
                        continue
                    for i in range(min(loc.count(), 4)):
                        el = loc.nth(i)
                        if el.is_visible():
                            el.click(timeout=3500)
                            wait_until(
                                page,
                                lambda: not any(frame.get_by_text(t, exact=True).count()
                                                and frame.get_by_text(t, exact=True).first.is_visible()
                                                for t in ("帮我提交", "全部提交", "确定")),
                                timeout_ms=2500,
                            )
                            return True, f"已提交（点了「{label}」）"
                except Exception:
                    continue
        # 没有二次确认弹窗，说明点「提交」就直接交上去了
        done = frame.locator("button").filter(has_text="已提交")
        try:
            if done.count() and done.first.is_visible():
                return True, "已提交"
        except Exception:
            pass
        return True, "已点提交（这一步没有二次确认）"
    except Exception as e:
        return False, f"提交异常：{e}"


class HomeworkRunner:
    """一份作业的完整会话：进页面 → 答题 → 等你确认 → 提交 → 关闭。

    所有方法必须在同一个线程里调用（Playwright sync API 有线程亲和）。

    manual_advance=True 时不自己切题（页面的箭头/答题卡在无头环境里点不动），
    改成等你手动切题、按 Enter 放行。
    """

    def __init__(self, cfg, ai: AI, classroom: str, leaf_id: int, log,
                 headless: bool = True, manual_advance: bool = False):
        self.cfg = cfg
        self.ai = ai
        self.classroom = classroom
        self.leaf_id = leaf_id
        self.log = log
        self.headless = headless
        self.manual_advance = manual_advance
        self.session: Session | None = None
        self.page = None
        self.frame = None
        self.results: list[dict] = []
        self.total = 0
        self.advance_event: threading.Event | None = None
        self.on_wait_advance = None      # 回调：告诉 UI "该你了"

    # ------------------------------------------------------------- 进入
    def enter(self) -> bool:
        self.session = Session(self.cfg)
        self.session.start(headless=self.headless, mobile=True)
        self.page = self.session.page()
        goto(self.page, cover_url(self.classroom, self.leaf_id))
        self.page.wait_for_timeout(4500)

        # 按钮文案会随状态变：没开始是「开始作答」，中途退出过是「继续作答」
        LABELS = ("开始作答", "继续作答", "继续答题", "开始答题", "进入作业", "继续学习")
        clicked = ""
        for label in LABELS:
            for f in self.page.frames:
                try:
                    loc = f.get_by_text(label, exact=True)
                    if not loc.count():
                        continue
                    for i in range(min(loc.count(), 3)):
                        el = loc.nth(i)
                        try:
                            if el.is_visible():
                                el.click(timeout=5000)
                                clicked = label
                                break
                        except Exception:
                            continue
                except Exception:
                    continue
                if clicked:
                    break
            if clicked:
                break

        if not clicked:
            self.page.screenshot(path=str(self.cfg.out_dir / f"hw-{self.leaf_id}-enter.png"),
                                 full_page=True)
            self.log("找不到开始/继续作答按钮（截图已存 out/）")
            return False
        self.log(f"点了「{clicked}」")

        for _ in range(14):
            self.page.wait_for_timeout(1500)
            self.frame = find_exam_frame(self.page)
            if self.frame:
                return True
        self.log("点了按钮，题目没出来")
        return False

    # ------------------------------------------------------------- 答题
    def solve(self, progress=None, dry_run: bool = False, max_q: int = 30,
              cancel=None) -> dict:
        shot_dir = self.cfg.out_dir / f"hw-{self.leaf_id}"
        shot_dir.mkdir(parents=True, exist_ok=True)

        if not ocr_mod.available():
            return {"ok": False, "reason": "系统没有可用的中文 OCR 引擎"}
        if not self.ai.usable:
            return {"ok": False, "reason": "AI 没启用或没配 Key"}

        try:
            if not self.enter():
                return {"ok": False, "reason": "进不去作答页"}
            frame = self.frame
            total = total_questions(frame) or 1
            self.total = total
            self.log(f"答题卡：共 {total} 题")

            for n in range(1, min(total, max_q) + 1):
                if cancel is not None and cancel.is_set():
                    self.log("收到停止信号，停止作答")
                    break
                if self.manual_advance:
                    if n > 1:
                        # 等你手动切到下一题
                        if self.on_wait_advance:
                            self.on_wait_advance(n, total)
                        if self.advance_event is not None:
                            self.advance_event.wait(timeout=1800)
                            self.advance_event.clear()
                    actual = current_q(frame)
                    if actual == 0:
                        self.log("读不到当前题号，停下")
                        break
                    n = actual
                else:
                    if not goto_q(self.page, frame, n):
                        self.log(f"跳不到第 {n} 题，停止")
                        break
                # 切题后确认真的停在第 n 题，到位就走
                if not wait_until(self.page, lambda: current_q(frame) == n, timeout_ms=3500):
                    self.log(f"第 {n} 题没切到位（当前 {current_q(frame)}），跳过")
                    continue
                info = read_problem(frame)
                if not info or not info.get("opts"):
                    self.log(f"第 {n} 题读不出选项，跳过")
                    continue
                # 已经做过的题别再碰 —— 重选会把原来的勾选改掉
                already = info.get("checked") or []
                if already or info.get("filledText"):
                    tip = "、".join(already) if already else "已填写"
                    self.log(f"第 {n} 题已经做过（{tip}），跳过")
                    continue
                if progress:
                    progress(n, total, f"第 {n} 题")

                path = shot_dir / f"q{n}.png"
                if not shoot_problem(frame, str(path)):
                    self.log(f"第 {n} 题截图失败")
                    continue
                text = ocr_mod.ocr_png(str(path))
                if not text.strip():
                    self.log(f"第 {n} 题 OCR 为空")
                    continue
                try:
                    js = ai_answer(self.ai, text)
                except Exception as e:
                    self.log(f"第 {n} 题 AI 失败：{e}")
                    continue
                letters = js.get("letters") or []
                clicked = click_options(frame, letters, self.page) if not dry_run else 0
                if not dry_run and letters and clicked < len(letters):
                    self.log(f"    警告：{len(letters)} 个选项只勾上 {clicked} 个")
                title = info.get("title") or ""
                self.results.append({
                    "index": n,
                    "title": title,
                    "type": js.get("type"),
                    "subjective": is_subjective(title) or bool(info.get("editable")),
                    "body": js.get("body"),
                    "answer": ",".join(letters),
                    "reason": js.get("reason"),
                    "clicked": clicked,
                    "options": info.get("opts"),
                    "ocr": text[:800],
                })
                flag = " [主观题]" if self.results[-1]["subjective"] else ""
                self.log(f"  第 {n} 题 → {','.join(letters) or '?'}{flag}  {str(js.get('body'))[:40]}")

            # 最后一题答完后没有「切题」动作，所以它的「帮我提交」不会自己弹，
            # 这里补一次收尾提交，否则整份会差一题。
            if not dry_run and self.results:
                try:
                    self.page.wait_for_timeout(900)
                    okv, msg = entry_submit(self.page, frame)
                    self.log(f"收尾提交：{msg}")
                except Exception as e:
                    self.log(f"收尾提交失败：{e}")

            p = self.cfg.out_dir / f"hw-{self.leaf_id}-answers.json"
            p.write_text(json.dumps(self.results, ensure_ascii=False, indent=2), encoding="utf-8")
            self.log(f"答案已存 {p}")

            subjective = [r["index"] for r in self.results if r["subjective"]]
            return {
                "ok": True,
                "answered": self.results,
                "file": str(p),
                "shots": str(shot_dir),
                "total": total,
                "subjective": subjective,
                "can_submit": not subjective,
            }
        except Exception as e:
            return {"ok": False, "reason": f"作答异常：{e}"}

    # ------------------------------------------------------------- 提交
    def submit(self) -> tuple[bool, str]:
        bad = [r["index"] for r in self.results if r["subjective"]]
        if bad:
            return False, f"第 {','.join(map(str, bad))} 题是主观题，先手动补完再提交"
        answered = len(self.results)
        if answered == 0:
            return False, "一题都没答，没什么可交的"
        if self.page is None or self.frame is None:
            return False, "会话已关闭"
        return entry_submit(self.page, self.frame)

    def close(self) -> None:
        try:
            if self.session:
                self.session.close()
        except Exception:
            pass
        self.session = None
        self.page = None
        self.frame = None


def ai_answer(ai: AI, text: str) -> dict:
    """把 OCR 文本交给 AI，返回 {letters, body, type, reason}。

    多选题少选不得分，所以题面里带「多选」时额外提醒模型逐项判断。
    """
    is_multi = "多选" in (text or "")
    extra = (
        "\n注意：这是多选题，通常有 2 到 4 个正确选项，只有一个正确的情况很少见。"
        "请逐项判断每个选项是否成立，不要只挑最明显的那一个。\n"
        if is_multi else ""
    )
    prompt = f"""下面是一道大学课程的作业题（截图 OCR 结果，可能有少量识别错字，请结合语义理解）：

{text[:2000]}
{extra}
请解析题目并作答。只输出 JSON：
{{"type":"单选题/多选题/判断题/填空题/简答题","body":"题干","options":{{"A":"...","B":"..."}},"answer":"A,C","reason":"一句话理由"}}
多选题答案按字母序逗号分隔；填空/简答直接用文字作答。"""
    raw = ai.chat("你是答题助手。", prompt, max_tokens=1200, timeout=120, json_mode=True)
    js = parse_json_loose(raw, keys=("answer", "reason", "type", "body", "options"))
    if js is None:
        raise AIError(f"AI 没给出可用答案：{raw[:200]}")
    ans = js.get("answer") or ""
    letters = [x.strip().upper() for x in re.split(r"[,\s，、]+", str(ans)) if x.strip()]
    letters = [x for x in letters if re.fullmatch(r"[A-H]", x)]
    js["letters"] = sorted(set(letters))
    return js
