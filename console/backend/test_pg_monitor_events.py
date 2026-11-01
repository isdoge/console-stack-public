# -*- coding: utf-8 -*-
"""monitor_events 的 PG 路径回归（PG-02）。

在真 PG16 上复验 SQLite 版锁住的业务语义：生命周期三态、部分唯一索引幂等、
折叠、窗口/筛选/分页、保留策略。另加 PG 专有的两条：

- `ON CONFLICT (fingerprint) WHERE resolved_at IS NULL DO NOTHING` 在**真并发**下
  只留一条活动事件，且冲突方回 `updated`（事务不中止）；
- 连接在冲突后仍可用（PG 事务内约束冲突会中止整个事务，这是 SQLite 没有的坑）。
"""
import datetime
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import db as dbmod  # noqa: E402
import monitor_events as me  # noqa: E402
from pg02_support import count_rows, fetch_all  # noqa: E402

# 夹具通过插件注册加载：直接 import 同名夹具会被 pyflakes 判 F811（测试参数同名）。
pytest_plugins = ("pg02_support",)



def _ts(offset_hours=0):
    moment = datetime.datetime(2026, 9, 7, 12, 0, 0, tzinfo=datetime.timezone.utc)
    return (moment + datetime.timedelta(hours=offset_hours)).strftime(me.TS_FORMAT)


def _parse_now(offset_hours=0):
    moment = datetime.datetime(2026, 9, 7, 12, 0, 0, tzinfo=datetime.timezone.utc)
    return moment + datetime.timedelta(hours=offset_hours)


@pytest.fixture(autouse=True)
def _clean_retention_env(monkeypatch):
    monkeypatch.delenv("CONSOLE_EVENT_RETENTION_DAYS", raising=False)


# ---- 生命周期 ---------------------------------------------------------------


def test_failure_opens_event(pg_client):
    assert me.observe_machine("nas", "down", label="NAS", ts=_ts()) == "opened"
    event = me.active_events()[0]
    assert event["kind"] == me.KIND_MACHINE
    assert event["target"] == "nas"
    assert event["opened_at"] == _ts()
    assert event["resolved_at"] is None
    assert event["state"] == "active"


def test_repeated_failure_updates_not_reopens(pg_client):
    me.observe_machine("nas", "down", detail="first", ts=_ts(0))
    assert me.observe_machine("nas", "down", detail="second", ts=_ts(1)) == "updated"
    assert count_rows(pg_client, "monitor_events") == 1
    assert me.active_events()[0]["detail"] == "second"
    assert me.active_events()[0]["opened_at"] == _ts(0)


def test_recovery_resolves_same_event(pg_client):
    me.observe_machine("nas", "down", ts=_ts(0))
    assert me.observe_machine("nas", "up", ts=_ts(2)) == "resolved"
    assert me.active_events() == []
    row = fetch_all(pg_client, "SELECT * FROM monitor_events")[0]
    assert row["resolved_at"] == _ts(2)
    assert row["opened_at"] == _ts(0)


def test_recovery_without_open_event_is_noop(pg_client):
    assert me.observe_machine("nas", "up", ts=_ts()) is None
    assert count_rows(pg_client, "monitor_events") == 0


def test_fail_recover_fail_leaves_two_events(pg_client):
    me.observe_machine("nas", "down", ts=_ts(0))
    me.observe_machine("nas", "up", ts=_ts(1))
    me.observe_machine("nas", "down", ts=_ts(2))
    assert count_rows(pg_client, "monitor_events") == 2
    assert len(me.active_events()) == 1


def test_unknown_probe_makes_no_event(pg_client):
    assert me.observe_machine("nas", "unknown", ts=_ts()) is None
    assert count_rows(pg_client, "monitor_events") == 0


def test_blank_target_and_bad_kind_rejected(pg_client):
    assert me.observe(me.KIND_MACHINE, "", False, ts=_ts()) is None
    assert me.observe("nope", "nas", False, ts=_ts()) is None
    assert count_rows(pg_client, "monitor_events") == 0


def test_service_event_carries_machine(pg_client):
    me.observe_service("caddy", "Unhealthy", machine="nas", detail="Status 500", ts=_ts())
    event = me.active_events()[0]
    assert event["kind"] == me.KIND_SERVICE
    assert event["machine"] == "nas"
    assert event["title"] == "caddy 健康检查失败"


