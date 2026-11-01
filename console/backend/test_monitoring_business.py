# -*- coding: utf-8 -*-
"""B5-3 业务接回回归：feature flag、480m 投影、流量重建、断流事件、缺口表达。

纯函数部分（flag/投影/差分回放）离线跑；PG 部分用 pg02 夹具在真 PG16 上验——
与 test_monitoring_ingest 同底座（enrol → insert_samples → 读回）。

重点在「什么时候不该给数字」（与 capacity 测试同纪律）：
- flag 关着调数据函数必须炸（D3 宁失败不伪装），不是退化为空数据；
- 样本不足门槛时机器从 trends 里缺席、gap_summary.insufficient=True；
- 影子流量账是差分回放，计数器归零不算负数、不写 machine_traffic。
"""
import builtins
import datetime
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import machine_config as mc  # noqa: E402
import monitoring_business as mb  # noqa: E402
import monitoring_ingest as mi  # noqa: E402
import monitoring_schema as ms  # noqa: E402
from pg02_support import count_rows  # noqa: E402

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
    """打开 console_pg 影子开关（默认关——flag 本身的默认值有独立用例）。"""
    monkeypatch.setenv("CONSOLE_MONITORING_BUSINESS_SOURCE", "console_pg")


def _enrolled(pg_client, machine="nas"):
    mi.enrol_machine(machine, machine.upper())


def _put(pg_client, machine, metric, rows, source="0.1.0"):
    """rows: [(datetime|minutes-from-base, value)]，统一走 insert_samples。"""
    mi.insert_samples(
        machine,
        [
            {
                "metric_name": metric,
                "metric_value": value,
                "sampled_at": ts,
            }
            for ts, value in rows
        ],
        source,
    )


# ---- feature flag -----------------------------------------------------------


def test_console_pg_is_the_only_business_source(monkeypatch):
    monkeypatch.delenv("CONSOLE_MONITORING_BUSINESS_SOURCE", raising=False)
    assert mb.business_source() == "console_pg"


def test_snapshot_interval_no_longer_imports_old_hub_adapter(monkeypatch):
    import probe_cache

    monkeypatch.setattr(probe_cache, "probe_interval", lambda: 600)
    assert mb._snapshot_probe_interval() == 600


def test_pg_observability_projects_events_without_legacy_adapter(monkeypatch):
    """正式 PG 投影在 Beszel 历史适配器不可导入时仍保留事件语义。"""
    original_import = builtins.__import__

    def without_legacy(name, *args, **kwargs):
        if name in {"monitor_observability", "beszel_api"}:
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_legacy)
    calls = []
    connection = object()
    rows = [
        {"machine": "synthetic", "title": "活动事件", "state": "active",
         "severity": "warn", "opened_at": "2026-10-31T00:00:00Z",
         "resolved_at": None, "duration_sec": 120},
        {"machine": "synthetic", "title": "已恢复事件", "state": "resolved",
         "severity": "error", "opened_at": "2026-10-30T00:00:00Z",
         "resolved_at": "2026-10-30T00:10:00Z", "duration_sec": 600},
    ]

    def list_events(**kwargs):
        calls.append(kwargs)
        return {"events": rows}

    monkeypatch.setattr(mb.monitor_events, "list_events", list_events)
    monkeypatch.setattr(mb, "_snapshot_probe_interval", lambda: 600)
    snapshot = mb.observability_snapshot(conn=connection)
    assert calls == [{"days": 7, "state": "all", "limit": 50, "conn": connection}]
    assert [row["name"] for row in snapshot["active_alerts"]] == ["活动事件"]
    assert [row["state"] for row in snapshot["recent_events"]] == ["active", "resolved"]
    assert snapshot["recent_events"][1]["duration_sec"] == 600
    assert snapshot["capabilities"] == {"systemd": "unsupported", "smart": "unsupported"}
    assert snapshot["source"] == "console_pg"


