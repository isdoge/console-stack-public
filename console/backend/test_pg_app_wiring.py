# -*- coding: utf-8 -*-
"""app.py 的 PG 接线回归（PG-02）。

覆盖 §8.5 PG-02 的完成门槛里属于本任务的部分：

- 非账本 API：bookmarks CRUD、panel_scratch、oplog 读写，在 PG 上语义与 SQLite 一致；
- 启动契约：PG 模式下不创建本地 console.db、不执行 SQLite 迁移；
- 采集门禁：client 不 seed、不写审计；观测路由转交 authority（origin 缺失时 503）；
- 维护模式：read_only 下写路由 503、读路由正常；
- 异常映射：约束冲突 → 409、存储不可达 → 503。

**集成待 PG-05**：账本/续费/文件路由的真实 PG 行为依赖 PG-01 的交付，
本文件只用契约替身验证 app.py 的接线形状，不代表端到端完成。
"""
import io
import os
import pathlib
import selectors
import subprocess
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import app as console_app  # noqa: E402
import db as dbmod  # noqa: E402
from pg02_support import count_rows, fetch_all  # noqa: E402
from plugin_imports import load_module  # noqa: E402

plugin_ledger = load_module("plugins.ledger.backend.ledger")


feed_routes = load_module("plugins.feed.backend.routes")


# 夹具通过插件注册加载：直接 import 同名夹具会被 pyflakes 判 F811（测试参数同名）。
pytest_plugins = ("pg02_support",)


@pytest.fixture(autouse=True)
def _configured_authority(monkeypatch):
    monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", "http://nas.example.test:8081")



@pytest.fixture()
def client(pg_client):
    return console_app.app.test_client()


@pytest.fixture()
def authority_client(pg_authority):
    return console_app.app.test_client()


# ---- bookmarks ---------------------------------------------------------------


def test_bookmarks_start_empty_and_roundtrip(client):
    body = client.get("/api/bookmarks").get_json()
    assert body["success"] is True
    assert body["data"]["list"] == []
    assert body["data"]["grouped"] == {}

    created = client.post(
        "/api/bookmarks",
        json={"id": "a", "title": "A", "url": "https://a.test", "category": "cat1", "tags": ["x"]},
    )
    assert created.status_code == 201
    assert created.get_json()["data"]["tags"] == ["x"]

    listed = client.get("/api/bookmarks").get_json()["data"]
    assert [item["id"] for item in listed["list"]] == ["a"]
    assert [item["id"] for item in listed["grouped"]["cat1"]] == ["a"]


def test_bookmark_missing_fields_400(client):
    resp = client.post("/api/bookmarks", json={"title": "no url"})
    assert resp.status_code == 400


def test_duplicate_bookmark_id_is_409(client):
    payload = {"id": "dup", "title": "T", "url": "https://dup.test"}
    assert client.post("/api/bookmarks", json=payload).status_code == 201
    assert client.post("/api/bookmarks", json=payload).status_code == 409


def test_bookmark_update_persists_and_missing_is_404(client):
    client.post("/api/bookmarks", json={"id": "a", "title": "A", "url": "https://a.test"})
    resp = client.put("/api/bookmarks/a", json={"title": "A2", "tags": ["y"]})
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["title"] == "A2"
    assert data["tags"] == ["y"]
    assert data["updated_at"]

    assert client.put("/api/bookmarks/ghost", json={"title": "x"}).status_code == 404
    assert client.put("/api/bookmarks/a", json={}).status_code == 400


def test_bookmark_delete_and_missing_404(client):
    client.post("/api/bookmarks", json={"id": "a", "title": "A", "url": "https://a.test"})
    assert client.delete("/api/bookmarks/a").status_code == 200
    assert client.delete("/api/bookmarks/a").status_code == 404


def test_seed_runs_only_on_authority(authority_client, pg_authority):
    assert feed_routes.seed_workstation_data(console_app.plugin_context) == 6
    assert count_rows(pg_authority, "bookmarks") == 6
    # 幂等：表非空时不再播种
    assert feed_routes.seed_workstation_data(console_app.plugin_context) == 0
    assert count_rows(pg_authority, "bookmarks") == 6


