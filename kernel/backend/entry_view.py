# -*- coding: utf-8 -*-
"""入口维度（C5 → Track L4）：把服务的「域名 → 边缘 → 内网」一层层摊出来。

C5 原方案的「入口拓扑视图」评审后并入 L4 作**入口维度**，并**去掉 Cloudflare 层**
（全仓无任何 CF 集成）。本模块是那一维的后端投影，纪律有三条：

1. **零新探测**：只读两样已经在手的数据——``assets.yaml`` 的
   ``access.{home,tailscale,domain}`` 静态字段，和 C11 已有的 Caddy upstream 快照
   （``caddy_view``，NAS crontab 导出文件 + 600s TTL）。不发任何请求、不跑 ssh /
   docker exec，因此也**不会碰 health_probe:false 与 openviking 的压制纪律**——
   压制的是探测，这里一次探测都没有。
2. **不外泄内网地址**：投影只回 ``domain``（公网域名，本就公开）与三个布尔位，
   ``access.tailscale`` / ``access.home`` 的地址**不进 entry**（服务台账本身另有
   ``access`` 字段供前端做链接，那是既有设计；entry 不再复制一份）。
3. **边缘层怎么判**：服务名出现在 Caddy upstream 名单里即 ``edge='caddy'``。
   名单来自 ``caddy_view``，它已经用 ``access.tailscale`` 把 upstream 的 tailnet
   地址反查成了服务名（零 IP 出后端），这里直接用名字比对，不重做地址匹配。
   快照不可用（cron 未就绪 / 文件缺失）时 ``edge=None`` + ``edge_known=False``——
   **显式「未知」而不是假装「没有」**，与 C2/C11 的降级口径一致。

投影字段是白名单式的固定五键，前端按 ``layers`` 顺序渲染即可：

``{domain, edge, edge_known, tailscale, home, layers}``
"""


def _access(svc):
    access = (svc or {}).get("access")
    return access if isinstance(access, dict) else {}


def _text(value):
    text = str(value or "").strip()
    return text or None


def caddy_upstream_names(snapshot):
    """C11 快照 → 服务名集合；不可用时返回 None（**未知**，不是空集合）。"""
    if not isinstance(snapshot, dict) or not snapshot.get("available"):
        return None
    upstreams = snapshot.get("upstreams")
    if not isinstance(upstreams, list):
        return None
    return {
        str(row.get("name")).strip()
        for row in upstreams
        if isinstance(row, dict) and _text(row.get("name"))
    }


def entry_projection(svc, upstream_names=None):
    """单条服务的入口维度投影（零探测，纯静态字段 + C11 快照名单）。"""
    access = _access(svc)
    domain = _text(access.get("domain"))
    name = _text((svc or {}).get("name"))
    edge_known = upstream_names is not None
    edge = "caddy" if edge_known and name and name in upstream_names else None
    tailscale = bool(_text(access.get("tailscale")))
    home = bool(_text(access.get("home")))

    layers = []
    if domain:
        layers.append("domain")
    if edge:
        layers.append("edge")
    if tailscale:
        layers.append("tailscale")
    if home:
        layers.append("home")

    return {
        "domain": domain,
        "edge": edge,
        # False = 边缘数据源不可用（快照缺失），前端显示「未知」而不是「无边缘」
        "edge_known": edge_known,
        "tailscale": tailscale,
        "home": home,
        "layers": layers,
    }


def attach_entries(services, snapshot=None):
    """给一批服务字典就地补 ``entry`` 字段；返回同一个列表（便于链式使用）。"""
    upstream_names = caddy_upstream_names(snapshot)
    for svc in services or []:
        if isinstance(svc, dict):
            svc["entry"] = entry_projection(svc, upstream_names)
    return services
