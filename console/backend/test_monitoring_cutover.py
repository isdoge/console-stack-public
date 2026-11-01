# -*- coding: utf-8 -*-
"""B6-3 面板 flag 分支回归：三条路由的 beszel/console_pg 双路径 + PG 快照形状。

覆盖：
- flag=console_pg 时 /api/monitor、/api/observability、/api/capacity 返回 PG
  载荷，键名与 Beszel 分支的既有契约逐键对齐（前端可选链消费，键名不能漂）；
- flag=beszel 时三路由仍走原路径（mock beszel 侧模块验证调用与透传）；
- monitor_snapshot 的容器指标名解析（含下划线容器名）、空库不炸、
  页面内流量记账在 console_pg 分支不触发（Q3：账本仍由 Beszel 链驱动）。

PG 部分与 test_monitoring_business 同底座（pg02 夹具：enrol → insert_samples
→ 读回）；app 级用例沿用 test_machines_api 的 test_client 姿势。
"""
import datetime
import os
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("CONSOLE_AUTH_ENABLED", "false")

import app as console_app  # noqa: E402
import machine_config as mc  # noqa: E402
import monitoring_business as mb  # noqa: E402
import monitoring_ingest as mi  # noqa: E402
import monitoring_schema as ms  # noqa: E402

pytest_plugins = ("pg02_support",)

GIB = 1024 ** 3
UTC = datetime.timezone.utc


@pytest.fixture(autouse=True)
def _clean_registry():
    mc.invalidate()
    yield
    mc.invalidate()


@pytest.fixture()
def pg_mode(monkeypatch):
    monkeypatch.setenv("CONSOLE_MONITORING_BUSINESS_SOURCE", "console_pg")


@pytest.fixture()
def beszel_mode(monkeypatch):
    monkeypatch.setenv("CONSOLE_MONITORING_BUSINESS_SOURCE", "beszel")


def _enrolled(pg_client, machine="nas", display="NAS"):
    mi.enrol_machine(machine, display)


def _put(pg_client, machine, metric, rows, source="0.1.0"):
    mi.insert_samples(
        machine,
        [
            {"metric_name": metric, "metric_value": value, "sampled_at": ts}
            for ts, value in rows
        ],
        source,
    )


# ---- 容器指标名解析（纯函数） -------------------------------------------------


def test_parse_container_metric_plain_names():
    assert mb.parse_container_metric("container_nginx_cpu_pct") == ("nginx", "cpu_pct")
    assert mb.parse_container_metric("container_nginx_memory_used_bytes") == (
        "nginx",
        "memory_used_bytes",
    )


def test_parse_container_metric_names_with_underscores():
    """容器名可含下划线：按已知字段后缀切，不按第一个下划线切。"""
    assert mb.parse_container_metric("container_hindsight_pg_cpu_pct") == (
        "hindsight_pg",
        "cpu_pct",
    )
    assert mb.parse_container_metric(
        "container_1panel_axonhub_memory_used_bytes"
    ) == ("1panel_axonhub", "memory_used_bytes")


def test_parse_container_metric_rejects_malformed():
    assert mb.parse_container_metric("cpu_used_pct") == (None, None)
    assert mb.parse_container_metric("container_cpu_pct") == (None, None)  # 名字段为空
    assert mb.parse_container_metric("container_nginx_threads") == (None, None)
    assert mb.parse_container_metric("") == (None, None)


# ---- monitor_snapshot（PG） ----------------------------------------------------


