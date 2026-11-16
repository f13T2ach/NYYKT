"""视频自动播放：原生 video 接管 + 反暂停 + 心跳保活 + 弹窗清扫。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from playwright.sync_api import Frame, Page

from .config import Config
from .utils import log, warn

# 注入的 video 控制器（幂等，支持 shadow DOM）
INSTALL_JS = r"""
(args) => {
  const opts = args || {};
  const rate = opts.rate || 1.0;
  const muted = opts.muted !== false;

  const deepFind = (tag) => {
    const walk = (root) => {
      const hit = root.querySelector(tag);
      if (hit) return hit;
      for (const el of root.querySelectorAll('*')) {
        if (el.shadowRoot) { const r = walk(el.shadowRoot); if (r) return r; }
      }
      return null;
    };
    return walk(document);
  };

  const v = deepFind('video');
  if (!v) return { ok: false, reason: 'no-video-element' };

  if (window.__NYYKT && window.__NYYKT.timer) clearInterval(window.__NYYKT.timer);

  const S = { ok: true, ended: false };

  const ensure = () => {
    try {
      if (muted && !v.muted) v.muted = true;
      if (Math.abs(v.playbackRate - rate) > 1e-3) v.playbackRate = rate;
      if (v.paused && !v.ended && v.readyState >= 2) {
        const p = v.play();
        if (p && p.catch) p.catch(() => {});
      }
    } catch (e) {}
  };

  v.addEventListener('pause', () => { if (!v.ended) setTimeout(ensure, 250); });
  v.addEventListener('ended', () => { S.ended = true; });

  S.timer = setInterval(ensure, 1200);
  S.get = () => ({
    currentTime: Math.round((v.currentTime || 0) * 10) / 10,
    duration: Number.isFinite(v.duration) ? Math.round(v.duration * 10) / 10 : null,
    paused: v.paused,
    ended: v.ended || S.ended,
    rate: v.playbackRate,
    muted: v.muted,
    readyState: v.readyState,
    buffered: v.buffered && v.buffered.length ? Math.round(v.buffered.end(v.buffered.length - 1) * 10) / 10 : 0,
    src: (v.currentSrc || v.src || '').slice(0, 120),
  });
  S.seek = (t) => { try { v.currentTime = t; ensure(); } catch (e) {} };
  S.setRate = (r) => { try { v.playbackRate = r; } catch (e) {} };
  S.pause = () => { try { v.pause(); } catch (e) {} };

  window.__NYYKT = Object.assign(window.__NYYKT || {}, S);
  ensure();
  return { ok: true, duration: Number.isFinite(v.duration) ? v.duration : null, muted: v.muted, rate: v.playbackRate };
}
"""

STATE_JS = "() => (window.__NYYKT && window.__NYYKT.get) ? window.__NYYKT.get() : null"
SEEK_JS = "(t) => window.__NYYKT && window.__NYYKT.seek ? window.__NYYKT.seek(t) : null"
HAS_VIDEO_JS = (
    "() => { const w=(r)=>{ if(r.querySelector('video')) return true;"
    " for(const e of r.querySelectorAll('*')) if(e.shadowRoot && w(e.shadowRoot)) return true; return false; };"
    " return w(document); }"
)

DISMISS_TEXTS = [
    "我知道了", "知道了", "继续观看", "继续学习", "继续播放", "我还在",
    "关闭", "跳过", "稍后再说", "再想想",
]

ATTENTION_TEXTS = ["随堂练习", "课堂测验", "播放验证", "拖动滑块", "完成验证", "人脸识别"]


@dataclass
class PlayResult:
    watched: float = 0.0
    duration: float | None = None
    finished: bool = False
    reason: str = ""
    events: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ 工具

def pick_frame(page: Page) -> Frame:
    """视频可能嵌在 iframe 里。返回真正含 video 的 frame。"""
    for f in page.frames:
        try:
            if f.evaluate(HAS_VIDEO_JS):
                return f
        except Exception:
            continue
    return page.main_frame


def _click_text(page: Page, text: str) -> bool:
    try:
        loc = page.get_by_text(text, exact=True)
        n = loc.count()
        if n == 0:
            loc = page.get_by_text(text)
            n = loc.count()
        if n == 0:
            return False
        for i in range(min(n, 6)):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    el.click(timeout=1500)
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def dismiss_popups(page: Page) -> list[str]:
    hit: list[str] = []
    for t in DISMISS_TEXTS:
        if _click_text(page, t):
            hit.append(t)
            page.wait_for_timeout(200)
    return hit


def attention_needed(page: Page) -> str | None:
    from .domsnap import page_text

    body = page_text(page)
    for t in ATTENTION_TEXTS:
        if t in body:
            return t
    # 提示"进度异常"之类
    if "进度" in body and "异常" in body:
        return "进度异常提示"
    return None


# ------------------------------------------------------------------ 控制

def start_video(page: Page, cfg: Config) -> tuple[Frame, dict[str, Any]]:
    rate = float(cfg.get("video", "playback_rate", 1.0))
    muted = bool(cfg.get("video", "muted", True))

    frame = pick_frame(page)
    if not frame.evaluate(HAS_VIDEO_JS):
        for t in ("播放", "继续播放", "开始学习", "继续学习", "立即学习"):
            if _click_text(page, t):
                page.wait_for_timeout(1600)
                break
        try:
            page.wait_for_function(HAS_VIDEO_JS, timeout=20_000)
        except Exception:
            return frame, {"ok": False, "reason": "no-video-element"}
        frame = pick_frame(page)

    info = frame.evaluate(INSTALL_JS, {"rate": rate, "muted": muted})
    if not info.get("ok") and info.get("reason") == "no-video-element":
        page.wait_for_timeout(1500)
        frame = pick_frame(page)
        info = frame.evaluate(INSTALL_JS, {"rate": rate, "muted": muted})
    return frame, info


def play_to_end(
    page: Page,
    frame: Frame,
    cfg: Config,
    on_tick: Callable[[dict], None] | None = None,
) -> PlayResult:
    keep = float(cfg.get("video", "keepalive_seconds", 25))
    max_wall = float(cfg.get("video", "max_wall_seconds", 5400))
    finish_ratio = float(cfg.get("video", "finish_ratio", 0.995))
    turbo = bool(cfg.get("video", "turbo_seek", False))
    turbo_step = float(cfg.get("video", "turbo_step_seconds", 30))

    res = PlayResult()
    started = time.time()
    last_report = 0.0
    stalls = 0
    duration: float | None = None
    last_cur = -1.0

    while True:
        elapsed = time.time() - started
        if elapsed > max_wall:
            res.reason = f"超过最长时限 {int(max_wall)}s"
            break

        try:
            st = frame.evaluate(STATE_JS)
        except Exception:
            st = None

        if st is None:
            # 控制器失效（可能 frame 换了）
            frame, info = start_video(page, cfg)
            if not info.get("ok"):
                res.reason = f"控制器不可用: {info.get('reason')}"
                break
            page.wait_for_timeout(1200)
            continue

        if st.get("duration"):
            duration = float(st["duration"])
            res.duration = duration
        cur = float(st.get("currentTime") or 0)
        res.watched = cur

        if on_tick:
            on_tick(st)

        need = attention_needed(page)
        if need:
            res.reason = f"需要人工处理：{need}"
            res.events.append(f"attention:{need}")
            break

        if st.get("ended"):
            res.finished = True
            res.reason = "视频结束"
            break
        if duration and duration > 5 and cur / duration >= finish_ratio:
            res.finished = True
            res.reason = f"进度 {cur:.0f}/{duration:.0f}s 达阈值"
            break

        if elapsed - last_report >= keep:
            last_report = elapsed
            pct = f"{cur / duration * 100:.0f}%" if duration else "?"
            log(f"  播放 {cur:.0f}/{duration or '?'}s ({pct}) rate={st.get('rate')} "
                f"paused={st.get('paused')} ready={st.get('readyState')} 用时{int(elapsed)}s")
            dismiss_popups(page)
            page.mouse.move(30, 60, steps=4)
            if st.get("paused"):
                stalls += 1
                if stalls >= 5:
                    res.reason = "视频反复暂停（疑似被反自动化拦截）"
                    break
            else:
                stalls = 0
            if abs(cur - last_cur) < 0.5 and not st.get("paused"):
                res.events.append("progress-stuck")
            last_cur = cur

        if turbo and duration:
            api = min(cur + turbo_step, max(duration - 1, 1))
            if api > cur + 1:
                try:
                    frame.evaluate(SEEK_JS, api)
                except Exception:
                    pass

        page.wait_for_timeout(1000)

    try:
        frame.evaluate("() => window.__NYYKT && window.__NYYKT.pause && window.__NYYKT.pause()")
    except Exception:
        pass
    return res


def mark_done(page: Page) -> str | None:
    for t in ("下一节", "下一章", "继续学习", "下一集", "下一个", "标记完成", "完成学习"):
        if _click_text(page, t):
            log(f"  点击：{t}")
            return t
    return None
