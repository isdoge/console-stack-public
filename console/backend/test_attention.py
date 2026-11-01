# -*- coding: utf-8 -*-
"""attention.py 回归：mock 各来源，覆盖单类 / 混合 / 全正常。"""
import datetime
import pathlib
import sqlite3
import sys

import pytest
import yaml

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import attention  # noqa: E402
import feed_aggregator  # noqa: E402
import probe_cache  # noqa: E402
from plugin_imports import load_module  # noqa: E402

renewal_contributions = load_module("plugins.renewals.backend.contributions")
feed_contributions = load_module("plugins.feed.backend.contributions")



def test_inactive_renewals_do_not_alert(monkeypatch):
    expiry = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    rows = [{"name": status, "status": status, "expires_at": expiry}
            for status in ("active", "not_renewing", "ended")]
    monkeypatch.setattr(renewal_contributions.rn, "list_renewals", lambda **kwargs: rows)
    assert [item["title"] for item in renewal_contributions.attention_items(None)] == ["active 已过期"]


def test_renewal_dismiss_identity_distinguishes_rows_periods_and_phase(monkeypatch):
    today = datetime.date.today()
    old_date = (today - datetime.timedelta(days=1)).isoformat()
    rows = [
        {"id": "first", "name": "same", "expires_at": old_date},
        {"id": "second", "name": "same", "expires_at": old_date},
    ]
    monkeypatch.setattr(renewal_contributions.rn, "list_renewals", lambda **kwargs: rows)
    first, second = renewal_contributions.attention_items(None)
    assert first["dismiss_key"] != second["dismiss_key"]
    rows[0]["name"] = "renamed"
    assert renewal_contributions.attention_items(None)[0]["dismiss_key"] == first["dismiss_key"]
    rows[0]["expires_at"] = (today - datetime.timedelta(days=2)).isoformat()
    assert renewal_contributions.attention_items(None)[0]["dismiss_key"] != first["dismiss_key"]
    rows[0]["expires_at"] = today.isoformat()
    warning = renewal_contributions.attention_items(None)[0]
    assert warning["severity"] == "warn"
    assert warning["dismiss_key"] != first["dismiss_key"]


@pytest.fixture
def assets_file(tmp_path):
    path = tmp_path / "assets.yaml"
    path.write_text(
        yaml.safe_dump(
            [
                {"name": "svc-a", "machine": "nas", "type": "docker", "port": 1},
                {"name": "svc-b", "machine": "nas", "type": "docker", "port": 2},
            ],
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return str(path)


@pytest.fixture
def versions_db(tmp_path):
    db_path = tmp_path / "console.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE versions (
            svc_name TEXT PRIMARY KEY,
            local_version TEXT,
            upstream_version TEXT,
            behind_count INTEGER,
            update_status TEXT,
            recent_changes TEXT,
            last_checked TEXT
        )
        """
    )
    conn.commit()
    conn.close()
    return str(db_path)


@pytest.fixture(autouse=True)
def _clean_side_effects():
    probe_cache.invalidate()
    feed_aggregator._last_fetch_errors = []
    yield
    probe_cache.invalidate()
    feed_aggregator._last_fetch_errors = []




@pytest.fixture(autouse=True)
def _isolate_monitoring_coverage(monkeypatch):
    """既有 probe/version/流量测试不混入 PG 能力缺口提示。"""
    monkeypatch.setattr(attention, "_monitoring_items", lambda: [])


def _seed_version(db_path, name, local, upstream, behind=0, status="最新"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO versions
            (svc_name, local_version, upstream_version, behind_count, update_status)
        VALUES (?, ?, ?, ?, ?)
        """,
        (name, local, upstream, behind, status),
    )
    conn.commit()
    conn.close()