# ---- 幂等的物理保证 ---------------------------------------------------------


def test_second_open_row_rejected_by_unique_index(pg_client):
    """绕过 observe() 直接插第二条未恢复行 → 被部分唯一索引拒。"""
    me.observe_machine("nas", "down", ts=_ts())
    with pytest.raises(Exception) as excinfo:
        with dbmod.pg_transaction() as conn:
            conn.execute(
                "INSERT INTO monitor_events (fingerprint, kind, target, opened_at)"
                " VALUES (%s,%s,%s,%s)",
                ("machine:nas", "machine", "nas", _ts(1)),
            )
    # 约束冲突必须映射成 ConcurrentConflict（HTTP 409），不是裸驱动异常
    assert type(excinfo.value).__name__ == "ConcurrentConflict"
    assert count_rows(pg_client, "monitor_events") == 1


def test_concurrent_open_degrades_to_updated(pg_client, monkeypatch):
    """两个并发观测都读到「没有活动事件」时，后到的必须回 updated 而不是抛错。

    用 monkeypatch 在 `observe()` 的 SELECT 之后插入一条对手事务的提交，精确复现
    Flask 多线程下两个并发 `/api/services?force=1` 的窗口。
    """
    real_execute = dbmod.pg_transaction

    class _RacingCursor:
        def __init__(self, cursor):
            self._cursor = cursor
            self.rowcount = cursor.rowcount

        def fetchone(self):
            return self._cursor.fetchone()

    class _RacingConn:
        def __init__(self, conn, raced):
            self._conn = conn
            self._raced = raced

        def execute(self, sql, params=None, *args):
            if (
                not self._raced[0]
                and sql.lstrip().upper().startswith("SELECT ID FROM MONITOR_EVENTS")
            ):
                self._raced[0] = True
                rows = self._conn.execute(sql, params).fetchall()
                # 对手线程抢先用**另一条连接**提交同名活动事件
                import pg_test_support

                other = pg_test_support.connect(pg_client["app_dsn"])
                try:
                    other.execute(
                        "INSERT INTO monitor_events (fingerprint, kind, target, opened_at)"
                        " VALUES (%s,%s,%s,%s)",
                        ("machine:nas", "machine", "nas", _ts()),
                    )
                    other.commit()
                finally:
                    other.close()

                class _Replay:
                    def __init__(self, rows):
                        self._rows = list(rows)

                    def fetchone(self):
                        return self._rows.pop(0) if self._rows else None

                return _Replay(rows)
            return _RacingCursor(self._conn.execute(sql, params))

        def __getattr__(self, item):
            return getattr(self._conn, item)

    import contextlib

    raced = [False]

    @contextlib.contextmanager
    def fake_tx(*, conn=None, db_path=None, lock=None, locks=None):
        with real_execute(lock=lock, locks=locks) as real_conn:
            yield _RacingConn(real_conn, raced)

    monkeypatch.setattr(me.sd, "write_tx", fake_tx)
    assert me.observe_machine("nas", "down", ts=_ts(1)) == "updated"
    monkeypatch.undo()
    # 只留下对手插的那一条，没有重复行
    assert count_rows(pg_client, "monitor_events") == 1
    assert me.active_events()[0]["opened_at"] == _ts()


def test_open_row_allowed_after_resolve(pg_client):
    for hour in range(3):
        me.observe_machine("nas", "down", ts=_ts(hour * 2))
        me.observe_machine("nas", "up", ts=_ts(hour * 2 + 1))
    assert me.list_events(now=_parse_now(6))["total"] == 3


# ---- 折叠 -------------------------------------------------------------------


def test_collapse_groups_services_under_machine(pg_client):
    me.observe_machine("nas", "down", ts=_ts(0))
    me.observe_service("caddy", "Unhealthy", machine="nas", ts=_ts(1))
    top = me.collapse(me.active_events())
    assert [e["kind"] for e in top] == [me.KIND_MACHINE]
    assert [c["target"] for c in top[0]["children"]] == ["caddy"]
    assert top[0]["impact_count"] == 1


def test_collapse_keeps_service_without_machine_root(pg_client):
    me.observe_service("caddy", "Unhealthy", machine="nas", ts=_ts())
    top = me.collapse(me.active_events())
    assert [e["kind"] for e in top] == [me.KIND_SERVICE]


