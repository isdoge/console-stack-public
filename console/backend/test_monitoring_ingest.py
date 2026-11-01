# -*- coding: utf-8 -*-
"""monitoring_ingest 的真 PG16 回归（B4-2）。

复验契约锁死的存储语义：enrol/verify/revoke 身份生命周期、insert_samples
幂等（重复上报/同批重复/缓冲补传）、get_history 两档分辨率与 auto 边界、
hourly 聚合的已完结小时边界与幂等重算、raw/hourly 保留清理。
"""
import datetime
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import monitoring_ingest as mi  # noqa: E402
from pg02_support import count_rows, fetch_all  # noqa: E402

pytest_plugins = ("pg02_support",)


def _base():
    """基准时刻：now - 2 天、取整小时。

    不能锚在「今天」固定钟点：样本落在进行中的小时时会被 aggregate_hourly
    正确跳过（B1-3 §2.2），测试窗口随真实运行时刻漂移就会间歇性失败。
    -2 天保证永远在已完结小时、也在 raw 30 天保留期内。
    """
    return (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)
    ).replace(minute=0, second=0, microsecond=0)


def _ts(minute=0):
    return _base() + datetime.timedelta(minutes=minute)


def _sample(metric, minute, value):
    return {"metric_name": metric, "metric_value": value, "sampled_at": _ts(minute)}


@pytest.fixture()
def enrolled_machine(pg_client):
    secret = mi.enrol_machine("nas", "NAS 主机")
    return "nas", secret


# ---- 机器身份 ---------------------------------------------------------------


def test_enrol_returns_secret_once_and_stores_hash_only(pg_client):
    secret = mi.enrol_machine("nas", "NAS 主机", public_key="pk-1")
    assert len(secret) >= 32
    machine = mi.get_machine_by_id("nas")
    assert machine["status"] == "active"
    assert machine["public_key"] == "pk-1"
    import hashlib

    assert machine["secret_hash"] == hashlib.sha256(secret.encode()).hexdigest()
    # 台账 id 统一小写：enrol 大写、查小写，同一台机器。
    assert mi.get_machine_by_id("NAS")["machine_id"] == "nas"


def test_duplicate_enrol_is_refused(pg_client):
    mi.enrol_machine("nas", "NAS 主机")
    with pytest.raises(mi.AlreadyEnrolledError):
        mi.enrol_machine("nas", "NAS 主机 again")


def test_verify_machine_checks_hash_and_status(pg_client, enrolled_machine):
    machine_id, secret = enrolled_machine
    assert mi.verify_machine(machine_id, secret) is True
    # 错误 secret / 未登记机器都只回 False，不区分原因。
    assert mi.verify_machine(machine_id, "wrong-secret") is False
    assert mi.verify_machine("ghost", secret) is False
    assert mi.get_machine_by_secret(mi._hash_secret(secret))["machine_id"] == "nas"


def test_revoke_then_verify_and_write_fail(pg_client, enrolled_machine):
    machine_id, secret = enrolled_machine
    assert mi.revoke_machine(machine_id) is True
    assert mi.verify_machine(machine_id, secret) is False
    assert mi.revoke_machine(machine_id) is False  # 重复吊销是 no-op
    with pytest.raises(mi.RevokedMachineError):
        mi.insert_samples(machine_id, [_sample("cpu", 0, 1.0)], "go-agent/0.1")
    # 吊销挡写不删身份：记录还在，状态 revoked。
    assert mi.get_machine_by_id(machine_id)["status"] == "revoked"


def test_get_machine_by_id_unknown_returns_none(pg_client):
    assert mi.get_machine_by_id("ghost") is None


# ---- insert_samples：身份门 + 幂等 ------------------------------------------


def test_insert_rejects_unknown_machine(pg_client):
    with pytest.raises(mi.UnknownMachineError):
        mi.insert_samples("ghost", [_sample("cpu", 0, 1.0)], "go-agent/0.1")


