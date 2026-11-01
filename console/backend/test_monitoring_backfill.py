# -*- coding: utf-8 -*-
"""monitoring_backfill 的回归（B5-2）。

复验任务冻结语义：480m 历史读取（含分页）、raw/hourly 写入与来源/粒度标记、
与实时数据的物理去重、raw 保留窗外的 ``retention_exceeded`` 缺口、窗口内
头尾 ``no_samples`` 缺口、回填不伪造在线（不刷新 last_seen/agent_version）、
身份与传输失败只出 ``ok=False`` 报告不写库。
"""
import datetime
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import monitoring_backfill as mb  # noqa: E402
import monitoring_ingest as mi  # noqa: E402
import monitoring_schema as ms  # noqa: E402
from pg02_support import count_rows, fetch_all  # noqa: E402

pytest_plugins = ("pg02_support",)


def _now():
    """now - 2 天、整点：kept 样本落在已完结小时（aggregate_hourly 可见），也在 raw 保留窗内。"""
    return (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)
    ).replace(minute=0, second=0, microsecond=0)


def _ts(days_offset, hours=0):
    return _now() + datetime.timedelta(days=days_offset, hours=hours)


def _created(moment):
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"


def _stats_row(system_id, moment, **stats):
    return {"system": system_id, "created": _created(moment), "stats": stats}


def _system_row(system_id, name):
    return {"id": system_id, "name": name}


def _transport(systems, stats_pages):
    """按路径前缀分发的假传输：systems 恒定返回，system_stats 逐页出队列。"""
    queues = {"stats": list(stats_pages)}

    def get(path):
        if path.startswith("/api/collections/systems/"):
            return {"items": systems}
        if path.startswith("/api/collections/system_stats/"):
            return queues["stats"].pop(0)
        raise AssertionError(f"意外路径: {path}")

    return get


@pytest.fixture()
def enrolled(pg_client):
    mi.enrol_machine("nas", "NAS 主机")
    return "nas"


@pytest.fixture()
def now():
    return _now()


# ---- 指标映射（Beszel stats → monitoring_schema 指标名）----------------------


def test_flatten_maps_scalars_loads_and_billing_nic():
    stats = {
        "cpu": 12.5, "mp": 43.0, "mu": 6.0, "m": 16.0, "dp": 9.7, "du": 40.2,
        "d": 412.8, "su": 0.1, "la": [0.5, 1.25, 2.5],
        "ni": {
            "lo": [0, 0, 999, 999],
            "tailscale0": [1024.0, 2048.0, 50000, 60000],
            "ens5": [512.0, 256.0, 714717441, 5526032382],
        },
    }
    pairs = dict(mb.flatten_stats(stats, preferred_nic="ens5"))
    for key, expected in {
        ms.METRIC_CPU_USED_PCT: 12.5, ms.METRIC_MEM_USED_PCT: 43.0,
        ms.METRIC_MEM_USED_BYTES: 6.0, ms.METRIC_MEM_TOTAL_BYTES: 16.0,
        ms.METRIC_DISK_USED_PCT: 9.7, ms.METRIC_DISK_USED_BYTES: 40.2,
        ms.METRIC_DISK_TOTAL_BYTES: 412.8, ms.METRIC_SWAP_USED_BYTES: 0.1,
        ms.METRIC_LOAD_AVG_1M: 0.5, ms.METRIC_LOAD_AVG_5M: 1.25,
        ms.METRIC_LOAD_AVG_15M: 2.5,
        ms.nic_metric(ms.METRIC_NET_SENT_TOTAL_BYTES, "ens5"): 714717441.0,
        ms.nic_metric(ms.METRIC_NET_RECV_TOTAL_BYTES, "ens5"): 5526032382.0,
        "net_up": 512.0, "net_down": 256.0,
    }.items():
        assert pairs[key] == expected
    # 仅计费网卡：lo / tailscale0 不产生 net_* 系列（键集合里只有一套）。
    assert {
        name for name in pairs if name.startswith("net_")
    } == {
        ms.nic_metric(ms.METRIC_NET_SENT_TOTAL_BYTES, "ens5"),
        ms.nic_metric(ms.METRIC_NET_RECV_TOTAL_BYTES, "ens5"),
        "net_up", "net_down",
    }


