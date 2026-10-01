"""115 网盘客户端（网页版 cookie 鉴权）。

为什么用 cookie 而不是开放平台 token：
    115 开放平台（open.115.com）**没有提供「分享转存」能力**，只有上传/下载/离线
    等接口。而网页版点击「转存」时调用的是 webapi.115.com/share/receive，属 cookie
    鉴权。因此本应用登录方式与网页版一致。

接口依据（已对照 p115client 源码核实）：
    GET  https://qrcodeapi.115.com/api/1.0/web/1.0/token/     取二维码
    GET  https://qrcodeapi.115.com/api/1.0/web/1.0/qrcode     二维码图片
    GET  https://qrcodeapi.115.com/get/status/                扫码状态
    POST https://qrcodeapi.115.com/app/1.0/web/1.0/login/qrcode/   换取 cookie
    GET  https://my.115.com/?ct=ajax&ac=nav                   登录态/用户信息
    GET  https://webapi.115.com/files                         目录列表
    POST https://webapi.115.com/files/add                     新建目录
    GET  https://webapi.115.com/share/snap                    分享内文件列表
    POST https://webapi.115.com/share/receive                 转存（share_code/receive_code/file_id/cid）
    GET  https://115.com/?ct=clouddownload&ac=space           离线下载 sign
    POST https://clouddownload.115.com/lixianssp/?ac=add_task_urls  离线下载任务
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .http import RateLimitedSession

QR_BASE = "https://qrcodeapi.115.com"
WEBAPI = "https://webapi.115.com"
MY115 = "https://my.115.com"
CLOUD_BASE = "https://clouddownload.115.com"

# 扫码状态
QR_WAITING = 0
QR_SCANNED = 1
QR_CONFIRMED = 2
QR_EXPIRED = -1
QR_CANCELED = -2

QR_STATUS_TEXT = {
    QR_WAITING: "等待扫码",
    QR_SCANNED: "已扫码，请在手机上确认",
    QR_CONFIRMED: "已确认，正在登录",
    QR_EXPIRED: "二维码已过期，请刷新",
    QR_CANCELED: "已取消登录",
}

SHARE_URL_RE = re.compile(
    r"(?:115|115cdn|anxia|115pan)\.com/s/([0-9a-zA-Z_\-]+)"
    r"(?:[^\s]*?[?&#](?:password|pwd)=([0-9a-zA-Z]{0,8}))?",
    re.IGNORECASE,
)


class P115Error(RuntimeError):
    """115 业务错误。"""


@dataclass
class P115Entry:
    """115 目录项。"""

    id: str
    name: str
    is_dir: bool
    size: int = 0
    parent_id: str = "0"
    updated: str = ""
    pickcode: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "is_dir": self.is_dir,
            "size": self.size,
            "parent_id": self.parent_id,
            "updated": self.updated,
        }


def parse_share_url(url: str) -> tuple[str, str]:
    """从 115 分享链接里取出 (share_code, receive_code)。"""
    text = (url or "").strip()
    if not text:
        raise P115Error("分享链接为空")
    if m := SHARE_URL_RE.search(text):
        return m.group(1), (m.group(2) or "")
    # 允许直接传 share_code
    if re.fullmatch(r"[0-9a-zA-Z_\-]{6,}", text):
        return text, ""
    raise P115Error(f"无法从链接中识别 115 分享码：{text[:80]}")


class P115Client:
    """一个实例对应一个 115 会话。"""

    def __init__(
        self,
        cookie: str = "",
        *,
        min_interval: float = 1.2,
        timeout: float = 25.0,
    ) -> None:
        self.http = RateLimitedSession(min_interval=min_interval, timeout=timeout)
        self.http.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://115.com",
                "Referer": "https://115.com/",
            }
        )
        if cookie:
            self.set_cookie(cookie)
        self._user: dict[str, Any] = {}

    # ---- 会话 ----------------------------------------------------------
    def set_cookie(self, cookie: str) -> None:
        self.http.set_cookie_string(cookie)
        self._user = {}

    def cookie_string(self) -> str:
        return self.http.cookie_string()

    def _json(self, resp, *, what: str) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError:
            snippet = (resp.text or "")[:200]
            raise P115Error(f"{what}返回非 JSON：{snippet}") from None
        if not isinstance(data, dict):
            raise P115Error(f"{what}返回格式异常")
        return data

    def _ok(self, data: dict[str, Any], *, what: str) -> dict[str, Any]:
        """115 的成功标志在不同接口里可能是 state / errno。"""
        if data.get("state") is True or data.get("state") == 1:
            return data
        if data.get("errno") in (0, "0"):
            return data
        # 有些接口既没 state 也没 errno，但有 data
        if "state" not in data and "errno" not in data and "data" in data:
            return data
        msg = (
            data.get("error")
            or data.get("message")
            or data.get("msg")
            or data.get("errmsg")
            or f"{what}失败"
        )
        errno = data.get("errno")
        if errno:
            msg = f"{msg}（errno={errno}）"
        raise P115Error(str(msg))

    # ---- 登录态 --------------------------------------------------------
    def user_info(self, *, refresh: bool = False) -> dict[str, Any]:
        if self._user and not refresh:
            return self._user
        if not self.cookie_string():
            self._user = {"logged_in": False, "error": "未配置 115 Cookie"}
            return self._user
        try:
            resp = self.http.get(
                f"{MY115}/?ct=ajax&ac=nav",
                headers={"Referer": "https://115.com/"},
            )
            data = self._json(resp, what="获取用户信息")
            if data.get("state") is True or data.get("state") == 1:
                d = data.get("data") or {}
                self._user = {
                    "logged_in": True,
                    "user_id": str(d.get("user_id") or ""),
                    "user_name": d.get("user_name") or d.get("uname") or "",
                    "is_vip": bool(d.get("is_vip")),
                    "vip_name": d.get("vip_name") or "",
                    "space_info": d.get("space_info") or {},
                }
            else:
                self._user = {
                    "logged_in": False,
                    "error": str(data.get("error") or data.get("msg") or "Cookie 已失效"),
                }
        except Exception as exc:  # noqa: BLE001
            self._user = {"logged_in": False, "error": f"115 连接失败：{exc}"}
        return self._user

    @property
    def logged_in(self) -> bool:
        return bool(self.user_info().get("logged_in"))

    def require_login(self) -> None:
        info = self.user_info()
        if not info.get("logged_in"):
            raise P115Error(info.get("error") or "115 未登录，请先扫码或填入 Cookie")

    # ---- 扫码登录 ------------------------------------------------------
    def qrcode_start(self) -> dict[str, Any]:
        """取二维码：返回 uid / time / sign 和图片 data URI。"""
        resp = self.http.get(f"{QR_BASE}/api/1.0/web/1.0/token/")
        data = self._ok(self._json(resp, what="获取二维码"), what="获取二维码")
        d = data.get("data") or {}
        uid = str(d.get("uid") or "")
        if not uid:
            raise P115Error("二维码 uid 获取失败")
        return {
            "uid": uid,
            "time": d.get("time"),
            "sign": d.get("sign"),
        }

    def qrcode_image(self, uid: str) -> bytes:
        resp = self.http.get(
            f"{QR_BASE}/api/1.0/web/1.0/qrcode",
            params={"uid": uid},
            headers={"Accept": "image/png,image/*,*/*"},
        )
        return resp.content

    def qrcode_status(self, uid: str, time_: Any, sign: str) -> dict[str, Any]:
        resp = self.http.get(
            f"{QR_BASE}/get/status/",
            params={"uid": uid, "time": time_, "sign": sign},
        )
        data = self._json(resp, what="查询扫码状态")
        d = data.get("data") or {}
        status = d.get("status")
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = QR_WAITING
        return {
            "status": status,
            "text": QR_STATUS_TEXT.get(status, d.get("msg") or f"状态 {status}"),
            "confirmed": status == QR_CONFIRMED,
        }

    def qrcode_result(self, uid: str) -> dict[str, Any]:
        """扫码确认后换取 cookie。"""
        resp = self.http.post(
            f"{QR_BASE}/app/1.0/web/1.0/login/qrcode/",
            data={"account": uid},
            headers={
                "Origin": "https://115.com",
                "Referer": "https://115.com/",
            },
        )
        data = self._json(resp, what="扫码登录")
        if data.get("state") is not True and data.get("state") != 1:
            raise P115Error(
                str(data.get("error") or data.get("msg") or "扫码登录失败（可能触发 IP 登录异常）")
            )
        d = data.get("data") or {}
        cookie_map = d.get("cookie") or {}
        if not cookie_map:
            raise P115Error("扫码成功但未返回 Cookie")
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_map.items())
        self.set_cookie(cookie_str)
        self.user_info(refresh=True)
        return {
            "cookie": cookie_str,
            "user_id": str(d.get("user_id") or ""),
            "user_name": d.get("user_name") or "",
            "logged_in": bool(self._user.get("logged_in")),
        }

    # ---- 目录 ----------------------------------------------------------
    def list_dir(self, cid: str = "0", *, limit: int = 200) -> list[P115Entry]:
        """列出目录内容（目录在前）。"""
        self.require_login()
        resp = self.http.get(
            f"{WEBAPI}/files",
            params={
                "aid": 1,
                "cid": cid or "0",
                "o": "user_ptime",
                "asc": 0,
                "show_dir": 1,
                "limit": limit,
                "offset": 0,
                "fc_mix": 0,
                "cur": 1,
                "count_folders": 1,
                "custom_order": 0,
            },
        )
        data = self._ok(self._json(resp, what="读取目录"), what="读取目录")
        rows = data.get("data") or []
        if isinstance(rows, dict):
            rows = rows.get("list") or []
        out: list[P115Entry] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            # 115 网页版约定：目录的 fid 恒为 0，真实 id 在 cid；文件则相反
            fid = str(row.get("fid") or "0")
            is_dir = fid in ("0", "", "None")
            entry_id = str(
                (row.get("cid") if is_dir else (row.get("fid") or row.get("cid"))) or ""
            )
            if not entry_id or entry_id == "0":
                continue
            out.append(
                P115Entry(
                    id=entry_id,
                    name=str(row.get("n") or row.get("file_name") or ""),
                    is_dir=is_dir,
                    size=int(row.get("s") or row.get("file_size") or 0),
                    parent_id=str(row.get("pid") or cid or "0"),
                    updated=str(row.get("te") or row.get("upt") or ""),
                    pickcode=str(row.get("pc") or ""),
                )
            )
        out.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return out

    def list_folders(self, cid: str = "0") -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.list_dir(cid) if e.is_dir]

    def mkdir(self, name: str, pid: str = "0") -> dict[str, Any]:
        """新建目录，返回新目录 id。"""
        self.require_login()
        cname = (name or "").strip()
        if not cname:
            raise P115Error("目录名不能为空")
        if len(cname) > 255:
            raise P115Error("目录名过长")
        if any(ch in cname for ch in '<>'):
            raise P115Error('目录名不能包含 < > 字符')
        resp = self.http.post(
            f"{WEBAPI}/files/add",
            data={"cname": cname, "pid": pid or "0"},
        )
        data = self._ok(self._json(resp, what="新建目录"), what="新建目录")
        d = data.get("data") or {}
        new_cid = str(d.get("cid") or data.get("cid") or "")
        return {"cid": new_cid, "name": d.get("cname") or cname}

    def ensure_path(self, path: str, *, root_cid: str = "0") -> str:
        """按 /a/b/c 逐级创建目录，返回最终 cid。"""
        segments = [s.strip() for s in (path or "").split("/") if s.strip()]
        cid = root_cid or "0"
        for seg in segments:
            existing = {e.name: e.id for e in self.list_dir(cid) if e.is_dir}
            if seg in existing:
                cid = existing[seg]
            else:
                cid = self.mkdir(seg, cid)["cid"] or cid
        return cid

    def file_count(self, cid: str = "0") -> int:
        """统计某个目录下的条目数（用于「是否已转存过」的轻量判断）。"""
        try:
            return len(self.list_dir(cid, limit=300))
        except P115Error:
            return 0

    def delete(self, entry_ids: Iterable[str], *, parent_id: str = "0") -> dict[str, Any]:
        """把目录/文件移入回收站（115 的删除即入回收站，可还原）。

        POST https://webapi.115.com/rb/delete
        注意：删除与还原互斥，不要并发调用。
        """
        self.require_login()
        ids = [str(i) for i in entry_ids if str(i).strip()]
        if not ids:
            raise P115Error("没有指定要删除的 id")
        payload: dict[str, Any] = {"fid": ",".join(ids)}
        if parent_id:
            payload["pid"] = parent_id
        resp = self.http.post(f"{WEBAPI}/rb/delete", data=payload)
        data = self._json(resp, what="删除")
        if data.get("state") is True or data.get("state") == 1:
            return {"ok": True, "count": len(ids), "raw": data}
        msg = data.get("error") or data.get("msg") or "删除失败"
        errno = data.get("errno")
        raise P115Error(f"{msg}（errno={errno}）" if errno else str(msg))

    # ---- 分享 ----------------------------------------------------------
    def share_snap(
        self,
        share_code: str,
        receive_code: str = "",
        cid: str = "0",
        *,
        limit: int = 200,
    ) -> list[P115Entry]:
        """列出分享内的文件/目录。"""
        resp = self.http.get(
            f"{WEBAPI}/share/snap",
            params={
                "share_code": share_code,
                "receive_code": receive_code or "",
                "cid": cid or "0",
                "limit": limit,
                "offset": 0,
                "asc": 1,
                "o": "file_name",
            },
        )
        data = self._json(resp, what="读取分享内容")
        if data.get("state") is not True and data.get("state") != 1:
            msg = data.get("error") or data.get("msg") or "读取分享内容失败"
            raise P115Error(str(msg))
        d = data.get("data")
        rows: Any = d
        if isinstance(d, dict):
            rows = d.get("list") or d.get("data") or []
        if not isinstance(rows, list):
            rows = []
        out: list[P115Entry] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            fid = str(row.get("fid") or "")
            is_dir = bool(row.get("is_dir")) or (not fid and bool(row.get("cid")))
            entry_id = str(row.get("fid") or row.get("cid") or "")
            if not entry_id:
                continue
            out.append(
                P115Entry(
                    id=entry_id,
                    name=str(row.get("n") or row.get("file_name") or ""),
                    is_dir=is_dir,
                    size=int(row.get("s") or row.get("file_size") or 0),
                    parent_id=str(row.get("cid") or "0"),
                )
            )
        return out

    def receive(
        self,
        share_code: str,
        receive_code: str,
        file_ids: Iterable[str],
        cid: str = "0",
    ) -> dict[str, Any]:
        """转存分享内的指定条目到 cid。

        注意：file_id 指的是**分享内**的文件/目录 id（可由 share_snap 得到），
        目标目录是 cid —— 这两个参数容易混淆。
        """
        self.require_login()
        ids = [str(i) for i in file_ids if str(i).strip()]
        if not ids:
            raise P115Error("没有可转存的文件 id")
        resp = self.http.post(
            f"{WEBAPI}/share/receive",
            data={
                "share_code": share_code,
                "receive_code": receive_code or "",
                "file_id": ",".join(ids),
                "cid": cid or "0",
            },
            headers={"Referer": f"https://115.com/s/{share_code}"},
        )
        data = self._json(resp, what="转存")
        if data.get("state") is True or data.get("state") == 1:
            return {"ok": True, "count": len(ids), "raw": data}
        msg = data.get("error") or data.get("msg") or "转存失败"
        errno = data.get("errno")
        if errno == 40101017:
            msg = "分享已失效或提取码错误"
        elif errno == 4100002:
            msg = "该分享已被禁止转存"
        elif errno == 990009:
            msg = "转存过于频繁，请稍后再试"
        raise P115Error(f"{msg}（errno={errno}）" if errno else str(msg))

    def transfer_share_url(
        self,
        url: str,
        cid: str = "0",
        *,
        all_files: bool = True,
        file_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """转存一个 115 分享链接。"""
        share_code, receive_code = parse_share_url(url)
        if file_ids:
            ids = list(file_ids)
        elif all_files:
            entries = self.share_snap(share_code, receive_code)
            if not entries:
                raise P115Error("分享内没有可转存的内容（或需要提取码）")
            ids = [e.id for e in entries]
        else:
            raise P115Error("未指定要转存的文件")
        result = self.receive(share_code, receive_code, ids, cid)
        result.update({"share_code": share_code, "receive_code": receive_code, "files": len(ids)})
        return result

    # ---- 离线下载（磁力/电驴/直链）-------------------------------------
    def _cloud_sign(self) -> dict[str, str]:
        resp = self.http.get(
            "https://115.com/",
            params={"ct": "clouddownload", "ac": "space"},
            headers={"Accept": "application/json, text/javascript, */*; q=0.01"},
        )
        data = self._json(resp, what="获取离线下载签名")
        d = data.get("data") or data
        sign = d.get("sign") or ""
        time_ = d.get("time") or ""
        if not sign:
            raise P115Error("未能获取离线下载 sign（可能需要登录）")
        return {"sign": str(sign), "time": str(time_)}

    def offline_add(
        self,
        urls: Iterable[str],
        cid: str = "0",
        *,
        savepath: str = "",
    ) -> dict[str, Any]:
        """提交离线下载任务（支持 magnet / ed2k / http）。"""
        self.require_login()
        items = [u.strip() for u in urls if (u or "").strip()]
        if not items:
            raise P115Error("没有可提交的链接")
        sign = self._cloud_sign()
        # 注意：ac 与参数一起放在 POST body 里（网页版 /lixianssp/ 的约定）
        payload: dict[str, Any] = {
            "ac": "add_task_urls",
            "sign": sign["sign"],
            "time": sign["time"],
            "wp_path_id": cid or "0",
        }
        if savepath:
            payload["savepath"] = savepath
        for i, u in enumerate(items):
            payload[f"url[{i}]"] = u
        resp = self.http.post(
            f"{CLOUD_BASE}/lixianssp/",
            data=payload,
            headers={"Referer": "https://115.com/"},
        )
        data = self._json(resp, what="添加离线任务")
        if data.get("state") is True or data.get("state") == 1:
            return {"ok": True, "count": len(items), "raw": data}
        msg = data.get("error") or data.get("msg") or "添加离线任务失败"
        errno = data.get("errno")
        raise P115Error(f"{msg}（errno={errno}）" if errno else str(msg))

    def offline_list(self) -> list[dict[str, Any]]:
        resp = self.http.post(
            f"{CLOUD_BASE}/lixianssp/",
            data={"ac": "task_lists", "page": "1"},
            headers={"Referer": "https://115.com/"},
        )
        data = self._json(resp, what="读取离线任务")
        rows = (data.get("data") or {}).get("tasks") if isinstance(data.get("data"), dict) else data.get("data")
        return rows or []