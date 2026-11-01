# -*- coding: utf-8 -*-
"""monitor_events.py 回归（Track K）：生命周期 / 幂等 / 折叠 / 窗口 / 保留 / 路由。

覆盖：
- 迁移 v10：空库直跑、重复幂等、旧库补齐、表列集与**部分唯一索引**；
- K1/K2 生命周期：opened → updated（幂等，不新开）→ resolved；恢复后再坏开新事件；
  unknown / Unknown 不造事件；恢复时没有活动事件是 no-op；
- 幂等的物理保证：绕过 observe() 直接 INSERT 第二条未恢复行会被唯一索引拒；
- K3 折叠：机器根因收同机服务、无根服务保持顶层、机器恢复后服务浮回顶层、重复调用不累加；
- K4 查询：默认 7 天窗口、**活动事件不被窗口裁掉**、kind/machine/severity/state 筛选、
  分页、impact_count / collapsed_by、非法参数退化；
- 保留策略：0 不清理、只删已恢复、活动事件永不删；
- 路由：/api/events 200 且不被 Beszel 同源代理吞掉；响应零 tailscale IP / 零凭据。
"""
import datetime
import os
import pathlib
import re
import sqlite3
import sys

import pytest
import yaml

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import db as dbmod  # noqa: E402
import machine_config as mc  # noqa: E402
import monitor_events as me  # noqa: E402

MACHINES_YAML = [
    {"id": "nas", "tailscale": "100.64.0.1"},
    {"id": "txy", "tailscale": "100.64.0.2"},
]


def _ts(offset_hours=0):
    moment = datetime.datetime(2026, 9, 7, 12, 0, 0, tzinfo=datetime.timezone.utc)
    return (moment + datetime.timedelta(hours=offset_hours)).strftime(me.TS_FORMAT)


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "console.db"
    dbmod.run_migrations(str(path))
    return str(path)


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
def client(tmp_path, monkeypatch, registry):
    import app as console_app

    monkeypatch.setattr(console_app, "DB_PATH", str(tmp_path / "console.db"))
    monkeypatch.setattr(console_app, "ASSETS_PATH", str(tmp_path / "assets.yaml"))
    console_app.init_db()
    return console_app.app.test_client()


@pytest.fixture(autouse=True)
def _clean_retention_env(monkeypatch):
    monkeypatch.delenv("CONSOLE_EVENT_RETENTION_DAYS", raising=False)


# ---------------------------------------------------------------------------
# 迁移 v10
# ---------------------------------------------------------------------------


def test_migration_v10_on_empty_db(tmp_path):
    applied = dbmod.run_migrations(str(tmp_path / "fresh.db"))
    assert 10 in applied
    assert applied == [version for version, _desc, _sql in dbmod.MIGRATIONS]


def test_migration_v10_idempotent(tmp_path):
    path = str(tmp_path / "again.db")
    first = dbmod.run_migrations(path)
    assert dbmod.run_migrations(path) == first
    conn = sqlite3.connect(path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 10"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_migration_v10_on_old_db_adds_it(tmp_path):
    """旧库（v1–v9 已应用、v10 未应用）补 v10，不动既有表。"""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,"
            " applied_at TEXT NOT NULL DEFAULT (datetime('now')),"
            " description TEXT NOT NULL DEFAULT '')"
        )
        for version in range(1, 10):
            conn.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                (version, f"seed-{version}"),
            )
        conn.execute("CREATE TABLE renewals (id TEXT PRIMARY KEY, name TEXT NOT NULL)")
        conn.commit()
    finally:
        conn.close()
    assert 10 in dbmod.run_migrations(path)
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
    assert "monitor_events" in tables
    assert "renewals" in tables


def test_migration_v10_columns(db_path):
    conn = sqlite3.connect(db_path)
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(monitor_events)").fetchall()
        }
    finally:
        conn.close()
    assert columns == {
        "id",
        "fingerprint",
        "kind",
        "source",
        "target",
        "machine",
        "severity",
        "title",
        "detail",
        "opened_at",
        "updated_at",
        "resolved_at",
    }


def test_migration_v10_partial_unique_index(db_path):
    """幂等靠这个部分唯一索引物理保证，不是靠调用方自觉。"""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index'"
            " AND name = 'idx_monitor_events_open'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert "WHERE resolved_at IS NULL" in row[0]


