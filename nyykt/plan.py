"""课程目录：从页面里扒节点，生成 plan.json。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page

from .config import Config
from .utils import clean, log, ok, truncate, warn

COLLECT_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const push = (rec) => {
    if (!rec.node_id || seen.has(rec.node_id)) return;
    seen.add(rec.node_id);
    out.push(rec);
  };

  // 1) 链接里带 node_id
  for (const a of document.querySelectorAll('a[href]')) {
    let url;
    try { url = new URL(a.href, location.origin); } catch (e) { continue; }
    const nid = url.searchParams.get('node_id') || url.searchParams.get('nodeId');
    if (!nid) continue;
    push({
      node_id: nid,
      url: url.href,
      text: (a.innerText || a.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 160),
      cls: (typeof a.className === 'string' ? a.className : '').slice(0, 100),
      attrs: '',
      via: 'href',
    });
  }

  // 2) 带 data-* 的元素（SPA 常常用点击事件而非 href）
  const sel = '[data-node-id],[data-nodeid],[data-node],[data-id]';
  for (const el of document.querySelectorAll(sel)) {
    const nid = el.getAttribute('data-node-id') || el.getAttribute('data-nodeid')
      || el.getAttribute('data-node') || el.getAttribute('data-id');
    if (!nid || !/^\d+$/.test(nid)) continue;
    const attrs = [...el.attributes]
      .filter(a => /type|kind|category|resource|chapter/i.test(a.name))
      .map(a => a.name + '=' + a.value).join(';');
    push({
      node_id: nid,
      url: '',
      text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 160),
      cls: (typeof el.className === 'string' ? el.className : '').slice(0, 100),
      attrs,
      via: 'data',
    });
  }
  return out;
};
"""

COURSE_ID_RE = re.compile(r"lms-graph-mobile/(\d+)")
KIND_RULES: list[tuple[str, re.Pattern]] = [
    ("video", re.compile(r"/video/|视频|微课|录播|回放", re.I)),
    ("discussion", re.compile(r"讨论|/discussion|话题|互动|评论", re.I)),
    ("quiz", re.compile(r"作业|/homework|测试|测验|考试|/exam|习题|答题", re.I)),
    ("doc", re.compile(r"文档|资料|阅读|/document|ppt|讲义", re.I)),
]


@dataclass
class Node:
    node_id: str
    url: str = ""
    title: str = ""
    kind: str = "unknown"
    via: str = ""
    attrs: str = ""
    status: str = "pending"
    note: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def course_id_of(url: str) -> str | None:
    m = COURSE_ID_RE.search(url)
    return m.group(1) if m else None


def classify(node: Node) -> str:
    hay = " ".join([node.url or "", node.title or "", node.attrs or ""])
    for kind, pat in KIND_RULES:
        if pat.search(hay):
            return kind
    if node.via == "href":
        return "unknown"
    return "unknown"


def expand_catalog(page: Page, cfg: Config) -> None:
    """点开目录/章节入口，并滚动加载。"""
    timeout_ms = int(float(cfg.get("catalog", "expand_timeout_seconds", 6)) * 1000)

    # 目录可能在抽屉里，先试常见入口
    for label in ("目录", "章节", "全部章节", "课程目录", "大纲", "展开全部", "查看更多"):
        try:
            loc = page.get_by_text(label, exact=True)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=1500)
                page.wait_for_timeout(800)
                log(f"  展开入口：{label}")
        except Exception:
            continue

    # 滚动到底触发懒加载
    last_h = 0
    for _ in range(12):
        page.mouse.wheel(0, 1600)
        page.wait_for_timeout(600)
        h = page.evaluate("() => document.body.scrollHeight")
        if h == last_h:
            break
        last_h = h
    page.wait_for_timeout(int(timeout_ms * 0.3))


def collect_nodes(page: Page) -> list[Node]:
    raw = page.evaluate(COLLECT_JS) or []
    nodes: list[Node] = []
    for r in raw:
        n = Node(
            node_id=str(r.get("node_id")),
            url=r.get("url") or "",
            title=clean(r.get("text") or ""),
            via=r.get("via") or "",
            attrs=r.get("attrs") or "",
        )
        n.kind = classify(n)
        nodes.append(n)
    return nodes


def build_plan(page: Page, entry_url: str, cfg: Config) -> dict:
    cid = course_id_of(entry_url) or ""
    nodes = collect_nodes(page)

    # 补全 url：href 缺失的，用同课程 video 页模板拼不上，只能靠点击；
    # 这里至少保证有 node_id，后续以 click 方式访问
    have_url = sum(1 for n in nodes if n.url)
    log(f"  抓到节点 {len(nodes)} 个（含直链 {have_url}）")

    order = {"video": 0, "discussion": 1, "quiz": 2, "doc": 3, "unknown": 4}
    nodes.sort(key=lambda n: (order.get(n.kind, 9),))

    return {
        "entry_url": entry_url,
        "course_id": cid,
        "generated_by": "nyykt explore",
        "nodes": [n.to_dict() for n in nodes],
    }


def save_plan(plan: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")


def load_plan(path: Path) -> list[Node]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("nodes", data if isinstance(data, list) else [])
    out = []
    for it in items:
        out.append(Node(
            node_id=str(it.get("node_id", "")),
            url=it.get("url", ""),
            title=it.get("title", ""),
            kind=it.get("kind", "unknown"),
            via=it.get("via", ""),
            attrs=it.get("attrs", ""),
            status=it.get("status", "pending"),
            note=it.get("note", ""),
        ))
    return out
