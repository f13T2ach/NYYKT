"""侦察：把页面结构 + 网络请求 dump 出来，用于校准选择器。"""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import Page

from .config import Config
from .domsnap import snapshot
from .netlog import NetLog
from .utils import log, now_stamp, ok


def _md(snap: dict, net: list[dict]) -> str:
    L: list[str] = []
    L.append(f"# probe: {snap.get('title')}\n")
    L.append(f"- URL: `{snap.get('url')}`")
    L.append(f"- 视口: {snap.get('viewport')}  正文字数: {snap.get('bodyTextLen')}")
    L.append(f"- iframe: {len(snap.get('iframes') or [])}")

    if snap.get("iframes"):
        L.append("\n## iframe")
        for f in snap["iframes"]:
            L.append(f"- `{f.get('src')}`")

    L.append("\n## video")
    if snap.get("videos"):
        for v in snap["videos"]:
            L.append(
                f"- duration={v['duration']} cur={v['currentTime']} paused={v['paused']} "
                f"visible={v['visible']} rect={v['rect']}\n  src=`{v['src']}`\n  parent=`{v['parent']}`"
            )
    else:
        L.append("- （无）")

    L.append("\n## 输入框")
    for i in snap.get("inputs") or []:
        L.append(
            f"- <{i['tag']}> type={i['type']} placeholder=`{i['placeholder']}` "
            f"aria=`{i['ariaLabel']}` cls=`{i['cls']}` visible={i['visible']}\n  {i['path']}"
        )
    if not snap.get("inputs"):
        L.append("- （无）")

    L.append("\n## 可点击文本")
    seen = set()
    for c in snap.get("clickables") or []:
        t = (c.get("text") or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        L.append(f"- 「{t}」 <{c['tag']}> href=`{c['href']}` cls=`{c['cls']}`")

    L.append("\n## cursor:pointer 文本")
    for c in (snap.get("cursorPointers") or [])[:50]:
        L.append(f"- 「{c['text']}」 cls=`{c['cls']}`")

    L.append("\n## data-* / id 属性")
    for a in snap.get("nodeAttrs") or []:
        L.append(f"- `{a['key']}` <{a['tag']}> 「{a['text']}」 cls=`{a['cls']}`")

    L.append("\n## 链接")
    for l in (snap.get("links") or [])[:80]:
        L.append(f"- `{l['href']}` 「{l['text']}」")

    L.append("\n## 文本块")
    for b in (snap.get("textBlocks") or [])[:80]:
        L.append(f"- [{b['tag']}.{b['cls']}] {b['text']}")

    L.append("\n## 网络（已过滤静态资源）")
    for r in net[:200]:
        if r.get("t") == "req":
            L.append(f"- → {r['method']} {r['url']}" + (f"\n    body={r.get('body')}" if r.get("body") else ""))
        else:
            L.append(f"- ← {r['status']} {r['url']}")
    return "\n".join(L)


def run_probe(page: Page, url: str, cfg: Config, wait_ms: int = 4000, scroll: bool = True) -> Path:
    out = cfg.out_dir / f"probe-{now_stamp()}"
    out.mkdir(parents=True, exist_ok=True)

    net = NetLog(out / "net.jsonl")
    net.attach(page)

    log(f"打开 {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    except Exception as e:
        log(f"导航警告: {e}")
    page.wait_for_timeout(wait_ms)

    if scroll:
        log("滚动加载…")
        last = 0
        for _ in range(10):
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(500)
            h = page.evaluate("() => document.body.scrollHeight")
            if h == last:
                break
            last = h
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(600)

    snap = snapshot(page, max_items=300)
    (out / "snapshot.json").write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        page.screenshot(path=str(out / "page.png"), full_page=True)
    except Exception:
        pass

    net.close()
    net_recs = []
    try:
        net_recs = [json.loads(l) for l in (out / "net.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        pass

    md = _md(snap, net_recs)
    (out / "report.md").write_text(md, encoding="utf-8")

    ok(f"probe 完成 → {out}")
    log(f"  快照 {out / 'snapshot.json'}")
    log(f"  报告 {out / 'report.md'}")
    log(f"  截图 {out / 'page.png'}")
    log(f"  网络 {out / 'net.jsonl'}（{len(net_recs)} 条）")
    return out