def test_insert_counts_and_updates_last_seen(pg_client, enrolled_machine):
    machine_id, _ = enrolled_machine
    result = mi.insert_samples(
        machine_id,
        [_sample("cpu", 0, 12.5), _sample("mem", 0, 43.0)],
        "go-agent/0.1",
    )
    assert result == {"inserted": 2, "duplicates": 0}
    machine = mi.get_machine_by_id(machine_id)
    assert machine["agent_version"] == "go-agent/0.1"
    assert machine["last_seen_at"] is not None


def test_duplicate_report_is_idempotent_and_refreshes_received_at(pg_client, enrolled_machine):
    machine_id, _ = enrolled_machine
    first = mi.insert_samples(machine_id, [_sample("cpu", 0, 12.5)], "go-agent/0.1")
    second = mi.insert_samples(
        machine_id,
        [{"metric_name": "cpu", "metric_value": 99.9, "sampled_at": _ts(0)}],
        "go-agent/0.1",
    )
    assert first["inserted"] == 1
    assert second == {"inserted": 0, "duplicates": 1}
    assert count_rows(pg_client, "monitoring_samples") == 1
    # 冲突不改 metric_value（先到的值不被动），只刷新 received_at。
    row = fetch_all(pg_client, "SELECT * FROM monitoring_samples")[0]
    assert float(row["metric_value"]) == 12.5
    assert row["received_at"] >= row["sampled_at"]


def test_in_batch_duplicate_keys_collapse_to_first(pg_client, enrolled_machine):
    machine_id, _ = enrolled_machine
    result = mi.insert_samples(
        machine_id,
        [_sample("cpu", 0, 10.0), _sample("cpu", 0, 20.0), _sample("mem", 0, 1.0)],
        "go-agent/0.1",
    )
    assert result == {"inserted": 2, "duplicates": 1}
    row = fetch_all(
        pg_client, "SELECT metric_value FROM monitoring_samples WHERE metric_name = 'cpu'"
    )[0]
    assert float(row["metric_value"]) == 10.0


def test_insert_rejects_bad_values(pg_client, enrolled_machine):
    machine_id, _ = enrolled_machine
    with pytest.raises(ValueError):
        mi.insert_samples(
            machine_id,
            [{"metric_name": "cpu", "metric_value": float("nan"), "sampled_at": _ts(0)}],
            "go-agent/0.1",
        )
    with pytest.raises(ValueError):
        mi.insert_samples(machine_id, [_sample("", 0, 1.0)], "go-agent/0.1")
    with pytest.raises(ValueError):
        mi.insert_samples(machine_id, [_sample("cpu", 0, 1.0)], "  ")


def test_sampled_at_accepts_iso_string_and_normalizes_to_utc(pg_client, enrolled_machine):
    machine_id, _ = enrolled_machine
    iso = _ts(0).strftime(mi.TS_FORMAT)
    mi.insert_samples(
        machine_id,
        [{"metric_name": "cpu", "metric_value": 5.0, "sampled_at": iso}],
        "go-agent/0.1",
    )
    # +08:00 偏移与 Z 是同一时刻 → 同一唯一键，第二次上报算重复。
    offset_form = (_ts(0) + datetime.timedelta(hours=8)).strftime(
        "%Y-%m-%dT%H:%M:%S+08:00"
    )
    result = mi.insert_samples(
        machine_id,
        [{"metric_name": "cpu", "metric_value": 6.0, "sampled_at": offset_form}],
        "go-agent/0.1",
    )
    assert result == {"inserted": 0, "duplicates": 1}


