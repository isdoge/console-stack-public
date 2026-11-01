# -*- coding: utf-8 -*-
"""entry_view（C5 → L4 入口维度）：分层投影、零探测、内网地址不外泄。"""
import json
import pathlib
import sys
import textwrap

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import app as console_app  # noqa: E402
import caddy_view as cv  # noqa: E402
import entry_view as ev  # noqa: E402


def _snapshot(names, available=True):
    return {
        "available": available,
        "sampled_at": "2026-09-07T00:00:00Z",
        "error": None,
        "upstreams": [
            {"id": f"upstream-{i + 1}", "name": name, "num_requests": 0, "fails": 0}
            for i, name in enumerate(names)
        ],
    }


FULL_SVC = {
    "name": "sublinkpro",
    "access": {
        "home": "http://192.168.50.100:8015",
        "tailscale": "http://100.64.0.10:8015",
        "domain": "https://sub.example.test",
    },
}


# ---- 分层投影 ------------------------------------------------------------


def test_full_chain_domain_edge_tailscale_home():
    names = ev.caddy_upstream_names(_snapshot(["sublinkpro"]))
    entry = ev.entry_projection(FULL_SVC, names)
    assert entry["domain"] == "https://sub.example.test"
    assert entry["edge"] == "caddy"
    assert entry["edge_known"] is True
    assert entry["tailscale"] is True
    assert entry["home"] is True
    assert entry["layers"] == ["domain", "edge", "tailscale", "home"]


def test_service_not_behind_caddy_has_no_edge_layer():
    names = ev.caddy_upstream_names(_snapshot(["别的服务"]))
    entry = ev.entry_projection(FULL_SVC, names)
    assert entry["edge"] is None
    assert entry["edge_known"] is True  # 数据源在，只是这条不在名单里
    assert "edge" not in entry["layers"]


def test_snapshot_unavailable_marks_edge_unknown_not_absent():
    """cron 未就绪时 edge 是**未知**，不能假装成「没有边缘」。"""
    entry = ev.entry_projection(FULL_SVC, ev.caddy_upstream_names(_snapshot([], available=False)))
    assert entry["edge"] is None
    assert entry["edge_known"] is False
    assert entry["layers"] == ["domain", "tailscale", "home"]


@pytest.mark.parametrize("snapshot", [None, {}, {"available": True}, {"available": True, "upstreams": "x"}])
def test_caddy_upstream_names_degrades_to_unknown(snapshot):
    assert ev.caddy_upstream_names(snapshot) is None


def test_internal_only_service():
    entry = ev.entry_projection(
        {"name": "internal", "access": {"tailscale": "http://100.1.2.3:9000"}}, set()
    )
    assert entry["domain"] is None
    assert entry["layers"] == ["tailscale"]


@pytest.mark.parametrize("svc", [{}, {"name": "x"}, {"name": "x", "access": None}, {"name": "x", "access": {"domain": "  "}}])
def test_no_access_yields_empty_layers(svc):
    entry = ev.entry_projection(svc, set())
    assert entry["layers"] == []
    assert entry["domain"] is None
    assert entry["tailscale"] is False
    assert entry["home"] is False


def test_projection_never_leaks_internal_addresses():
    """entry 只回公网域名与布尔位——tailnet / 家庭内网地址一概不进这个字段。"""
    raw = json.dumps(ev.entry_projection(FULL_SVC, {"sublinkpro"}), ensure_ascii=False)
    assert "100.64.0.10" not in raw
    assert "192.168.50.100" not in raw


def test_attach_entries_is_in_place_and_tolerates_junk():
    services = [dict(FULL_SVC), "不是字典", {"name": "另一个"}]
    ev.attach_entries(services, _snapshot(["sublinkpro"]))
    assert services[0]["entry"]["edge"] == "caddy"
    assert services[1] == "不是字典"
    assert services[2]["entry"]["layers"] == []


# ---- 路由级 -------------------------------------------------------------


@pytest.fixture()
def services_client(tmp_path, monkeypatch):
    assets_path = tmp_path / "assets.yaml"
    assets_path.write_text(
        textwrap.dedent(
            """
            - name: secure-web
              type: external
              project:
                repo: https://example.test/sample
                links:
                - label: 资料
                  href: https://example.test/sample/docs
              access:
                domain: https://secure.example.test
                tailscale: http://100.9.9.9:8080
                probe:
                  domain: true
            - name: 只在内网
              type: external
              access:
                home: http://192.168.1.2:80
            """
        ),
        encoding="utf-8",
    )
    # PG-02：/api/services 会触发采集，client 角色下被转交/拒绝；
    # 本文件测的是本机 entry 投影，按 authority 运行。
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authority")
    monkeypatch.setattr(console_app, "ASSETS_PATH", str(assets_path))
    monkeypatch.setattr(console_app, "DB_PATH", str(tmp_path / "console.db"))
    console_app.init_db()
    monkeypatch.setattr(console_app.sm, "get_all_status", lambda *a, **k: {})
    monkeypatch.setattr(console_app.sm, "service_running", lambda *a, **k: "N/A")
    monkeypatch.setattr(console_app.cr, "is_caddy_service", lambda svc: False)
    monkeypatch.setattr(console_app.pc, "check_service_health", lambda svc, force=False: ("Unknown", ""))
    monkeypatch.setattr(console_app.pc, "peek_health_detail", lambda name: {})
    return console_app.app.test_client()


def test_services_endpoint_attaches_entry(services_client, monkeypatch):
    monkeypatch.setattr(cv, "get_caddy_snapshot", lambda force=False: _snapshot(["secure-web"]))
    rows = services_client.get("/api/services").get_json()
    by_name = {row["name"]: row for row in rows}
    assert by_name["secure-web"]["entry"]["layers"] == ["domain", "edge", "tailscale"]
    assert by_name["secure-web"]["project"] == {
        "repo": "https://example.test/sample",
        "links": [{"label": "资料", "href": "https://example.test/sample/docs"}],
    }
    assert by_name["只在内网"]["entry"]["layers"] == ["home"]


def test_services_endpoint_survives_caddy_snapshot_outage(services_client, monkeypatch):
    """C11 快照挂掉只让 edge 变「未知」，服务列表照常返回（零新探测、不降级主链路）。"""
    monkeypatch.setattr(
        cv, "get_caddy_snapshot", lambda force=False: {"available": False, "upstreams": []}
    )
    rows = services_client.get("/api/services").get_json()
    entry = rows[0]["entry"]
    assert entry["edge_known"] is False
    assert entry["domain"] == "https://secure.example.test"


def test_entry_projection_triggers_no_probe(services_client, monkeypatch):
    """入口维度**不发任何探测**：健康探测与 Caddy 路由采集的调用次数都不因它增加。

    这条守的是 health_probe:false / openviking 压制纪律——压制的是探测，
    entry 一次探测都不做，自然破不了它。
    """
    probes = []
    monkeypatch.setattr(
        console_app.pc,
        "check_service_health",
        lambda svc, force=False: probes.append(svc.get("name")) or ("Unknown", ""),
    )
    routes = []
    monkeypatch.setattr(
        console_app.cr,
        "get_service_routes",
        lambda svc, force=False: routes.append(svc.get("name")) or ([], None),
    )
    monkeypatch.setattr(cv, "get_caddy_snapshot", lambda force=False: _snapshot(["secure-web"]))

    services_client.get("/api/services")

    # 健康探测仍是每服务一次（既有行为），Caddy 路由采集一次都不会因 entry 被触发
    assert probes == ["secure-web", "只在内网"]
    assert routes == []
