# -*- coding: utf-8 -*-
"""node_probe 探测时序回归（E4）。"""
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import db  # noqa: E402
import node_probe as np  # noqa: E402


@pytest.fixture()
def dbp(tmp_path):
    path = str(tmp_path / "console.db")
    db.run_migrations(path)
    return path


def test_history_is_oldest_first(dbp):
    for i in range(3):
        np.record("kr", "up", latency_ms=float(i), ts=f"2026-08-29T00:0{i}:00Z", db_path=dbp)
    rows = np.history("kr", db_path=dbp)
    assert [r["latency_ms"] for r in rows] == [0.0, 1.0, 2.0]


def test_history_limit_returns_most_recent(dbp):
    for i in range(30):
        np.record("kr", "up", latency_ms=float(i), ts="t", db_path=dbp)
    rows = np.history("kr", limit=5, db_path=dbp)
    assert [r["latency_ms"] for r in rows] == [25.0, 26.0, 27.0, 28.0, 29.0]


def test_machines_do_not_mix(dbp):
    np.record("kr", "up", 1.0, "t", db_path=dbp)
    np.record("sg", "down", None, "t", db_path=dbp)
    assert [r["status"] for r in np.history("kr", db_path=dbp)] == ["up"]
    assert [r["status"] for r in np.history("sg", db_path=dbp)] == ["down"]


def test_retention_prunes_old_rows(dbp, monkeypatch):
    monkeypatch.setattr(np, "RETAIN_PER_MACHINE", 5)
    for i in range(12):
        np.record("kr", "up", float(i), "t", db_path=dbp)
    conn = db.get_connection(dbp)
    try:
        kept = conn.execute(
            "SELECT COUNT(*) AS c FROM probe_history WHERE machine = 'kr'"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert kept == 5
    assert [r["latency_ms"] for r in np.history("kr", db_path=dbp)] == [7, 8, 9, 10, 11]


def test_down_row_keeps_null_latency(dbp):
    np.record("kr", "down", None, "t", db_path=dbp)
    assert np.history("kr", db_path=dbp)[0]["latency_ms"] is None


@pytest.mark.parametrize("status", ["", "unknown", "UP", None])
def test_only_up_down_are_recorded(dbp, status):
    assert np.record("kr", status, 1.0, "t", db_path=dbp) is False
    assert np.history("kr", db_path=dbp) == []


def test_blank_machine_ignored(dbp):
    assert np.record("", "up", 1.0, "t", db_path=dbp) is False
    assert np.history("", db_path=dbp) == []
