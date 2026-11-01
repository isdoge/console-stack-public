# -*- coding: utf-8 -*-
"""服务健康探测结果缓存，降低 TCP/HTTP 探测频率。

机器地址不再本地硬编码，统一取 machine_config registry（machines.yaml）。

采集门禁（PG-02 / Q1）：`check_service_health` / `check_machine_probe` /
`probe_all_machines` 只由 authority 发起真实探测。client 命中本地缓存就返回缓存，
否则返回 Unknown/unknown——**不发起任何 TCP/HTTP 请求，也不落历史、告警、流量**。
Mac 要看到真实观测结果，走 §3.3 的固定路由转交 NAS（app.py 的 client 转发）。
"""
import ipaddress
import math
import os
import socket
import time
from datetime import datetime, timedelta, timezone

import collect_guard
import machine_config as mc
import node_probe
from checker import check_http, check_http_semantic, check_tcp, get_ssl_expiry_days
from validation import validate_health_probe

_cache = {}
# 机器探活（/api/machines）与服务健康分开存，避免 name 撞 machine id
_machine_cache = {}
# 家宽公网探测（public_probe）再单独一份：同一台机器的两种探测互不顶掉
_public_cache = {}

# mihomo fake-ip 段（台账默认 198.18.0.1/16，取 RFC 2544 整段 /15 更稳）：
# 解析落在这里说明域名没进 fake-ip-filter，连过去必被 TUN 本地代答
_FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")
# 公网握手的可信下限。代理节点都在境外，真实 RTT 百毫秒级；
# NAS mihomo TUN 代答实测 0.1–5ms，低于这条线只可能是没绕过去
PUBLIC_PROBE_MIN_PLAUSIBLE_MS = 10.0
# 单次 connect 超时：容得下 1s、2s 两次 SYN 重传（家宽到境外实测偶发 1.2s 样本）
PUBLIC_PROBE_TIMEOUT = 5
PUBLIC_PROBE_ATTEMPTS = 2


def _trusted_ssl_days(ok, value):
    if ok is not True or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def probe_interval():
    try:
        return max(60, int(os.environ.get("CONSOLE_PROBE_INTERVAL", "600")))
    except ValueError:
        return 600


def machine_probe_interval():
    """机器探针（E4 色带）的采样间隔，独立于服务健康 TTL。

    默认 1800s（每半小时）：色带节点对齐到本地半点，与服务健康/镜像快照的
    CONSOLE_PROBE_INTERVAL 解耦——放慢探针不该连带放慢 Beszel 曲线刷新。
    """
    try:
        return max(60, int(os.environ.get("CONSOLE_MACHINE_PROBE_INTERVAL", "1800")))
    except ValueError:
        return 1800


def public_probe_interval():
    """家宽公网探测的采样间隔（秒），默认 300。

    独立于机器探针的 1800s：代理节点的公网封堵常是几十分钟级的间歇
    （2026-09-28 us 一晚上断了好几次），半点采样会整段漏掉。
    """
    try:
        return max(60, int(os.environ.get("CONSOLE_PUBLIC_PROBE_INTERVAL", "300")))
    except ValueError:
        return 300


def public_history_key(machine_id):
    """家宽公网时序在 probe_history 里的 machine 键：``<id>:public``。

    复用 probe_history 表而不开迁移：机器 id 过 validate_identifier（字母数字
    与 ``._-``，不含冒号），带冒号的键不会撞上任何真实机器，按机器 id 查的
    既有读者（色带、巡检报告）也读不到它。
    """
    return "{}:public".format(str(machine_id or "").strip().lower())


