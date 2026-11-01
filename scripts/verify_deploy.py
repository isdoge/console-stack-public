#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""部署验收自动化：对运行中的 console-stack 逐项核验固定清单。

纯 stdlib（urllib/json/re/argparse），零第三方依赖——NAS 宿主（python3.11）与
mac 本地都能裸跑，不需要 venv。`deploy.sh` 不会自动调用本脚本，它是部署后的
人工/CI 可选步骤。用法见 .agents/operations/deploy-acceptance.md。

退出码：全部 PASS（或 SKIP）为 0，任一 FAIL 为 1。
"""
import argparse
import datetime
import ipaddress
import json
import math
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:8081"
DEFAULT_TIMEOUT = 20
RETRY_INTERVAL = 10
# Kept explicit because this verifier must run without importing the backend.
SOURCE_MAX_AGE_SECONDS = 900
SOURCE_FUTURE_TOLERANCE_SECONDS = 60
SOURCE_CONTRACTS = {
    "source_sampled_at": ("Tailnet", "available", "error", "machines"),
    "upstreams_sampled_at": ("Caddy 上游", "available", "error", "upstreams"),
    "metrics_sampled_at": ("Caddy 指标", "metrics_available", "metrics_error", "hosts"),
}

# Tailscale 内网 IP：任何前端可见响应里都不该出现（C2/C11 的零 IP 出后端约定）
TAILSCALE_IP_RE = re.compile(r"100\.\d+\.\d+\.\d+")

# 敏感文本正则：命中即判 FAIL。按需扩充，新增项写 (人读名称, 已编译正则)。
SENSITIVE_PATTERNS = (
    ("Tailscale 内网 IP", TAILSCALE_IP_RE),
    ("GitHub token", re.compile(r"\bgho_")),
    ("API key", re.compile(r"\bsk-")),  # \b 避免误伤 risk- 之类的普通词
    ("Authorization 凭据", re.compile(r"Bearer ")),
)

# 部署后 /api/ops-status 必须齐全的七键（D0 起六键，G/F17 起新增 timed_backup，
# B6-3 起新增 monitoring_business_source——R5 防静默回落：.env 键被模板覆盖/误删时
# 这里会露出 beszel，部署验收直接变红）
OPS_STATUS_KEYS = (
    "running_commit",
    "versions_last_checked",
    "feed_last_fetched",
    "last_backup_tag",
    "last_deploy",
    "last_probe_run",
    "timed_backup",
    "monitoring_business_source",
)
# monitoring_business_source 只许这两个值；其它值 = flag 拼写错误，比缺键更危险
MONITORING_BUSINESS_SOURCES = ("beszel", "console_pg")

# 必须可直达的核心页面（SPA 路由，返回 HTML 200）
CORE_PAGES = ("/", "/devices", "/feed", "/ledger", "/renewals", "/audit", "/settings")

# 列表型 API 契约（2026-09-06 对线上实测定）：(路径, 每项必含键, 是否允许空列表)。
# 只断言必含键的子集而非「恰含」——后端新增字段属兼容演进，不该让验收变红。
LIST_API_CONTRACTS = (
    # services 只取写入与读取路径保证的必含键。container/port/priority/access 只在
    # 部分条目存在；tags 为空时 _build_entry 不落盘，前端也按 [] 读取，均属可选。
    ("/api/services", ("name", "machine", "type", "health", "running", "actions",
                       "version", "version_info"), False),
    # panels 必须非空：面板自 2026-09-12 起走私有模式（CONSOLE_PANELS_HOST_PATH 指向
    # NAS 宿主 600 权限文件，Git 里的 console/panels.yaml 刻意保持 []）。所以线上返回
    # 空列表不代表「owner 没配面板」，只代表私有挂载没生效——正是本项要抓的故障。
    ("/api/panels", ("id", "name", "url", "machine", "enabled", "sort_order"), False),
    ("/api/renewals", ("id", "name", "kind", "cost", "currency", "cycle", "expires_at",
                       "auto_renew", "source"), False),
    ("/api/attention", ("severity", "kind", "title", "detail", "link"), True),
    ("/api/machines", ("id", "label", "role", "capabilities", "probe_status", "probe_ts"), False),
)

# 今日关注里不该出现的 kind：探针自监控超时 warn，部署后即出现说明探针线程没跑起来
FORBIDDEN_ATTENTION_KINDS = ("probe-monitor",)
# Diagnostics use known categories, not arbitrary titles/kinds from a response.
ATTENTION_KINDS = frozenset({
    "machine", "probe", "version", "feed", "renewal", "traffic", "traffic-forecast",
    "probe-monitor", "tailnet", "beszel", "systemd", "smart", "public",
})

# 迁移基线：D1' 起 /api/config/effective 的 schema_version 是 DB 已应用版本，
# v11 = create_personal_ledger（console/backend/db.py MIGRATIONS 末项）。
# 本脚本刻意零第三方依赖、不 import 后端包（见文件头设计说明），所以这里保留显式
# 常量而非直接从 db.MIGRATIONS 派生；test_verify_deploy.py 有一条测试把它与
# db.MIGRATIONS 的 max 绑定，任一侧漂移（改了迁移忘改这里，或反过来）测试就会红。
MIN_SCHEMA_VERSION = 11

# /api/tailnet 的 machines 每项恰含的字段（W2）。machine_id 是 2026-09-06 实测补入的
# 第 9 个字段（短 ID，非 IP）；「恰含」是安全约束的一部分——多出字段即可能是新的泄漏面。
TAILNET_MACHINE_FIELDS = frozenset({
    "hostname", "online", "key_expiry_days", "last_seen",
    "client_version", "os", "update_available", "authorized", "machine_id",
})

# /api/caddy/entry 的 upstreams 每项恰含的字段（C11，address 已在后端反查成服务名）
CADDY_UPSTREAM_FIELDS = frozenset({"id", "name", "num_requests", "fails"})
# C11 累计量（2026-09-07）：域名维度，与 upstreams 那半边不是同一个形状。
CADDY_HOST_FIELDS = frozenset(
    {"host", "requests_total", "ok_2xx", "redirect_3xx", "client_4xx", "server_5xx"}
)

# W3 PWA：iOS 主屏安装依赖这个 MIME，退化成 text/html 或 application/json 都装不上
MANIFEST_PATH = "/manifest.webmanifest"
MANIFEST_CONTENT_TYPE = "application/manifest+json"

# W1 巡检报告：markdown 必须以这个标题开头，json 顶层必须有这三键
INSPECTION_TITLE = "巡检报告"
INSPECTION_JSON_KEYS = ("generated_at", "sections", "summary")

# E1–E9 已 drop 的历史端点：不该复活成可用 API
LEGACY_PATHS = ("/api/agents", "/api/projects")

# /api/events 顶层键（Track K1，monitor_events.py:376-384 list_events 的
# empty/成功分支同构）。events 允许空列表——零活动事件是健康态，不是异常。
EVENTS_TOP_KEYS = (
    "events", "total", "limit", "offset", "active_count", "window_days", "generated_at",
)

# /api/capacity 顶层键（Track M2，capacity.py:175-184 _empty()）。machines 是
# **对象**（machine_id → 拟合结果），不是数组——这里的类型校验就是为了防止把它
# 误改成和其余列表型端点一样的数组顶层。
CAPACITY_TOP_KEYS = (
    "ok", "ttl", "history_type", "min_r_squared", "machines", "error",
)

# /api/machines 零 IP（第 16 项）用两层正则，理由见 check_machines_zero_ip 的
# docstring：全文层只认 Tailscale 的 CGNAT 段（100.64.0.0/10 = 第一段 100、第二段
# 64–127），不用通用 IPv4——machine_config.machines_for_api() 的 public_host 今天
# 是域名，但合法值本可以是任意公网 IP；通用 IPv4 全文扫描会把未来一台正常登记
# 公网 IP 的机器误判成 FAIL。这条正则刻意比上面 TAILSCALE_IP_RE（供 1–13 使用、
# 未收窄到 CGNAT 真实范围）更精确，两者不合并，避免改动既有 1–13 检查的判定面。
TAILSCALE_CGNAT_RE = re.compile(
    r"\b100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])(?:\.\d{1,3}){2}\b"
)

# probe_msg 字段层：该字段过 monitor_events.scrub_detail() 脱敏
# （_IPV4_RE = r"\b\d{1,3}(?:\.\d{1,3}){3}\b"，通用 IPv4，非仅 tailnet 段），脱敏后
# 不该残留任何 IPv4——只在这一个字段上用通用正则是安全的，因为它天生就不该含
# 地址，不会像全文扫描那样殃及合法的 public_host。
PROBE_MSG_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

# Only these *constant* paths/field names may enter diagnostics unchanged.
# A backend can return an arbitrary key/header/message, so regex redaction is
# a final guard rather than permission to echo whole response values.
DIAGNOSTIC_PATHS = frozenset(CORE_PAGES + LEGACY_PATHS + (
    "/api/health", "/api/ops-status", "/api/config/effective", "/api/tailnet",
    "/api/dashboard-layout", "/api/caddy/entry", "/api/events", "/api/capacity",
    "/api/inspection/report?format=markdown", "/api/inspection/report?format=json",
    "/api/ledger/overview", "/api/ledger/tx?size=1", "/api/ledger/dict", "/api/ledger/unknown",
    MANIFEST_PATH,
) + tuple(path for path, _, _ in LIST_API_CONTRACTS))
DIAGNOSTIC_EXTRA_FIELDS = frozenset({"address", "upstream_address", "tailscale_ip"})
MAX_DIAGNOSTIC_LENGTH = 500


def _safe_extra_fields(fields):
    known = sorted(DIAGNOSTIC_EXTRA_FIELDS.intersection(fields))
    unknown_count = len(fields) - len(known)
    if unknown_count:
        known.append(f"{unknown_count} 个其他字段（名称已省略）")
    return ", ".join(known)


def _safe_content_type(value):
    mime = value.split(";", 1)[0].strip()
    if mime in {"application/json", "text/html", "text/markdown", MANIFEST_CONTENT_TYPE,
                "text/plain", "application/octet-stream"}:
        return mime
    return "其他类型（值已省略）" if mime else "空"


def _safe_timestamp(value):
    if value is None:
        return "null"
    if not isinstance(value, str):
        return "非字符串（值已省略）"
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.isoformat()
    except ValueError:
        return "非日期时间（值已省略）"


def _safe_commit(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{7,40}", value):
        return value
    return "非标准提交号（值已省略）"


def _safe_machine_id(value, index):
    # A short machine slug is useful for locating a broken probe. Long or
    # arbitrary payloads use the array index; do not stringify objects.
    if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,15}", value):
        return value
    return f"索引 {index}"


def _redact_ipv6(match):
    candidate = match.group(0)
    try:
        ipaddress.IPv6Address(candidate)
    except ValueError:
        return candidate
    return "[IP 已省略]"


def _safe_diagnostic(message):
    """Bounded, single-line evidence, with no exception or response stringify."""
    if not isinstance(message, str):
        return "诊断已省略（非字符串）"
    message = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", message)
    message = "".join(ch for ch in message if unicodedata.category(ch) not in {"Cc", "Cf", "Cs"})
    message = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", "[URL 已省略]", message)
    message = re.sub(r"(?i)\b(?:cookie|set-cookie)\b[\"']?\s*[:=].*", "[Cookie 已省略]", message)
    message = re.sub(r"(?i)\b(?:bearer|basic)\s+[^\s,;]+", "[凭据已省略]", message)
    message = re.sub(
        r"(?i)\b(?:authorization|proxy-authorization|api[_-]?key|(?:access[_-]?|refresh[_-]?)?token|"
        r"password|passwd|pwd|secret)\b[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        "[凭据已省略]", message,
    )
    message = re.sub(r"\b(?:gh[pousr]_|github_pat_|sk-|tskey-)[A-Za-z0-9_./+=-]+",
                     "[Token 已省略]", message)
    message = re.sub(r"(?<![\w:])(?:[0-9a-fA-F]*:){2,}[0-9a-fA-F:.]*(?:%[A-Za-z0-9_.-]+)?",
                     _redact_ipv6, message)
    message = PROBE_MSG_IPV4_RE.sub("[IP 已省略]", message)
    # Quoted paths may contain spaces. Public API paths survive only by exact
    # allowlist match; a path merely starting with /api/ is not trusted.
    message = re.sub(r'''(["'])(?:~?/|\.\.?/|[A-Za-z]:[\\/]|\\\\)[^"']*\1''',
                     "[路径已省略]", message)
    message = re.sub(
        r'''(?<![\w/])(?:~?/|\.\.?/|[A-Za-z]:[\\/]|\\\\)[^\s<>"'，；：（）()\[\]{}]+''',
        lambda match: match.group(0) if match.group(0) in DIAGNOSTIC_PATHS else "[路径已省略]",
        message,
    )
    return message[:MAX_DIAGNOSTIC_LENGTH] + ("…" if len(message) > MAX_DIAGNOSTIC_LENGTH else "")


def _safe_base_url(value):
    """Keep the existing target field, omitting userinfo, query and fragments."""
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "[目标 URL 已省略]"
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{host}{port}"
    except ValueError:
        return "[目标 URL 已省略]"


# ---------------------------------------------------------------- HTTP 客户端


class Response:
    """一次 GET 的结果；4xx/5xx 也是结果，不是异常。"""

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def content_type(self):
        return (self.headers.get("Content-Type") or "").lower()

    def json(self):
        return json.loads(self.body)


class Client:
    """带轮内缓存的极简 GET 客户端：同一轮里重复取同一端点只发一次请求。"""

    def __init__(self, base_url, timeout=DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._cache = {}
        # 显式不走系统代理。验收目标只会是 127.0.0.1 或 tailnet 内网地址，而 mac 上
        # 常设的 HTTP_PROXY（Surge 等）配套的 no_proxy 通常只列 localhost——tailnet IP
        # 会被误送进代理，验收结果就取决于代理规则而不是被验环境。
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def reset_cache(self):
        self._cache.clear()

    def get(self, path, cache=True):
        if cache and path in self._cache:
            return self._cache[path]
        req = urllib.request.Request(
            self.base_url + path,
            headers={"Accept": "application/json, text/html;q=0.9, */*;q=0.8"},
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as raw:
                resp = Response(raw.status, dict(raw.headers), raw.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:  # 404 等属正常结果，交给核验项判定
            resp = Response(exc.code, dict(exc.headers or {}), exc.read().decode("utf-8", "replace"))
        if cache:
            self._cache[path] = resp
        return resp


# ---------------------------------------------------------------- 核验项注册表


class Check:
    def __init__(self, cid, name, fn):
        self.id = cid
        self.name = name
        self.fn = fn


CHECKS = []


def check(cid, name):
    """注册一个核验项；函数签名 (ctx) -> (ok: bool, message: str)。"""

    def decorator(fn):
        if any(c.id == cid for c in CHECKS):
            raise RuntimeError(f"核验项编号 {cid} 重复注册")
        CHECKS.append(Check(cid, name, fn))
        return fn

    return decorator


def all_checks():
    """按编号排序的核验项；代码里可按逻辑分组书写，输出恒为编号序。"""
    return sorted(CHECKS, key=lambda c: c.id)


class Context:
    """一轮核验的共享状态：HTTP 客户端 + 运行画像。"""

    def __init__(self, client, profile="auto", expected_monitoring_source=None):
        self.client = client
        self.profile = profile
        # B6-3 核验项 2 的 flag 预期值（beszel/console_pg）；None = 只验枚举不比对
        self.expected_monitoring_source = expected_monitoring_source

    def get(self, path, cache=True):
        return self.client.get(path, cache=cache)

    def is_nas(self, ops):
        """profile=auto 时按 running_commit 判定：真 commit 视为已部署环境。"""
        if self.profile != "auto":
            return self.profile == "nas"
        commit = (ops or {}).get("running_commit")
        return bool(commit) and commit != "dev"


# ---------------------------------------------------------------- 通用断言工具


def _expect_status(resp, path, expected=200):
    if resp.status != expected:
        return f"{path} 返回 {resp.status}，期望 {expected}"
    return None


def _missing(obj, keys):
    return [k for k in keys if k not in obj]


def _expect_json(resp, path):
    """确认响应确实是 JSON API 的输出。

    后端没有该路由时不会 404：未登记进 CONSOLE_API_PATHS / _is_console_api 的
    `/api/*` 会被同源代理转发给 Beszel，回来的是 Beszel 前端的 HTML 200。不验
    Content-Type 就会去解析/扫描那份 HTML，得出毫无意义的结论。
    """
    bad = _expect_status(resp, path)
    if bad:
        return bad
    if "application/json" not in resp.content_type:
        return (f"{path} Content-Type 为 {_safe_content_type(resp.content_type)}，非 JSON"
                "（未登记的 /api 前缀会被转发给 Beszel 代理）")
    return None


def _json_body(ctx, path):
    """取一个 JSON 端点，返回 (data, 错误说明)；两者恰有一个非 None。"""
    resp = ctx.get(path)
    bad = _expect_json(resp, path)
    if bad:
        return None, bad
    try:
        return resp.json(), None
    except ValueError:
        return None, f"{path} 响应不是合法 JSON"


def _list_problem(path, data, required, allow_empty):
    """校验一个列表型端点的顶层结构，返回错误说明（无问题返回 None）。"""
    if not isinstance(data, list):
        return f"{path} 顶层应为数组，实为 {type(data).__name__}"
    if not data and not allow_empty:
        return f"{path} 返回空数组"
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            return f"{path}[{index}] 应为对象，实为 {type(item).__name__}"
        missing = _missing(item, required)
        if missing:
            return f"{path}[{index}] 缺键 " + ", ".join(missing)
    return None


# ---------------------------------------------------------------- 核验项 1–3


@check(1, "/api/health 健康")
def check_health(ctx):
    resp = ctx.get("/api/health")
    bad = _expect_status(resp, "/api/health")
    if bad:
        return False, bad
    payload = resp.json()
    message = str(payload.get("message", ""))
    if "healthy" not in message.lower():
        return False, "message 不含 healthy（响应正文已省略）"
    return True, "200，message 含 healthy"


@check(2, "/api/ops-status 七键")
def check_ops_status(ctx):
    resp = ctx.get("/api/ops-status")
    bad = _expect_status(resp, "/api/ops-status")
    if bad:
        return False, bad
    ops = resp.json()
    if not isinstance(ops, dict):
        return False, f"顶层应为对象，实为 {type(ops).__name__}"
    missing = _missing(ops, OPS_STATUS_KEYS)
    if missing:
        return False, "缺键：" + ", ".join(missing)
    # B6-3 R5：值域校验。expected_source 由 --expect-monitoring-source 注入
    # （缺省 beszel）；实际值不在枚举内 = flag 拼错，切到 console_pg 后忘改
    # --expect = 配置漂移，两者都该红。
    actual_source = ops.get("monitoring_business_source")
    if actual_source not in MONITORING_BUSINESS_SOURCES:
        return False, "monitoring_business_source 不在枚举 {beszel, console_pg}"
    if ctx.expected_monitoring_source is not None and actual_source != ctx.expected_monitoring_source:
        return False, (f"monitoring_business_source={actual_source} 与预期 "
                       f"{ctx.expected_monitoring_source} 不一致（静默回落？）")
    if not ctx.is_nas(ops):
        return True, "七键齐全（dev 画像，跳过 running_commit 严格断言）"
    commit = ops.get("running_commit")
    if not commit or commit == "dev":
        return False, "NAS 画像下 running_commit 不应缺失或为 dev"
    last_deploy = ops.get("last_deploy")
    if not isinstance(last_deploy, dict):
        return False, f"last_deploy 应为对象，实为 {type(last_deploy).__name__}"
    deployed = last_deploy.get("commit")
    if not deployed:
        return False, "last_deploy.commit 缺失"
    # running_commit 是短 sha，last_deploy.commit 是全 sha：短的必须是长的前缀
    short, full = sorted([str(commit), str(deployed)], key=len)
    if not full.startswith(short):
        return False, "running_commit 与 last_deploy.commit 不一致"
    return True, f"七键齐全，running_commit={_safe_commit(commit)} 与 last_deploy 一致"


@check(3, "核心页面 200")
def check_core_pages(ctx):
    bad = []
    for path in CORE_PAGES:
        resp = ctx.get(path)
        if resp.status != 200:
            bad.append(f"{path}={resp.status}")
        elif "text/html" not in resp.content_type:
            bad.append(f"{path} 非 HTML（{_safe_content_type(resp.content_type)}）")
    if bad:
        return False, "，".join(bad)
    return True, f"{len(CORE_PAGES)} 个页面均 200 HTML"


# ---------------------------------------------------------------- 核验项 4–5


@check(4, "列表型 API 形状")
def check_list_apis(ctx):
    problems = []
    for path, required, allow_empty in LIST_API_CONTRACTS:
        data, err = _json_body(ctx, path)
        problems.append(err or _list_problem(path, data, required, allow_empty))
    problems = [p for p in problems if p]
    if problems:
        return False, "；".join(problems)
    return True, f"{len(LIST_API_CONTRACTS)} 个端点顶层结构与必含键均正确"


@check(5, "attention 无 probe-monitor")
def check_attention_kinds(ctx):
    data, err = _json_body(ctx, "/api/attention")
    if err:
        return False, err
    if not isinstance(data, list):
        return False, f"/api/attention 顶层应为数组，实为 {type(data).__name__}"
    hits = [item for item in data
            if isinstance(item, dict) and item.get("kind") in FORBIDDEN_ATTENTION_KINDS]
    if hits:
        return False, f"出现 {len(hits)} 条 probe-monitor 误报：机器探针长时间未运行（原始标题已省略）"
    kinds = sorted({item.get("kind") for item in data if isinstance(item, dict)
                    and isinstance(item.get("kind"), str) and item["kind"] in ATTENTION_KINDS})
    if any(isinstance(item, dict) and item.get("kind") not in kinds for item in data):
        kinds.append("其他类别（值已省略）")
    return True, f"{len(data)} 条关注项，kind={kinds or '无'}"


# --------------------------------------------------- 核验项 6 / 13（有效配置）


@check(6, "有效配置敏感遮蔽")
def check_effective_masking(ctx):
    resp = ctx.get("/api/config/effective")
    bad = _expect_json(resp, "/api/config/effective")
    if bad:
        return False, bad
    # 先确认是 JSON：路由不在时回来的是 Beszel 前端 HTML，而那份 HTML 内联的
    # BESZEL.HUB_URL 带真实内网 IP——直接扫会「扫到 IP 判 FAIL」，理由完全错位。
    # 扫原始响应全文而非解析后的对象：键名、嵌套、来源徽章里的残留都要覆盖。
    # 命中只报类别与条数，绝不回显命中内容——验收输出可能进日志或提交。
    hits = [f"{label} × {len(pattern.findall(resp.body))}"
            for label, pattern in SENSITIVE_PATTERNS if pattern.search(resp.body)]
    if hits:
        return False, "响应含未遮蔽的敏感文本：" + "，".join(hits)
    return True, f"200，{len(SENSITIVE_PATTERNS)} 类敏感正则全文零命中（{len(resp.body)} 字节）"


@check(13, "迁移基线 schema_version")
def check_schema_version(ctx):
    data, err = _json_body(ctx, "/api/config/effective")
    if err:
        return False, err
    if not isinstance(data, dict):
        return False, f"顶层应为对象，实为 {type(data).__name__}"
    version = data.get("schema_version")
    if version is None:
        return False, "schema_version 为 null——D1' 起该值应为 DB 已应用版本，null 说明读不到库"
    if not isinstance(version, int) or isinstance(version, bool):
        return False, f"schema_version 应为整数，实为 {type(version).__name__}"
    if version < MIN_SCHEMA_VERSION:
        return False, f"schema_version={version}，低于基线 {MIN_SCHEMA_VERSION}（迁移未应用？）"
    return True, f"schema_version={version} ≥ {MIN_SCHEMA_VERSION}"


@check(19, "存储引擎诊断（PG-04）")
def check_storage_engine(ctx):
    """/api/config/effective 的 storage_engine/database 字段：存在、合法、非敏感。

    PG-05 集成收口：割接后 PG 是**唯一**在线业务源（D3），engine=sqlite 不再是
    可接受的目标态——它意味着这台实例还在跑 legacy 存储，验收必须拦下。割接前的
    `--profile auto` 验收请用 `--skip 19`（或等割接窗口），不要靠「sqlite 合法」
    蒙过门禁。本检查只读验收接口，不直接连库。
    """
    data, err = _json_body(ctx, "/api/config/effective")
    if err:
        return False, err
    engine = data.get("storage_engine")
    if engine not in ("postgres", "sqlite"):
        return False, f"storage_engine 缺失或不合法：{engine!r}（应为 postgres/sqlite）"
    if engine == "sqlite":
        return False, (
            "storage_engine=sqlite：割接后 PG 是唯一在线业务源（D3），"
            "该实例仍在 legacy 存储；割接前验收请显式 --skip 19"
        )
    database = data.get("database")
    if not isinstance(database, dict):
        return False, "storage_engine=postgres 时 database 应为探针对象"
    if database.get("reachable") is not True:
        return False, f"database.reachable={database.get('reachable')!r}，PG 时代应为 True"
    # 探针对象本身是脱敏契约（pgdb.healthcheck）；键名含凭据字样即违反。
    suspect = [k for k in database if any(h in k.lower() for h in ("password", "token", "secret"))]
    if suspect:
        return False, f"database 对象含疑似敏感键（不回显）：{len(suspect)} 个"
    return True, (
        f"storage_engine=postgres，database 可达（server {database.get('server_version')!r}，"
        f"schema {database.get('schema_version')!r}）"
    )


# -------------------------------------------- 核验项 7–9（可降级卡片型端点）


def _source_freshness_result(ctx, data, sample_key):
    """Validate revoked values and source time; missing/null time is compatible.

    NAS treats stale/future acquisition as failed. Dev may accept it only when
    explicitly degraded and old values are revoked, never as available=true.
    """
    label, available_key, error_key, rows_key = SOURCE_CONTRACTS[sample_key]
    if data.get(available_key) is False:
        if data.get(rows_key) != []:
            return False, f"{label} 不可用，但 {rows_key} 旧值未撤销（应为空数组）"
        if sample_key == "metrics_sampled_at" and data.get("counting_since") is not None:
            return False, f"{label} 不可用，但 counting_since 仍保留旧值"
    error = data.get(error_key)
    stale = error == f"{label} 数据已过期（超过 15 分钟未更新）"
    future = error == f"{label} 数据采集时间异常（超过 60 秒未来容差）"
    # Caddy labels do not have a space before 数据; keep exact backend messages.
    if label.startswith("Caddy"):
        stale = error == f"{label}数据已过期（超过 15 分钟未更新）"
        future = error == f"{label}数据采集时间异常（超过 60 秒未来容差）"
    sampled = data.get(sample_key)
    if sampled is not None:
        if not isinstance(sampled, str):
            return False, f"{sample_key} 应为字符串或 null"
        try:
            parsed = datetime.datetime.fromisoformat(sampled.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("missing timezone")
            age = time.time() - parsed.timestamp()
            stale = stale or age > SOURCE_MAX_AGE_SECONDS
            future = future or age < -SOURCE_FUTURE_TOLERANCE_SECONDS
        except (ValueError, OverflowError, OSError):
            return False, f"{sample_key} 应为含时区的有效 ISO 日期时间"
    if not (stale or future):
        return None
    reason = "采集时间异常（超过 60 秒未来容差）" if future else "采集已过期（超过 15 分钟未更新）"
    ops = None
    if ctx.profile == "auto":
        ops, err = _json_body(ctx, "/api/ops-status")
        if (err or not isinstance(ops, dict)
                or not isinstance(ops.get("running_commit"), str) or not ops["running_commit"]):
            return False, f"{label}{reason}；无法确认运行画像"
    if ctx.is_nas(ops):
        return False, f"{label}{reason}；请检查采集链"
    if data.get(available_key) is not False:
        return False, f"{label}{reason}，但未显式降级"
    return True, f"{label}{reason}；dev 已显式降级并撤销旧值"


def _source_error_summary(error):
    """Only fixed acquisition categories, never an upstream error body."""
    if not isinstance(error, str) or not error:
        return "无"
    for fragment, summary in (
        ("已过期", "采集过期"), ("采集时间异常", "采集时间异常"),
        ("不存在", "文件或连接缺失"), ("解析失败", "解析失败"),
        ("不可读", "文件不可读"), ("超时", "请求超时"),
        ("无有效支持样本", "无有效支持样本"),
    ):
        if fragment in error:
            return summary
    return "详情已省略"


def _check_availability_endpoint(ctx, path, list_key, fields, sample_key):
    """校验 `available` + 列表 型端点（tailnet / caddy entry 同构）。

    available 必须是真布尔；为 true 时列表每项**恰含** fields（多一个少一个都算
    契约破坏）。内网 IP 全文扫描无条件执行——降级态的 error 字段同样不许带 IP。
    """
    resp = ctx.get(path)
    bad = _expect_json(resp, path)
    if bad:
        return False, bad
    leaked = TAILSCALE_IP_RE.findall(resp.body)
    if leaked:
        return False, f"{path} 响应含 {len(leaked)} 处内网 IP（应零 IP 出后端）"
    data = resp.json()
    if not isinstance(data, dict):
        return False, f"{path} 顶层应为对象，实为 {type(data).__name__}"
    available = data.get("available")
    if not isinstance(available, bool):
        return False, f"{path} available 应为布尔，实为 {type(available).__name__}"
    freshness = _source_freshness_result(ctx, data, sample_key)
    if freshness is not None:
        return freshness
    if not available:
        return True, f"available=false（降级态），零 IP；error={_source_error_summary(data.get('error'))}"
    items = data.get(list_key)
    if not isinstance(items, list):
        return False, f"{path} {list_key} 应为数组，实为 {type(items).__name__}"
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return False, f"{path} {list_key}[{index}] 应为对象，实为 {type(item).__name__}"
        actual = set(item)
        if actual != fields:
            extra = sorted(actual - fields)
            missing = sorted(fields - actual)
            detail = []
            if extra:
                detail.append("多出 " + _safe_extra_fields(extra))
            if missing:
                detail.append("缺少 " + ", ".join(missing))
            return False, f"{path} {list_key}[{index}] 字段不符：" + "；".join(detail)
    return True, f"available=true，{list_key} {len(items)} 项字段恰合白名单，零 IP"


@check(7, "/api/tailnet 契约")
def check_tailnet(ctx):
    return _check_availability_endpoint(
        ctx, "/api/tailnet", "machines", TAILNET_MACHINE_FIELDS, "source_sampled_at")


@check(8, "/api/dashboard-layout")
def check_dashboard_layout(ctx):
    data, err = _json_body(ctx, "/api/dashboard-layout")
    if err:
        return False, err
    if not isinstance(data, dict):
        return False, f"顶层应为对象，实为 {type(data).__name__}"
    items = data.get("items")
    if not isinstance(items, list):
        return False, f"items 应为数组，实为 {type(items).__name__}"
    return True, f"200，items 为数组（{len(items)} 项，updated_at={_safe_timestamp(data.get('updated_at'))}）"


@check(9, "/api/caddy/entry 契约")
def check_caddy_entry(ctx):
    return _check_availability_endpoint(
        ctx, "/api/caddy/entry", "upstreams", CADDY_UPSTREAM_FIELDS, "upstreams_sampled_at")


# ------------------------------------------------------------- 核验项 10–12


@check(10, "PWA manifest MIME")
def check_manifest(ctx):
    resp = ctx.get(MANIFEST_PATH)
    bad = _expect_status(resp, MANIFEST_PATH)
    if bad:
        return False, bad
    if MANIFEST_CONTENT_TYPE not in resp.content_type:
        return False, (f"Content-Type 为 {_safe_content_type(resp.content_type)}，"
                       f"应为 {MANIFEST_CONTENT_TYPE}（iOS 主屏安装依赖它）")
    return True, f"200，Content-Type={MANIFEST_CONTENT_TYPE}"


@check(11, "巡检报告双格式")
def check_inspection_report(ctx):
    problems = []
    md_path = "/api/inspection/report?format=markdown"
    md = ctx.get(md_path)
    md_bad = _expect_status(md, md_path)
    if md_bad:
        problems.append(md_bad)
    elif "text/markdown" not in md.content_type:
        problems.append(f"markdown 的 Content-Type 为 {_safe_content_type(md.content_type)}，应为 text/markdown")
    elif INSPECTION_TITLE not in md.body:
        problems.append(f"markdown 正文不含「{INSPECTION_TITLE}」标题")

    data, err = _json_body(ctx, "/api/inspection/report?format=json")
    if err:
        problems.append(err)
    elif not isinstance(data, dict):
        problems.append(f"json 顶层应为对象，实为 {type(data).__name__}")
    else:
        missing = _missing(data, INSPECTION_JSON_KEYS)
        if missing:
            problems.append("json 缺键 " + ", ".join(missing))

    if problems:
        return False, "；".join(problems)
    return True, f"markdown 与 json 均 200，markdown 含「{INSPECTION_TITLE}」标题"


@check(12, "legacy 端点已下线")
def check_legacy_gone(ctx):
    # 当前后端对未登记的 /api 路径不返 404，而是转发给 Beszel 代理（HTML 200）；
    # 404 与 HTML 都算「已下线」，真正的失败是它重新返回 JSON——历史端点复活了。
    alive = []
    for path in LEGACY_PATHS:
        resp = ctx.get(path)
        if resp.status == 404:
            continue
        if "application/json" in resp.content_type:
            alive.append(f"{path} 仍返回 {resp.status} JSON")
    if alive:
        return False, "，".join(alive)
    return True, f"{len(LEGACY_PATHS)} 个历史端点均非活 API（404 或转 Beszel 代理）"


# ------------------------------------------------------- 核验项 14–16（当前契约）


@check(14, "/api/events 契约")
def check_events(ctx):
    """Track K1 事件时间线：只断言顶层结构，不锁单条事件的字段集。

    单条事件的折叠标记（impact_count/collapsed_by）比这张表本身晚出现，锁死子
    字段容易把正常演进判成 FAIL；顶层这七键从 v10 起才是契约主体，才是验收该
    守的红线。events 允许空列表——零活动事件是健康态，不是异常。
    """
    data, err = _json_body(ctx, "/api/events")
    if err:
        return False, err
    if not isinstance(data, dict):
        return False, f"顶层应为对象，实为 {type(data).__name__}"
    missing = _missing(data, EVENTS_TOP_KEYS)
    if missing:
        return False, "缺键：" + ", ".join(missing)
    events = data.get("events")
    if not isinstance(events, list):
        return False, f"events 应为数组，实为 {type(events).__name__}"
    active = data.get("active_count")
    active_summary = active if isinstance(active, int) and not isinstance(active, bool) else "非整数（值已省略）"
    return True, f"200，七键齐全，events {len(events)} 条，active_count={active_summary}"


@check(15, "/api/capacity 契约")
def check_capacity(ctx):
    """Track M2 磁盘趋势：machines 是对象（machine_id → 拟合结果），不是数组——
    与其余列表型端点的形状不同，这里必须专门校验类型，否则「顶层结构」这条红线
    形同虚设。ok=false（Beszel 历史暂不可用）视为合法降级态，不判 FAIL，与
    tailnet/caddy 的 available=false（核验项 7/9）同一套哲学。
    """
    data, err = _json_body(ctx, "/api/capacity")
    if err:
        return False, err
    if not isinstance(data, dict):
        return False, f"顶层应为对象，实为 {type(data).__name__}"
    missing = _missing(data, CAPACITY_TOP_KEYS)
    if missing:
        return False, "缺键：" + ", ".join(missing)
    ok = data.get("ok")
    if not isinstance(ok, bool):
        return False, f"ok 应为布尔，实为 {type(ok).__name__}"
    machines = data.get("machines")
    if not isinstance(machines, dict):
        return False, f"machines 应为对象（machine_id → 拟合结果），实为 {type(machines).__name__}"
    return True, f"200，六键齐全，ok={ok}，machines {len(machines)} 台"


@check(16, "/api/machines 零内网 IP")
def check_machines_zero_ip(ctx):
    """零 IP 出后端，但不是朴素的「全文禁止任何 IPv4」。

    `machine_config.machines_for_api()` 的 `public_host` 今天恰好都是域名
    （machines.yaml 两台 node 用的是 kr/sg.sunxc.eu.cc），但它是合法的公开字段——
    将来登记一台 public_host 是裸公网 IP 的机器完全正常，若在这里做「全文零
    IPv4」会把那种正常情况误判成 FAIL，而那根本不是安全问题。

    真正要防的是 tailnet 内网地址泄漏，所以分两层：
    1. 全文层：只认 Tailscale 的 CGNAT 段（100.64.0.0/10），不用通用 IPv4——公网
       IP 无论落在哪个字段都不会被误伤，但内网段一旦出现在响应任何位置都算泄漏。
    2. 字段层：`probe_msg` 经 `monitor_events.scrub_detail()` 脱敏（通用 IPv4
       正则，非仅 tailnet 段），脱敏后这个字段不该残留任何 IPv4——只在这一个
       字段上用通用正则是安全的，因为它天生就不该含地址，不会像全文扫描那样
       殃及 public_host。
    """
    resp = ctx.get("/api/machines")
    bad = _expect_json(resp, "/api/machines")
    if bad:
        return False, bad
    cgnat_hits = TAILSCALE_CGNAT_RE.findall(resp.body)
    if cgnat_hits:
        return False, (f"响应含 {len(cgnat_hits)} 处 Tailscale 内网 IP"
                       "（100.64.0.0/10 CGNAT 段，应零 IP 出后端）")
    data = resp.json()
    if not isinstance(data, list):
        return False, f"顶层应为数组，实为 {type(data).__name__}"
    leaky = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            return False, f"[{index}] 应为对象，实为 {type(item).__name__}"
        if PROBE_MSG_IPV4_RE.search(str(item.get("probe_msg") or "")):
            leaky.append(_safe_machine_id(item.get("id"), index))
    if leaky:
        return False, "probe_msg 残留 IPv4（脱敏失效）：" + ", ".join(leaky)
    return True, (f"{len(data)} 台机器，全文零 Tailscale CGNAT 段，"
                  "probe_msg 均无 IPv4 残留")


@check(17, "/api/caddy/entry 累计量契约")
def check_caddy_metrics(ctx):
    r"""C11 累计量（2026-09-07）：与核验项 9 同一个端点，但守的是**另一条数据链**。

    刻意单独立项而不是并进 9：两条链各自降级——upstreams 的导出文件坏了、9 会在
    `available=false` 那一步就提前返回，`hosts` 根本不会被校验到。合成一项就等于
    让一条链的故障掩盖另一条链的契约破坏。

    `metrics_available=false` 视为合法降级态（cron 未就绪 / 指标文件缺失），
    与 7/9/15 同一套哲学；唯 NAS 画像明确过期视为采集不健康、判 FAIL。

    零 IP：核验项 9 对本端点做过全文扫描，但用的是 `TAILSCALE_IP_RE`
    （`100.\d+\.\d+\.\d+` 前缀），**只认 100 段**——一个 `192.168.*` 的 LAN 地址
    从 host 标签漏出来，9 完全看不见。所以这里补一层**字段级**断言：`host` 标签
    理论上可能是裸 IP（有人直接按 IP 打 Host 头），后端 `caddy_view._safe_host()`
    会把它折叠成 `_other`，这条检查确保那层折叠没失效。用通用 IPv4 正则是安全的
    ——host 天生就该是域名，不像 `public_host` 那样会误伤合法的公网 IP。
    """
    data, err = _json_body(ctx, "/api/caddy/entry")
    if err:
        return False, err
    if not isinstance(data, dict):
        return False, f"顶层应为对象，实为 {type(data).__name__}"
    available = data.get("metrics_available")
    if not isinstance(available, bool):
        return False, (f"metrics_available 应为布尔，实为 "
                       f"{type(available).__name__}")
    freshness = _source_freshness_result(ctx, data, "metrics_sampled_at")
    if freshness is not None:
        return freshness
    if not available:
        return True, ("metrics_available=false（降级态）；error="
                      + _source_error_summary(data.get("metrics_error")))
    hosts = data.get("hosts")
    if not isinstance(hosts, list):
        return False, f"hosts 应为数组，实为 {type(hosts).__name__}"
    for index, item in enumerate(hosts):
        if not isinstance(item, dict):
            return False, f"hosts[{index}] 应为对象，实为 {type(item).__name__}"
        actual = set(item)
        if actual != CADDY_HOST_FIELDS:
            extra = sorted(actual - CADDY_HOST_FIELDS)
            missing = sorted(CADDY_HOST_FIELDS - actual)
            detail = []
            if extra:
                detail.append("多出 " + _safe_extra_fields(extra))
            if missing:
                detail.append("缺少 " + ", ".join(missing))
            return False, f"hosts[{index}] 字段不符白名单：" + "；".join(detail)
        if PROBE_MSG_IPV4_RE.search(str(item.get("host") or "")):
            return False, (f"hosts[{index}].host 是裸 IP（_safe_host 折叠失效）"
                           "——域名维度不该出现地址")
    since = data.get("counting_since")
    if since is not None and not isinstance(since, str):
        return False, f"counting_since 应为字符串或 null，实为 {type(since).__name__}"
    total = sum(int(h.get("requests_total") or 0) for h in hosts)
    return True, (f"200，hosts {len(hosts)} 个域名恰合白名单、零裸 IP，"
                  f"累计 {total} 请求，counting_since={_safe_timestamp(since)}")


# ---------------------------------------------------------------- 执行与输出


@check(18, "/api/ledger 记账接口与代理边界")
def check_ledger(ctx):
    """检查空库/有数据库共用契约；诊断不输出金额、商户、备注或原始响应。"""
    overview, err = _json_body(ctx, "/api/ledger/overview")
    if err:
        return False, err
    if not isinstance(overview, dict):
        return False, "记账概览应为对象"
    for key in ("month_income", "month_expense", "year_income", "year_expense"):
        value = overview.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return False, f"记账概览 {key} 不是有限数值（值已省略）"
    for key in ("total", "uncategorized"):
        value = overview.get(key)
        if type(value) is not int or value < 0:
            return False, f"记账概览 {key} 不是非负整数（值已省略）"
    data, err = _json_body(ctx, "/api/ledger/tx?size=1")
    if err:
        return False, err
    if (not isinstance(data, dict) or not isinstance(data.get("rows"), list)
            or type(data.get("total")) is not int or data["total"] < 0
            or type(data.get("page")) is not int or data["page"] < 1):
        return False, "记账流水分页契约无效（值已省略）"
    if len(data["rows"]) > 1:
        return False, "记账流水未遵守 size 上限"
    dictionary, err = _json_body(ctx, "/api/ledger/dict")
    if err:
        return False, err
    if not isinstance(dictionary, dict) or any(
        not isinstance(values, list) or any(not isinstance(value, str) for value in values)
        for values in dictionary.values()
    ):
        return False, "记账分类字典契约无效（值已省略）"
    unknown = ctx.get("/api/ledger/unknown")
    if unknown.status != 404 or "application/json" not in unknown.content_type:
        return False, "记账未知接口应为 JSON 404，不能回落 Beszel 代理"
    return True, "概览、流水、字典契约通过；未知接口 JSON 404"


def run_once(ctx, checks, attempt=1, on_attempt=None):
    """跑一轮：每个核验项独立 try/except，异常判 FAIL 不抛栈。"""
    results = []
    for item in checks:
        started = time.perf_counter()
        try:
            ok, message = item.fn(ctx)
        except Exception as exc:  # noqa: BLE001 —— 任何异常都只降级为 FAIL
            # Even __str__ may fail or contain a credential/path/body. Only
            # fixed exception categories are diagnostic; never inspect args.
            category = {
                urllib.error.URLError: "URLError", TimeoutError: "TimeoutError",
                ConnectionError: "ConnectionError", OSError: "OSError",
                ValueError: "ValueError", TypeError: "TypeError", KeyError: "KeyError",
                json.JSONDecodeError: "JSONDecodeError",
            }.get(type(exc), "Exception")
            ok, message = False, f"{category}（异常详情已省略）"
        elapsed_ms = round(max(0.0, (time.perf_counter() - started) * 1000), 3)
        row = {"id": item.id, "name": item.name, "status": "PASS" if ok else "FAIL",
               "message": _safe_diagnostic(message), "attempt": attempt, "elapsed_ms": elapsed_ms}
        results.append(row)
        if on_attempt is not None:
            on_attempt(row)
    return results


def run(ctx, checks, skip_ids, retry, interval, on_attempt=None):
    skipped = [{"id": c.id, "name": c.name, "status": "SKIP", "message": "--skip 指定跳过",
                "attempt_history": []}
               for c in all_checks() if c.id in skip_ids]
    results = {r["id"]: r for r in skipped}
    pending = list(checks)
    for attempt in range(1, max(1, retry) + 1):
        if attempt > 1:
            time.sleep(interval)
            ctx.client.reset_cache()
        for row in run_once(ctx, pending, attempt=attempt, on_attempt=on_attempt):
            history = results.get(row["id"], {}).get("attempt_history", [])
            evidence = {key: row[key] for key in ("id", "attempt", "status", "elapsed_ms", "message")}
            row["attempt_history"] = history + [evidence]
            results[row["id"]] = row
        pending = [c for c in pending if results[c.id]["status"] == "FAIL"]
        if not pending or attempt == max(1, retry):
            break
    return [results[c.id] for c in all_checks() if c.id in results], attempt


def _pad(text, width):
    """按终端显示宽度补空格：中日韩全角字符占 2 列，f-string 的 :<n 会算错。"""
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - shown)


def format_row(row):
    return f"{row['status']:<4} [{row['id']:>2}] {_pad(row['name'], 26)} {row['message']}"


def _print_attempt(row):
    print(f"{row['status']:<4} [{row['id']:>2}] attempt {row['attempt']} "
          f"({row['elapsed_ms']:.3f} ms) {row['name']} {row['message']}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="console-stack 部署验收：逐项核验固定清单，任一 FAIL 退出码 1。")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"被验环境根地址（默认 {DEFAULT_BASE_URL}）")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="输出机器可读 JSON 而非逐行文本")
    parser.add_argument("--skip", default="",
                        help="跳过的核验项编号，逗号分隔，例如 --skip 7,9")
    parser.add_argument("--retry", type=int, default=1,
                        help=f"最多尝试轮数，失败项间隔 {RETRY_INTERVAL}s 重试（服务刚起用 3）")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"单次请求超时秒数（默认 {DEFAULT_TIMEOUT}）")
    parser.add_argument("--profile", choices=("auto", "nas", "dev"), default="auto",
                        help="运行画像：nas 启用 running_commit 严格断言，auto 按响应自动判定")
    parser.add_argument("--list", action="store_true", dest="list_checks",
                        help="只列出核验项编号与名称后退出")
    parser.add_argument("--expect-monitoring-source",
                        choices=MONITORING_BUSINESS_SOURCES, default=None,
                        help="B6-3：断言 ops-status.monitoring_business_source 等于该值"
                             "（beszel=影子态预期，console_pg=切换后预期）；缺省只验枚举")
    args = parser.parse_args(argv)

    if args.list_checks:
        for item in all_checks():
            print(f"[{item.id:>2}] {item.name}")
        return 0

    try:
        skip_ids = {int(x) for x in args.skip.split(",") if x.strip()}
    except ValueError:
        parser.error("--skip 需要逗号分隔的整数")
    unknown = skip_ids - {c.id for c in all_checks()}
    if unknown:
        parser.error("--skip 含未知核验项编号：" + ", ".join(str(i) for i in sorted(unknown)))

    ctx = Context(Client(args.base_url, args.timeout), args.profile,
                  expected_monitoring_source=args.expect_monitoring_source)
    selected = [c for c in all_checks() if c.id not in skip_ids]
    rows, attempts = run(ctx, selected, skip_ids, args.retry, RETRY_INTERVAL,
                         on_attempt=None if args.as_json else _print_attempt)

    failed = [r for r in rows if r["status"] == "FAIL"]
    if args.as_json:
        print(json.dumps({
            "base_url": _safe_base_url(ctx.client.base_url),
            "attempts": attempts,
            "passed": sum(1 for r in rows if r["status"] == "PASS"),
            "failed": len(failed),
            "skipped": sum(1 for r in rows if r["status"] == "SKIP"),
            "checks": rows,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"== 部署验收 {_safe_base_url(ctx.client.base_url)}（尝试 {attempts} 轮）")
        for row in rows:
            print(format_row(row))
        print(f"== {sum(1 for r in rows if r['status'] == 'PASS')} PASS / {len(failed)} FAIL / "
              f"{sum(1 for r in rows if r['status'] == 'SKIP')} SKIP")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
