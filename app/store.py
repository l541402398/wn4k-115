"""轻量本地缓存（SQLite）。

目的：让「浏览过的页面」不再重复打站点。列表页 TTL 短（默认 30 分钟），
详情页 TTL 长（默认 1 天）。这也是对站点友好的一部分。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from .config import DB_PATH, ensure_dirs

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            ensure_dirs()
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cache (
                    key        TEXT PRIMARY KEY,
                    value      TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    ttl        REAL NOT NULL
                )
                """
            )
            _conn.execute(
                """
                CREATE TABLE IF NOT EXISTS history (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    action     TEXT NOT NULL,
                    target_cid TEXT,
                    payload    TEXT NOT NULL,
                    ok         INTEGER NOT NULL,
                    detail     TEXT
                )
                """
            )
            _conn.commit()
        return _conn


def get(key: str) -> Any | None:
    conn = _connect()
    with _lock:
        row = conn.execute(
            "SELECT value, created_at, ttl FROM cache WHERE key = ?", (key,)
        ).fetchone()
    if not row:
        return None
    value, created_at, ttl = row
    if ttl and (time.time() - created_at) > ttl:
        delete(key)
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def put(key: str, value: Any, ttl: float) -> None:
    conn = _connect()
    with _lock:
        conn.execute(
            "INSERT OR REPLACE INTO cache (key, value, created_at, ttl) VALUES (?, ?, ?, ?)",
            (key, json.dumps(value, ensure_ascii=False), time.time(), float(ttl)),
        )
        conn.commit()


def delete(key: str) -> None:
    conn = _connect()
    with _lock:
        conn.execute("DELETE FROM cache WHERE key = ?", (key,))
        conn.commit()


def purge_expired() -> int:
    conn = _connect()
    with _lock:
        cur = conn.execute("DELETE FROM cache WHERE ttl > 0 AND (? - created_at) > ttl", (time.time(),))
        conn.commit()
        return cur.rowcount


def record(
    action: str,
    *,
    ok: bool,
    target_cid: str = "",
    payload: Any = None,
    detail: str = "",
) -> None:
    """记录一次写操作（转存/新建目录），便于回溯。"""
    conn = _connect()
    with _lock:
        conn.execute(
            "INSERT INTO history (created_at, action, target_cid, payload, ok, detail)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                action,
                str(target_cid or ""),
                json.dumps(payload or {}, ensure_ascii=False),
                1 if ok else 0,
                (detail or "")[:500],
            ),
        )
        conn.commit()


def recent_history(limit: int = 50) -> list[dict[str, Any]]:
    conn = _connect()
    with _lock:
        rows = conn.execute(
            "SELECT id, created_at, action, target_cid, payload, ok, detail"
            " FROM history ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            payload = json.loads(r[4])
        except json.JSONDecodeError:
            payload = {}
        out.append(
            {
                "id": r[0],
                "at": r[1],
                "action": r[2],
                "target_cid": r[3],
                "payload": payload,
                "ok": bool(r[5]),
                "detail": r[6],
            }
        )
    return out