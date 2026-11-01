# -*- coding: utf-8 -*-
"""service_health_history 的 PG 路径回归（PG-02）。

对应 SQLite 版 `test_service_health_history.py` 的业务断言，在真 PG16 上复验：
状态白名单、旧→新顺序、服务隔离、保留期裁剪；另加 `conn` 复用与只读读取。
"""
import pathlib
import sys

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import db as dbmod  # noqa: E402
import service_health_history as shh  # noqa: E402
from pg02_support import count_rows  # noqa: E402

# 夹具通过插件注册加载：直接 import 同名夹具会被 pyflakes 判 F811（测试参数同名）。
pytest_plugins = ("pg02_support",)



def test_record_only_accepts_real_health_status(pg_client):
    assert shh.record("svc", "Healthy", ts="2026-09-03T00:00:00Z") is True
    assert shh.record("svc", "Unhealthy", ts="2026-09-03T00:10:00Z") is True
    # Unknown（无 checker / 显式关闭）不落点
    assert shh.record("svc", "Unknown", ts="2026-09-03T00:20:00Z") is False
    assert shh.record("", "Healthy") is False
    rows = shh.history("svc")
    assert [r["status"] for r in rows] == ["Healthy", "Unhealthy"]


def test_history_oldest_first_and_retained(pg_client):
    for i in range(15):
        status = "Healthy" if i % 2 == 0 else "Unhealthy"
        shh.record("svc", status, ts=f"2026-09-03T00:{i:02d}:00Z")
    rows = shh.history("svc")
    # 默认 12 条（BAND_SIZE），取最近的
    assert len(rows) == 12
    assert rows[0]["ts"] == "2026-09-03T00:03:00Z"
    assert rows[-1]["ts"] == "2026-09-03T00:14:00Z"


def test_services_are_isolated(pg_client):
    shh.record("a", "Healthy", ts="2026-09-03T00:00:00Z")
    shh.record("b", "Unhealthy", ts="2026-09-03T00:01:00Z")
    assert [r["status"] for r in shh.history("a")] == ["Healthy"]
    assert [r["status"] for r in shh.history("b")] == ["Unhealthy"]


def test_empty_name_returns_empty_history(pg_client):
    assert shh.history("") == []


def test_retain_cap_prunes_old_rows(pg_client):
    for i in range(shh.RETAIN_PER_SERVICE + 10):
        shh.record("svc", "Healthy", ts=f"2026-09-03T0{i % 10}:{i % 60:02d}:00Z")
    rows = shh.history("svc", limit=200)
    assert len(rows) == shh.RETAIN_PER_SERVICE


def test_retention_is_per_service(pg_client, monkeypatch):
    """裁剪只清同名服务，别的服务历史不受影响。"""
    monkeypatch.setattr(shh, "RETAIN_PER_SERVICE", 3)
    for i in range(6):
        shh.record("a", "Healthy", ts=f"t{i}")
        shh.record("b", "Healthy", ts=f"t{i}")
    assert count_rows(pg_client, "service_health_history", "name = %s", ("a",)) == 3
    assert count_rows(pg_client, "service_health_history", "name = %s", ("b",)) == 3


def test_record_reuses_caller_transaction_without_committing(pg_client):
    with dbmod.pg_transaction() as conn:
        shh.record("svc", "Healthy", ts="t", conn=conn)
        assert len(shh.history("svc", conn=conn)) == 1
        conn.rollback()
    assert count_rows(pg_client, "service_health_history") == 0


def test_history_accepts_readonly_transaction(pg_client):
    shh.record("svc", "Healthy", ts="t")
    with dbmod.pg_transaction(readonly=True) as conn:
        rows = shh.history("svc", conn=conn)
    assert [r["status"] for r in rows] == ["Healthy"]


def test_ts_defaults_to_empty_string(pg_client):
    """与 SQLite 版一致：ts 缺省写空串（表定义 ts NOT NULL）。"""
    shh.record("svc", "Healthy")
    assert shh.history("svc")[0]["ts"] == ""
