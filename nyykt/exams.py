"""可用的考试列表。

接口：GET /api/v3/classroom/on-lesson-upcoming-exam
返回 data.upcomingExam[]，每条：
    {id: exam_id, classroom_id, title, classroom_name, start_time, end_time, type}

这是「最近一周有考试的课程」那个入口背后的接口，按时间窗滚动，
没考试的时候就返回空数组。
"""

from __future__ import annotations

import time

from .api import Api

PATH = "/api/v3/classroom/on-lesson-upcoming-exam"


def fetch_exams(api: Api) -> list[dict]:
    """返回 [{exam_id, classroom_id, title, classroom_name, start_time}]。"""
    try:
        r = api.get(PATH, params={"front_time": int(time.time() * 1000)})
        data = (r.get("json") or {}).get("data") or {}
    except Exception:
        return []

    out: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for it in data.get("upcomingExam") or []:
        if (it.get("type") or "exam") != "exam":
            continue
        try:
            eid = int(it.get("id") or 0)
            cid = int(it.get("classroom_id") or 0)
        except (TypeError, ValueError):
            continue
        if not eid or not cid or (eid, cid) in seen:
            continue
        seen.add((eid, cid))
        out.append({
            "exam_id": eid,
            "classroom_id": cid,
            "title": (it.get("title") or "").strip() or f"考试 {eid}",
            "classroom_name": (it.get("classroom_name") or "").strip(),
            "start_time": it.get("start_time") or 0,
        })
    return out


def label_of(e: dict, width: int = 0) -> str:
    """一行菜单文字，例如「期中测验  ·  <班级名>」。"""
    text = e.get("title") or f"考试 {e.get('exam_id')}"
    extra = []
    if e.get("classroom_name"):
        extra.append(e["classroom_name"])
    if extra:
        text += "  ·  " + "  ".join(extra)
    if width and len(text) > width:
        text = text[: max(4, width - 1)] + "…"
    return text