def test_probe_only(assets_file, versions_db):
    probe_cache._cache["svc-a"] = {
        "ts": 1,
        "status": "Unhealthy",
        "msg": "connection refused",
    }
    probe_cache._cache["svc-b"] = {"ts": 1, "status": "Healthy", "msg": "ok"}
    items = attention.collect_attention(assets_file, versions_db)
    assert len(items) == 1
    assert items[0]["kind"] == "probe"
    assert items[0]["severity"] == "error"
    assert items[0]["link"] == "/devices"
    assert "svc-a" in items[0]["title"]


def test_version_only(assets_file, versions_db):
    _seed_version(versions_db, "svc-b", "1.0.0", "1.1.0", behind=1, status="可更新")
    items = attention.collect_attention(assets_file, versions_db)
    assert len(items) == 1
    assert items[0]["kind"] == "version"
    assert items[0]["severity"] == "warn"
    assert "1.0.0 → 1.1.0" in items[0]["detail"]


def test_version_build_metadata_not_outdated(assets_file, versions_db):
    """上游 tag 带 semver build metadata（v0.8.11+obsidian.1.12.7）而本地只报
    核心版本号（0.8.11）时不算可更新——与 version_service._normalize_tag 同规。"""
    _seed_version(versions_db, "svc-b", "0.8.11", "v0.8.11+obsidian.1.12.7", behind=0, status="最新")
    items = attention.collect_attention(assets_file, versions_db)
    assert [i for i in items if i["kind"] == "version"] == []

    _seed_version(versions_db, "svc-c", "0.8.10", "v0.8.11+obsidian.1.12.7", behind=1, status="可更新")
    items = attention.collect_attention(assets_file, versions_db)
    assert any(i["kind"] == "version" and "svc-c" in i["title"] for i in items)


def test_feed_only(assets_file, versions_db):
    feed_aggregator._last_fetch_errors = ["GitHub events 返回 401"]
    items = attention.collect_attention(assets_file, versions_db)
    assert len(items) == 1
    assert items[0]["kind"] == "feed"
    assert items[0]["link"] == "/feed"
    assert "401" in items[0]["detail"]


def test_mixed_sources(assets_file, versions_db):
    probe_cache._cache["svc-a"] = {"ts": 1, "status": "Unhealthy", "msg": "down"}
    _seed_version(versions_db, "svc-b", "0.1", "0.2", behind=1, status="可更新")
    feed_aggregator._last_fetch_errors = ["timeout"]
    items = attention.collect_attention(assets_file, versions_db)
    kinds = [i["kind"] for i in items]
    assert kinds == ["probe", "version", "feed"]


def test_renewal_due_within_7(assets_file, versions_db, pg_online):
    import datetime

    import db as dbmod

    dbmod.run_migrations(versions_db)
    import renewals

    soon = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
    renewals.create_renewal(
        {"name": "example.com", "kind": "domain", "expires_at": soon},
        db_path=versions_db,
    )
    items = attention.collect_attention(assets_file, versions_db)
    assert len(items) == 1
    assert items[0]["kind"] == "renewal"
    assert items[0]["link"] == "/renewals"
    assert "example.com" in items[0]["title"]


def test_all_healthy_returns_empty(assets_file, versions_db):
    probe_cache._cache["svc-a"] = {"ts": 1, "status": "Healthy", "msg": "ok"}
    _seed_version(versions_db, "svc-a", "1.0.0", "1.0.0", behind=0, status="最新")
    assert attention.collect_attention(assets_file, versions_db) == []


def test_peek_does_not_probe(assets_file, versions_db, monkeypatch):
    """无缓存时不得触发 check_tcp/check_http。"""

    def boom(*_a, **_k):
        raise AssertionError("must not probe")

    monkeypatch.setattr("checker.check_tcp", boom)
    monkeypatch.setattr("checker.check_http", boom)
    assert attention.collect_attention(assets_file, versions_db) == []


# ---------------------------------------------------------------------------
# E1 月流量告警
# ---------------------------------------------------------------------------

GIB = 1024 ** 3


