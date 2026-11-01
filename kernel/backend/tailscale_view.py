# -*- coding: utf-8 -*-
"""C2 Tailscale tailnet 视图（cron 导出文件版，Track W2 + 集成复核改造）。

数据源（owner 2026-09-06 拍板：cron 导出，弃用 socket 直连）：
NAS 宿主 crontab 每 5 分钟 ``tailscale status --json`` 原子落盘到
``tailnet/status.json``，console 只读挂载 ``./tailnet:/app/tailnet:ro``，
本模块读 ``CONSOLE_TAILNET_STATUS_FILE``（默认 ``/app/tailnet/status.json``）
解析。与 LocalAPI 等价的字段结构，无云 token、不出网、不受 mihomo fake-ip
影响；**不挂载 tailscaled socket**（LocalAPI 并非只读接口，挂载即把控制面
交给无鉴权容器——集成评审 2026-09-06）。socket 直连（``_read_status_json``）
仅保留为未配置文件源时的回退路径。

纪律（commit e329e27「tailscale IP 刻意不经 API 外泄」）：peer 的
TailscaleIPs 只在服务端用于「tailscale IP → machines.yaml 机器 id」映射，
绝不下发前端；API 只输出白名单字段
``{machine_id, hostname, online, key_expiry_days, last_seen, client_version,
os, update_available, authorized}``（D4 补三字段，来源 gethomepage.dev
Tailscale widget 的字段设计，数据仍走 cron 导出文件）。

三个布尔/文本字段口径：
- ``os``：peer 的 OS 文本（linux/windows/…），缺失 → None；
- ``update_available``：只有 peer 明确 ``True`` 才报「有更新」，缺失/非布尔
  一律 False——不把「没说」冒充「有/没有更新」之外的第三种断言；
- ``authorized``：缺失/非布尔缺省 **True**（tailscale 设备缺省已授权，
  LocalAPI status 不带该字段时按已授权处理，避免把缺字段误报成待授权）；
  只有明确 ``False`` 才报未授权。

输出以台账为骨架（registry 驱动）：台账里的每台机器恒占一行，在 tailnet
status 里找不到对应 peer 时 ``online=None`` 显式标「未知」（新增三字段同
None）——机器从 tailnet 消失不该被静默丢弃。反向方向（tailnet 里有、台账
没有的设备，如 mac/win/手机）不进响应，响应只覆盖 console 纳管机器。

降级：文件缺失/坏 JSON/socket 缺失/超时/非 200 一律不抛 500——返回并**缓存**
``{"available": False, ...}`` 显式降级态（失败同样占 TTL，避免每个请求都去
戳坏掉的数据源）。TTL 走 ``CONSOLE_TAILNET_INTERVAL``（默认 600s、下限 60s，
与 probe_cache 同模式）。

测试 mock ``_read_status_json`` / 临时文件，不碰真实数据源。
"""
import datetime
import http.client
import json
import math
import os
import socket
import threading
import time

import machine_config as mc
from snapshot_freshness import source_stamp

_cache = {"ts": 0.0, "data": None, "source": None}
_refresh_lock = threading.Lock()
SOURCE_STALE_ERROR = "Tailnet 数据已过期（超过 15 分钟未更新）"
SOURCE_FUTURE_ERROR = "Tailnet 数据采集时间异常（超过 60 秒未来容差）"

# Go 零值时间（0001-01-01T00:00:00Z）= key 不过期
_ZERO_TIME_PREFIX = "0001-01-01"


def tailnet_interval():
    """快照 TTL 秒数：env CONSOLE_TAILNET_INTERVAL，默认 600，下限 60。"""
    try:
        return max(60, int(os.environ.get("CONSOLE_TAILNET_INTERVAL", "600")))
    except ValueError:
        return 600


def tailscale_socket_path():
    """tailscaled LocalAPI unix socket 路径（容器内只读挂载点）。"""
    return os.environ.get("CONSOLE_TAILSCALE_SOCKET", "/var/run/tailscaled.socket")


def tailnet_timeout():
    """LocalAPI 单次调用超时秒数：本地 socket 理应毫秒级，5s 已是宽限。"""
    try:
        return max(1.0, float(os.environ.get("CONSOLE_TAILNET_TIMEOUT", "5")))
    except ValueError:
        return 5.0