def test_monitor_snapshot_system_row_shape(pg_client, pg_mode):
    _enrolled(pg_client, "nas", "NAS Demo")
    base = datetime.datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    moment = base + datetime.timedelta(minutes=10)
    _put(pg_client, "nas", mb.METRIC_CPU, [(moment, 12.5)])
    _put(pg_client, "nas", ms.METRIC_MEM_USED_PCT, [(moment, 45.0)])
    _put(pg_client, "nas", ms.METRIC_MEM_USED_BYTES, [(moment, 4.0 * GIB)])
    _put(pg_client, "nas", ms.METRIC_MEM_TOTAL_BYTES, [(moment, 8.0 * GIB)])
    _put(pg_client, "nas", ms.METRIC_DISK_USED_PCT, [(moment, 61.0)])
    _put(pg_client, "nas", mb.METRIC_DISK_USED, [(moment, 61.0 * GIB)])
    _put(pg_client, "nas", mb.METRIC_DISK_TOTAL, [(moment, 100.0 * GIB)])
    _put(pg_client, "nas", ms.METRIC_LOAD_AVG_1M, [(moment, 0.5)])
    _put(pg_client, "nas", ms.METRIC_LOAD_AVG_5M, [(moment, 0.7)])
    _put(pg_client, "nas", ms.METRIC_LOAD_AVG_15M, [(moment, 0.9)])
    _put(pg_client, "nas", ms.METRIC_UPTIME_SEC, [(moment, 86400)])

    payload = mb.monitor_snapshot(now=base + datetime.timedelta(minutes=12))
    assert payload["ok"] is True
    assert payload["source"] == mb.SOURCE_CONSOLE_PG
    assert payload["updated_at"] == "2026-10-25 12:12:00"
    assert payload["probe_interval"] >= 60
    assert len(payload["systems"]) == 1
    entry = payload["systems"][0]
    # 前端消费面（today-focus/machine-card）读的键逐一断言
    assert entry["machine"] == "nas"
    assert entry["name"] == "NAS Demo"
    assert entry["cpu"] == 12.5
    assert entry["memory_pct"] == 45.0
    assert entry["disk_pct"] == 61.0
    assert entry["load_avg"] == [0.5, 0.7, 0.9]
    assert entry["uptime_sec"] == 86400
    assert entry["temperature"] is None  # PG 无传感器数据，如实置空
    # 机器卡副行绝对值键
    assert entry["disk_total_gb"] == 100.0
    assert entry["disk_used_gb"] == 61.0
    assert entry["memory_total_mb"] == 8192.0
    assert entry["memory_used_mb"] == 4096.0
    # Beszel 分支的既有键：PG 无数据的置空；status 是判活推导不是 None——
    # 窗口内有样本 → 'up'（前端 countOfflineServers 按 status !== 'up' 数离线）
    assert entry["host"] is None
    assert entry["status"] == "up"
    assert entry["net_bytes"] is None


def test_monitor_snapshot_memory_pct_falls_back_to_bytes(pg_client, pg_mode):
    """mem_used_pct 缺席时用 bytes 算——agent 两个口径都采，防个别轮次缺样本。"""
    _enrolled(pg_client, "nas", "NAS")
    moment = datetime.datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    _put(pg_client, "nas", ms.METRIC_MEM_USED_BYTES, [(moment, 2.0 * GIB)])
    _put(pg_client, "nas", ms.METRIC_MEM_TOTAL_BYTES, [(moment, 8.0 * GIB)])
    payload = mb.monitor_snapshot(now=moment + datetime.timedelta(minutes=5))
    assert payload["systems"][0]["memory_pct"] == 25.0


