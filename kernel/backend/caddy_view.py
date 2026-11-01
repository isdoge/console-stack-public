# -*- coding: utf-8 -*-
"""C11 入口流量小卡的数据层（Caddy admin API 经 NAS crontab 导出，Track C11）。

数据链：aly 宿主 Caddy admin API（`localhost:2019`，仅回环、零外部暴露——2026-09-06
前置核验）→ NAS 宿主 crontab 每 5 分钟 `ssh aly 'curl -s
localhost:2019/reverse_proxy/upstreams'` 原子落盘 `caddy/status.json` → console 只读
挂载 `./caddy:/app/caddy:ro` 后由本模块解析。与 C2 的 tailscale/status.json 完全同构
（owner 裁决：cron 导出文件制，不开放任何控制面端口）。

纪律（e329e27「tailscale IP 刻意不经 API 外泄」）：upstream 的 `address` 全部是
tailnet IP，只在服务端用于反查服务名（assets.yaml 的 access.tailscale 地址匹配），
**绝不出后端**；`/api/caddy/entry` 输出仅
`{id, name, num_requests, fails}`，name 匹配不到时降级 `upstream-N`。

字段语义（2026-09-07 复核结论，源码 / 官方文档 / aly 实测三方一致——**两个字段都是
瞬时量，不是累计量**，读数恒 0 属正常态而非采集故障）：

* ``num_requests`` = **当前活跃（in-flight）请求数**。Caddy 源码
  ``modules/caddyhttp/reverseproxy/hosts.go``：``// NumRequests returns the number of
  active requests to the upstream.``；计数由 ``countRequest(delta)`` 在请求进出时
  ``atomic.AddInt64`` ±1，请求结束即减回。官方 admin API 文档同口径（"the amount of
  active requests currently being handled by the upstream"）。
  cron 每 5 分钟采一次样，撞上请求在途的概率极低 → **常态就是 0**。
  2026-09-07 aly 受控实测（8 个并发回环请求 + 连续轮询 admin）：
  ``0 → 8 7 7 7 6 6 4 2 1 → 0``，gauge 语义实证。
* ``fails`` = **被动健康检查「记住」的失败数**，且到 ``fail_duration`` 会被自动忘掉
  （``healthchecks.go`` 的 ``countFailure`` 起 goroutine ``countFail(-1)``）。
  ``h.HealthChecks == nil || h.HealthChecks.Passive == nil`` 或 ``fail_duration == 0``
  时该函数**直接 return（no-op）**——即没配被动健康检查就永远不计数。
  2026-09-07 实测 aly live config：4 个 reverse_proxy handler 全部没有 ``health_checks``
  键 → **fails 恒 0，与真实失败率无关**。

因此 upstream 那半边**只做透传**，不做任何累计、速率或趋势推断；前端文案也必须写成
瞬时口径（见 ``entry-traffic-card.tsx`` 的「活跃 / 失败」与脚注）。

累计量（2026-09-07 owner 授权补齐，第二条数据链）
-------------------------------------------------

真实的累计请求量来自 **Prometheus 指标**，与上面的 upstreams 是**两条互不依赖的链**：

    aly Caddyfile 全局 ``metrics { per_host }``  →  admin API localhost:2019/metrics
    →  NAS crontab 每 5 分钟 grep 三类指标原子落盘 ``caddy/metrics.prom``
    →  本模块 ``_host_rows()`` 解析

三个必须记住的口径坑：

1. **Caddy 2.11 的 ``metrics`` 是全局选项**（落 ``apps.http.metrics``），**不是**
   ``servers.<srv>.metrics``。旧写法 ``servers { metrics }`` 在 2.11 里**被接受但不产生
   任何 JSON**——`caddy validate` 照样说 Valid configuration，静默无效。本模块 docstring
   此前写的正是那个旧写法，2026-09-07 实测推翻。
2. **指标按 ``host``（域名）打标签，不是按 upstream 地址**。所以累计量这半边的行是
   *域名维度*，与 upstreams 那半边的 *上游维度* 对不上一一映射（本例 4 upstream / 3 域名，
   ``axonhub.example.test`` 按 path 分流到两个上游）。两者刻意分开展示，不要强行合并。
3. **计数器在每次 Caddy reload/重启时归零**。所以「累计」永远是「自上次重载起累计」，
   ``counting_since`` 就取 ``caddy_config_last_reload_success_timestamp_seconds``——
   前端必须把这个起点显示出来，否则用户会把它当成开天辟地以来的总量。

零 IP 纪律在这半边同样成立：``host`` 标签理论上可能是裸 IP（有人直接按 IP 打 Host 头），
Caddy 的 catchall 一般会归到 ``_other``，但本模块**再兜一层**——凡是形如 IPv4 的 host
一律折叠成 ``_other``（见 ``_safe_host``），保证 ``/api/caddy/entry`` 全响应零内网 IP。

降级：文件缺失/坏 JSON/结构非法一律不抛 500——返回并**缓存**
`{"available": False, ...}` 显式降级态（失败同样占 TTL，避免每个请求反复读盘）。
TTL 走 `CONSOLE_CADDY_INTERVAL`（默认 600s、下限 60s，与 probe/tailnet 同模式）。

写边界：Caddy admin API 具备配置写能力（POST /load 可整体替换公网入口配置），
本模块**只允许 GET 消费**，且只落 `/reverse_proxy/upstreams` 数组、不落 `/config/`
全量配置、不透传 admin 端口进任何前端响应（2026-09-06 前置核验结论）。

测试 mock 文件读取（tmp_path 写真实 JSON），不碰真实数据源。
"""
import datetime
import json
import math
import os
import re
import threading
import time
from urllib.parse import urlsplit