# ---------------------------------------------------------------------------
# K1 / K2 生命周期
# ---------------------------------------------------------------------------


def test_failure_opens_event(db_path):
    assert (
        me.observe_machine("nas", "down", detail="tcp refused", ts=_ts(), db_path=db_path)
        == "opened"
    )
    events = me.active_events(db_path=db_path)
    assert len(events) == 1
    assert events[0]["kind"] == "machine"
    assert events[0]["target"] == "nas"
    assert events[0]["state"] == "active"
    assert events[0]["resolved_at"] is None
    assert events[0]["detail"] == "tcp refused"


def test_repeated_failure_updates_not_reopens(db_path):
    """同一未恢复事件幂等更新（K1）：detail 刷新，opened_at 不动，只有一行。"""
    me.observe_machine("nas", "down", detail="first", ts=_ts(), db_path=db_path)
    assert (
        me.observe_machine("nas", "down", detail="second", ts=_ts(1), db_path=db_path)
        == "updated"
    )
    assert (
        me.observe_machine("nas", "down", detail="third", ts=_ts(2), db_path=db_path)
        == "updated"
    )
    events = me.active_events(db_path=db_path)
    assert len(events) == 1
    assert events[0]["opened_at"] == _ts()
    assert events[0]["updated_at"] == _ts(2)
    assert events[0]["detail"] == "third"


def test_recovery_resolves_same_event(db_path):
    me.observe_machine("nas", "down", ts=_ts(), db_path=db_path)
    assert me.observe_machine("nas", "up", ts=_ts(3), db_path=db_path) == "resolved"
    assert me.active_events(db_path=db_path) == []
    result = me.list_events(db_path=db_path, now=_parse_now(3))
    assert result["total"] == 1
    assert result["events"][0]["state"] == "resolved"
    # 关闭时记录持续时长（3 小时）
    assert result["events"][0]["duration_sec"] == 3 * 3600


def _parse_now(offset_hours=0):
    return datetime.datetime(
        2026, 9, 7, 12, 0, 0, tzinfo=datetime.timezone.utc
    ) + datetime.timedelta(hours=offset_hours)


def test_recovery_without_open_event_is_noop(db_path):
    """一直好着的目标每轮都报 up，不该写出任何行。"""
    assert me.observe_machine("nas", "up", ts=_ts(), db_path=db_path) is None
    assert me.list_events(db_path=db_path)["total"] == 0


def test_fail_recover_fail_leaves_two_events(db_path):
    me.observe_machine("nas", "down", ts=_ts(), db_path=db_path)
    me.observe_machine("nas", "up", ts=_ts(1), db_path=db_path)
    assert me.observe_machine("nas", "down", ts=_ts(2), db_path=db_path) == "opened"
    result = me.list_events(db_path=db_path, now=_parse_now(2))
    assert result["total"] == 2
    assert {event["state"] for event in result["events"]} == {"active", "resolved"}


def test_unknown_probe_makes_no_event(db_path):
    assert me.observe_machine("nas", "unknown", ts=_ts(), db_path=db_path) is None
    assert me.observe_service("svc", "Unknown", ts=_ts(), db_path=db_path) is None
    assert me.observe("machine", "nas", None, db_path=db_path) is None
    assert me.list_events(db_path=db_path)["total"] == 0


def test_blank_target_and_bad_kind_rejected(db_path):
    assert me.observe("machine", "  ", False, db_path=db_path) is None
    assert me.observe("galaxy", "nas", False, db_path=db_path) is None
    assert me.observe_machine("", "down", db_path=db_path) is None
    assert me.observe_service("", "Unhealthy", db_path=db_path) is None
    assert me.list_events(db_path=db_path)["total"] == 0


def test_service_event_carries_machine(db_path):
    me.observe_service(
        "svc-a", "Unhealthy", machine="NAS", detail="500", ts=_ts(), db_path=db_path
    )
    event = me.active_events(db_path=db_path)[0]
    assert event["kind"] == "service"
    assert event["machine"] == "nas"  # 归一化小写，与 machines.yaml 的 id 对齐
    assert event["title"] == "svc-a 健康检查失败"


def test_second_open_row_rejected_by_unique_index(db_path):
    """绕过 observe() 直接插第二条未恢复行 → 被部分唯一索引拒。"""
    me.observe_machine("nas", "down", ts=_ts(), db_path=db_path)
    conn = dbmod.get_connection(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO monitor_events (fingerprint, kind, target, opened_at)"
                " VALUES (?,?,?,?)",
                ("machine:nas", "machine", "nas", _ts(1)),
            )
    finally:
        conn.close()


