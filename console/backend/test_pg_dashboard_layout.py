# -*- coding: utf-8 -*-
"""dashboard_layout 的 PG 路径回归（PG-02）。

在真 PG16 上复验：整体替换语义、白名单/坐标/重复 key 校验、幂等 PUT、空表形状，
以及「表不存在退化为空布局」的既有契约。
"""
import datetime
import os
import pathlib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import dashboard_layout as dl  # noqa: E402
import db as dbmod  # noqa: E402
import machine_config as mc  # noqa: E402
from pg02_support import count_rows  # noqa: E402

# 夹具通过插件注册加载：直接 import 同名夹具会被 pyflakes 判 F811（测试参数同名）。
pytest_plugins = ("pg02_support",)


MACHINES_YAML = [
    {"id": "nas", "tailscale": "100.64.0.1"},
    {"id": "txy", "tailscale": "100.64.0.2"},
]


@pytest.fixture(autouse=True)
def _registry(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("MACHINE_TAILSCALE_"):
            monkeypatch.delenv(key, raising=False)
    path = tmp_path / "machines.yaml"
    path.write_text(yaml.safe_dump(MACHINES_YAML), encoding="utf-8")
    monkeypatch.setenv("CONSOLE_MACHINES_PATH", str(path))
    mc.invalidate()
    yield
    mc.invalidate()


# ---- 读写 -------------------------------------------------------------------


def test_get_empty_layout(pg_client):
    assert dl.get_layout() == {"items": [], "updated_at": None}


def test_replace_and_read_back(pg_client):
    stamp = "2026-09-10T03:00:00Z"
    result = dl.replace_layout(
        [{"widget_key": "attention", "x": 0, "y": 0, "w": 4, "hidden": False}],
        now=stamp,
    )
    assert result["updated_at"] == stamp
    assert result["items"] == [
        {"widget_key": "attention", "x": 0, "y": 0, "w": 4, "hidden": False}
    ]
    assert dl.get_layout() == result


def test_replace_is_whole_table_replacement(pg_client):
    dl.replace_layout([{"widget_key": "attention", "x": 0, "y": 0, "w": 4}])
    dl.replace_layout([{"widget_key": "ops-status", "x": 1, "y": 1, "w": 3}])
    assert [item["widget_key"] for item in dl.get_layout()["items"]] == ["ops-status"]
    assert count_rows(pg_client, "dashboard_layout") == 1


def test_put_empty_returns_to_default(pg_client):
    dl.replace_layout([{"widget_key": "attention", "x": 0, "y": 0, "w": 4}])
    assert dl.replace_layout([]) == {"items": [], "updated_at": dl.get_layout()["updated_at"]}
    assert count_rows(pg_client, "dashboard_layout") == 0


def test_hidden_flag_roundtrip(pg_client):
    dl.replace_layout(
        [
            {"widget_key": "attention", "x": 0, "y": 0, "w": 4, "hidden": True},
            {"widget_key": "tailnet", "x": 4, "y": 0, "w": 4, "hidden": 0},
        ]
    )
    by_key = {item["widget_key"]: item for item in dl.get_layout()["items"]}
    assert by_key["attention"]["hidden"] is True
    assert by_key["tailnet"]["hidden"] is False


def test_idempotent_put_keeps_state(pg_client):
    payload = [{"widget_key": "attention", "x": 2, "y": 3, "w": 4, "hidden": 1}]
    first = dl.replace_layout(payload, now="2026-09-10T03:00:00Z")
    second = dl.replace_layout(payload, now="2026-09-10T04:00:00Z")
    assert first["items"] == second["items"]
    assert second["updated_at"] == "2026-09-10T04:00:00Z"


def test_updated_at_is_utc_text(pg_client):
    dl.replace_layout([{"widget_key": "attention", "x": 0, "y": 0, "w": 4}])
    stamp = dl.get_layout()["updated_at"]
    parsed = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    assert abs(
        (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - parsed)
        .total_seconds()
    ) < 120


# ---- 校验 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        [{"widget_key": "ghost-widget", "x": 0, "y": 0, "w": 4}],
        [{"widget_key": "machine-card:ghost", "x": 0, "y": 0, "w": 4}],
        [{"widget_key": "attention", "x": 12, "y": 0, "w": 4}],
        [{"widget_key": "attention", "x": 0, "y": -1, "w": 4}],
        [{"widget_key": "attention", "x": 0, "y": 0, "w": 0}],
        [{"widget_key": "attention", "x": 0, "y": 0, "w": 13}],
        [{"widget_key": "attention", "x": 1.5, "y": 0, "w": 4}],
        [{"widget_key": "attention", "x": True, "y": 0, "w": 4}],
        [
            {"widget_key": "attention", "x": 0, "y": 0, "w": 4},
            {"widget_key": "attention", "x": 1, "y": 0, "w": 4},
        ],
        "not-a-list",
    ],
)
def test_invalid_payload_is_rejected(pg_client, payload):
    with pytest.raises(ValueError):
        dl.replace_layout(payload)
    assert count_rows(pg_client, "dashboard_layout") == 0