def test_insert_accepts_rfc3339nano_from_go_agent(pg_client, enrolled_machine):
    """Go time.Time JSON 序列化走 RFC3339Nano（纳秒），存储层必须接受。

    真实 agent 上报 400/500 根因：_to_utc_datetime 不认超过 6 位小数秒。
    """
    machine_id, _ = enrolled_machine
    nano = _ts(0).strftime(mi.TS_FORMAT)[:-1] + ".762665865Z"
    result = mi.insert_samples(
        machine_id,
        [{"metric_name": "cpu", "metric_value": 5.0, "sampled_at": nano}],
        "go-agent/0.1",
    )
    assert result == {"inserted": 1, "duplicates": 0}
    rows = fetch_all(
        pg_client,
        "SELECT sampled_at FROM monitoring_samples WHERE metric_name = 'cpu'",
    )
    # 纳秒截断到微秒；PG timestamptz 精度一致
    assert len(rows) == 1
    expected = _ts(0).replace(microsecond=762665)
    assert rows[0]["sampled_at"].replace(tzinfo=datetime.timezone.utc) == expected


# ---- get_history：两档分辨率 -------------------------------------------------


@pytest.fixture()
def loaded_machine(pg_client, enrolled_machine):
    """02:00–02:55 每 5 分钟一个 cpu 样本，03:00 起无数据。"""
    machine_id, secret = enrolled_machine
    samples = [
        _sample("cpu", minute, 10.0 + minute / 10.0) for minute in range(0, 60, 5)
    ]
    mi.insert_samples(machine_id, samples, "0.1.0")
    return machine_id, secret


def test_history_raw_resolution(pg_client, loaded_machine):
    machine_id, _ = loaded_machine
    history = mi.get_history(machine_id, "cpu", _ts(0), _ts(55), resolution="raw")
    assert history["resolution"] == "raw"
    assert len(history["points"]) == 12
    assert history["points"][0] == {
        "ts": _ts(0).strftime(mi.TS_FORMAT), "value": 10.0, "source": "0.1.0"}
    # 窗口外的不算：02:55 之后没有点。
    tail = mi.get_history(machine_id, "cpu", _ts(56), _ts(59), resolution="raw")
    assert tail["points"] == []


def test_history_auto_boundary_24h(pg_client, loaded_machine):
    machine_id, _ = loaded_machine
    # 跨度恰好 24h → raw；超过一秒 → hourly。
    exactly = mi.get_history(machine_id, "cpu", _ts(0), _ts(0) + datetime.timedelta(hours=24))
    assert exactly["resolution"] == "raw"
    over = mi.get_history(machine_id, "cpu", _ts(0), _ts(0) + datetime.timedelta(hours=24, seconds=1))
    assert over["resolution"] == "hourly"


def test_history_auto_overrides_to_explicit_resolution(pg_client, loaded_machine):
    machine_id, _ = loaded_machine
    explicit = mi.get_history(machine_id, "cpu", _ts(0), _ts(0) + datetime.timedelta(hours=24), resolution="hourly")
    assert explicit["resolution"] == "hourly"


def test_history_hourly_reads_aggregates(pg_client, loaded_machine):
    machine_id, _ = loaded_machine
    mi.aggregate_hourly()
    history = mi.get_history(
        machine_id, "cpu", _ts(0), _ts(0) + datetime.timedelta(days=7), resolution="hourly"
    )
    assert history["resolution"] == "hourly"
    assert len(history["points"]) == 1  # 全部样本落在 02:00 这一小时桶
    point = history["points"][0]
    assert point["ts"] == _ts(0).strftime(mi.TS_FORMAT)
    assert point["sample_count"] == 12
    assert point["min"] == pytest.approx(10.0)
    assert point["max"] == pytest.approx(15.5)
    assert point["avg"] == pytest.approx(12.75)
    # hourly 桶按逻辑来源回显：agent 版本折叠为 console-agent。
    assert point["source"] == mi.AGENT_SOURCE


def test_history_unknown_metric_returns_empty_points(pg_client, loaded_machine):
    machine_id, _ = loaded_machine
    history = mi.get_history(machine_id, "temperature", _ts(0), _ts(55))
    assert history["points"] == []


def test_history_rejects_bad_window(pg_client, loaded_machine):
    machine_id, _ = loaded_machine
    with pytest.raises(ValueError):
        mi.get_history(machine_id, "cpu", _ts(10), _ts(0))
    with pytest.raises(ValueError):
        mi.get_history(machine_id, "cpu", _ts(0), _ts(10), resolution="daily")