def test_monitor_snapshot_container_rows(pg_client, pg_mode):
    _enrolled(pg_client, "nas", "NAS")
    moment = datetime.datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    # 下划线容器名 + 普通名各一个；cpu 与 memory 两个字段
    _put(
        pg_client, "nas", ms.container_metric("hindsight_pg", "cpu_pct"),
        [(moment, 3.5)],
    )
    _put(
        pg_client, "nas", ms.container_metric("hindsight_pg", "memory_used_bytes"),
        [(moment, 512.0 * 1024 * 1024)],
    )
    _put(
        pg_client, "nas", ms.container_metric("caddy", "cpu_pct"),
        [(moment, 1.0)],
    )

    payload = mb.monitor_snapshot(now=moment + datetime.timedelta(minutes=5))
    rows = {row["name"]: row for row in payload["containers"]}
    assert set(rows) == {"hindsight_pg", "caddy"}

    hindsight = rows["hindsight_pg"]
    # unified-table 读的键（全部可选链）：name/machine/system_name/image/status/cpu/memory_mb/net_io
    assert hindsight["machine"] == "nas"
    assert hindsight["system_name"] == "NAS"
    assert hindsight["image"] == ""
    assert hindsight["status"] == "running"  # 窗口内有样本
    assert hindsight["cpu"] == 3.5
    assert hindsight["memory_mb"] == 512.0
    assert hindsight["net_io"] == ""
    assert hindsight["ports"] == ""
    assert hindsight["health"] == "unknown"
    assert hindsight["health_code"] == 0

    caddy = rows["caddy"]
    assert caddy["cpu"] == 1.0
    assert caddy["memory_mb"] is None  # 没有内存样本，如实置空
    assert caddy["status"] == "running"

    # container_count 按本机容器行数计
    assert payload["systems"][0]["container_count"] == 2


def test_monitor_snapshot_stale_container_unknown(pg_client, pg_mode):
    """窗口外（>15min）的容器样本不进快照——没样本就是 unknown/缺席。"""
    _enrolled(pg_client, "nas", "NAS")
    old = datetime.datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC)
    _put(
        pg_client, "nas", ms.container_metric("caddy", "cpu_pct"), [(old, 1.0)]
    )
    payload = mb.monitor_snapshot(
        now=old + datetime.timedelta(minutes=30)
    )
    assert payload["containers"] == []
    assert payload["systems"][0]["container_count"] == 0
    assert payload["systems"][0]["cpu"] is None  # 主机指标同样出窗


def test_monitor_snapshot_empty_tables_do_not_explode(pg_client, pg_mode):
    """零 enrol、零样本：载荷结构完整，列表为空，不炸不伪装。"""
    payload = mb.monitor_snapshot()
    assert payload["ok"] is True
    assert payload["systems"] == []
    assert payload["containers"] == []


def test_monitor_snapshot_ignores_retired_source_flag(pg_client, monkeypatch):
    """旧环境变量不再把正式快照切回 Hub。"""
    monkeypatch.setenv("CONSOLE_MONITORING_BUSINESS_SOURCE", "beszel")
    assert mb.monitor_snapshot()["source"] == "console_pg"


# ---- observability_snapshot（PG） ---------------------------------------------


def test_observability_snapshot_maps_events(pg_client, pg_mode):
    import monitor_events as me

    _enrolled(pg_client, "nas", "NAS")
    me.observe(
        me.KIND_MACHINE,
        "nas",
        False,
        source=mb.GAP_EVENT_SOURCE,
        machine="nas",
        title="NAS 采集断流 30 分钟",
        detail="last_seen=…",
        severity="warn",
        ts="2026-10-25T08:00:00Z",
    )
    me.observe(
        me.KIND_SERVICE,
        "caddy",
        False,
        source=me.SOURCE_HEALTH,
        machine="nas",
        title="caddy 健康检查失败",
        severity="error",
        ts="2026-10-25T09:00:00Z",
    )

    payload = mb.observability_snapshot(
        now=datetime.datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC)
    )
    # _empty_payload 的键一个不少
    for key in (
        "ok", "updated_at", "probe_interval", "source_errors", "machine_links",
        "active_alerts", "recent_events", "systemd_services", "smart_devices",
        "systemd_unit_names",
    ):
        assert key in payload, key
    assert payload["ok"] is True
    assert payload["source"] == mb.SOURCE_CONSOLE_PG
    assert payload["source_errors"] == []
    assert payload["systemd_services"] == []
    assert payload["smart_devices"] == []
    assert payload["systemd_unit_names"] == {}
    assert payload["capabilities"] == {"systemd": "unsupported", "smart": "unsupported"}

    alerts = {row["name"]: row for row in payload["active_alerts"]}
    gap = alerts["NAS 采集断流 30 分钟"]
    assert gap["machine"] == "nas"
    assert gap["severity"] == "warn"
    assert gap["threshold"] is None
    assert gap["duration_min"] is None
    assert gap["triggered_at"] == "2026-10-25T08:00:00Z"

    recent = {row["name"]: row for row in payload["recent_events"]}
    caddy = recent["caddy 健康检查失败"]
    assert caddy["severity"] == "error"
    assert caddy["opened_at"] == "2026-10-25T09:00:00Z"
    assert caddy["resolved_at"] is None
    assert caddy["state"] == "active"
    assert caddy["duration_sec"] is not None


