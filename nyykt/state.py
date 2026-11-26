"""TUI 状态持久化（state.json）。

注意：AI 的 api_key 只存在这个文件里，绝不写进代码。
"""

from __future__ import annotations

import json
from pathlib import Path

STATE_FILE = Path("state.json")

DEFAULT = {
    "classroom": "",
    "server": "长江雨课堂",
    "automation": {
        "enabled": False,
        "rate": 2.0,
        "click_delay": 2.0,
        "segment": 12,
    },
    "discussion": {
        "auto": True,
        "auto_scan": False,      # 讨论数目自动统计，默认关（扫全课程比较慢）
        "strategy": "mixed",     # mixed | peer | outline | ai
        "include_quiz": False,
        "class_ai": True,        # 学习讨论用 AI 生成（默认开）
        "avoid_note": True,      # 回避含「笔记」的帖子
    },
    "homework": {
        "auto_submit": False,    # 默认关：答完停住，等你在选单里确认
        "show_browser": False,   # 默认静默；开了会弹出窗口
    },
    "ui": {
        "out_lines": 8,          # 输出区行数，8-30
        "hide_from_capture": False,   # 防止监视此窗口（需管理员）
    },
    "ai": {
        "enabled": False,
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "api_key": "",
        "temperature": 0.3,
        "protocol": "openai",
    },
}

STRATEGIES = ["mixed", "peer", "outline"]
STRATEGY_NAMES = {
    "mixed": "混合（优先抄他人，没有则大纲）",
    "peer": "只抄他人讨论",
    "outline": "只用大纲/讲稿",
}

PROVIDERS = ["deepseek", "openai", "anthropic", "custom"]


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_state(path: Path | None = None) -> dict:
    p = path or STATE_FILE
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    st = _merge(DEFAULT, data)
    # 迁移：旧版默认温度 0.8 偏放飞，新版默认 0.3
    ai = st.setdefault("ai", {})
    try:
        if abs(float(ai.get("temperature", 0.3)) - 0.8) < 1e-6:
            ai["temperature"] = 0.3
    except (TypeError, ValueError):
        ai["temperature"] = 0.3
    return st


def save_state(state: dict, path: Path | None = None) -> None:
    p = path or STATE_FILE
    try:
        p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
