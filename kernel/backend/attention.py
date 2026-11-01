# -*- coding: utf-8 -*-
"""今日关注：聚合 probe / 版本 / feed / 流量既有状态，不新探测、不写库。"""
import datetime
import os

import yaml

import machine_config as mc
import monitor_events as me
import node_traffic as nt
import paths
import pg_diagnostics
import plugin_contributions
import probe_cache as pc
import tailscale_view as tv


def _normalize_tag(value):
    text = str(value or "").strip()
    # 与 version_service._normalize_tag 同规：剥 semver build metadata（`+` 之后），
    # 否则上游 tag 带打包信息（v0.8.11+obsidian.1.12.7）时本地 0.8.11 永远判过期。
    text = text.split("+", 1)[0]
    if text.lower().startswith("v") and len(text) > 1 and text[1].isdigit():
        return text[1:]
    return text


def _is_outdated(local, upstream, behind_count=0, update_status=""):
    if int(behind_count or 0) > 0 or str(update_status or "") == "可更新":
        return True
    local_n = _normalize_tag(local)
    upstream_n = _normalize_tag(upstream)
    if local_n in ("", "N/A", "未知", "latest") or upstream_n in ("", "N/A", "未知"):
        return False
    return local_n != upstream_n


def _load_services(assets_path):
    if not assets_path or not os.path.exists(assets_path):
        return []
    with open(assets_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    return data if isinstance(data, list) else []


def _active_events(db_path):
    """活动事件，只读 monitor_events（不新探测、不写库，符合本模块契约）。

    表还没建或读不出来一律空列表 —— 机器折叠与公网项都是加分项，拿不到就
    退化回逐服务平铺的老行为。
    """
    try:
        return me.active_events(db_path=db_path)
    except Exception:
        return []


def _down_machines(events):
    """K3：当前有活动「机器探测失败」事件的机器 → {machine_id: event}。"""
    return {
        event["machine"]: event
        for event in events
        if event.get("kind") == me.KIND_MACHINE and event.get("machine")
    }


def _machine_down_items(down_machines, services):
    """K3 根因项：机器掉线时只出**一条**，摘要写「影响 N 个登记服务」。

    N 取**台账口径**（该机器上登记的服务条数），不是「此刻探测失败的服务数」——
    机器探针与服务健康的 TTL 不同（默认 1800s vs 600s），机器刚 down 时同机服务
    的健康缓存往往还是旧的 Healthy，用实测数会给出一个随刷新时机跳动的假数字。
    实测口径留给事件页（`monitor_events.list_events` 回的 `impact_count`）。
    """
    counts = {}
    for svc in services:
        machine = str((svc or {}).get("machine") or "").strip().lower()
        if machine:
            counts[machine] = counts.get(machine, 0) + 1

    items = []
    for machine, event in sorted(down_machines.items()):
        affected = counts.get(machine, 0)
        detail = str(event.get("detail") or "").strip() or "探测失败"
        if affected:
            detail = f"{detail}；影响 {affected} 个登记服务"
        items.append(
            {
                "severity": "error",
                "kind": "machine",
                "title": str(event.get("title") or f"{machine.upper()} 探测失败"),
                "detail": detail,
                "link": f"/devices/{machine}",
            }
        )
    return items


def _public_down_items(events, down_machines):
    """家宽公网不通项（warn）：机器在线、代理端口从家宽连不上。

    机器本身已掉线时公网必然也不通，由 K3 根因项覆盖，这里不再重复出。
    """
    items = []
    for event in sorted(events, key=lambda e: str(e.get("machine") or "")):
        machine = event.get("machine")
        if event.get("kind") != me.KIND_PUBLIC or not machine or machine in down_machines:
            continue
        items.append(
            {
                "severity": "warn",
                "kind": "public",
                "title": str(event.get("title") or f"{machine.upper()} 家宽公网不通"),
                "detail": str(event.get("detail") or "").strip() or "家宽公网探测失败",
                "link": f"/devices/{machine}",
            }
        )
    return items


def _probe_items(services, down_machines=None):
    """服务健康失败项。同机已有「机器探测失败」根因项时折叠掉，不再单独刷屏。"""
    down = down_machines or {}
    items = []
    for svc in services:
        name = (svc or {}).get("name")
        if not name:
            continue
        if str((svc or {}).get("machine") or "").strip().lower() in down:
            # K3：机器掉线是根因，同机服务失败只是它的影响面
            continue
        peeked = pc.peek_health(name)
        if not peeked:
            continue
        status, msg = peeked
        if status != "Unhealthy":
            continue
        items.append(
            {
                "severity": "error",
                "kind": "probe",
                "title": f"{name} 健康检查失败",
                "detail": str(msg or "Unhealthy"),
                "link": "/devices",
            }
        )
    return items


def _version_items(db_path):
    """版本落后项；在线 PG 经 pg_diagnostics 只读入口（PG-04），具名列。"""
    items = []
    with pg_diagnostics.readonly_connection(db_path) as conn:
        try:
            rows = conn.execute(
                """
                SELECT svc_name, local_version, upstream_version,
                       behind_count, update_status
                FROM versions
                """
            ).fetchall()
        except Exception:
            # 表尚未创建时视为无版本数据
            return items

    for row in rows:
        name = row["svc_name"]
        local = row["local_version"]
        upstream = row["upstream_version"]
        if str(row["update_status"] or "") == "自部署":
            continue
        if not _is_outdated(
            local, upstream, row["behind_count"], row["update_status"]
        ):
            continue
        items.append(
            {
                "severity": "warn",
                "kind": "version",
                "title": f"{name} 有可更新版本",
                "detail": f"{local or 'N/A'} → {upstream or 'N/A'}",
                "link": "/devices",
            }
        )
    return items


def _node_traffic_items(db_path):
    """E1：月流量逼近/超出配额。只读 machine_traffic 账本，不碰 Beszel。"""
    items = []
    try:
        alerts = nt.quota_alerts(db_path=db_path)
    except Exception:
        return items
    for alert in alerts:
        machine = alert["machine"]
        usage = alert["usage"]
        over = usage["pct"] >= 100
        items.append(
            {
                "severity": "error" if over else "warn",
                "kind": "traffic",
                "title": "{} 月流量{}".format(
                    machine["label"], "已超配额" if over else "接近配额"
                ),
                "detail": "{} 已用 {} / {} GB（{}%，计费网卡 {}）".format(
                    usage["month"],
                    usage["used_gb"],
                    usage["quota_gb"],
                    usage["pct"],
                    usage["nic"] or "自动",
                ),
                "link": "/devices",
            }
        )
    return items


def _traffic_projection_items(db_path):
    """M1：**按当前速率**月底会超配额 → warn。只读账本，不碰 Beszel。

    与 `_node_traffic_items` 的分工：那条报「已经用了多少」（实测），这条报
    「照这个速度会用到多少」（预测）。**已经触发实测告警（≥80%）的机器这里不再报**
    —— 同一台机器出两条只是噪音，实测那条更硬。

    预测本身只在观测满 72 小时后才有（`node_traffic.month_projection`），
    所以新装的 agent 不会一上来就报。设计 §5.7：**预测只作 warning**，
    永远不升级成 error，也不触发任何动作。
    """
    items = []
    try:
        machines = mc.load_machines() or {}
    except Exception:
        return items
    for machine in machines.values():
        if not machine.get("traffic_quota_gb"):
            continue
        try:
            usage = nt.usage(machine["id"], db_path=db_path)
        except Exception:
            continue
        if not usage:
            continue
        # 实测已经进关注了（≥80%）就别再叠一条预测
        if usage.get("pct") is not None and usage["pct"] >= nt.WARN_RATIO * 100:
            continue
        projection = usage.get("projection")
        if not projection or not projection.get("over_quota"):
            continue
        detail = "按 {} GB/天推算，本月将用 {} GB / {} GB（{}%）".format(
            projection["daily_gb"],
            projection["projected_gb"],
            projection["quota_gb"],
            projection["projected_pct"],
        )
        if projection.get("partial_month"):
            detail += "；账本自 {} 起算，非整月".format(usage.get("since") or "未知")
        items.append(
            {
                "severity": "warn",
                "kind": "traffic-forecast",
                "title": "{} 月流量预计超配额".format(machine["label"]),
                "detail": detail,
                "link": "/devices",
            }
        )
    return items


def _probe_monitor_items():
    """C10-a 自监控（评审新建议 D）：机器探针守护线程超过两个探针周期未运行 → warn。

    只读 effective_config 的内存时间戳（守护线程每轮 mark_probe_run），不触发探测、
    不写库。从未跑过（进程刚起、线程未到首个节点）也报——两个周期还没有第一轮，
    与「跑着跑着停了」一样值得进关注项。
    """
    items = []
    try:
        import effective_config as ec

        reference = ec.probe_run_reference_utc()
    except Exception:
        return items
    interval = pc.machine_probe_interval()
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        moment = datetime.datetime.fromisoformat(
            str(reference).replace("Z", "+00:00")
        )
        stale_seconds = (now - moment).total_seconds()
    except (ValueError, TypeError):
        stale_seconds = float("inf")
    if stale_seconds > interval * 2:
        detail = (
            f"上次探针运行于 {reference}（UTC）"
            if ec.last_probe_run_utc()
            else "本进程内机器探针尚未运行"
        )
        items.append(
            {
                "severity": "warn",
                "kind": "probe-monitor",
                "title": "机器探针长时间未运行",
                "detail": detail,
                "link": "/settings",
            }
        )
    return items


def _tailnet_items():
    """C2/W2：Tailscale key 剩余 ≤7 天进今日关注（复用 renewals 7 天窗口语义）。

    只读 tailscale_view 的缓存快照（peek），绝不触发 LocalAPI 调用；
    快照未采样或显式降级（available=False）时不产生条目——降级态由
    总控台的 tailnet 卡片自己呈现，今日关注不重复报警。
    """
    items = []
    try:
        snap = tv.peek_tailnet_snapshot()
    except Exception:
        return items
    if not isinstance(snap, dict) or not snap.get("available"):
        return items
    for row in snap.get("machines") or []:
        if not isinstance(row, dict):
            continue
        days = row.get("key_expiry_days")
        if days is None or isinstance(days, bool):
            # None = 不过期/未知，静默跳过
            continue
        try:
            days = int(days)
        except (TypeError, ValueError):
            continue
        machine = str(row.get("machine_id") or "").strip() or "未知机器"
        if days < 0:
            items.append(
                {
                    "severity": "error",
                    "kind": "tailnet",
                    "title": f"{machine} Tailscale key 已过期",
                    "detail": f"已过期 {-days} 天，机器将无法接入 tailnet",
                    "link": "/",
                }
            )
        elif days <= 7:
            items.append(
                {
                    "severity": "warn",
                    "kind": "tailnet",
                    "title": f"{machine} Tailscale key 即将到期",
                    "detail": f"剩余 {days} 天",
                    "link": "/",
                }
            )
    return items




def _monitoring_items():
    return [{
        "severity": "warn",
        "kind": "monitoring-coverage",
        "title": "原生监控有未覆盖能力",
        "detail": "systemd 服务与 SMART/mdraid 尚未接入 PG；空列表不代表检查通过。",
        "link": "/devices",
    }]


def collect_attention(assets_path=None, db_path=None):
    """返回关注项列表；仅读缓存/库/内存错误态。"""
    assets = assets_path or os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "assets.yaml")
    )
    database = db_path or paths.DB_PATH
    services = _load_services(assets)
    # K3：先取活动的机器故障作根因，同机服务失败折叠进它的「影响 N 个登记服务」
    events = _active_events(database)
    down_machines = _down_machines(events)
    return (
        _machine_down_items(down_machines, services)
        + _public_down_items(events, down_machines)
        + _probe_items(services, down_machines)
        + _version_items(database)
        + plugin_contributions.attention_items(database)
        + _node_traffic_items(database)
        + _traffic_projection_items(database)
        + _tailnet_items()
        + _monitoring_items()
        + _probe_monitor_items()
    )