class _ReplayCursor:
    """回放已取尽的结果集：让 observe() 在「对手线程插队」之后仍读到旧快照。"""

    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class _RacingConnection:
    """在 observe() 的 SELECT 之后、INSERT 之前，替对手线程抢先开一条同名事件。"""

    def __init__(self, conn, db_path):
        self._conn = conn
        self._db_path = db_path
        self._raced = False

    def execute(self, sql, *args):
        if not self._raced and sql.lstrip().upper().startswith("SELECT ID FROM MONITOR_EVENTS"):
            self._raced = True
            rows = self._conn.execute(sql, *args).fetchall()  # 我方读到「没有活动事件」
            other = sqlite3.connect(self._db_path, timeout=5)
            try:
                other.execute(
                    "INSERT INTO monitor_events (fingerprint, kind, target, opened_at)"
                    " VALUES (?,?,?,?)",
                    ("machine:nas", "machine", "nas", _ts()),
                )
                other.commit()
            finally:
                other.close()
            return _ReplayCursor(rows)
        return self._conn.execute(sql, *args)

    def __getattr__(self, item):
        return getattr(self._conn, item)


def test_concurrent_open_degrades_to_updated(db_path, monkeypatch):
    """SELECT→INSERT 之间被别人抢先开了同一事件 → 回 updated，不往上抛。

    Flask 多线程下两个并发 `/api/services?force=1` 就能撞上：唯一索引挡住了
    重复行，但 IntegrityError 不能冒到探测主线（那会连累 /api/machines）。
    """
    real_connect = dbmod.get_connection
    monkeypatch.setattr(
        dbmod, "get_connection", lambda path=None: _RacingConnection(real_connect(path), db_path)
    )
    assert me.observe_machine("nas", "down", ts=_ts(1), db_path=db_path) == "updated"
    monkeypatch.undo()
    # 只留下对手插的那一条，没有重复行
    assert _row_count(db_path) == 1
    assert me.active_events(db_path=db_path)[0]["opened_at"] == _ts()


def test_open_row_allowed_after_resolve(db_path):
    """已恢复的历史行不占唯一性，同一目标能反复开闭。"""
    for hour in range(3):
        me.observe_machine("nas", "down", ts=_ts(hour * 2), db_path=db_path)
        me.observe_machine("nas", "up", ts=_ts(hour * 2 + 1), db_path=db_path)
    assert me.list_events(db_path=db_path, now=_parse_now(6))["total"] == 3


def test_reopen_updates_machine_and_severity(db_path):
    """服务换机器后，活动事件的归属跟随最新观测自愈（opened_at 不动）。"""
    me.observe_service("svc-a", "Unhealthy", machine="nas", ts=_ts(), db_path=db_path)
    me.observe_service("svc-a", "Unhealthy", machine="txy", ts=_ts(1), db_path=db_path)
    event = me.active_events(db_path=db_path)[0]
    assert event["machine"] == "txy"
    assert event["opened_at"] == _ts()


# ---------------------------------------------------------------------------
# K3 影响折叠
# ---------------------------------------------------------------------------


def test_collapse_groups_services_under_machine(db_path):
    me.observe_machine("nas", "down", ts=_ts(), db_path=db_path)
    me.observe_service("svc-a", "Unhealthy", machine="nas", ts=_ts(), db_path=db_path)
    me.observe_service("svc-b", "Unhealthy", machine="nas", ts=_ts(), db_path=db_path)
    top = me.collapse(me.active_events(db_path=db_path))
    assert len(top) == 1
    assert top[0]["kind"] == "machine"
    assert top[0]["impact_count"] == 2
    assert {child["target"] for child in top[0]["children"]} == {"svc-a", "svc-b"}


def test_collapse_keeps_service_without_machine_root(db_path):
    """机器好着但这个服务坏了 —— 独立故障，不该被折叠掉。"""
    me.observe_machine("nas", "down", ts=_ts(), db_path=db_path)
    me.observe_service("svc-x", "Unhealthy", machine="txy", ts=_ts(), db_path=db_path)
    top = me.collapse(me.active_events(db_path=db_path))
    assert len(top) == 2
    lone = [event for event in top if event["kind"] == "service"][0]
    assert lone["target"] == "svc-x"
    assert lone["impact_count"] == 0


