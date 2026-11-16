"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .browser import Session, goto, session
from .config import load
from .discussion import grab_outline, post_discussion
from .domsnap import page_text
from .netlog import NetLog
from .plan import Node, build_plan, collect_nodes, expand_catalog, load_plan, save_plan
from .probe import run_probe
from .utils import err, log, now_stamp, ok, step, truncate, warn
from .video import play_to_end, start_video, mark_done, pick_frame, HAS_VIDEO_JS


# ------------------------------------------------------------------ 通用

def open_node(page, node: Node, cfg) -> bool:
    if node.url:
        return goto(page, node.url)
    # 没有直链：在当前页点 data-node-id
    for attr in ("data-node-id", "data-nodeid", "data-node", "data-id"):
        try:
            loc = page.locator(f'[{attr}="{node.node_id}"]')
            if loc.count():
                loc.first.scroll_into_view_if_needed(timeout=3000)
                page.wait_for_timeout(300)
                loc.first.click(timeout=3000)
                page.wait_for_timeout(2500)
                return True
        except Exception:
            continue
    warn(f"无法打开节点 {node.node_id}（无直链且未找到可点击元素）")
    return False


def settle(page, ms: int = 2200) -> None:
    page.wait_for_timeout(ms)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass


# ------------------------------------------------------------------ login

LOGIN_COOKIES = {"sessionid", "csrftoken", "university_id", "user_id", "session_id"}

WHOAMI_JS = r"""
async () => {
  const eps = [
    '/api/v3/user/basic-info',
    '/v/course_meta/user_info',
    '/api/v3/user/info',
    '/v2/api/web/user/info',
  ];
  const out = [];
  for (const ep of eps) {
    try {
      const r = await fetch(ep, { credentials: 'include', headers: { 'Accept': 'application/json' } });
      const t = await r.text();
      out.push({ ep, status: r.status, body: t.slice(0, 600) });
    } catch (e) { out.push({ ep, status: 0, body: 'ERR ' + String(e) }); }
  }
  return out;
}
"""


def cmd_whoami(cfg, args) -> int:
    step("检查登录态")
    with session(cfg) as s:
        page = s.page()
        goto(page, cfg.base_url)
        page.wait_for_timeout(2000)
        log(f"当前 URL: {page.url}")
        res = page.evaluate(WHOAMI_JS)
        for r in res:
            print(f"\n  [{r['status']}] {r['ep']}")
            print(f"    {r['body'][:400]}")
        print()
        names = sorted({c["name"] for c in s.ctx.cookies()})
        log(f"cookie: {names}")
        print()
    return 0


BASIC_INFO_JS = r"""
async () => {
  try {
    const r = await fetch('/api/v3/user/basic-info', { credentials: 'include' });
    const t = await r.text();
    return { status: r.status, body: t };
  } catch (e) { return { status: 0, body: 'ERR ' + String(e) }; }
}
"""


def login_state(page) -> tuple[bool, str]:
    """用 basic-info 的真实返回判断，不迷信 cookie 名字。"""
    try:
        r = page.evaluate(BASIC_INFO_JS)
    except Exception as e:
        return False, f"检测异常: {e}"
    body = (r.get("body") or "").strip()
    if "UNAUTHENTICATED" in body or not body:
        return False, "UNAUTHENTICATED"
    try:
        j = json.loads(body)
    except Exception:
        return False, body[:100]
    data = j.get("data")
    if j.get("code") == 0 and data:
        return True, body[:200]
    return False, body[:120]