def test_seed_is_skipped_on_client(pg_client):
    assert feed_routes.seed_workstation_data(console_app.plugin_context) == 0
    assert count_rows(pg_client, "bookmarks") == 0


# ---- panel_scratch -----------------------------------------------------------


def test_panel_scratch_roundtrip(client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        console_app, "_authority_config_rows", lambda path: [{"id": 1, "name": "p", "url": "https://p.test"}]
    )
    assert client.get("/api/panels/1/scratch").get_json() == {
        "content": "", "updated_at": None
    }
    put = client.put("/api/panels/1/scratch", json={"content": "hello"})
    assert put.status_code == 200
    assert client.get("/api/panels/1/scratch").get_json()["content"] == "hello"


def test_panel_scratch_rejects_oversize(client, monkeypatch):
    monkeypatch.setattr(
        console_app, "_authority_config_rows", lambda path: [{"id": 1, "name": "p", "url": "https://p.test"}]
    )
    resp = client.put("/api/panels/1/scratch", json={"content": "x" * 20001})
    assert resp.status_code == 400


def test_panel_scratch_unknown_panel_404(client, monkeypatch):
    monkeypatch.setattr(console_app, "_authority_config_rows", lambda path: [])
    assert client.get("/api/panels/9/scratch").status_code == 404


# ---- oplog -------------------------------------------------------------------


def test_log_op_writes_and_reads_back(client):
    console_app._log_op("console", "dashboard", "dashboard_layout_replace", True)
    logs = client.get("/api/oplog").get_json()
    assert [row["action"] for row in logs] == ["dashboard_layout_replace"]
    assert logs[0]["result"] == "Success"
    assert logs[0]["operator"] == "self"


def test_log_op_records_failure_detail(client):
    console_app._log_op("console", "svc", "svc_action", False, "boom")
    row = client.get("/api/oplog").get_json()[0]
    assert row["result"] == "Failed: boom"


def test_oplog_query_is_case_insensitive(client):
    """SQLite 的 LIKE 不区分大小写；PG 要 ILIKE 才等价（§4.3）。"""
    console_app._log_op("console", "Dashboard", "layout_replace", True)
    assert client.get("/api/oplog?q=dashboard").get_json()
    assert client.get("/api/oplog?q=LAYOUT").get_json()


def test_oplog_failure_does_not_raise(client, monkeypatch):
    """审计写不进去不能把已提交的业务变成 500（§4.2）。"""
    import storage_dispatch

    class Boom(Exception):
        pass

    def explode(**kwargs):
        raise Boom("db down")

    monkeypatch.setattr(storage_dispatch, "write_tx", explode)
    console_app._log_op("console", "svc", "x", True)  # 不抛即通过


def test_oplog_limit_validation(client):
    assert client.get("/api/oplog?limit=abc").status_code == 400
    assert client.get("/api/oplog?limit=99999").status_code == 200


# ---- 采集门禁与转交 -----------------------------------------------------------


def test_client_observation_route_without_origin_is_503(client, monkeypatch):
    monkeypatch.delenv("CONSOLE_AUTHORITY_ORIGIN", raising=False)
    resp = client.get("/api/machines")
    assert resp.status_code == 503
    assert "CONSOLE_AUTHORITY_ORIGIN" in resp.get_json()["error"]


def test_client_observation_route_forwards_to_authority(client, monkeypatch):
    seen = {}
    monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", "http://nas.example.test:8081")

    def fake_proxy(origin, subpath, rewrite_beszel_paths=False):
        seen["origin"] = origin
        seen["subpath"] = subpath
        return console_app.jsonify({"forwarded": True})

    monkeypatch.setattr(console_app, "_proxy_authority", fake_proxy)
    resp = client.get("/api/services?force=1")
    assert resp.get_json() == {"forwarded": True}
    assert seen["origin"] == "http://nas.example.test:8081"
    assert seen["subpath"] == "api/services"


def test_client_self_origin_is_refused(client, monkeypatch):
    monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", "http://localhost")
    resp = client.get("/api/machines")
    assert resp.status_code == 503
    assert "指向自身" in resp.get_json()["error"]