def test_collapse_releases_children_after_machine_recovers(db_path):
    """主机恢复后仍失败的服务浮回顶层（设计 §5.4），表里没有 parent 列要回填。"""
    me.observe_machine("nas", "down", ts=_ts(), db_path=db_path)
    me.observe_service("svc-a", "Unhealthy", machine="nas", ts=_ts(), db_path=db_path)
    assert len(me.collapse(me.active_events(db_path=db_path))) == 1
    me.observe_machine("nas", "up", ts=_ts(1), db_path=db_path)
    top = me.collapse(me.active_events(db_path=db_path))
    assert len(top) == 1
    assert top[0]["kind"] == "service"
    assert top[0]["impact_count"] == 0


def test_collapse_is_repeatable(db_path):
    """重复调用不累加 children（就地改写但每次重置）。"""
    me.observe_machine("nas", "down", ts=_ts(), db_path=db_path)
    me.observe_service("svc-a", "Unhealthy", machine="nas", ts=_ts(), db_path=db_path)
    events = me.active_events(db_path=db_path)
    me.collapse(events)
    top = me.collapse(events)
    assert top[0]["impact_count"] == 1


def test_collapse_on_empty_list():
    assert me.collapse([]) == []


# ---------------------------------------------------------------------------
# K4 查询：窗口 / 筛选 / 分页
# ---------------------------------------------------------------------------


def test_window_hides_old_resolved_events(db_path):
    me.observe_machine("nas", "down", ts=_ts(-24 * 30), db_path=db_path)
    me.observe_machine("nas", "up", ts=_ts(-24 * 29), db_path=db_path)
    assert me.list_events(days=7, db_path=db_path, now=_parse_now())["total"] == 0
    assert me.list_events(days=90, db_path=db_path, now=_parse_now())["total"] == 1


def test_active_event_survives_window(db_path):
    """开了 30 天还没恢复的故障，在「最近 7 天」里必须仍然看得见。"""
    me.observe_machine("nas", "down", ts=_ts(-24 * 30), db_path=db_path)
    result = me.list_events(days=7, db_path=db_path, now=_parse_now())
    assert result["total"] == 1
    assert result["events"][0]["state"] == "active"
    assert result["active_count"] == 1


def test_filters_by_kind_machine_severity_state(db_path):
    me.observe_machine("nas", "down", ts=_ts(), db_path=db_path)
    me.observe_service("svc-a", "Unhealthy", machine="txy", ts=_ts(), db_path=db_path)
    me.observe_service("svc-b", "Unhealthy", machine="txy", ts=_ts(), db_path=db_path)
    me.observe_service("svc-b", "Healthy", ts=_ts(1), db_path=db_path)
    now = _parse_now(1)

    assert me.list_events(kind="machine", db_path=db_path, now=now)["total"] == 1
    assert me.list_events(kind="service", db_path=db_path, now=now)["total"] == 2
    assert me.list_events(machine="TXY", db_path=db_path, now=now)["total"] == 2
    assert me.list_events(severity="error", db_path=db_path, now=now)["total"] == 3
    assert me.list_events(severity="warn", db_path=db_path, now=now)["total"] == 0
    assert me.list_events(state="active", db_path=db_path, now=now)["total"] == 2
    assert me.list_events(state="resolved", db_path=db_path, now=now)["total"] == 1
    assert me.list_events(state="all", db_path=db_path, now=now)["total"] == 3


def test_pagination(db_path):
    for index in range(5):
        me.observe_service(
            f"svc-{index}", "Unhealthy", machine="nas", ts=_ts(index), db_path=db_path
        )
    first = me.list_events(limit=2, offset=0, db_path=db_path, now=_parse_now(5))
    second = me.list_events(limit=2, offset=2, db_path=db_path, now=_parse_now(5))
    assert first["total"] == second["total"] == 5
    assert len(first["events"]) == len(second["events"]) == 2
    assert {event["id"] for event in first["events"]}.isdisjoint(
        {event["id"] for event in second["events"]}
    )


def test_active_events_sort_before_resolved(db_path):
    me.observe_service("old", "Unhealthy", machine="nas", ts=_ts(0), db_path=db_path)
    me.observe_service("new", "Unhealthy", machine="nas", ts=_ts(5), db_path=db_path)
    me.observe_service("new", "Healthy", ts=_ts(6), db_path=db_path)
    events = me.list_events(db_path=db_path, now=_parse_now(6))["events"]
    assert events[0]["state"] == "active"  # 更老但还没恢复的排前面