def test_flatten_missing_fields_produce_no_points():
    assert mb.flatten_stats({}) == []
    assert mb.flatten_stats(None) == []
    assert mb.flatten_stats({"cpu": "n/a"}) == []
    # la 短数组：只出实际存在的位。
    assert dict(mb.flatten_stats({"la": [1.0]})) == {ms.METRIC_LOAD_AVG_1M: 1.0}


def test_flatten_picks_largest_billable_nic_without_preference():
    stats = {"ni": {"ens5": [1, 2, 100, 100], "eth0": [3, 4, 900, 900]}}
    pairs = dict(mb.flatten_stats(stats))
    # 无显式 traffic_nic：取累计量最大的 eth0，两套 nic 不并存。
    assert pairs[ms.nic_metric(ms.METRIC_NET_SENT_TOTAL_BYTES, "eth0")] == 900.0
    assert pairs[ms.nic_metric(ms.METRIC_NET_RECV_TOTAL_BYTES, "eth0")] == 900.0


def test_resolve_system_id_by_beszel_name_then_lower_fallback():
    systems = [
        _system_row("sys-nas", "NAS"),
        _system_row("sys-other", "kr"),
    ]
    assert mb.resolve_system_id("nas", systems) == "sys-nas"
    # 台账外机器按小写兜底（与 beszel_client 同规则），不改 Beszel 名。
    assert mb.resolve_system_id("kr", systems) == "sys-other"
    assert mb.resolve_system_id("ghost", systems) is None


# ---- 历史读取：窗口、分页与缺口 ----------------------------------------------