def _fill_traffic(db_path, machine, used_gib):
    import db as dbmod
    import node_traffic as nt

    dbmod.run_migrations(db_path)
    nt.record_sample(machine, "ens5", 0, 0, db_path=db_path)
    nt.record_sample(machine, "ens5", used_gib * GIB, 0, db_path=db_path)


def test_traffic_under_threshold_is_quiet(assets_file, versions_db):
    _fill_traffic(versions_db, "kr", 500)
    assert attention.collect_attention(assets_file, versions_db) == []


def test_traffic_warns_at_80_percent(assets_file, versions_db):
    _fill_traffic(versions_db, "kr", 850)
    items = attention.collect_attention(assets_file, versions_db)
    assert len(items) == 1
    assert items[0]["kind"] == "traffic"
    assert items[0]["severity"] == "warn"
    assert "光帆 KR" in items[0]["title"] and "接近配额" in items[0]["title"]
    assert "1000 GB" in items[0]["detail"]
    assert items[0]["link"] == "/devices"


def test_traffic_errors_when_over_quota(assets_file, versions_db):
    _fill_traffic(versions_db, "sg", 1100)
    items = attention.collect_attention(assets_file, versions_db)
    assert items[0]["severity"] == "error"
    assert "已超配额" in items[0]["title"]


def test_traffic_table_missing_does_not_break_attention(assets_file, versions_db):
    """老库还没跑迁移 7 时，今日关注不能整块塌掉。"""
    assert attention.collect_attention(assets_file, versions_db) == []


def test_traffic_does_not_probe(assets_file, versions_db, monkeypatch):
    _fill_traffic(versions_db, "kr", 900)

    def boom(*_a, **_k):
        raise AssertionError("must not probe")

    monkeypatch.setattr("checker.check_tcp", boom)
    monkeypatch.setattr("checker.check_http", boom)
    assert len(attention.collect_attention(assets_file, versions_db)) == 1


# ---------------------------------------------------------------------------
# J-C C1：T2 过期续费（error）与 7 天窗口（warn）
# ---------------------------------------------------------------------------


def _seed_renewal(db_path, name, expires_at=None, cycle=None, kind="domain"):
    import db as dbmod
    import renewals

    dbmod.run_migrations(db_path)
    data = {"name": name, "kind": kind, "expires_at": expires_at}
    if cycle:
        data["cycle"] = cycle
    renewals.create_renewal(data, db_path=db_path)


def test_renewal_expired_is_error(assets_file, versions_db, pg_online):
    import datetime

    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    _seed_renewal(versions_db, "expired.com", yesterday)
    items = attention.collect_attention(assets_file, versions_db)
    assert len(items) == 1
    assert items[0]["kind"] == "renewal"
    assert items[0]["severity"] == "error"
    assert items[0]["title"] == "expired.com 已过期"
    assert items[0]["link"] == "/renewals"


def test_renewal_due_today_is_warn(assets_file, versions_db, pg_online):
    import datetime

    today = datetime.date.today().isoformat()
    _seed_renewal(versions_db, "due-today.com", today)
    items = attention.collect_attention(assets_file, versions_db)
    assert len(items) == 1
    assert items[0]["kind"] == "renewal"
    assert items[0]["severity"] == "warn"
    assert "即将到期" in items[0]["title"]


def test_renewal_due_in_8_days_is_quiet(assets_file, versions_db, pg_online):
    import datetime

    far = (datetime.date.today() + datetime.timedelta(days=8)).isoformat()
    _seed_renewal(versions_db, "far-future.com", far)
    assert attention.collect_attention(assets_file, versions_db) == []


def test_renewal_without_expiry_is_quiet(assets_file, versions_db, pg_online):
    _seed_renewal(versions_db, "topup-pool", cycle="topup", kind="vps")
    assert attention.collect_attention(assets_file, versions_db) == []


