"""接口层：直接调用雨课堂 REST 接口（带 cookie），比 DOM 驱动稳定。

已知接口（2026-11 实测）：

  GET  /mooc-api/v1/lms/learn/course/chapter?cid=<classroom_id>&sign=<sign>&classroom_id=<classroom_id>
       → 整门课的章节树
  GET  /mooc-api/v1/lms/learn/leaf_info/<classroom_id>/<leaf_id>/?classroom_id=<classroom_id>
       → 单个学习单元详情
  GET  /video-log/get_video_watch_progress/?cid=<cid>&user_id=<uid>&classroom_id=&video_id=
       → 观看进度
  POST /video-log/heartbeat/   {"heart_data":[{...}]}
       → 观看心跳上报
  GET  /v/discussion/v2/unit/discussion/?classroom_id=&leaf_id=&topic_type=0&channel=xt&sku_id=
       → 该单元的讨论 topic
  GET  /v/discussion/v2/comment/list/<topic_id>/?offset=&limit=
       → 讨论区评论列表
  POST /c27/online_courseware/xty/instance/add_study_leaf/  {"leaf_id":..,"classroom_id":..}
       → 标记学习单元
"""

from __future__ import annotations

import json
import re
from typing import Any

from playwright.sync_api import BrowserContext, Page

from .config import Config
from .utils import log, warn

BASE_HEADERS = {"xtbz": "ykt", "accept": "application/json, text/plain, */*"}
SIGN_PAT = re.compile(r"""sign["']?\s*[:=]\s*["']([A-Za-z0-9_-]{6,24})""")


class Api:
    def __init__(self, ctx: BrowserContext, cfg: Config):
        self.ctx = ctx
        self.cfg = cfg
        self.base = cfg.base_url
        self._my_uid: int = 0

    def my_user_id(self) -> int:
        """讨论系统里的 user_id（= basic-info 的 data.id）。

        注意：视频系统用的是另一个 uid（heartbeat 里的 u），两者不同。
        """
        if self._my_uid:
            return self._my_uid
        r = self.call("GET", "/api/v3/user/basic-info")
        d = (r.get("json") or {}).get("data") or {}
        try:
            self._my_uid = int(d.get("id") or 0)
        except (TypeError, ValueError):
            self._my_uid = 0
        return self._my_uid

    # ------------------------------------------------------------- 底层
    def _csrf(self) -> str:
        """Django 的 CSRF token 在 cookie 里，POST 必须回填到 header。"""
        try:
            for c in self.ctx.cookies():
                if c.get("name") == "csrftoken":
                    return c.get("value") or ""
        except Exception:
            pass
        return ""

    def call(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        body: Any = None,
        headers: dict | None = None,
        referer: str | None = None,
    ) -> dict:
        method = method.upper()
        h = dict(BASE_HEADERS)
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            tok = self._csrf()
            if tok:
                h["X-CSRFToken"] = tok
                h["X-CSRF-Token"] = tok
            h["origin"] = self.base
            h["referer"] = referer or (self.base + "/")
        if headers:
            h.update(headers)
        if referer:
            h["referer"] = referer
        url = path if path.startswith("http") else self.base + path
        try:
            r = self.ctx.request.fetch(
                url,
                method=method,
                headers=h,
                params=params,
                data=body if body is None or isinstance(body, str) else json.dumps(body),
                timeout=30_000,
            )
        except Exception as e:
            return {"http": 0, "ok": False, "error": str(e), "json": None}

        text = ""
        try:
            text = r.text()
        except Exception:
            pass
        data = None
        try:
            data = json.loads(text)
        except Exception:
            pass
        return {"http": r.status, "ok": 200 <= r.status < 300, "json": data, "text": text}

    def get(self, path: str, **kw) -> dict:
        return self.call("GET", path, **kw)

    def post(self, path: str, **kw) -> dict:
        return self.call("POST", path, **kw)

    # ------------------------------------------------------------- 课程树
    def find_sign(self, page: Page, url: str | None = None) -> str | None:
        """sign 是服务端渲染进 HTML / 全局变量的签名参数。"""
        try:
            html = page.content()
            m = SIGN_PAT.search(html)
            if m:
                return m.group(1)
        except Exception:
            pass
        try:
            val = page.evaluate(
                """() => {
                  const keys = ['__INITIAL_STATE__','__NUXT__','__DATA__','__PRELOADED_STATE__'];
                  for (const k of keys) {
                    try { const s = JSON.stringify(window[k]); if (s && s.includes('sign')) return s.slice(0, 5000); } catch(e){}
                  }
                  return '';
                }"""
            )
            if val:
                m = SIGN_PAT.search(val)
                if m:
                    return m.group(1)
        except Exception:
            pass
        return None

    def chapter(self, classroom_id: int | str, sign: str | None = None) -> dict:
        p: dict = {"cid": classroom_id, "classroom_id": classroom_id, "show_unpublished": 0}
        if sign:
            p["sign"] = sign
        return self.get("/mooc-api/v1/lms/learn/course/chapter", params=p)

    def schedule(self, classroom_id: int | str, sign: str | None = None) -> dict:
        p: dict = {"cid": classroom_id, "classroom_id": classroom_id, "is_h5": "true"}
        if sign:
            p["sign"] = sign
        return self.get("/mooc-api/v1/lms/learn/course/schedule", params=p)

    def leaf_info(self, classroom_id: int | str, leaf_id: int | str) -> dict:
        return self.get(
            f"/mooc-api/v1/lms/learn/leaf_info/{classroom_id}/{leaf_id}/",
            params={"classroom_id": classroom_id},
        )

    # ------------------------------------------------------------- 视频
    def watch_progress(self, cid, user_id, classroom_id, video_id) -> dict:
        return self.get(
            "/video-log/get_video_watch_progress/",
            params={
                "cid": cid, "user_id": user_id, "classroom_id": classroom_id,
                "video_type": "video", "vtype": "rate", "video_id": video_id,
                "snapshot": 1, "no_loading": "true",
            },
        )

    def heartbeat(self, heart_data: list[dict]) -> dict:
        return self.post(
            "/video-log/heartbeat/",
            body={"heart_data": heart_data},
            headers={"content-type": "application/json", "x-requested-with": "XMLHttpRequest"},
        )

    def add_study_leaf(self, classroom_id, leaf_id) -> dict:
        return self.post(
            "/c27/online_courseware/xty/instance/add_study_leaf/",
            body={"leaf_id": int(leaf_id), "classroom_id": int(classroom_id)},
        )

    # ------------------------------------------------------------- 讨论
    def unit_discussion(self, classroom_id, leaf_id, sku_id) -> dict:
        return self.get(
            "/v/discussion/v2/unit/discussion/",
            params={
                "classroom_id": classroom_id, "leaf_id": leaf_id,
                "topic_type": 0, "channel": "xt", "sku_id": sku_id,
            },
        )

    def comments(self, topic_id, offset: int = 0, limit: int = 20) -> dict:
        return self.get(
            f"/v/discussion/v2/comment/list/{topic_id}/",
            params={"offset": offset, "limit": limit},
        )

    def post_comment(self, topic_id, content: str, **extra) -> dict:
        body = {"content": content, "topic_id": int(topic_id), "anonymous": 0}
        body.update(extra)
        return self.post(f"/v/discussion/v2/comment/add/{topic_id}/", body=body)


