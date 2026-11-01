"""Postcutover client authority routing and raw HTTP money regressions."""
import os
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest

import app as console_app
import money
from plugin_imports import load_module  # noqa: E402

plugin_renewals = load_module("plugins.renewals.backend.renewals")
plugin_ledger = load_module("plugins.ledger.backend.ledger")



@pytest.fixture
def client_mode(monkeypatch, tmp_path):
    # Forwarding regressions can reach real local writers before assertions fail.
    # Keep every YAML writer (and its audit DB) disposable even on the first red run.
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setattr(console_app, "PANELS_PATH", str(tmp_path / "panels.yaml"))
    monkeypatch.setattr(console_app, "ASSETS_PATH", str(tmp_path / "assets.yaml"))
    monkeypatch.setattr(console_app, "DB_PATH", str(tmp_path / "console.db"))
    # PG ignores DB_PATH; accidental local audit must never open an inherited DSN.
    def forbid_pg(*args, **kwargs):
        pytest.fail("client fixture forbids PostgreSQL connections")
    monkeypatch.setattr(console_app.dbmod, "pg_open", forbid_pg)
    monkeypatch.setattr(console_app.dbmod, "pg_transaction", forbid_pg)
    monkeypatch.setenv("CONSOLE_MACHINES_PATH", str(tmp_path / "machines.yaml"))
    monkeypatch.setenv("CONSOLE_SETTINGS_FILE", str(tmp_path / "settings.yaml"))
    monkeypatch.setattr(console_app.mc, "_cache", {"path": None, "mtime": None, "machines": None})
    monkeypatch.setattr(console_app.settings_store, "_injected_keys", set())
    for name in ("panels.yaml", "assets.yaml", "machines.yaml"):
        (tmp_path / name).write_text("[]\n", encoding="utf-8")
    (tmp_path / "settings.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "client")
    monkeypatch.setenv("CONSOLE_MAINTENANCE_MODE", "normal")
    monkeypatch.setenv("CONSOLE_AUTH_ENABLED", "false")
    monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", "http://nas.example.test:8081")
    monkeypatch.delenv("CONSOLE_PUBLIC_URL", raising=False)
    return console_app.app.test_client()


@pytest.mark.parametrize("origin", ["", "file:///tmp", "http://user:secret@nas:8081", "http://nas:8081/api", "http://nas:bad", "http://nas?target=elsewhere"])
def test_client_startup_rejects_bad_authority_before_connecting(client_mode, monkeypatch, origin):
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", origin)
    monkeypatch.setattr(console_app.dbmod, "pg_open", lambda **kw: pytest.fail("invalid client config must fail before PG"))
    with pytest.raises(SystemExit) as failure:
        console_app._startup()
    assert failure.value.code == 4


def test_client_startup_rejects_own_public_origin(client_mode, monkeypatch):
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    monkeypatch.setenv("CONSOLE_PUBLIC_URL", "http://mac.example.test:8081")
    monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", "http://mac.example.test:8081/")
    monkeypatch.setattr(console_app.dbmod, "pg_open", lambda **kw: pytest.fail("self origin must fail before PG"))
    with pytest.raises(SystemExit) as failure:
        console_app._startup()
    assert failure.value.code == 4


def test_client_health_refuses_missing_authority(client_mode, monkeypatch):
    monkeypatch.delenv("CONSOLE_AUTHORITY_ORIGIN")
    response = client_mode.get("/api/health")
    assert response.status_code == 503
    assert "CONSOLE_AUTHORITY_ORIGIN" in response.get_json()["error"]


FORWARDED = [
    ("GET", "/api/config"),
    ("GET", "/api/config/settings"),
    ("PUT", "/api/config/settings"),
    ("GET", "/api/machines"),
    ("POST", "/api/machines"),
    ("GET", "/api/machines/nas?full=1"),
    ("PUT", "/api/machines/nas"),
    ("DELETE", "/api/machines/nas"),
    ("GET", "/api/machines/nas/discover"),
    ("GET", "/api/services"),
    ("POST", "/api/service"),
    ("PUT", "/api/service"),
    ("DELETE", "/api/service/example"),
    ("POST", "/api/services/priority"),
    ("GET", "/api/panels"),
    ("POST", "/api/panels"),
    ("PUT", "/api/panels"),
    ("PUT", "/api/panels/1"),
    ("DELETE", "/api/panels/1"),
    ("GET", "/api/tailnet"),
    ("GET", "/api/caddy/entry"),
    ("GET", "/api/capacity?force=1"),
    ("GET", "/api/attention"),
    ("GET", "/api/monitor"),
    ("GET", "/api/observability"),
    ("GET", "/api/feed"),
    ("GET", "/api/feed/sources"),
    ("GET", "/api/service/example/version?sync=1"),
]


@pytest.mark.parametrize("method,path", FORWARDED)
def test_client_authority_routes_do_not_execute_local_handlers(client_mode, monkeypatch, method, path):
    seen = []

    def proxy(origin, subpath, **kwargs):
        seen.append((origin, subpath, console_app.request.method, console_app.request.get_data()))
        return console_app.jsonify({"remote": True}), 202

    monkeypatch.setattr(console_app, "_proxy_authority", proxy)
    monkeypatch.setattr(console_app, "_proxy_authority", proxy)
    with console_app.app.test_request_context(path, method=method):
        endpoint = console_app.request.endpoint
    monkeypatch.setitem(console_app.app.view_functions, endpoint, lambda **kw: pytest.fail("client executed local authority handler"))
    monkeypatch.setattr(console_app, "_log_op", lambda *a, **kw: pytest.fail("client audited forwarded write"))
    response = client_mode.open(path, method=method, data=b'{"intent":"unchanged"}', content_type="application/json")
    assert response.status_code == 202
    assert response.get_json() == {"remote": True}
    assert seen == [("http://nas.example.test:8081", path.split("?")[0].lstrip("/"), method, b'{"intent":"unchanged"}')]


@pytest.mark.parametrize("method,path", [
    ("PUT", "/api/services/priority"),
    ("POST", "/api/machines/nas/discover"),
    ("PUT", "/api/machines/nas/unknown"),
    ("DELETE", "/api/panels/1/scratch"),
    ("POST", "/api/renewals"),
    ("POST", "/api/bookmarks"),
    ("PUT", "/api/dashboard-layout"),
    ("GET", "/api/events"),
    ("GET", "/api/config/effective"),
    ("GET", "/api/panels/1/scratch"),
])
def test_forwarding_allowlist_is_method_and_matched_route(client_mode, monkeypatch, method, path):
    monkeypatch.setattr(console_app, "_proxy_authority", lambda *a, **kw: pytest.fail("unlisted route forwarded"))
    monkeypatch.setattr(console_app, "_proxy_authority", lambda *a, **kw: pytest.fail("unlisted route forwarded"))
    with console_app.app.test_request_context(path, method=method):
        assert console_app.forward_observation_to_authority() is None


@pytest.mark.parametrize("path,method", [("/api/renewals", "POST"), ("/api/renewals/r1", "PUT")])
def test_http_renewal_cost_rejects_precision_loss_before_domain_write(monkeypatch, path, method):
    seen = []

    def validate(data, **kwargs):
        seen.append(data["cost"])
        return {"name": "test", "cost": money.json_number(plugin_renewals._normalize_cost(data["cost"]))}

    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "test")
    monkeypatch.setattr(plugin_renewals, "create_renewal", validate)
    monkeypatch.setattr(plugin_renewals, "update_renewal", lambda rid, data, **kw: validate(data, **kw))
    monkeypatch.setattr(console_app, "_log_op", lambda *a, **kw: None)
    response = console_app.app.test_client().open(path, method=method, data='{"cost":0.1234567890123456789}', content_type="application/json")
    assert response.status_code == 400
    assert "JSON number" in response.get_json()["error"]
    assert seen == [Decimal("0.1234567890123456789")]