import service_manager as sm
from snapshot_freshness import source_stamp

_cache = {"ts": 0.0, "data": None, "metrics_source": None, "upstreams_source": None}
_refresh_lock = threading.Lock()
UPSTREAMS_STALE_ERROR = "Caddy 上游数据已过期（超过 15 分钟未更新）"
UPSTREAMS_FUTURE_ERROR = "Caddy 上游数据采集时间异常（超过 60 秒未来容差）"
METRICS_STALE_ERROR = "Caddy 指标数据已过期（超过 15 分钟未更新）"
METRICS_FUTURE_ERROR = "Caddy 指标数据采集时间异常（超过 60 秒未来容差）"
METRICS_INVALID_ERROR = "Caddy 指标文件无有效支持样本"

# Prometheus 文本行：`name{k="v",...} value` 或 `name value`。
_PROM_LINE_RE = re.compile(
    r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?[ \t]+(?P<value>\S+)'
)
_PROM_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
# 兜底：host 标签若形如 IPv4 一律折叠成 _other，保住「零内网 IP 出后端」契约。
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

CATCHALL_HOST = "_other"


def caddy_interval():
    """快照 TTL 秒数：env CONSOLE_CADDY_INTERVAL，默认 600，下限 60。"""
    try:
        return max(60, int(os.environ.get("CONSOLE_CADDY_INTERVAL", "600")))
    except (TypeError, ValueError):
        return 600


def status_file_path():
    """cron 导出的 upstreams JSON 容器内路径（compose 挂 ./caddy:/app/caddy:ro）。"""
    return os.environ.get("CONSOLE_CADDY_STATUS_FILE", "/app/caddy/status.json")


def metrics_file_path():
    """cron 导出的 Prometheus 文本容器内路径（同一个只读挂载目录）。"""
    return os.environ.get("CONSOLE_CADDY_METRICS_FILE", "/app/caddy/metrics.prom")


