# -*- coding: utf-8 -*-
"""dashboard_layout.py 回归（C8 / D1'）：迁移幂等 + PUT 校验三件套 + oplog + 反代理吞并。

覆盖：
- 迁移 v9：空库直跑、重复执行幂等、旧库（v1–v8 已应用）只补 v9、表列集；
- GET：空库形状、表缺失显式空、读回 roundtrip（含 hidden 标记与 updated_at）；
- PUT：白名单（固定件 / machine-card:<台账 id> / machine-card:sys:<id> / 未知 400）、
  坐标边界（整数与 0≤x≤11、y≥0、1≤w≤12）、重复 key 400、非数组 400、幂等；
- oplog：host='console', svc='dashboard', action='dashboard_layout_replace'，
  成功 Success / 校验失败 Failed；
- 路由不被 Beszel 同源代理吞掉（CONSOLE_API_PATHS 登记兜底）；
- 响应零凭据 / 零 tailscale IP。
"""
import json
import os
import pathlib
import sqlite3
import sys

import pytest
import yaml

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import dashboard_layout as dl  # noqa: E402
import db as dbmod  # noqa: E402
import machine_config as mc  # noqa: E402

# 测试台账：nas/txy 可作 machine-card:<id>；ghost 不在台账 → 400
MACHINES_YAML = [
    {"id": "nas", "tailscale": "100.64.0.1"},
    {"id": "txy", "tailscale": "100.64.0.2"},
]


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("MACHINE_TAILSCALE_"):
            monkeypatch.delenv(key, raising=False)
    path = tmp_path / "machines.yaml"
    path.write_text(yaml.safe_dump(MACHINES_YAML), encoding="utf-8")
    monkeypatch.setenv("CONSOLE_MACHINES_PATH", str(path))
    mc.invalidate()
    yield
    mc.invalidate()


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "console.db"
    dbmod.run_migrations(str(path))
    return str(path)


@pytest.fixture()
def client(tmp_path, monkeypatch, registry):
    import app as console_app

    monkeypatch.setattr(console_app, "DB_PATH", str(tmp_path / "console.db"))
    monkeypatch.setattr(console_app, "ASSETS_PATH", str(tmp_path / "assets.yaml"))
    console_app.init_db()
    return console_app.app.test_client()


# ---------------------------------------------------------------------------
# 迁移 v9
# ---------------------------------------------------------------------------


def test_migration_v9_on_empty_db(tmp_path):
    applied = dbmod.run_migrations(str(tmp_path / "fresh.db"))
    assert 9 in applied
    # 末项跟着 MIGRATIONS 走，不写死版本号：写死的话每加一个新迁移都会来改这行
    assert applied == [version for version, _desc, _sql in dbmod.MIGRATIONS]


