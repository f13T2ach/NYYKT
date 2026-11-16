"""课程（作用域）列表。

作用域就是一门课：刷课、讨论、学习讨论都在选中的那门课里跑。
存储仍然只存 classroom_id，别的字段随时能重新拉。

接口：
  GET /v/course_meta/learning_list/   我听的课
  GET /v/course_meta/teaching_list/   我教的课
"""

from __future__ import annotations

from .api import Api

LEARN_PATH = "/v/course_meta/learning_list/"
TEACH_PATH = "/v/course_meta/teaching_list/"


def _rows(api: Api, path: str) -> list[dict]:
    try:
        r = api.get(path)
        return (r.get("json") or {}).get("data") or []
    except Exception:
        return []


def fetch_my_courses(api: Api, include_teach: bool = True) -> list[dict]:
    """返回 [{classroom_id, course_name, classroom_name, teacher, role, teach}]。

    我听的课在前，我教的课在后；按 classroom_id 去重。
    """
    out: list[dict] = []
    seen: set[int] = set()

    def push(rows: list[dict], teach: bool) -> None:
        for it in rows:
            try:
                cid = int(it.get("classroom_id") or 0)
            except (TypeError, ValueError):
                continue
            if not cid or cid in seen:
                continue
            seen.add(cid)
            teacher = (it.get("teacher") or {}).get("name") or ""
            out.append({
                "classroom_id": cid,
                "course_name": (it.get("course_name") or "").strip(),
                "classroom_name": (it.get("classroom_name") or "").strip(),
                "teacher": teacher,
                "role": it.get("role"),
                "teach": teach,
            })

    push(_rows(api, LEARN_PATH), False)
    if include_teach:
        push(_rows(api, TEACH_PATH), True)
    return out


def label_of(c: dict, width: int = 0) -> str:
    """一行菜单文字。宽度够就带上班级名和老师。"""
    name = c.get("course_name") or f"classroom {c.get('classroom_id')}"
    extra = []
    if c.get("classroom_name"):
        extra.append(c["classroom_name"])
    if c.get("teacher"):
        extra.append(c["teacher"])
    tail = "  ".join(extra)
    text = f"{name}" + (f"  ·  {tail}" if tail else "")
    if width and len(text) > width:
        text = text[: max(4, width - 1)] + "…"
    return text