def test_backfill_writes_within_retention_and_reports_retention_exceeded(
    pg_client, enrolled, now
):
    old = _ts(-40)  # 窗外：40 天前
    kept1 = _ts(-20)
    kept2 = _ts(-20, hours=8)
    transport = _transport(
        [_system_row("sys-nas", "NAS")],
        [
            {
                "items": [
                    _stats_row("sys-nas", kept2, cpu=40.0, du=40.2, d=412.8),
                    _stats_row("sys-nas", kept1, cpu=11.0),
                    _stats_row("sys-nas", old, cpu=99.0),
                ]
            }
        ],
    )
    report = mb.backfill_machine("nas", now=now, transport=transport)
    assert report["ok"] is True
    assert report["source"] == mb.SOURCE_BACKFILL
    assert report["granularity"] == "480m"
    assert report["interval_sec"] == 28800
    assert report["rows_read"] == 3
    assert report["rows_kept"] == 2
    assert report["rows_dropped_retention"] == 1
    assert report["rows_skipped_invalid"] == 0
    assert report["inserted"] == 4  # kept2 三个指标 + kept1 一个
    assert report["coverage"]["first_at"] == kept1.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert report["coverage"]["last_at"] == kept2.strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = fetch_all(
        pg_client,
        "SELECT metric_name, metric_value, sampled_at, source "
        "FROM monitoring_samples ORDER BY sampled_at, metric_name",
    )
    assert len(rows) == 4
    assert {row["source"] for row in rows} == {mb.SOURCE_BACKFILL}
    sampled = {row["metric_name"] for row in rows}
    assert {ms.METRIC_CPU_USED_PCT, ms.METRIC_DISK_USED_BYTES,
            ms.METRIC_DISK_TOTAL_BYTES} <= sampled
    assert all(
        row["metric_name"] != ms.METRIC_CPU_USED_PCT
        or float(row["metric_value"]) != 99.0
        for row in rows
    )

    retention_gaps = [g for g in report["gaps"] if g["reason"] == "retention_exceeded"]
    assert len(retention_gaps) == 1
    assert retention_gaps[0]["end"] == (now - datetime.timedelta(days=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert retention_gaps[0]["start"] == old.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_backfill_reports_no_samples_head_tail_and_full_gap(pg_client, enrolled, now):
    # 窗内只有一条且不贴边：头尾都该报 no_samples。
    mid = _ts(-10)
    transport = _transport(
        [_system_row("sys-nas", "NAS")],
        [{"items": [_stats_row("sys-nas", mid, cpu=1.0)]}],
    )
    report = mb.backfill_machine("nas", now=now, transport=transport)
    assert report["ok"] is True
    reasons = [gap["reason"] for gap in report["gaps"]]
    assert reasons.count("no_samples") == 2
    head = next(g for g in report["gaps"] if g["reason"] == "no_samples" and g["start"] < g["end"])
    assert head["end"] == mid.strftime("%Y-%m-%dT%H:%M:%SZ")

    # 窗内零数据：整窗一个 no_samples 缺口。
    empty = _transport([_system_row("sys-nas", "NAS")], [{"items": []}])
    report = mb.backfill_machine("nas", now=now, transport=empty)
    assert report["ok"] is True
    assert report["gaps"] == [
        {
            "start": (now - datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reason": "no_samples",
        }
    ]
    # 空数据不新增：第一轮写入的 1 行原样保留。
    assert count_rows(pg_client, "monitoring_samples") == 1
    pages = [
        {"items": [_stats_row("sys-nas", _ts(-1, hours=8 * i), cpu=float(i)) for i in range(mb.PAGE_SIZE)]},
        {"items": [_stats_row("sys-nas", _ts(-5), cpu=1.0)]},  # 短页：到此为止
        {"items": [_stats_row("sys-nas", _ts(-6), cpu=2.0)]},  # 不应被读到
    ]
    transport = _transport([_system_row("sys-nas", "NAS")], pages)
    report = mb.backfill_machine("nas", now=now, transport=transport)
    assert report["ok"] is True
    assert report["rows_read"] == mb.PAGE_SIZE + 1


def test_backfill_skips_invalid_rows_without_writing(pg_client, enrolled, now):
    rows = [
        {"system": "sys-nas", "stats": {"cpu": 1.0}},  # 缺 created
        {"system": "sys-nas", "created": _created(_ts(-1)), "stats": "junk"},
        _stats_row("sys-nas", _ts(-1), cpu=3.0),  # 唯一有效行
    ]
    transport = _transport([_system_row("sys-nas", "NAS")], [{"items": rows}])
    report = mb.backfill_machine("nas", now=now, transport=transport)
    assert report["ok"] is True
    assert report["rows_skipped_invalid"] == 2
    assert report["rows_kept"] == 1
    assert report["inserted"] == 1


# ---- 去重：回填 vs 实时 ------------------------------------------------------


def test_backfill_does_not_clobber_existing_realtime_sample(pg_client, enrolled, now):
    at = _ts(-1)
    mi.insert_samples(
        "nas",
        [{"metric_name": ms.METRIC_CPU_USED_PCT, "metric_value": 25.0, "sampled_at": at}],
        "go-agent/0.1.0",
    )
    transport = _transport(
        [_system_row("sys-nas", "NAS")],
        [{"items": [_stats_row("sys-nas", at, cpu=77.0, mp=50.0)]}],
    )
    report = mb.backfill_machine("nas", now=now, transport=transport)
    assert report["ok"] is True
    assert report["duplicates"] == 1
    row = fetch_all(
        pg_client,
        "SELECT metric_value, source FROM monitoring_samples WHERE metric_name = %s",
        (ms.METRIC_CPU_USED_PCT,),
    )[0]
    # 先到的实时值不被动，来源仍是 go-agent。
    assert float(row["metric_value"]) == 25.0
    assert row["source"] == "go-agent/0.1.0"
    # 同批新指标正常写入。
    assert count_rows(
        pg_client, "monitoring_samples",
        "metric_name = %s", (ms.METRIC_MEM_USED_PCT,),
    ) == 1


def test_backfill_rerun_is_idempotent(pg_client, enrolled, now):
    rows = [{"items": [_stats_row("sys-nas", _ts(-3), cpu=10.0, du=30.0)]}]
    # 每轮新建 transport（真实运行每次都重新请求 Beszel）。
    first = mb.backfill_machine(
        "nas", now=now, transport=_transport([_system_row("sys-nas", "NAS")], rows)
    )
    second = mb.backfill_machine(
        "nas", now=now, transport=_transport([_system_row("sys-nas", "NAS")], rows)
    )
    assert first["inserted"] == 2
    assert second["inserted"] == 0
    assert second["duplicates"] == 2
    assert count_rows(pg_client, "monitoring_samples") == 2


# ---- hourly 折算与在线语义 ---------------------------------------------------


def test_backfill_aggregates_hourly_for_completed_hours(pg_client, enrolled, now):
    at = _ts(-2)  # now-4 天：必然已完结小时
    transport = _transport(
        [_system_row("sys-nas", "NAS")],
        [{"items": [_stats_row("sys-nas", at, cpu=42.0)]}],
    )
    report = mb.backfill_machine("nas", now=now, transport=transport)
    assert report["ok"] is True
    assert report["hourly_buckets"] >= 1
    buckets = fetch_all(
        pg_client,
        "SELECT hour_bucket, avg_value, max_value, sample_count "
        "FROM monitoring_hourly WHERE metric_name = %s",
        (ms.METRIC_CPU_USED_PCT,),
    )
    assert len(buckets) == 1
    assert buckets[0]["sample_count"] == 1
    assert float(buckets[0]["avg_value"]) == 42.0
    assert float(buckets[0]["max_value"]) == 42.0


def test_backfill_does_not_touch_last_seen_or_agent_version(pg_client, enrolled, now):
    at = _ts(-1)
    mi.insert_samples(
        "nas",
        [{"metric_name": ms.METRIC_CPU_USED_PCT, "metric_value": 25.0, "sampled_at": at}],
        "go-agent/0.1.0",
    )
    before = mi.get_machine_by_id("nas")
    transport = _transport(
        [_system_row("sys-nas", "NAS")],
        [{"items": [_stats_row("sys-nas", _ts(-2), cpu=1.0)]}],
    )
    report = mb.backfill_machine("nas", now=now, transport=transport)
    assert report["ok"] is True
    after = mi.get_machine_by_id("nas")
    assert after["last_seen_at"] == before["last_seen_at"]
    assert after["agent_version"] == "go-agent/0.1.0"


# ---- 失败路径：ok=False 报告，不写库 -----------------------------------------


def test_backfill_refuses_unenrolled_machine(pg_client, now):
    transport = _transport([_system_row("sys-nas", "NAS")], [{"items": []}])
    report = mb.backfill_machine("nas", now=now, transport=transport)
    assert report["ok"] is False
    assert report["error"] == "not_enrolled"
    assert count_rows(pg_client, "monitoring_samples") == 0


def test_backfill_refuses_revoked_machine(pg_client, now):
    mi.enrol_machine("nas", "NAS 主机")
    mi.revoke_machine("nas")
    transport = _transport([_system_row("sys-nas", "NAS")], [{"items": []}])
    report = mb.backfill_machine("nas", now=now, transport=transport)
    assert report["ok"] is False
    assert report["error"] == "revoked"


def test_backfill_machine_missing_in_beszel(pg_client, enrolled, now):
    transport = _transport([_system_row("sys-other", "OTHER")], [{"items": []}])
    report = mb.backfill_machine("nas", now=now, transport=transport)
    assert report["ok"] is False
    assert report["error"] == "not_in_beszel"


def test_backfill_transport_failure_reports_safe_error(pg_client, enrolled, now):
    def broken(path):
        raise TimeoutError("boom")

    report = mb.backfill_machine("nas", now=now, transport=broken)
    assert report["ok"] is False
    assert report["error"] == "请求超时"
    assert count_rows(pg_client, "monitoring_samples") == 0


def test_backfill_rejects_empty_machine_id(pg_client, now):
    report = mb.backfill_machine("", now=now, transport=lambda path: {})
    assert report["ok"] is False
    assert report["error"] == "machine_id 不能为空"