def test_collapse_releases_children_after_machine_recovers(pg_client):
    me.observe_machine("nas", "down", ts=_ts(0))
    me.observe_service("caddy", "Unhealthy", machine="nas", ts=_ts(1))
    me.observe_machine("nas", "up", ts=_ts(2))
    top = me.collapse(me.active_events())
    assert [e["kind"] for e in top] == [me.KIND_SERVICE]


def test_collapse_is_repeatable(pg_client):
    me.observe_machine("nas", "down", ts=_ts(0))
    me.observe_service("caddy", "Unhealthy", machine="nas", ts=_ts(1))
    events = me.active_events()
    first = me.collapse(events)
    second = me.collapse(events)
    assert first[0]["impact_count"] == second[0]["impact_count"] == 1


def test_collapse_on_empty_list():
    assert me.collapse([]) == []


# ---- 查询 -------------------------------------------------------------------


def test_window_hides_old_resolved_events(pg_client):
    me.observe_machine("nas", "down", ts=_ts(-24 * 30))
    me.observe_machine("nas", "up", ts=_ts(-24 * 30 + 1))
    assert me.list_events(days=7, now=_parse_now(0))["total"] == 0


def test_active_event_survives_window(pg_client):
    me.observe_machine("nas", "down", ts=_ts(-24 * 30))
    assert me.list_events(days=7, now=_parse_now(0))["total"] == 1


def test_filters_by_kind_machine_severity_state(pg_client):
    me.observe_machine("nas", "down", ts=_ts(0))
    me.observe_service("caddy", "Unhealthy", machine="nas", ts=_ts(1))
    me.observe_service("api", "Unhealthy", machine="kr", ts=_ts(2))
    assert me.list_events(kind="service", now=_parse_now(3))["total"] == 2
    assert me.list_events(machine="kr", now=_parse_now(3))["total"] == 1
    assert me.list_events(state="active", now=_parse_now(3))["total"] == 3
    assert me.list_events(state="resolved", now=_parse_now(3))["total"] == 0


def test_pagination(pg_client):
    for i in range(5):
        me.observe(me.KIND_MACHINE, f"m{i}", False, ts=_ts(i))
    page = me.list_events(limit=2, offset=0, now=_parse_now(6))
    assert page["total"] == 5
    assert len(page["events"]) == 2
    assert page["limit"] == 2 and page["offset"] == 0


def test_active_events_sort_before_resolved(pg_client):
    me.observe_machine("old", "down", ts=_ts(0))
    me.observe_machine("old", "up", ts=_ts(1))
    me.observe_machine("new", "down", ts=_ts(2))
    events = me.list_events(now=_parse_now(3))["events"]
    assert events[0]["target"] == "new"
    assert events[0]["state"] == "active"


def test_impact_and_collapsed_by_markers(pg_client):
    me.observe_machine("nas", "down", ts=_ts(0))
    me.observe_service("caddy", "Unhealthy", machine="nas", ts=_ts(1))
    events = me.list_events(now=_parse_now(2))["events"]
    by_kind = {e["kind"]: e for e in events}
    assert by_kind[me.KIND_MACHINE]["impact_count"] == 1
    assert by_kind[me.KIND_SERVICE]["collapsed_by"] == "machine:nas"


def test_resolved_events_carry_no_collapse_markers(pg_client):
    me.observe_machine("nas", "down", ts=_ts(0))
    me.observe_machine("nas", "up", ts=_ts(1))
    events = me.list_events(now=_parse_now(2))["events"]
    assert "impact_count" not in events[0]
    assert "collapsed_by" not in events[0]


def test_bad_query_params_degrade_to_defaults(pg_client):
    result = me.list_events(
        days="x", kind="bogus", state="bogus", limit="y", offset="z", now=_parse_now(0)
    )
    assert result["window_days"] == me.DEFAULT_WINDOW_DAYS
    assert result["limit"] == me.DEFAULT_LIMIT
    assert result["offset"] == 0


def test_query_params_clamped(pg_client):
    result = me.list_events(days=10**6, limit=10**6, offset=-5, now=_parse_now(0))
    assert result["window_days"] == me.MAX_WINDOW_DAYS
    assert result["limit"] == me.MAX_LIMIT
    assert result["offset"] == 0


