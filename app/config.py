"""配置与本地状态。

凭证只落在本机 data/ 目录，且 data/ 已被 .gitignore 排除。
也支持用环境变量覆盖，方便不想把密码写进文件的人。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"
CONFIG_PATH = DATA_DIR / "config.json"
STATE_PATH = DATA_DIR / "state.json"
DB_PATH = DATA_DIR / "cache.db"

_lock = threading.RLock()

DEFAULTS: dict[str, Any] = {
    "wn4k": {
        "base_url": "https://www.wn4k.com",
        # 站点网盘链接需登录可见；留空则只能浏览列表，看不到链接
        "username": "",
        "password": "",
        # 手动导入的站点 cookie（登录被验证码挡住时用）
        "cookie": "",
        # 对站点的最小请求间隔（秒），避免给站点造成压力
        "min_interval": 1.5,
    },
    "p115": {
        # 网页版 cookie 字符串，含 UID / CID / SEID / KID
        "cookie": "",
        # 默认转存目标目录 cid，0 = 根目录
        "default_cid": "0",
        "min_interval": 1.2,
    },
    "layout": {
        # 转存时的目录结构模板（在目标目录下逐级创建）
        # 可用占位符：{category} 分类  {genre} 类型  {year} 年份
        #             {title} 片名  {region} 地区  {quality} 清晰度
        "per_video_dir": True,
        "template": "{category}/{genre}/{year}/{title}",
        # 多部影片是否共用一个「批次」目录（按模板里的分类/类型/年份自动分层）
        "group_by_template": True,
    },
    "ui": {
        "page_size": 24,
        # 列表页缓存有效期（秒），避免重复请求站点
        "list_ttl": 1800,
        # 详情页缓存有效期（秒）
        "detail_ttl": 86400,
    },
    "safety": {
        # 单次转存最多提交多少个条目，防止误操作批量刷
        "max_batch": 30,
        # 单次任务最多连续转存多少个链接
        "max_links_per_batch": 60,
    },
}

ENV_MAP = {
    ("wn4k", "username"): "WN4K_USERNAME",
    ("wn4k", "password"): "WN4K_PASSWORD",
    ("wn4k", "cookie"): "WN4K_COOKIE",
    ("wn4k", "base_url"): "WN4K_BASE_URL",
    ("p115", "cookie"): "P115_COOKIE",
    ("p115", "default_cid"): "P115_CID",
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    """读取配置；文件不存在时返回默认值。"""
    with _lock:
        ensure_dirs()
        raw: dict[str, Any] = {}
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
        cfg = _deep_merge(DEFAULTS, raw)
        for (section, key), env_name in ENV_MAP.items():
            value = os.environ.get(env_name)
            if value:
                cfg.setdefault(section, {})[key] = value
        return cfg


def save_config(partial: dict[str, Any]) -> dict[str, Any]:
    """合并写入配置（只覆盖传入的字段）。"""
    with _lock:
        ensure_dirs()
        current: dict[str, Any] = {}
        if CONFIG_PATH.exists():
            try:
                current = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                current = {}
        merged = _deep_merge(current, partial)
        CONFIG_PATH.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return load_config()


def load_state() -> dict[str, Any]:
    with _lock:
        ensure_dirs()
        if not STATE_PATH.exists():
            return {}
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}


def save_state(state: dict[str, Any]) -> None:
    with _lock:
        ensure_dirs()
        STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def update_state(**kwargs: Any) -> dict[str, Any]:
    with _lock:
        state = load_state()
        state.update(kwargs)
        save_state(state)
        return state


def public_config() -> dict[str, Any]:
    """给前端用的配置视图：不含任何明文凭证。"""
    cfg = load_config()
    return {
        "wn4k": {
            "base_url": cfg["wn4k"]["base_url"],
            "has_credentials": bool(cfg["wn4k"]["username"] and cfg["wn4k"]["password"]),
            "has_cookie": bool(cfg["wn4k"].get("cookie")),
            "min_interval": cfg["wn4k"]["min_interval"],
        },
        "p115": {
            "default_cid": cfg["p115"]["default_cid"],
            "has_cookie": bool(cfg["p115"].get("cookie")),
            "min_interval": cfg["p115"]["min_interval"],
        },
        "layout": cfg.get("layout", DEFAULTS["layout"]),
        "ui": cfg["ui"],
        "safety": cfg["safety"],
    }