# ---- aggregate_hourly -------------------------------------------------------


def test_aggregate_is_idempotent_and_recomputes_on_late_samples(pg_client, enrolled_machine):
    machine_id, _ = enrolled_machine
    mi.insert_samples(machine_id, [_sample("cpu", 0, 10.0), _sample("cpu", 30, 20.0)], "go-agent/0.1")
    buckets = mi.aggregate_hourly()
    assert buckets == 1
    # 乱序补传进同一小时的旧样本 → 重算自动并入，不需要脏桶簿记。
    mi.insert_samples(machine_id, [_sample("cpu", 45, 40.0)], "go-agent/0.1")
    assert mi.aggregate_hourly() == 1  # ON CONFLICT DO UPDATE 不新增桶
    rows = fetch_all(
        pg_client,
        "SELECT sample_count, max_value FROM monitoring_hourly WHERE machine_id = 'nas'",
    )
    assert rows[0]["sample_count"] == 3
    assert float(rows[0]["max_value"]) == 40.0


def test_aggregate_skips_current_hour(pg_client, enrolled_machine):
    machine_id, _ = enrolled_machine
    now = datetime.datetime.now(datetime.timezone.utc)
    current_minute = (now.minute // 5) * 5
    samples = [
        {
            "metric_name": "cpu",
            "metric_value": 50.0,
            "sampled_at": now.replace(second=0, microsecond=0)
            - datetime.timedelta(minutes=current_minute - offset),
        }
        for offset in range(0, current_minute + 1, 5)
    ]
    # base 是 now-2 天，再退 25h：一个必然已完结的小时，证明聚合确实跑了。
    samples.append(_sample("cpu", -(24 * 60 + 60), 50.0))
    mi.insert_samples(machine_id, samples, "go-agent/0.1")
    mi.aggregate_hourly()
    # 不用 len==1 计数：执行期间整点翻转时上一小时变为已完结、入桶是正确
    # 行为。稳定的断言是「聚合完成时刻的进行中小时永远没有桶」。
    assert count_rows(pg_client, "monitoring_hourly") >= 1
    in_progress = fetch_all(
        pg_client,
        "SELECT 1 FROM monitoring_hourly "
        "WHERE machine_id = 'nas' AND hour_bucket = date_trunc('hour', now())",
    )
    assert in_progress == []


def test_aggregate_p95(pg_client, enrolled_machine):
    machine_id, _ = enrolled_machine
    # 1..20：p95 = 19.05（PG percentile_cont 线性插值口径）。
    mi.insert_samples(
        machine_id,
        [_sample("cpu", i, float(i)) for i in range(1, 21)],
        "go-agent/0.1",
    )
    mi.aggregate_hourly()
    row = fetch_all(pg_client, "SELECT p95_value FROM monitoring_hourly")[0]
    assert float(row["p95_value"]) == pytest.approx(19.05)


# ---- 保留清理 ---------------------------------------------------------------


def test_cleanup_raw_deletes_old_samples_only(pg_client, enrolled_machine):
    machine_id, _ = enrolled_machine
    old = _ts(0) - datetime.timedelta(days=31)
    mi.insert_samples(
        machine_id,
        [
            {"metric_name": "cpu", "metric_value": 1.0, "sampled_at": old},
            _sample("cpu", 0, 2.0),
        ],
        "go-agent/0.1",
    )
    assert mi.cleanup_raw() == 1
    rows = fetch_all(pg_client, "SELECT sampled_at FROM monitoring_samples")
    assert len(rows) == 1
    assert rows[0]["sampled_at"] == _ts(0)


def test_cleanup_hourly_deletes_old_buckets(pg_client, enrolled_machine):
    machine_id, _ = enrolled_machine
    two_years_ago = _ts(0) - datetime.timedelta(days=730)
    mi.insert_samples(
        machine_id,
        [
            {"metric_name": "cpu", "metric_value": 1.0, "sampled_at": two_years_ago},
            _sample("cpu", 0, 2.0),
        ],
        "go-agent/0.1",
    )
    mi.aggregate_hourly()
    assert mi.cleanup_hourly() == 1
    assert count_rows(pg_client, "monitoring_hourly") == 1


# ---- 引擎门 -----------------------------------------------------------------


def test_refuses_non_pg_engine(pg_client, monkeypatch):
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "sqlite")
    with pytest.raises(mi.MonitoringIngestError):
        mi.get_machine_by_id("nas")


