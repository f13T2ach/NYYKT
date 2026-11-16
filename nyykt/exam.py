"""考试辅助答题。

原则：**只读，不操作浏览器**。
  - 可以打开考试页、点「继续答题」进入（这是导航）
  - 不点选项、不填答案、不提交，用户自己作答
  - 工具只负责读题 + AI 给答案 + 显示思考过程

考试页的字**没有**字体混淆（跟作业不一样），DOM 里是明文，
所以不需要截图 OCR，直接读文本。
"""

from __future__ import annotations

import json
import re
import time

from .ai import AI, AIError
from .browser import Session, goto

SEL_ITEM = ".subject-item"

# 题型归类
KIND_NAMES = {
    "single": "单选题",
    "multi": "多选题",
    "judge": "判断题",
    "blank": "填空题",
    "essay": "主观题",
    "vote": "投票题",
    "other": "其他",
}


def kind_of(type_text: str) -> str:
    t = type_text or ""
    if "单选" in t:
        return "single"
    if "多选" in t:
        return "multi"
    if "判断" in t:
        return "judge"
    if "填空" in t:
        return "blank"
    if "投票" in t:
        return "vote"
    if any(k in t for k in ("简答", "论述", "问答", "主观", "分析", "计算", "名词解释")):
        return "essay"
    return "other"


def parse_type(type_text: str) -> tuple[int, str, float]:
    """从 "1.单选题 (1分)" 里抠出 (题号, 题型, 分值)。"""
    t = (type_text or "").replace("\n", " ")
    m = re.search(r"(\d+)\s*[.、]", t)
    no = int(m.group(1)) if m else 0
    m2 = re.search(r"[（(]\s*([\d.]+)\s*分\s*[)）]", t)
    score = float(m2.group(1)) if m2 else 0.0
    kind = kind_of(t)
    return no, kind, score


READ_JS = """
() => {
    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();

    // 把 MathJax 渲染结果换回原始 LaTeX：源码就藏在 .kfformula-latex > script 里
    const grab = (root) => {
        if (!root) return '';
        const c = root.cloneNode(true);
        for (const el of c.querySelectorAll('.kfformula-latex')) {
            const tex = ((el.querySelector('script') || {}).textContent || '').trim();
            el.replaceWith(document.createTextNode(tex ? ` $${tex}$ ` : ' '));
        }
        for (const p of c.querySelectorAll('p, br, div, li')) {
            p.appendChild(document.createTextNode('\\n'));
        }
        return (c.textContent || '')
            .replace(/[ \\t]+/g, ' ')
            .replace(/\\n{2,}/g, '\\n')
            .trim();
    };

    const items = [...document.querySelectorAll('.subject-item')];
    return items.map((el, i) => {
        const typeEl = el.querySelector('.item-type');
        const typeText = norm(typeEl ? typeEl.innerText : '');
        const bodyEl = el.querySelector('.item-body h4 .custom_ueditor_cn_body')
                    || el.querySelector('.item-body h4')
                    || el.querySelector('.item-body');
        const body = grab(bodyEl);

        // 题干里的公式源码（LaTeX）
        const tex = bodyEl ? [...bodyEl.querySelectorAll('.kfformula-latex script')]
            .map(s => (s.textContent || '').trim())
            .filter(Boolean) : [];

        // 图片（公式截图、图表、图形）—— 这些才走多模态
        const imgs = bodyEl ? [...bodyEl.querySelectorAll('img')].map(im => ({
            src: (im.src || '').slice(0, 300),
            alt: norm(im.alt || ''),
        })).filter(x => x.src) : [];

        // 表格转成竖线文本
        const tables = bodyEl ? [...bodyEl.querySelectorAll('table')].map(tb => {
            return [...tb.querySelectorAll('tr')].map(tr =>
                [...tr.querySelectorAll('th,td')].map(td => grab(td)).join(' | ')
            ).filter(Boolean).join('\\n');
        }).filter(Boolean) : [];

        const opts = [...el.querySelectorAll('.el-radio, .el-checkbox')].map(l => {
            const txt = l.querySelector('.radioText, .checkboxText');
            return {
                key: norm((l.querySelector('.radioInput, .checkboxInput') || {}).innerText),
                text: grab(txt),
                checked: l.className.toString().includes('is-checked'),
                imgs: [...l.querySelectorAll('img')].length,
            };
        }).filter(o => o.key);

        const blanks = [...el.querySelectorAll('textarea, input[type=text], .blank-input')]
            .map(e => e.value || '');
        const saved = /答案保存成功|已保存/.test(typeText)
                   || !!el.querySelector('.status.success');
        return {index: i + 1, domId: el.id || '', typeText, body, opts, blanks, saved,
                imgs, tex, tables};
    });
}
"""


