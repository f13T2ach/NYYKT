"""视频进度引擎（接口直推）。

原理（2026-11 实测）：
    POST /video-log/heartbeat/  上报 {"heart_data":[{cp,fp,tp,d,et,ts,...}]}
    服务端维护 watch_length += sum(tp 增量)，并在 watch_length >= video_length 时
    把 completed 置 1。客户端上报的 cp/ts 完全被信任，所以只要把"观看轨迹"补满即可。

实测：
    起始 watch_length=283 / video_length=327 → 发 17 条 playing（tp 累加到 327）
    → watch_length=328, rate=1.0, completed=1

注意：
    - "ended" 不是合法事件（服务端返回 Partial data anomaly），只用 playing/loadstart/loadeddata
    - 单次请求的 tp 会被部分吞掉（实测 330 → 只加 272），所以默认发两遍轨迹
"""

from __future__ import annotations

import json
import random
import string
import time
from dataclasses import dataclass, field

from .api import Api, flatten_chapter, leaf_kind
from .utils import log, ok, truncate, warn

VALID_EVENTS = ("loadstart", "loadeddata", "playing", "pause", "seeking")
CDN_HOST = "ali-cdn.xuetangx.com"


@dataclass
class CourseIds:
    classroom_id: int
    course_id: int
    user_id: int
    sku_id: int


@dataclass
class Leaf:
    leaf_id: int
    name: str
    kind: str
    section_id: int | None = None
    chapter: str = ""
    section: str = ""
    progress: float | None = None
    ccid: str = ""
    duration: float = 0.0
    is_discuss: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return isinstance(self.progress, (int, float)) and self.progress >= 1.0

    def url(self, classroom_id: int) -> str:
        base = "https://changjiang.yuketang.cn/ai-workspace/lms-graph-mobile"
        if self.section_id:
            return f"{base}/{classroom_id}/video/{self.leaf_id}?node_id={self.section_id}&is_chapter=1"
        return f"{base}/{classroom_id}/video/{self.leaf_id}"


# ------------------------------------------------------------------ 拉取

def fetch_ids(api: Api, classroom_id: int, hint_leaf: int) -> CourseIds:
    r = api.leaf_info(classroom_id, hint_leaf)
    d = (r.get("json") or {}).get("data") or {}
    if not d.get("course_id"):
        raise RuntimeError(f"leaf_info 拿不到课程信息: {(r.get('text') or '')[:200]}")
    return CourseIds(
        classroom_id=int(classroom_id),
        course_id=int(d["course_id"]),
        user_id=int(d["user_id"]),
        sku_id=int(d["sku_id"]),
    )


def fetch_course(api: Api, classroom_id: int) -> tuple[str, list[Leaf]]:
    """course/progress 一次拿到 树 + 每个 leaf 的完成度。"""
    r = api.get(
        "/c27/online_courseware/schedule/course/progress/",
        params={"classroom_id": classroom_id},
    )
    js = r.get("json") or {}
    data = js.get("data") or {}
    if not data:
        raise RuntimeError(f"拿不到课程结构: HTTP {r.get('http')} {(r.get('text') or '')[:200]}")

    course_name = data.get("course_name") or ""
    leaves: list[Leaf] = []

    def add_node(lf: dict, chapter: str, section: str, section_id):
        kind = leaf_kind({"leaf_type": lf.get("leaf_type"), "name": lf.get("name")})
        prog = lf.get("progress")
        if isinstance(prog, dict):
            prog = prog.get("user_score")
        leaves.append(Leaf(
            leaf_id=int(lf.get("id") or 0),
            name=lf.get("name") or "",
            kind=kind,
            section_id=section_id,
            chapter=chapter,
            section=section,
            progress=prog if isinstance(prog, (int, float)) else None,
            extra={"leaf_type": lf.get("leaf_type")},
        ))

    for ch in data.get("course_progress") or []:
        ch_name = ch.get("name") or ""
        for node in ch.get("section_leaf_list") or []:
            if "leaf_list" in node:
                for lf in node["leaf_list"]:
                    add_node(lf, ch_name, node.get("name") or "", node.get("id"))
            else:
                add_node(node, ch_name, node.get("name") or "", node.get("section_id"))
    return course_name, leaves