# ---- hourly source 维度（B6-3 迁移 17） --------------------------------------


def test_hourly_source_of_maps_raw_sources():
    # agent 版本（任意 x.y.z）→ console-agent；回填保持；其余 → legacy-mixed。
    assert mi.hourly_source_of("0.1.0") == mi.AGENT_SOURCE
    assert mi.hourly_source_of("12.34.56") == mi.AGENT_SOURCE
    assert mi.hourly_source_of("beszel-backfill") == "beszel-backfill"
    assert mi.hourly_source_of("") == mi.LEGACY_SOURCE
    assert mi.hourly_source_of(None) == mi.LEGACY_SOURCE
    assert mi.hourly_source_of("go-agent/0.1") == mi.LEGACY_SOURCE
    assert mi.hourly_source_of("0.1") == mi.LEGACY_SOURCE  # 非三段版本不冒充 agent


def test_aggregate_separates_sources_into_distinct_buckets(pg_client, enrolled_machine):
    """回填 480m 点与 agent 5min 点同小时落桶时必须分桶，不得等权混合。

    时刻错开（真实形态：480m 与 5min 网格几乎不重合）；同键冲突的物理去重
    是 B0 冻结语义，另行钉在 test_raw_dedup_uses_first_value_across_sources。
    """
    machine_id, _ = enrolled_machine
    mi.insert_samples(machine_id, [_sample("cpu", 0, 10.0), _sample("cpu", 30, 20.0)],
                      "0.2.0")
    mi.insert_samples(
        machine_id,
        [{"metric_name": "cpu", "metric_value": 90.0, "sampled_at": _ts(45)}],
        "beszel-backfill",
    )
    assert mi.aggregate_hourly() == 2  # 同一小时、两个来源 → 两个桶
    rows = fetch_all(
        pg_client,
        "SELECT source, sample_count, avg_value FROM monitoring_hourly "
        "WHERE machine_id = 'nas' ORDER BY source",
    )
    assert [row["source"] for row in rows] == ["beszel-backfill", mi.AGENT_SOURCE]
    by_source = {row["source"]: row for row in rows}
    assert by_source[mi.AGENT_SOURCE]["sample_count"] == 2
    assert float(by_source[mi.AGENT_SOURCE]["avg_value"]) == 15.0
    assert by_source["beszel-backfill"]["sample_count"] == 1
    assert float(by_source["beszel-backfill"]["avg_value"]) == 90.0


def test_raw_dedup_uses_first_value_across_sources(pg_client, enrolled_machine):
    """同键 (machine, metric, sampled_at) 跨 source 冲突：首到值不被动（B0 冻结）。

    raw 的物理去重不含 source——这是 B4 冻结的表结构，迁移 17 只给 hourly 加
    维度，不改 raw 唯一键。冲突时回填算 duplicate，桶仍只有首到来源。
    """
    machine_id, _ = enrolled_machine
    first = mi.insert_samples(machine_id, [_sample("cpu", 0, 10.0)], "0.1.0")
    second = mi.insert_samples(
        machine_id,
        [{"metric_name": "cpu", "metric_value": 90.0, "sampled_at": _ts(0)}],
        "beszel-backfill",
    )
    assert first["inserted"] == 1
    assert second == {"inserted": 0, "duplicates": 1}
    assert count_rows(pg_client, "monitoring_samples") == 1
    assert mi.aggregate_hourly() == 1
    rows = fetch_all(pg_client, "SELECT source FROM monitoring_hourly")
    assert rows[0]["source"] == mi.AGENT_SOURCE