def read_problems(frame) -> list[dict]:
    """一次读完页面上所有题目（考试页是整卷渲染，不用翻页）。"""
    try:
        raw = frame.evaluate(READ_JS) or []
    except Exception:
        return []
    out = []
    for r in raw:
        no, kind, score = parse_type(r.get("typeText", ""))
        opts = r.get("opts") or []
        out.append({
            "index": r.get("index", 0),
            "no": no or r.get("index", 0),
            "domId": r.get("domId", ""),
            "kind": kind,
            "kindName": KIND_NAMES.get(kind, "其他"),
            "score": score,
            "typeText": r.get("typeText", ""),
            "body": r.get("body", ""),
            "opts": opts,
            "blanks": r.get("blanks") or [],
            "saved": bool(r.get("saved")),
            "imgs": r.get("imgs") or [],
            "tex": r.get("tex") or [],
            "tables": r.get("tables") or [],
            "optImgs": sum(int(o.get("imgs") or 0) for o in opts),
        })
    return out


SYSTEM = (
    "你是考试辅助助手。你要读懂题目，给出可直接填写的答案。"
    "把推理过程写清楚，让考生能自己判断。只输出 JSON。"
)


def build_prompt(p: dict, exam_title: str = "") -> str:
    lines = []
    if exam_title:
        lines.append(f"【{exam_title}】")
    lines.append(f"题型：{p['kindName']}")
    lines.append(f"题干：{p['body']}")

    if p.get("tex"):
        lines.append("题干里的公式（LaTeX 源码）：" + " ; ".join(p["tex"][:12]))
    if p.get("tables"):
        lines.append("题干表格：")
        for t in p["tables"][:3]:
            lines.append(t)
    n_img = len(p.get("imgs") or []) + int(p.get("optImgs") or 0)
    if p.get("vision_b64"):
        lines.append("上面附了一张这道题的截图，请直接看图（公式、分式、上下限、图形都在图里）。")
    elif p.get("ocr"):
        lines.append("图片里识别出的文字（OCR，可能有错字）：")
        lines.append(p["ocr"][:900])
    elif n_img:
        lines.append(
            f"注意：题目里有 {n_img} 张图片，但你收不到图。请依据文字推断，"
            f"关键信息在图片里就在 reason 里说明「需自行看图」。"
        )

    if p["opts"]:
        lines.append("选项：")
        for o in p["opts"]:
            lines.append(f"  {o['key']}. {o['text']}")
    if p["kind"] == "blank":
        n = max(1, len(p["blanks"]))
        lines.append(f"这是一道填空题，共 {n} 个空。")
    if p["kind"] == "essay":
        lines.append("这是主观题，请给出可以直接抄写的完整答案，必要时写出计算过程。")
    if p["kind"] == "vote":
        lines.append("这是投票题，通常没有标准答案，说明建议选哪项及理由即可。")

    lines.append("""
请只输出 JSON：
{"answer": "单选题填一个字母；多选题填多个字母逗号分隔；判断题填 对/错；填空题按顺序用 | 分隔各空；主观题填完整答案文字",
 "reason": "你的推理过程，分点写清楚为什么这么答，不超过 200 字"}
""")
    return "\n".join(lines)


def ai_solve(ai: AI, p: dict, images: list[str] | None = None, timeout: int = 120,
             exam_title: str = "") -> dict:
    """让 AI 答一道题，返回 {answer, reason}。"""
    raw = ai.chat(SYSTEM, build_prompt(p, exam_title), max_tokens=1200, timeout=timeout,
                  images=images)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise AIError(f"AI 没返回 JSON：{raw[:160]}")
    body = m.group(0)
    js = None
    for cand in (
        body,
        re.sub(r"[\r\n]+", " ", body),
        re.sub(r",\s*([}\]])", r"\1", body),
        re.sub(r"[\r\n]+", " ", re.sub(r",\s*([}\]])", r"\1", body)),
    ):
        try:
            js = json.loads(cand)
            break
        except Exception:
            continue
    if js is None:
        raise AIError(f"JSON 解析失败：{body[:160]}")

    ans = str(js.get("answer") or "").strip()
    if p["kind"] in ("single", "multi"):
        letters = [x.strip().upper() for x in re.split(r"[,\s，、]+", ans) if x.strip()]
        letters = [x for x in letters if re.fullmatch(r"[A-H]", x)]
        ans = ",".join(sorted(set(letters)))
    if p["kind"] == "judge":
        if any(k in ans for k in ("对", "正确", "√", "T", "true")):
            ans = "对"
        elif any(k in ans for k in ("错", "错误", "×", "F", "false")):
            ans = "错"
    return {"answer": ans, "reason": str(js.get("reason") or "").strip()}