class _UnixSocketHTTPConnection(http.client.HTTPConnection):
    """HTTP over unix socket：LocalAPI 不走 TCP，自然不受 mihomo fake-ip 影响。"""

    def __init__(self, socket_path, timeout):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def status_file_path():
    """cron 导出的 status.json 容器内路径；未配置返回 None（走 socket 回退）。"""
    return os.environ.get("CONSOLE_TAILNET_STATUS_FILE") or None


def _read_status_from_file(path):
    """cron JSON 与同一打开描述符的 mtime，避免原子替换混用新旧源。

    错误一律转固定前缀 RuntimeError，让 _safe_error 能给出可排障文案。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            parsed = json.load(fh)
            stat = os.fstat(fh.fileno())
    except FileNotFoundError:
        raise RuntimeError("tailnet status 文件不存在（cron 未就绪或挂载缺失）")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RuntimeError("tailnet status 文件解析失败（非完整 JSON）")
    except OSError:
        raise RuntimeError("tailnet status 文件不可读")
    if not isinstance(parsed, dict):
        raise RuntimeError("tailnet status 文件内容非 JSON 对象")
    return parsed, stat


def _load_status():
    """数据源优先级：cron 导出文件（owner 拍板终态）→ LocalAPI socket（回退）。

    配置了文件源就走文件（失败直接抛，降级文案指向 cron），不静默转
    socket——语义单一，排障时能一眼看出是哪一路数据源出的问题。
    """
    file_path = status_file_path()
    if file_path:
        return _read_status_from_file(file_path)
    return _read_status_json(tailscale_socket_path(), tailnet_timeout()), None


def _read_status_json(socket_path, timeout):
    """调 LocalAPI status 并解析 JSON；所有异常由调用方降级为 available=False。"""
    conn = _UnixSocketHTTPConnection(socket_path, timeout)
    try:
        conn.request("GET", "/localapi/v0/status")
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        parsed = json.loads(body.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise RuntimeError("LocalAPI 返回非 JSON 对象")
        return parsed
    finally:
        conn.close()


def _safe_error(exc):
    """异常 → 固定中文摘要，不带 socket 路径/系统错误原文进 API 响应。"""
    if isinstance(exc, FileNotFoundError):
        return "tailscaled socket 不存在（宿主演化或挂载缺失）"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "LocalAPI 请求超时"
    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError)):
        return "tailscaled 拒绝连接"
    if isinstance(exc, RuntimeError):
        text = str(exc).strip()
        if text in (SOURCE_STALE_ERROR, SOURCE_FUTURE_ERROR):
            return text
        # 自己抛的 "HTTP <status>" / 固定文案可以放行
        if text.startswith("HTTP ") or text == "LocalAPI 返回非 JSON 对象" or text.startswith("tailnet status 文件"):
            return f"LocalAPI 调用失败：{text}" if text.startswith("HTTP ") else text
    return "LocalAPI 调用失败"


def _parse_rfc3339(raw):
    """RFC3339 → aware datetime；缺失/坏值返回 None。兼容 3.9（Z 手动转正）。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def key_expiry_days(raw, now=None):
    """KeyExpiry → 剩余整天数（floor）。

    - 不过期（Go 零值 0001-01-01）/缺失/坏值 → None
    - 已过期 → 负数（过期 12 小时 floor 为 -1）
    - 恰好 7 天整 → 7（落进 attention 的 ≤7 天告警窗）
    """
    text = str(raw or "").strip()
    if not text or text.startswith(_ZERO_TIME_PREFIX):
        return None
    expiry = _parse_rfc3339(text)
    if expiry is None:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return math.floor((expiry - now).total_seconds() / 86400)


def _clean_text(value):
    text = str(value or "").strip()
    return text or None


def _clean_ts(raw):
    """LastSeen 原样透传（已是 UTC RFC3339），坏值丢弃为 None。"""
    parsed = _parse_rfc3339(raw)
    if parsed is None:
        return None
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _strict_bool(value, default):
    """只有 Python bool 才可信；字符串/数字/缺失一律走缺省（D4 口径，见模块 docstring）。"""
    if isinstance(value, bool):
        return value
    return default


def _peer_index(status):
    """Tailscale IP → peer dict（Self + 全部 Peer）。仅限服务端使用，不出本模块。"""
    sources = []
    self_node = status.get("Self")
    if isinstance(self_node, dict):
        sources.append(self_node)
    peers = status.get("Peer")
    if isinstance(peers, dict):
        sources.extend(v for v in peers.values() if isinstance(v, dict))

    index = {}
    for peer in sources:
        ips = peer.get("TailscaleIPs")
        if not isinstance(ips, list):
            continue
        for ip in ips:
            ip = str(ip).strip()
            if ip and ip not in index:
                index[ip] = peer
    return index