def test_observability_snapshot_empty_events(pg_client, pg_mode):
    """monitor_events 空表：两个事件分块空列表，载荷不炸。"""
    payload = mb.observability_snapshot()
    assert payload["ok"] is True
    assert payload["active_alerts"] == []
    assert payload["recent_events"] == []


# ---- 路由分支（app 级） --------------------------------------------------------

BESZEL_MONITOR = {
    "ok": True,
    "updated_at": "2026-10-25 12:00:00",
    "probe_interval": 600,
    "systems": [{"id": "sys1", "machine": "nas", "cpu": 9.9}],
    "containers": [],
}
BESZEL_OBSERVABILITY = {
    "ok": True,
    "updated_at": "2026-10-25T12:00:00Z",
    "probe_interval": 600,
    "source_errors": [],
    "machine_links": {"nas": "/beszel/system/sys1"},
    "active_alerts": [],
    "recent_events": [],
    "systemd_services": [{"machine": "nas", "unit": "caddy"}],
    "smart_devices": [],
    "systemd_unit_names": {},
}
BESZEL_CAPACITY = {
    "ok": True,
    "ttl": 3600,
    "history_type": "480m",
    "min_r_squared": 0.5,
    "machines": {},
}


@pytest.fixture()
def http(pg_client, monkeypatch):
    """app 级 test_client：pg_client 提供真 PG 底座（console_pg 分支读它）。

    角色改 authority：client 角色下这三条路由被 before_request 转发给权威端，
    本地 handler（flag 分支所在处）根本不会执行——面板切换语义只在权威端生效。
    """
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authority")
    monkeypatch.setattr(console_app, "DB_PATH", "/nonexistent/console.db")
    return console_app.app.test_client()




def test_monitor_route_console_pg_branch(http, pg_client, pg_mode, monkeypatch):
    _enrolled(pg_client, "nas", "NAS")
    moment = datetime.datetime.now(UTC)
    _put(pg_client, "nas", mb.METRIC_CPU, [(moment, 11.0)])

    resp = http.get("/api/monitor")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["source"] == mb.SOURCE_CONSOLE_PG
    assert payload["systems"][0]["machine"] == "nas"
    assert payload["systems"][0]["cpu"] == 11.0
    assert not hasattr(console_app, "bc")


def test_observability_route_console_pg_branch(http, pg_client, pg_mode):
    resp = http.get("/api/observability")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["source"] == mb.SOURCE_CONSOLE_PG
    assert payload["active_alerts"] == []
    assert payload["recent_events"] == []


def test_capacity_route_console_pg_branch(http, pg_client, pg_mode, monkeypatch):
    _enrolled(pg_client, "nas", "NAS")
    base = datetime.datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    for index in range(12):
        at = base + datetime.timedelta(hours=index * 8)
        _put(pg_client, "nas", mb.METRIC_DISK_USED, [(at, (100.0 + index) * GIB)])
        _put(pg_client, "nas", mb.METRIC_DISK_TOTAL, [(at, 500.0 * GIB)])

    import capacity_legacy

    capacity_legacy.invalidate_cache()
    monkeypatch.setattr(
        capacity_legacy, "get_disk_trends",
        lambda force=False: (_ for _ in ()).throw(AssertionError("beszel path hit")),
    )
    # disk_trends 只算 role=server；默认台账里 nas 是 server
    resp = http.get("/api/capacity")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["source"] == mb.SOURCE_CONSOLE_PG
    assert "nas" in payload["machines"]