def test_retired_env_cannot_select_old_business_source(monkeypatch):
    """缺省/旧环境值都不再触发旧源保护；正式读写始终 PG。"""
    import storage_dispatch as sd

    class ReadOnly:
        def __enter__(self): return self
        def __exit__(self, *args): return None

    monkeypatch.setattr(sd, "read_tx", lambda conn=None: ReadOnly())
    monkeypatch.delenv("CONSOLE_MONITORING_BUSINESS_SOURCE", raising=False)
    assert mb._read_tx().__enter__() is not None
    monkeypatch.setenv("CONSOLE_MONITORING_BUSINESS_SOURCE", "beszel")
    assert mb._read_tx().__enter__() is not None


# ---- 480m 投影（纯函数） ------------------------------------------------------


def test_project_480m_buckets_and_skips_empty():
    base = datetime.datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    points = []
    # 桶 0（00:00–08:00）三个点、桶 1 空洞、桶 2（16:00–24:00）一个点
    for hour, value in ((0, 10.0), (4, 20.0), (7, 30.0), (20, 60.0)):
        points.append((base + datetime.timedelta(hours=hour), value))
    projected = mb.project_480m(points)
    assert len(projected) == 2
    assert projected[0][0] == base
    assert projected[0][1] == pytest.approx(20.0)  # (10+20+30)/3
    assert projected[1][0] == base + datetime.timedelta(hours=16)
    assert projected[1][1] == pytest.approx(60.0)


def test_project_480m_empty_and_out_of_order():
    assert mb.project_480m([]) == []
    base = datetime.datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    points = [
        (base + datetime.timedelta(hours=20), 60.0),
        (base + datetime.timedelta(hours=0), 10.0),
    ]
    projected = mb.project_480m(points)
    assert [ts for ts, _ in projected] == [base, base + datetime.timedelta(hours=16)]


def test_nic_metric_roundtrip():
    assert mb.nic_metric(mb.METRIC_NET_SENT, "ens5") == "net_sent_total_bytes@ens5"
    assert mb.parse_nic_metric("net_sent_total_bytes@ens5") == (
        "net_sent_total_bytes",
        "ens5",
    )
    assert mb.parse_nic_metric("disk_used_bytes") == ("disk_used_bytes", "")


# ---- 差分回放（纯函数） -------------------------------------------------------


def test_ledger_replay_accumulates_and_handles_reset():
    ledger = mb._LedgerReplay(month="2026-10")
    ledger.step(None, 1000, 2000)
    ledger.step(None, 1500, 2200)
    assert ledger.summary() == {"sent_bytes": 500, "recv_bytes": 200, "resets": 0}
    ledger.step(None, 30, 40)  # 计数器变小 = 归零
    assert ledger.summary() == {"sent_bytes": 530, "recv_bytes": 240, "resets": 1}


# ---- 磁盘预测（PG） ----------------------------------------------------------


