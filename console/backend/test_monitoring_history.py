"""B3 历史读取接线：真实 PG 样本、查询边界和 client 权威转发。"""
import datetime as dt

import pytest

import app as console_app
import monitoring_ingest as mi
import monitoring_schema as ms

pytest_plugins = ("pg02_support",)
UTC = dt.timezone.utc
BASE = dt.datetime(2026, 10, 26, 0, 0, tzinfo=UTC)


def test_history_route_is_owned_even_when_pg_unavailable(monkeypatch):
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "test")
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "sqlite")
    monkeypatch.setattr(console_app, "_proxy_authority", lambda *a, **k: pytest.fail("历史 API 不得落到 Beszel"))
    response = console_app.app.test_client().get("/api/monitor/history?machine=nas")
    assert response.status_code == 503
    assert response.is_json


def test_history_client_forwards_query_without_reading_local_pg(monkeypatch):
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "client")
    monkeypatch.setenv("CONSOLE_AUTH_ENABLED", "false")
    monkeypatch.setattr(console_app.runtime_mode, "authority_origin", lambda *_: "http://authority.example")
    calls = []
    def forward(origin, path):
        from flask import request
        calls.append((origin, path, request.query_string))
        return {"ok": True, "source": "console_pg", "series": []}
    monkeypatch.setattr(console_app, "_proxy_authority", forward)
    response = console_app.app.test_client().get("/api/monitor/history?machine=nas&metrics=cpu")
    assert response.status_code == 200
    assert calls == [("http://authority.example", "api/monitor/history", b"machine=nas&metrics=cpu")]


def put(machine, metric, values, source="0.1.0"):
    mi.insert_samples(machine, [{"metric_name": metric, "metric_value": value,
        "sampled_at": BASE + dt.timedelta(minutes=minute)} for minute, value in values], source)


def test_raw_history_derives_memory_and_preserves_zero_and_gaps(pg_client, monkeypatch):
    import monitoring_history as mh
    mi.enrol_machine("nas", "NAS")
    put("nas", ms.METRIC_CPU_USED_PCT, [(0, 0), (5, 10), (25, 20)])
    put("nas", ms.METRIC_MEM_USED_BYTES, [(0, 1024**3), (5, 2 * 1024**3)])
    put("nas", ms.METRIC_MEM_TOTAL_BYTES, [(0, 4 * 1024**3), (5, 4 * 1024**3)])
    put("nas", ms.METRIC_CPU_USED_PCT, [(10, 99)], source="beszel-backfill")
    put("nas", ms.METRIC_CPU_USED_PCT, [(15, 98)], source="unknown")
    monkeypatch.setattr(mh, "utc_now", lambda: BASE + dt.timedelta(hours=2))
    result = mh.read_history("nas", ["cpu", "mem_pct"], BASE, BASE + dt.timedelta(minutes=30))
    cpu, mem = result["series"]
    assert [p["value"] for p in cpu["points"]] == [0, 10, 20]
    assert [p["value"] for p in mem["points"]] == [25, 50]
    assert cpu["gaps"] == [{"start": "2026-10-26T00:10:00Z", "end": "2026-10-26T00:25:00Z", "reason": "no_samples"}]
    assert result["source"] == "console_pg"
    assert result["resolution"] == "raw"


def test_hourly_reads_agent_buckets_and_excludes_unfinished_hour(pg_client, monkeypatch):
    import monitoring_history as mh
    import storage_dispatch as sd
    mi.enrol_machine("nas", "NAS")
    with sd.write_tx() as conn:
        for source, hour, value in (("console-agent", 0, 10), ("beszel-backfill", 0, 99), ("console-agent", 1, 20)):
            conn.execute("INSERT INTO monitoring_hourly (machine_id,metric_name,hour_bucket,source,avg_value,max_value,min_value,p95_value,sample_count) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1)",
                ("nas", ms.METRIC_CPU_USED_PCT, BASE + dt.timedelta(hours=hour), source, value, value, value, value))
    monkeypatch.setattr(mh, "utc_now", lambda: BASE + dt.timedelta(hours=1, minutes=10))
    result = mh.read_history("nas", ["cpu"], BASE, BASE + dt.timedelta(hours=3), resolution="hourly")
    assert [p["value"] for p in result["series"][0]["points"]] == [10]
    assert result["series"][0]["gaps"] == []
    assert result["window"]["effective_end"] == "2026-10-26T01:00:00Z"


def test_history_limit_rejects_instead_of_truncating(pg_client, monkeypatch):
    import monitoring_history as mh
    mi.enrol_machine("nas", "NAS")
    put("nas", ms.METRIC_CPU_USED_PCT, [(0, 1), (5, 2), (10, 3)])
    monkeypatch.setattr(mh, "utc_now", lambda: BASE + dt.timedelta(hours=1))
    with pytest.raises(ValueError, match="点数"):
        mh.read_history("nas", ["cpu"], BASE, BASE + dt.timedelta(minutes=15), max_points=2)


def test_gaps_keep_retention_archive_and_tail_protection_separate():
    import monitoring_history as mh
    start = BASE - dt.timedelta(days=32)
    first = BASE - dt.timedelta(days=2)
    points = [{"ts": first.isoformat(), "value": 1}]
    gaps = mh.history_gaps(points, start, BASE, 300, BASE - dt.timedelta(days=30), first, BASE - dt.timedelta(minutes=12))
    assert [g["reason"] for g in gaps] == ["retention_exceeded", "archived", "no_samples"]
    assert gaps[-1]["end"] == "2026-10-25T23:48:00Z"