def test_renewal_bad_date_does_not_break_attention(assets_file, versions_db, pg_online):
    import datetime

    import pgdb

    soon = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
    _seed_renewal(versions_db, "bad-date.com", soon)
    # 现行写入口拒绝坏日期；直接在隔离库构造存量坏数据，仍验证读取容错。
    with pgdb.pg_transaction() as conn:
        conn.execute(
            "UPDATE renewals SET expires_at=%s WHERE name=%s",
            ("not-a-date", "bad-date.com"),
        )
    _seed_renewal(versions_db, "good.com", soon)
    items = attention.collect_attention(assets_file, versions_db)
    # 坏日期被单条跳过，正常条目仍被收集，且不打死 Attention
    assert len(items) == 1
    assert items[0]["title"] == "good.com 即将到期"


def test_pg_attention_marks_coverage_without_old_hub_dependency(monkeypatch):
    monkeypatch.undo()
    """旧源开关残留也不会改变 PG 能力缺口提示。"""
    monkeypatch.setenv("CONSOLE_MONITORING_BUSINESS_SOURCE", "beszel")
    items = attention._monitoring_items()
    assert len(items) == 1
    assert items[0]["kind"] == "monitoring-coverage"
    assert "不代表检查通过" in items[0]["detail"]


# ---------------------------------------------------------------------------
# K3 影响折叠（机器掉线时同机服务不再逐条刷屏）
# ---------------------------------------------------------------------------