def fill_video_meta(api: Api, classroom_id: int, leaf: Leaf) -> None:
    """补 ccid / duration / is_discuss。"""
    r = api.leaf_info(classroom_id, leaf.leaf_id)
    d = (r.get("json") or {}).get("data") or {}
    ci = d.get("content_info") or {}
    media = ci.get("media") or {}
    leaf.ccid = media.get("ccid") or media.get("ccurl") or ""
    leaf.duration = float(media.get("duration") or 0)
    leaf.is_discuss = bool(ci.get("is_discuss"))
    if not leaf.name:
        leaf.name = d.get("name") or ""


# ------------------------------------------------------------------ 心跳

def _rand_tag(n: int = 5) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def make_beat(ids: CourseIds, leaf_id: int, ccid: str, cp: float, fp: float,
              tp: float, d: float, et: str, sq: int, ts_ms: int, pg: str) -> dict:
    return {
        "i": 5, "et": et, "p": "web", "n": CDN_HOST, "lob": "ykt",
        "cp": round(cp, 1), "fp": round(fp, 1), "tp": round(tp, 1), "sp": 1,
        "ts": str(ts_ms),
        "u": ids.user_id, "uip": "", "c": ids.course_id, "v": leaf_id,
        "skuid": ids.sku_id, "classroomid": ids.classroom_id,
        "cc": ccid, "d": round(d, 1), "pg": pg, "sq": sq,
        "t": "video", "cards_id": 0, "slide": 0, "v_url": "",
    }


def build_beats(ids: CourseIds, leaf_id: int, ccid: str, vlen: float,
                passes: int = 2, step: float | None = None) -> list[dict]:
    """构造心跳批次。

    实测（2026-11）服务端只认 et="heartbeat" 的记账：playing 只报位置，tp 恒为 0，
    真正累加 watch_length 的是 heartbeat，且按 ts 差值计算——所以 ts 可以按
    5 秒一条递增，请求瞬间发完即可，不必真的等。
    """
    if step is None:
        step = 5.0                      # 服务端真实播放时也是 5 秒一条
    pg = f"{leaf_id}_{_rand_tag()}"
    t = int(time.time() * 1000)
    beats: list[dict] = []
    sq = 1
    beats.append(make_beat(ids, leaf_id, ccid, 0, 0, 0, vlen, "loadstart", sq, t, pg)); sq += 1
    beats.append(make_beat(ids, leaf_id, ccid, 0, 0, 0, vlen, "loadeddata", sq, t + 300, pg)); sq += 1
    t += 1000
    for _ in range(max(1, passes)):
        cp = 0.0
        while cp < vlen:
            nxt = min(cp + step, vlen)
            beats.append(make_beat(ids, leaf_id, ccid, nxt, 0, 0, vlen, "heartbeat", sq, t, pg))
            sq += 1
            t += int(step * 1000)
            cp = nxt
    return beats


def progress_of(api: Api, ids: CourseIds, leaf_id: int) -> dict:
    r = api.watch_progress(ids.course_id, ids.user_id, ids.classroom_id, leaf_id)
    d = (r.get("json") or {}).get("data") or {}
    return d.get(str(leaf_id)) or {}


def warm_up(api: Api, ids: CourseIds, leaf: Leaf) -> dict:
    """第一次心跳：让服务端去探测并写入 video_length。

    实测：给一个从未打开过的视频发 loadstart+loadeddata+playing，
    之后 watch_progress 才出现 video_length（552.0）。
    """
    pg = f"{leaf.leaf_id}_{_rand_tag()}"
    t = int(time.time() * 1000)
    beats = [
        make_beat(ids, leaf.leaf_id, leaf.ccid, 0, 0, 0, 0, "loadstart", 1, t, pg),
        make_beat(ids, leaf.leaf_id, leaf.ccid, 0, 0, 0, 0, "loadeddata", 2, t + 300, pg),
        make_beat(ids, leaf.leaf_id, leaf.ccid, 8, 0, 8, 8, "playing", 3, t + 8000, pg),
    ]
    r = api.heartbeat(beats)
    time.sleep(2.5)
    return r


# ------------------------------------------------------------------ 主入口

# ------------------------------------------------------------------ 防封节流