def _assets_path():
    """assets.yaml 候选路径（存在才命中，按序）：
    1. 显式 CONSOLE_CADDY_ASSETS_PATH
    2. 容器 /app/assets.yaml（代码平铺在 /app，../assets.yaml 会算成不存在的 /assets.yaml——
       C11 首轮部署时映射全降级 upstream-N 的根因，2026-09-07 修）
    3. 开发机 console/backend/../assets.yaml
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.environ.get("CONSOLE_CADDY_ASSETS_PATH"),
        "/app/assets.yaml",
        os.path.normpath(os.path.join(here, "..", "assets.yaml")),
    )
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return os.path.normpath(os.path.join(here, "..", "assets.yaml"))


def _host_port(raw):
    """URL 或 address → host:port 小写串；解析失败返回 None。"""
    text = str(raw or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "//" + text
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    return f"{host}:{port or 80}"


def _service_index():
    """assets.yaml 的 access.tailscale 地址 → 服务名；坏条目容忍跳过。"""
    index = {}
    try:
        services = sm.load_assets(_assets_path())
    except Exception:
        return index
    for svc in services if isinstance(services, list) else []:
        if not isinstance(svc, dict):
            continue
        name = str(svc.get("name") or "").strip()
        access = svc.get("access")
        url = (access or {}).get("tailscale") if isinstance(access, dict) else None
        key = _host_port(url)
        if name and key and key not in index:
            index[key] = name
    return index


def _read_upstreams_from_file(path):
    """cron upstreams JSON 与同一打开描述符的 mtime；错误转固定文案。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            parsed = json.load(handle)
            stat = os.fstat(handle.fileno())
    except FileNotFoundError:
        raise RuntimeError("Caddy 导出文件不存在（cron 未就绪或挂载缺失）")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RuntimeError("Caddy 导出文件解析失败（非完整 JSON）")
    except OSError:
        raise RuntimeError("Caddy 导出文件不可读")
    if not isinstance(parsed, list) or not all(isinstance(u, dict) for u in parsed):
        raise RuntimeError("Caddy 导出文件结构非法（应为 upstream 数组）")
    return parsed, stat


def _safe_error(exc):
    """异常 → 固定中文摘要，不带路径/系统错误原文进 API 响应。"""
    if isinstance(exc, RuntimeError):
        text = str(exc).strip()
        if text in (UPSTREAMS_STALE_ERROR, UPSTREAMS_FUTURE_ERROR):
            return text
        if text.startswith("Caddy 导出文件"):
            return text
    if isinstance(exc, FileNotFoundError):
        return "Caddy 导出文件不存在（cron 未就绪或挂载缺失）"
    return "Caddy 导出数据不可用"


def _upstream_rows(upstreams):
    """address 反查服务名后丢弃，输出零 IP 的白名单行。

    ``num_requests`` / ``fails`` 原样透传（见模块 docstring「字段语义」）：
    两者都是瞬时量，**0 是合法读数不是缺失**，所以这里不做任何「0 → None」的转换，
    也不因为全 0 而把快照判成不可用。
    """
    index = _service_index()
    rows = []
    for i, upstream in enumerate(upstreams):
        key = _host_port(upstream.get("address"))
        rows.append(
            {
                "id": f"upstream-{i + 1}",
                "name": index.get(key) or f"upstream-{i + 1}",
                "num_requests": _int_or_none(upstream.get("num_requests")),
                "fails": _int_or_none(upstream.get("fails")),
            }
        )
    return rows


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_host(raw):
    """host 标签规范化。空 / 裸 IPv4 一律折叠成 ``_other``——零内网 IP 契约的兜底层。"""
    text = str(raw or "").strip().lower()
    if not text or _IPV4_RE.match(text):
        return CATCHALL_HOST
    return text


def _parse_prom_lines(text):
    """Prometheus 文本 → (name, labels dict, float value) 迭代器；坏行静默跳过。

    只处理本项目 cron 导出的那三类指标，不追求完整实现 exposition format
    （不支持 exemplar / 时间戳列，遇到也只是多解析一个字段后被忽略）。
    """
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PROM_LINE_RE.match(line)
        if not match:
            continue
        try:
            value = float(match.group("value"))
        except (TypeError, ValueError):
            continue
        # NaN / +Inf / -Inf 是 exposition format 的合法值，float() 收得下但后面
        # int() 会抛 ValueError/OverflowError，一路冒到 _metrics_section 把整个
        # 分区判成不可用——一行坏数据不该让整张卡失明，这里直接跳过该样本。
        if not math.isfinite(value):
            continue
        raw_labels = match.group("labels") or ""
        labels = {}
        offset = 0
        for label in _PROM_LABEL_RE.finditer(raw_labels):
            separator = raw_labels[offset:label.start()].strip()
            if separator != ("," if offset else "") or label.group(1) in labels:
                break
            labels[label.group(1)] = label.group(2)
            offset = label.end()
        else:
            tail = raw_labels[offset:].strip()
            if tail not in ("", ",") or (tail == "," and not labels):
                continue
            yield match.group("name"), labels, value
        # A malformed/duplicate label must not silently become the catchall host.