def test_client_pure_query_routes_are_not_forwarded(client, monkeypatch):
    """纯查询（事件/布局/书签）读共享 PG，不转交。"""
    monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", "http://nas.example.test:8081")
    monkeypatch.setattr(
        console_app, "_proxy_authority",
        lambda *a, **k: pytest.fail("纯查询不该被转交"),
    )
    assert client.get("/api/events").status_code == 200
    assert client.get("/api/dashboard-layout").status_code == 200
    assert client.get("/api/bookmarks").status_code == 200


def test_authority_does_not_forward(authority_client, monkeypatch):
    monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", "http://nas.example.test:8081")
    monkeypatch.setattr(
        console_app, "_proxy_authority",
        lambda *a, **k: pytest.fail("authority 不该转发给自己"),
    )
    monkeypatch.setattr(
        console_app.mc, "machines_for_api", lambda: []
    )
    assert authority_client.get("/api/machines").status_code == 200


def test_monitor_and_observability_never_read_retired_hub(authority_client, monkeypatch):
    """页面路由只走 PG，旧源环境值不得改变选择。"""
    monkeypatch.setenv("CONSOLE_MONITORING_BUSINESS_SOURCE", "beszel")
    monkeypatch.setattr(console_app.mb, "monitor_snapshot", lambda: {"ok": True, "source": "console_pg"})
    monkeypatch.setattr(console_app.mb, "observability_snapshot", lambda: {"ok": True, "source": "console_pg"})
    assert authority_client.get("/api/monitor").get_json()["source"] == "console_pg"
    assert authority_client.get("/api/observability").get_json()["source"] == "console_pg"
    assert authority_client.get("/api/definitely-unregistered").status_code == 404


# ---- 维护模式 ---------------------------------------------------------------


def test_read_only_blocks_business_write(pg_readonly):
    http = console_app.app.test_client()
    resp = http.post(
        "/api/bookmarks", json={"id": "x", "title": "T", "url": "https://x.test"}
    )
    assert resp.status_code == 503
    assert "维护" in resp.get_json()["error"]
    # 读仍然正常
    assert http.get("/api/bookmarks").status_code == 200


def test_read_only_blocks_audit_write(pg_readonly):
    """read_only 下审计写入被拒，但 `_log_op` 不把它变成异常（§4.2）。"""
    console_app._log_op("console", "svc", "x", True)
    assert count_rows(pg_readonly, "oplog") == 0


# ---- 启动契约 ---------------------------------------------------------------


def test_pg_startup_does_not_create_sqlite_file(tmp_path, monkeypatch):
    """PG 模式启动不建本地 console.db、不跑 SQLite 迁移（D3）。"""
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    target = tmp_path / "should-not-exist.db"
    monkeypatch.setattr(console_app, "DB_PATH", str(target))
    console_app.init_db()
    assert not target.exists()


def test_legacy_conn_is_refused_in_pg_mode(monkeypatch):
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    with pytest.raises(RuntimeError, match="不得使用 legacy SQLite"):
        console_app.get_db_conn()