def _machine_rows(status, now):
    """台账每机器一行（保持 yaml 顺序）；peer 缺失 → 全字段 None 显式未知。"""
    peers = _peer_index(status)
    rows = []
    for machine_id in mc.load_machines():
        ip = mc.tailscale_ip(machine_id)
        peer = peers.get(ip) if ip else None
        if peer is None:
            rows.append(
                {
                    "machine_id": machine_id,
                    "hostname": None,
                    "online": None,
                    "key_expiry_days": None,
                    "last_seen": None,
                    "client_version": None,
                    "os": None,
                    "update_available": None,
                    "authorized": None,
                }
            )
            continue
        rows.append(
            {
                "machine_id": machine_id,
                "hostname": _clean_text(peer.get("HostName")),
                # Online 缺省按 False 处理：tailnet 说不上在线就是不在线
                "online": bool(peer.get("Online")),
                "key_expiry_days": key_expiry_days(peer.get("KeyExpiry"), now),
                "last_seen": _clean_ts(peer.get("LastSeen")),
                "client_version": _clean_text(peer.get("ClientVersion")),
                # D4 三字段；口径见模块 docstring（update_available 缺省 False、
                # authorized 缺省 True，都只有明确布尔才采信）
                "os": _clean_text(peer.get("OS")),
                "update_available": _strict_bool(peer.get("UpdateAvailable"), False),
                "authorized": _strict_bool(peer.get("Authorized"), True),
            }
        )
    return rows


def _expire_cached(cached, now):
    """仅按已有截止投影降级；不修改旧引用、不读盘/配置/网络。"""
    payload = cached["data"]
    stamp = cached["source"]
    if (payload is not None and payload["available"] and payload["source_sampled_at"] is not None
            and stamp is not None and stamp.expired(now)):
        return {**payload, "available": False, "error": SOURCE_STALE_ERROR, "machines": []}
    return payload


def _sample_tailnet(cached, now_mono):
    now = datetime.datetime.fromtimestamp(time.time(), datetime.timezone.utc)
    sampled_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    source_sampled_at = None
    source = cached["source"]
    try:
        status, stat = _load_status()
        observed = None
        if stat is not None:
            observed = source_stamp(stat, time.time(), time.monotonic(), source)
            source_sampled_at = observed.sampled_at
            if observed.deadline is None:
                raise RuntimeError(SOURCE_FUTURE_ERROR)
        machines = _machine_rows(status, now)
        # A registry/row-conversion failure must retain the previous identity;
        # successful conversion commits even a stale source's past deadline.
        if observed is not None:
            source = observed
            if source.expired(time.monotonic()):
                raise RuntimeError(SOURCE_STALE_ERROR)
        payload = {
            "available": True,
            "sampled_at": sampled_at,
            "source_sampled_at": source_sampled_at,
            "error": None,
            "machines": machines,
        }
    except Exception as exc:  # socket 缺失/超时/坏 JSON 全部显式降级
        payload = {
            "available": False,
            "sampled_at": sampled_at,
            "source_sampled_at": source_sampled_at,
            "error": _safe_error(exc),
            "machines": [],
        }
    return {"ts": now_mono, "data": payload, "source": source}


def get_tailnet_snapshot(force=False):
    """600s 单调 TTL；失败同样缓存，文件源期限不因读取或 invalidate 续期。"""
    global _cache
    ttl = tailnet_interval()
    now_mono = time.monotonic()
    cached = _cache
    if not force and cached["data"] is not None and now_mono - cached["ts"] < ttl:
        return _expire_cached(cached, now_mono)
    with _refresh_lock:
        cached = _cache
        now_mono = time.monotonic()
        if not force and cached["data"] is not None and now_mono - cached["ts"] < ttl:
            return _expire_cached(cached, now_mono)
        _cache = _sample_tailnet(cached, now_mono)
        return _expire_cached(_cache, time.monotonic())


def peek_tailnet_snapshot():
    """只读当前缓存快照；未采样返回 None，绝不触网（attention 专用）。"""
    return _expire_cached(_cache, time.monotonic())


def invalidate():
    """清空 payload/TTL，但保留源版本期限，防止 invalidate 为旧源续命。"""
    global _cache
    with _refresh_lock:
        _cache = {**_cache, "ts": 0.0, "data": None}
