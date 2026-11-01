# -*- coding: utf-8 -*-
"""台账写操作审计回归：服务增删改、面板整表替换写 oplog。

背景：renewals 与服务动作早已记审计，但服务/机器/面板的配置写操作此前不入审计页；
本轮补齐（action 命名沿用 renewal_* 的 snake_case 惯例，host 为模块名）。
不触网。
"""
import os
import pathlib
import sys
import textwrap

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ["CONSOLE_AUTH_ENABLED"] = "false"

import app as console_app  # noqa: E402
import machine_config as mc  # noqa: E402
import probe_cache as pc  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_caches():
    mc.invalidate()
    pc.invalidate()
    yield
    mc.invalidate()
    pc.invalidate()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(console_app, "DB_PATH", str(tmp_path / "console.db"))
    monkeypatch.setattr(console_app, "ASSETS_PATH", str(tmp_path / "assets.yaml"))
    monkeypatch.setattr(console_app, "PANELS_PATH", str(tmp_path / "panels.yaml"))
    console_app.init_db()
    monkeypatch.setattr(pc, "check_tcp", lambda host, port, timeout=3: (True, "Port Open"))
    return console_app.app.test_client()


def _write_assets(monkeypatch, tmp_path, body="- name: demo\n  machine: txy\n  type: process\n"):
    path = tmp_path / "assets.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    monkeypatch.setattr(console_app, "ASSETS_PATH", str(path))
    return path


def _oplogs(client):
    rows = client.get("/api/oplog").get_json()
    rows = rows if isinstance(rows, list) else rows.get("data", [])
    return [(r["host"], r["svc"], r["action"]) for r in rows]


class TestServiceWriteAudit:
    def test_create_writes_oplog(self, client, tmp_path, monkeypatch):
        _write_assets(monkeypatch, tmp_path, "")
        resp = client.post(
            "/api/service",
            json={"name": "demo", "machine": "us", "type": "docker", "container": "demo", "port": 80},
        )
        assert resp.status_code == 200
        assert ("services", "demo", "service_create") in _oplogs(client)

    def test_update_writes_oplog_with_update_action(self, client, tmp_path, monkeypatch):
        _write_assets(
            monkeypatch, tmp_path, "- name: demo\n  machine: txy\n  type: process\n  port: 80\n"
        )
        resp = client.put(
            "/api/service",
            json={"_original_name": "demo", "name": "demo", "machine": "us",
                  "type": "docker", "container": "demo", "port": 81},
        )
        assert resp.status_code == 200
        assert ("services", "demo", "service_update") in _oplogs(client)

    def test_duplicate_create_rejected_and_not_audited(self, client, tmp_path, monkeypatch):
        _write_assets(
            monkeypatch, tmp_path, "- name: demo\n  machine: txy\n  type: process\n"
        )
        before = _oplogs(client)
        resp = client.post(
            "/api/service",
            json={"name": "demo", "machine": "us", "type": "docker", "container": "demo"},
        )
        assert resp.status_code == 400
        assert _oplogs(client) == before

    def test_delete_writes_oplog(self, client, tmp_path, monkeypatch):
        _write_assets(
            monkeypatch, tmp_path, "- name: demo\n  machine: txy\n  type: process\n"
        )
        resp = client.delete("/api/service/demo")
        assert resp.status_code == 200
        assert ("services", "demo", "service_delete") in _oplogs(client)


class TestPanelsReplaceAudit:
    def test_replace_writes_oplog(self, client):
        resp = client.put(
            "/api/panels",
            json=[{"name": "p1", "url": "http://127.0.0.1:1", "machine": "nas", "enabled": True}],
        )
        assert resp.status_code == 200
        assert ("panels", "panels", "panels_replace") in _oplogs(client)


class TestOplogQueryParams:
    def test_limit_must_be_int(self, client):
        assert client.get("/api/oplog?limit=abc").status_code == 400

    def test_limit_clamped(self, client):
        assert client.get("/api/oplog?limit=99999").status_code == 200

    def test_q_filters_host_svc_action(self, client):
        resp = client.get("/api/oplog?q=restart")
        assert resp.status_code == 200