def test_startup_exits_nonzero_when_pg_unreachable(pg_client):
    """PG 连不上时进程以非零码退出，**不降级到 SQLite**（D3/D10）。

    在子进程里跑，避免污染本进程的池与线程。
    """
    import subprocess

    env = dict(os.environ)
    env.update(
        {
            "CONSOLE_STORAGE_ENGINE": "postgres",
            "CONSOLE_PG_HOST": "127.0.0.1",
            "CONSOLE_PG_PORT": "1",          # 无监听端口 → 必然连不上
            "CONSOLE_PG_DB": "nope",
            "CONSOLE_PG_USER": "nobody",
            "CONSOLE_PG_PASSWORD": "x",
            "CONSOLE_PG_CONNECT_TIMEOUT": "1",
            "CONSOLE_PG_POOL_MIN": "1",
            "CONSOLE_PG_POOL_MAX": "1",
            "CONSOLE_INSTANCE_ROLE": "authority",
        }
    )
    proc = subprocess.run(
        [sys.executable, "-c", "import app"],
        cwd=str(BACKEND_DIR), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 2, proc.stderr
    assert "拒绝以 SQLite 降级启动" in proc.stderr


def test_pg_startup_preconditions_hold_on_isolated_db(pg_client):
    """启动序列的前置条件：池可开、schema 与代码一致。"""
    import pg_migrations

    dbmod.pg_open(wait=True, timeout=10)
    health = dbmod.pg_healthcheck()
    assert health["reachable"] is True
    assert health["schema_version"] == pg_migrations.latest_schema_version()
    with dbmod.pg_transaction(readonly=True) as conn:
        assert pg_migrations.verify(conn)["compatible"] is True


def test_storage_error_mapping_covers_the_contract():
    """映射表与 PG-00 interfaces.md §3 一致。"""
    import pgdb

    assert console_app._storage_error_response(pgdb.StorageUnavailable("x"))[1] == 503
    assert console_app._storage_error_response(pgdb.ConcurrentConflict("x"))[1] == 409
    assert console_app._storage_error_response(pgdb.PgConfigError("x"))[1] == 500
    assert console_app._storage_error_response(pgdb.PgPermissionError("x"))[1] == 500
    assert console_app._storage_error_response(pgdb.CommitOutcomeUnknown("x"))[1] == 503
    import runtime_mode

    assert console_app._storage_error_response(runtime_mode.ReadOnlyMode("x"))[1] == 503


def test_storage_error_text_has_no_credentials():
    import pgdb

    payload, _ = console_app._storage_error_response(
        pgdb.StorageUnavailable("host=100.1.2.3 user=console_nas password=secret")
    )
    assert "secret" not in payload["error"]
    assert "console_nas" not in payload["error"]


def test_ledger_routes_go_through_storage_error_mapping(monkeypatch):
    """账本路由的兜底分支也必须先过 §4.1 映射（PG-05 集成修正）。

    `_ledger_call` 的 `except Exception` 曾把 `StorageUnavailable` 吞成 500
    「账本操作暂时失败」——客户端因此不重试也不告警，而 `/api/renewals` 走
    Flask 错误处理器是 503。两处对同一故障给出不同语义是接线缺陷，这里锁住。
    """
    import pgdb

    def boom(*args, **kwargs):
        raise pgdb.StorageUnavailable("host=100.1.2.3 user=console_nas password=secret")

    monkeypatch.setattr(plugin_ledger, "overview", boom)
    http = console_app.app.test_client()
    resp = http.get("/api/ledger/overview")
    assert resp.status_code == 503, resp.get_json()
    assert "密码" not in resp.get_json()["error"] and "secret" not in resp.get_json()["error"]


def test_foreign_key_violation_maps_to_409(pg_client):
    """真 PG 上的约束冲突走 409，而不是 500（连接仍可继续使用）。"""
    import pgdb

    with pytest.raises(pgdb.ConcurrentConflict):
        with dbmod.pg_transaction() as conn:
            conn.execute(
                "INSERT INTO monitor_events (fingerprint, kind, target, opened_at)"
                " VALUES ('dup','machine','t','t')"
            )
            conn.execute(
                "INSERT INTO monitor_events (fingerprint, kind, target, opened_at)"
                " VALUES ('dup','machine','t','t')"
            )


# ---- 维护模式挡住 YAML 写（§6.4） -------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/machines"),
        ("PUT", "/api/machines/kr"),
        ("DELETE", "/api/machines/kr"),
        ("POST", "/api/service"),
        ("PUT", "/api/service"),
        ("DELETE", "/api/service/web"),
        ("POST", "/api/services/priority"),
        ("POST", "/api/panels"),
        ("PUT", "/api/panels"),
        ("PUT", "/api/panels/1"),
        ("DELETE", "/api/panels/1"),
        ("PUT", "/api/config/settings"),
    ],
)
def test_read_only_blocks_yaml_writes(pg_readonly, method, path):
    """YAML/配置写入不经数据库，必须由 HTTP 入口挡住，且返回 503 而不是 400。"""
    http = console_app.app.test_client()
    resp = http.open(path, method=method, json={})
    assert resp.status_code == 503, f"{method} {path} → {resp.status_code}"
    assert "维护" in resp.get_json()["error"]