def cmd_login(cfg, args) -> int:
    step("登录：浏览器窗口已弹出，请在里面扫码登录")
    print("  （桌面视口 1600x1000，二维码完整显示）\n")
    s = Session(cfg)
    try:
        s.start(mobile=False)
        page = s.page()
        goto(page, cfg.base_url)
        page.wait_for_timeout(1500)

        deadline = time.time() + int(args.timeout)
        detected = False
        last_print = 0.0
        while time.time() < deadline:
            okflag, detail = login_state(page)
            if okflag:
                detected = True
                ok(f"已登录：{detail[:160]}")
                break
            if time.time() - last_print > 15:
                last_print = time.time()
                log(f"等待扫码… ({detail[:40]}) 剩余 {int(deadline - time.time())}s")
            time.sleep(2.5)

        if not detected:
            warn(f"{int(args.timeout)}s 内未确认登录")
            log(f"当前 URL: {page.url}")
            return 2

        # 验证目标页能否直通
        if args.check_url:
            log("验证目标页访问…")
            goto(page, args.check_url)
            page.wait_for_timeout(3000)
            cur = page.url or ""
            if "/web" in cur and "next=" in cur:
                warn(f"仍被重定向到登录页：{cur}")
            else:
                ok(f"目标页可访问：{truncate(cur, 80)}")
    finally:
        s.close()

    ok(f"登录态已保存 → {cfg.profile_dir}")
    log("下一步： python -m nyykt explore <课程入口URL>")
    return 0


# ------------------------------------------------------------------ probe

def cmd_probe(cfg, args) -> int:
    with session(cfg) as s:
        page = s.page()
        run_probe(page, args.url, cfg, wait_ms=args.wait, scroll=not args.no_scroll)
    return 0


# ------------------------------------------------------------------ explore

def cmd_explore(cfg, args) -> int:
    out = Path(args.out)
    with session(cfg) as s:
        page = s.page()
        net = NetLog(cfg.out_dir / f"explore-net-{now_stamp()}.jsonl")
        net.attach(page)

        step(f"打开课程入口 {args.url}")
        if not goto(page, args.url):
            err("入口打不开")
            return 2
        settle(page, 3000)

        log("尝试展开目录…")
        expand_catalog(page, cfg)
        nodes = collect_nodes(page)

        if len(nodes) < 3:
            warn("节点过少，可能是目录未展开或页面结构不同；跑一次 probe 看看")
            log("退一步：直接把页面上所有 node_id 链接收一遍")
            nodes = collect_nodes(page)

        plan = {
            "entry_url": args.url,
            "course_id": (build_plan(page, args.url, cfg).get("course_id") or ""),
            "generated_by": "nyykt explore",
            "outline_file": "",
            "nodes": [n.to_dict() for n in nodes],
        }

        # 顺便抓大纲
        outline = grab_outline(page)
        if outline:
            p = cfg.out_dir / "outline.txt"
            p.write_text(outline, encoding="utf-8")
            plan["outline_file"] = str(p)
            log(f"大纲已存 {p}（{len(outline)} 字）")

        save_plan(plan, out)
        net.close()

    kinds: dict[str, int] = {}
    for n in nodes:
        kinds[n.kind] = kinds.get(n.kind, 0) + 1
    ok(f"plan 已生成 → {out}")
    log(f"  共 {len(nodes)} 个节点：{kinds}")
    for n in nodes[:20]:
        log(f"   - [{n.kind}] {n.node_id}  {truncate(n.title, 40)}  {truncate(n.url, 60)}")
    if len(nodes) > 20:
        log(f"   … 其余 {len(nodes) - 20} 个见文件")
    return 0


# ------------------------------------------------------------------ run

def _load_outline(cfg, plan_data: dict | None) -> str | None:
    if plan_data and plan_data.get("outline_file"):
        p = Path(plan_data["outline_file"])
        if p.exists():
            return p.read_text(encoding="utf-8")
    p = cfg.out_dir / "outline.txt"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return None


