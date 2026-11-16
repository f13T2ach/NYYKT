"""网络记录：把所有请求/响应落到 jsonl，用于校准接口。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from playwright.sync_api import Page

from .utils import truncate

# 只记录有信息量的请求，滤掉静态资源
SKIP_SUFFIX = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".woff", ".woff2",
    ".ttf", ".eot", ".mp4", ".m3u8", ".ts", ".css",
)


def _interesting(url: str) -> bool:
    low = url.lower().split("?")[0]
    if any(low.endswith(s) for s in SKIP_SUFFIX):
        return False
    return any(k in low for k in ("api", "graph", "lms", "lesson", "video", "discussion", "node", "course", "study"))


class NetLog:
    """挂到 page 上，把关键请求写成 jsonl。"""

    def __init__(self, path: Path, max_body: int = 800):
        self.path = path
        self.max_body = max_body
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = self.path.open("a", encoding="utf-8")
        self.count = 0

    def attach(self, page: Page) -> None:
        page.on("request", self._on_request)
        page.on("response", self._on_response)

    def _write(self, rec: dict[str, Any]) -> None:
        self.fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.fh.flush()
        self.count += 1

    def _on_request(self, req) -> None:
        try:
            url = req.url
            if not _interesting(url):
                return
            rec = {
                "t": "req",
                "method": req.method,
                "url": url,
                "rt": req.resource_type,
                "headers": {
                    k: v for k, v in (req.headers or {}).items()
                    if k.lower() in ("content-type", "authorization", "x-client", "xtbz", "referer", "x-requested-with")
                },
            }
            body = req.post_data
            if body:
                rec["body"] = truncate(body, self.max_body)
            self._write(rec)
        except Exception:
            pass

    def _on_response(self, resp) -> None:
        try:
            url = resp.url
            if not _interesting(url):
                return
            ct = (resp.headers or {}).get("content-type", "")
            if "json" not in ct:
                return
            rec = {"t": "resp", "status": resp.status, "url": url}
            if resp.status >= 400 or "graph" in url:
                try:
                    rec["body"] = truncate(resp.text(), self.max_body)
                except Exception:
                    rec["body"] = "<unreadable>"
            self._write(rec)
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.fh.close()
        except Exception:
            pass