def _blank_host_row(host):
    return {
        "host": host,
        "requests_total": 0,
        "ok_2xx": 0,
        "redirect_3xx": 0,
        "client_4xx": 0,
        "server_5xx": 0,
    }


_CODE_FIELD = {"2": "ok_2xx", "3": "redirect_3xx", "4": "client_4xx", "5": "server_5xx"}


def _host_rows(text):
    """Prometheus 文本 → (按域名聚合的行, counting_since ISO 串或 None)。

    ``requests_total`` 对同一 host 的多个 handler **求和**（本例每域名一个 subroute
    handler，但配置一变就可能多出来，求和才是稳的）。状态码分档取自
    ``caddy_http_response_duration_seconds_count`` 的 ``code`` 标签首位数字。
    行按累计量降序、同量按域名升序，保证输出稳定可断言。
    """
    if not isinstance(text, str):
        raise ValueError(METRICS_INVALID_ERROR)
    rows = {}
    counting_since = None
    supported = False
    for name, labels, value in _parse_prom_lines(text):
        if value < 0:
            continue
        if name == "caddy_config_last_reload_success_timestamp_seconds":
            try:
                counting_since = datetime.datetime.fromtimestamp(
                    value, datetime.timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            except (OverflowError, OSError, ValueError):
                continue
            supported = True
            continue
        # Request/response counters count events: negatives and fractional samples
        # are invalid, rather than values to truncate into a plausible zero.
        if not value.is_integer():
            continue
        if name == "caddy_http_requests_total":
            supported = True
            host = _safe_host(labels.get("host"))
            rows.setdefault(host, _blank_host_row(host))["requests_total"] += int(value)
        elif name == "caddy_http_response_duration_seconds_count":
            host = _safe_host(labels.get("host"))
            code = labels.get("code") or ""
            field = _CODE_FIELD.get(code[:1]) if re.fullmatch(r"[2-5][0-9]{2}", code) else None
            if field:
                supported = True
                rows.setdefault(host, _blank_host_row(host))[field] += int(value)

    if not supported and any(line.strip() and not line.lstrip().startswith("#")
                             for line in text.splitlines()):
        raise ValueError(METRICS_INVALID_ERROR)

    ordered = sorted(rows.values(), key=lambda r: (-r["requests_total"], r["host"]))
    return ordered, counting_since


def _read_metrics_from_file(path):
    """返回同一描述符的文本和 mtime，避免原子替换时内容与时间不同源。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
            return text, os.fstat(handle.fileno())
    except FileNotFoundError:
        raise RuntimeError("Caddy 指标文件不存在（cron 未就绪或挂载缺失）")
    except UnicodeDecodeError:
        raise RuntimeError("Caddy 指标文件解析失败（非 UTF-8 文本）")
    except OSError:
        raise RuntimeError("Caddy 指标文件不可读")


def _metrics_error(exc):
    if isinstance(exc, ValueError) and str(exc) == METRICS_INVALID_ERROR:
        return METRICS_INVALID_ERROR
    if isinstance(exc, RuntimeError):
        text = str(exc).strip()
        if text in (METRICS_STALE_ERROR, METRICS_FUTURE_ERROR):
            return text
        if text.startswith("Caddy 指标文件"):
            return text
    return "Caddy 指标数据不可用"


def _metrics_section(source):
    """累计量分区。**与 upstreams 各自降级**：指标文件缺失不影响瞬时量那半边。"""
    metrics_sampled_at = None
    try:
        text, stat = _read_metrics_from_file(metrics_file_path())
        observed = source_stamp(stat, time.time(), time.monotonic(), source)
        metrics_sampled_at = observed.sampled_at
        if observed.deadline is None:
            raise RuntimeError(METRICS_FUTURE_ERROR)
        hosts, counting_since = _host_rows(text)
        # Rejected content must not evict the last validated source identity.
        # Valid-but-stale content still records its past deadline before failing.
        source = observed
        if source.expired(time.monotonic()):
            raise RuntimeError(METRICS_STALE_ERROR)
    except Exception as exc:
        return {
            "metrics_available": False,
            "metrics_error": _metrics_error(exc),
            "metrics_sampled_at": metrics_sampled_at,
            "counting_since": None,
            "hosts": [],
        }, source
    return {
        "metrics_available": True,
        "metrics_error": None,
        "metrics_sampled_at": metrics_sampled_at,
        "counting_since": counting_since,
        "hosts": hosts,
    }, source


def _upstreams_section(source):
    upstreams_sampled_at = None
    try:
        upstreams, stat = _read_upstreams_from_file(status_file_path())
        observed = source_stamp(stat, time.time(), time.monotonic(), source)
        upstreams_sampled_at = observed.sampled_at
        if observed.deadline is None:
            raise RuntimeError(UPSTREAMS_FUTURE_ERROR)
        rows = _upstream_rows(upstreams)
        source = observed
        if source.expired(time.monotonic()):
            raise RuntimeError(UPSTREAMS_STALE_ERROR)
        return {
            "available": True, "error": None, "upstreams_sampled_at": upstreams_sampled_at,
            "upstreams": rows,
        }, source
    except Exception as exc:
        return {
            "available": False, "error": _safe_error(exc),
            "upstreams_sampled_at": upstreams_sampled_at, "upstreams": [],
        }, source


def _expire_cached(cached, now):
    """三源独立；本分区只用已发布条目的两份截止，不触发 I/O。"""
    payload = cached["data"]
    if payload is None:
        return None
    stamp = cached["upstreams_source"]
    if payload["available"] and stamp is not None and stamp.expired(now):
        payload = {**payload, "available": False, "error": UPSTREAMS_STALE_ERROR, "upstreams": []}
    stamp = cached["metrics_source"]
    if payload["metrics_available"] and stamp is not None and stamp.expired(now):
        payload = {
            **payload, "metrics_available": False, "metrics_error": METRICS_STALE_ERROR,
            "counting_since": None, "hosts": [],
        }
    return payload


def get_caddy_snapshot(force=False):
    """600s 单调 TTL + 独立源截止；同源刷新串行，完整 entry 一次发布。"""
    global _cache
    ttl = caddy_interval()
    now_mono = time.monotonic()
    cached = _cache  # 同一条目读，避免并发刷新混用 payload 与 deadline。
    if not force and cached["data"] is not None and now_mono - cached["ts"] < ttl:
        return _expire_cached(cached, now_mono)

    with _refresh_lock:
        cached = _cache
        now_mono = time.monotonic()
        if not force and cached["data"] is not None and now_mono - cached["ts"] < ttl:
            return _expire_cached(cached, now_mono)
        sampled_at = datetime.datetime.fromtimestamp(time.time(), datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        upstreams, upstreams_source = _upstreams_section(cached["upstreams_source"])
        metrics, metrics_source = _metrics_section(cached["metrics_source"])
        # 两个分区各自失败/保留源期限，不把一边的状态或采集时间套到另一边。
        payload = {"sampled_at": sampled_at, **upstreams, **metrics}
        _cache = {
            "ts": now_mono, "data": payload, "metrics_source": metrics_source,
            "upstreams_source": upstreams_source,
        }
        return _expire_cached(_cache, time.monotonic())


def peek_caddy_snapshot():
    """只读当前缓存快照；未采样返回 None，绝不读盘。"""
    return _expire_cached(_cache, time.monotonic())


def invalidate():
    """清空 payload/TTL；两个源各自的已知期限必须跨失效保留。"""
    global _cache
    with _refresh_lock:
        _cache = {**_cache, "ts": 0.0, "data": None}