def cmd_run(cfg, args) -> int:
    plan_data = None
    nodes: list[Node] = []

    if args.plan:
        p = Path(args.plan)
        if not p.exists():
            err(f"找不到 plan：{p}（先跑 explore）")
            return 2
        plan_data = json.loads(p.read_text(encoding="utf-8"))
        nodes = load_plan(p)
    elif args.url:
        nodes = [Node(node_id="0", url=args.url, title="单页", kind=args.kind)]
    else:
        err("需要 --plan 或 --url")
        return 2

    if args.only:
        want = set(args.only)
        nodes = [n for n in nodes if n.kind in want]
    nodes = [n for n in nodes if n.kind in ("video", "discussion", "quiz", "doc", "unknown")]
    if args.start:
        nodes = nodes[args.start:]
    if args.limit:
        nodes = nodes[: args.limit]

    if not nodes:
        warn("没有待处理节点")
        return 0

    outline = _load_outline(cfg, plan_data)
    posted_path = cfg.out_dir / "posted.json"
    results: list[dict] = []
    stamp = now_stamp()

    with session(cfg) as s:
        page = s.page()
        net = NetLog(cfg.out_dir / f"run-net-{stamp}.jsonl")
        net.attach(page)

        if outline is None and any(n.kind == "discussion" for n in nodes):
            entry = (plan_data or {}).get("entry_url") or args.url
            if entry:
                log("先抓课程大纲（讨论兜底用）…")
                if goto(page, entry):
                    settle(page, 2500)
                    expand_catalog(page, cfg)
                    outline = grab_outline(page)
                    if outline:
                        (cfg.out_dir / "outline.txt").write_text(outline, encoding="utf-8")
                        log(f"  大纲 {len(outline)} 字")

        total = len(nodes)
        for idx, node in enumerate(nodes, 1):
            step(f"[{idx}/{total}] [{node.kind}] {truncate(node.title or node.node_id, 50)}")
            rec = {"node_id": node.node_id, "kind": node.kind, "title": node.title,
                   "url": node.url, "ok": False, "reason": "", "detail": {}}

            if not open_node(page, node):
                rec["reason"] = "打开失败"
                results.append(rec)
                continue
            settle(page, args.settle)

            if node.kind == "video":
                frame = pick_frame(page)
                if not frame.evaluate(HAS_VIDEO_JS):
                    settle(page, 3000)
                    frame = pick_frame(page)
                frame, info = start_video(page, cfg)
                if not info.get("ok"):
                    rec["reason"] = f"未找到视频元素（{info.get('reason')}）"
                    rec["detail"] = info
                    results.append(rec)
                    log(f"  {rec['reason']}")
                    continue
                log(f"  视频就绪 duration={info.get('duration')} rate={info.get('rate')}")
                res = play_to_end(page, frame, cfg)
                rec.update(ok=res.finished, reason=res.reason,
                           detail={"watched": res.watched, "duration": res.duration,
                                   "events": res.events})
                if res.finished:
                    ok(f"  完成 {res.watched:.0f}s / {res.duration or '?'}s")
                    if cfg.get("video", "auto_next", True):
                        mark_done(page)
                else:
                    warn(f"  未完成：{res.reason}")

            elif node.kind == "discussion":
                pr = post_discussion(page, cfg, outline=outline, posted_path=posted_path,
                                     dry_run=args.dry_run)
                rec.update(ok=pr.ok, reason=pr.reason,
                           detail={"text": pr.text, "source": pr.source,
                                   "candidates": pr.candidates, "notes": pr.notes})
                if pr.ok:
                    ok(f"  讨论：{pr.reason}（来源 {pr.source}）")
                    log(f"    内容：{truncate(pr.text, 80)}")
                else:
                    warn(f"  讨论失败：{pr.reason}")

            elif node.kind == "quiz":
                rec["reason"] = "测验/作业类，跳过（需人工作答）"
                rec["ok"] = False
                log("  跳过：测验类节点")
                _write_progress(results)

            else:
                rec["reason"] = f"未知类型，跳过"
                log("  跳过：未知类型")

            results.append(rec)
            _write_progress(results)

        net.close()

    # 汇总
    stamp_path = cfg.out_dir / f"run-{stamp}.json"
    stamp_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    done = sum(1 for r in results if r["ok"])
    print()
    ok(f"跑完：{done}/{len(results)} 成功")
    for r in results:
        flag = "[OK]" if r["ok"] else "[--]"
        log(f"  {flag} [{r['kind']}] {truncate(r['title'] or r['node_id'], 36)} — {truncate(r['reason'], 50)}")
    log(f"明细：{stamp_path}")
    return 0 if done == len(results) else 1


def _write_progress(results: list[dict]) -> None:
    try:
        p = Path.cwd() / "out" / "progress.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ------------------------------------------------------------------ status

