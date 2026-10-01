"""FastAPI 后端：分类浏览 / 排序筛选 / 详情 / 目录 / 转存。

启动： python -m app.server     或      uvicorn app.server:app --port 8848
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import store
from .config import WEB_DIR, load_config, public_config, save_config, update_state
from .genre import (
    DEFAULT_GENRE_RULES,
    GENRE_FALLBACK,
    load_genre_rules,
    reset_genre_rules,
    save_genre_rules,
)
from .p115 import P115Error
from .service import MAX_PAGES, get_service
from .site import CATEGORIES, GENRES, ORDERS, REGIONS, display_region

app = FastAPI(title="蜗牛4K 精选 → 115 转存工作台", version="1.0.0")


def _svc():
    return get_service()


def _fail(exc: Exception, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=status)


# ---------------------------------------------------------------- 基础状态
@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/api/status")
def status() -> Any:
    try:
        return {"ok": True, **_svc().status()}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, 500)


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return {"ok": True, "config": public_config()}


@app.post("/api/config")
def post_config(payload: dict[str, Any] = Body(default_factory=dict)) -> Any:
    try:
        partial: dict[str, Any] = {}
        if isinstance(payload.get("ui"), dict):
            partial["ui"] = payload["ui"]
        if isinstance(payload.get("safety"), dict):
            partial["safety"] = payload["safety"]
        if isinstance(payload.get("layout"), dict):
            partial["layout"] = payload["layout"]
        # 115 默认目录允许改
        if "default_cid" in payload:
            partial.setdefault("p115", {})["default_cid"] = str(payload["default_cid"])
        if "min_interval" in payload:
            partial.setdefault("wn4k", {})["min_interval"] = float(payload["min_interval"])
        if partial:
            save_config(partial)
            _svc().boot()  # 让限速等设置立即生效
        return {"ok": True, "config": public_config()}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


# ---------------------------------------------------------------- 类型规则
@app.get("/api/genres")
def get_genres() -> dict[str, Any]:
    """返回类型规则表与可用占位符说明。"""
    return {
        "ok": True,
        "rules": load_genre_rules(),
        "fallback": GENRE_FALLBACK,
        "defaults": DEFAULT_GENRE_RULES,
        "placeholders": [
            {"key": "category", "desc": "分类（电影/连续剧/综艺/动漫）"},
            {"key": "genre", "desc": "类型（由关键词推断，如 动作/科幻）"},
            {"key": "year", "desc": "年份"},
            {"key": "title", "desc": "片名"},
            {"key": "region", "desc": "地区"},
            {"key": "quality", "desc": "清晰度"},
        ],
    }


@app.post("/api/genres")
def post_genres(payload: dict[str, Any] = Body(...)) -> Any:
    rules = payload.get("rules")
    if not isinstance(rules, dict):
        return _fail(ValueError("rules 必须是 {类型: [关键词...]} 结构"))
    return {"ok": True, "rules": save_genre_rules(rules)}


@app.post("/api/genres/reset")
def post_genres_reset() -> dict[str, Any]:
    return {"ok": True, "rules": reset_genre_rules()}


# ---------------------------------------------------------------- 站点登录
@app.post("/api/site/login")
def site_login(payload: dict[str, Any] = Body(...)) -> Any:
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or not password:
        return _fail(ValueError("请填写蜗牛站点的账号和密码"))
    try:
        return {"ok": True, **_svc().site_login(username, password)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@app.post("/api/site/cookie")
def site_cookie(payload: dict[str, Any] = Body(...)) -> Any:
    cookie = str(payload.get("cookie") or "").strip()
    if not cookie:
        return _fail(ValueError("Cookie 为空"))
    try:
        return {"ok": True, **_svc().site_set_cookie(cookie)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


# ---------------------------------------------------------------- 115 登录
@app.post("/api/p115/cookie")
def p115_cookie(payload: dict[str, Any] = Body(...)) -> Any:
    cookie = str(payload.get("cookie") or "").strip()
    if not cookie:
        return _fail(ValueError("Cookie 为空"))
    try:
        info = _svc().p115_set_cookie(cookie)
        ok = bool(info.get("logged_in"))
        return JSONResponse({"ok": ok, **info}, status_code=200 if ok else 400)
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@app.post("/api/p115/qrcode/start")
def p115_qrcode_start() -> Any:
    try:
        return {"ok": True, **_svc().p115_qrcode_start()}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@app.post("/api/p115/qrcode/poll")
def p115_qrcode_poll(payload: dict[str, Any] = Body(...)) -> Any:
    uid = str(payload.get("uid") or "")
    if not uid:
        return _fail(ValueError("缺少 uid"))
    try:
        result = _svc().p115_qrcode_poll(
            uid, payload.get("time"), str(payload.get("sign") or "")
        )
        return {"ok": True, **result}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@app.post("/api/p115/logout")
def p115_logout() -> Any:
    return {"ok": True, **_svc().p115_logout()}


# ---------------------------------------------------------------- 115 目录
@app.get("/api/p115/dirs")
def p115_dirs(cid: str = Query("0")) -> Any:
    try:
        return {"ok": True, **_svc().p115_dirs(cid)}
    except P115Error as exc:
        return _fail(exc)
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@app.post("/api/p115/mkdir")
def p115_mkdir(payload: dict[str, Any] = Body(...)) -> Any:
    name = str(payload.get("name") or "").strip()
    pid = str(payload.get("pid") or "0")
    if not name:
        return _fail(ValueError("目录名不能为空"))
    try:
        return {"ok": True, **_svc().p115_mkdir(name, pid)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@app.post("/api/p115/mkdir_path")
def p115_mkdir_path(payload: dict[str, Any] = Body(...)) -> Any:
    path = str(payload.get("path") or "").strip()
    root = str(payload.get("root_cid") or "0")
    if not path:
        return _fail(ValueError("路径不能为空"))
    try:
        return {"ok": True, **_svc().p115_mkdir_path(path, root_cid=root)}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@app.post("/api/p115/delete")
def p115_delete(payload: dict[str, Any] = Body(...)) -> Any:
    """删除（移入回收站）指定的目录/文件 id。"""
    ids = payload.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return _fail(ValueError("请提供要删除的 ids"))
    try:
        return {"ok": True, **_svc().p115_delete([str(i) for i in ids], parent_id=str(payload.get("parent_id") or "0"))}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


# ---------------------------------------------------------------- 分类浏览
@app.get("/api/categories")
def categories() -> dict[str, Any]:
    return {"ok": True, "categories": CATEGORIES}


@app.get("/api/videos")
def videos(
    category: int | None = Query(None),
    page: int = Query(1, ge=1),
    pages: int = Query(1, ge=1, le=MAX_PAGES),
    keyword: str = Query(""),
    year: str = Query(""),
    area: str = Query(""),
    genre: str = Query(""),
    order: str = Query(""),
    min_score: float | None = Query(None),
    sort: str = Query("default"),
    order_dir: str = Query("desc"),
) -> Any:
    try:
        data = _svc().list_videos(
            category=category,
            page=page,
            pages=pages,
            keyword=keyword.strip(),
            year=year.strip(),
            area=area.strip(),
            genre=genre.strip(),
            order=order.strip(),
            min_score=min_score,
            sort=sort,
            order_dir=order_dir,
        )
        return {"ok": True, "category": category, "keyword": keyword.strip(), **data}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@app.get("/api/facets")
def facets() -> dict[str, Any]:
    """站点支持的服务端筛选项（类型与地区均经实测枚举，非猜测）。"""
    return {
        "ok": True,
        "genres": GENRES,
        "regions": [{"value": v, "label": display_region(v)} for v in REGIONS],
        "orders": ORDERS,
        "categories": CATEGORIES,
    }


@app.get("/api/videos/{vod_id}")
def video_detail(vod_id: int, refresh: bool = Query(False)) -> Any:
    try:
        data = _svc().detail(vod_id, refresh=refresh)
        return {"ok": True, **data}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


# ---------------------------------------------------------------- 转存
@app.post("/api/transfer/plan")
def transfer_plan(payload: dict[str, Any] = Body(...)) -> Any:
    selections = payload.get("selections") or []
    if not isinstance(selections, list) or not selections:
        return _fail(ValueError("请先选择要转存的影片"))
    try:
        plan = _svc().transfer_plan(
            selections,
            cid=str(payload.get("cid") or "0"),
            target_path=str(payload.get("target_path") or ""),
            use_template=bool(payload.get("use_template", True)),
            template=str(payload.get("template") or ""),
        )
        return {"ok": True, "plan": plan}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@app.post("/api/transfer/execute")
def transfer_execute(payload: dict[str, Any] = Body(...)) -> Any:
    selections = payload.get("selections") or []
    if not isinstance(selections, list) or not selections:
        return _fail(ValueError("请先选择要转存的影片"))
    try:
        result = _svc().transfer_execute(
            selections,
            cid=str(payload.get("cid") or "0"),
            target_path=str(payload.get("target_path") or ""),
            create_path=bool(payload.get("create_path")),
            use_template=bool(payload.get("use_template", True)),
            template=str(payload.get("template") or ""),
        )
        return {"ok": True, **result}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@app.post("/api/transfer/one")
def transfer_one(payload: dict[str, Any] = Body(...)) -> Any:
    url = str(payload.get("url") or "").strip()
    if not url:
        return _fail(ValueError("链接为空"))
    try:
        result = _svc().transfer_one(
            url,
            cid=str(payload.get("cid") or "0"),
            kind=str(payload.get("kind") or ""),
            title=str(payload.get("title") or ""),
        )
        return {"ok": True, **result}
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@app.get("/api/transfer/jobs")
def transfer_jobs() -> dict[str, Any]:
    return {"ok": True, "jobs": _svc().jobs()}


@app.get("/api/transfer/jobs/{job_id}")
def transfer_job(job_id: str) -> Any:
    try:
        return {"ok": True, **(_svc().job(job_id))}
    except KeyError:
        return _fail(ValueError("任务不存在"), 404)


@app.get("/api/history")
def history(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    return {"ok": True, "history": _svc().history(limit)}


@app.post("/api/cache/clear")
def cache_clear() -> dict[str, Any]:
    removed = store.purge_expired()
    return {"ok": True, "removed": removed}


# ---------------------------------------------------------------- 前端
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
def index() -> Any:
    index_file = WEB_DIR / "index.html"
    if not index_file.exists():
        return JSONResponse({"ok": False, "error": "web/index.html 不存在"}, status_code=500)
    return FileResponse(str(index_file))


def main() -> None:
    import uvicorn

    cfg = load_config()
    port = int(cfg.get("ui", {}).get("port", 8848) or 8848)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()