@pytest.fixture
def events_db(tmp_path):
    """跑过全部迁移的库（含 monitor_events），另建 versions 表供版本项复用。"""
    import db as dbmod

    path = str(tmp_path / "events.db")
    dbmod.run_migrations(path)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE versions (
            svc_name TEXT PRIMARY KEY,
            local_version TEXT,
            upstream_version TEXT,
            behind_count INTEGER,
            update_status TEXT,
            recent_changes TEXT,
            last_checked TEXT
        )
        """
    )
    conn.commit()
    conn.close()
    return path


def test_machine_down_collapses_same_machine_services(assets_file, events_db):
    """assets 里 svc-a / svc-b 都在 nas：nas 掉线只出一条根因项。"""
    import monitor_events as me

    me.observe_machine("nas", "down", label="NAS", detail="tcp refused", db_path=events_db)
    probe_cache._cache["svc-a"] = {"ts": 1, "status": "Unhealthy", "msg": "refused"}
    probe_cache._cache["svc-b"] = {"ts": 1, "status": "Unhealthy", "msg": "refused"}

    items = attention.collect_attention(assets_file, events_db)
    assert len(items) == 1
    assert items[0]["kind"] == "machine"
    assert items[0]["severity"] == "error"
    assert items[0]["link"] == "/devices/nas"
    assert "NAS 探测失败" in items[0]["title"]
    # N 是台账口径（该机器上登记的服务数），不是此刻探测失败的服务数
    assert "影响 2 个登记服务" in items[0]["detail"]


def test_machine_down_on_other_machine_does_not_collapse(assets_file, events_db):
    """txy 掉线不该折叠 nas 上的服务故障。"""
    import monitor_events as me

    me.observe_machine("txy", "down", db_path=events_db)
    probe_cache._cache["svc-a"] = {"ts": 1, "status": "Unhealthy", "msg": "boom"}
    items = attention.collect_attention(assets_file, events_db)
    kinds = [item["kind"] for item in items]
    assert kinds == ["machine", "probe"]
    # txy 上没有登记服务 → 不拼「影响 N 个」
    assert "影响" not in items[0]["detail"]


def test_machine_recovered_restores_per_service_items(assets_file, events_db):
    """机器恢复后，仍失败的服务重新逐条出现（折叠是查询期动态算的）。"""
    import monitor_events as me

    me.observe_machine("nas", "down", db_path=events_db)
    me.observe_machine("nas", "up", db_path=events_db)
    probe_cache._cache["svc-a"] = {"ts": 1, "status": "Unhealthy", "msg": "still bad"}
    items = attention.collect_attention(assets_file, events_db)
    assert [item["kind"] for item in items] == ["probe"]
    assert "svc-a" in items[0]["title"]


def test_attention_degrades_without_events_table(assets_file, versions_db):
    """迁移没跑（versions_db 没有 monitor_events）时退回逐服务平铺，不抛。"""
    probe_cache._cache["svc-a"] = {"ts": 1, "status": "Unhealthy", "msg": "down"}
    items = attention.collect_attention(assets_file, versions_db)
    assert [item["kind"] for item in items] == ["probe"]


def test_attention_does_not_write_events(assets_file, events_db):
    """今日关注只读事件表，绝不写（契约：不新探测、不写库）。"""
    import monitor_events as me

    me.observe_machine("nas", "down", db_path=events_db)
    before = me.list_events(db_path=events_db)["total"]
    attention.collect_attention(assets_file, events_db)
    assert me.list_events(db_path=events_db)["total"] == before


# ---------------------------------------------------------------------------
# 家宽公网不通（kind=public）：机器在线、代理端口从家宽连不上
# ---------------------------------------------------------------------------


def test_public_down_is_a_warn_item(assets_file, events_db):
    import monitor_events as me

    me.observe_public(
        "us", "down", label="Oracle", detail="tcp us.sunxc.eu.cc:62037 timed out",
        db_path=events_db,
    )
    assert attention.collect_attention(assets_file, events_db) == [
        {
            "severity": "warn",
            "kind": "public",
            "title": "Oracle 家宽公网不通",
            "detail": "tcp us.sunxc.eu.cc:62037 timed out",
            "link": "/devices/us",
        }
    ]


def test_public_item_folded_when_machine_itself_is_down(assets_file, events_db):
    """机器掉线时公网必然也不通：只留 K3 根因项，不重复报。"""
    import monitor_events as me

    me.observe_machine("us", "down", label="Oracle", db_path=events_db)
    me.observe_public("us", "down", label="Oracle", db_path=events_db)
    items = attention.collect_attention(assets_file, events_db)
    assert [item["kind"] for item in items] == ["machine"]


def test_public_item_clears_after_recovery(assets_file, events_db):
    import monitor_events as me

    me.observe_public("kr", "down", db_path=events_db)
    me.observe_public("kr", "up", db_path=events_db)
    assert attention.collect_attention(assets_file, events_db) == []


def test_public_items_sorted_and_never_fold_services(assets_file, events_db):
    """公网事件不是根因：同机服务失败照常单独出现。"""
    import monitor_events as me

    me.observe_public("sg", "down", db_path=events_db)
    me.observe_public("nas", "down", db_path=events_db)
    probe_cache._cache["svc-a"] = {"ts": 1, "status": "Unhealthy", "msg": "boom"}
    items = attention.collect_attention(assets_file, events_db)
    assert [(item["kind"], item["link"]) for item in items] == [
        ("public", "/devices/nas"),
        ("public", "/devices/sg"),
        ("probe", "/devices"),
    ]
    assert items[0]["title"] == "NAS 家宽公网不通"
    assert items[0]["detail"] == "家宽公网探测失败"


# ---------------------------------------------------------------------------
# M1：月流量预测告警（只 warn，且不与实测告警重复）
# ---------------------------------------------------------------------------


@pytest.fixture
def quota_registry(tmp_path, monkeypatch):
    """一台配了 100GB 配额的机器。"""
    import machine_config as mc_mod

    path = tmp_path / "machines.yaml"
    path.write_text(
        yaml.safe_dump(
            [{"id": "kr", "label": "光帆 KR", "role": "node", "traffic_quota_gb": 100}],
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CONSOLE_MACHINES_PATH", str(path))
    mc_mod.invalidate()
    yield
    mc_mod.invalidate()


def _stub_usage(monkeypatch, pct, projection):
    """把 usage() 换掉：这里测的是 attention 的取舍，不是账本算术
    （`month_projection` 本身在 test_node_traffic 里逐条测过）。"""
    import node_traffic as nt_mod

    monkeypatch.setattr(
        nt_mod, "usage",
        lambda machine_id, db_path=None: {
            "month": "2026-09", "nic": "ens5", "sent_gb": 50.0, "recv_gb": 1.0,
            "used_gb": 50.0, "quota_gb": 100, "pct": pct,
            "since": "2026-09-01T00:00:00Z", "updated_at": "2026-09-11T00:00:00Z",
            "resets": 0, "projection": projection,
        },
    )


_OVER = {
    "projected_gb": 450.0, "daily_gb": 15.0, "observed_hours": 240.0,
    "remaining_days": 19.0, "partial_month": False,
    "quota_gb": 100, "projected_pct": 450.0, "over_quota": True,
}


def test_projected_over_quota_is_warn(assets_file, events_db, quota_registry, monkeypatch):
    """按当前速率会超配额，但实测才 50%（不触发 E1 的 ≥80% 线）。"""
    _stub_usage(monkeypatch, pct=50.0, projection=_OVER)
    forecast = [
        item for item in attention.collect_attention(assets_file, events_db)
        if item["kind"] == "traffic-forecast"
    ]
    assert len(forecast) == 1
    assert forecast[0]["severity"] == "warn"  # 预测永远只是 warn，不升级成 error
    assert "光帆 KR" in forecast[0]["title"]
    assert "15.0 GB/天" in forecast[0]["detail"]
    assert "450.0 GB / 100 GB" in forecast[0]["detail"]


def test_partial_month_is_flagged_in_detail(
    assets_file, events_db, quota_registry, monkeypatch
):
    """月中装的 agent：预测是「按这个速率跑满一个月」，必须说清不是整月账。"""
    _stub_usage(monkeypatch, pct=50.0, projection={**_OVER, "partial_month": True})
    forecast = [
        item for item in attention.collect_attention(assets_file, events_db)
        if item["kind"] == "traffic-forecast"
    ]
    assert "非整月" in forecast[0]["detail"]


def test_projection_not_stacked_on_measured_alert(
    assets_file, events_db, quota_registry, monkeypatch
):
    """实测已 ≥80% 时不再叠预测条 —— 同一台机器出两条只是噪音。"""
    _stub_usage(monkeypatch, pct=90.0, projection=_OVER)
    kinds = [
        item["kind"] for item in attention.collect_attention(assets_file, events_db)
    ]
    assert "traffic" in kinds
    assert "traffic-forecast" not in kinds


def test_no_projection_yet_is_quiet(assets_file, events_db, quota_registry, monkeypatch):
    """观测不足 72 小时时 projection 为 None（新装 agent 不该一上来就报）。"""
    _stub_usage(monkeypatch, pct=50.0, projection=None)
    kinds = [
        item["kind"] for item in attention.collect_attention(assets_file, events_db)
    ]
    assert "traffic-forecast" not in kinds


def test_projection_within_quota_is_quiet(
    assets_file, events_db, quota_registry, monkeypatch
):
    _stub_usage(monkeypatch, pct=50.0, projection={**_OVER, "over_quota": False})
    kinds = [
        item["kind"] for item in attention.collect_attention(assets_file, events_db)
    ]
    assert "traffic-forecast" not in kinds


def test_machine_without_quota_is_quiet(events_db, monkeypatch):
    """没配配额的机器（nas）不该出预测条——没有可超的东西。"""
    monkeypatch.setattr(
        attention.mc, "load_machines",
        lambda: {"nas": {"id": "nas", "label": "NAS", "role": "server"}},
    )
    _stub_usage(monkeypatch, pct=None, projection=_OVER)
    assert attention._traffic_projection_items(events_db) == []