def test_impact_and_collapsed_by_markers(db_path):
    me.observe_machine("nas", "down", ts=_ts(), db_path=db_path)
    me.observe_service("svc-a", "Unhealthy", machine="nas", ts=_ts(), db_path=db_path)
    events = me.list_events(db_path=db_path, now=_parse_now())["events"]
    root = [event for event in events if event["kind"] == "machine"][0]
    child = [event for event in events if event["kind"] == "service"][0]
    assert root["impact_count"] == 1
    assert child["collapsed_by"] == "machine:nas"


def test_resolved_events_carry_no_collapse_markers(db_path):
    """折叠关系只在活动事件之间成立；历史条目平铺展示。"""
    me.observe_machine("nas", "down", ts=_ts(), db_path=db_path)
    me.observe_machine("nas", "up", ts=_ts(1), db_path=db_path)
    event = me.list_events(db_path=db_path, now=_parse_now(1))["events"][0]
    assert "impact_count" not in event
    assert "collapsed_by" not in event


def test_bad_query_params_degrade_to_defaults(db_path):
    result = me.list_events(
        days="abc", kind="galaxy", severity="fatal", state="weird",
        limit="lots", offset="-5", db_path=db_path, now=_parse_now(),
    )
    assert result["window_days"] == me.DEFAULT_WINDOW_DAYS
    assert result["limit"] == me.DEFAULT_LIMIT
    assert result["offset"] == 0


def test_query_params_clamped(db_path):
    result = me.list_events(days=9999, limit=99999, db_path=db_path, now=_parse_now())
    assert result["window_days"] == me.MAX_WINDOW_DAYS
    assert result["limit"] == me.MAX_LIMIT


def test_missing_table_returns_empty(tmp_path):
    """迁移没跑（或库损坏）时退化为空，不抛。"""
    path = str(tmp_path / "bare.db")
    sqlite3.connect(path).close()
    assert me.active_events(db_path=path) == []
    assert me.list_events(db_path=path)["total"] == 0
    assert me.prune(db_path=path, days=1) == 0


def test_bad_timestamp_row_does_not_break_listing(db_path):
    """手工塞进来的坏时间戳只让 duration 为 None，不打死整页。"""
    conn = dbmod.get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO monitor_events (fingerprint, kind, target, opened_at)"
            " VALUES (?,?,?,?)",
            ("machine:ghost", "machine", "ghost", "not-a-time"),
        )
        conn.commit()
    finally:
        conn.close()
    events = me.active_events(db_path=db_path)
    assert len(events) == 1
    assert events[0]["duration_sec"] is None


# ---------------------------------------------------------------------------
# 保留策略
# ---------------------------------------------------------------------------


def _row_count(db_path):
    """直接数行：比 list_events 更钝，但不受 90 天查询窗口影响。"""
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM monitor_events").fetchone()[0]
    finally:
        conn.close()


def test_retention_default_zero_keeps_everything(db_path):
    me.observe_machine("nas", "down", ts=_ts(-24 * 400), db_path=db_path)
    me.observe_machine("nas", "up", ts=_ts(-24 * 399), db_path=db_path)
    assert me.retention_days() == 0
    assert me.prune(db_path=db_path) == 0
    assert _row_count(db_path) == 1


def test_retention_env_is_read(monkeypatch):
    monkeypatch.setenv("CONSOLE_EVENT_RETENTION_DAYS", "30")
    assert me.retention_days() == 30
    monkeypatch.setenv("CONSOLE_EVENT_RETENTION_DAYS", "-7")
    assert me.retention_days() == 0
    monkeypatch.setenv("CONSOLE_EVENT_RETENTION_DAYS", "nope")
    assert me.retention_days() == 0


def test_prune_deletes_only_old_resolved(db_path):
    me.observe_machine("nas", "down", ts=_ts(-24 * 60), db_path=db_path)
    me.observe_machine("nas", "up", ts=_ts(-24 * 59), db_path=db_path)  # 老，已恢复
    me.observe_service("svc-a", "Unhealthy", machine="nas", ts=_ts(-1), db_path=db_path)
    me.observe_service("svc-a", "Healthy", ts=_ts(), db_path=db_path)  # 新，已恢复
    assert me.prune(db_path=db_path, days=30, now=_parse_now()) == 1
    remaining = me.list_events(days=90, db_path=db_path, now=_parse_now())
    assert remaining["total"] == 1
    assert remaining["events"][0]["target"] == "svc-a"


