"""离线自检：不写入 115 的前提下，验证解析、筛选、排序与安全上限。

用法：  python selftest.py
需要网络（会访问蜗牛站点少量页面），不会触碰 115 账号。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.service import MAX_PAGES, Service, quality_rank  # noqa: E402
from app.site import classify_link  # noqa: E402

PASS, FAIL = "  [OK] ", "  [FAIL] "


def check(label: str, cond: bool, extra: str = "") -> bool:
    print((PASS if cond else FAIL) + label + (f"  {extra}" if extra else ""))
    return cond


def main() -> int:
    ok = True
    print("=== 1. 链接识别 ===")
    cases = [
        ("https://115.com/s/swzab12c?password=abc1", "share", "swzab12c", "abc1"),
        ("https://115cdn.com/s/abcd1234", "share", "abcd1234", ""),
        ("magnet:?xt=urn:btih:ABCDEF123456", "magnet", "", ""),
        ("ed2k://|file|m.mkv|123|HASH|/", "ed2k", "", ""),
        ("https://pan.baidu.com/s/1xyz", "baidu", "", ""),
    ]
    for url, kind, code, rcode in cases:
        got = classify_link(url)
        ok &= check(f"{url[:42]} -> {kind}", got == (kind, code, rcode), str(got))

    print("\n=== 2. 清晰度排序权重 ===")
    ok &= check("4K > 1080P", quality_rank("4K WEB") > quality_rank("1080P蓝光"))
    ok &= check("蓝光原盘 > 720P", quality_rank("蓝光原盘") > quality_rank("720P"))
    ok &= check("无法识别 -> 0", quality_rank("未知") == 0)

    svc = Service()

    print("\n=== 3. 站点状态 ===")
    st = svc.status()
    print(f"  站点登录: {st['site']['logged_in']}  115登录: {st['p115']['logged_in']}")
    ok &= check("站点可访问（能拿到分类）", len(st["categories"]) == 4)

    print("\n=== 4. 分类列表 + 分面 ===")
    data = svc.list_videos(category=1, page=1, pages=1)
    ok &= check("电影分类有数据", data["collected"] > 0, f"{data['collected']} 条")
    ok &= check("总页数已识别", data["total_pages"] > 0, f"{data['total_pages']} 页")
    ok &= check("分面含年份/地区/清晰度", all(data["facets"].get(k) for k in ("year", "region", "quality")))
    print(f"  抓取 {data['collected']} 条，显示 {data['shown']} 条")

    print("\n=== 5. 排序 ===")
    by_score = svc.list_videos(category=1, page=1, pages=1, sort="score", order="desc")
    scores = [float(i["score"] or 0) for i in by_score["items"] if i["score"]]
    ok &= check("评分降序", scores == sorted(scores, reverse=True), str(scores[:5]))
    by_quality = svc.list_videos(category=1, page=1, pages=1, sort="quality", order="desc")
    ranks = [quality_rank(i["quality"]) for i in by_quality["items"]]
    ok &= check("清晰度降序", ranks == sorted(ranks, reverse=True))

    print("\n=== 6. 筛选 ===")
    flt = svc.list_videos(category=1, page=1, pages=1, regions=["US"])
    ok &= check("地区筛选生效", all("US" in (i["region"] or "") for i in flt["items"]), f"{flt['shown']} 条")
    flt2 = svc.list_videos(category=1, page=1, pages=1, min_score=8)
    vals = [float(i["score"] or 0) for i in flt2["items"]]
    ok &= check("最低分筛选生效", all(v >= 8 for v in vals), f"{flt2['shown']} 条")

    print("\n=== 7. 分页上限（防全站拉取）===")
    multi = svc.list_videos(category=4, page=1, pages=2)
    ok &= check(f"可显式加载 2 页（上限 {MAX_PAGES}）", multi["pages_loaded"] == 2, f"{multi['collected']} 条")

    print("\n=== 8. 详情解析 ===")
    detail = svc.detail(18005, refresh=True)
    ok &= check("标题解析", bool(detail["title"]), detail["title"])
    ok &= check("标签解析", len(detail["tags"]) >= 3, str(detail["tags"]))
    ok &= check("简介解析", len(detail["desc"]) > 10, f"{len(detail['desc'])} 字")
    ok &= check("资源条目解析", detail["link_count"] > 0, f"{detail['link_count']} 条")
    print(f"  可用 {detail['usable_count']} 条（游客通常为 0，属正常）")

    print("\n=== 9. 安全上限 ===")
    try:
        svc.transfer_plan([{"vod_id": i} for i in range(1, 60)])
        ok &= check("超量选择被拒绝", False, "竟然通过了")
    except ValueError as exc:
        ok &= check("超量选择被拒绝", "上限" in str(exc), str(exc)[:60])

    # 未登录与已登录两种状态下的行为都应当正确，不能写死
    plan = svc.transfer_plan([{"vod_id": 18005}])
    item = plan["items"][0]
    site_in = bool(st.get("site", {}).get("logged_in"))
    if site_in:
        print("  （站点已登录：应能取到真实链接）")
        ok &= check("已登录时能取到可用链接", plan["links"] >= 1, f"{plan['links']} 条")
        ok &= check("链接类型为 115 分享", all(p["kind"] == "share" for p in item["picked"]))
        ok &= check("分享码已解析", all(p["share_code"] for p in item["picked"]))
        ok &= check("无「需登录」提示", not item["warning"], item["warning"][:40])
    else:
        print("  （站点未登录：应拒绝锁定链接并提示）")
        ok &= check("锁定链接不进入转存队列", plan["links"] == 0 and item["picked_count"] == 0)
        ok &= check("并给出明确提示", bool(item["warning"]), item["warning"][:40])

    # 非 115 资源（如百度）永远不应进入转存队列
    mixed = svc.detail(35360, refresh=True)
    if mixed["links"] and all(l["kind"] == "baidu" for l in mixed["links"]):
        mp = svc.transfer_plan([{"vod_id": 35360, "link_indexes": [0]}])
        ok &= check("百度等非 115 资源被跳过", mp["links"] == 0 and bool(mp["items"][0]["skipped"]))

    print("\n=== 10. 115 未登录时的保护（不产生任何写入）===")
    # 用一个没有 cookie 的独立客户端验证守卫生效——绝不能真的发起转存
    from app.p115 import P115Client, P115Error

    guard = P115Client(cookie="")
    ok &= check("空客户端判定为未登录", not guard.logged_in)
    try:
        guard.require_login()
        ok &= check("未登录时拒绝操作", False, "竟然通过了")
    except P115Error as exc:
        ok &= check("未登录时拒绝操作", True, str(exc)[:40])

    # 生产服务同样应当拒绝（无论当前是否已登录 115，都不能因为自检而写入）
    try:
        svc.p115.delete([])  # 空列表应被拒绝，且不会触碰网络
        ok &= check("空删除请求被拒绝", False, "竟然通过了")
    except Exception as exc:  # noqa: BLE001
        ok &= check("空删除请求被拒绝", True, str(exc)[:40])

    print("  （注意：本自检从不发起真实转存，也不会向 115 写入任何内容）")

    print("\n" + ("全部通过 ✔" if ok else "存在失败项 ✘"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())