"""浏览器工厂：持久化 profile、反后台挂起、可见性伪造。"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

from .config import Config
from .utils import err, log, warn

# 让窗口即使在后台也保持渲染与计时器精度
CHROME_ARGS = [
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling",
    "--no-first-run",
    "--no-default-browser-check",
    "--autoplay-policy=no-user-gesture-required",
]

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)

# 关键：雨课堂会监听 visibilitychange / blur 暂停视频并判定"切屏"
STEALTH_JS = r"""
(() => {
  const patch = (obj, prop, value) => {
    try { Object.defineProperty(obj, prop, { get: () => value, configurable: true }); } catch (e) {}
  };
  patch(document, 'visibilityState', 'visible');
  patch(document, 'hidden', false);
  patch(document, 'webkitVisibilityState', 'visible');
  patch(document, 'webkitHidden', false);
  try { Object.defineProperty(document, 'hasFocus', { value: () => true, configurable: true }); } catch (e) {}

  const kill = (e) => { e.stopImmediatePropagation(); };
  for (const t of ['visibilitychange', 'webkitvisibilitychange', 'blur', 'pagehide', 'freeze']) {
    window.addEventListener(t, kill, true);
    document.addEventListener(t, kill, true);
  }
  // 兜底：某些页面用 onblur 属性
  window.onblur = null;
  document.onvisibilitychange = null;
})();
"""


class Session:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._pw = None
        self._ctx: BrowserContext | None = None

    # ------------------------------------------------------------ lifecycle
    def __enter__(self) -> "Session":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self, mobile: bool | None = None, headless: bool | None = None,
              viewport: dict | None = None) -> BrowserContext:
        b = self.cfg.raw["browser"]
        use_mobile = bool(b.get("mobile", True)) if mobile is None else mobile
        use_headless = bool(b.get("headless", False)) if headless is None else headless
        profile = self.cfg.profile_dir
        profile.mkdir(parents=True, exist_ok=True)

        self._pw = sync_playwright().start()
        vp = viewport or ({"width": 430, "height": 932} if use_mobile
                          else {"width": 1600, "height": 1000})
        kwargs: dict = dict(
            user_data_dir=str(profile),
            headless=use_headless,
            args=CHROME_ARGS,
            slow_mo=int(b.get("slow_mo", 0)),
            locale=b.get("locale", "zh-CN"),
            timezone_id="Asia/Shanghai",
            viewport=vp,
            device_scale_factor=3 if use_mobile else 1,
            is_mobile=use_mobile,
            has_touch=use_mobile,
        )
        if use_mobile:
            kwargs["user_agent"] = MOBILE_UA

        channel = (b.get("channel") or "").strip()
        if channel:
            kwargs["channel"] = channel

        try:
            self._ctx = self._pw.chromium.launch_persistent_context(**kwargs)
        except Exception as e:  # channel 不存在 / 浏览器未安装
            if channel:
                warn(f"channel={channel} 启动失败（{e}），回退到自带 chromium")
                kwargs.pop("channel", None)
                self._ctx = self._pw.chromium.launch_persistent_context(**kwargs)
            else:
                err("浏览器启动失败。先执行： python -m playwright install chromium")
                raise

        self._ctx.add_init_script(STEALTH_JS)
        try:
            self._ctx.set_default_timeout(20_000)
        except Exception:
            pass
        # 外部注入的登录态（.profile/login.json）。persistent context 自己存不住
        # add_cookies 的结果，所以单独放一份，每次启动都盖上去。
        # 想要它失效：在 TUI 里「重新登录」会把它删掉，之后就是正常登录的 cookie 了。
        login_file = profile / "login.json"
        if login_file.exists():
            try:
                import json as _json

                payload = _json.loads(login_file.read_text(encoding="utf-8")) or {}
                cookies = payload.get("cookies") or []
                if cookies:
                    self._ctx.add_cookies(cookies)
            except Exception:
                pass
        log(f"浏览器就绪 · profile={profile.name} · headless={kwargs['headless']}")
        return self._ctx

    def close(self) -> None:
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    # ----------------------------------------------------------------- page
    @property
    def ctx(self) -> BrowserContext:
        if not self._ctx:
            self.start()
        return self._ctx  # type: ignore[return-value]

    def page(self) -> Page:
        """复用第一个标签页，避免开关页面丢登录态上下文。"""
        ctx = self.ctx
        pages = [p for p in ctx.pages if not p.is_closed()]
        pg = pages[0] if pages else ctx.new_page()
        return pg

    def new_page(self) -> Page:
        return self.ctx.new_page()


@contextmanager
def session(cfg: Config):
    s = Session(cfg)
    try:
        s.start()
        yield s
    finally:
        s.close()


def goto(page: Page, url: str, wait: str = "domcontentloaded", retries: int = 2) -> bool:
    """带重试的导航；超时不算失败（SPA 长轮询常见）。"""
    last = None
    for i in range(retries + 1):
        try:
            page.goto(url, wait_until=wait, timeout=45_000)
            return True
        except Exception as e:
            last = e
            log(f"导航重试 {i + 1}/{retries + 1}: {e.__class__.__name__}")
    warn(f"导航失败: {url} -> {last}")
    return False