def test_prune_never_deletes_active(db_path):
    """还没恢复的故障不该因为「开得久」被清掉。"""
    me.observe_machine("nas", "down", ts=_ts(-24 * 400), db_path=db_path)
    assert me.prune(db_path=db_path, days=1, now=_parse_now()) == 0
    assert me.active_events(db_path=db_path)[0]["target"] == "nas"


def test_prune_zero_days_is_noop(db_path):
    me.observe_machine("nas", "down", ts=_ts(-24 * 400), db_path=db_path)
    me.observe_machine("nas", "up", ts=_ts(-24 * 399), db_path=db_path)
    assert me.prune(db_path=db_path, days=0, now=_parse_now()) == 0
    assert _row_count(db_path) == 1


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def test_events_endpoint_shape(client):
    response = client.get("/api/events")
    assert response.status_code == 200
    payload = response.get_json()
    assert set(payload) == {
        "events",
        "total",
        "limit",
        "offset",
        "active_count",
        "window_days",
        "generated_at",
    }
    assert payload["events"] == []
    assert payload["window_days"] == me.DEFAULT_WINDOW_DAYS


def test_events_endpoint_not_swallowed_by_beszel_proxy(client):
    """`/api/events` 必须返回 JSON，不能被同源反代吞成 HTML+200。

    B7-3 起它的归属改由 `plugins/audit` 的 manifest `api_prefixes` 声明，
    kernel 的 `CONSOLE_API_PATHS` 不再重复登记。断言因此落在**归属来源**与
    **实际响应**上，而不是某个常量的成员关系。
    """
    import app as console_app
    import plugin_runtime

    assert plugin_runtime.owner("/api/events") == "audit"
    assert console_app._is_console_api("/api/events")
    assert response_is_json(client.get("/api/events"))


def response_is_json(response):
    return response.headers.get("Content-Type", "").startswith("application/json")


def test_events_endpoint_filters_and_leaks_nothing(client, tmp_path):
    db = str(tmp_path / "console.db")
    me.observe_machine("nas", "down", detail="tcp 100.64.0.1:22 refused", ts=_ts(), db_path=db)
    me.observe_service("svc-a", "Unhealthy", machine="nas", ts=_ts(), db_path=db)

    payload = client.get("/api/events?kind=machine").get_json()
    assert payload["total"] == 1
    assert payload["events"][0]["impact_count"] == 1

    payload = client.get("/api/events?machine=nas&state=active&days=1&limit=1").get_json()
    assert payload["window_days"] == 1
    assert payload["limit"] == 1
    assert payload["active_count"] == 2

    # 安全红线：detail 落库前抹了 IPv4，事件页长期展示也不会漏 tailscale 地址
    body = client.get("/api/events").get_data(as_text=True)
    assert "100.64.0.1" not in body
    assert not re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", body)
    assert "SECRET" not in body.upper()
    assert not re.search(r"(?i)(token|password|api[_-]?key)\"\s*:", body)


# ---------------------------------------------------------------------------
# 家宽公网（kind=public）：独立事件，不当机器宕机、不作折叠根
# ---------------------------------------------------------------------------


def test_public_down_opens_warn_event(db_path):
    assert (
        me.observe_public(
            "US", "down", label="Oracle", detail="tcp us.sunxc.eu.cc:62037 timed out",
            ts=_ts(), db_path=db_path,
        )
        == "opened"
    )
    event = me.active_events(db_path=db_path)[0]
    assert event["kind"] == me.KIND_PUBLIC == "public"
    assert event["fingerprint"] == "public:us"
    assert event["machine"] == "us"
    assert event["source"] == me.SOURCE_PUBLIC_PROBE
    assert event["severity"] == "warn"
    assert event["title"] == "Oracle 家宽公网不通"


def test_public_title_falls_back_to_upper_id(db_path):
    me.observe_public("kr", "down", ts=_ts(), db_path=db_path)
    assert me.active_events(db_path=db_path)[0]["title"] == "KR 家宽公网不通"