def test_read_only_does_not_block_reads(pg_readonly):
    http = console_app.app.test_client()
    for path in ("/api/health", "/api/bookmarks", "/api/dashboard-layout", "/api/events"):
        assert http.get(path).status_code == 200, path


def test_normal_authority_does_not_block_yaml_writes(pg_authority, monkeypatch):
    """对照组：非维护模式下同一路由不会被门禁拦下（走到业务校验）。"""
    http = console_app.app.test_client()
    resp = http.post("/api/panels", json={})
    assert resp.status_code != 503


# ---- 启动期 schema 兼容闸门（提案 §5.1 / §7；PG-06 演练 S7 ⑥ 缺陷回归）--------


def _startup_subprocess(env_overrides, timeout=120, with_logging=False):
    """在子进程里跑 `import app`（触发模块级 `_startup()`），返回 CompletedProcess。

    必须在子进程里跑：`_startup()` 会起后台线程并打开连接池，在本进程里跑会污染
    其它测试。这与既有的 `test_startup_exits_nonzero_when_pg_unreachable` 同法。

    `with_logging`：app 模块不配 logging handler，未配 handler 时 CPython 的
    `logging.lastResort` 只兜 WARNING 及以上——`logger.critical()`（拒绝启动）能进
    stderr，`logger.info()`（放行）会被丢弃。要在正向对照里断言放行日志，就得先装
    handler。root 级别留在 WARNING，避免第三方库的 INFO 混进来。
    """
    import subprocess

    env = dict(os.environ)
    env.update(env_overrides)
    env.pop("PYTHONPATH", None)
    source = "import app"
    if with_logging:
        source = (
            "import logging, sys;"
            "logging.basicConfig(level=logging.WARNING, stream=sys.stderr);"
            "logging.getLogger('console.backend').setLevel(logging.INFO);"
            "import app"
        )
    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=str(BACKEND_DIR), env=env, capture_output=True, text=True, timeout=timeout,
    )


def _app_env(info, **extra):
    from pg02_support import ROLE, ROLE_PASSWORD

    env = {
        "CONSOLE_STORAGE_ENGINE": "postgres",
        "CONSOLE_PG_HOST": info["host"],
        "CONSOLE_PG_PORT": str(info["port"]),
        "CONSOLE_PG_DB": info["db_name"],
        "CONSOLE_PG_USER": ROLE,
        "CONSOLE_PG_PASSWORD": ROLE_PASSWORD,
        "CONSOLE_INSTANCE_ROLE": "client",
        "CONSOLE_AUTHORITY_ORIGIN": "http://nas.example.test:8081",
        "CONSOLE_MAINTENANCE_MODE": "normal",
        "CONSOLE_PG_POOL_MIN": "1",
        "CONSOLE_PG_POOL_MAX": "2",
    }
    env.update(extra)
    return env


def test_startup_refuses_future_schema_version(pg_client):
    """目标库 schema 比代码新 → 拒绝启动（rc=3）且提示可读、不含凭据。

    这是 PG-06 演练 S7 ⑥ 的缺陷回归：修复前，期望 v12 的 app 遇到 v13 目标库
    照常启动、读写全通（真 HTTP 实测写请求 201 且行落 PG）。§5.1 要求
    「Mac 旧代码遇不兼容版本必须拒绝业务写入并提示更新」，§7 要求有界失败。
    """
    import pg_migrations
    import pg_test_support

    admin = pg_test_support.connect(pg_client["admin_dsn"], autocommit=True)
    try:
        admin.execute(
            "INSERT INTO schema_migrations (version, description, checksum) "
            "VALUES (%s, 'test_future_schema_probe', 'deadbeef')",
            (pg_migrations.latest_schema_version() + 1,),
        )
    finally:
        admin.close()

    proc = _startup_subprocess(_app_env(pg_client))
    assert proc.returncode == 3, proc.stderr
    combined = proc.stdout + proc.stderr
    assert "schema 不兼容" in combined
    assert "无需迁移" not in combined  # 提示必须指到真正的修复动作
    assert "pg_schema.py migrate" in combined
    # 反向约束：拒绝提示不得泄露连接细节。
    for secret in ("console_pg02_app_pw", "password=", pg_client["db_name"]):
        assert secret not in combined, secret


