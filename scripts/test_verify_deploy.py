#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_deploy.py 的自测：用 stdlib http.server 起一个假后端，断言各核验函数
对「好响应」与「坏响应」的判定。

跑法：`python3 -m pytest scripts/ -q`（除 pytest 外零依赖，不需要真实后端）。

所有样本数据都是虚构的：IP 用 100.64.0.1（Tailscale CGNAT 段起始，非本项目任何
真实机器），token 用 gho_EXAMPLE / sk-EXAMPLE——测试要构造「坏响应」验证检测逻辑，
但不把真实拓扑或凭据写进仓库。
"""
import datetime
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_deploy as vd  # noqa: E402 —— 必须先把 scripts/ 放进 sys.path

FAKE_IP = "100.64.0.1"
SHORT_SHA = "abc1234"
FULL_SHA = SHORT_SHA + "0" * 33
HTML = "text/html; charset=utf-8"
JSON = "application/json"
SPA_PAGE = "<!doctype html><html><title>自托管运维总控台</title></html>"
# 未登记的 /api 前缀会被转发给 Beszel，回来的是 Beszel 前端 HTML（内联真实 HUB_URL）
BESZEL_PAGE = '<!doctype html><html><title>Beszel</title><script>BESZEL={}</script></html>'


# ---------------------------------------------------------------- 假后端


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 —— BaseHTTPRequestHandler 的固定命名
        spec = self.server.routes.get(self.path)
        if isinstance(spec, list):  # 序列：每次请求消费一个，末项之后恒定
            spec = spec.pop(0) if len(spec) > 1 else spec[0]
        if spec is None:
            self.send_error(404, "not found")
            return
        status, content_type, body = spec
        payload = (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass  # 静音：pytest 输出里不需要访问日志


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.routes = {}
    # poll_interval 默认 0.5s，shutdown() 要等满一轮——50 个测试就白等 25s
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                              daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def ctx_for(httpd, routes, profile="auto", expected_monitoring_source=None):
    httpd.routes = routes
    return vd.Context(vd.Client(f"http://127.0.0.1:{httpd.server_address[1]}", timeout=5), profile,
                      expected_monitoring_source=expected_monitoring_source)


# ---------------------------------------------------------------- 好响应样本


def good_routes():
    """一份让全部核验项通过的假后端；每次调用返回全新对象。"""
    routes = {
        "/api/health": (200, JSON, {"code": 200, "data": {}, "message": "API is healthy."}),
        "/api/ops-status": (200, JSON, {
            "running_commit": SHORT_SHA,
            "versions_last_checked": "2026-09-06 12:00:06",
            "feed_last_fetched": "2026-09-06 12:23:03",
            "last_backup_tag": "20260906-182143",
            "last_deploy": {"backup_tag": "20260906-182143", "commit": FULL_SHA,
                            "deployed_at": "2026-09-06T18:22:08+08:00", "migrations_applied": vd.MIN_SCHEMA_VERSION},
            "last_probe_run": "2026-09-06T12:00:01Z",
            "timed_backup": {"state": "ok", "last_attempt": "2026-09-06T04:10:03+08:00",
                             "last_success": "2026-09-06T04:10:03+08:00",
                             "snapshot": "console.db.20260906", "stage": None},
            "monitoring_business_source": "beszel",
        }),
        "/api/services": (200, JSON, [
            # 第二项故意不带 container/port：真实台账里非容器类服务就是这样
            {"name": "console-brain", "machine": "nas", "type": "container", "health": {"status": "ok"},
             "running": "running", "tags": ["core"], "actions": ["restart"], "container": "console-brain",
             "port": 8081, "version": {"enabled": True}, "version_info": {"status": "ok"}},
            {"name": "caddy", "machine": "aly", "type": "host", "health": {"status": "ok"},
             "running": "running", "tags": [], "actions": [], "version": {"enabled": False},
             "version_info": {"status": "skipped"}},
        ]),
        "/api/panels": (200, JSON, [
            {"id": 1, "name": "Beszel", "url": "https://example.invalid", "machine": "nas",
             "enabled": True, "sort_order": 1, "icon": "chart", "note": "", "note2": "", "proxy": False},
        ]),
        "/api/renewals": (200, JSON, [
            {"id": "r1", "name": "示例域名", "kind": "domain", "cost": 55.0, "currency": "CNY",
             "cycle": "yearly", "expires_at": "2027-01-01", "auto_renew": False, "source": "manual"},
        ]),
        "/api/attention": (200, JSON, [
            {"severity": "warn", "kind": "version", "title": "mihomo 有可更新版本",
             "detail": "落后 2 个版本", "link": "/"},
        ]),
        "/api/machines": (200, JSON, [
            {"id": "nas", "label": "NAS", "role": "server", "capabilities": ["beszel"],
             "probe_status": "ok", "probe_ts": "2026-09-06T12:00:01Z",
             # public_host 真实值是域名（machines.yaml），但契约上允许是任意公网 IP;
             # probe_msg 已过 scrub_detail() 脱敏，正常态应为空串。
             "public_host": "nas.example.invalid", "probe_msg": ""},
            {"id": "kr", "label": "光帆 KR", "role": "node", "capabilities": ["beszel", "ssh"],
             "probe_status": "ok", "probe_ts": "2026-09-06T12:00:01Z",
             "public_host": "kr.sunxc.eu.cc", "probe_msg": ""},
        ]),
        "/api/tailnet": (200, JSON, {
            "available": True, "error": None, "sampled_at": "2026-09-06T12:20:01Z",
            "machines": [{"hostname": "nas", "online": True, "key_expiry_days": 120,
                          "last_seen": "2026-09-06T12:19:00Z", "client_version": "1.80.0",
                          "os": "linux", "update_available": False, "authorized": True,
                          "machine_id": "n1a"}],
        }),
        "/api/dashboard-layout": (200, JSON, {"items": [], "updated_at": None}),
        "/api/events": (200, JSON, {
            "events": [
                {"id": 1, "fingerprint": "machine:nas:probe", "kind": "machine",
                 "source": "probe", "target": "nas", "machine": "nas", "severity": "warn",
                 "title": "NAS 探针超时", "detail": "", "opened_at": "2026-09-06T10:00:00Z",
                 "updated_at": "2026-09-06T10:00:00Z", "resolved_at": None,
                 "impact_count": 0, "collapsed_by": None},
            ],
            "total": 1, "limit": 100, "offset": 0, "active_count": 1, "window_days": 7,
            "generated_at": "2026-09-06T12:25:00Z",
        }),
        "/api/capacity": (200, JSON, {
            "ok": True, "ttl": 3600, "history_type": "480m", "min_r_squared": 0.5,
            "machines": {"nas": {
                "span_hours": 240.0, "first_at": "2026-08-27T12:00:00Z",
                "last_at": "2026-09-06T12:00:00Z", "disk_total_gb": 40.0,
                "disk_used_gb": 12.3, "disk_free_gb": 27.7, "daily_gb": 0.05,
                "r_squared": 0.9, "trend": "flat", "days_to_full": None,
            }},
            "error": None,
        }),
        "/api/caddy/entry": (200, JSON, {
            "available": True, "error": None, "sampled_at": "2026-09-06T12:20:01Z",
            "upstreams": [{"id": "u1", "name": "axonhub", "num_requests": 0, "fails": 0}],
            # C11 累计量（核验项 17）：与 upstreams 同端点但另一条数据链，各自降级
            "metrics_available": True, "metrics_error": None,
            "counting_since": "2026-09-07T07:39:46Z",
            "hosts": [{"host": "axonhub.example.test", "requests_total": 40,
                       "ok_2xx": 38, "redirect_3xx": 0, "client_4xx": 0,
                       "server_5xx": 2}],
        }),
        "/api/config/effective": (200, JSON, {
            "generated_at": "2026-09-06T12:25:00Z", "last_probe_run": "2026-09-06T12:00:01Z",
            "running_commit": SHORT_SHA, "schema_version": vd.MIN_SCHEMA_VERSION,
            # PG-05 集成收口：割接后唯一合法目标态是 postgres + 可达探针对象。
            # 假后端模拟的是**已割接**实例，所以这里必须是 PG 形态（见 check 19）。
            "storage_engine": "postgres",
            "database": {"engine": "postgres", "reachable": True,
                         "database": "console", "user": "console_nas",
                         "server_version": "16.4", "timezone": "UTC",
                         "schema_version": vd.MIN_SCHEMA_VERSION, "latency_ms": 1.2},
            "groups": {"ops": {"title": "运维", "entries": [
                {"key": "CONSOLE_AUTH_ENABLED", "value": "false", "source": "default"}]}},
            "machines": [{"key": "tailscale", "machine": "nas", "source": "machines.yaml",
                          "value": "***"}],
        }),
        vd.MANIFEST_PATH: (200, vd.MANIFEST_CONTENT_TYPE, {"name": "Console", "icons": []}),
        "/api/inspection/report?format=markdown": (
            200, "text/markdown; charset=utf-8", "# 巡检报告 2026-09-06T12:25:55Z\n\n## 总评\n"),
        "/api/inspection/report?format=json": (200, JSON, {
            "generated_at": "2026-09-06T12:25:55Z", "sections": [], "summary": {"verdict": "ok"}}),
        "/api/ledger/overview": (200, JSON, {
            "year": "2026", "month": "09", "total": 0, "uncategorized": 0,
            "month_income": 0, "month_expense": 0, "year_income": 0, "year_expense": 0,
        }),
        "/api/ledger/tx?size=1": (200, JSON, {"total": 0, "page": 1, "rows": []}),
        "/api/ledger/dict": (200, JSON, {}),
        "/api/ledger/unknown": (404, JSON, {"error": "接口不存在"}),
    }
    for page in vd.CORE_PAGES:
        routes[page] = (200, HTML, SPA_PAGE)
    for legacy in vd.LEGACY_PATHS:  # 未登记的 /api 转 Beszel 代理，不是 404
        routes[legacy] = (200, HTML, BESZEL_PAGE)
    return routes


def run_check(httpd, routes, check_id, profile="auto", expected_monitoring_source=None):
    ctx = ctx_for(httpd, routes, profile, expected_monitoring_source)
    fn = next(c.fn for c in vd.all_checks() if c.id == check_id)
    return fn(ctx)


# ---------------------------------------------------------------- 整体


def test_good_backend_passes_every_check(server):
    ctx = ctx_for(server, good_routes())
    rows = vd.run_once(ctx, vd.all_checks())
    failed = [r for r in rows if r["status"] == "FAIL"]
    assert failed == [], failed
    # PG-04 起为 19 项；从 all_checks() 推导，避免加检查时改这里
    assert len(rows) == len(vd.all_checks()) == 19


def test_registry_is_contiguous_and_sorted():
    ids = [c.id for c in vd.all_checks()]
    assert ids == sorted(ids)
    assert ids == list(range(1, len(ids) + 1))


def test_duplicate_check_id_is_rejected():
    with pytest.raises(RuntimeError, match="重复注册"):
        vd.check(1, "冲突项")(lambda ctx: (True, ""))


# ---------------------------------------------------------------- 1 健康


def test_health_rejects_non_healthy_message(server):
    routes = good_routes()
    routes["/api/health"] = (200, JSON, {"message": "API is degraded."})
    ok, msg = run_check(server, routes, 1)
    assert ok is False
    assert "healthy" in msg


def test_health_rejects_500(server):
    routes = good_routes()
    routes["/api/health"] = (500, JSON, {"message": "boom"})
    ok, msg = run_check(server, routes, 1)
    assert ok is False
    assert "500" in msg


# ---------------------------------------------------------------- 2 ops-status


def test_ops_status_reports_missing_keys(server):
    routes = good_routes()
    payload = dict(routes["/api/ops-status"][2])
    del payload["last_probe_run"]
    routes["/api/ops-status"] = (200, JSON, payload)
    ok, msg = run_check(server, routes, 2)
    assert ok is False
    assert "last_probe_run" in msg


def test_ops_status_reports_missing_timed_backup(server):
    # G/F17：七键契约——旧六键后端缺 timed_backup 必须判 FAIL，不能静默通过
    routes = good_routes()
    payload = dict(routes["/api/ops-status"][2])
    del payload["timed_backup"]
    routes["/api/ops-status"] = (200, JSON, payload)
    ok, msg = run_check(server, routes, 2)
    assert ok is False
    assert "timed_backup" in msg


def test_ops_status_monitoring_source_enum_and_expect(server):
    # B6-3 R5：缺键、枚举外值、与 --expect-monitoring-source 不符都要 FAIL
    routes = good_routes()
    payload = dict(routes["/api/ops-status"][2])
    del payload["monitoring_business_source"]
    routes["/api/ops-status"] = (200, JSON, payload)
    ok, msg = run_check(server, routes, 2)
    assert ok is False
    assert "monitoring_business_source" in msg

    routes = good_routes()
    payload = dict(routes["/api/ops-status"][2])
    payload["monitoring_business_source"] = "conosle_pg"  # 拼写错误
    routes["/api/ops-status"] = (200, JSON, payload)
    ok, msg = run_check(server, routes, 2)
    assert ok is False
    assert "枚举" in msg

    # 缺省（不传 expect）：枚举内即 PASS
    ok, _ = run_check(server, good_routes(), 2, profile="nas")
    assert ok is True

    # expect=console_pg 而实际 beszel（静默回落场景）→ FAIL
    ok, msg = run_check(server, good_routes(), 2, profile="nas",
                        expected_monitoring_source="console_pg")
    assert ok is False
    assert "不一致" in msg

    # expect=beszel 且实际 beszel → PASS
    ok, _ = run_check(server, good_routes(), 2, profile="nas",
                      expected_monitoring_source="beszel")
    assert ok is True


def test_ops_status_dev_commit_fails_under_nas_profile(server):
    routes = good_routes()
    payload = dict(routes["/api/ops-status"][2])
    payload["running_commit"] = "dev"
    routes["/api/ops-status"] = (200, JSON, payload)
    assert run_check(server, routes, 2, profile="nas")[0] is False
    # dev 画像下同一份响应只校验七键齐全
    assert run_check(server, routes, 2, profile="dev")[0] is True


def test_ops_status_detects_commit_mismatch(server):
    routes = good_routes()
    payload = dict(routes["/api/ops-status"][2])
    payload["last_deploy"] = dict(payload["last_deploy"], commit="f" * 40)
    routes["/api/ops-status"] = (200, JSON, payload)
    ok, msg = run_check(server, routes, 2, profile="nas")
    assert ok is False
    assert "不一致" in msg


def test_ops_status_accepts_short_sha_prefix(server):
    # running_commit 是 7 位短 sha、last_deploy.commit 是 40 位全 sha，属正常
    ok, msg = run_check(server, good_routes(), 2, profile="nas")
    assert ok is True
    assert SHORT_SHA in msg


# ---------------------------------------------------------------- 3 页面


def test_core_page_failure_is_reported(server):
    routes = good_routes()
    routes["/settings"] = (500, HTML, "boom")
    ok, msg = run_check(server, routes, 3)
    assert ok is False
    assert "/settings" in msg


def test_core_page_must_be_html(server):
    routes = good_routes()
    routes["/feed"] = (200, JSON, {"unexpected": True})
    ok, msg = run_check(server, routes, 3)
    assert ok is False
    assert "/feed" in msg


# ---------------------------------------------------------------- 4 列表形状


def test_list_api_rejects_object_toplevel(server):
    routes = good_routes()
    routes["/api/panels"] = (200, JSON, {"items": []})
    ok, msg = run_check(server, routes, 4)
    assert ok is False
    assert "顶层应为数组" in msg


def test_list_api_rejects_missing_key(server):
    routes = good_routes()
    broken = [dict(routes["/api/machines"][2][0])]
    del broken[0]["probe_status"]
    routes["/api/machines"] = (200, JSON, broken)
    ok, msg = run_check(server, routes, 4)
    assert ok is False
    assert "probe_status" in msg


def test_list_api_rejects_empty_when_not_allowed(server):
    routes = good_routes()
    routes["/api/services"] = (200, JSON, [])
    ok, msg = run_check(server, routes, 4)
    assert ok is False
    assert "空数组" in msg


def test_list_api_allows_empty_attention(server):
    routes = good_routes()
    routes["/api/attention"] = (200, JSON, [])
    assert run_check(server, routes, 4)[0] is True


def test_list_api_tolerates_extra_fields(server):
    routes = good_routes()
    items = [dict(item, brand_new_field=1) for item in routes["/api/panels"][2]]
    routes["/api/panels"] = (200, JSON, items)
    assert run_check(server, routes, 4)[0] is True


def test_list_api_allows_service_without_optional_tags(server):
    routes = good_routes()
    services = [dict(item) for item in routes["/api/services"][2]]
    services[0].pop("tags")
    routes["/api/services"] = (200, JSON, services)
    assert run_check(server, routes, 4)[0] is True


# ---------------------------------------------------------------- 5 attention


def test_attention_flags_probe_monitor(server):
    routes = good_routes()
    routes["/api/attention"] = (200, JSON, [
        {"severity": "warn", "kind": "probe-monitor", "title": "机器探针长时间未运行",
         "detail": "本进程内机器探针尚未运行", "link": "/settings"},
    ])
    ok, msg = run_check(server, routes, 5)
    assert ok is False
    assert "机器探针长时间未运行" in msg


def test_attention_allows_normal_kinds(server):
    ok, msg = run_check(server, good_routes(), 5)
    assert ok is True
    assert "version" in msg


# ---------------------------------------------------------------- 6 遮蔽


@pytest.mark.parametrize("leak,label", [
    (FAKE_IP, "Tailscale 内网 IP"),
    ("gho_EXAMPLETOKEN", "GitHub token"),
    ("sk-EXAMPLEKEY", "API key"),
    ("Bearer EXAMPLE", "Authorization 凭据"),
])
def test_masking_detects_each_pattern(server, leak, label):
    routes = good_routes()
    payload = json.loads(json.dumps(routes["/api/config/effective"][2]))
    payload["groups"]["ops"]["entries"][0]["value"] = leak
    routes["/api/config/effective"] = (200, JSON, payload)
    ok, msg = run_check(server, routes, 6)
    assert ok is False
    assert label in msg
    assert leak not in msg  # 命中内容绝不回显：验收输出可能进日志或提交


def test_masking_ignores_words_containing_sk_dash(server):
    routes = good_routes()
    payload = json.loads(json.dumps(routes["/api/config/effective"][2]))
    payload["groups"]["ops"]["entries"][0]["value"] = "low-risk-mode"
    routes["/api/config/effective"] = (200, JSON, payload)
    assert run_check(server, routes, 6)[0] is True


def test_masking_rejects_html_proxy_fallback(server):
    # 路由不在时回来的是 Beszel 前端 HTML，其内联 HUB_URL 带真实内网 IP；
    # 必须报「非 JSON」，而不是去扫它然后以「扫到 IP」为由判 FAIL
    routes = good_routes()
    routes["/api/config/effective"] = (200, HTML, f"<html>HUB_URL:http://{FAKE_IP}:8090</html>")
    ok, msg = run_check(server, routes, 6)
    assert ok is False
    assert "非 JSON" in msg


# ---------------------------------------------------------------- 7 / 9 契约


def test_tailnet_rejects_extra_field(server):
    routes = good_routes()
    payload = json.loads(json.dumps(routes["/api/tailnet"][2]))
    payload["machines"][0]["tailscale_ip"] = FAKE_IP
    routes["/api/tailnet"] = (200, JSON, payload)
    ok, msg = run_check(server, routes, 7)
    assert ok is False
    # IP 全文扫描先于字段比对触发，两条理由都指向同一处泄漏
    assert "内网 IP" in msg or "多出 tailscale_ip" in msg


def test_tailnet_rejects_missing_field(server):
    routes = good_routes()
    payload = json.loads(json.dumps(routes["/api/tailnet"][2]))
    del payload["machines"][0]["authorized"]
    routes["/api/tailnet"] = (200, JSON, payload)
    ok, msg = run_check(server, routes, 7)
    assert ok is False
    assert "缺少 authorized" in msg


def test_tailnet_rejects_non_boolean_available(server):
    routes = good_routes()
    payload = json.loads(json.dumps(routes["/api/tailnet"][2]))
    payload["available"] = "true"
    routes["/api/tailnet"] = (200, JSON, payload)
    ok, msg = run_check(server, routes, 7)
    assert ok is False
    assert "available 应为布尔" in msg


def test_tailnet_accepts_degraded_state(server):
    routes = good_routes()
    routes["/api/tailnet"] = (200, JSON, {"available": False, "error": "快照过期",
                                          "sampled_at": None, "machines": []})
    ok, msg = run_check(server, routes, 7)
    assert ok is True
    assert "降级" in msg


def test_degraded_error_field_still_scanned_for_ip(server):
    routes = good_routes()
    routes["/api/tailnet"] = (200, JSON, {"available": False, "machines": [],
                                          "error": f"连不上 {FAKE_IP}", "sampled_at": None})
    ok, msg = run_check(server, routes, 7)
    assert ok is False
    assert "内网 IP" in msg


def test_caddy_entry_rejects_extra_field(server):
    routes = good_routes()
    payload = json.loads(json.dumps(routes["/api/caddy/entry"][2]))
    payload["upstreams"][0]["address"] = "nas:8765"
    routes["/api/caddy/entry"] = (200, JSON, payload)
    ok, msg = run_check(server, routes, 9)
    assert ok is False
    assert "多出 address" in msg


# ---------------------------------------------------------------- 8 布局


def test_dashboard_layout_requires_items_array(server):
    routes = good_routes()
    routes["/api/dashboard-layout"] = (200, JSON, {"items": {}, "updated_at": None})
    ok, msg = run_check(server, routes, 8)
    assert ok is False
    assert "items 应为数组" in msg


# ---------------------------------------------------------------- 10 manifest


def test_manifest_rejects_wrong_mime(server):
    routes = good_routes()
    routes[vd.MANIFEST_PATH] = (200, JSON, {"name": "Console"})
    ok, msg = run_check(server, routes, 10)
    assert ok is False
    assert vd.MANIFEST_CONTENT_TYPE in msg


# ---------------------------------------------------------------- 11 巡检报告


def test_inspection_requires_markdown_title(server):
    routes = good_routes()
    routes["/api/inspection/report?format=markdown"] = (
        200, "text/markdown; charset=utf-8", "# 别的标题\n")
    ok, msg = run_check(server, routes, 11)
    assert ok is False
    assert vd.INSPECTION_TITLE in msg


def test_inspection_requires_json_keys(server):
    routes = good_routes()
    routes["/api/inspection/report?format=json"] = (200, JSON, {"generated_at": "x"})
    ok, msg = run_check(server, routes, 11)
    assert ok is False
    assert "sections" in msg and "summary" in msg


def test_inspection_reports_both_formats_at_once(server):
    routes = good_routes()
    routes["/api/inspection/report?format=markdown"] = (500, "text/markdown", "boom")
    routes["/api/inspection/report?format=json"] = (500, JSON, {})
    ok, msg = run_check(server, routes, 11)
    assert ok is False
    assert msg.count("500") == 2


# ---------------------------------------------------------------- 12 legacy


def test_legacy_json_response_fails(server):
    routes = good_routes()
    routes["/api/agents"] = (200, JSON, [])
    ok, msg = run_check(server, routes, 12)
    assert ok is False
    assert "/api/agents" in msg


def test_legacy_404_passes(server):
    routes = good_routes()
    for legacy in vd.LEGACY_PATHS:
        del routes[legacy]  # 假后端对未注册路径回 404
    assert run_check(server, routes, 12)[0] is True


def test_legacy_beszel_proxy_fallback_passes(server):
    assert run_check(server, good_routes(), 12)[0] is True


# ---------------------------------------------------------------- 13 迁移基线


@pytest.mark.parametrize("value", [8, None, True, "9"])
def test_schema_version_rejects_bad_values(server, value):
    routes = good_routes()
    payload = json.loads(json.dumps(routes["/api/config/effective"][2]))
    payload["schema_version"] = value
    routes["/api/config/effective"] = (200, JSON, payload)
    assert run_check(server, routes, 13)[0] is False


def test_schema_version_accepts_future_version(server):
    routes = good_routes()
    payload = json.loads(json.dumps(routes["/api/config/effective"][2]))
    payload["schema_version"] = vd.MIN_SCHEMA_VERSION + 3
    routes["/api/config/effective"] = (200, JSON, payload)
    assert run_check(server, routes, 13)[0] is True


# ------------------------------------------------------- 迁移基线单一事实源绑定


def test_min_schema_version_matches_db_migrations():
    """MIN_SCHEMA_VERSION 是显式常量（verify_deploy.py 刻意零第三方依赖、不 import
    后端包，见文件头的设计说明），这里把它和 db.MIGRATIONS 的事实来源绑定：任一侧
    漂移（改了迁移忘改这里，或反过来）测试就红——这正是本任务要修的 bug 本身
    （db 已到 v10，verify_deploy.py 曾经还停在 9）。
    """
    backend_dir = Path(__file__).resolve().parent.parent / "console" / "backend"
    sys.path.insert(0, str(backend_dir))
    import db  # noqa: E402 —— 必须先把 console/backend 放进 sys.path

    max_version = max(version for version, _, _ in db.MIGRATIONS)
    assert vd.MIN_SCHEMA_VERSION == max_version


# ---------------------------------------------------------------- 14 events


def test_events_reports_missing_keys(server):
    routes = good_routes()
    payload = dict(routes["/api/events"][2])
    del payload["active_count"]
    routes["/api/events"] = (200, JSON, payload)
    ok, msg = run_check(server, routes, 14)
    assert ok is False
    assert "active_count" in msg


def test_events_rejects_non_list_events(server):
    routes = good_routes()
    payload = dict(routes["/api/events"][2])
    payload["events"] = {}
    routes["/api/events"] = (200, JSON, payload)
    ok, msg = run_check(server, routes, 14)
    assert ok is False
    assert "events 应为数组" in msg


def test_events_allows_empty_list(server):
    # 零活动事件是健康态（没有故障），不是异常——不同于 /api/services 那类必须
    # 非空的列表型端点（核验项 4）。
    routes = good_routes()
    payload = dict(routes["/api/events"][2])
    payload["events"] = []
    payload["total"] = 0
    payload["active_count"] = 0
    routes["/api/events"] = (200, JSON, payload)
    ok, msg = run_check(server, routes, 14)
    assert ok is True
    assert "0 条" in msg


def test_events_rejects_html_proxy_fallback(server):
    routes = good_routes()
    routes["/api/events"] = (200, HTML, BESZEL_PAGE)
    ok, msg = run_check(server, routes, 14)
    assert ok is False
    assert "非 JSON" in msg


# ---------------------------------------------------------------- 15 capacity


def test_capacity_reports_missing_keys(server):
    routes = good_routes()
    payload = dict(routes["/api/capacity"][2])
    del payload["min_r_squared"]
    routes["/api/capacity"] = (200, JSON, payload)
    ok, msg = run_check(server, routes, 15)
    assert ok is False
    assert "min_r_squared" in msg


def test_capacity_rejects_machines_as_list(server):
    # machines 契约上是对象（machine_id → 拟合结果），不是数组——这是与其余列表型
    # 端点最容易混淆、最该锁住的一点（capacity.py:175-184 _empty()）。
    routes = good_routes()
    payload = dict(routes["/api/capacity"][2])
    payload["machines"] = []
    routes["/api/capacity"] = (200, JSON, payload)
    ok, msg = run_check(server, routes, 15)
    assert ok is False
    assert "machines 应为对象" in msg


def test_capacity_rejects_non_boolean_ok(server):
    routes = good_routes()
    payload = dict(routes["/api/capacity"][2])
    payload["ok"] = "true"
    routes["/api/capacity"] = (200, JSON, payload)
    ok, msg = run_check(server, routes, 15)
    assert ok is False
    assert "ok 应为布尔" in msg


def test_capacity_accepts_degraded_ok_false(server):
    # Beszel 历史暂不可用时 ok=false + error 非空是合法降级态，不是 FAIL——
    # 与 tailnet/caddy 的 available=false（核验项 7/9）同一套哲学。
    routes = good_routes()
    routes["/api/capacity"] = (200, JSON, {
        "ok": False, "ttl": 3600, "history_type": "480m", "min_r_squared": 0.5,
        "machines": {}, "error": "beszel 历史暂不可用",
    })
    ok, msg = run_check(server, routes, 15)
    assert ok is True
    assert "ok=False" in msg


# ------------------------------------------------------- 16 machines 零内网 IP


def test_machines_zero_ip_flags_cgnat_leak_anywhere_in_body(server):
    # 全文层：CGNAT 地址混进任意字段（这里放进 label，模拟拓扑信息意外泄漏）都要
    # 判 FAIL，不局限于 probe_msg。
    routes = good_routes()
    items = [dict(item) for item in routes["/api/machines"][2]]
    items[0]["label"] = f"NAS ({FAKE_IP})"
    routes["/api/machines"] = (200, JSON, items)
    ok, msg = run_check(server, routes, 16)
    assert ok is False
    assert "CGNAT" in msg


def test_machines_zero_ip_flags_probe_msg_generic_ipv4(server):
    # 字段层：probe_msg 里残留一个非 CGNAT 段的 IPv4（203.0.113.0/24 是 RFC 5737
    # 文档保留段，确定不是任何真实机器）——全文 CGNAT 层认不出它，必须靠字段层的
    # 通用正则单独抓，这正是两层设计要覆盖的缺口。
    routes = good_routes()
    items = [dict(item) for item in routes["/api/machines"][2]]
    items[0]["probe_msg"] = "connect timeout to 203.0.113.5:8080"
    routes["/api/machines"] = (200, JSON, items)
    ok, msg = run_check(server, routes, 16)
    assert ok is False
    assert "probe_msg" in msg
    assert "nas" in msg


def test_machines_zero_ip_allows_plain_public_ipv4_in_public_host(server):
    # 反面用例：public_host 是一个合法公网 IP（非 CGNAT 段），必须 PASS——这是本
    # 检查项设计要避开的误报陷阱：今天两台 node 的 public_host 恰好是域名，但
    # 契约允许它是任意公网 IP，不能因为「看起来像 IP」就判 FAIL。
    routes = good_routes()
    items = [dict(item) for item in routes["/api/machines"][2]]
    items[0]["public_host"] = "203.0.113.9"
    routes["/api/machines"] = (200, JSON, items)
    ok, msg = run_check(server, routes, 16)
    assert ok is True


def test_machines_zero_ip_rejects_non_list_toplevel(server):
    routes = good_routes()
    routes["/api/machines"] = (200, JSON, {"items": []})
    ok, msg = run_check(server, routes, 16)
    assert ok is False
    assert "顶层应为数组" in msg


def test_machines_zero_ip_rejects_html_proxy_fallback(server):
    routes = good_routes()
    routes["/api/machines"] = (200, HTML, BESZEL_PAGE)
    ok, msg = run_check(server, routes, 16)
    assert ok is False
    assert "非 JSON" in msg


# ---------------------------------------------------------------- 运行器 / CLI


def test_retry_recovers_and_resets_cache(server):
    routes = good_routes()
    routes["/api/health"] = [(500, JSON, {"message": "starting"}),
                             (200, JSON, {"message": "API is healthy."})]
    ctx = ctx_for(server, routes)
    only_health = [c for c in vd.all_checks() if c.id == 1]
    rows, attempts = vd.run(ctx, only_health, set(), retry=3, interval=0)
    assert attempts == 2  # 第二轮就过，不会白跑第三轮
    assert rows[0]["status"] == "PASS"
    history = rows[0]["attempt_history"]
    assert [entry["status"] for entry in history] == ["FAIL", "PASS"]
    assert [entry["attempt"] for entry in history] == [1, 2]
    assert [entry["id"] for entry in history] == [1, 1]
    assert "500" in history[0]["message"]
    assert all(isinstance(entry["elapsed_ms"], (int, float)) and entry["elapsed_ms"] >= 0
               for entry in history)


def test_retry_gives_up_after_last_attempt(server):
    routes = good_routes()
    routes["/api/health"] = (500, JSON, {"message": "down"})
    ctx = ctx_for(server, routes)
    rows, attempts = vd.run(ctx, [c for c in vd.all_checks() if c.id == 1], set(), retry=2,
                            interval=0)
    assert attempts == 2
    assert rows[0]["status"] == "FAIL"
    assert [entry["status"] for entry in rows[0]["attempt_history"]] == ["FAIL", "FAIL"]
    assert [entry["attempt"] for entry in rows[0]["attempt_history"]] == [1, 2]


def test_exception_degrades_to_fail_without_traceback(server):
    ctx = ctx_for(server, {})
    ctx.client.base_url = "http://127.0.0.1:1"  # 必然连不上
    rows = vd.run_once(ctx, [c for c in vd.all_checks() if c.id == 1])
    assert rows[0]["status"] == "FAIL"
    assert "URLError" in rows[0]["message"] or "Errno" in rows[0]["message"]


def test_main_exit_code_and_skip(server, capsys):
    server.routes = good_routes()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    assert vd.main(["--base-url", base]) == 0

    server.routes["/api/health"] = (500, JSON, {"message": "down"})
    assert vd.main(["--base-url", base]) == 1
    assert vd.main(["--base-url", base, "--skip", "1"]) == 0
    out = capsys.readouterr().out
    assert "SKIP" in out


def test_main_json_output(server, capsys):
    server.routes = good_routes()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    assert vd.main(["--base-url", base, "--json", "--skip", "2,3"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["failed"] == 0
    assert payload["skipped"] == 2
    assert payload["base_url"] == base
    assert {r["id"] for r in payload["checks"]} == {c.id for c in vd.all_checks()}
    assert {"base_url", "attempts", "passed", "failed", "skipped", "checks"} <= payload.keys()
    for row in payload["checks"]:
        assert len(row["attempt_history"]) == (0 if row["status"] == "SKIP" else 1)


def test_main_rejects_unknown_skip_id(server):
    with pytest.raises(SystemExit):
        vd.main(["--skip", "99"])


def test_pad_counts_wide_characters():
    # 中文占 2 列：f-string 的 :<n 按字符数算会把输出排歪
    assert vd._pad("巡检", 6) == "巡检  "
    assert vd._pad("ab", 4) == "ab  "


def test_attempt_text_keeps_first_failure_after_success(server, monkeypatch, capsys):
    routes = good_routes()
    routes["/api/health"] = [(500, JSON, {"message": "starting"}),
                             (200, JSON, {"message": "API is healthy."})]
    server.routes = routes
    monkeypatch.setattr(vd, "RETRY_INTERVAL", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    assert vd.main(["--base-url", base, "--retry", "2"]) == 0
    out = capsys.readouterr().out
    assert "500" in out
    assert "attempt 1" in out and "attempt 2" in out
    assert "FAIL" in out and "PASS" in out


def test_attempt_json_keeps_first_failure_after_success(server, monkeypatch, capsys):
    server.routes = good_routes()
    server.routes["/api/health"] = [(500, JSON, {"message": "PRIVATE_STARTING_BODY"}),
                                    (200, JSON, {"message": "healthy"})]
    monkeypatch.setattr(vd, "RETRY_INTERVAL", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    assert vd.main(["--base-url", base, "--retry", "2", "--json", "--skip", "2"]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    health = next(row for row in payload["checks"] if row["id"] == 1)
    assert payload["attempts"] == 2
    assert payload["failed"] == 0 and payload["passed"] == len(vd.all_checks()) - 1 and payload["skipped"] == 1
    assert health["status"] == "PASS"
    assert [entry["status"] for entry in health["attempt_history"]] == ["FAIL", "PASS"]
    assert "500" in health["attempt_history"][0]["message"]
    assert "PRIVATE_STARTING_BODY" not in out


def test_attempt_text_keeps_persistent_failures_and_skip(server, monkeypatch, capsys):
    server.routes = good_routes()
    server.routes["/api/health"] = (503, JSON, {"message": "PRIVATE_DOWN_BODY"})
    monkeypatch.setattr(vd, "RETRY_INTERVAL", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    assert vd.main(["--base-url", base, "--retry", "2", "--skip", "2"]) == 1
    out = capsys.readouterr().out
    assert "FAIL [ 1] attempt 1" in out and "FAIL [ 1] attempt 2" in out
    assert f"{len(vd.all_checks()) - 2} PASS / 1 FAIL / 1 SKIP" in out
    assert "PRIVATE_DOWN_BODY" not in out
    assert all("attempt" not in line for line in out.splitlines() if line.startswith("SKIP"))


def test_attempt_callback_precedes_next_check_and_success_is_not_retried(server):
    evidence = []
    calls = []

    def first(ctx):
        calls.append(1)
        return True, "ready"

    def second(ctx):
        assert evidence[0]["id"] == 1
        calls.append(2)
        return calls.count(2) > 1, "starting"

    rows, attempts = vd.run(ctx_for(server, {}), [vd.Check(1, "first", first), vd.Check(2, "second", second)],
                            set(), retry=3, interval=0, on_attempt=lambda row: evidence.append(dict(row)))
    assert attempts == 2 and calls == [1, 2, 2]
    assert [entry["attempt"] for entry in evidence] == [1, 1, 2]
    assert [len(row["attempt_history"]) for row in rows] == [1, 2]


def test_skips_have_no_fabricated_attempt(server):
    ctx = ctx_for(server, good_routes())
    rows, attempts = vd.run(ctx, [], {1}, retry=3, interval=0)
    assert attempts == 1
    assert rows[0]["status"] == "SKIP"
    assert rows[0]["attempt_history"] == []


def test_attempt_duration_is_per_check_monotonic(server, monkeypatch):
    ctx = ctx_for(server, {})
    ticks = iter([10.0, 10.025])
    monkeypatch.setattr(vd.time, "perf_counter", lambda: next(ticks))
    row = vd.run_once(ctx, [vd.Check(1, "duration", lambda ctx: (True, "ok"))])[0]
    assert row["elapsed_ms"] == pytest.approx(25.0)


def test_attempt_exception_message_never_stringifies_untrusted_exception(server):
    class UnsafeException(Exception):
        def __str__(self):
            raise RuntimeError("exception string must never be inspected")

    def fail(ctx):
        raise UnsafeException()

    row = vd.run_once(ctx_for(server, {}), [vd.Check(1, "unsafe", fail)])[0]
    assert row["status"] == "FAIL"
    assert "exception string" not in row["message"]


@pytest.mark.parametrize("as_exception", [False, True])
def test_attempt_diagnostics_remove_credentials_paths_and_ips(server, as_exception):
    secret_parts = ["alice", "SYNTHETIC_PASSWORD", "SYNTHETIC_TOKEN", "private-token.txt",
                    "ghp_SYNTHETIC", "sk-SYNTHETIC", "203.0.113.10", "2001:db8::5"]
    message = (
        "GET https://alice:SYNTHETIC_PASSWORD@example.invalid/secret?token=SYNTHETIC_TOKEN "
        "/private/config/private-token.txt C:\\private\\private-token.txt "
        "ghp_SYNTHETIC sk-SYNTHETIC 203.0.113.10 [2001:db8::5]"
    )

    def fail(ctx):
        if as_exception:
            raise RuntimeError(message)
        return False, message

    row = vd.run_once(ctx_for(server, {}), [vd.Check(1, "safe", fail)])[0]
    assert row["status"] == "FAIL"
    for fragment in secret_parts:
        assert fragment not in json.dumps(row)


@pytest.mark.parametrize("message,private", [
    ("HTTP 500 Bearer SYNTHETIC_BEARER", "SYNTHETIC_BEARER"),
    ("HTTP 500 Basic SYNTHETIC_BASIC", "SYNTHETIC_BASIC"),
    ('HTTP 500 api_key: "SYNTHETIC_API_KEY"', "SYNTHETIC_API_KEY"),
    ("HTTP 500 password='SYNTHETIC PASSWORD WITH SPACES'", "SYNTHETIC PASSWORD"),
    ("HTTP 500 Cookie: session=SYNTHETIC_COOKIE; other=SECOND_PRIVATE", "SECOND_PRIVATE"),
    ("HTTP 500 github_pat_SYNTHETIC", "github_pat_SYNTHETIC"),
    ("HTTP 500 tskey-auth-SYNTHETIC", "tskey-auth-SYNTHETIC"),
    ('HTTP 500 "/private folder/SYNTHETIC_FILENAME.cfg"', "SYNTHETIC_FILENAME"),
    (r'HTTP 500 "C:\private folder\SYNTHETIC_FILENAME.cfg"', "SYNTHETIC_FILENAME"),
    ("HTTP 500 [2001:db8::5]:8080", "2001:db8::5"),
    ("HTTP 500 fe80::5%en0", "fe80::5"),
    ("HTTP 500 ::ffff:192.0.2.5", "192.0.2.5"),
    ("HTTP 500\x00\r\n\x1b[31m token=SYNTHETIC_CONTROL\x1b[0m", "SYNTHETIC_CONTROL"),
])
def test_attempt_sanitizer_variants_keep_http_evidence(server, message, private):
    row = vd.run_once(ctx_for(server, {}), [vd.Check(1, "safe", lambda ctx: (False, message))])[0]
    assert private not in row["message"]
    assert "HTTP 500" in row["message"]
    assert not any(ord(ch) < 32 for ch in row["message"])


def test_attempt_sanitizes_before_truncating(server):
    message = "HTTP 500 " + "x" * 465 + " Bearer SYNTHETIC_PRIVATE_" + "y" * 1000
    row = vd.run_once(ctx_for(server, {}), [vd.Check(1, "long", lambda ctx: (False, message))])[0]
    assert "SYNTHETIC" not in row["message"]
    assert len(row["message"]) <= vd.MAX_DIAGNOSTIC_LENGTH + 1


@pytest.mark.parametrize("as_json", [False, True])
def test_attempt_cli_target_does_not_echo_url_credentials(as_json, capsys):
    base = "https://alice:SYNTHETIC_PASSWORD@example.invalid/private/file?token=SYNTHETIC_TOKEN#SYNTHETIC_FRAGMENT"
    args = ["--base-url", base, "--skip", ",".join(str(check.id) for check in vd.all_checks())]
    if as_json:
        args.append("--json")
    assert vd.main(args) == 0
    out = capsys.readouterr().out
    assert "SYNTHETIC" not in out and "alice" not in out and "/private/file" not in out
    if as_json:
        assert json.loads(out)["base_url"] == "https://example.invalid"


@pytest.mark.parametrize("cid", [1, 7, 9, 17])
def test_attempt_does_not_copy_opaque_response_body(server, cid):
    routes = good_routes()
    opaque = "PRIVATE_RESPONSE_VALUE_WITHOUT_A_TOKEN_PREFIX"
    if cid == 1:
        routes["/api/health"][2]["message"] = opaque
    else:
        endpoint = "/api/tailnet" if cid == 7 else "/api/caddy/entry"
        routes[endpoint][2]["metrics_available" if cid == 17 else "available"] = False
        routes[endpoint][2]["metrics_error" if cid == 17 else "error"] = opaque
    check = next(check for check in vd.all_checks() if check.id == cid)
    rows, _ = vd.run(ctx_for(server, routes), [check], set(), retry=1, interval=0)
    assert opaque not in json.dumps(rows)


@pytest.mark.parametrize("case,cid", [
    ("commit", 2), ("title", 5), ("kind", 5), ("schema", 13), ("updated", 8),
    ("extra-tailnet", 7), ("extra-upstream", 9), ("extra-host", 17), ("active", 14),
    ("capacity", 15), ("machine-id", 16), ("counting", 17), ("mime-json", 4),
    ("mime-html", 3), ("mime-manifest", 10), ("mime-markdown", 11),
])
def test_attempt_omits_opaque_dynamic_response_fields(server, case, cid):
    routes = good_routes()
    opaque = "PRIVATE_RESPONSE_VALUE_WITHOUT_A_TOKEN_PREFIX"
    if case == "commit":
        routes["/api/ops-status"][2].update(running_commit=opaque, last_deploy={"commit": opaque})
    elif case == "title":
        routes["/api/attention"][2][0].update(kind="probe-monitor", title=opaque)
    elif case == "kind":
        routes["/api/attention"][2][0]["kind"] = opaque
    elif case == "schema":
        routes["/api/config/effective"][2]["schema_version"] = opaque
    elif case == "updated":
        routes["/api/dashboard-layout"][2]["updated_at"] = opaque
    elif case.startswith("extra-"):
        path, key = {"extra-tailnet": ("/api/tailnet", "machines"),
                     "extra-upstream": ("/api/caddy/entry", "upstreams"),
                     "extra-host": ("/api/caddy/entry", "hosts")}[case]
        routes[path][2][key][0][opaque] = "extra"
    elif case == "active":
        routes["/api/events"][2]["active_count"] = opaque
    elif case == "capacity":
        routes["/api/capacity"][2]["ok"] = opaque
    elif case == "machine-id":
        routes["/api/machines"][2][0].update(id=opaque, probe_msg="203.0.113.5")
    elif case == "counting":
        routes["/api/caddy/entry"][2]["counting_since"] = opaque
    elif case.startswith("mime-"):
        path = {"mime-json": "/api/services", "mime-html": "/feed",
                "mime-manifest": vd.MANIFEST_PATH,
                "mime-markdown": "/api/inspection/report?format=markdown"}[case]
        routes[path] = (200, opaque, routes[path][2])
    check = next(check for check in vd.all_checks() if check.id == cid)
    rows, _ = vd.run(ctx_for(server, routes), [check], set(), retry=1, interval=0)
    assert opaque.lower() not in json.dumps(rows).lower()


@pytest.mark.parametrize(
    "cid,path,field,flag,error_key,rows_key",
    [
        (7, "/api/tailnet", "source_sampled_at", "available", "error", "machines"),
        (9, "/api/caddy/entry", "upstreams_sampled_at", "available", "error", "upstreams"),
        (17, "/api/caddy/entry", "metrics_sampled_at", "metrics_available", "metrics_error", "hosts"),
    ],
)
@pytest.mark.parametrize("age,profile,available,expected", [
    (899, "nas", True, True), (900, "nas", True, True), (901, "nas", False, False),
    (901, "auto", False, False), (901, "dev", False, True), (901, "dev", True, False),
    (-59, "nas", True, True), (-60, "nas", True, True), (-61, "nas", False, False),
    (-86400, "nas", False, False), (-61, "dev", False, True), (-61, "dev", True, False),
])
def test_verifier_source_clock_contract(server, monkeypatch, cid, path, field, flag,
                                        error_key, rows_key, age, profile, available, expected):
    now = 1800000000
    monkeypatch.setattr(vd.time, "time", lambda: now)
    routes = good_routes()
    payload = routes[path][2]
    payload[field] = datetime.datetime.fromtimestamp(now - age, datetime.timezone.utc).isoformat()
    payload[flag] = available
    if not available:
        payload[rows_key] = []
        payload[error_key] = "source degraded"
        if cid == 17:
            payload["counting_since"] = None
    ok, message = run_check(server, routes, cid, profile=profile)
    assert ok is expected
    if not expected:
        assert "过期" in message or "采集时间异常" in message


@pytest.mark.parametrize("cid,field", [(7, "source_sampled_at"), (9, "upstreams_sampled_at"),
                                       (17, "metrics_sampled_at")])
@pytest.mark.parametrize("sampled", [True, 123, {}, "invalid", "2026-09-08", "2026-09-08T12:00:00"])
def test_verifier_json_source_rejects_malformed_timestamps(server, cid, field, sampled):
    routes = good_routes()
    path = "/api/tailnet" if cid == 7 else "/api/caddy/entry"
    routes[path][2][field] = sampled
    assert run_check(server, routes, cid, profile="dev")[0] is False


SOURCE_CASES = [
    (7, "/api/tailnet", "source_sampled_at", "available", "error", "machines", "Tailnet 数据"),
    (9, "/api/caddy/entry", "upstreams_sampled_at", "available", "error", "upstreams", "Caddy 上游数据"),
    (17, "/api/caddy/entry", "metrics_sampled_at", "metrics_available", "metrics_error", "hosts", "Caddy 指标数据"),
]


@pytest.mark.parametrize("cid,path,field,flag,error_key,rows_key,prefix", SOURCE_CASES)
@pytest.mark.parametrize("sampled", ["missing", None, "fresh"])
@pytest.mark.parametrize("profile", ["nas", "dev", "auto"])
@pytest.mark.parametrize("keep_old_values", [False, True])
def test_verifier_unavailable_source_requires_revoked_values(server, monkeypatch, cid, path, field,
                                                              flag, error_key, rows_key, prefix,
                                                              sampled, profile, keep_old_values):
    now = 1800000000
    monkeypatch.setattr(vd.time, "time", lambda: now)
    routes = good_routes()
    payload = routes[path][2]
    payload[flag] = False
    if sampled != "missing":
        payload[field] = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat() if sampled else None
    if not keep_old_values:
        payload[rows_key] = []
        if cid == 17:
            payload["counting_since"] = None
    ok, message = run_check(server, routes, cid, profile=profile)
    assert ok is (not keep_old_values)
    if keep_old_values:
        assert "旧值" in message


@pytest.mark.parametrize("sampled", ["missing", None, "fresh"])
@pytest.mark.parametrize("profile", ["nas", "dev", "auto"])
def test_verifier_unavailable_metrics_counting_since_is_revoked(server, monkeypatch, sampled, profile):
    now = 1800000000
    monkeypatch.setattr(vd.time, "time", lambda: now)
    routes = good_routes()
    payload = routes["/api/caddy/entry"][2]
    payload.update(metrics_available=False, hosts=[])
    if sampled != "missing":
        payload["metrics_sampled_at"] = (
            datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat() if sampled else None)
    ok, message = run_check(server, routes, 17, profile=profile)
    assert ok is False and "旧值" in message


@pytest.mark.parametrize("cid,path,field,flag,error_key,rows_key,prefix", SOURCE_CASES)
@pytest.mark.parametrize("include_null", [False, True])
def test_verifier_source_legacy_missing_and_null_are_compatible(server, cid, path, field, flag,
                                                              error_key, rows_key, prefix, include_null):
    routes = good_routes()
    assert field not in routes[path][2]
    if include_null:
        routes[path][2][field] = None
    assert run_check(server, routes, cid, profile="nas")[0] is True


@pytest.mark.parametrize("cid,path,field,flag,error_key,rows_key,prefix", SOURCE_CASES)
@pytest.mark.parametrize("profile,expected", [("nas", False), ("auto", False), ("dev", True)])
@pytest.mark.parametrize("suffix", ["已过期（超过 15 分钟未更新）", "采集时间异常（超过 60 秒未来容差）"])
def test_verifier_source_fixed_error_without_timestamp(server, cid, path, field, flag, error_key,
                                                      rows_key, prefix, profile, expected, suffix):
    routes = good_routes()
    payload = routes[path][2]
    payload.update({field: None, flag: False, error_key: prefix + suffix, rows_key: []})
    if cid == 17:
        payload["counting_since"] = None
    assert run_check(server, routes, cid, profile=profile)[0] is expected


@pytest.mark.parametrize("cid,path,field,flag,error_key,rows_key,prefix", SOURCE_CASES)
def test_verifier_source_dev_degraded_must_revoke_old_rows(server, monkeypatch, cid, path, field,
                                                         flag, error_key, rows_key, prefix):
    now = 1800000000
    monkeypatch.setattr(vd.time, "time", lambda: now)
    routes = good_routes()
    routes[path][2].update({field: datetime.datetime.fromtimestamp(now - 901, datetime.timezone.utc).isoformat(),
                           flag: False})
    ok, message = run_check(server, routes, cid, profile="dev")
    assert ok is False and "旧值" in message


def test_verifier_source_dev_degraded_metrics_must_revoke_counting_since(server, monkeypatch):
    now = 1800000000
    monkeypatch.setattr(vd.time, "time", lambda: now)
    routes = good_routes()
    routes["/api/caddy/entry"][2].update(
        metrics_sampled_at=datetime.datetime.fromtimestamp(now - 901, datetime.timezone.utc).isoformat(),
        metrics_available=False, hosts=[])
    ok, message = run_check(server, routes, 17, profile="dev")
    assert ok is False and "旧值" in message


@pytest.mark.parametrize("ops", [(500, JSON, {}), (200, HTML, "not json"), (200, JSON, {}),
                                 (200, JSON, {"running_commit": None})])
def test_verifier_source_auto_does_not_assume_dev_when_ops_unknown(server, ops):
    routes = good_routes()
    routes["/api/ops-status"] = ops
    routes["/api/tailnet"][2].update(available=False, machines=[],
                                    error="Tailnet 数据已过期（超过 15 分钟未更新）")
    ok, message = run_check(server, routes, 7)
    assert ok is False and "无法确认运行画像" in message


def test_verifier_source_auto_accepts_explicit_dev_degradation(server):
    routes = good_routes()
    routes["/api/ops-status"][2]["running_commit"] = "dev"
    routes["/api/tailnet"][2].update(available=False, machines=[],
                                    error="Tailnet 数据已过期（超过 15 分钟未更新）")
    assert run_check(server, routes, 7)[0] is True


def test_verifier_source_clock_constants_match_backend(monkeypatch):
    backend_dir = Path(__file__).resolve().parent.parent / "console" / "backend"
    monkeypatch.syspath_prepend(str(backend_dir))
    import snapshot_freshness

    assert vd.SOURCE_MAX_AGE_SECONDS == snapshot_freshness.SOURCE_MAX_AGE_SECONDS
    assert vd.SOURCE_FUTURE_TOLERANCE_SECONDS == snapshot_freshness.SOURCE_FUTURE_TOLERANCE_SECONDS


# ------------------------------------------------- 核验项 17（C11 累计量契约）


@pytest.mark.parametrize("profile,age,available,expected", [
    ("nas", 0, True, True), ("nas", 900, True, True),
    ("nas", 901, False, False), ("dev", 901, False, True),
    ("nas", 901, True, False),
])
def test_caddy_metrics_freshness(server, monkeypatch, profile, age, available, expected):
    now = 1800000000
    monkeypatch.setattr(vd.time, "time", lambda: now)
    routes = good_routes()
    payload = routes["/api/caddy/entry"][2]
    payload["metrics_sampled_at"] = datetime.datetime.fromtimestamp(
        now - age, datetime.timezone.utc
    ).isoformat()
    payload["metrics_available"] = available
    if not available:
        payload.update(hosts=[], counting_since=None,
                       metrics_error="Caddy 指标数据已过期（超过 15 分钟未更新）")
    ok, msg = run_check(server, routes, 17, profile=profile)
    assert ok is expected
    if not expected:
        assert "过期" in msg


@pytest.mark.parametrize("sampled", [123, "bad", "2026-09-07", "2026-99-99T00:00:00Z"])
def test_caddy_metrics_rejects_invalid_sample_time(server, sampled):
    routes = good_routes()
    routes["/api/caddy/entry"][2]["metrics_sampled_at"] = sampled
    assert run_check(server, routes, 17)[0] is False


def test_caddy_metrics_legacy_response_remains_compatible(server):
    assert "metrics_sampled_at" not in good_routes()["/api/caddy/entry"][2]
    assert run_check(server, good_routes(), 17, profile="nas")[0] is True


def test_caddy_metrics_degraded_is_not_a_failure(server):
    """cron 未就绪 / 指标文件缺失是合法降级，与 7/9/15 同一套哲学。"""
    routes = good_routes()
    payload = json.loads(json.dumps(routes["/api/caddy/entry"][2]))
    payload["metrics_available"] = False
    payload["metrics_error"] = "Caddy 指标文件不存在（cron 未就绪或挂载缺失）"
    payload["hosts"] = []
    payload["counting_since"] = None
    routes["/api/caddy/entry"] = (200, JSON, payload)
    ok, msg = run_check(server, routes, 17)
    assert ok is True
    assert "降级态" in msg


def test_caddy_metrics_rejects_extra_host_field(server):
    routes = good_routes()
    payload = json.loads(json.dumps(routes["/api/caddy/entry"][2]))
    payload["hosts"][0]["upstream_address"] = "100.64.0.70:8090"
    routes["/api/caddy/entry"] = (200, JSON, payload)
    ok, msg = run_check(server, routes, 17)
    assert ok is False
    assert "多出 upstream_address" in msg


def test_caddy_metrics_rejects_bare_ip_host(server):
    """后端 _safe_host() 应把裸 IP 折叠成 _other；这条守的是那层折叠没失效。"""
    routes = good_routes()
    payload = json.loads(json.dumps(routes["/api/caddy/entry"][2]))
    payload["hosts"][0]["host"] = "100.64.0.10"
    routes["/api/caddy/entry"] = (200, JSON, payload)
    ok, msg = run_check(server, routes, 17)
    assert ok is False
    assert "裸 IP" in msg


def test_caddy_metrics_survives_upstream_half_being_degraded(server):
    r"""两条链各自降级：upstreams 塌了，17 仍必须把 hosts 契约验完。

    这正是 17 不并进 9 的理由，而且有**两层**：
    1. 9 在 `available=false` 时提前 return，`hosts` 根本走不到；
    2. 9 的全文扫描用的是 `TAILSCALE_IP_RE`（`100.\d+\.\d+\.\d+` 前缀），**只认
       100 段**——用例里故意放一个 `192.168.*` 的 LAN 地址，9 从头到尾都发现不了，
       只有 17 的字段级通用 IPv4 正则能抓住。
    """
    routes = good_routes()
    payload = json.loads(json.dumps(routes["/api/caddy/entry"][2]))
    payload["available"] = False
    payload["error"] = "Caddy 导出文件不存在（cron 未就绪或挂载缺失）"
    payload["upstreams"] = []
    payload["hosts"][0]["host"] = "192.168.1.9"       # 契约破坏藏在另一半里
    routes["/api/caddy/entry"] = (200, JSON, payload)
    assert run_check(server, routes, 9)[0] is True    # 9 既漏 LAN 段又已提前返回
    ok, msg = run_check(server, routes, 17)           # 17 仍抓到
    assert ok is False
    assert "裸 IP" in msg


# ---------------------------------------------------------------- 18 personal ledger

def test_ledger_empty_database_is_healthy(server):
    assert run_check(server, good_routes(), 18)[0] is True


@pytest.mark.parametrize('path', ['/api/ledger/overview', '/api/ledger/tx?size=1', '/api/ledger/dict'])
def test_ledger_rejects_beszel_html_fallback(server, path):
    routes = good_routes()
    routes[path] = (200, HTML, BESZEL_PAGE)
    assert run_check(server, routes, 18)[0] is False


def test_ledger_rejects_bad_money_and_does_not_log_private_rows(server):
    routes = good_routes()
    routes['/api/ledger/overview'][2]['month_expense'] = 'private merchant =123'
    ok, message = run_check(server, routes, 18)
    assert ok is False
    assert 'private merchant' not in message


def test_ledger_unknown_route_stays_in_console(server):
    routes = good_routes()
    routes['/api/ledger/unknown'] = (200, HTML, BESZEL_PAGE)
    assert run_check(server, routes, 18)[0] is False


# ---------------------------------------------------------------- 19 存储引擎


def test_storage_engine_postgres_with_probe_is_healthy(server):
    assert run_check(server, good_routes(), 19)[0] is True


def test_storage_engine_sqlite_is_rejected_after_cutover(server):
    """割接后 `sqlite` 是 FAIL，不是「过渡期合法」（PG-05 收口决策，design §4.3）。

    实测依据：`sqlite` 下账本/续费域无条件走 `db.pg_transaction()` → 直接不可用，
    旧语义会把「半可用实例」验收成通过。割接前验收请显式 `--skip 19`。
    """
    routes = good_routes()
    routes["/api/config/effective"][2]["storage_engine"] = "sqlite"
    routes["/api/config/effective"][2]["database"] = None
    ok, msg = run_check(server, routes, 19)
    assert ok is False
    assert "--skip 19" in msg and "D3" in msg


@pytest.mark.parametrize("database,reason", [
    (None, "探针对象"),
    ({"engine": "postgres", "reachable": False}, "reachable"),
    ({"engine": "postgres", "reachable": True, "password": "x"}, "敏感键"),
])
def test_storage_engine_probe_contract(server, database, reason):
    routes = good_routes()
    routes["/api/config/effective"][2]["database"] = database
    ok, msg = run_check(server, routes, 19)
    assert ok is False
    assert reason in msg