def test_public_lifecycle_and_unknown(db_path):
    assert me.observe_public("us", "unknown", ts=_ts(), db_path=db_path) is None
    assert me.observe_public("", "down", ts=_ts(), db_path=db_path) is None
    assert me.observe_public("us", "up", ts=_ts(), db_path=db_path) is None
    me.observe_public("us", "down", ts=_ts(), db_path=db_path)
    assert me.observe_public("us", "down", ts=_ts(1), db_path=db_path) == "updated"
    assert me.observe_public("us", "up", ts=_ts(2), db_path=db_path) == "resolved"
    assert me.active_events(db_path=db_path) == []


def test_public_and_machine_events_are_independent(db_path):
    """同一台机器的公网事件与机器事件指纹不同，互不恢复对方。"""
    me.observe_machine("us", "down", ts=_ts(), db_path=db_path)
    me.observe_public("us", "down", ts=_ts(), db_path=db_path)
    me.observe_machine("us", "up", ts=_ts(1), db_path=db_path)
    active = me.active_events(db_path=db_path)
    assert [event["fingerprint"] for event in active] == ["public:us"]


def test_public_event_is_not_collapsed_under_machine_root(db_path):
    me.observe_machine("us", "down", ts=_ts(), db_path=db_path)
    me.observe_public("us", "down", ts=_ts(), db_path=db_path)
    me.observe_service("svc-a", "Unhealthy", machine="us", ts=_ts(), db_path=db_path)
    top = me.collapse(me.active_events(db_path=db_path))
    assert {event["fingerprint"] for event in top} == {"machine:us", "public:us"}
    root = [event for event in top if event["kind"] == "machine"][0]
    assert root["impact_count"] == 1  # 只收服务，不收公网事件


def test_public_event_reclaimed_after_probe_config_removed(db_path, registry, monkeypatch):
    """台账撤掉 public_probe 后，残留的活动公网事件由后台一轮关闭，不永久悬挂。"""
    import probe_cache as pc

    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authority")
    monkeypatch.setattr(pc, "check_tcp", lambda *a, **kw: pytest.fail("未配置的机器不应探测"))
    me.observe_public("nas", "down", ts=_ts(), db_path=db_path)
    assert pc.probe_all_public(db_path=db_path) == 0
    assert me.active_events(db_path=db_path) == []
    resolved = me.list_events(db_path=db_path, now=_parse_now(1))["events"][0]
    assert resolved["fingerprint"] == "public:nas"
    assert resolved["state"] == "resolved"
    # 恢复时不改写这次故障的原因
    assert resolved["title"] == "NAS 家宽公网不通"


def test_list_events_filters_public_and_marks_no_collapse(db_path):
    me.observe_machine("us", "down", ts=_ts(), db_path=db_path)
    me.observe_public("us", "down", ts=_ts(), db_path=db_path)
    result = me.list_events(kind="public", db_path=db_path, now=_parse_now())
    assert result["total"] == 1
    event = result["events"][0]
    assert event["kind"] == "public"
    assert "collapsed_by" not in event
    assert "impact_count" not in event
    assert me.list_events(kind="machine", db_path=db_path, now=_parse_now())["total"] == 1
    assert me.list_events(severity="warn", db_path=db_path, now=_parse_now())["total"] == 1


def test_detail_ipv4_is_scrubbed_before_storage(db_path):
    """机器探活的 msg 形如 `tcp <tailscale_ip>:22 refused` —— IP 不许进库。"""
    me.observe_machine(
        "nas", "down", detail="tcp 100.64.0.10:22 connection refused",
        ts=_ts(), db_path=db_path,
    )
    detail = me.active_events(db_path=db_path)[0]["detail"]
    assert detail == "tcp ***:22 connection refused"  # 端口与原因保留，IP 没了
    assert "100.64.0.10" not in detail


def test_scrub_detail_handles_multiple_and_none():
    assert me.scrub_detail("a 10.0.0.1 b 192.168.1.2 c") == "a *** b *** c"
    assert me.scrub_detail(None) == ""
    assert me.scrub_detail("Status 500 body mismatch") == "Status 500 body mismatch"
    # 版本号不是 IP（只有四段才抹）
    assert me.scrub_detail("nginx 1.27.3 down") == "nginx 1.27.3 down"


def test_events_endpoint_bad_params_do_not_500(client):
    for query in ("days=abc", "limit=-1", "offset=xyz", "state=nope", "kind=galaxy"):
        response = client.get(f"/api/events?{query}")
        assert response.status_code == 200