def test_migration_v9_idempotent(tmp_path):
    path = str(tmp_path / "again.db")
    first = dbmod.run_migrations(path)
    second = dbmod.run_migrations(path)
    assert first == second
    conn = sqlite3.connect(path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 9"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_migration_v9_on_old_db_adds_missing_versions(tmp_path):
    """旧库（v1–v8 已应用）补齐余下迁移（v9 起），不动既有表。"""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,"
            " applied_at TEXT NOT NULL DEFAULT (datetime('now')),"
            " description TEXT NOT NULL DEFAULT '')"
        )
        for version in range(1, 9):
            conn.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                (version, f"seed-{version}"),
            )
        conn.execute("CREATE TABLE renewals (id TEXT PRIMARY KEY, name TEXT NOT NULL)")
        conn.commit()
    finally:
        conn.close()
    applied = dbmod.run_migrations(path)
    assert 9 in applied
    assert applied == [version for version, _desc, _sql in dbmod.MIGRATIONS]
    conn = sqlite3.connect(path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert "dashboard_layout" in tables
    assert "renewals" in tables


def test_migration_v9_columns(db_path):
    conn = sqlite3.connect(db_path)
    try:
        cols = {row[1]: row for row in conn.execute("PRAGMA table_info(dashboard_layout)")}
    finally:
        conn.close()
    assert set(cols) == {"widget_key", "x", "y", "w", "hidden", "updated_at"}
    assert cols["widget_key"][5] == 1  # PRIMARY KEY（pk 序号 1）
    assert str(cols["hidden"][4]) == "0"  # DEFAULT 0
    # 不存 h：C8 高度由 sizeToContent 现算
    assert "h" not in cols


# ---------------------------------------------------------------------------
# GET / PUT（模块级函数）
# ---------------------------------------------------------------------------


def test_get_empty_layout(db_path):
    assert dl.get_layout(db_path) == {"items": [], "updated_at": None}


def test_get_missing_table_is_empty(tmp_path):
    """未跑迁移的库文件：GET 显式空，不抛 500。"""
    assert dl.get_layout(str(tmp_path / "no-table.db"))["items"] == []


def test_put_and_get_roundtrip(db_path):
    items = [
        {"widget_key": "attention", "x": 0, "y": 12, "w": 12},
        {"widget_key": "machine-card:nas", "x": 3, "y": 0, "w": 3, "hidden": True},
    ]
    result = dl.replace_layout(items, db_path, now="2026-09-06T00:00:00Z")
    assert result["updated_at"] == "2026-09-06T00:00:00Z"
    by_key = {it["widget_key"]: it for it in result["items"]}
    assert by_key["attention"] == {
        "widget_key": "attention",
        "x": 0,
        "y": 12,
        "w": 12,
        "hidden": False,
    }
    assert by_key["machine-card:nas"]["hidden"] is True
    assert by_key["machine-card:nas"]["x"] == 3  # 隐藏行坐标保留（找回后恢复原位）

    readback = dl.get_layout(db_path)
    assert readback["items"] == result["items"]


def test_put_replace_not_merge(db_path):
    """整体替换：上一轮的行不留在表里。"""
    dl.replace_layout([{"widget_key": "attention", "x": 0, "y": 0, "w": 12}], db_path)
    dl.replace_layout([{"widget_key": "tailnet", "x": 0, "y": 0, "w": 12}], db_path)
    keys = {it["widget_key"] for it in dl.get_layout(db_path)["items"]}
    assert keys == {"tailnet"}


# ---------------------------------------------------------------------------
# PUT 校验三件套
# ---------------------------------------------------------------------------


def test_put_whitelist_fixed_and_machine_keys(db_path):
    items = [
        {"widget_key": "inspection", "x": 0, "y": 0, "w": 12},
        {"widget_key": "attention", "x": 0, "y": 12, "w": 12},
        {"widget_key": "monitor-events", "x": 0, "y": 24, "w": 12},
        {"widget_key": "ops-status", "x": 0, "y": 36, "w": 12},
        {"widget_key": "tailnet", "x": 0, "y": 48, "w": 12},
        {"widget_key": "machine-card:nas", "x": 0, "y": 60, "w": 3},
        {"widget_key": "machine-card:txy", "x": 3, "y": 60, "w": 3},
        {"widget_key": "machine-card:sys:beszel-orphan", "x": 6, "y": 60, "w": 3},
    ]
    result = dl.replace_layout(items, db_path)
    assert {it["widget_key"] for it in result["items"]} == {
        it["widget_key"] for it in items
    }


@pytest.mark.parametrize(
    "key",
    [
        "bogus-widget",  # 完全未知
        "machine-card:ghost",  # 台账里没有的机器
        "machine-card:",  # 空 id
        "machine-card:sys:",  # 孤儿前缀但空 id
        "machine-card:nas extra",  # 含空白
        "MACHINE-CARD:nas",  # 大小写敏感
        "machine-cardx:nas",
        "",
        123,  # 非字符串
    ],
)
def test_put_unknown_key_400(key, db_path):
    with pytest.raises(ValueError):
        dl.replace_layout([{"widget_key": key, "x": 0, "y": 0, "w": 12}], db_path)


@pytest.mark.parametrize(
    "coords",
    [
        {"x": -1, "y": 0, "w": 12},
        {"x": 12, "y": 0, "w": 1},  # 12 列网格 x 最大 11
        {"x": 0, "y": -1, "w": 12},
        {"x": 0, "y": 0, "w": 0},
        {"x": 0, "y": 0, "w": 13},
        {"x": 0, "y": 0, "w": 12.0},  # 浮点
        {"x": "0", "y": 0, "w": 12},  # 字符串
        {"x": True, "y": 0, "w": 12},  # bool 是 int 子类，必须拒
        {"y": 0, "w": 12},  # 缺 x
        {"x": 0, "y": 0, "w": 12, "hidden": "yes"},  # hidden 非 0/1/bool
    ],
)
def test_put_out_of_bounds_400(coords, db_path):
    with pytest.raises(ValueError):
        dl.replace_layout([{"widget_key": "attention", **coords}], db_path)


def test_put_boundary_values_ok(db_path):
    items = [
        {"widget_key": "attention", "x": 11, "y": 0, "w": 1},
        {"widget_key": "tailnet", "x": 0, "y": 99999, "w": 12},
        {"widget_key": "machine-card:nas", "x": 0, "y": 0, "w": 3, "hidden": 1},
        {"widget_key": "machine-card:txy", "x": 0, "y": 0, "w": 3, "hidden": 0},
    ]
    dl.replace_layout(items, db_path)  # 不抛即通过


def test_put_duplicate_key_400(db_path):
    items = [
        {"widget_key": "attention", "x": 0, "y": 0, "w": 12},
        {"widget_key": "attention", "x": 3, "y": 0, "w": 12},
    ]
    with pytest.raises(ValueError, match="重复"):
        dl.replace_layout(items, db_path)


def test_put_items_not_a_list(db_path):
    with pytest.raises(ValueError):
        dl.replace_layout({"widget_key": "attention"}, db_path)
    with pytest.raises(ValueError):
        dl.replace_layout(None, db_path)


def test_put_idempotent(db_path):
    items = [{"widget_key": "attention", "x": 0, "y": 12, "w": 12}]
    first = dl.replace_layout(items, db_path, now="2026-09-06T00:00:00Z")
    second = dl.replace_layout(items, db_path, now="2026-09-06T00:00:00Z")
    assert first == second


# ---------------------------------------------------------------------------
# /api/dashboard-layout 路由
# ---------------------------------------------------------------------------


def test_route_get_empty(client):
    resp = client.get("/api/dashboard-layout")
    assert resp.status_code == 200
    assert resp.get_json() == {"items": [], "updated_at": None}


def test_route_put_roundtrip(client):
    resp = client.put(
        "/api/dashboard-layout",
        json={"items": [{"widget_key": "attention", "x": 0, "y": 12, "w": 12}]},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["items"][0]["widget_key"] == "attention"
    assert body["updated_at"]

    readback = client.get("/api/dashboard-layout").get_json()
    assert readback == body


def test_route_put_unknown_key_400(client):
    resp = client.put(
        "/api/dashboard-layout",
        json={"items": [{"widget_key": "bogus", "x": 0, "y": 0, "w": 12}]},
    )
    assert resp.status_code == 400
    assert "widget_key" in resp.get_json()["error"]


def test_route_put_out_of_bounds_400(client):
    resp = client.put(
        "/api/dashboard-layout",
        json={"items": [{"widget_key": "attention", "x": 0, "y": 0, "w": 13}]},
    )
    assert resp.status_code == 400


def test_route_put_non_json_400(client):
    resp = client.put("/api/dashboard-layout", data="not json", content_type="text/plain")
    assert resp.status_code == 400


def test_route_not_swallowed_by_beszel_proxy(client, monkeypatch):
    """新路由漏登记 CONSOLE_API_PATHS 时不会 404，而是被代理成 HTML+200。"""
    import app as console_app

    def boom(*args, **kwargs):
        raise AssertionError("dashboard-layout 被 Beszel 代理接管了")

    monkeypatch.setattr(console_app, "_proxy_authority", boom)
    assert console_app._is_console_api("/api/dashboard-layout")
    assert client.get("/api/dashboard-layout").mimetype == "application/json"


# ---------------------------------------------------------------------------
# oplog
# ---------------------------------------------------------------------------


def _oplog_rows(db_file):
    conn = sqlite3.connect(db_file)
    try:
        return conn.execute(
            "SELECT host, svc, action, result FROM oplog"
            " WHERE action = 'dashboard_layout_replace' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_oplog_success_and_failure(client, tmp_path):
    ok = client.put(
        "/api/dashboard-layout",
        json={"items": [{"widget_key": "attention", "x": 0, "y": 0, "w": 12}]},
    )
    assert ok.status_code == 200
    bad = client.put(
        "/api/dashboard-layout",
        json={"items": [{"widget_key": "bogus", "x": 0, "y": 0, "w": 12}]},
    )
    assert bad.status_code == 400

    rows = _oplog_rows(tmp_path / "console.db")
    assert len(rows) == 2
    for host, svc, action, _ in rows:
        assert (host, svc, action) == ("console", "dashboard", "dashboard_layout_replace")
    assert rows[0][3] == "Success"
    assert rows[1][3].startswith("Failed")


# ---------------------------------------------------------------------------
# 泄漏断言
# ---------------------------------------------------------------------------


def test_response_has_no_credentials_or_ips(client):
    client.put(
        "/api/dashboard-layout",
        json={"items": [{"widget_key": "machine-card:nas", "x": 0, "y": 0, "w": 3}]},
    )
    body = client.get("/api/dashboard-layout").get_data(as_text=True)
    assert "100.64" not in body  # 台账里配置的 tailscale IP 不出现在响应
    assert "tailscale" not in body
    lowered = body.lower()
    assert all(hint not in lowered for hint in ("password", "token", "secret"))
    json.loads(body)  # 且始终是合法 JSON