def cmd_status(cfg, args) -> int:
    p = Path(args.plan or "plan.json")
    if not p.exists():
        err(f"没有 {p}")
        return 2
    nodes = load_plan(p)
    kinds: dict[str, int] = {}
    for n in nodes:
        kinds[n.kind] = kinds.get(n.kind, 0) + 1
    print(f"plan: {p}")
    print(f"节点: {len(nodes)}  {kinds}")
    for n in nodes:
        print(f"  [{n.kind:10s}] {n.node_id:>10s}  {truncate(n.title, 50)}")
    return 0


# ------------------------------------------------------------------ api

def cmd_api(cfg, args) -> int:
    from .api import Api, flatten_chapter, leaf_kind

    with session(cfg) as s:
        page = s.page()
        url = args.page or cfg.base_url
        goto(page, url)
        page.wait_for_timeout(2500)

        api = Api(s.ctx, cfg)
        sign = args.sign or api.find_sign(page)
        log(f"sign = {sign}")

        r = api.chapter(args.classroom, sign=sign)
        log(f"chapter → HTTP {r.get('http')} ok={r.get('ok')}")
        js = r.get("json")
        if js is None:
            print((r.get("text") or "")[:1500])
            return 1

        out = cfg.out_dir / "chapter.json"
        out.write_text(json.dumps(js, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"原始响应已存 {out}")

        leaves = flatten_chapter(r)
        log(f"叶子节点 {len(leaves)} 个")
        for lf in leaves[:40]:
            log(f"  [{leaf_kind(lf):10s}] {lf['leaf_id']:>9} {truncate(lf['chapter'], 14)}/{truncate(lf['section'], 14)} {truncate(lf['name'], 34)} finish={lf['is_finish']}")
        return 0


# ------------------------------------------------------------------ boost

def cmd_boost(cfg, args) -> int:
    from .api import Api
    from .booster import boost_leaf, fetch_course, fetch_ids

    with session(cfg) as s:
        page = s.page()
        goto(page, cfg.base_url)
        page.wait_for_timeout(1200)
        api = Api(s.ctx, cfg)

        step(f"拉课程结构 classroom={args.classroom}")
        course_name, leaves = fetch_course(api, args.classroom)
        vids = [l for l in leaves if l.kind == "video"]
        if not vids:
            err("没抓到视频节点")
            return 2
        ids = fetch_ids(api, args.classroom, vids[0].leaf_id)
        ok(f"{truncate(course_name, 40)} · 视频 {len(vids)} 个 · "
           f"cid={ids.course_id} uid={ids.user_id} sku={ids.sku_id}")

        todo = [l for l in vids if not l.done]
        done_cnt = len(vids) - len(todo)
        log(f"已完成 {done_cnt}，待刷 {len(todo)}")
        if args.limit:
            todo = todo[: args.limit]
            log(f"限 {args.limit} 个")

        if args.dry_run:
            for l in todo:
                log(f"  · {l.leaf_id} {truncate(l.chapter, 10)}/{truncate(l.name, 40)} progress={l.progress}")
            return 0

        results = []
        started = time.time()
        for i, leaf in enumerate(todo, 1):
            step(f"[{i}/{len(todo)}] {leaf.leaf_id} {truncate(leaf.name, 46)}")
            try:
                r = boost_leaf(api, ids, leaf,
                               max_rounds=args.max_rounds,
                               sleep_between=args.gap,
                               verify=not args.no_verify)
            except Exception as e:
                r = {"leaf_id": leaf.leaf_id, "name": leaf.name, "ok": False,
                     "reason": f"异常: {e}", "detail": {}, "rounds": 0}
            results.append(r)
            if r["ok"]:
                d = r.get("detail") or {}
                ok(f"  {r['reason']}  watch={d.get('watch_length')}/{d.get('video_length')} rate={d.get('rate')}")
            else:
                warn(f"  {r['reason']}")
            _write_progress(results)
            time.sleep(args.sleep)

        okf = sum(1 for r in results if r["ok"])
        elapsed = time.time() - started
        print()
        ok(f"刷完 {okf}/{len(results)}，用时 {elapsed:.0f}s")
        failed = [r for r in results if not r["ok"]]
        for r in failed:
            warn(f"  失败 {r['leaf_id']} {truncate(r['name'], 40)} — {r['reason']}")

        p = cfg.out_dir / f"boost-{now_stamp()}.json"
        p.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"明细 {p}")
        return 0 if not failed else 1


