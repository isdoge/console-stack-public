"""HTTP contract for the service page snapshot."""
import threading

import pytest

import app as console_app
from live_snapshot import SnapshotCache


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authority")
    monkeypatch.setenv("CONSOLE_AUTH_ENABLED", "false")
    monkeypatch.setattr(console_app, "_services_page_cache", SnapshotCache(ttl=0))
    monkeypatch.setattr(console_app, "DB_PATH", str(tmp_path / "console.db"))
    assets = tmp_path / "assets.yaml"
    assets.write_text("first", encoding="utf-8")
    monkeypatch.setattr(console_app, "ASSETS_PATH", str(assets))
    return console_app.app.test_client(), assets




def test_service_file_change_invalidates_old_rows(client, monkeypatch):
    http, assets = client
    calls = []

    def build(force=False):
        calls.append(force)
        return [{"name": assets.read_text(encoding="utf-8")}]

    monkeypatch.setattr(console_app, "_build_services", build)
    first = http.get("/api/services")
    assert first.get_json() == [{"name": "first"}]
    assert first.headers["X-Snapshot-At"]

    assets.write_text("second row", encoding="utf-8")
    second = http.get("/api/services")
    assert second.get_json() == [{"name": "second row"}]
    assert second.headers["X-Snapshot-Refreshing"] == "0"
    assert calls == [False, False]


def test_failed_background_refresh_keeps_last_good_and_sets_error(client, monkeypatch):
    http, _ = client
    failed = threading.Event()
    calls = 0

    def build(force=False):
        nonlocal calls
        calls += 1
        if calls > 1:
            failed.set()
            raise RuntimeError("probe down")
        return [{"name": "last-good"}]

    monkeypatch.setattr(console_app, "_build_services", build)
    assert http.get("/api/services").get_json() == [{"name": "last-good"}]
    stale = http.get("/api/services")
    assert stale.get_json() == [{"name": "last-good"}]
    assert failed.wait(1)
    console_app._services_page_cache.wait_for_idle(1)
    retained = http.get("/api/services")
    assert retained.get_json() == [{"name": "last-good"}]
    assert retained.headers["X-Snapshot-Error"] == "1"
    assert calls == 2