@dataclass
class Pace:
    """防封自动化：不瞬间发完，按"理论最快播放时间"慢慢推。

    理论耗时 = 视频时长 / 倍速 + 人手点击间隔
    """
    enabled: bool = False
    rate: float = 2.0          # 倍速（服务端允许的功能，用它把时间压一半）
    click_delay: float = 2.0   # 每节开始前"人手点一下"的停顿
    segment: float = 12.0      # 每段推送间隔（秒）

    def planned(self, vlen: float) -> float:
        return max(1.0, vlen) / max(1.0, self.rate) + self.click_delay


def boost_leaf_paced(
    api: Api,
    ids: CourseIds,
    leaf: Leaf,
    pace: Pace,
    on_tick=None,
    cancel=None,
    max_rounds: int = 4,
) -> dict:
    """按节流节奏刷完一个视频。on_tick(dict) 用于刷新 UI。"""
    res = {"leaf_id": leaf.leaf_id, "name": leaf.name, "ok": False,
           "rounds": 0, "detail": {}, "reason": "", "paced": True}

    def _cancelled() -> bool:
        return bool(cancel and cancel.is_set())

    st = progress_of(api, ids, leaf.leaf_id)
    if st.get("completed") == 1:
        res.update(ok=True, reason="已完成（跳过）", detail=st)
        return res

    if not leaf.ccid or leaf.duration <= 0:
        fill_video_meta(api, ids.classroom_id, leaf)
    if not leaf.ccid:
        res["reason"] = "拿不到 ccid"
        return res

    vlen = float(st.get("video_length") or leaf.duration or 0)
    if vlen <= 0:
        warm_up(api, ids, leaf)
        st = progress_of(api, ids, leaf.leaf_id)
        vlen = float(st.get("video_length") or 0)
    if vlen <= 0:
        res["reason"] = "服务端没给出视频时长"
        return res

    total_plan = pace.planned(vlen)
    per_seg = max(1.0, pace.segment * max(1.0, pace.rate))
    n_seg = max(1, int(-(-vlen // per_seg)))

    started = time.time()
    done_cp = 0.0
    pg = f"{leaf.leaf_id}_{_rand_tag()}"
    ts = int(time.time() * 1000)
    sq = 1

    # 开场两条
    api.heartbeat([
        make_beat(ids, leaf.leaf_id, leaf.ccid, 0, 0, 0, 0, "loadstart", sq, ts, pg),
        make_beat(ids, leaf.leaf_id, leaf.ccid, 0, 0, 0, 0, "loadeddata", sq + 1, ts + 300, pg),
    ])
    sq += 2

    for seg in range(n_seg):
        if _cancelled():
            res["reason"] = "已中止"
            return res

        lo = done_cp
        hi = min(done_cp + per_seg, vlen)
        beats = []
        step = max(1.0, (hi - lo) / 6.0)
        cp = lo
        while cp < hi - 0.01:
            nxt = min(cp + step, hi)
            ts += int(step / max(1.0, pace.rate) * 1000)
            beats.append(make_beat(ids, leaf.leaf_id, leaf.ccid, nxt, cp, nxt, nxt - cp,
                                   "playing", sq, ts, pg))
            sq += 1
            cp = nxt
        api.heartbeat(beats)
        done_cp = hi

        # 到下一段的时刻
        target = (seg + 1) * pace.segment + (pace.click_delay if seg == 0 else 0)
        eta = max(0.0, target - (time.time() - started))
        if on_tick:
            on_tick({
                "cp": done_cp, "vlen": vlen, "eta": eta,
                "elapsed": time.time() - started, "planned": total_plan,
                "seg": seg + 1, "nseg": n_seg,
            })
        # 分片睡，随时可取消
        end = time.time() + eta
        while time.time() < end:
            if _cancelled():
                res["reason"] = "已中止"
                return res
            time.sleep(min(0.2, max(0.0, end - time.time())))

    # 收尾：补满（真实进度可能被吞一点）
    for rnd in range(1, max_rounds + 1):
        time.sleep(0.8)
        st = progress_of(api, ids, leaf.leaf_id)
        res["detail"] = st
        if st.get("completed") == 1:
            res.update(ok=True, reason=f"完成（节流 {pace.rate}x）")
            return res
        if _cancelled():
            res["reason"] = "已中止"
            return res
        build = build_beats(ids, leaf.leaf_id, leaf.ccid, vlen, passes=2)
        api.heartbeat(build)
        res["rounds"] = rnd

    res["reason"] = f"节流后仍未完成（watch={res['detail'].get('watch_length')}/{vlen}）"
    return res


def probe_duration_from_page(page, classroom_id, leaf_id) -> float:
    """有些视频 leaf_info 里 duration=0，只能打开页面问 <video>。"""
    try:
        url = (f"https://changjiang.yuketang.cn/ai-workspace/lms-graph-mobile/"
               f"{classroom_id}/video/{leaf_id}")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        for _ in range(8):
            page.wait_for_timeout(1200)
            d = page.evaluate("""() => {
                const v = document.querySelector('video');
                return v && isFinite(v.duration) ? v.duration : 0;
            }""")
            if d and float(d) > 0:
                return float(d)
        return 0.0
    except Exception:
        return 0.0


def boost_leaf(api: Api, ids: CourseIds, leaf: Leaf, max_rounds: int = 4,
               sleep_between: float = 1.2, verify: bool = True,
               page=None) -> dict:
    """把一个 leaf 刷到 completed=1。"""
    res = {"leaf_id": leaf.leaf_id, "name": leaf.name, "ok": False,
           "rounds": 0, "detail": {}, "reason": ""}

    st = progress_of(api, ids, leaf.leaf_id)
    if st.get("completed") == 1:
        res.update(ok=True, reason="已完成（跳过）", detail=st)
        return res

    # 没打开过的视频，watch_progress 里没有 video_length，回退到 leaf_info
    if not leaf.ccid or leaf.duration <= 0:
        fill_video_meta(api, ids.classroom_id, leaf)

    if not leaf.ccid:
        res["reason"] = "拿不到 ccid"
        return res

    vlen = float(st.get("video_length") or 0)
    if vlen <= 0:
        vlen = float(leaf.duration or 0)

    # 服务端还没这个视频的时长 → 先发一条预热心跳触发探测
    if vlen <= 0:
        res["detail"]["warmup"] = True
        warm_up(api, ids, leaf)
        st = progress_of(api, ids, leaf.leaf_id)
        vlen = float(st.get("video_length") or 0)

    # 有些视频 leaf_info 的 duration 就是 0，预热也不给，只能开页面问 <video>
    if vlen <= 0 and page is not None:
        d = probe_duration_from_page(page, ids.classroom_id, leaf.leaf_id)
        if d > 0:
            vlen = d
            leaf.duration = d
            res["detail"]["probed_from_page"] = d

    if vlen <= 0:
        res["reason"] = "服务端没给出视频时长（预热后仍为 0）"
        return res

    try:
        api.add_study_leaf(ids.classroom_id, leaf.leaf_id)
    except Exception:
        pass

    for rnd in range(1, max_rounds + 1):
        res["rounds"] = rnd
        beats = build_beats(ids, leaf.leaf_id, leaf.ccid, vlen, passes=1)
        # 长视频会生成上百条，分批发，别一次塞爆
        chunk = 80
        sent = 0
        for i in range(0, len(beats), chunk):
            part = beats[i:i + chunk]
            r = api.heartbeat(part)
            sent += len(part)
            if not r.get("ok"):
                res["detail"]["bad_http"] = r.get("http")
                break
            js = r.get("json")
            if isinstance(js, dict) and js.get("message"):
                res["detail"]["bad_msg"] = str(js.get("message"))
                break
            if i + chunk < len(beats):
                time.sleep(0.4)

        if not verify:
            res.update(ok=True, reason="已上报（未校验）", detail={"beats": sent})
            return res

        time.sleep(sleep_between)
        st = progress_of(api, ids, leaf.leaf_id)
        res["detail"] = st
        if st.get("completed") == 1:
            res.update(ok=True, reason=f"完成（{rnd} 轮）")
            return res
        if bad:
            log(f"    轮 {rnd} 服务端提示: {bad}")

    res["reason"] = f"{res['rounds']} 轮后仍未完成（watch={res['detail'].get('watch_length')}/{vlen}）"
    return res