# ------------------------------------------------------------------ discuss

def cmd_discuss(cfg, args) -> int:
    from .api import Api
    from .booster import fetch_course, fetch_ids
    from .forum import run_for_leaf

    posted_path = cfg.out_dir / "posted.json"
    posted: set[str] = set()
    if posted_path.exists():
        try:
            posted = set(json.loads(posted_path.read_text(encoding="utf-8")))
        except Exception:
            posted = set()

    with session(cfg) as s:
        page = s.page()
        goto(page, cfg.base_url)
        page.wait_for_timeout(1200)
        api = Api(s.ctx, cfg)

        step(f"拉课程结构 classroom={args.classroom}")
        course_name, leaves = fetch_course(api, args.classroom)
        if not leaves:
            err("没拿到课程结构")
            return 2
        ids = fetch_ids(api, args.classroom, leaves[0].leaf_id)
        ok(f"{truncate(course_name, 30)} · 单元 {len(leaves)} 个")

        targets = leaves
        if not args.include_quiz:
            targets = [l for l in leaves if l.kind != "quiz"]
        if args.start:
            targets = targets[args.start:]
        if args.limit:
            targets = targets[: args.limit]

        log(f"待处理 {len(targets)} 个单元，dry_run={args.dry_run}")
        results = []
        for i, leaf in enumerate(targets, 1):
            log(f"[{i}/{len(targets)}] {leaf.leaf_id} {truncate(leaf.name, 42)}")
            try:
                out = run_for_leaf(api, cfg, ids, leaf, posted,
                                   dry_run=args.dry_run, force=args.force)
            except Exception as e:
                results.append({"leaf_id": leaf.leaf_id, "name": leaf.name,
                                "ok": False, "reason": f"异常: {e}"})
                warn(f"  异常: {e}")
                continue

            results.append({
                "leaf_id": leaf.leaf_id, "name": leaf.name, "ok": out.ok,
                "skipped": out.skipped, "source": out.source,
                "text": out.text, "reason": out.reason,
            })
            if out.skipped:
                log(f"  {out.reason}")
            elif out.ok:
                ok(f"  {out.reason}（来源 {out.source}）: {truncate(out.text, 60)}")
            else:
                warn(f"  {out.reason}")

            posted_path.write_text(
                json.dumps(sorted(posted), ensure_ascii=False, indent=0), encoding="utf-8")
            time.sleep(args.sleep)

        good = sum(1 for r in results if r["ok"])
        skipped = sum(1 for r in results if r.get("skipped"))
        print()
        ok(f"完成 {good}/{len(results)}（其中跳过 {skipped}）")
        p = cfg.out_dir / f"discuss-{now_stamp()}.json"
        p.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"明细 {p}")
        return 0


# ------------------------------------------------------------------ report

def cmd_report(cfg, args) -> int:
    from .api import Api
    from .booster import fetch_course, fetch_ids
    from .forum import get_topic, has_my_comment

    with session(cfg) as s:
        api = Api(s.ctx, cfg)
        goto(page := s.page(), cfg.base_url)
        page.wait_for_timeout(1000)

        name, leaves = fetch_course(api, args.classroom)
        if not leaves:
            err("拿不到课程结构")
            return 2
        ids = fetch_ids(api, args.classroom, leaves[0].leaf_id)

        vids = [l for l in leaves if l.kind == "video"]
        done = [l for l in vids if l.done]
        print()
        print(f"  课程  {name or '(未命名)'}")
        print(f"  单元  {len(leaves)}  视频 {len(vids)}  作业 {sum(1 for l in leaves if l.kind == 'quiz')}")
        print(f"  视频  {len(done)}/{len(vids)} 已完成" + ("  [ALL]" if len(done) == len(vids) else ""))

        if args.skip_discussion:
            return 0

        log("统计讨论区（逐个查，稍慢）…")
        total = posted = 0
        pending = []
        for i, leaf in enumerate(leaves, 1):
            t = get_topic(api, ids, leaf.leaf_id)
            if not t:
                continue
            total += 1
            if has_my_comment(api, t):
                posted += 1
            else:
                pending.append(leaf)
            if i % 20 == 0:
                log(f"  {i}/{len(leaves)}…")

        print(f"  讨论  {posted}/{total} 个单元已发" + ("  [ALL]" if posted == total else ""))
        if pending:
            warn(f"  还差 {len(pending)} 个：")
            for l in pending[:15]:
                warn(f"    {l.leaf_id} {truncate(l.name, 40)}")
        print()
    return 0