def test_startup_refuses_empty_schema(pg_container):
    """空库（没跑过 migrate）同样拒绝启动，且**不因缺表探测而崩**。

    第二层根因回归：`applied()` 早期实现在缺表时捕获异常却不清事务状态，
    非自动提交连接上会把只读事务拖成 aborted，后续语句全部
    `InFailedSqlTransaction`。修复前实测这条路径抛的是事务污染错误而不是
    可读的「不兼容」提示。
    """
    import uuid

    import pg_test_support

    admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
    name = f"console_gate_empty_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    admin.close()
    try:
        proc = _startup_subprocess(
            {
                "CONSOLE_STORAGE_ENGINE": "postgres",
                "CONSOLE_PG_HOST": pg_container.host,
                "CONSOLE_PG_PORT": str(pg_container.port),
                "CONSOLE_PG_DB": name,
                # 凭据取自 pg_container（external 模式下 = DSN 里的账号），**不要**写
                # pg_test_support.ADMIN_USER 常量：那个角色只在「docker 自建底座」里
                # 存在，CI 用的 external 模式（postgres service + CONSOLE_PG_TEST_DSN）
                # 下不存在，本用例会以「认证失败 → rc=2」误红（2026-09-11 实测）。
                "CONSOLE_PG_USER": pg_container.user,
                "CONSOLE_PG_PASSWORD": pg_container.password,
                "CONSOLE_INSTANCE_ROLE": "client",
                "CONSOLE_AUTHORITY_ORIGIN": "http://nas.example.test:8081",
                "CONSOLE_PG_POOL_MIN": "1",
                "CONSOLE_PG_POOL_MAX": "2",
            }
        )
    finally:
        admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()
    assert proc.returncode == 3, proc.stderr
    combined = proc.stdout + proc.stderr
    assert "schema 不兼容" in combined
    assert "InFailedSqlTransaction" not in combined
    assert "缺表" in combined


def test_startup_allows_compatible_schema(pg_client):
    """正向对照：兼容库必须放行，闸门不能把正常启动也拦下。

    没有这条，「闸门一律拒绝」也能让上面两条测试变绿。
    """
    proc = _startup_subprocess(_app_env(pg_client), timeout=120, with_logging=True)
    # 正常启动会常驻起线程/服务；这里以「进程没有立刻以 2/3 退出」为放行证据。
    # `import app` 只跑启动序列（服务在 `__main__` 分支里起），因此会正常结束。
    assert proc.returncode == 0, proc.stderr
    combined = proc.stdout + proc.stderr
    assert "schema 兼容" in combined
    assert "拒绝启动" not in combined


@pytest.fixture
def authority_http(pg_client, tmp_path):
    """Independent real Flask authority, real YAML files and the same isolated PG."""
    import yaml

    panels = tmp_path / "nas-panels.yaml"
    panels.write_text("[]\n")
    machines = tmp_path / "nas-machines.yaml"
    machines.write_text(yaml.safe_dump([{"id": "nas-only", "tailscale": "100.64.0.9"}]))
    settings = tmp_path / "nas-settings.yaml"
    env = {key: value for key, value in os.environ.items() if not key.startswith("CONSOLE_")}
    env.update(_app_env(pg_client, CONSOLE_INSTANCE_ROLE="authority", CONSOLE_AUTH_ENABLED="false",
                        CONSOLE_MACHINES_PATH=str(machines), CONSOLE_SETTINGS_FILE=str(settings)))
    source = """
import sys
import collect_guard, hindsight_proxy
collect_guard.collection_allowed = lambda: False
hindsight_proxy.run = lambda: None
import app
app.PANELS_PATH = sys.argv[1]
from werkzeug.serving import make_server
server = make_server('127.0.0.1', 0, app.app)
print(server.server_port, flush=True)
server.serve_forever()
"""
    with (tmp_path / "authority.log").open("w+") as log:
        process = subprocess.Popen([sys.executable, "-u", "-c", source, str(panels)],
                                   cwd=BACKEND_DIR, env=env, stdout=subprocess.PIPE,
                                   stderr=log, text=True)
        try:
            with selectors.DefaultSelector() as ready:
                ready.register(process.stdout, selectors.EVENT_READ)
                assert ready.select(timeout=10), "isolated authority did not start"
                line = process.stdout.readline().strip()
            assert line.isdigit(), "isolated authority failed before HTTP listener"
            yield {"origin": f"http://127.0.0.1:{line}", "panels": panels, "settings": settings}
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()


