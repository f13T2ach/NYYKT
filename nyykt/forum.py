"""讨论区（接口版）：遍历每个单元的讨论，抄一条现成的发上去；没有就抄大纲一句。

实测接口：
    GET  /v/discussion/v2/unit/discussion/?classroom_id=&leaf_id=&topic_type=0&channel=xt&sku_id=
         → data.id = topic_id, data.user_id = to_user, data.commented = 总评论数,
           data.user_comment_num = 自己发过几条
    GET  /v/discussion/v2/comment/list/{topic_id}/?offset=&limit=
         → data.results[].content.text
    POST /v/discussion/v2/comment/
         {"to_user":<unit.user_id>,"topic_id":<id>,"content":{"text":..,"upload_images":[]}}
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any

from .api import Api
from .booster import CourseIds, Leaf
from .config import Config
from .utils import clean, cjk_ratio, log, ok, pick_sentence, squeeze, truncate, warn

# 抄写时要避开的文本特征（老师口吻、系统文案）
BAD_PATTERNS = [
    re.compile(r"^(?:老师|助教|教师|管理员)"),
    re.compile(r"同学你好|同学们好|请大家|本节课|作业要求|评分标准"),
    re.compile(r"^\d+[\.、]"),           # 纯编号
    re.compile(r"^[\[【（(]"),           # 系统标签开头
]


@dataclass
class Topic:
    leaf_id: int
    topic_id: int
    to_user: int
    commented: int = 0
    my_count: int = 0
    score: float = 0.0
    raw: dict = field(default_factory=dict)


@dataclass
class PostOutcome:
    ok: bool = False
    text: str = ""
    source: str = ""
    skipped: bool = False
    reason: str = ""
    candidates: int = 0


# ------------------------------------------------------------------ 读取

def get_topic(api: Api, ids: CourseIds, leaf_id: int) -> Topic | None:
    r = api.unit_discussion(ids.classroom_id, leaf_id, ids.sku_id)
    data = (r.get("json") or {}).get("data")
    if not isinstance(data, dict):
        return None
    tid = data.get("id")
    if not tid:
        return None
    return Topic(
        leaf_id=leaf_id,
        topic_id=int(tid),
        to_user=int(data.get("user_id") or 0),
        commented=int(data.get("commented") or 0),
        my_count=int(data.get("user_comment_num") or 0),
        score=float(data.get("score") or 0),
        raw=data,
    )


def list_comments(api: Api, topic_id: int, offset: int = 0, limit: int = 20) -> list[dict]:
    r = api.comments(topic_id, offset, limit)
    data = (r.get("json") or {}).get("data") or {}
    return data.get("results") or []


def comment_text(item: dict) -> str:
    c = item.get("content") or {}
    return clean(c.get("text") or c.get("app_text") or "")


def has_my_comment(api: Api, topic: Topic, scan: int = 20) -> bool:
    """自己发过没有 —— 只看 user_id，user_comment_num 字段不可信。"""
    uid = api.my_user_id()
    if not uid:
        return False
    items = list_comments(api, topic.topic_id, 0, scan)
    for it in items:
        try:
            if int(it.get("user_id") or 0) == uid:
                return True
        except (TypeError, ValueError):
            continue
    return False


# ------------------------------------------------------------------ 素材

def usable(text: str, cfg: Config, me_ids: set[int] | None = None, author: int | None = None) -> bool:
    d = cfg.raw["discussion"]
    if not text:
        return False
    if len(text) < int(d.get("min_len", 15)) or len(text) > int(d.get("max_len", 220)):
        return False
    if cjk_ratio(text) < 0.55:
        return False
    for kw in d.get("avoid_keywords", []):
        if kw and kw in text:
            return False
    for pat in BAD_PATTERNS:
        if pat.search(text):
            return False
    return True


def pick_from_peers(api: Api, topic: Topic, cfg: Config, posted: set[str]) -> str | None:
    """从别人的讨论里挑一条。随机偏移，避免每次都抄同一条。"""
    d = cfg.raw["discussion"]
    total = max(1, min(topic.commented, 200))
    offsets = [random.randint(0, max(0, total - 10))]
    if total > 20:
        offsets += [random.randint(0, max(0, total - 10)), 0]
    for off in offsets:
        items = list_comments(api, topic.topic_id, off, 10)
        random.shuffle(items)
        for it in items:
            t = comment_text(it)
            if squeeze(t) in posted:
                continue
            if usable(t, cfg):
                return t
    return None


def _collect_strings(obj: Any, out: list[str], depth: int = 0) -> None:
    if depth > 7:
        return
    if isinstance(obj, str):
        if len(obj) >= 8:
            out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_strings(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _collect_strings(v, out, depth + 1)


def outline_full(api: Api, ids: CourseIds, leaf: Leaf) -> str:
    """讲稿/知识点全文，给 AI 当上下文。"""
    for path in (
        f"/c27/online_courseware/leaf_lecture_note/{leaf.leaf_id}/",
        f"/c27/online_courseware/leaf_knowledge/{leaf.leaf_id}/",
    ):
        r = api.get(path, params={"classroom_id": ids.classroom_id})
        js = r.get("json")
        if not js:
            continue
        chunks: list[str] = []
        _collect_strings(js, chunks)
        text = "\n".join(chunks)
        if text.strip():
            return text
    return ""


def outline_sentence(api: Api, ids: CourseIds, leaf: Leaf) -> str | None:
    """兜底：讲稿 / 知识导引里挑一句。"""
    text = outline_full(api, ids, leaf)
    if text.strip():
        s = pick_sentence(text, lo=20, hi=110)
        if s:
            return s
    # 最后兜底：用单元名本身造一句
    if leaf.name:
        return f"这一节讲的是{leaf.name}，看完之后对这部分内容理解更清楚了一些。"
    return None


# ------------------------------------------------------------------ 发布

def publish(api: Api, ids: CourseIds, leaf: Leaf, topic: Topic, text: str) -> dict:
    body = {
        "to_user": topic.to_user,
        "topic_id": topic.topic_id,
        "content": {"text": text, "upload_images": []},
    }
    referer = (
        f"{api.base}/ai-workspace/lms-graph-mobile/{ids.classroom_id}"
        f"/video/{leaf.leaf_id}" + (f"?node_id={leaf.section_id}" if leaf.section_id else "")
    )
    return api.post(
        "/v/discussion/v2/comment/",
        body=body,
        headers={"content-type": "application/json;charset=UTF-8"},
        referer=referer,
    )


def run_for_leaf(
    api: Api,
    cfg: Config,
    ids: CourseIds,
    leaf: Leaf,
    posted: set[str],
    dry_run: bool = False,
    force: bool = False,
    strategy: str = "mixed",
) -> PostOutcome:
    """讨论只走「抄现成的」路线：他人讨论 → 大纲/讲稿兜底。

    AI 不参与讨论生成（由用户明确要求）；AI 只用于作业作答。
    """
    out = PostOutcome()

    topic = get_topic(api, ids, leaf.leaf_id)
    if not topic:
        out.reason = "该单元没有讨论区"
        return out

    if not force and has_my_comment(api, topic):
        out.skipped = True
        out.ok = True
        out.reason = "已发过（跳过）"
        return out

    text = None
    if strategy in ("mixed", "peer"):
        text = pick_from_peers(api, topic, cfg, posted)
        if text:
            out.source = "他人讨论"
    if not text and strategy in ("mixed", "outline"):
        if cfg.get("discussion", "fallback_outline", True):
            s = outline_sentence(api, ids, leaf)
            if s:
                text, out.source = s, "讲稿/大纲"
    if not text:
        out.reason = "找不到可抄的讨论，也没有可用大纲"
        return out

    text = clean(text)
    out.text = text
    out.candidates = topic.commented

    if dry_run:
        out.ok = True
        out.reason = "dry-run"
        return out

    r = publish(api, ids, leaf, topic, text)
    js = r.get("json") or {}
    if r.get("ok") and (js.get("success") is not False) and js.get("code", 0) == 0:
        out.ok = True
        out.reason = "已发布"
        posted.add(squeeze(text))
    else:
        out.reason = f"发布失败 HTTP {r.get('http')} {(r.get('text') or '')[:120]}"
    return out