# ---- PG 记账写入方（B6-3/Q3 顺延）----------------------------------------------

# 台账 override：record_traffic_from_pg 按 record_from_systems 同口径过滤
# （mc.get_machine 存在才记），默认 machines.yaml 含 nas/kr/sg/us 等真实机器，
# 必须把台账缩到测试自己的机器集合，否则断言「不记台账外机器」无从谈起。


@pytest.fixture()
def traffic_ledger(tmp_path, monkeypatch):
    ledger = tmp_path / "machines.yaml"
    ledger.write_text("- id: kr\n  label: KR\n  role: node\n", encoding="utf-8")
    monkeypatch.setenv("CONSOLE_MACHINES_PATH", str(ledger))
    mc.invalidate()
    return ledger


def _put_nic(pg_client, machine, base, nic, steps):
    """steps: [((minute_offset, value_sent, value_recv))] → 两个 metric 落同一时间戳。"""
    for offset, sent, recv in steps:
        at = base + datetime.timedelta(minutes=offset)
        _put(pg_client, machine, mb.nic_metric(mb.METRIC_NET_SENT, nic), [(at, sent)])
        _put(pg_client, machine, mb.nic_metric(mb.METRIC_NET_RECV, nic), [(at, recv)])


def test_record_traffic_from_pg_writes_machine_traffic(
    pg_client, pg_mode, traffic_ledger
):
    """正常路径：挑中 nic 的最新同批样本写进 machine_traffic。"""
    from pg02_support import count_rows

    _enrolled(pg_client, "kr", "KR")
    base = datetime.datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    # tailscale0 峰值更大但不可计费；计费卡必须是 ens5
    _put_nic(pg_client, "kr", base, "tailscale0", [(0, 10 ** 9, 10 ** 9), (5, 2 * 10 ** 9, 2 * 10 ** 9)])
    _put_nic(pg_client, "kr", base, "ens5", [(0, 1000, 2000), (5, 1500, 2200)])

    recorded = mb.record_traffic_from_pg(now=base + datetime.timedelta(minutes=10))
    assert [row["machine"] for row in recorded] == ["kr"]
    row = recorded[0]
    assert row["nic"] == "ens5"
    assert (row["last_sent_total"], row["last_recv_total"]) == (1500, 2200)
    # now 用样本的 sampled_at：updated_at 是 12:05，不是调用时刻 12:10
    assert row["updated_at"] == "2026-10-25T12:05:00Z"
    assert count_rows(pg_client, "machine_traffic") == 1


def test_record_traffic_from_pg_second_round_accumulates(
    pg_client, pg_mode, traffic_ledger
):
    """两轮记账走 record_sample 差分：增量累计由冻结原语负责，不在本模块。"""
    _enrolled(pg_client, "kr", "KR")
    base = datetime.datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    _put_nic(pg_client, "kr", base, "ens5", [(0, 1000, 2000), (5, 1500, 2200)])
    mb.record_traffic_from_pg(now=base + datetime.timedelta(minutes=6))
    _put_nic(pg_client, "kr", base, "ens5", [(10, 1800, 2500)])
    recorded = mb.record_traffic_from_pg(now=base + datetime.timedelta(minutes=11))
    row = recorded[0]
    assert (row["sent_bytes"], row["recv_bytes"]) == (300, 300)
    assert row["resets"] == 0