def test_client_yaml_mutations_use_real_authority_and_one_pg_audit(pg_client, authority_http, tmp_path, monkeypatch):
    import yaml

    client_panels = tmp_path / "mac-panels.yaml"
    client_panels.write_text("[]\n")
    client_settings = tmp_path / "mac-settings.yaml"
    client_settings.write_text("{}\n")
    monkeypatch.setattr(console_app, "PANELS_PATH", str(client_panels))
    monkeypatch.setenv("CONSOLE_SETTINGS_FILE", str(client_settings))
    monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", authority_http["origin"])
    monkeypatch.setattr(console_app, "_log_op", lambda *a, **kw: pytest.fail("client double audit"))
    http = console_app.app.test_client()
    body = [{"id": 9, "name": "NAS only", "url": "https://panel.example.test"}]
    saved = http.put("/api/panels", json=body)
    assert saved.status_code == 200, saved.get_json()
    assert yaml.safe_load(authority_http["panels"].read_text())[0]["id"] == 9
    assert http.get("/api/panels").get_json()[0]["id"] == 9
    assert client_panels.read_text() == "[]\n"
    settings = http.put("/api/config/settings", json={"values": {"CONSOLE_OPLOG_RETENTION_DAYS": 30}})
    assert settings.status_code == 200, settings.get_json()
    assert yaml.safe_load(authority_http["settings"].read_text())["CONSOLE_OPLOG_RETENTION_DAYS"] == "30"
    assert client_settings.read_text() == "{}\n"
    actions = [row["action"] for row in fetch_all(pg_client, "SELECT action FROM oplog ORDER BY id")]
    assert actions == ["panels_replace", "settings_update"]
    # Scratch is a direct PG write but checks the authoritative panel collection.
    assert http.put("/api/panels/9/scratch", json={"content": "PG shared"}).status_code == 200
    assert fetch_all(pg_client, "SELECT content FROM panel_scratch")[0]["content"] == "PG shared"


def test_client_layout_validates_authority_machine_then_writes_shared_pg(pg_client, authority_http, monkeypatch):
    monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", authority_http["origin"])
    monkeypatch.setattr(console_app.mc, "load_machines", lambda: {})
    http = console_app.app.test_client()
    saved = http.put("/api/dashboard-layout", json={"items": [
        {"widget_key": "machine-card:nas-only", "x": 0, "y": 0, "w": 4},
    ]})
    assert saved.status_code == 200, saved.get_json()
    assert fetch_all(pg_client, "SELECT widget_key FROM dashboard_layout")[0]["widget_key"] == "machine-card:nas-only"
    assert count_rows(pg_client, "oplog") == 1


@pytest.mark.parametrize("path,body,table", [
    ("/api/panels/9/scratch", {"content": "blocked"}, "panel_scratch"),
    ("/api/dashboard-layout", {"items": [{"widget_key": "machine-card:nas-only", "x": 0, "y": 0, "w": 4}]}, "dashboard_layout"),
])
def test_authority_unavailable_blocks_shared_pg_validation_writes(pg_client, monkeypatch, path, body, table):
    monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", "http://127.0.0.1:1")
    response = console_app.app.test_client().put(path, json=body)
    assert response.status_code == 503
    assert count_rows(pg_client, table) == count_rows(pg_client, "oplog") == 0