# ------------------------------------------------------------------ main

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="nyykt", description="长江雨课堂自动化")
    ap.add_argument("-c", "--config", default="config.toml", help="配置文件")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="打开浏览器手动登录并保存 profile")
    p.add_argument("--timeout", type=int, default=300, help="等待登录的秒数")
    p.add_argument("--check-url", default="", help="登录成功后顺带验证这个 URL 能打开")
    p.set_defaults(func=cmd_login)

    sub.add_parser("whoami", help="打印当前登录态（接口返回）").set_defaults(func=cmd_whoami)

    p = sub.add_parser("probe", help="dump 页面结构，用于校准")
    p.add_argument("url")
    p.add_argument("--wait", type=int, default=4000, help="首屏等待毫秒")
    p.add_argument("--no-scroll", action="store_true")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("explore", help="遍历课程目录生成 plan.json")
    p.add_argument("url", help="课程入口 URL")
    p.add_argument("-o", "--out", default="plan.json")
    p.set_defaults(func=cmd_explore)

    p = sub.add_parser("run", help="按 plan 自动播放视频 / 发讨论")
    p.add_argument("--plan", default="plan.json")
    p.add_argument("--url", help="单页模式：直接跑这个 URL")
    p.add_argument("--kind", default="video", choices=["video", "discussion", "quiz", "doc", "unknown"])
    p.add_argument("--only", nargs="*", help="只跑这些类型，如 --only video discussion")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--settle", type=int, default=2500, help="页面打开后等待毫秒")
    p.add_argument("--dry-run", action="store_true", help="讨论只打印不发送")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="查看 plan")
    p.add_argument("--plan", default="plan.json")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("api", help="直接打接口，dump 课程章节树")
    p.add_argument("--classroom", required=True, help="classroom_id（课程 URL 里的那串数字）")
    p.add_argument("--page", default="", help="用来取 sign 的页面 URL")
    p.add_argument("--sign", default="", help="手动指定 sign")
    p.set_defaults(func=cmd_api)

    p = sub.add_parser("boost", help="接口直推视频进度（秒级）")
    p.add_argument("--classroom", required=True, help="classroom_id")
    p.add_argument("--limit", type=int, default=0, help="最多处理几个")
    p.add_argument("--dry-run", action="store_true", help="只列出待刷的，不发送")
    p.add_argument("--max-rounds", type=int, default=4)
    p.add_argument("--gap", type=float, default=1.2, help="轮与轮之间等待秒数")
    p.add_argument("--sleep", type=float, default=0.8, help="视频之间等待秒数")
    p.add_argument("--no-verify", action="store_true", help="不复查进度")
    p.set_defaults(func=cmd_boost)

    p = sub.add_parser("discuss", help="遍历讨论区，抄讨论/大纲发帖")
    p.add_argument("--classroom", required=True, help="classroom_id")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--dry-run", action="store_true", help="只打印要发的内容")
    p.add_argument("--force", action="store_true", help="自己发过也重发")
    p.add_argument("--include-quiz", action="store_true", help="作业单元也处理")
    p.add_argument("--sleep", type=float, default=1.5, help="每次之间等待秒数")
    p.set_defaults(func=cmd_discuss)

    p = sub.add_parser("report", help="汇总视频完成度与讨论覆盖")
    p.add_argument("--classroom", required=True)
    p.add_argument("--skip-discussion", action="store_true")
    p.set_defaults(func=cmd_report)

    sub.add_parser("tui", help="进入交互界面（同 python -m nyykt）").set_defaults(
        func=lambda cfg, args: __import__("nyykt.app", fromlist=["main"]).main())

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    cfg = load(args.config)
    try:
        return args.func(cfg, args)
    except KeyboardInterrupt:
        warn("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
