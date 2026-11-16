"""讨论作业：抄现有讨论，没有就抄大纲一句，填进"说点什么"并提交。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import ElementHandle, Page

from .config import Config
from .domsnap import page_text
from .utils import clean, cjk_ratio, log, ok, pick_sentence, squeeze, truncate, warn

PLACEHOLDER_PAT = re.compile(r"说点什么|说两句|写点什么|发表.{0,4}看法|输入.{0,6}内容|评论", re.I)
SUBMIT_TEXTS = ["发送", "发布", "提交", "回复", "发表", "确定"]
UI_NOISE = {
    "全部", "最新", "热门", "展开", "收起", "查看更多", "暂无讨论", "暂无评论",
    "写评论", "说点什么", "讨论", "作业", "发消息", "加载中", "更多", "回复",
}

COLLECT_JS = r"""
(el, args) => {
  const { minLen, maxLen, avoid, maxItems, containerSel } = args;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();

  // 1) 若显式指定容器，优先用它
  let scope = null;
  if (containerSel) { try { scope = document.querySelector(containerSel); } catch (e) {} }

  // 2) 否则：从输入框向上找到"列表型"容器
  if (!scope) {
    let node = el, best = null;
    for (let i = 0; i < 10 && node; i++) {
      node = node.parentElement;
      if (!node) break;
      const kids = [...node.children];
      if (kids.length >= 2 && (node.innerText || '').length > 150) { best = node; break; }
    }
    scope = best || document.body;
  }

  const out = [];
  const seen = new Set();
  const cands = scope.querySelectorAll('div, p, li, span, article, section, dd, blockquote');

  for (const c of cands) {
    if (out.length >= maxItems) break;
    if (c === el || c.contains(el) || el.contains(c)) continue;
    // 只取"叶子块"：内部子元素少，避免把整列表抓成一条
    if (c.children.length > 3) continue;
    const t = norm(c.innerText);
    if (t.length < minLen || t.length > maxLen) continue;
    if (seen.has(t)) continue;
    if (avoid.some(k => t.includes(k))) continue;
    // 必须像人话：有中文标点 或 足够长
    const hasPunct = /[，。！？、；：）]/.test(t);
    if (!hasPunct && t.length < 26) continue;
    // 排除 UI 文案
    if (!/[，。！？]/.test(t) && t.length < 34) continue;
    seen.add(t);
    out.push({ text: t, len: t.length, cls: (c.className || '').toString().slice(0, 80) });
  }
  // 长句优先（信息量更大），但别太长
  out.sort((a, b) => (b.len - a.len));
  return out;
}
"""


@dataclass
class PostResult:
    ok: bool = False
    text: str = ""
    source: str = ""
    reason: str = ""
    candidates: int = 0
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ 输入框

def find_input(page: Page) -> ElementHandle | None:
    """定位"说点什么"输入框。顺序：placeholder → 附近文本 → 可见 contenteditable。"""
    # 1) placeholder / aria-label 命中
    for sel in (
        '[contenteditable="true"][data-placeholder*="说点什么"]',
        '[contenteditable="true"]',
        'textarea',
        'input[type="text"]',
    ):
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 12)):
                el = loc.nth(i)
                try:
                    if not el.is_visible():
                        continue
                    meta = " ".join(filter(None, [
                        el.get_attribute("placeholder") or "",
                        el.get_attribute("aria-label") or "",
                        el.get_attribute("data-placeholder") or "",
                    ]))
                    if PLACEHOLDER_PAT.search(meta):
                        return el.element_handle()
                except Exception:
                    continue
        except Exception:
            continue

    # 2) 页面里有"说点什么"这几个字，找它附近的可编辑元素
    try:
        hint = page.get_by_text(PLACEHOLDER_PAT).first
        if hint.count() and hint.is_visible():
            box = hint.bounding_box()
            if box:
                handle = page.evaluate_handle(
                    """(pt) => {
                      const els = [...document.querySelectorAll('[contenteditable="true"], textarea, input')];
                      let best = null, bestD = 1e9;
                      for (const e of els) {
                        const r = e.getBoundingClientRect();
                        if (r.width < 30 || r.height < 10) continue;
                        const d = Math.hypot(r.x - pt.x, r.y - pt.y);
                        if (d < bestD) { bestD = d; best = e; }
                      }
                      return best;
                    }""",
                    {"x": box["x"], "y": box["y"]},
                )
                el = handle.as_element()
                if el:
                    return el
    except Exception:
        pass

    # 3) 兜底：第一个可见的 contenteditable
    try:
        loc = page.locator('[contenteditable="true"]')
        for i in range(min(loc.count(), 12)):
            el = loc.nth(i)
            if el.is_visible():
                return el.element_handle()
    except Exception:
        pass
    return None


# ------------------------------------------------------------------ 候选

def collect_candidates(page: Page, input_el: ElementHandle, cfg: Config) -> list[dict]:
    d = cfg.raw["discussion"]
    try:
        return input_el.evaluate(
            COLLECT_JS,
            {
                "minLen": int(d.get("min_len", 15)),
                "maxLen": int(d.get("max_len", 220)),
                "avoid": list(d.get("avoid_keywords", [])),
                "maxItems": int(d.get("max_candidates", 40)),
                "containerSel": d.get("container_selector") or "",
            },
        ) or []
    except Exception as e:
        warn(f"收集讨论失败: {e}")
        return []


def load_posted(path: Path) -> set[str]:
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_posted(path: Path, posted: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(posted), ensure_ascii=False, indent=0), encoding="utf-8")


def choose(cands: list[dict], posted: set[str], cfg: Config) -> dict | None:
    for c in cands:
        t = c["text"]
        if squeeze(t) in posted:
            continue
        if cjk_ratio(t) < 0.5:
            continue
        if t in UI_NOISE:
            continue
        return c
    return None


# ------------------------------------------------------------------ 填写

def fill_box(page: Page, input_el: ElementHandle, text: str) -> bool:
    try:
        input_el.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        input_el.click(timeout=4000)
    except Exception:
        try:
            box = input_el.bounding_box()
            if box:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        except Exception:
            return False

    tag = (input_el.evaluate("e => e.tagName.toLowerCase()") or "").lower()
    if tag in ("textarea", "input"):
        try:
            input_el.fill("")
            input_el.type(text, delay=28)
            return True
        except Exception:
            pass
    # contenteditable：逐字输入，让富文本框架吃到 input 事件
    try:
        page.keyboard.type(text, delay=28)
        return True
    except Exception:
        try:
            page.keyboard.insert_text(text)
            return True
        except Exception:
            return False


def submit(page: Page, cfg: Config) -> tuple[bool, str]:
    """点发送；优先按钮，其次 Enter。"""
    for t in SUBMIT_TEXTS:
        try:
            loc = page.get_by_text(t, exact=True)
            for i in range(min(loc.count(), 6)):
                el = loc.nth(i)
                try:
                    if el.is_visible() and el.is_enabled():
                        el.click(timeout=2500)
                        return True, f"点击按钮「{t}」"
                except Exception:
                    continue
        except Exception:
            continue
    # 按钮可能是 <button> 无文本 / 图标，试 Enter
    try:
        page.keyboard.press("Enter")
        return True, "按 Enter 提交"
    except Exception as e:
        return False, f"提交失败: {e}"


# ------------------------------------------------------------------ 主流程

def post_discussion(
    page: Page,
    cfg: Config,
    outline: str | None = None,
    posted_path: Path | None = None,
    dry_run: bool | None = None,
) -> PostResult:
    d = cfg.raw["discussion"]
    dry = d.get("dry_run", False) if dry_run is None else dry_run
    res = PostResult()

    input_el = find_input(page)
    if not input_el:
        # 可能要先点开"参与讨论 / 写评论"入口
        for t in ("参与讨论", "写评论", "说点什么", "发表评论", "我也说两句", "讨论"):
            try:
                loc = page.get_by_text(t, exact=True)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=2000)
                    page.wait_for_timeout(900)
                    input_el = find_input(page)
                    if input_el:
                        break
            except Exception:
                continue
    if not input_el:
        res.reason = "找不到「说点什么」输入框"
        return res

    cands = collect_candidates(page, input_el, cfg)
    res.candidates = len(cands)
    posted = load_posted(posted_path) if posted_path else set()

    pick = choose(cands, posted, cfg)
    if pick:
        res.text = pick["text"]
        res.source = "他人讨论"
    elif d.get("fallback_outline", True) and outline:
        sent = pick_sentence(outline, lo=20, hi=int(d.get("max_len", 220)))
        if sent:
            res.text = sent
            res.source = "课程大纲"
            for _ in range(3):
                if squeeze(res.text) not in posted:
                    break
                sent = pick_sentence(outline, lo=24, hi=int(d.get("max_len", 220)))
                if not sent:
                    break
                res.text = sent
        else:
            res.reason = "大纲里没有可用句子"
            return res
    else:
        res.reason = f"没有可用讨论（候选 {len(cands)} 条，全被去重/过滤）"
        return res

    res.text = clean(res.text)
    if not res.text:
        res.reason = "内容为空"
        return res

    log(f"  待发内容（来源：{res.source}）：{truncate(res.text, 70)}")

    if dry:
        res.ok = True
        res.reason = "dry-run，未实际提交"
        res.notes.append("dry_run")
        return res

    if not fill_box(page, input_el, res.text):
        res.reason = "填写输入框失败"
        return res

    page.wait_for_timeout(500)
    sent, how = submit(page, cfg)
    res.notes.append(how)
    if not sent:
        res.reason = how
        return res

    page.wait_for_timeout(int(float(d.get("submit_wait_seconds", 8)) * 1000 * 0.5))

    # 验证：内容是否出现在页面（或输入框已清空）
    body = squeeze(page_text(page))
    appeared = squeeze(res.text)[:24] in body
    cleared = False
    try:
        if input_el:
            cleared = not (input_el.inner_text() or input_el.evaluate("e => e.value || ''")).strip()
    except Exception:
        pass

    if appeared or cleared:
        res.ok = True
        res.reason = "已提交"
        if posted_path:
            posted.add(squeeze(res.text))
            save_posted(posted_path, posted)
    else:
        res.reason = "提交后未检测到内容出现（可能被拦截或需要审核）"
    return res


def grab_outline(page: Page) -> str:
    """从当前课程页尽量扒出大纲/章节/简介文本。"""
    parts: list[str] = []
    # 常见大纲容器
    for sel in (
        '[class*="outline"]', '[class*="catalog"]', '[class*="chapter"]',
        '[class*="syllabus"]', '[class*="course-detail"]', '[class*="intro"]',
        "main", "article",
    ):
        try:
            loc = page.locator(sel)
            if loc.count():
                t = clean(loc.first.inner_text())
                if len(t) > 120:
                    parts.append(t)
                    break
        except Exception:
            continue
    if not parts:
        parts.append(page_text(page))
    return "\n".join(parts)