def test_applied_does_not_poison_readonly_transaction(pg_client):
    """`applied()` 在事务内探测缺表后，外层事务必须仍然可用。

    直接钉住根因，而不是只钉行为：`pg_transaction(readonly=True)` 借出的是非
    自动提交连接，早期实现在缺表时留下 aborted 事务。
    """
    import psycopg

    import pg_migrations

    admin = psycopg.connect(pg_client["admin_dsn"], autocommit=True)
    try:
        admin.execute("DROP TABLE IF EXISTS schema_migrations CASCADE")
        with dbmod.pg_transaction(readonly=True) as conn:
            assert pg_migrations.applied(conn) == {}
            # 关键断言：事务没被污染，后续语句照常返回。
            assert conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
    finally:
        admin.close()


# ---- 账本批次号边界（PG-00 契约 §8；G09/G10 的 HTTP 侧接线） -----------------
#
# PG-01 的 `test_pg01_batch_protocol.py` 只走模块入口（它的 `_route_import` 每次都
# 自己发号），所以「页面路由是否真的把调用方的 batch_id 透传下去」在这批用例之前
# 没有任何断言覆盖（PG-01 也明确把 HTTP 边界划给 PG-05）。本组补的就是这一段：
# 缺号/非法一律 400 且**不进库层**，合法号原样（小写化）透传。

_BATCH_ID = "0f3a5c7e-2b41-4d69-8a10-5e2f7c9b4d31"


def _stub_ledger_import(monkeypatch, calls):
    """把 `ledger_import` 换成记录调用的替身：路由若越过校验去调它，calls 非空。"""

    def import_files(files, **kwargs):
        calls.append(("import_files", kwargs))
        return {"batch_id": kwargs.get("batch_id"), "artifacts": []}

    def reclassify(**kwargs):
        calls.append(("reclassify", kwargs))
        return {"batch_id": kwargs.get("batch_id"), "classified": 0}

    module = load_module("plugins.ledger.backend.ledger_import")
    monkeypatch.setattr(module, "import_files", import_files)
    monkeypatch.setattr(module, "reclassify", reclassify)


@pytest.mark.parametrize("value", [None, "", "   ", "not-a-uuid", "0f3a5c7e-2b41"])
def test_import_route_rejects_bad_batch_id_before_the_library(client, monkeypatch, value):
    """缺号/空号/非 UUID → 400；库层的 `batch_id or new_batch_id()` 兜底不得被触到。"""
    calls = []
    _stub_ledger_import(monkeypatch, calls)
    data = {"files": (io.BytesIO(b"synthetic"), "bill.csv")}
    if value is not None:
        data["batch_id"] = value
    response = client.post("/api/ledger/import", data=data)
    assert response.status_code == 400 and response.is_json
    assert response.get_json()["error"] == "batch_id 必须是 UUID"
    assert calls == []
    assert response.cache_control.no_store


def test_import_route_passes_caller_batch_id_through(client, monkeypatch):
    """正对照：合法号透传到库层且大小写归一——证明上面的 400 不是「一律拒绝」。"""
    calls = []
    _stub_ledger_import(monkeypatch, calls)
    response = client.post(
        "/api/ledger/import",
        data={"batch_id": _BATCH_ID.upper(), "files": (io.BytesIO(b"synthetic"), "bill.csv")},
    )
    assert response.status_code == 200 and response.is_json
    assert [name for name, _ in calls] == ["import_files"]
    assert calls[0][1]["batch_id"] == _BATCH_ID


@pytest.mark.parametrize("value", [None, "", "not-a-uuid"])
def test_reclassify_route_rejects_bad_batch_id_before_the_library(client, monkeypatch, value):
    calls = []
    _stub_ledger_import(monkeypatch, calls)
    payload = {} if value is None else {"batch_id": value}
    response = client.post("/api/ledger/reclassify", json=payload)
    assert response.status_code == 400 and response.is_json
    assert response.get_json()["error"] == "batch_id 必须是 UUID"
    assert calls == []


def test_reclassify_route_passes_caller_batch_id_through(client, monkeypatch):
    calls = []
    _stub_ledger_import(monkeypatch, calls)
    response = client.post("/api/ledger/reclassify", json={"batch_id": _BATCH_ID})
    assert response.status_code == 200 and response.is_json
    assert [name for name, _ in calls] == ["reclassify"]
    assert calls[0][1]["batch_id"] == _BATCH_ID
