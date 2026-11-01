"""升级动作完成后，版本表和服务列表必须一起更新；不执行真实升级或网络请求。"""
from unittest.mock import Mock

import pytest
import yaml

import app as console_app
from live_snapshot import SnapshotCache


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "sqlite")
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "test")
    monkeypatch.setenv("CONSOLE_AUTH_ENABLED", "false")
    monkeypatch.setenv("CONSOLE_MAINTENANCE_MODE", "normal")
    monkeypatch.setattr(console_app, "DB_PATH", str(tmp_path / "console.db"))
    console_app.init_db()
    svc = {
        "name": "magpie", "machine": "us", "type": "docker",
        "container": "magpie", "actions": ["upgrade"],
        "compose_dir": "/opt/magpie",
        "version": {"enabled": True, "repo_url": "https://github.com/yetone/magpie"},
    }
    assets = tmp_path / "assets.yaml"
    assets.write_text(yaml.safe_dump([svc]), encoding="utf-8")
    monkeypatch.setattr(console_app, "ASSETS_PATH", str(assets))
    monkeypatch.setattr(console_app, "_services_page_cache", SnapshotCache(ttl=600))
    monkeypatch.setattr(console_app.sm, "get_all_status", lambda *a, **k: {})
    monkeypatch.setattr(console_app.sm, "service_running", lambda *a: "running")
    monkeypatch.setattr(console_app.pc, "check_service_health", lambda *a, **k: ("Healthy", ""))
    monkeypatch.setattr(console_app.pc, "peek_health_detail", lambda *a: {})
    monkeypatch.setattr(console_app, "_service_health_history", lambda *a: [])
    monkeypatch.setattr(console_app.cv, "get_caddy_snapshot", lambda: {})
    monkeypatch.setattr(console_app.entry_view, "attach_entries", lambda *a: None)
    monkeypatch.setattr(console_app.vs.collect_guard, "collection_allowed", lambda: True)
    actual = {"version": "v0.1.1311"}
    monkeypatch.setattr(console_app.vs, "get_local_service_version", lambda svc: actual["version"])
    monkeypatch.setattr(
        console_app.vs, "get_upstream_release", lambda repo: ("v0.1.1326", "", [])
    )
    monkeypatch.setattr(console_app.vs, "_gh_api", lambda *a: [])
    console_app.vs.sync_service_version(svc, db_path=console_app.DB_PATH)
    actual["version"] = "v0.1.1326"
    monkeypatch.setattr(console_app.sm, "do_action", Mock(return_value=(True, "upgraded")))
    return console_app.app.test_client()


def test_upgrade_updates_versions_before_returning_and_invalidates_list(client):
    assert client.get("/api/services").get_json()[0]["version_info"]["local"] == "v0.1.1311"

    response = client.post("/api/service/action", json={"name": "magpie", "action": "upgrade"})
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "output": "upgraded"}
    # 普通读取也应更新，不靠 force=1 绕过旧快照。
    version = client.get("/api/services").get_json()[0]["version_info"]
    assert version["local"] == version["upstream"] == "v0.1.1326"
    assert version["status"] == "最新"
    assert version["behind"] == 0
    console_app.sm.do_action.assert_called_once()


@pytest.mark.parametrize("action,ok", [
    ("upgrade", False), ("start", True), ("stop", True), ("restart", True), ("logs", True),
])
def test_failed_upgrade_and_other_actions_do_not_sync_version(client, monkeypatch, action, ok):
    console_app.sm.do_action.return_value = (ok, "action output")
    sync = Mock(side_effect=AssertionError("must not sync"))
    monkeypatch.setattr(console_app.vs, "sync_service_version", sync)
    result = client.post("/api/service/action", json={"name": "magpie", "action": action})
    assert result.get_json() == {"ok": ok, "output": "action output"}
    sync.assert_not_called()


def test_version_sync_failure_keeps_upgrade_success_and_exposes_safe_warning(client, monkeypatch, caplog):
    client.get("/api/services")
    invalidate = Mock(wraps=console_app._services_page_cache.invalidate)
    monkeypatch.setattr(console_app._services_page_cache, "invalidate", invalidate)
    monkeypatch.setattr(
        console_app.vs, "sync_service_version", Mock(side_effect=RuntimeError("secret credentials"))
    )
    response = client.post("/api/service/action", json={"name": "magpie", "action": "upgrade"})
    result = response.get_json()
    assert response.status_code == 200
    assert result["ok"] is True
    assert result["output"] == "upgraded"
    assert result["warning"] == "升级成功，但版本刷新失败，请在版本详情中重试刷新"
    assert "secret credentials" not in response.get_data(as_text=True) + caplog.text
    invalidate.assert_called_once()


@pytest.mark.parametrize("action,timeout", [("upgrade", 420), ("restart", 60)])
def test_client_forwarding_allows_upgrade_and_version_sync_time(monkeypatch, action, timeout):
    request_authority = Mock(return_value="forwarded")
    monkeypatch.setattr(console_app, "_request_authority", request_authority)
    with console_app.app.test_request_context(
        "/api/service/action", method="POST", json={"name": "magpie", "action": action},
    ):
        assert console_app._proxy_authority("http://nas.example.test:8081", "api/service/action") == "forwarded"
    assert request_authority.call_args.kwargs["timeout"] == timeout
