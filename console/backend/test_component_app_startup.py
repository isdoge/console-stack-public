"""显式 public 组合在真实组件新库启动；不创建监听服务或访问生产。"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import uuid

import pytest

import component_migrations as cm
import kernel_baseline
import pg_test_support as support

BACKEND = pathlib.Path(__file__).resolve().parent
REPO = BACKEND.parent.parent


@pytest.fixture(params=[[], ["audit"]], ids=["zero-plugins", "audit"])
def public_component_database(pg_container, tmp_path, request):
    name = "b7_public_" + uuid.uuid4().hex[:10]
    role = "b7_public_app_" + uuid.uuid4().hex[:10]
    password = uuid.uuid4().hex
    admin = support.connect(pg_container.dsn(), autocommit=True)
    admin.execute(f'CREATE DATABASE "{name}"')
    owner = support.connect(pg_container.dsn(dbname=name), autocommit=True)
    try:
        components = cm.component_table_map({})
        cm.apply_components(owner, components=components)
        report = cm.registry_report(owner, components)
        assert report["ok"] is True, report["issues"]
        assert report["components"]
        support.create_app_role(owner, role, password,
                                tables=kernel_baseline.KERNEL_BASELINE_TABLES, database=name)
        owner.execute(f'GRANT SELECT ON component_schema_migrations TO "{role}"')
        plugins = tmp_path / "plugins"
        plugins.mkdir()
        shutil.copyfile(REPO / "plugins/contract.json", plugins / "contract.json")
        selected_plugins = request.param
        (plugins / "composition.json").write_text(json.dumps({"plugins": selected_plugins}))
        for plugin_id in selected_plugins:
            shutil.copytree(REPO / "plugins" / plugin_id, plugins / plugin_id,
                            ignore=shutil.ignore_patterns("__pycache__"))
        config = tmp_path / "config"
        config.mkdir()
        for filename in ("machines.yaml", "assets.yaml", "panels.yaml"):
            (config / filename).write_text("[]\n")
        (config / "settings.yaml").write_text("{}\n")
        document = {
            "schema_version": 1, "profile": "public", "host": {}, "alert_machines": {},
            "roots": {"config": str(config), "plugins": str(plugins),
                      "data": str(tmp_path / "data"), "static": str(tmp_path / "static"),
                      "backup": str(tmp_path / "backup")},
        }
        # 缺席 API 的 404 分支要求静态根存在，避免落到「前端未构建」的 503。
        (tmp_path / "static").mkdir()
        composition = tmp_path / "composition.json"
        composition.write_text(json.dumps(document))
        env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "TZ", "PGOPTIONS")
               if key in os.environ}
        env.update(
            CONSOLE_COMPOSITION_FILE=str(composition), CONSOLE_INSTANCE_PROFILE="public",
            CONSOLE_STORAGE_ENGINE="postgres", CONSOLE_PG_HOST=pg_container.host,
            CONSOLE_PG_PORT=str(pg_container.port), CONSOLE_PG_DB=name,
            CONSOLE_PG_USER=role, CONSOLE_PG_PASSWORD=password,
            CONSOLE_PG_POOL_MIN="1", CONSOLE_PG_POOL_MAX="2",
            CONSOLE_AUTH_ENABLED="false", HINDSIGHT_PROXY_PORT="0",
        )
        yield owner, env, selected_plugins
    finally:
        owner.close()
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


def _app(env, role="authority", maintenance="read_only"):
    run_env = dict(env, CONSOLE_INSTANCE_ROLE=role, CONSOLE_MAINTENANCE_MODE=maintenance)
    if role == "client":
        run_env["CONSOLE_AUTHORITY_ORIGIN"] = "http://authority.example.invalid:8081"
    code = '''
import json
import sys
import app
from contract_errors import ContractError
assert app._storage_error_response(ContractError("synthetic")) == ({"error": "synthetic"}, 400)
assert "pg_contracts" not in sys.modules
import collect_guard
import db
import plugin_runtime
import composition
selected = json.loads((composition.root_path("plugins") / "composition.json").read_text())["plugins"]
assert [item["id"] for item in plugin_runtime.MANIFESTS] == selected
client = app.app.test_client()
assert client.get("/api/health").status_code == 200
if "audit" in selected:
    # shared 插件仍须核对 authority 开关；只替换网络传输，保留真实门禁与 PG 读取。
    if not collect_guard.is_collector():
        from flask import jsonify
        def unavailable(origin, path, **kwargs):
            assert path == "/api/plugins"
            return jsonify({"error": "synthetic unavailable"}), 503
        app._request_authority = unavailable
        assert client.get("/api/events").status_code == 503
        def available(origin, path, **kwargs):
            assert path == "/api/plugins"
            return jsonify([{"id": "audit", "enabled": True, "available": True}])
        app._request_authority = available
    for path in ("/api/events", "/api/oplog"):
        response = client.get(path)
        assert response.status_code == 200, (path, response.status_code, response.get_json())
else:
    assert client.get("/api/events").status_code == 404
if collect_guard.is_collector():
    assert client.get("/api/machines").get_json() == []
    assert client.get("/api/observability").get_json()["source"] == "console_pg"
    assert client.get("/api/capacity").get_json()["machines"] == {}
assert not collect_guard.collection_allowed()
with db.pg_transaction(readonly=True) as connection:
    assert connection.execute(
        "SELECT has_table_privilege(current_user, 'component_schema_migrations', 'INSERT,UPDATE,DELETE') AS allowed"
    ).fetchone()["allowed"] is False
    assert connection.execute(
        "SELECT has_sequence_privilege(current_user, 'oplog_id_seq', 'USAGE') AS allowed"
    ).fetchone()["allowed"] is True
assert not any(name == "plugins.ledger" or name.startswith("plugins.ledger.") for name in sys.modules)
assert not {"beszel_api", "beszel_client", "monitor_observability", "capacity_legacy"} & sys.modules.keys()
print(json.dumps({"started": True, "collector": collect_guard.is_collector()}))
db.pg_close()
'''
    return subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=run_env,
                          capture_output=True, text=True, timeout=45)


@pytest.mark.parametrize("role,maintenance", [("authority", "read_only"), ("client", "normal")])
def test_public_app_starts_on_component_only_database(public_component_database, role, maintenance):
    owner, env, selected_plugins = public_component_database
    before = owner.execute("SELECT count(*) AS n FROM component_schema_migrations").fetchone()["n"]
    result = _app(env, role, maintenance)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"started": True, "collector": role == "authority"}
    assert owner.execute("SELECT to_regclass('public.schema_migrations') AS t").fetchone()["t"] is None
    assert owner.execute("SELECT count(*) AS n FROM component_schema_migrations").fetchone()["n"] == before
    for table in ("oplog", "machine_traffic", "monitoring_samples"):
        assert owner.execute(f'SELECT count(*) AS n FROM "{table}"').fetchone()["n"] == 0


def test_public_app_refuses_unknown_component_version(public_component_database):
    owner, env, selected_plugins = public_component_database
    owner.execute("UPDATE component_schema_migrations SET version=999 WHERE component='kernel'")
    result = _app(env)
    assert result.returncode == 3
    assert "组件登记核验失败" in result.stderr
    assert "pg_component_schema.py" in result.stderr
    assert env["CONSOLE_PG_PASSWORD"] not in result.stderr
