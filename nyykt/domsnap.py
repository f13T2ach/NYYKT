"""页面结构快照 —— probe 用，也是自适应选择器的底层依据。"""

from __future__ import annotations

from typing import Any

from playwright.sync_api import Page

SNAPSHOT_JS = r"""
(args) => {
  const max = args.max || 200;
  const clip = (s, n) => (s || '').replace(/\s+/g, ' ').trim().slice(0, n || 120);

  // 收集包括 open shadow root 在内的元素
  const deepAll = (sel) => {
    const out = [];
    const walk = (root) => {
      try { out.push(...root.querySelectorAll(sel)); } catch (e) {}
      for (const el of root.querySelectorAll('*')) {
        if (el.shadowRoot) walk(el.shadowRoot);
      }
    };
    walk(document);
    return out;
  };

  const path = (el) => {
    if (!el) return null;
    const parts = [];
    let n = el, depth = 0;
    while (n && n.nodeType === 1 && depth < 6) {
      let p = n.tagName.toLowerCase();
      if (n.id) { p += '#' + n.id; parts.unshift(p); break; }
      const cls = (n.className && typeof n.className === 'string')
        ? n.className.trim().split(/\s+/).slice(0, 2).join('.') : '';
      if (cls) p += '.' + cls;
      parts.unshift(p);
      n = n.parentElement; depth++;
    }
    return parts.join(' > ');
  };

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
  };

  const snap = {
    url: location.href,
    title: document.title,
    readyState: document.readyState,
    viewport: { w: innerWidth, h: innerHeight },
    bodyTextLen: (document.body && document.body.innerText || '').length,
    iframes: [...document.querySelectorAll('iframe')].map(f => ({ src: clip(f.src, 200), title: f.title })),
    videos: deepAll('video').map(v => ({
      src: clip((v.currentSrc || v.src || ''), 160),
      duration: Number.isFinite(v.duration) ? Math.round(v.duration) : String(v.duration),
      currentTime: Math.round(v.currentTime || 0),
      paused: v.paused, ended: v.ended, muted: v.muted,
      playbackRate: v.playbackRate,
      readyState: v.readyState,
      poster: clip(v.poster, 120),
      visible: visible(v),
      rect: (() => { const r = v.getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }; })(),
      parent: path(v.parentElement),
    })),
    inputs: deepAll('input, textarea, [contenteditable="true"], [contenteditable=""]').slice(0, max).map(el => ({
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      placeholder: clip(el.getAttribute('placeholder'), 80),
      ariaLabel: clip(el.getAttribute('aria-label'), 80),
      role: el.getAttribute('role') || '',
      value: clip(el.value || el.innerText, 60),
      visible: visible(el),
      cls: clip(el.className && el.className.toString(), 100),
      path: path(el),
    })),
    clickables: deepAll('button, a, [role="button"], [role="tab"], [role="menuitem"]')
      .filter(visible)
      .slice(0, max)
      .map(el => ({
        tag: el.tagName.toLowerCase(),
        text: clip(el.innerText, 60),
        href: clip(el.getAttribute('href'), 160),
        aria: clip(el.getAttribute('aria-label'), 60),
        cls: clip(el.className && el.className.toString(), 100),
        path: path(el),
      })),
    cursorPointers: (() => {
      const out = [];
      for (const el of deepAll('div, span, li, section')) {
        if (out.length >= 80) break;
        if (!visible(el)) continue;
        if (getComputedStyle(el).cursor !== 'pointer') continue;
        const t = clip(el.innerText, 40);
        if (!t) continue;
        out.push({ text: t, cls: clip(el.className && el.className.toString(), 100), path: path(el) });
      }
      return out;
    })(),
    links: [...document.querySelectorAll('a[href]')]
      .map(a => ({ href: clip(a.getAttribute('href'), 200), text: clip(a.innerText, 60) }))
      .filter(x => x.href && !x.href.startsWith('javascript'))
      .slice(0, max * 2),
    nodeAttrs: (() => {
      const out = [];
      const seen = new Set();
      for (const el of deepAll('[data-node-id],[data-nodeid],[data-id],[data-key],[id]')) {
        const v = el.getAttribute('data-node-id') || el.getAttribute('data-nodeid')
          || el.getAttribute('data-id') || el.getAttribute('data-key') || el.id;
        if (!v || seen.has(v)) continue;
        seen.add(v);
        if (out.length >= max) break;
        out.push({ key: v, tag: el.tagName.toLowerCase(), text: clip(el.innerText, 40), cls: clip(el.className && el.className.toString(), 80) });
      }
      return out;
    })(),
    textBlocks: (() => {
      const out = [];
      const seen = new Set();
      const els = [...document.body.querySelectorAll('p, li, h1, h2, h3, h4, td, span, div')];
      for (const el of els) {
        if (out.length >= 400) break;
        // 只取叶子块，避免整页重复
        if (el.children.length > 2) continue;
        const t = (el.innerText || '').trim();
        if (t.length < 4 || t.length > 400) continue;
        const k = t.slice(0, 80);
        if (seen.has(k)) continue;
        seen.add(k);
        out.push({ tag: el.tagName.toLowerCase(), text: t.replace(/\n+/g, ' | ').slice(0, 300), cls: clip(el.className && el.className.toString(), 80) });
      }
      return out;
    })(),
  };
  return snap;
};
"""


def snapshot(page: Page, max_items: int = 200) -> dict[str, Any]:
    return page.evaluate(SNAPSHOT_JS, {"max": max_items})


def page_text(page: Page) -> str:
    try:
        return page.inner_text("body") or ""
    except Exception:
        try:
            return page.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            return ""


def has_video(page: Page) -> bool:
    try:
        return bool(page.evaluate(
            "() => { const w=(r)=>{ for(const v of r.querySelectorAll('video')) return true;"
            " for(const e of r.querySelectorAll('*')) if(e.shadowRoot && w(e.shadowRoot)) return true; return false; };"
            " return w(document); }"
        ))
    except Exception:
        return False