def test_bad_timestamp_row_does_not_break_listing(pg_client):
    """手工塞进来的坏时间戳只让 duration 为 None，不打死整页。"""
    with dbmod.pg_transaction() as conn:
        conn.execute(
            "INSERT INTO monitor_events (fingerprint, kind, target, opened_at)"
            " VALUES (%s,%s,%s,%s)",
            ("machine:bad", "machine", "bad", "not-a-timestamp"),
        )
    events = me.list_events(now=_parse_now(0))["events"]
    assert events[0]["duration_sec"] is None


# ---- 保留策略 ---------------------------------------------------------------


def test_retention_default_zero_keeps_everything(pg_client):
    me.observe_machine("nas", "down", ts=_ts(-24 * 30))
    me.observe_machine("nas", "up", ts=_ts(-24 * 30 + 1))
    assert me.prune() == 0
    assert count_rows(pg_client, "monitor_events") == 1


def test_retention_env_is_read(monkeypatch):
    monkeypatch.setenv("CONSOLE_EVENT_RETENTION_DAYS", "30")
    assert me.retention_days() == 30
    monkeypatch.setenv("CONSOLE_EVENT_RETENTION_DAYS", "abc")
    assert me.retention_days() == 0


def test_prune_deletes_only_old_resolved(pg_client):
    me.observe_machine("old", "down", ts=_ts(-24 * 40))
    me.observe_machine("old", "up", ts=_ts(-24 * 39))
    me.observe_machine("fresh", "down", ts=_ts(0))
    assert me.prune(days=30, now=_parse_now(0)) == 1
    remaining = fetch_all(pg_client, "SELECT target FROM monitor_events")
    assert [row["target"] for row in remaining] == ["fresh"]


def test_prune_never_deletes_active(pg_client):
    me.observe_machine("nas", "down", ts=_ts(-24 * 100))
    assert me.prune(days=1, now=_parse_now(0)) == 0
    assert count_rows(pg_client, "monitor_events") == 1


def test_prune_zero_days_is_noop(pg_client):
    me.observe_machine("old", "down", ts=_ts(-24 * 40))
    me.observe_machine("old", "up", ts=_ts(-24 * 39))
    assert me.prune(days=0, now=_parse_now(0)) == 0


# ---- 家宽公网（kind=public）-------------------------------------------------


def test_public_down_opens_warn_event(pg_client):
    assert me.observe_public("us", "down", label="Oracle", ts=_ts()) == "opened"
    event = me.active_events()[0]
    assert event["kind"] == me.KIND_PUBLIC
    assert event["fingerprint"] == "public:us"
    assert event["severity"] == "warn"
    assert event["source"] == me.SOURCE_PUBLIC_PROBE
    assert event["title"] == "Oracle 家宽公网不通"
    assert me.observe_public("us", "down", ts=_ts(1)) == "updated"
    assert count_rows(pg_client, "monitor_events") == 1
    assert me.observe_public("us", "up", ts=_ts(2)) == "resolved"


def test_public_event_is_independent_and_not_collapsed(pg_client):
    me.observe_machine("us", "down", ts=_ts())
    me.observe_public("us", "down", ts=_ts())
    me.observe_service("svc-a", "Unhealthy", machine="us", ts=_ts())
    top = me.collapse(me.active_events())
    assert {event["fingerprint"] for event in top} == {"machine:us", "public:us"}
    listed = me.list_events(kind="public", now=_parse_now())
    assert listed["total"] == 1
    assert "collapsed_by" not in listed["events"][0]
    me.observe_machine("us", "up", ts=_ts(1))
    assert [event["fingerprint"] for event in me.active_events()] == [
        "public:us", "service:svc-a",
    ]


# ---- 脱敏 -------------------------------------------------------------------


def test_detail_ipv4_is_scrubbed_before_storage(pg_client):
    me.observe_machine(
        "nas", "down", detail="tcp 100.64.0.9:22 timed out", ts=_ts()
    )
    assert me.active_events()[0]["detail"] == "tcp ***:22 timed out"


def test_scrub_detail_handles_multiple_and_none():
    assert me.scrub_detail(None) == ""
    assert me.scrub_detail("a 1.2.3.4 b 5.6.7.8") == "a *** b ***"