def test_record_traffic_from_pg_batch_alignment(
    pg_client, pg_mode, traffic_ledger
):
    """批次对齐：sent 独有 t2、recv 独有 t0 时只认两边都有的 t1，不跨批拼数。"""
    _enrolled(pg_client, "kr", "KR")
    base = datetime.datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    sent_metric = mb.nic_metric(mb.METRIC_NET_SENT, "ens5")
    recv_metric = mb.nic_metric(mb.METRIC_NET_RECV, "ens5")
    # t0 只有 recv、t1 两边都有、t2 只有 sent（值更大，勾着实现去取它）
    _put(pg_client, "kr", recv_metric, [(base, 100)])
    _put(pg_client, "kr", sent_metric, [(base + datetime.timedelta(minutes=5), 1500)])
    _put(pg_client, "kr", recv_metric, [(base + datetime.timedelta(minutes=5), 2200)])
    _put(pg_client, "kr", sent_metric, [(base + datetime.timedelta(minutes=10), 99999)])

    recorded = mb.record_traffic_from_pg(now=base + datetime.timedelta(minutes=15))
    row = recorded[0]
    # 取 t1 批次 (1500, 2200)，不是 sent@t2 + recv@t1 的拼数
    assert (row["last_sent_total"], row["last_recv_total"]) == (1500, 2200)
    assert row["updated_at"] == "2026-10-25T12:05:00Z"


def test_record_traffic_from_pg_no_paired_batch_skips(
    pg_client, pg_mode, traffic_ledger
):
    """窗口内没有任何同批时刻：不记该机（宁少一轮，不造错账）。"""
    from pg02_support import count_rows

    _enrolled(pg_client, "kr", "KR")
    base = datetime.datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    _put(pg_client, "kr", mb.nic_metric(mb.METRIC_NET_SENT, "ens5"),
         [(base, 1000)])
    _put(pg_client, "kr", mb.nic_metric(mb.METRIC_NET_RECV, "ens5"),
         [(base + datetime.timedelta(minutes=5), 2000)])

    assert mb.record_traffic_from_pg(now=base + datetime.timedelta(minutes=10)) == []
    assert count_rows(pg_client, "machine_traffic") == 0


def test_record_traffic_from_pg_skips_machine_outside_ledger(
    pg_client, pg_mode, traffic_ledger
):
    """enrolled 但台账外：不记（record_from_systems 同口径）。"""
    from pg02_support import count_rows

    _enrolled(pg_client, "ghost", "Ghost")
    base = datetime.datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    _put_nic(pg_client, "ghost", base, "ens5", [(0, 1000, 2000)])

    assert mb.record_traffic_from_pg(now=base + datetime.timedelta(minutes=10)) == []
    assert count_rows(pg_client, "machine_traffic") == 0




# ---- 后台记账循环的 flag 路由 ----------------------------------------------------


def test_traffic_sampler_uses_only_pg(monkeypatch):
    """B6-4 后旧 Hub 已停用，记账不得解析旧源开关或拉旧快照。"""
    pg_calls = []
    monkeypatch.setattr(console_app.mb, "record_traffic_from_pg",
                        lambda now=None, conn=None: pg_calls.append(1) or [])
    monkeypatch.setenv("CONSOLE_MONITORING_BUSINESS_SOURCE", "beszel")
    monkeypatch.setattr(console_app.collect_guard, "collection_allowed", lambda: True)
    console_app._traffic_sampler()
    assert pg_calls == [1]


def test_traffic_sampler_non_authority_noop(monkeypatch):
    """client 角色下两个记账薄函数都直接返回（Q1：非 authority 不采样）。"""
    calls = []
    monkeypatch.setattr(console_app.mb, "record_traffic_from_pg",
                        lambda now=None, conn=None: calls.append("pg") or [])
    monkeypatch.setattr(console_app.nt, "record_from_systems",
                        lambda systems, db_path=None: calls.append("beszel") or [])
    monkeypatch.setattr(console_app.collect_guard, "collection_allowed", lambda: False)

    console_app._sample_node_traffic_pg()
    assert calls == []
