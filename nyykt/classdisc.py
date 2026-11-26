"""班级讨论区（学习讨论）。

和视频下面的「单元讨论」不是一回事：
  - 单元讨论：每个 leaf 一个 topic，`/v/discussion/v2/topic/...`
  - 学习讨论：老师在班级讨论区发的大题，一条 topic 全班一起评

页面：https://changjiang.yuketang.cn/m/v2/discussion/{classroom_id}
列表：GET /v/discussion/v2/topics/?cid=..&limit=..&ol=1&offset=..&real=true&rank=1
发帖：POST /v/discussion/v2/comment/  （和单元讨论同一个接口，to_user 填 topic 作者）
"""

from __future__ import annotations

import re
import time

from .ai import AI, AIError
from .api import Api

LIST_PATH = "/v/discussion/v2/topics/"
COMMENT_PATH = "/v/discussion/v2/comment/"

# 只处理这两种；「学习笔记」不是讨论题
TOPIC_DISCUSSION = "学习讨论"


def html_to_text(html: str) -> str:
    """把富文本正文拍成纯文本，段落之间留空行。"""
    if not html:
        return ""
    s = html
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</p\s*>", "\n\n", s)
    s = re.sub(r"(?i)</div\s*>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def list_topics(api: Api, classroom: int, offset: int = 0, limit: int = 20) -> tuple[list[dict], int]:
    """返回 (本页 topics, 总数)。"""
    r = api.get(LIST_PATH, params={
        "cid": classroom,
        "limit": limit,
        "ol": 1,
        "offset": offset,
        "real": "true",
        "rank": 1,
        "name": "",
        "front_time": int(time.time() * 1000),
    })
    d = (r.get("json") or {}).get("data") or {}
    return (d.get("results") or []), int(d.get("count") or 0)


def all_discussions(api: Api, classroom: int, want: str = TOPIC_DISCUSSION,
                    avoid_note: bool = True, max_pages: int = 20) -> list[dict]:
    """翻完所有页，只留「学习讨论」类的 topic。

    avoid_note=True 时跳过标题或开头正文里带"笔记"的（那是学习笔记，不是讨论题）。
    """
    out: list[dict] = []
    offset = 0
    total = None
    for _ in range(max_pages):
        rows, cnt = list_topics(api, classroom, offset, 20)
        if total is None:
            total = cnt
        if not rows:
            break
        for it in rows:
            name = (it.get("topic_name") or "").strip()
            if name != want:
                continue
            if avoid_note:
                body = html_to_text((it.get("content") or {}).get("text"))
                if "笔记" in name or "笔记" in body[:200]:
                    continue
            out.append(it)
        offset += len(rows)
        if total and offset >= total:
            break
    return out


def my_comment_exists(api: Api, topic_id: int, scan: int = 50) -> bool:
    """这个 topic 下我评过没有。只认 user_id，分页往后扫。"""
    uid = api.my_user_id()
    if not uid:
        return False
    offset = 0
    for _ in range(max(1, scan // 20) + 1):
        r = api.comments(topic_id, offset, 20)
        data = (r.get("json") or {}).get("data") or {}
        rows = data.get("results") or []
        if not rows:
            break
        for it in rows:
            try:
                if int(it.get("user_id") or 0) == uid:
                    return True
            except (TypeError, ValueError):
                continue
        offset += len(rows)
    return False


SYSTEM = (
    "你在帮一个大学生写雨课堂「学习讨论」区的回复。"
    "写出来的东西必须像一个学生自己敲的：有具体判断，语气自然，不是作文模板。"
)

BAN_LIST = (
    "不要使用任何 Markdown：不要 **加粗**、# 标题、- 或 * 列表、1. 编号、`代码`、> 引用。"
    "只用普通中文段落。"
)


def build_prompt(topic_text: str, title: str = "") -> str:
    return f"""下面是老师在学习讨论区发的题目：

{topic_text[:1800]}

请写一条回复，要求：
- 直接回应题目里问的问题，给出你自己的判断，不要只复述题意
- 300 到 450 字，一到两段，不要分点、不要小标题
- {BAN_LIST}
- 语气像学生自己写的：可以有一点犹豫、口语，句子长短不齐，不要每句都对仗
- 不要用"这不仅仅是……更是……"、"在当今社会"、"随着……的发展"、"让我们一起"、"总而言之"、"深刻认识到"这类套话
- 不要"首先/其次/最后"三段式
- 可以结合一个具体的、常见的小场景讲，但别编太戏剧化的故事
- 结尾自然收住，不要升华、不要喊口号

直接输出回复正文，不要任何前缀说明。"""


def ai_write(ai: AI, topic_text: str, title: str = "", timeout: int = 120,
             temperature: float = 0.3, retry_on_slang: bool = True) -> str:
    """写一条回复。温度默认压到 0.3，别让模型写得太日常。

    生成后过一遍词表；命中 AI 腔/网络用语就用加严的 prompt 重写一次。
    """
    from . import words

    text = sanitize(ai.chat(SYSTEM, build_prompt(topic_text, title), max_tokens=1000,
                            timeout=timeout, temperature=temperature))
    hits = words.lint(text)
    if hits and retry_on_slang:
        strict = (build_prompt(topic_text, title)
                  + "\n\n补充要求：上一版用了不该用的词（"
                  + words.explain(hits) + "）。" + words.REPLACE_HINTS)
        try:
            again = sanitize(ai.chat(SYSTEM, strict, max_tokens=1000,
                                     timeout=timeout, temperature=temperature))
            if again and len(words.lint(again)) < len(hits):
                return again
        except Exception:
            pass
    return text


def sanitize(text: str) -> str:
    """清掉模型偶尔漏出来的 Markdown 和客套开场。"""
    s = (text or "").strip()
    # 去掉包裹的引号 / “好的，这是……”
    s = re.sub(r"^(好的|当然|没问题)[，,。：:]\s*", "", s)
    s = re.sub(r"^以下是[^\n]{0,30}[：:]\s*", "", s)
    # Markdown 标记
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", s)
    s = re.sub(r"(?m)^\s{0,3}[-*+]\s+", "", s)
    s = re.sub(r"(?m)^\s{0,3}\d+[.、)]\s+", "", s)
    s = s.replace("`", "")
    s = re.sub(r"(?m)^\s*>\s?", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def publish(api: Api, classroom: int, topic: dict, text: str) -> dict:
    """在一条班级讨论下发表评论。"""
    topic_id = int(topic.get("id") or 0)
    to_user = int(topic.get("user_id") or 0)
    body = {
        "to_user": to_user,
        "topic_id": topic_id,
        "content": {"text": text, "upload_images": []},
    }
    referer = f"{api.base}/m/v2/discussion/{classroom}"
    return api.post(
        COMMENT_PATH,
        body=body,
        headers={"content-type": "application/json;charset=UTF-8"},
        referer=referer,
    )


def verdict(r: dict) -> tuple[bool, str]:
    """把服务端返回翻成 (成功?, 人话原因)。"""
    if not r.get("ok"):
        return False, f"HTTP {r.get('http')}"

    js = r.get("json")
    if not isinstance(js, dict):
        # 拿不到 JSON 就退回看文本里有没有 code:0
        txt = (r.get("text") or "").replace(" ", "")
        return ('"code":0' in txt), (r.get("text") or "")[:150]

    code = js.get("code")
    msg = str(js.get("msg") or js.get("message") or "")
    if code in (0, "0") or js.get("success") is True:
        return True, "已发表"

    # 服务端把中文转义成 \uXXXX，先还原再展示
    try:
        msg = msg.encode("utf-8").decode("unicode_escape").encode("latin1").decode("utf-8")
    except Exception:
        pass
    if not msg:
        msg = str(js)[:150]
    return False, msg


def is_audit_reject(msg: str) -> bool:
    """是不是被内容审核拦了（换措辞重写可能能过）。"""
    return any(k in (msg or "") for k in ("不合法", "违规", "敏感", "请修改后重试"))


# 被审核拦下时，重写要求加这段
RETRY_HINT = (
    "\n\n补充要求：上一版被平台的自动审核拦下了。"
    "请换一种写法：只用平实的事实陈述和自己身边的具体小事，"
    "不要引用政策文件、法律条文原文，不要评价任何机构或制度，"
    "不要出现宏大叙事和口号，把篇幅控制在一段之内。"
)


def ai_write_safe(ai: AI, topic_text: str, title: str = "", timeout: int = 120,
                  temperature: float = 0.3, tries: int = 3,
                  on_try=None) -> str:
    """生成一条尽量能过的评论。被审核拦下时按提示重写。"""
    text = ai_write(ai, topic_text, title, timeout, temperature)
    for i in range(1, tries):
        if on_try:
            on_try(i, text)
        try:
            text = ai.chat(SYSTEM, build_prompt(topic_text, title) + RETRY_HINT,
                           max_tokens=1000, timeout=timeout,
                           temperature=temperature)
            text = sanitize(text)
        except Exception:
            break
    return text