def next_probe_slot(now=None, interval=None):
    """下一个本地对齐的探测时间节点。

    interval=1800 时节点即每个半点（00:00 / 00:30 / 01:00 …以此类推），
    从本地零点起算，改大间隔后节点仍落在整刻度上。
    """
    interval = interval or machine_probe_interval()
    now = now or datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (now - midnight).total_seconds()
    return midnight + timedelta(seconds=(int(elapsed) // interval + 1) * interval)


def _semantic_probe(svc):
    """取台账里的语义检查配置（L1）；没配 / 配错都回 None（退回默认推断）。

    坏配置**不报故障**：把「YAML 写错了」显示成「服务挂了」会误导排障。
    写回路径（`service_manager._build_entry`）已经在保存时校验过，这里能读到
    坏值只可能是手工编辑 `assets.yaml` 造成的。
    """
    raw = svc.get("health_probe")
    if not isinstance(raw, dict):
        return None
    try:
        return validate_health_probe(raw)
    except ValueError:
        return None


def _run_semantic_probe(probe, access):
    """按 `health_probe` 对象跑一次有限 HTTP 检查，返回 (status, msg)。"""
    target = probe.get("target", "domain")
    url = str((access or {}).get(target) or "").strip()
    if not url:
        # 配了 target=domain 但台账没有 domain：这是配置缺口不是服务故障
        return "Unknown", f"health_probe.target={target} 在 access 里没有地址"
    ok, msg = check_http_semantic(
        url,
        expected_status=probe.get("expected_status"),
        max_latency_ms=probe.get("max_latency_ms"),
        body_contains=probe.get("body_contains"),
    )
    return ("Healthy" if ok else "Unhealthy"), msg


def _reclaim_disabled_event(name, machine=""):
    """``health_probe: false`` 的服务没有真实探测，也就没有 ``ok=True`` 的观测来关闭
    旧事件——从「探测」切到「不探测」的服务会留下永久悬挂的活动事件（实测
    beszel-agent-txy/aly 自 09-07 挂了 16 天未恢复，agent 实际早已重连）。

    这里在关闭探测的真实路径上补一次 ``ok=True`` 观测：有同指纹活动事件就关闭，
    没有则 ``observe`` 返回 ``None`` 且不写行（见 ``monitor_events._observe``）。
    只在过缓存、过采集门禁的真实路径调用，缓存命中不重复查库。
    """
    try:
        import monitor_events

        monitor_events.observe_service(
            name, "Healthy", machine=machine, detail="探测已由 assets.yaml 关闭"
        )
    except Exception:
        # 与健康时序/事件落库同纪律：写不进去不影响探测主线
        pass


def _clear_stale_band(name):
    """关闭探测时清掉残留的健康时序红格（见 service_health_history.clear）。

    与 `_reclaim_disabled_event` 同路径同纪律：只在真实路径调用，
    清不掉不影响探测主线。
    """
    try:
        import service_health_history as shh

        shh.clear(name)
    except Exception:
        pass


def check_service_health(svc, force=False):
    """对 assets.yaml 单项做健康检查，默认缓存 CONSOLE_PROBE_INTERVAL 秒。

    探测方式三选一（优先级从上到下）：``health_probe: false`` 关闭 →
    ``health_probe: {...}`` 语义检查（L1）→ 按 port/type 的默认 TCP/HTTP 推断。

    **非 authority 实例不探测**（Q1）：命中本地缓存返回缓存，否则回 Unknown。
    """
    name = svc.get("name") or "unknown"
    ttl = probe_interval()
    now = time.time()
    cached = _cache.get(name)
    if not force and cached and now - cached["ts"] < ttl:
        return cached["status"], cached["msg"]
    if not collect_guard.collection_allowed():
        if cached:
            return cached["status"], cached["msg"]
        return "Unknown", "本实例不采集（观测请求请转交 authority 实例）"

    # 内部数据库、仅 loopback 绑定的端口、主动出站 agent 等没有可从机器
    # Tailscale IP 探测的入口。由台账显式关闭，避免把“不可从宿主外访问”
    # 误报成服务故障；缺省仍保持现有自动探测行为。
    if svc.get("health_probe") is False:
        entry = {
            "ts": now,
            "status": "Unknown",
            "msg": "Health probe disabled by assets.yaml",
        }
        _cache[name] = entry
        _reclaim_disabled_event(name, svc.get("machine", ""))
        _clear_stale_band(name)
        return entry["status"], entry["msg"]

    port = svc.get("port")
    machine = svc.get("machine") or mc.local_machine()
    access = svc.get("access") or {}
    domain_url = access.get("domain") or ""
    status, msg = "Unknown", "No checker configured"
    trusted_ssl_days = None

    semantic = _semantic_probe(svc)
    if semantic is not None:
        # L1：台账显式配了语义检查 → 优先于下面按 port/type 的默认推断。
        # 这条分支存在的理由：端口通 ≠ 业务好，500 也能 Accept 连接。
        status, msg = _run_semantic_probe(semantic, access)
    elif port and svc.get("type") in ["docker", "docker-compose", "process"]:
        if not machine:
            msg = "未配置服务机器或组合本机身份"
        else:
            # Tailscale IP 优先，无 TS 的机器退到公网域名，仍取不到才回环。
            ip = mc.probe_host(machine) or "127.0.0.1"
            ok, msg = check_tcp(ip, port)
            status = "Healthy" if ok else "Unhealthy"
    elif domain_url:
        ok, msg = check_http(domain_url)
        status = "Healthy" if ok else "Unhealthy"

    # 证书采集独立于健康探测分支：type+port 且带 https domain 的服务也走 TCP 探活，
    # 但仍要采集证书可信值（§4.4）。
    if domain_url.strip().lower().startswith("https://"):
        ssl_ok, ssl_days = get_ssl_expiry_days(domain_url)
        trusted_ssl_days = _trusted_ssl_days(ssl_ok, ssl_days)
        if trusted_ssl_days is not None:
            import plugin_contributions
            plugin_contributions.collect('certificate', url=domain_url, days=trusted_ssl_days)

    entry = {"ts": now, "status": status, "msg": msg}
    if trusted_ssl_days is not None:
        entry["ssl_days"] = trusted_ssl_days
        entry["ssl_ok"] = True
    _cache[name] = entry
    # T12 分支实验：真实探测才落健康时序（命中缓存/Unknown/显式关闭都不记）
    if status in ("Healthy", "Unhealthy"):
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            import service_health_history as shh

            shh.record(name, status, ts=stamp, db_path=None)
        except Exception:
            # 时序是加分项，写不进去不影响健康探测主线
            pass
        # K2：同一次真实探测顺带推进事件生命周期（只在状态变化时真正写行）
        try:
            import monitor_events

            monitor_events.observe_service(
                name, status, machine=machine, detail=str(msg or ""), ts=stamp
            )
        except Exception:
            # 与时序同纪律：事件写不进去不该影响健康探测主线
            pass
    return status, msg


def peek_health(name):
    """只读缓存，不发起探测。无缓存返回 None。"""
    cached = _cache.get(name)
    if not cached:
        return None
    return cached["status"], cached["msg"]


def peek_health_detail(name):
    """只读单项完整缓存副本；无缓存返回 None，绝不发起探测。"""
    cached = _cache.get(name)
    return dict(cached) if cached else None


def check_machine_probe(machine_id, force=False, db_path=None):
    """机器探活（node 与 server 都用），TTL 独立走 machine_probe_interval。

    探活目标按 role 分流（原 Q3「node 打 public_host 测业务可用性」已失效，
    2026-09-03 实测修订）：
    - **NAS 宿主跑着 mihomo（Clash Meta）TUN + fake-ip**：对任何公网目标（含真实
      IP）的 TCP 握手都在本地 TUN 内完成（实测 0.1–2ms），且节点宕机时 mihomo
      照样本地收握手——公网探测的延迟与 up/down 全是假的。ICMP 与 UDP 53（直连
      公共 DNS）也被拦或劫持，容器内测不到真实公网路径。
    - node：改走 Tailscale IP 打同一 probe 端口（sing-box 监听 0.0.0.0，同一进程），
      握手真实（可用性与耗时都可信）；无 Tailscale 的 node 退回 public_host。
    - server：走 tailscale IP（内网可达性，**不走公网**），不变。
    无 probe 配置的机器恒 unknown。
    返回 (status, msg, ts_iso)，status ∈ up|down|unknown；unknown 时 ts 为 None。
    """
    machine = mc.get_machine(machine_id)
    if not machine or not machine.get("probe"):
        return "unknown", "", None

    key = machine["id"]
    ttl = machine_probe_interval()
    now = time.time()
    cached = _machine_cache.get(key)
    if not force and cached and now - cached["ts"] < ttl:
        return cached["status"], cached["msg"], cached["ts_iso"]
    if not collect_guard.collection_allowed():
        if cached:
            return cached["status"], cached["msg"], cached["ts_iso"]
        return "unknown", "", None

    port = machine["probe"]["port"]
    if machine["role"] == "server":
        # Q3：服务器探活走 Tailscale IP，不碰公网
        host = mc.tailscale_ip(key)
    else:
        # mihomo fake-ip 会吃掉公网探测（见 docstring）：tailnet 直连才有真样本
        host = mc.tailscale_ip(key) or machine.get("public_host")
    if not host:
        return "unknown", "", None

    started = time.perf_counter()
    ok, detail = check_tcp(host, port)
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    status = "up" if ok else "down"
    msg = f"tcp {host}:{port} " + (f"ok {latency_ms}ms" if ok else str(detail))
    ts_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _machine_cache[key] = {"ts": now, "status": status, "msg": msg, "ts_iso": ts_iso}
    # E4：只有真探测才落时序，命中缓存的那些调用不重复写
    try:
        node_probe.record(key, status, latency_ms if ok else None, ts_iso, db_path=db_path)
    except Exception:
        # 探活是主线，时序是加分项：库写不进去不该让 /api/machines 挂掉
        pass
    # K2：同一次真探测顺带推进事件生命周期（只在 up⇄down 变化时真正写行）
    try:
        import monitor_events

        monitor_events.observe_machine(
            key,
            status,
            label=machine.get("label") or "",
            detail=msg,
            ts=ts_iso,
            db_path=db_path,
        )
    except Exception:
        # 与时序同纪律：事件写不进去不该让 /api/machines 挂掉
        pass
    return status, msg, ts_iso


def _resolve_ipv4(host):
    """public_host → 第一个 IPv4。只取 v4：console 容器网络没有 IPv6，NAS 侧
    mihomo 豁免规则也只写了 `ip saddr`。解析失败抛 OSError/UnicodeError。"""
    infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    return infos[0][4][0]


def _public_tcp(host, port):
    """一次家宽公网 TCP 探测 → (status, msg, latency_ms)，status ∈ up|down|unknown。

    两道防伪闸门，拦下的一律 unknown（不是 up，也不是 down）：
    1. 解析落在 fake-ip 段 —— 域名没进 mihomo fake-ip-filter；
    2. 握手快于 ``PUBLIC_PROBE_MIN_PLAUSIBLE_MS`` —— NAS 侧豁免失效，被 TUN 代答。
    两者都是「本机代理没放行」，说明不了节点通不通。msg 里写域名不写 IP。
    """
    try:
        ip = _resolve_ipv4(host)
    except (OSError, UnicodeError) as exc:
        return "unknown", f"tcp {host}:{port} 解析失败：{exc}", None
    if ipaddress.ip_address(ip) in _FAKE_IP_NET:
        return "unknown", f"tcp {host}:{port} 解析到 fake-ip，本机代理未放行，结果不可信", None

    detail = ""
    for _ in range(PUBLIC_PROBE_ATTEMPTS):
        started = time.perf_counter()
        ok, detail = check_tcp(ip, port, timeout=PUBLIC_PROBE_TIMEOUT)
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        if not ok:
            continue
        if latency_ms < PUBLIC_PROBE_MIN_PLAUSIBLE_MS:
            return (
                "unknown",
                f"tcp {host}:{port} 握手 {latency_ms}ms，快于公网下限，疑似本机代理代答",
                None,
            )
        return "up", f"tcp {host}:{port} ok {latency_ms}ms", latency_ms
    return "down", f"tcp {host}:{port} {detail}", None


def check_machine_public_probe(machine_id, force=False, db_path=None):
    """家宽公网可达探测：从本实例直连 ``public_host:public_probe.port``。

    和 ``check_machine_probe`` 回答的是两个问题：那边走 Tailscale 看机器活没活，
    这边走家宽直连看公网端口通不通。代理节点被封时机器照样在线（Tailscale 经
    DERP 中继仍 up），只有这里会红——这正是 owner 要看的信号。

    只在 NAS 上有意义：NAS 的 mihomo TUN 默认代答所有公网握手，依赖两处 NAS 侧
    配置（fake-ip-filter 放行 ``+.sunxc.eu.cc``、nft 放行 console 网段到探测端口），
    失效时由 ``_public_tcp`` 的闸门降级成 unknown，不产生假 up。

    TTL 走 ``public_probe_interval``；非 authority 只回缓存（Q1）。
    返回 (status, msg, ts_iso)；unknown 且无缓存时 ts 为 None。
    """
    machine = mc.get_machine(machine_id)
    if not machine or not machine.get("public_probe") or not machine.get("public_host"):
        return "unknown", "", None

    key = machine["id"]
    ttl = public_probe_interval()
    now = time.time()
    cached = _public_cache.get(key)
    if not force and cached and now - cached["ts"] < ttl:
        return cached["status"], cached["msg"], cached["ts_iso"]
    if not collect_guard.collection_allowed():
        if cached:
            return cached["status"], cached["msg"], cached["ts_iso"]
        return "unknown", "", None

    status, msg, latency_ms = _public_tcp(
        machine["public_host"], machine["public_probe"]["port"]
    )
    ts_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _public_cache[key] = {"ts": now, "status": status, "msg": msg, "ts_iso": ts_iso}
    if status not in ("up", "down"):
        # 闸门拦下的样本不落时序也不推进事件：它说明的是本机配置，不是节点
        return status, msg, ts_iso
    try:
        node_probe.record(public_history_key(key), status, latency_ms, ts_iso, db_path=db_path)
    except Exception:
        # 与机器探活同纪律：时序写不进去不影响探测主线
        pass
    try:
        import monitor_events

        monitor_events.observe_public(
            key,
            status,
            label=machine.get("label") or "",
            detail=msg,
            ts=ts_iso,
            db_path=db_path,
        )
    except Exception:
        pass
    return status, msg, ts_iso


def _reclaim_public_event(machine_id, db_path=None):
    """台账撤掉 public_probe 的机器不会再有观测来关闭旧事件：补一次 up 观测收掉它。

    与 ``_reclaim_disabled_event`` 同一个坑（服务侧实测悬挂 16 天）。没有活动事件时
    ``observe`` 不改任何行；只在 authority 的后台一轮里调用。
    """
    try:
        import monitor_events

        monitor_events.observe_public(
            machine_id, "up", detail="家宽公网探测已从台账撤掉", db_path=db_path
        )
    except Exception:
        pass


def peek_machine_public(machine_id):
    """只读家宽公网探测缓存，绝不发起探测。无缓存回 ("unknown", "", None)。

    `/api/machines` 用它：公网不通时一次探测最多要等 ``ATTEMPTS × TIMEOUT`` 秒，
    不能放在请求路径上；采样全靠后台调度推进。
    """
    cached = _public_cache.get(str(machine_id or "").strip().lower())
    if not cached:
        return "unknown", "", None
    return cached["status"], cached["msg"], cached["ts_iso"]


def probe_all_public(db_path=None):
    """对配了 public_probe 的机器强制探一轮，返回拿到 up/down 的台数。

    后台调度（app.py _bg_public_probes）每 ``public_probe_interval`` 调一次；
    非 authority 直接返回 0（Q1）。单台异常不拖垮整轮。没配 public_probe 的机器
    不探，只顺手回收它可能残留的活动事件（配置撤掉之前开的那条）。
    """
    if not collect_guard.collection_allowed():
        return 0
    probed = 0
    for machine_id, machine in list((mc.load_machines() or {}).items()):
        if not machine.get("public_probe"):
            _reclaim_public_event(machine_id, db_path=db_path)
            continue
        try:
            status, _msg, _ts = check_machine_public_probe(
                machine_id, force=True, db_path=db_path
            )
        except Exception:
            continue
        if status in ("up", "down"):
            probed += 1
    return probed


def invalidate(name=None):
    if name:
        _cache.pop(name, None)
    else:
        _cache.clear()
        _machine_cache.clear()
        _public_cache.clear()


def probe_all_machines(db_path=None):
    """对台账里所有配了 probe 的机器强制探一轮，返回真探到的台数。

    半点调度（app.py _bg_machine_probes）每到一个时间节点调一次：没人开页面
    色带也按节点推进。单台失败不拖垮整轮；force=True 绕过 TTL，保证每个节点
    都有新样本落进 probe_history。

    非 authority 实例直接返回 0（Q1：调度只归 NAS）。
    """
    if not collect_guard.collection_allowed():
        return 0
    probed = 0
    for machine_id in list(mc.load_machines() or {}):
        try:
            status, _msg, _ts = check_machine_probe(
                machine_id, force=True, db_path=db_path
            )
        except Exception:
            continue
        if status != "unknown":
            probed += 1
    return probed


def invalidate_machine(machine_id=None):
    """只清机器探活缓存（node 动作后让下一次 /api/machines 重新探）。"""
    if machine_id:
        _machine_cache.pop(str(machine_id).strip().lower(), None)
    else:
        _machine_cache.clear()