def test_http_ledger_manual_preserves_original_decimal_token(monkeypatch):
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "test")
    monkeypatch.setenv("CONSOLE_MAINTENANCE_MODE", "normal")
    seen = []

    def manual(data, db_path):
        seen.append(data["amount"])
        return {"ok": 1}

    monkeypatch.setattr(plugin_ledger, "manual", manual)
    monkeypatch.setattr(console_app, "_log_op", lambda *a, **kw: None)
    response = console_app.app.test_client().post("/api/ledger/tx/manual", data='{"amount":1.0050000000000000001}', content_type="application/json")
    assert response.status_code == 200
    assert seen == [Decimal("1.0050000000000000001")]


def test_authority_proxy_strips_credentials_and_preserves_body_status_cache(client_mode, monkeypatch):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_PUT(self):  # noqa: N802
            seen.append((self.path, dict(self.headers), self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Set-Cookie", "internal=never-forward")
            self.end_headers()
            self.wfile.write('{"error":"权威端拒绝"}'.encode())

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", f"http://127.0.0.1:{server.server_port}")
        try:
            client_mode.set_cookie("session", "never-forward")
            response = client_mode.put("/api/config/settings?probe=1", data=b'{"values":{}}', content_type="application/json",
                                       headers={"Authorization": "Bearer never-forward", "X-Internal": "never-forward"})
            assert response.status_code == 409
            assert response.get_json() == {"error": "权威端拒绝"}
            assert response.headers["Cache-Control"] == "no-store"
            assert "Set-Cookie" not in response.headers
            path, headers, body = seen[0]
            assert path == "/api/config/settings?probe=1"
            assert body == b'{"values":{}}'
            assert not ({"authorization", "cookie", "x-internal"} & {key.lower() for key in headers})
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_authority_proxy_preserves_page_snapshot_metadata(client_mode, monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Snapshot-At", "2026-09-29T01:00:00Z")
            self.send_header("X-Snapshot-Refreshing", "1")
            self.send_header("X-Snapshot-Error", "0")
            self.send_header("Set-Cookie", "internal=never-forward")
            self.end_headers()
            self.wfile.write(b'[]')

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", f"http://127.0.0.1:{server.server_port}")
        try:
            response = client_mode.get("/api/services")
            assert response.status_code == 200
            assert response.get_json() == []
            assert response.headers["X-Snapshot-At"] == "2026-09-29T01:00:00Z"
            assert response.headers["X-Snapshot-Refreshing"] == "1"
            assert response.headers["X-Snapshot-Error"] == "0"
            assert "Set-Cookie" not in response.headers
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_unreachable_authority_does_not_write_local_yaml(client_mode, tmp_path, monkeypatch):
    local = tmp_path / "panels.yaml"
    local.write_text("[]\n")
    monkeypatch.setattr(console_app, "PANELS_PATH", str(local))
    monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", "http://127.0.0.1:1")
    monkeypatch.setattr(console_app, "_log_op", lambda *a, **kw: pytest.fail("failure fell back to local audit"))
    response = client_mode.put("/api/panels", json=[])
    assert response.status_code == 503
    assert local.read_text() == "[]\n"


def test_forwarded_request_cannot_recurse_through_another_client(client_mode, monkeypatch):
    monkeypatch.setattr(console_app, "_proxy_authority", lambda *a, **kw: pytest.fail("second proxy hop"))
    response = client_mode.get("/api/panels", headers={"X-Console-Authority-Hop": "1"})
    assert response.status_code == 503
    assert "递归" in response.get_json()["error"]


def test_incomplete_authority_response_is_structured_unavailable(client_mode, monkeypatch):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_PUT(self):  # noqa: N802
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b'{"partial":')

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setattr(console_app, "_log_op", lambda *a, **kw: pytest.fail("local audit after forwarding"))
        try:
            response = client_mode.put("/api/panels", json=[])
            assert response.status_code == 503
            assert "响应中断" in response.get_json()["error"]
            assert len(received) == 1
        finally:
            server.shutdown()
            thread.join(timeout=2)


@pytest.mark.parametrize("kind", ["panels", "assets", "machines", "settings"])
def test_client_fixture_isolates_all_runtime_yaml(client_mode, tmp_path, kind):
    paths = {
        "panels": console_app.PANELS_PATH,
        "assets": console_app.ASSETS_PATH,
        "machines": console_app.mc.machines_path(),
        "settings": console_app.settings_store.settings_path(),
    }
    # Fail before any I/O if this test is run against the original unsafe fixture.
    assert Path(paths[kind]).resolve().is_relative_to(tmp_path.resolve())



def test_missing_forwarding_writes_only_temporary_panels_before_audit_failure(client_mode, tmp_path, monkeypatch):
    target = Path(console_app.PANELS_PATH).resolve()
    # This assertion must precede all writes so the regression itself is first-red safe.
    assert target.is_relative_to(tmp_path.resolve())
    target.write_text("- id: 1\n  name: synthetic-panel\n", encoding="utf-8")
    monkeypatch.setattr(console_app, "_log_op", lambda *a, **kw: pytest.fail("synthetic audit failure"))
    # Calling the actual view bypasses forwarding exactly as a broken routing hook would.
    with console_app.app.test_request_context("/api/panels", method="PUT", json=[]):
        with pytest.raises(pytest.fail.Exception, match="synthetic audit failure"):
            console_app.app.view_functions["panels_collection"]()
    assert target.read_text(encoding="utf-8") == "[]\n"


@pytest.mark.parametrize("kind", ["assets", "machines", "settings"])
def test_local_config_writers_use_client_fixture_sandbox(client_mode, tmp_path, kind):
    targets = {
        "assets": console_app.ASSETS_PATH,
        "machines": console_app.mc.machines_path(),
        "settings": console_app.settings_store.settings_path(),
    }
    target = Path(targets[kind]).resolve()
    assert target.is_relative_to(tmp_path.resolve())
    if kind == "assets":
        console_app.sm.save_assets(console_app.ASSETS_PATH, [{"name": "synthetic-service"}])
    elif kind == "machines":
        console_app.mc.save_machines_raw([{"id": "synthetic-machine"}])
    else:
        console_app.settings_store._write_settings({"CONSOLE_OPLOG_RETENTION_DAYS": "7"})
    assert target.stat().st_size > 3



def test_client_fixture_blocks_postgres_audit_before_connecting(client_mode, monkeypatch):
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    # pytest.fail inherits BaseException, so the production audit's Exception
    # handler cannot turn this safety tripwire into an apparently successful test.
    with pytest.raises(pytest.fail.Exception, match="fixture forbids PostgreSQL"):
        console_app._log_op("synthetic", "synthetic", "review", True)
