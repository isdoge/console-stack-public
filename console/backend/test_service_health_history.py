# -*- coding: utf-8 -*-
"""service_health_history 服务健康时序回归（T12 分支实验）。"""
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import db  # noqa: E402
import service_health_history as shh  # noqa: E402


@pytest.fixture()
def dbp(tmp_path):
    path = str(tmp_path / "console.db")
    db.run_migrations(path)
    return path


def test_record_only_accepts_real_health_status(dbp):
    assert shh.record("svc", "Healthy", ts="2026-09-03T00:00:00Z", db_path=dbp) is True
    assert shh.record("svc", "Unhealthy", ts="2026-09-03T00:10:00Z", db_path=dbp) is True
    # Unknown（无 checker / 显式关闭）不落点
    assert shh.record("svc", "Unknown", ts="2026-09-03T00:20:00Z", db_path=dbp) is False
    assert shh.record("", "Healthy", db_path=dbp) is False
    rows = shh.history("svc", db_path=dbp)
    assert [r["status"] for r in rows] == ["Healthy", "Unhealthy"]


def test_history_oldest_first_and_retained(dbp):
    for i in range(15):
        status = "Healthy" if i % 2 == 0 else "Unhealthy"
        shh.record("svc", status, ts=f"2026-09-03T00:{i:02d}:00Z", db_path=dbp)
    rows = shh.history("svc", db_path=dbp)
    # 默认 12 条（BAND_SIZE），取最近的
    assert len(rows) == 12
    assert rows[0]["ts"] == "2026-09-03T00:03:00Z"
    assert rows[-1]["ts"] == "2026-09-03T00:14:00Z"


def test_services_are_isolated(dbp):
    shh.record("a", "Healthy", ts="2026-09-03T00:00:00Z", db_path=dbp)
    shh.record("b", "Unhealthy", ts="2026-09-03T00:01:00Z", db_path=dbp)
    assert [r["status"] for r in shh.history("a", db_path=dbp)] == ["Healthy"]
    assert [r["status"] for r in shh.history("b", db_path=dbp)] == ["Unhealthy"]


def test_empty_name_returns_empty_history(dbp):
    assert shh.history("", db_path=dbp) == []


def test_retain_cap_prunes_old_rows(dbp):
    for i in range(shh.RETAIN_PER_SERVICE + 10):
        shh.record("svc", "Healthy", ts=f"2026-09-03T0{i % 10}:{i % 60:02d}:00Z", db_path=dbp)
    rows = shh.history("svc", limit=200, db_path=dbp)
    assert len(rows) == shh.RETAIN_PER_SERVICE


def test_clear_wipes_stale_band_only_for_that_service(dbp):
    """关闭探测后旧红格不该残留（beszel-agent-aly 9 月故障样本挂到 10 月）。

    clear 只清自己：别的服务时序不受影响；空名安全返回。
    """
    shh.record("beszel-agent-aly", "Unhealthy", ts="2026-09-23T01:19:30Z", db_path=dbp)
    shh.record("beszel-agent-aly", "Unhealthy", ts="2026-09-23T02:00:00Z", db_path=dbp)
    shh.record("magpie", "Healthy", ts="2026-10-16T09:00:00Z", db_path=dbp)

    assert shh.clear("beszel-agent-aly", db_path=dbp) is True
    assert shh.history("beszel-agent-aly", db_path=dbp) == []
    # 别人的时序原样保留
    assert [r["status"] for r in shh.history("magpie", db_path=dbp)] == ["Healthy"]
    # 清空后再 record 正常工作（探测重新打开的场景）
    shh.record("beszel-agent-aly", "Healthy", ts="2026-10-16T10:00:00Z", db_path=dbp)
    assert [r["status"] for r in shh.history("beszel-agent-aly", db_path=dbp)] == ["Healthy"]
    # 空名不炸不写
    assert shh.clear("", db_path=dbp) is False