def history_client(monkeypatch):
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "test")
    monkeypatch.setenv("CONSOLE_AUTH_ENABLED", "false")
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    monkeypatch.setenv("CONSOLE_MONITORING_BUSINESS_SOURCE", "console_pg")
    monkeypatch.setattr(console_app.mc, "load_machines", lambda: {"nas": {}})
    return console_app.app.test_client()


def valid_query(**overrides):
    return {"machine": "nas", "metrics": "cpu", "start": "2026-10-26T00:00:00Z",
            "end": "2026-10-26T01:00:00Z", **overrides}


@pytest.mark.parametrize("overrides,error", [
    ({"machine": "unknown"}, "machine"),
    ({"metrics": "oops"}, "metrics"),
    ({"metrics": "net_sent_total"}, "metrics"),
    ({"scope": "container:foo"}, "尚未覆盖"),
    ({"nic": "eth0"}, "尚未覆盖"),
    ({"max_points": "50001"}, "max_points"),
    ({"resolution": "other"}, "resolution"),
    ({"start": "bad"}, "ISO 8601"),
    ({"start": "2026-10-26T01:00:00Z"}, "start 必须早于 end"),
])
def test_history_rejects_invalid_queries_before_reading_pg(monkeypatch, overrides, error):
    import monitoring_history as mh
    client = history_client(monkeypatch)
    monkeypatch.setattr(mh.sd, "read_tx", lambda: pytest.fail("非法参数不得读取 PG"))
    response = client.get("/api/monitor/history", query_string=valid_query(**overrides))
    assert response.status_code == 400
    assert error in response.json["error"]


def test_history_storage_failure_is_safe_json(monkeypatch):
    import monitoring_history as mh
    client = history_client(monkeypatch)
    def unavailable(*args, **kwargs):
        raise RuntimeError("private-connection-detail")
    monkeypatch.setattr(mh.sd, "read_tx", unavailable)
    monkeypatch.setattr(mh, "utc_now", lambda: BASE + dt.timedelta(hours=2))
    response = client.get("/api/monitor/history", query_string=valid_query())
    assert response.status_code == 503
    assert response.json == {"ok": False, "error": "历史存储暂时不可用，请稍后重试"}


def test_history_events_overlap_window_and_keep_second_boundaries(pg_client, monkeypatch):
    import monitor_events as me
    import monitoring_history as mh
    mi.enrol_machine("nas", "NAS")
    for target, machine, opened, resolved in (
        ("earlier", "nas", -60, 0), ("overlap", "nas", -30, 15),
        ("active", "nas", 5, None), ("other", "txy", 5, None),
        ("later", "nas", 61, None), ("edge", "nas", 60, None),
    ):
        me.observe("machine", target, False, machine=machine, title=target,
                   ts=mh.iso(BASE + dt.timedelta(minutes=opened)))
        if resolved is not None:
            me.observe("machine", target, True, ts=mh.iso(BASE + dt.timedelta(minutes=resolved)))
    monkeypatch.setattr(mh, "utc_now", lambda: BASE + dt.timedelta(hours=2))
    result = mh.read_history("nas", ["cpu"], BASE + dt.timedelta(microseconds=100),
                             BASE + dt.timedelta(hours=1, microseconds=100))
    assert {event["target"] for event in result["events"]} == {"overlap", "active", "edge"}


def test_raw_gaps_use_sample_phase_instead_of_hourly_archive_origin():
    import monitoring_history as mh
    start = BASE + dt.timedelta(minutes=48, seconds=26)
    points = [{"ts": mh.iso(BASE + dt.timedelta(minutes=minute, seconds=28)), "value": 1}
              for minute in (48, 53, 58)]
    assert mh.history_gaps(points, start, BASE + dt.timedelta(hours=1), 300,
                           BASE - dt.timedelta(days=30), BASE, BASE + dt.timedelta(hours=1)) == []


def test_gap_slots_tolerate_subsecond_sampling_jitter():
    import monitoring_history as mh
    points = [{"ts": mh.iso(BASE + dt.timedelta(seconds=second)), "value": 1}
              for second in (0, 299.95, 600.1, 899.9)]
    assert mh.history_gaps(points, BASE, BASE + dt.timedelta(minutes=20), 300,
                           BASE - dt.timedelta(days=30), BASE, BASE + dt.timedelta(minutes=20)) == []


def test_hourly_partial_start_does_not_create_false_missing_bucket(pg_client, monkeypatch):
    import monitoring_history as mh
    import storage_dispatch as sd
    mi.enrol_machine("nas", "NAS")
    with sd.write_tx() as conn:
        for hour in range(3):
            conn.execute("INSERT INTO monitoring_hourly (machine_id,metric_name,hour_bucket,source,avg_value,sample_count) VALUES (%s,%s,%s,%s,%s,1)",
                         ("nas", ms.METRIC_CPU_USED_PCT, BASE + dt.timedelta(hours=hour), "console-agent", 10))
    monkeypatch.setattr(mh, "utc_now", lambda: BASE + dt.timedelta(hours=4))
    result = mh.read_history("nas", ["cpu"], BASE + dt.timedelta(minutes=10),
                             BASE + dt.timedelta(hours=3), resolution="hourly")
    assert len(result["series"][0]["points"]) == 2
    assert result["series"][0]["gaps"] == []