def test_known_machine_and_orphan_system_keys_are_accepted(pg_client):
    dl.replace_layout(
        [
            {"widget_key": "machine-card:nas", "x": 0, "y": 0, "w": 4},
            {"widget_key": "machine-card:sys:beszel-1", "x": 4, "y": 0, "w": 4},
            {"widget_key": "caddy-entry", "x": 8, "y": 0, "w": 4},
        ]
    )
    assert len(dl.get_layout()["items"]) == 3


def test_failed_validation_keeps_previous_layout(pg_client):
    dl.replace_layout([{"widget_key": "attention", "x": 0, "y": 0, "w": 4}])
    with pytest.raises(ValueError):
        dl.replace_layout([{"widget_key": "ghost", "x": 0, "y": 0, "w": 4}])
    assert [item["widget_key"] for item in dl.get_layout()["items"]] == ["attention"]


# ---- 事务复用 ---------------------------------------------------------------


def test_replace_reuses_caller_transaction(pg_client):
    with dbmod.pg_transaction() as conn:
        dl.replace_layout(
            [{"widget_key": "attention", "x": 0, "y": 0, "w": 4}], conn=conn
        )
        conn.rollback()
    assert count_rows(pg_client, "dashboard_layout") == 0


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("reuse_transaction", [False, True])
def test_concurrent_whole_replacements_never_merge(pg_client, monkeypatch, existing, reuse_transaction):
    """Pause after DELETE, then submit another real connection's replacement."""
    import pg_test_support

    if existing:
        dl.replace_layout([{"widget_key": "tailnet", "x": 0, "y": 0, "w": 4}])
    deleted = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()
    real_many = dl.sd.executemany

    def pause_first(conn, sql, params):
        if params and params[0][0] == "attention":
            deleted.set()
            assert release.wait(5), "test did not release first transaction"
        return real_many(conn, sql, params)

    monkeypatch.setattr(dl.sd, "executemany", pause_first)

    def replace(key):
        if key == "ops-status":
            second_entered.set()
        items = [{"widget_key": key, "x": 0, "y": 0, "w": 4}]
        if reuse_transaction:
            with dbmod.pg_transaction() as conn:
                return dl.replace_layout(items, conn=conn)
        return dl.replace_layout(items)

    observer = pg_test_support.connect(pg_client["admin_dsn"], autocommit=True)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(replace, "attention")
            assert deleted.wait(5)
            second = executor.submit(replace, "ops-status")
            assert second_entered.wait(5)
            waiting = False
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not second.done():
                    rows = observer.execute(
                        "SELECT wait_event FROM pg_stat_activity WHERE datname = %s AND wait_event_type = 'Lock'",
                        (pg_client["db_name"],),
                    ).fetchall()
                    waiting = any(row["wait_event"] == "advisory" for row in rows)
                    if waiting:
                        break
                    time.sleep(0.01)
            finally:
                release.set()
            first.result(timeout=5)
            second.result(timeout=5)
            assert waiting, "second whole replacement did not wait for domain advisory lock"
        assert [item["widget_key"] for item in dl.get_layout()["items"]] == ["ops-status"]
    finally:
        release.set()
        observer.close()


def test_get_inside_readonly_transaction(pg_client):
    dl.replace_layout([{"widget_key": "attention", "x": 0, "y": 0, "w": 4}])
    with dbmod.pg_transaction(readonly=True) as conn:
        assert len(dl.get_layout(conn=conn)["items"]) == 1


# ---- 表缺失 -----------------------------------------------------------------


def test_missing_table_returns_empty(pg_client):
    """表被删掉时退化为空布局，不抛——与 SQLite 版既有契约一致。"""
    import pg_test_support

    admin = pg_test_support.connect(pg_client["admin_dsn"], autocommit=True)
    try:
        admin.execute("DROP TABLE dashboard_layout")
        assert dl.get_layout() == {"items": [], "updated_at": None}
    finally:
        admin.close()


def test_read_only_mode_rejects_put(pg_client):
    """维护模式：读正常，写 503（ReadOnlyMode）。"""
    import runtime_mode

    os.environ["CONSOLE_MAINTENANCE_MODE"] = "read_only"
    try:
        assert dl.get_layout() == {"items": [], "updated_at": None}
        with pytest.raises(runtime_mode.ReadOnlyMode):
            dl.replace_layout([{"widget_key": "attention", "x": 0, "y": 0, "w": 4}])
    finally:
        os.environ["CONSOLE_MAINTENANCE_MODE"] = "normal"