def test_aggregate_folds_agent_versions_into_one_source(pg_client, enrolled_machine):
    """agent 版本升级不产生新序列：0.1.0 与 0.2.0 的样本进同一 console-agent 桶。"""
    machine_id, _ = enrolled_machine
    mi.insert_samples(machine_id, [_sample("cpu", 0, 10.0)], "0.1.0")
    mi.insert_samples(machine_id, [_sample("cpu", 30, 30.0)], "0.2.0")
    assert mi.aggregate_hourly() == 1
    rows = fetch_all(
        pg_client, "SELECT source, sample_count FROM monitoring_hourly")
    assert rows[0]["source"] == mi.AGENT_SOURCE
    assert rows[0]["sample_count"] == 2


def test_aggregate_maps_unknown_source_to_legacy_mixed(pg_client, enrolled_machine):
    """未知来源聚合为 legacy-mixed，不静默冒充 agent。"""
    machine_id, _ = enrolled_machine
    # 绕过 insert 的 source 校验直接落 raw：历史数据可能带任意 source 值。
    import storage_dispatch as sd

    with sd.write_tx() as tx:
        tx.execute(
            "INSERT INTO monitoring_samples "
            "       (machine_id, metric_name, metric_value, sampled_at, source) "
            "VALUES (%s, %s, %s, %s, %s)",
            (machine_id, "cpu", 7.0, _ts(0), "mystery-collector"),
        )
    assert mi.aggregate_hourly() == 1
    rows = fetch_all(pg_client, "SELECT source FROM monitoring_hourly")
    assert rows[0]["source"] == mi.LEGACY_SOURCE


def test_history_hourly_default_reads_agent_only(pg_client, enrolled_machine):
    """默认 hourly 序列只读 console-agent；回填桶必须显式 sources 才可见。"""
    machine_id, _ = enrolled_machine
    mi.insert_samples(machine_id, [_sample("cpu", 0, 10.0)], "0.1.0")
    mi.insert_samples(
        machine_id,
        [{"metric_name": "cpu", "metric_value": 90.0, "sampled_at": _ts(30)}],
        "beszel-backfill",
    )
    mi.aggregate_hourly()
    week = datetime.timedelta(days=7)
    default = mi.get_history(machine_id, "cpu", _ts(0), _ts(0) + week, resolution="hourly")
    assert [point["source"] for point in default["points"]] == [mi.AGENT_SOURCE]
    assert default["sources"] == [mi.AGENT_SOURCE]
    backfill = mi.get_history(
        machine_id, "cpu", _ts(0), _ts(0) + week, resolution="hourly",
        sources=("beszel-backfill",))
    assert [point["source"] for point in backfill["points"]] == ["beszel-backfill"]
    both = mi.get_history(
        machine_id, "cpu", _ts(0), _ts(0) + week, resolution="hourly",
        sources=mi.HOURLY_SOURCES)
    assert {point["source"] for point in both["points"]} == {
        mi.AGENT_SOURCE, "beszel-backfill"}


def test_history_raw_source_filter_and_echo(pg_client, enrolled_machine):
    machine_id, _ = enrolled_machine
    mi.insert_samples(machine_id, [_sample("cpu", 0, 10.0)], "0.1.0")
    mi.insert_samples(
        machine_id,
        [{"metric_name": "cpu", "metric_value": 90.0, "sampled_at": _ts(30)}],
        "beszel-backfill",
    )
    history = mi.get_history(
        machine_id, "cpu", _ts(0), _ts(30),
        resolution="raw", sources=("beszel-backfill",))
    assert [point["value"] for point in history["points"]] == [90.0]
    assert history["sources"] == ["beszel-backfill"]
    with pytest.raises(ValueError):
        mi.get_history(machine_id, "cpu", _ts(0), _ts(0), resolution="raw", sources=("",))
