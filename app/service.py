"""业务编排层。

核心设计取舍（对应「不要批量拉全站」）：
- 列表数据只来自「用户当前正在看的那一页」；最多允许连取 MAX_PAGES 页，
  且必须由用户点「加载更多」显式触发，绝不后台遍历全站。
- 详情（含网盘链接）只在用户点开某个影片、或明确勾选要转存时才抓取。
- 转存有硬性上限（safety.max_batch / max_links_per_batch），超限直接拒绝而非截断，
  避免「一不小心批量转存」。
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import store
from .config import load_config
from .genre import infer_genre, load_genre_rules, render_path_template, sanitize_segment
from .p115 import P115Client, P115Error, parse_share_url
from .site import CATEGORIES, CATEGORY_BY_ID, Wn4kClient, classify_link

# 单次请求最多允许抓取的列表页数（必须由用户显式触发）
MAX_PAGES = 5

# 清晰度优先级，用于排序
_QUALITY_RANKS: list[tuple[str, int]] = [
    ("4k", 100),
    ("2160", 95),
    ("remux", 90),
    ("蓝光原盘", 85),
    ("bluray", 80),
    ("蓝光", 75),
    ("hdr", 70),
    ("1080", 60),
    ("720", 40),
    ("480", 20),
]


def quality_rank(text: str) -> int:
    low = (text or "").lower()
    best = 0
    for key, score in _QUALITY_RANKS:
        if key in low:
            best = max(best, score)
    return best


def _year_value(text: str) -> int:
    m = re.search(r"(19|20)\d{2}", text or "")
    return int(m.group(0)) if m else 0


def _year_from_tags(tags: list[str] | None) -> str:
    for tag in tags or []:
        value = (tag or "").strip()
        if re.fullmatch(r"(19|20)\d{2}", value):
            return value
    return ""


def build_path_context(
    detail: dict[str, Any],
    *,
    category_name: str = "",
    genre_hint: str = "",
) -> dict[str, str]:
    """把详情页信息整理成目录模板可用的上下文。

    genre_hint 优先：如果用户是按站点真实类型（?class=动作）浏览/转存的，
    那就是权威值，不再做关键词猜测。否则回退到本地关键词推断。
    """
    tags = [str(t or "").strip() for t in (detail.get("tags") or [])]
    quality = ""
    region = ""
    for tag in tags:
        low = tag.lower()
        if not quality and any(
            k in low for k in ("4k", "1080", "720", "480", "dvd", "blu", "蓝光", "remux", "web", "hdr")
        ):
            quality = tag
        if not region and re.fullmatch(r"[A-Za-z]{2}(,[A-Za-z]{2})*", tag):
            region = tag

    genre = (genre_hint or "").strip()
    if not genre:
        genre = infer_genre(
            title=detail.get("title") or "",
            desc=detail.get("desc") or "",
            extra=" ".join([quality] + tags),
        )
    return {
        "category": sanitize_segment(category_name),
        "genre": sanitize_segment(genre),
        "year": sanitize_segment(_year_from_tags(tags) or ""),
        "title": sanitize_segment(detail.get("title") or ""),
        "region": sanitize_segment(region),
        "quality": sanitize_segment(quality),
    }


def _contains_any(haystack: str, needles: Iterable[str]) -> bool:
    low = (haystack or "").lower()
    return any(n.lower() in low for n in needles if n)


@dataclass
class TransferJob:
    """一次转存任务（后台线程执行，前端轮询进度）。"""

    id: str
    status: str = "pending"  # pending | running | done | failed
    total: int = 0
    done: int = 0
    target_cid: str = "0"
    items: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "total": self.total,
            "done": self.done,
            "target_cid": self.target_cid,
            "results": self.results,
            "errors": self.errors,
            "created_at": self.created_at,
            "message": self.message,
        }


class Service:
    """应用级单例：持有站点与 115 会话。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._write_lock = threading.RLock()
        self._jobs: dict[str, TransferJob] = {}
        # 已浏览过的 115 目录（cid -> {name,parent}），仅用于面包屑
        self._known_dirs: dict[str, dict[str, str]] = {}
        self.site: Wn4kClient | None = None
        self.p115: P115Client | None = None
        self.boot()

    # ---- 启动 ----------------------------------------------------------
    def boot(self) -> dict[str, Any]:
        cfg = load_config()
        with self._lock:
            self.site = Wn4kClient(
                base_url=cfg["wn4k"]["base_url"],
                min_interval=float(cfg["wn4k"]["min_interval"]),
            )
            self.p115 = P115Client(
                cookie=cfg["p115"].get("cookie") or "",
                min_interval=float(cfg["p115"]["min_interval"]),
            )
            site_cookie = (cfg["wn4k"].get("cookie") or "").strip()
            if site_cookie:
                self.site.set_cookie(site_cookie)
            user = cfg["wn4k"].get("username")
            pwd = cfg["wn4k"].get("password")
            if not site_cookie and user and pwd:
                ok, msg = self.site.login(user, pwd)
                self.site.last_error = "" if ok else msg
        return self.status()

    @property
    def cfg(self) -> dict[str, Any]:
        return load_config()

    # ---- 状态 ----------------------------------------------------------
    def status(self) -> dict[str, Any]:
        site_info: dict[str, Any] = {"logged_in": False, "username": "", "error": "未初始化"}
        p115_info: dict[str, Any] = {"logged_in": False, "error": "未初始化"}
        if self.site:
            self.site.check_login()
            site_info = self.site.user_info()
        if self.p115:
            p115_info = self.p115.user_info()
        return {
            "site": {
                "base_url": self.site.base_url if self.site else "",
                "logged_in": bool(site_info.get("logged_in")),
                "username": site_info.get("username") or "",
                "error": site_info.get("error") or "",
            },
            "p115": {
                "logged_in": bool(p115_info.get("logged_in")),
                "user_name": p115_info.get("user_name") or "",
                "vip_name": p115_info.get("vip_name") or "",
                "space_info": p115_info.get("space_info") or {},
                "error": p115_info.get("error") or "",
            },
            "categories": CATEGORIES,
            "default_cid": self.cfg["p115"]["default_cid"],
            "safety": self.cfg["safety"],
        }

    # ---- 站点登录 ------------------------------------------------------
    def site_login(self, username: str, password: str, *, persist: bool = True) -> dict[str, Any]:
        assert self.site is not None
        ok, msg = self.site.login(username, password)
        if ok and persist:
            # 持久化只是「顺手保存」，失败了也不能把登录成功报成失败
            try:
                from .config import save_config

                # 不落盘明文密码；持久化会话 Cookie 以便重启后仍是登录态
                save_config(
                    {
                        "wn4k": {
                            "username": username,
                            "password": "",
                            "cookie": self.site.cookie_string(),
                        }
                    }
                )
            except Exception as exc:  # noqa: BLE001
                msg = f"{msg}（但会话未能保存：{exc}）"
        return {"ok": ok, "message": msg, **self.site.user_info()}

    def site_set_cookie(self, cookie: str) -> dict[str, Any]:
        assert self.site is not None
        self.site.set_cookie(cookie)
        self.site.check_login()
        if self.site.logged_in:
            from .config import save_config

            save_config({"wn4k": {"cookie": cookie}})
        return {"ok": self.site.logged_in, **self.site.user_info()}

    # ---- 115 登录 ------------------------------------------------------
    def p115_set_cookie(self, cookie: str) -> dict[str, Any]:
        assert self.p115 is not None
        self.p115.set_cookie(cookie)
        info = self.p115.user_info(refresh=True)
        if info.get("logged_in"):
            from .config import save_config

            save_config({"p115": {"cookie": cookie}})
        return info

    def p115_qrcode_start(self) -> dict[str, Any]:
        assert self.p115 is not None
        info = self.p115.qrcode_start()
        png = self.p115.qrcode_image(info["uid"])
        import base64

        info["image"] = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        return info

    def p115_qrcode_poll(self, uid: str, time_: Any, sign: str, *, finish: bool = True) -> dict[str, Any]:
        assert self.p115 is not None
        status = self.p115.qrcode_status(uid, time_, sign)
        if status["confirmed"] and finish:
            result = self.p115.qrcode_result(uid)
            from .config import save_config

            save_config({"p115": {"cookie": result["cookie"]}})
            return {**status, "logged_in": True, "user_name": result.get("user_name") or ""}
        return status

    def p115_logout(self) -> dict[str, Any]:
        assert self.p115 is not None
        self.p115.set_cookie("")
        from .config import save_config

        save_config({"p115": {"cookie": ""}})
        return {"ok": True}

    # ---- 115 目录 ------------------------------------------------------
    def p115_dirs(self, cid: str = "0") -> dict[str, Any]:
        assert self.p115 is not None
        if not self.p115.logged_in:
            raise P115Error(self.p115.user_info().get("error") or "115 未登录")
        cid = cid or "0"
        entries = self.p115.list_dir(cid)
        # 边浏览边记录目录结构，用于面包屑；不主动遍历整棵树（避免大量请求）
        for e in entries:
            if e.is_dir:
                self._known_dirs[e.id] = {"name": e.name, "parent": cid}
        return {
            "cid": cid,
            "parent": self._cid_parent(cid),
            "path": self._cid_path(cid),
            "folders": [e.to_dict() for e in entries if e.is_dir],
            "files": [e.to_dict() for e in entries if not e.is_dir][:100],
        }

    def _cid_path(self, cid: str) -> list[dict[str, str]]:
        """用已浏览过的目录记录拼面包屑（缺失的层级就截断，不额外发请求）。"""
        chain: list[dict[str, str]] = [{"cid": "0", "name": "根目录"}]
        parts: list[dict[str, str]] = []
        cur = cid
        seen: set[str] = set()
        while cur and cur != "0" and cur not in seen:
            seen.add(cur)
            node = self._known_dirs.get(cur)
            if not node:
                break
            parts.append({"cid": cur, "name": node["name"]})
            cur = node["parent"]
        chain.extend(reversed(parts))
        return chain

    def _cid_parent(self, cid: str) -> str:
        if not cid or cid == "0":
            return "0"
        node = self._known_dirs.get(cid)
        return node["parent"] if node else "0"

    def p115_mkdir(self, name: str, pid: str = "0") -> dict[str, Any]:
        assert self.p115 is not None
        with self._write_lock:
            result = self.p115.mkdir(name, pid)
        if result.get("cid"):
            self._known_dirs[result["cid"]] = {"name": result.get("name") or name, "parent": pid or "0"}
        store.record("mkdir", ok=True, target_cid=result.get("cid", ""), payload={"name": name, "pid": pid})
        return result

    def p115_mkdir_path(self, path: str, *, root_cid: str = "0") -> dict[str, Any]:
        assert self.p115 is not None
        with self._write_lock:
            cid = self.p115.ensure_path(path, root_cid=root_cid)
        store.record("mkdir_path", ok=True, target_cid=cid, payload={"path": path, "root": root_cid})
        return {"cid": cid, "path": path}

    def p115_delete(self, entry_ids: list[str], *, parent_id: str = "0") -> dict[str, Any]:
        """删除（移入回收站）指定目录/文件。"""
        assert self.p115 is not None
        with self._write_lock:
            result = self.p115.delete(entry_ids, parent_id=parent_id)
        for eid in entry_ids:
            self._known_dirs.pop(str(eid), None)
        store.record(
            "delete",
            ok=True,
            target_cid=parent_id,
            payload={"ids": entry_ids[:20]},
            detail=f"已移入回收站：{len(entry_ids)} 项",
        )
        return result

    # ---- 分类浏览 ------------------------------------------------------
    def list_videos(
        self,
        *,
        category: int | None = None,
        page: int = 1,
        pages: int = 1,
        keyword: str = "",
        year: str = "",
        area: str = "",
        genre: str = "",
        order: str = "",
        min_score: float | None = None,
        sort: str = "default",
        order_dir: str = "desc",
    ) -> dict[str, Any]:
        """按分类/搜索列出影片。

        筛选走**服务端**（站点支持 ?year= ?area= ?class= ?order=），
        这样「2026」拿到的是全站该年份的结果，而不是只筛当前页。
        """
        assert self.site is not None
        pages = max(1, min(int(pages or 1), MAX_PAGES))
        collected: list[dict[str, Any]] = []
        seen: set[int] = set()
        total_pages = 0
        last_page = page

        ttl = float(self.cfg["ui"]["list_ttl"])
        for offset in range(pages):
            p = page + offset
            last_page = p
            key = "|".join([
                "list", "search" if keyword else "cat",
                str(category or 1), keyword, str(p), year, area, genre, order,
            ])
            data = store.get(key)
            if not data:
                if keyword:
                    data = self.site.search(keyword, p, year=year, order=order)
                else:
                    data = self.site.list_category(
                        int(category or 1), p, year=year, area=area, cls=genre, order=order
                    )
                store.put(key, data, ttl)
            total_pages = max(total_pages, int(data.get("total_pages") or 0))
            for item in data.get("items") or []:
                vid = int(item.get("id") or 0)
                if vid and vid not in seen:
                    seen.add(vid)
                    collected.append(item)
            if not data.get("has_next"):
                break

        # 服务端已按 year/area/class 筛过，这里只做评分下限与本地排序
        filtered = collected
        if min_score is not None:
            def score_of(item: dict[str, Any]) -> float:
                try:
                    return float(item.get("score") or 0)
                except (TypeError, ValueError):
                    return 0.0

            filtered = [i for i in filtered if score_of(i) >= float(min_score)]

        # 站点自带排序时，若用户没额外指定排序就保留站点顺序
        if sort and sort != "default":
            filtered = self._apply_sort(filtered, sort, order_dir)

        return {
            "items": filtered,
            "collected": len(collected),
            "shown": len(filtered),
            "page": page,
            "last_page": last_page,
            "pages_loaded": last_page - page + 1,
            "total_pages": total_pages,
            "has_next": bool(total_pages and last_page < total_pages),
            "max_pages": MAX_PAGES,
            "server_filters": {"year": year, "area": area, "class": genre, "order": order},
            "facets": self._facets(collected),
        }

    @staticmethod
    def _facets(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        def count_by(field: str) -> list[dict[str, Any]]:
            buckets: dict[str, int] = {}
            for it in items:
                value = (it.get(field) or "").strip()
                if not value:
                    continue
                # 地区可能是 "US,GB"
                for part in ([value] if field != "region" else [p.strip() for p in value.split(",")]):
                    if part:
                        buckets[part] = buckets.get(part, 0) + 1
            return [
                {"value": k, "count": v}
                for k, v in sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0]))
            ]

        return {
            "year": count_by("year")[:40],
            "region": count_by("region")[:30],
            "quality": count_by("quality")[:30],
        }

    @staticmethod
    def _apply_sort(items: list[dict[str, Any]], sort: str, order: str) -> list[dict[str, Any]]:
        reverse = (order or "desc").lower() != "asc"

        def score_of(item: dict[str, Any]) -> float:
            try:
                return float(item.get("score") or 0)
            except (TypeError, ValueError):
                return 0.0

        keys = {
            "score": lambda i: score_of(i),
            "year": lambda i: _year_value(i.get("year") or ""),
            "quality": lambda i: quality_rank(i.get("quality") or ""),
            "title": lambda i: (i.get("title") or ""),
            "region": lambda i: (i.get("region") or ""),
            "id": lambda i: int(i.get("id") or 0),
        }
        if sort in ("default", "", None):
            return items
        key = keys.get(sort)
        if not key:
            return items
        # 标题排序按升序更自然
        if sort == "title":
            return sorted(items, key=key, reverse=not reverse)
        return sorted(items, key=key, reverse=reverse)

    # ---- 详情 ----------------------------------------------------------
    def detail(self, vod_id: int, *, refresh: bool = False) -> dict[str, Any]:
        assert self.site is not None
        # 缓存键带上登录态：登录后链接才可见，不能复用游客时期的缓存
        auth = "in" if self.site.logged_in else "out"
        key = f"detail:{auth}:{vod_id}"
        if not refresh:
            cached = store.get(key)
            if cached:
                cached["cached"] = True
                return cached
        data = self.site.detail(vod_id)
        data["cached"] = False
        store.put(key, data, float(self.cfg["ui"]["detail_ttl"]))
        return data

    # ---- 转存 ----------------------------------------------------------
    def _pick_links(
        self,
        detail: dict[str, Any],
        *,
        link_indexes: list[int] | None,
        only_kinds: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """挑出真正可转存的链接，并说明被跳过的原因。

        返回 (可转存链接, 跳过原因列表)。锁定/不支持的链接一律不进入转存队列。
        """
        links = list(detail.get("links") or [])
        usable_kinds = set(only_kinds or ["share", "magnet", "ed2k", "http"])

        if link_indexes:
            candidates = [links[i] for i in link_indexes if 0 <= i < len(links)]
        else:
            # 默认只取第一条可用链接，避免一次拖入几十条
            candidates = [l for l in links if not l.get("locked") and l.get("kind") in usable_kinds][:1]

        picked: list[dict[str, Any]] = []
        skipped: list[str] = []
        for link in candidates:
            if link.get("locked"):
                skipped.append(f"「{(link.get('title') or '')[:40]}」需登录站点后才能读取链接")
                continue
            if link.get("kind") not in usable_kinds:
                skipped.append(f"「{(link.get('title') or '')[:40]}」类型为 {link.get('kind')}，不支持转存")
                continue
            picked.append(link)
        return picked, skipped

    def transfer_plan(
        self,
        selections: list[dict[str, Any]],
        *,
        cid: str = "0",
        target_path: str = "",
        use_template: bool = True,
        template: str = "",
    ) -> dict[str, Any]:
        """在不写入 115 的前提下，算出「将会转存什么、转到哪个目录」。

        selections: [{"vod_id": int, "link_indexes": [int], "title": str}]
        use_template=True 时，按 layout.template 为每部影片生成子目录路径
        （如 电影/动作/2026/限制法令）。
        """
        assert self.site is not None
        cfg = self.cfg
        safety = cfg["safety"]
        layout = cfg.get("layout", {})
        max_batch = int(safety["max_batch"])

        unique_ids: list[int] = []
        seen: set[int] = set()
        for sel in selections:
            vid = int(sel.get("vod_id") or 0)
            if vid and vid not in seen:
                seen.add(vid)
                unique_ids.append(vid)

        if not unique_ids:
            raise ValueError("没有选择任何影片")
        if len(unique_ids) > max_batch:
            raise ValueError(
                f"本次选择了 {len(unique_ids)} 部影片，超过单次上限 {max_batch}。"
                "请分批转存（这是刻意的安全限制）。"
            )

        path_template = (template or layout.get("template") or "{category}/{genre}/{year}/{title}").strip()
        per_video_dir = bool(layout.get("per_video_dir", True)) and use_template and bool(path_template)

        plan_items: list[dict[str, Any]] = []
        total_links = 0
        for sel in selections:
            vid = int(sel.get("vod_id") or 0)
            if not vid:
                continue
            detail = self.detail(vid)
            picked, skipped = self._pick_links(
                detail,
                link_indexes=sel.get("link_indexes"),
                only_kinds=sel.get("only_kinds"),
            )
            total_links += len(picked)

            # 分类名取站点分类（电影/连续剧/综艺/动漫）
            category_name = ""
            known = {c["name"] for c in CATEGORIES}
            for tag in detail.get("tags") or []:
                if tag in known:
                    category_name = tag
                    break
            if not category_name and sel.get("category"):
                category_name = str(sel["category"])

            ctx = build_path_context(
                detail,
                category_name=category_name,
                genre_hint=str(sel.get("genre") or ""),
            )
            rel_path = render_path_template(path_template, ctx) if per_video_dir else ""
            if target_path:
                rel_path = f"{target_path.strip('/')}/{rel_path}".strip("/")

            if picked:
                warning = ""
            elif detail.get("locked"):
                warning = "站点未登录，网盘链接不可见。请先在「登录设置」里登录蜗牛站点。"
            else:
                warning = "没有可转存的 115 资源（该片可能只有百度等其它网盘）"

            plan_items.append(
                {
                    "vod_id": vid,
                    "title": detail.get("title") or sel.get("title") or f"#{vid}",
                    "year": ctx["year"],
                    "genre": ctx["genre"],
                    "category": ctx["category"],
                    "region": ctx["region"],
                    "quality": ctx["quality"],
                    "rel_path": rel_path,
                    "locked": bool(detail.get("locked")),
                    "available_links": len(detail.get("links") or []),
                    "picked": picked,
                    "picked_count": len(picked),
                    "skipped": skipped,
                    "warning": warning,
                }
            )

        max_links = int(safety["max_links_per_batch"])
        if total_links > max_links:
            raise ValueError(f"本次将处理 {total_links} 条链接，超过单次上限 {max_links}。请减少选择。")

        return {
            "cid": cid or "0",
            "target_path": target_path,
            "template": path_template if per_video_dir else "",
            "use_template": per_video_dir,
            "videos": len(plan_items),
            "links": total_links,
            "items": plan_items,
            "limits": {"max_batch": max_batch, "max_links_per_batch": max_links},
        }

    def transfer_execute(
        self,
        selections: list[dict[str, Any]],
        *,
        cid: str = "0",
        target_path: str = "",
        create_path: bool = False,
        use_template: bool = True,
        template: str = "",
    ) -> dict[str, Any]:
        """创建后台转存任务，立即返回任务 id。

        use_template=True 时，会按模板为每部影片在 cid 下创建子目录
        （如 电影/动作/2026/限制法令），并把该片的资源转存进去。
        """
        assert self.p115 is not None
        if not self.p115.logged_in:
            raise P115Error(self.p115.user_info().get("error") or "115 未登录")

        base_cid = cid or "0"
        # 兼容旧参数：目标目录不存在时先建出来
        if create_path and target_path.strip():
            base_cid = self.p115_mkdir_path(target_path, root_cid=base_cid)["cid"]

        plan = self.transfer_plan(
            selections,
            cid=base_cid,
            target_path=target_path,
            use_template=use_template,
            template=template,
        )
        job = TransferJob(
            id=uuid.uuid4().hex[:12],
            total=sum(len(i["picked"]) for i in plan["items"]),
            target_cid=base_cid,
            items=plan["items"],
        )
        with self._lock:
            self._jobs[job.id] = job
        threading.Thread(target=self._run_job, args=(job,), daemon=True).start()
        return {"job_id": job.id, "plan": plan}

    def _run_job(self, job: TransferJob) -> None:
        """逐部影片执行：先按模板建目录，再转存到该目录。"""
        assert self.p115 is not None
        job.status = "running"
        for item in job.items:
            rel_path = (item.get("rel_path") or "").strip("/")
            target_cid = job.target_cid
            if rel_path:
                try:
                    with self._write_lock:
                        target_cid = self.p115.ensure_path(rel_path, root_cid=job.target_cid)
                    job.results.append(
                        {
                            "ok": True,
                            "title": item.get("title") or "",
                            "kind": "mkdir",
                            "detail": f"目录已就绪：{rel_path}",
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)
                    job.errors.append(f"{item.get('title')}（建目录）：{msg}")
                    job.results.append(
                        {
                            "ok": False,
                            "title": item.get("title") or "",
                            "kind": "mkdir",
                            "detail": f"建目录失败：{msg}",
                        }
                    )
                    job.done += len(item.get("picked") or [])
                    store.record(
                        "mkdir",
                        ok=False,
                        target_cid=job.target_cid,
                        payload={"path": rel_path, "title": item.get("title")},
                        detail=msg,
                    )
                    continue
                store.record(
                    "mkdir",
                    ok=True,
                    target_cid=target_cid,
                    payload={"path": rel_path, "title": item.get("title")},
                    detail=f"目录已就绪：{rel_path}",
                )

            for link in item.get("picked") or []:
                kind = link.get("kind") or "unknown"
                url = link.get("url") or ""
                title = f"{item.get('title')} · {link.get('title') or kind}"
                try:
                    with self._write_lock:
                        if kind == "share":
                            code, rcode = parse_share_url(url)
                            result = self.p115.transfer_share_url(url, target_cid)
                            detail = f"已转存分享 {code}（{result.get('files', 0)} 项）"
                        elif kind in ("magnet", "ed2k", "http"):
                            result = self.p115.offline_add([url], target_cid)
                            detail = "已提交离线下载"
                        else:
                            raise P115Error(f"不支持的链接类型：{kind}")
                    job.results.append(
                        {"ok": True, "title": title, "kind": kind, "detail": detail}
                    )
                    store.record(
                        "transfer",
                        ok=True,
                        target_cid=target_cid,
                        payload={"title": title, "kind": kind, "url": url[:200], "path": rel_path},
                        detail=detail,
                    )
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)
                    job.results.append({"ok": False, "title": title, "kind": kind, "detail": msg})
                    job.errors.append(f"{title}：{msg}")
                    store.record(
                        "transfer",
                        ok=False,
                        target_cid=target_cid,
                        payload={"title": title, "kind": kind, "url": url[:200], "path": rel_path},
                        detail=msg,
                    )
                job.done += 1
        job.status = "done"
        ok_count = sum(1 for r in job.results if r["ok"])
        job.message = f"完成：成功 {ok_count}，失败 {len(job.results) - ok_count}"

    def job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        return job.to_dict()

    def jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [j.to_dict() for j in sorted(self._jobs.values(), key=lambda x: -x.created_at)]

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return store.recent_history(limit)

    # ---- 单条链接转存（详情页里逐个点）--------------------------------
    def transfer_one(
        self,
        url: str,
        *,
        cid: str = "0",
        kind: str = "",
        title: str = "",
    ) -> dict[str, Any]:
        assert self.p115 is not None
        if not self.p115.logged_in:
            raise P115Error(self.p115.user_info().get("error") or "115 未登录")
        kind = kind or classify_link(url)[0]
        with self._write_lock:
            if kind == "share":
                result = self.p115.transfer_share_url(url, cid or "0")
                detail = f"已转存（{result.get('files', 0)} 项）"
            elif kind in ("magnet", "ed2k", "http"):
                self.p115.offline_add([url], cid or "0")
                detail = "已提交离线下载"
            else:
                raise P115Error(f"不支持的链接类型：{kind}")
        store.record("transfer_one", ok=True, target_cid=cid, payload={"title": title, "url": url[:200]}, detail=detail)
        return {"ok": True, "detail": detail}


_service: Service | None = None
_service_lock = threading.Lock()


def get_service() -> Service:
    global _service
    with _service_lock:
        if _service is None:
            _service = Service()
        return _service