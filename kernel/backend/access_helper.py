# -*- coding: utf-8 -*-
"""访问入口连通性探测。"""

from checker import check_http


def probe_url(url):
    """探测 URL 是否可访问。返回 (ok, message)。

    连通性只看「有没有 HTTP 响应」：把 expected_code 设为 None 后 check_http
    对任何状态码（含 401/403/5xx）都判 ok——入口探测要回答的是「这个地址
    活不活」，不是「业务对匿名访问回不回 200」。带鉴权的服务（如 magpie
    无 key 固定 401）按 200 判会把「活着」错存成「探测不通」。
    """
    if not url or not str(url).strip():
        return None, "未配置"

    url = str(url).strip()
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    ok, msg = check_http(url, timeout=5, expected_code=None)
    return ok, msg


def probe_access(home="", tailscale="", domain=""):
    """批量探测访问入口。"""
    result = {}
    for key, val in (("home", home), ("tailscale", tailscale), ("domain", domain)):
        if not val or not str(val).strip():
            result[key] = {"configured": False, "ok": None, "msg": "未配置"}
        else:
            ok, msg = probe_url(val)
            result[key] = {"configured": True, "ok": ok, "msg": msg}
    return result