# ---------------------------------------------------------------- 课程树解析

def flatten_chapter(tree: dict) -> list[dict]:
    """把 chapter 接口返回的嵌套结构拍平成 leaf 列表。

    雨课堂常见结构：
      {"data": {"course_info": {...},
                "chapter_list": [
                  {"id":.., "name":.., "section_list": [
                     {"id":.., "name":.., "leaf_list": [
                        {"id":.., "name":.., "leaf_type":.., "is_finish":..}
                     ]}
                  ]}
                ]}}
    """
    out: list[dict] = []
    data = tree.get("json") or {}
    data = data.get("data", data) if isinstance(data, dict) else {}

    chapters = (
        data.get("chapter_list")
        or data.get("chapters")
        or data.get("course_chapter_list")
        or []
    )
    for ch in chapters:
        ch_name = ch.get("name") or ch.get("title") or ""
        sections = ch.get("section_list") or ch.get("sections") or []
        if not sections and ch.get("leaf_list"):
            sections = [{"name": ch_name, "leaf_list": ch.get("leaf_list")}]
        for sec in sections:
            sec_name = sec.get("name") or sec.get("title") or ""
            for leaf in sec.get("leaf_list") or sec.get("leaves") or []:
                out.append({
                    "leaf_id": leaf.get("id") or leaf.get("leaf_id"),
                    "name": leaf.get("name") or leaf.get("title") or "",
                    "leaf_type": leaf.get("leaf_type"),
                    "leaf_type_id": leaf.get("leaf_type_id"),
                    "chapter": ch_name,
                    "section": sec_name,
                    "is_finish": leaf.get("is_finish"),
                    "progress": leaf.get("progress"),
                    "raw": leaf,
                })
    return out


LEAF_TYPE_NAMES = {
    0: "video", 1: "quiz", 2: "homework", 3: "exam", 4: "discussion",
    5: "doc", 6: "live", 7: "page", 8: "exercise",
}


def leaf_kind(leaf: dict) -> str:
    """把 leaf_type 归一成 video / discussion / quiz / doc / other。"""
    t = leaf.get("leaf_type")
    raw = leaf.get("raw") or {}
    if isinstance(t, str) and not t.isdigit():
        s = t.lower()
        if "video" in s:
            return "video"
        if "discuss" in s or "forum" in s:
            return "discussion"
        if "exam" in s or "quiz" in s or "homework" in s:
            return "quiz"
        if "doc" in s or "page" in s:
            return "doc"
    try:
        ti = int(t)
    except (TypeError, ValueError):
        ti = -1
    name = str(leaf.get("name") or "")
    if ti in (0,) or "视频" in name or "回放" in name:
        return "video"
    if "讨论" in name or "话题" in name:
        return "discussion"
    if any(k in name for k in ("作业", "测试", "测验", "考试", "习题")):
        return "quiz"
    return "other"
