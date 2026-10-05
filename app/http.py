"""带限速与重试的 HTTP 会话。

对站点和 115 都保持低频访问：默认串行 + 最小间隔 + 抖动。
这是「不批量拉站」的技术兜底，而不是靠自觉。
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any

import requests

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class RateLimitedSession:
    """线程安全的限速 requests.Session 包装。"""

    def __init__(
        self,
        *,
        min_interval: float = 1.0,
        jitter: float = 0.4,
        timeout: float = 25.0,
        retries: int = 2,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self.jitter = max(0.0, float(jitter))
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self._lock = threading.RLock()
        self._last_at = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_UA})
        if headers:
            self.session.headers.update(headers)

    # ---- 限速 ----------------------------------------------------------
    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self.min_interval - (now - self._last_at)
            if wait > 0:
                time.sleep(wait + random.uniform(0, self.jitter))
            self._last_at = time.monotonic()

    def set_interval(self, seconds: float) -> None:
        with self._lock:
            self.min_interval = max(0.0, float(seconds))

    # ---- 请求 ----------------------------------------------------------
    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("allow_redirects", True)
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                resp = self.session.request(method, url, **kwargs)
            except requests.RequestException as exc:  # 网络层重试
                last_error = exc
                if attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
            # 429/5xx 退避重试
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if (retry_after or "").isdigit() else 2.0 * (attempt + 1)
                time.sleep(min(delay, 30.0))
                continue
            return resp
        raise last_error if last_error else RuntimeError("request failed")

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)

    # ---- cookie --------------------------------------------------------
    def set_cookie_string(self, cookie: str, domain: str = "") -> None:
        """把浏览器里复制的一整条 cookie 串灌进会话。

        domain 必须传对：若用 ``cookies.set(name, value)``（domain 为空），
        这条 cookie 会匹配所有域名，并且**会遮蔽服务端随后 Set-Cookie 下发的同名值**。
        实测后果：带着旧 cookie 登录蜗牛，站点返回「登录成功」，
        但请求里仍发送旧的 user_check，会话实际无效（访问 /user/index/ 被 302 回登录页）。
        所以这里显式绑定 domain，让服务端的新值能正常覆盖它。
        """
        self.session.cookies.clear()
        for part in (cookie or "").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, _, value = part.partition("=")
            name, value = name.strip(), value.strip()
            if not name:
                continue
            if domain:
                self.session.cookies.set(name, value, domain=domain, path="/")
            else:
                self.session.cookies.set(name, value)

    def clear_cookies(self) -> None:
        self.session.cookies.clear()

    def cookie_string(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.session.cookies)