def test_disk_trends_rising_disk_reports_daily_gb(pg_client, pg_mode):
    _enrolled(pg_client)
    base = datetime.datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    used_rows, total_rows = [], []
    for index in range(3 * 24 // 8 * 3):  # 9 天，每 8h 一个点
        at = base + datetime.timedelta(hours=index * 8)
        used_rows.append((at, (100.0 + index * 8.0 / 24) * GIB))  # +1 GB/天
        total_rows.append((at, 500.0 * GIB))
    _put(pg_client, "nas", mb.METRIC_DISK_USED, used_rows)
    _put(pg_client, "nas", mb.METRIC_DISK_TOTAL, total_rows)

    payload = mb.disk_trends(now=base + datetime.timedelta(days=9))
    assert payload["ok"] is True
    assert payload["source"] == mb.SOURCE_CONSOLE_PG
    assert payload["min_r_squared"] == 0.5
    trend = payload["machines"]["nas"]
    assert trend["samples"] >= 8  # 门槛之上
    assert trend["daily_gb"] == pytest.approx(1.0, abs=0.05)
    assert trend["trend"] == "rising"
    # 500-100=400GB 空闲、1GB/天 → 400 天 > 365 上限 → days_to_full 必须不给
    assert trend["days_to_full"] is None


def test_disk_trends_insufficient_samples_machine_absent(pg_client, pg_mode):
    """样本不足门槛 → 该机缺席（capacity 语义：不造全 0 假趋势）。"""
    _enrolled(pg_client)
    base = datetime.datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    rows = [(base + datetime.timedelta(hours=i), (100.0 + i) * GIB) for i in range(3)]
    _put(pg_client, "nas", mb.METRIC_DISK_USED, rows)
    _put(pg_client, "nas", mb.METRIC_DISK_TOTAL, [(ts, 500.0 * GIB) for ts, _ in rows])

    payload = mb.disk_trends(now=base + datetime.timedelta(days=1))
    assert "nas" not in payload["machines"]


def test_disk_trends_node_role_excluded(pg_client, pg_mode):
    """kr 是 node：纯代理节点的趋势占位没意义（capacity 既有语义）。"""
    _enrolled(pg_client, "kr")
    base = datetime.datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    rows = []
    for index in range(12):
        at = base + datetime.timedelta(hours=index * 8)
        rows.append((at, (100.0 + index) * GIB))
    _put(pg_client, "kr", mb.METRIC_DISK_USED, rows)
    _put(pg_client, "kr", mb.METRIC_DISK_TOTAL, [(ts, 500.0 * GIB) for ts, _ in rows])
    payload = mb.disk_trends(now=base + datetime.timedelta(days=5))
    assert "kr" not in payload["machines"]


# ---- 流量账本重建（PG） -------------------------------------------------------


def test_traffic_rebuild_accumulates_month_sent(pg_client, pg_mode):
    _enrolled(pg_client, "kr")
    base = datetime.datetime(2026, 10, 6, 0, 0, tzinfo=UTC)
    sent, recv = [], []
    for index in range(5):
        at = base + datetime.timedelta(minutes=index * 5)
        sent.append((at, 1000 + index * 100))
        recv.append((at, 2000 + index * 50))
    _put(pg_client, "kr", mb.nic_metric(mb.METRIC_NET_SENT, "ens5"), sent)
    _put(pg_client, "kr", mb.nic_metric(mb.METRIC_NET_RECV, "ens5"), recv)

    result = mb.traffic_rebuild("kr", now=base + datetime.timedelta(minutes=30))
    assert result["month"] == "2026-10"
    assert result["nic"] == "ens5"
    assert result["sent_bytes"] == 400  # 1400-1000
    assert result["recv_bytes"] == 200
    assert result["resets"] == 0
    assert result["samples"] == 5
    assert result["source"] == mb.SOURCE_CONSOLE_PG
    # 影子路径绝不写正式账本
    assert count_rows(pg_client, "machine_traffic") == 0


def test_traffic_rebuild_counter_reset_not_negative(pg_client, pg_mode):
    _enrolled(pg_client, "kr")
    base = datetime.datetime(2026, 10, 6, 0, 0, tzinfo=UTC)
    steps = [(base, 1000), (base + datetime.timedelta(minutes=5), 1500),
             (base + datetime.timedelta(minutes=10), 30)]
    _put(pg_client, "kr", mb.nic_metric(mb.METRIC_NET_SENT, "ens5"), steps)
    _put(
        pg_client, "kr", mb.nic_metric(mb.METRIC_NET_RECV, "ens5"),
        [(ts, value // 2) for ts, value in steps],
    )
    result = mb.traffic_rebuild("kr", now=base + datetime.timedelta(minutes=30))
    assert result["sent_bytes"] == 530  # 500 + 归零后的 30
    assert result["resets"] == 1


def test_traffic_rebuild_excludes_non_billable_nics(pg_client, pg_mode):
    """tailscale0 的累计量再大也不进账（内网流量不算配额）。"""
    _enrolled(pg_client, "kr")
    base = datetime.datetime(2026, 10, 6, 0, 0, tzinfo=UTC)
    _put(
        pg_client, "kr", mb.nic_metric(mb.METRIC_NET_SENT, "tailscale0"),
        [(base, 10 ** 9), (base + datetime.timedelta(minutes=5), 2 * 10 ** 9)],
    )
    _put(
        pg_client, "kr", mb.nic_metric(mb.METRIC_NET_SENT, "ens5"),
        [(base, 1000), (base + datetime.timedelta(minutes=5), 1500)],
    )
    result = mb.traffic_rebuild("kr", now=base + datetime.timedelta(minutes=30))
    assert result["nic"] == "ens5"
    assert result["sent_bytes"] == 500


def test_traffic_rebuild_uses_preferred_nic(pg_client, pg_mode, tmp_path):
    ledger = tmp_path / "machines.yaml"
    ledger.write_text(
        "- id: kr\n  label: KR\n  role: node\n  traffic_nic: ens5\n",
        encoding="utf-8",
    )
    monkeypatch = pytest.MonkeyPatch()
    with monkeypatch.context() as mp:
        mp.setenv("CONSOLE_MACHINES_PATH", str(ledger))
        mc.invalidate()
        _enrolled(pg_client, "kr")
        base = datetime.datetime(2026, 10, 6, 0, 0, tzinfo=UTC)
        for nic, first, second in (("ens5", 1000, 1200), ("eth1", 10 ** 8, 2 * 10 ** 8)):
            _put(
                pg_client, "kr", mb.nic_metric(mb.METRIC_NET_SENT, nic),
                [(base, first), (base + datetime.timedelta(minutes=5), second)],
            )
        result = mb.traffic_rebuild("kr", now=base + datetime.timedelta(minutes=30))
        assert result["nic"] == "ens5"
        assert result["sent_bytes"] == 200


def test_traffic_rebuild_utc_month_boundary(pg_client, pg_mode):
    """窗口按 UTC 自然月切：now 在 10 月末 → 只算 10 月窗内样本。"""
    _enrolled(pg_client, "kr")
    oct_end = datetime.datetime(2026, 10, 31, 23, 0, tzinfo=UTC)
    steps = [
        (oct_end, 1000),
        (oct_end + datetime.timedelta(hours=0.5), 5000),  # 仍在 10 月
    ]
    _put(pg_client, "kr", mb.nic_metric(mb.METRIC_NET_SENT, "ens5"), steps)
    result = mb.traffic_rebuild("kr", now=oct_end + datetime.timedelta(hours=0.9))
    assert result["month"] == "2026-10"
    assert result["sent_bytes"] == 4000


def test_traffic_rebuild_no_nic_returns_empty(pg_client, pg_mode):
    _enrolled(pg_client, "kr")
    result = mb.traffic_rebuild("kr")
    assert result["nic"] == ""
    assert result["sent_bytes"] == 0
    assert result["since"] is None


# ---- source 分类：回填点不进投影/差分消费 -------------------------------------


def test_traffic_rebuild_ignores_backfill_samples(pg_client, pg_mode):
    """回填计数器与 agent 计数器不同基线：账目只由 agent 样本差分得出。"""
    _enrolled(pg_client, "kr")
    base = datetime.datetime(2026, 10, 6, 0, 0, tzinfo=UTC)
    metric = mb.nic_metric(mb.METRIC_NET_SENT, "ens5")
    _put(
        pg_client, "kr", metric,
        [(base, 10 ** 6), (base + datetime.timedelta(minutes=5), 10 ** 6 + 500)],
        source=ms.BACKFILL_SOURCE,
    )
    _put(
        pg_client, "kr", metric,
        [(base + datetime.timedelta(minutes=10), 1000),
         (base + datetime.timedelta(minutes=15), 1400)],
    )
    result = mb.traffic_rebuild("kr", now=base + datetime.timedelta(minutes=30))
    assert result["nic"] == "ens5"  # 峰值比较没被回填的 10⁶ 顶掉
    assert result["sent_bytes"] == 400  # 只有 agent 两点的差分
    assert result["samples"] == 2


def test_traffic_rebuild_backfill_only_nic_not_picked(pg_client, pg_mode):
    """只有回填点的网卡不参与计费网卡挑选——不造一个纯回填的影子账。"""
    _enrolled(pg_client, "kr")
    base = datetime.datetime(2026, 10, 6, 0, 0, tzinfo=UTC)
    _put(
        pg_client, "kr", mb.nic_metric(mb.METRIC_NET_SENT, "ens5"),
        [(base, 500), (base + datetime.timedelta(minutes=5), 700)],
        source=ms.BACKFILL_SOURCE,
    )
    result = mb.traffic_rebuild("kr", now=base + datetime.timedelta(minutes=30))
    assert result["nic"] == ""
    assert result["sent_bytes"] == 0


def test_disk_trends_excludes_backfill_samples(pg_client, pg_mode):
    """回填 480m 点不与 agent 5min 点等权混合（B1-3 §4.1）。"""
    _enrolled(pg_client, "us")
    base = datetime.datetime(2026, 10, 6, 0, 0, tzinfo=UTC)
    step = datetime.timedelta(days=2)
    # 回填点：磁盘占用 50%（巨大的旧值），若混入会把趋势整体拉歪
    for index in range(10):
        at = base + step * index
        _put(pg_client, "us", mb.METRIC_DISK_USED,
             [(at, 50 * GIB)], source=ms.BACKFILL_SOURCE)
        _put(pg_client, "us", mb.METRIC_DISK_TOTAL, [(at, 100 * GIB)])
    # agent 点：磁盘占用 10% 线性涨
    for index in range(10):
        at = base + step * index + datetime.timedelta(minutes=5)
        _put(pg_client, "us", mb.METRIC_DISK_USED, [(at, 10 * GIB + index)])
        _put(pg_client, "us", mb.METRIC_DISK_TOTAL, [(at, 100 * GIB)])
    result = mb.disk_trends(now=base + datetime.timedelta(days=20))
    trend = result["machines"]["us"]
    # 回填的 50 GiB 点没混进投影：趋势是 10 GiB 起步的平缓上涨，不是 50 GiB 拉歪
    assert trend["disk_used_gb"] < 11
    assert trend["samples"] == 10  # 只有 agent 点进投影


def test_gap_summary_counts_backfill_samples(pg_client, pg_mode):
    """反例：缺口表达把回填算进历史——回填就是用来填 archived 缺口的。"""
    _enrolled(pg_client, "us")
    old = datetime.datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    _put(pg_client, "us", mb.METRIC_CPU,
         [(old + datetime.timedelta(hours=i * 8), 1.0) for i in range(20)],
         source=ms.BACKFILL_SOURCE)
    summary = mb.gap_summary("us", mb.METRIC_CPU, now=datetime.datetime(2026, 10, 6, tzinfo=UTC))
    assert summary["samples"] == 20
    assert summary["first_at"] == old.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert summary["insufficient"] is False


# ---- 断流事件（PG） ----------------------------------------------------------


def test_ingest_gap_opens_and_resolves_event(pg_client, pg_mode):
    _enrolled(pg_client, "nas")
    fresh = datetime.datetime.now(UTC)
    _put(pg_client, "nas", mb.METRIC_CPU, [(fresh, 10.0)])

    # last_seen 刚刷新 → 不开事件
    observed = mb.ingest_gap_events(now=fresh + datetime.timedelta(minutes=5))
    assert observed[0]["down"] is False
    assert observed[0]["observe"] is None

    # 断流 30 分钟 → 开事件（kind=machine、source 带前缀）
    stale = fresh + datetime.timedelta(minutes=30)
    observed = mb.ingest_gap_events(now=stale)
    assert observed[0]["down"] is True
    assert observed[0]["observe"] == "opened"
    import monitor_events

    events = monitor_events.active_events()
    assert len(events) == 1
    assert events[0]["kind"] == monitor_events.KIND_MACHINE
    assert events[0]["fingerprint"] == "machine:nas"
    assert events[0]["source"] == mb.GAP_EVENT_SOURCE
    assert events[0]["severity"] == "warn"
    # detail 抹掉 IPv4 的契约由 monitor_events.scrub_detail 保证，这里验内容形状
    assert "last_seen=" in events[0]["detail"]

    # 再断一档 → 幂等更新，不开第二条
    observed = mb.ingest_gap_events(now=stale + datetime.timedelta(minutes=10))
    assert observed[0]["observe"] == "updated"
    assert len(monitor_events.active_events()) == 1

    # 恢复上报 → 事件关闭。last_seen_at 由 PG now() 刷新（墙上时钟），判定也用
    # 真实时钟才可比；now=墙上时钟 + 1 分钟，晚于刚刷新的 last_seen 且在阈值内。
    _put(pg_client, "nas", mb.METRIC_CPU, [(datetime.datetime.now(UTC), 12.0)])
    observed = mb.ingest_gap_events(
        now=datetime.datetime.now(UTC) + datetime.timedelta(minutes=1)
    )
    assert observed[0]["down"] is False
    assert observed[0]["observe"] == "resolved"
    assert monitor_events.active_events() == []


def test_ingest_gap_never_reported_machine_no_event(pg_client, pg_mode):
    """enrolled 但从未上报：装了没跑归部署排障，不造数据中断事件。"""
    _enrolled(pg_client, "nas")
    observed = mb.ingest_gap_events(now=datetime.datetime.now(UTC) + datetime.timedelta(days=1))
    assert observed == []


# ---- 缺口表达（PG） ----------------------------------------------------------


def test_gap_summary_no_samples(pg_client, pg_mode):
    _enrolled(pg_client, "nas")
    summary = mb.gap_summary("nas", mb.METRIC_DISK_USED)
    assert summary["first_at"] is None
    assert summary["insufficient"] is True
    assert summary["reason"] == "no_samples"
    assert summary["samples"] == 0
    assert summary["earliest_queryable"] is not None


def test_gap_summary_insufficient_span(pg_client, pg_mode):
    """有样本但跨度不足 48h → insufficient=True（「样本不足」的明确表达）。"""
    _enrolled(pg_client, "nas")
    base = datetime.datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
    rows = [(base + datetime.timedelta(hours=i), (100.0 + i) * GIB) for i in range(10)]
    _put(pg_client, "nas", mb.METRIC_DISK_USED, rows)
    summary = mb.gap_summary(
        "nas", mb.METRIC_DISK_USED, now=base + datetime.timedelta(hours=10)
    )
    assert summary["samples"] == 10
    assert summary["first_at"] is not None
    assert summary["insufficient"] is True  # 跨度 9h < 48h


def test_gap_summary_sufficient(pg_client, pg_mode):
    _enrolled(pg_client, "nas")
    base = datetime.datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    rows = [
        (base + datetime.timedelta(hours=index * 8), (100.0 + index) * GIB)
        for index in range(15)
    ]
    _put(pg_client, "nas", mb.METRIC_DISK_USED, rows)
    summary = mb.gap_summary(
        "nas", mb.METRIC_DISK_USED, now=base + datetime.timedelta(days=5)
    )
    assert summary["insufficient"] is False
    assert summary["reason"] == "ok"
    assert summary["samples"] == 15


# ---- 影子快照 ----------------------------------------------------------------


def test_shadow_snapshot_partitions_isolated(pg_client, pg_mode, monkeypatch):
    """一个分块炸不拖垮其余分块（对齐 monitor_observability 的 errors 纪律）。"""
    _enrolled(pg_client, "nas")

    def boom(now=None, conn=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(mb, "disk_trends", boom)
    snapshot = mb.shadow_snapshot()
    assert snapshot["errors"] == ["disk_trends: RuntimeError"]
    assert snapshot["disk_trends"] is None
    assert snapshot["traffic"] is not None
    assert snapshot["ingest_gaps"] is not None
