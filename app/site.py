"""蜗牛4K（wn4k.com）站点抓取。

设计约束（重要）：
- 只按需抓取用户正在浏览的那一页，不做全站遍历；
- 所有请求走限速会话，最小间隔可配置；
- 结果带 TTL 缓存，重复浏览不重复请求；
- 网盘链接需登录才可见，未登录时明确返回 locked 状态，而不是伪装成空。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .http import RateLimitedSession

CATEGORIES: list[dict[str, Any]] = [
    {"id": 1, "name": "电影", "slug": "movie"},
    {"id": 2, "name": "连续剧", "slug": "tv"},
    {"id": 3, "name": "综艺", "slug": "show"},
    {"id": 4, "name": "动漫", "slug": "anime"},
]

CATEGORY_BY_ID = {c["id"]: c for c in CATEGORIES}

_SHARE_HOST_RE = re.compile(
    r"https?://(?:www\.)?(?:115|115cdn|anxia|115pan)\.com/s/([0-9a-zA-Z_\-]+)"
    r"(?:[^\s]*?[?&#](?:password|pwd)=([0-9a-zA-Z]{0,8}))?",
    re.IGNORECASE,
)
_MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[0-9a-zA-Z]+[^\s\"'<>]*", re.IGNORECASE)
_ED2K_RE = re.compile(r"ed2k://\|file\|[^\s\"'<>]+", re.IGNORECASE)
_PLAIN_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


@dataclass
class PanLink:
    """详情页里的一条网盘/离线资源。"""

    title: str
    group: str
    raw: str = ""
    locked: bool = True
    kind: str = "unknown"  # share | magnet | ed2k | http | baidu | unknown
    share_code: str = ""
    receive_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "group": self.group,
            "url": self.raw,
            "locked": self.locked,
            "kind": self.kind,
            "share_code": self.share_code,
            "receive_code": self.receive_code,
            "display": self.display(),
        }

    def display(self) -> str:
        if self.locked:
            return "登录后可见"
        if self.kind == "share":
            tail = f" 提取码 {self.receive_code}" if self.receive_code else ""
            return f"115分享 {self.share_code}{tail}"
        return self.raw[:120]


@dataclass
class VodItem:
    """列表卡片。"""

    vod_id: int
    title: str
    score: str = ""
    quality: str = ""
    year: str = ""
    region: str = ""
    category: str = ""
    poster: str = ""
    detail_url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.vod_id,
            "title": self.title,
            "score": self.score,
            "quality": self.quality,
            "year": self.year,
            "region": self.region,
            "category": self.category,
            "poster": self.poster,
            "url": self.detail_url,
        }


def classify_link(raw: str) -> tuple[str, str, str]:
    """判断链接类型，返回 (kind, share_code, receive_code)。"""
    text = html.unescape(raw or "").strip()
    if not text:
        return "unknown", "", ""
    if m := _SHARE_HOST_RE.search(text):
        return "share", m.group(1), (m.group(2) or "")
    if _MAGNET_RE.search(text):
        return "magnet", "", ""
    if _ED2K_RE.search(text):
        return "ed2k", "", ""
    low = text.lower()
    if "pan.baidu.com" in low or "yun.baidu.com" in low:
        return "baidu", "", ""
    if _PLAIN_URL_RE.search(text):
        return "http", "", ""
    return "unknown", "", ""


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _is_masked(text: str) -> bool:
    """判断是否为「登录后可见」的占位文本，避免把 ****** 当成真链接。"""
    t = text or ""
    return "******" in t or "登录后可见" in t or "登录后可" in t


class Wn4kClient:
    """站点客户端；一个实例对应一个登录会话。"""

    def __init__(
        self,
        base_url: str = "https://www.wn4k.com",
        *,
        min_interval: float = 1.5,
        timeout: float = 25.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = RateLimitedSession(min_interval=min_interval, timeout=timeout)
        self.http.session.headers.update(
            {
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": self.base_url + "/",
            }
        )
        self.username = ""
        self.logged_in = False
        self.last_error = ""

    # ---- 会话 ----------------------------------------------------------
    def url(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def set_cookie(self, cookie: str) -> None:
        self.http.set_cookie_string(cookie)

    def cookie_string(self) -> str:
        """当前会话的 Cookie 串（用于持久化登录态）。"""
        return self.http.cookie_string()

    def login(self, username: str, password: str) -> tuple[bool, str]:
        """登录站点。

        站点是 MacCMS 的 ajax 登录：POST /user/login.html。
        若站点开启验证码，则本方法会明确失败并提示改用 cookie 导入。
        """
        self.username = username
        # 先取一次登录页，拿到必要 cookie
        try:
            self.http.get(self.url("/user/login/"))
        except Exception as exc:  # noqa: BLE001
            return False, f"无法访问站点登录页：{exc}"

        payload = {"user_name": username, "user_pwd": password}
        try:
            resp = self.http.post(
                self.url("/user/login.html"),
                data=payload,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": self.url("/user/login/"),
                    "Origin": self.base_url,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"登录请求失败：{exc}"

        try:
            data = resp.json()
        except ValueError:
            body = (resp.text or "")[:200]
            return False, f"登录返回非 JSON（可能需要验证码）：{body}"

        if str(data.get("code")) == "1":
            self.logged_in = True
            return True, data.get("msg") or "登录成功"
        msg = data.get("msg") or "登录失败"
        if "验证码" in msg or "verify" in str(data).lower():
            msg += "（站点开启了验证码，请改用「导入 Cookie」方式）"
        return False, msg

    def check_login(self) -> bool:
        """判断是否处于登录态。

        站点对游客访问 /user/index/ 会 302 跳转到登录页，登录后才会 200。
        这比在 HTML 里找「退出」这类文本可靠得多。
        """
        try:
            resp = self.http.get(
                self.url("/user/index/"),
                allow_redirects=False,
            )
        except Exception:  # noqa: BLE001
            return self.logged_in
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location") or ""
            if "login" in location.lower():
                self.logged_in = False
                return False
            # 其它跳转按登录成功处理（部分站点会跳 /user/ 首页）
            self.logged_in = True
            return True
        if resp.status_code == 200:
            body = resp.text or ""
            # 页面里出现登录表单即视为未登录
            self.logged_in = 'name="user_pwd"' not in body
            return self.logged_in
        return self.logged_in

    def user_info(self) -> dict[str, Any]:
        return {
            "logged_in": self.logged_in,
            "username": self.username,
            "has_cookie": bool(self.http.cookie_string()),
            "error": self.last_error,
        }

    # ---- 列表 ----------------------------------------------------------
    def list_category(self, type_id: int, page: int = 1) -> dict[str, Any]:
        page = max(1, int(page))
        path = f"/vodtype/{type_id}/" if page == 1 else f"/vodtype/{type_id}-{page}/"
        return self._parse_list(path, source=f"category:{type_id}", page=page)

    def search(self, keyword: str, page: int = 1) -> dict[str, Any]:
        from urllib.parse import quote

        page = max(1, int(page))
        kw = quote(keyword, safe="")
        path = f"/vodsearch/-------------/?wd={kw}" if page == 1 else f"/vodsearch/-------------/?wd={kw}&page={page}"
        return self._parse_list(path, source="search", page=page)

    def _parse_list(self, path: str, *, source: str, page: int) -> dict[str, Any]:
        resp = self.http.get(self.url(path))
        resp.encoding = resp.encoding or "utf-8"
        soup = BeautifulSoup(resp.text or "", "lxml")
        items: list[VodItem] = []
        seen: set[int] = set()

        for card in soup.select("a.video-card[href*='/voddetail/']"):
            href = card.get("href") or ""
            m = re.search(r"/voddetail/(\d+)/", href)
            if not m:
                continue
            vod_id = int(m.group(1))
            if vod_id in seen:
                continue
            seen.add(vod_id)

            title = _clean(card.get("title")) or _clean(
                card.select_one(".video-title").get_text() if card.select_one(".video-title") else ""
            )
            score = _clean(card.select_one(".video-score").get_text()) if card.select_one(".video-score") else ""
            quality = _clean(card.select_one(".video-episode").get_text()) if card.select_one(".video-episode") else ""

            meta_raw = ""
            if node := card.select_one(".video-meta"):
                meta_raw = _clean(node.get_text())
            year, region, category = "", "", ""
            parts = [p.strip() for p in re.split(r"[·|]", meta_raw) if p.strip()]
            if len(parts) >= 1:
                year = parts[0]
            if len(parts) >= 2:
                region = parts[1]
            if len(parts) >= 3:
                category = parts[2]

            poster = ""
            if img := card.select_one("img"):
                poster = urljoin(self.base_url + "/", (img.get("src") or "").strip())

            items.append(
                VodItem(
                    vod_id=vod_id,
                    title=title,
                    score=score,
                    quality=quality,
                    year=year,
                    region=region,
                    category=category,
                    poster=poster,
                    detail_url=urljoin(self.base_url + "/", href),
                )
            )

        total_pages = self._detect_total_pages(soup)
        has_next = page < total_pages if total_pages else bool(items)

        return {
            "source": source,
            "page": page,
            "total_pages": total_pages,
            "has_next": has_next,
            "count": len(items),
            "items": [i.to_dict() for i in items],
        }

    @staticmethod
    def _detect_total_pages(soup: BeautifulSoup) -> int:
        """从分页里推断总页数（站点把页码放在链接里）。"""
        pages: list[int] = []
        for a in soup.select("a[href]"):
            href = a.get("href") or ""
            if m := re.search(r"/vodtype/\d+-(\d+)/", href):
                pages.append(int(m.group(1)))
            elif m := re.search(r"[?&]page=(\d+)", href):
                pages.append(int(m.group(1)))
        return max(pages) if pages else 0

    # ---- 详情 ----------------------------------------------------------
    def detail(self, vod_id: int) -> dict[str, Any]:
        resp = self.http.get(self.url(f"/voddetail/{vod_id}/"))
        resp.encoding = resp.encoding or "utf-8"
        soup = BeautifulSoup(resp.text or "", "lxml")

        title = ""
        for sel in (".premium-title", "h1.mobile-detail-title", "h1"):
            if node := soup.select_one(sel):
                title = _clean(node.get_text())
                if title:
                    break

        tags: list[str] = []
        meta: dict[str, str] = {}
        for node in soup.select(".premium-tags-top .p-tag"):
            tags.append(_clean(node.get_text()))

        for node in soup.select(".meta-item"):
            label = node.select_one(".m-label")
            value = node.select_one(".m-val")
            if label and value:
                meta[_clean(label.get_text())] = _clean(value.get_text())

        desc = ""
        if node := soup.select_one("#detailDescText"):
            desc = _clean(node.get_text())
        elif node := soup.select_one(".detail-desc-text"):
            desc = _clean(node.get_text())

        poster = ""
        if img := soup.select_one(".premium-poster img, .detail-poster-wrapper img"):
            poster = urljoin(self.base_url + "/", (img.get("src") or "").strip())

        # ---- 网盘资源 ----
        links: list[PanLink] = []
        locked = bool(soup.select_one(".pan-lock-box")) and not soup.select(".pan-link-item:not(.is-locked)")

        for group in soup.select(".pan-group"):
            group_title = ""
            if node := group.select_one(".pan-group-title"):
                group_title = _clean(node.get_text())
            for item in group.select(".pan-link-item"):
                ltitle = ""
                if node := item.select_one(".pan-link-title"):
                    ltitle = _clean(node.get_text())

                raw = ""
                # 站点用 data-copy 存放可复制内容
                for holder in item.select("[data-copy]"):
                    raw = (holder.get("data-copy") or "").strip()
                    if raw:
                        break
                if not raw:
                    for holder in item.select("[data-url], [data-link], [data-href]"):
                        raw = (
                            holder.get("data-url")
                            or holder.get("data-link")
                            or holder.get("data-href")
                            or ""
                        ).strip()
                        if raw:
                            break
                if not raw:
                    if node := item.select_one(".pan-link-meta"):
                        candidate = _clean(node.get_text())
                        if candidate and not _is_masked(candidate):
                            raw = candidate
                # 兜底：整段文本里找链接（屏蔽「登录后可见」这类占位文本）
                if not raw:
                    blob = _clean(item.get_text(" "))
                    if not _is_masked(blob):
                        if m := (
                            _SHARE_HOST_RE.search(blob)
                            or _MAGNET_RE.search(blob)
                            or _ED2K_RE.search(blob)
                            or _PLAIN_URL_RE.search(blob)
                        ):
                            raw = m.group(0)

                is_locked = item.has_attr("class") and "is-locked" in item.get("class", [])
                if not is_locked and not raw:
                    is_locked = True

                kind, code, rcode = classify_link(raw)
                links.append(
                    PanLink(
                        title=ltitle,
                        group=group_title,
                        raw=raw,
                        locked=is_locked or not raw,
                        kind=kind,
                        share_code=code,
                        receive_code=rcode,
                    )
                )

        # 兜底：没有 .pan-group 结构时，直接从 data-copy 抓
        if not links:
            for holder in soup.select("section.detail-panel [data-copy]"):
                raw = (holder.get("data-copy") or "").strip()
                if not raw:
                    continue
                kind, code, rcode = classify_link(raw)
                links.append(
                    PanLink(
                        title="",
                        group="网盘资源",
                        raw=raw,
                        locked=False,
                        kind=kind,
                        share_code=code,
                        receive_code=rcode,
                    )
                )

        return {
            "id": vod_id,
            "title": title,
            "tags": tags,
            "meta": meta,
            "desc": desc,
            "poster": poster,
            "url": self.url(f"/voddetail/{vod_id}/"),
            "locked": bool(locked or (links and all(l.locked for l in links))),
            "links": [l.to_dict() for l in links],
            "link_count": len(links),
            "usable_count": sum(1 for l in links if not l.locked and l.kind in ("share", "magnet", "ed2k", "http")),
        }