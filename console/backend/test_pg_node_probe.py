# -*- coding: utf-8 -*-
"""node_probe 的 PG 路径回归（PG-02）。

对应 SQLite 版 `test_node_probe.py` 的业务断言，逐条在真 PG16 上复验：
保留期裁剪、旧→新顺序、机器隔离、down 的 NULL 延迟、非法 status/machine 拒绝。
另外覆盖 PG 特有的两点：`conn` 复用（不拆批次事务）与只读事务读取。
"""
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import db as dbmod  # noqa: E402
import node_probe as np  # noqa: E402

# 夹具必须 import 进本模块命名空间，pytest 才会发现（它们定义在 pg02_support.py）。
from pg02_support import count_rows  # noqa: E402

# 夹具通过插件注册加载：直接 import 同名夹具会被 pyflakes 判 F811（测试参数同名）。
pytest_plugins = ("pg02_support",)



def test_history_is_oldest_first(pg_client):
    for i in range(3):
        np.record("kr", "up", latency_ms=float(i), ts=f"2026-08-29T00:0{i}:00Z")
    rows = np.history("kr")
    assert [r["latency_ms"] for r in rows] == [0.0, 1.0, 2.0]


def test_history_limit_returns_most_recent(pg_client):
    for i in range(30):
        np.record("kr", "up", latency_ms=float(i), ts="t")
    rows = np.history("kr", limit=5)
    assert [r["latency_ms"] for r in rows] == [25.0, 26.0, 27.0, 28.0, 29.0]


def test_machines_do_not_mix(pg_client):
    np.record("kr", "up", 1.0, "t")
    np.record("sg", "down", None, "t")
    assert [r["status"] for r in np.history("kr")] == ["up"]
    assert [r["status"] for r in np.history("sg")] == ["down"]


def test_retention_prunes_old_rows(pg_client, monkeypatch):
    monkeypatch.setattr(np, "RETAIN_PER_MACHINE", 5)
    for i in range(12):
        np.record("kr", "up", float(i), "t")
    assert count_rows(pg_client, "probe_history", "machine = %s", ("kr",)) == 5
    assert [r["latency_ms"] for r in np.history("kr")] == [7, 8, 9, 10, 11]


def test_retention_is_per_machine(pg_client, monkeypatch):
    """裁剪只清同一台机器：另一台的历史不能被顺带删掉。"""
    monkeypatch.setattr(np, "RETAIN_PER_MACHINE", 3)
    for i in range(5):
        np.record("kr", "up", float(i), "t")
        np.record("sg", "up", float(i), "t")
    assert count_rows(pg_client, "probe_history", "machine = %s", ("kr",)) == 3
    assert count_rows(pg_client, "probe_history", "machine = %s", ("sg",)) == 3


def test_down_row_keeps_null_latency(pg_client):
    np.record("kr", "down", None, "t")
    assert np.history("kr")[0]["latency_ms"] is None


@pytest.mark.parametrize("status", ["", "unknown", "UP", None])
def test_only_up_down_are_recorded(pg_client, status):
    assert np.record("kr", status, 1.0, "t") is False
    assert np.history("kr") == []


def test_blank_machine_ignored(pg_client):
    assert np.record("", "up", 1.0, "t") is False
    assert np.history("", ) == []


def test_machine_name_is_normalised(pg_client):
    """与 SQLite 版同约定：机器 id 统一小写去空白，避免 'KR' 和 'kr' 分成两条。"""
    np.record("  KR  ", "up", 1.0, "t")
    assert count_rows(pg_client, "probe_history", "machine = %s", ("kr",)) == 1
    assert len(np.history("kr")) == 1


def test_record_reuses_caller_transaction_without_committing(pg_client):
    """`conn` 传入时不得自行提交：调用方回滚后该行必须消失（批次原子性）。"""
    with dbmod.pg_transaction() as conn:
        np.record("kr", "up", 1.0, "t", conn=conn)
        rows = np.history("kr", conn=conn)
        assert len(rows) == 1
        conn.rollback()
    assert count_rows(pg_client, "probe_history") == 0


def test_history_accepts_readonly_transaction(pg_client):
    np.record("kr", "up", 1.0, "t")
    with dbmod.pg_transaction(readonly=True) as conn:
        rows = np.history("kr", conn=conn)
    assert [r["status"] for r in rows] == ["up"]


def test_history_limit_is_clamped(pg_client):
    np.record("kr", "up", 1.0, "t")
    assert len(np.history("kr", limit=0)) == 1
    assert len(np.history("kr", limit="abc")) == 1
