"""配置加载（TOML），带默认值和缺省合并。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG: dict = {
    "server": {
        "base_url": "https://changjiang.yuketang.cn",
    },
    "browser": {
        "profile_dir": ".profile",
        "headless": False,
        "channel": "",          # 留空 = playwright 自带 chromium；可填 chrome / msedge
        "mobile": True,
        "slow_mo": 0,
        "locale": "zh-CN",
    },
    "video": {
        "playback_rate": 1.0,   # >1 有被检测风险，先用 1.0
        "muted": True,
        "keepalive_seconds": 25,
        "max_wall_seconds": 5400,
        "turbo_seek": False,     # 直接改 currentTime 快进（风险自负）
        "turbo_step_seconds": 30,
        "finish_ratio": 0.995,   # 进度达到该比例视为看完
        "auto_next": True,       # 看完自动点下一节
    },
    "discussion": {
        "enabled": True,
        "min_len": 15,
        "max_len": 220,
        "max_candidates": 40,
        "avoid_keywords": ["老师", "助教", "官方", "置顶", "公告", "课程通知"],
        "fallback_outline": True,
        "dry_run": False,        # True = 只打印要发的内容，不点发送
        "submit_wait_seconds": 8,
    },
    "catalog": {
        "expand_timeout_seconds": 6,
        "max_nodes": 300,
    },
    "paths": {
        "out_dir": "out",
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    raw: dict = field(default_factory=lambda: dict(DEFAULT_CONFIG))

    def get(self, section: str, key: str, default=None):
        return self.raw.get(section, {}).get(key, default)

    @property
    def base_url(self) -> str:
        return self.raw["server"].get("base_url", "").rstrip("/")

    @property
    def out_dir(self) -> Path:
        p = Path(self.raw["paths"]["out_dir"])
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def profile_dir(self) -> Path:
        return Path(self.raw["browser"]["profile_dir"]).resolve()


def load(path: str | Path | None = None) -> Config:
    cfg_path = Path(path or "config.toml")
    data: dict = {}
    if cfg_path.exists():
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh)
    return Config(raw=_merge(DEFAULT_CONFIG, data))
