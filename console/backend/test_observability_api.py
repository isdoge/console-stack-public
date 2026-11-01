# -*- coding: utf-8 -*-
"""Regression tests for Console observability and trusted SSL API fields."""
import os
import pathlib
import sys
import textwrap

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ["CONSOLE_AUTH_ENABLED"] = "false"

import app as console_app  # noqa: E402


@pytest.fixture(autouse=True)
def _authority(monkeypatch):
    """观测路由受采集门禁约束（Q1）：本文件测的是本机采集/投影，按 authority 运行。

    client 角色的转交行为在 `test_pg_app_wiring.py` 里单独断言。
    """
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authority")


@pytest.fixture()
def client():
    return console_app.app.test_client()


@pytest.fixture()
def services_client(tmp_path, monkeypatch):
    assets_path = tmp_path / "assets.yaml"
    assets_path.write_text(
        textwrap.dedent(
            """
            - name: secure-web
              type: external
              access:
                domain: https://secure.example.test
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(console_app, "ASSETS_PATH", str(assets_path))
    monkeypatch.setattr(console_app, "DB_PATH", str(tmp_path / "console.db"))
    console_app.init_db()
    monkeypatch.setattr(console_app.sm, "get_all_status", lambda *args, **kwargs: {})
    monkeypatch.setattr(console_app.sm, "service_running", lambda *args, **kwargs: "N/A")
    monkeypatch.setattr(console_app.cr, "is_caddy_service", lambda svc: False)
    return console_app.app.test_client()


class TestObservabilityEndpoint:
    def test_returns_bare_console_json_without_beszel_proxy(self, client, monkeypatch):
        payload = {
            "ok": True,
            "updated_at": "2026-08-29T19:20:00Z",
            "probe_interval": 600,
            "source_errors": [],
            "machine_links": {},
            "active_alerts": [],
            "recent_events": [],
            "systemd_services": [],
            "smart_devices": [],
        }
        monkeypatch.setattr(
            console_app.mb,
            "observability_snapshot",
            lambda: payload,
        )
        monkeypatch.setattr(
            console_app,
            "_proxy_authority",
            lambda *args, **kwargs: pytest.fail(
                "/api/observability must not be handled by the Beszel proxy"
            ),
        )

        response = client.get("/api/observability")

        assert response.status_code == 200
        assert response.is_json
        assert response.get_json() == payload
        assert "data" not in response.get_json()
        assert "success" not in response.get_json()

    def test_pg_snapshot_does_not_read_hub_cache(self, client, monkeypatch):
        monkeypatch.setattr(
            console_app.mb,
            "observability_snapshot",
            lambda: {"ok": True},
        )

        response = client.get("/api/observability?force=1")

        assert response.status_code == 200
        assert response.get_json() == {"ok": True}


class TestServicesSslFields:
    def test_trusted_ssl_days_are_returned(self, services_client, monkeypatch):
        monkeypatch.setattr(
            console_app.pc,
            "check_service_health",
            lambda svc, force=False: ("Healthy", "HTTP 200"),
        )
        monkeypatch.setattr(
            console_app.pc,
            "peek_health_detail",
            lambda name: {
                "status": "Healthy",
                "msg": "HTTP 200",
                "ssl_ok": True,
                "ssl_days": 42,
            },
        )

        health = services_client.get("/api/services").get_json()[0]["health"]

        assert health == {
            "status": "Healthy",
            "msg": "HTTP 200",
            "ssl_ok": True,
            "ssl_days": 42,
        }

    @pytest.mark.parametrize(
        "detail",
        [
            {"ssl_ok": False, "ssl_days": "certificate failed"},
            {"ssl_ok": True, "ssl_days": "42"},
            {"ssl_ok": True, "ssl_days": float("inf")},
            {"ssl_ok": True, "ssl_days": True},
        ],
    )
    def test_untrusted_ssl_values_are_omitted(
        self, services_client, monkeypatch, detail
    ):
        monkeypatch.setattr(
            console_app.pc,
            "check_service_health",
            lambda svc, force=False: ("Unhealthy", "certificate failed"),
        )
        monkeypatch.setattr(
            console_app.pc,
            "peek_health_detail",
            lambda name: detail,
        )

        health = services_client.get("/api/services").get_json()[0]["health"]

        assert health == {"status": "Unhealthy", "msg": "certificate failed"}