class ExamRunner:
    """一份考试的只读会话。

    所有方法要在同一个线程里调用（Playwright sync 有线程亲和）。
    """

    def __init__(self, cfg, classroom: str, exam_id: str, log, headless: bool = True):
        self.cfg = cfg
        self.classroom = classroom
        self.exam_id = exam_id
        self.log = log
        self.headless = headless
        self.session: Session | None = None
        self.page = None
        self.frame = None
        self.title = ""

    def open_cover(self) -> None:
        """打开考试封面页。**不点任何按钮** —— 点了就成代为操作了。"""
        base = "https://changjiang.yuketang.cn"
        try:
            from .config import load

            base = load().base_url or base
        except Exception:
            pass
        url = f"{base}/v/index/course/normalcourse/examcover/{self.classroom}/{self.exam_id}"

        self.session = Session(self.cfg)
        self.session.start(headless=self.headless, mobile=True)
        self.page = self.session.page()
        goto(self.page, url)
        self.page.wait_for_timeout(2500)
        # 考试标题，答题时带给模型做背景
        try:
            txt = self.page.evaluate("() => document.body.innerText.slice(0, 200)") or ""
            for line in txt.split("\n"):
                s = line.strip()
                if s and "考生须知" not in s and len(s) > 3:
                    self.title = s[:60]
                    break
        except Exception:
            pass

    def wait_enter(self, wait_seconds: int = 600, on_wait=None, cancel=None) -> str:
        """等用户自己点「继续答题」并在限时弹窗里确认。

        返回 "ok" / "timeout" / "closed" / "cancelled"
        """
        if on_wait:
            on_wait()
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if cancel is not None and cancel.is_set():
                return "cancelled"
            try:
                for f in self.page.frames:
                    try:
                        if f.evaluate(f"() => document.querySelectorAll('{SEL_ITEM}').length > 0"):
                            self.frame = f
                            self.log("检测到已进入考试页")
                            return "ok"
                    except Exception:
                        continue
                # 拆小一点，好让 Esc 及时生效
                for _ in range(5):
                    if cancel is not None and cancel.is_set():
                        return "cancelled"
                    self.page.wait_for_timeout(500)
            except Exception:
                return "closed"      # 浏览器被关掉了
        return "timeout"

    def enter(self, wait_seconds: int = 600, on_wait=None, cancel=None) -> str:
        """打开封面 + 等用户进入。"""
        self.open_cover()
        return self.wait_enter(wait_seconds, on_wait, cancel)

    def read(self) -> list[dict]:
        if not self.frame:
            return []
        return read_problems(self.frame)

    def shot_problem(self, p: dict) -> str:
        """截图这道题，返回图片路径（题目含图时用）。

        考试页的 .subject-item 常常没有 id，所以按索引定位。
        """
        if not self.frame:
            return ""
        out_dir = self.cfg.out_dir / f"exam-{self.exam_id}"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"q{p['no']}.png"
            if p.get("domId"):
                loc = self.frame.locator(f"#{p['domId']}").first
            else:
                loc = self.frame.locator(SEL_ITEM).nth(max(0, int(p.get("index", 1)) - 1))
            loc.screenshot(path=str(path), timeout=15000)
            return str(path)
        except Exception:
            return ""

    def ocr_problem(self, p: dict) -> str:
        """退路：图里的字用 OCR 读（模型不能看图时才走这条）。"""
        path = self.shot_problem(p)
        if not path:
            return ""
        from . import ocr as ocr_mod

        if not ocr_mod.available():
            return ""
        try:
            return ocr_mod.ocr_png(path).strip()
        except Exception:
            return ""

    def image_b64(self, p: dict) -> str:
        """截图并转 base64，喂给多模态模型。"""
        path = self.shot_problem(p)
        if not path:
            return ""
        try:
            import base64

            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode()
        except Exception:
            return ""

    def close(self) -> None:
        try:
            if self.session:
                self.session.close()
        except Exception:
            pass
        self.session = None
        self.page = None
        self.frame = None
