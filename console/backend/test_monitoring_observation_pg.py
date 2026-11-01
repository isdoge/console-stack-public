# -*- coding: utf-8 -*-
"""真实隔离 PG：观察取证仅 SELECT，回填不充当新 agent 心跳。"""
import datetime as dt

import monitoring_ingest as mi
import monitoring_observation as observation
import monitoring_schema as ms
from pg02_support import count_rows

pytest_plugins = ("pg02_support",)


def test_snapshot_excludes_backfill_and_keeps_empty_enrol_visible(pg_authority, monkeypatch, tmp_path):
    monkeypatch.setenv("CONSOLE_MONITORING_BUSINESS_SOURCE", "console_pg")
    monkeypatch.setenv("CONSOLE_MONITORING_MAINTENANCE_JSON", str(tmp_path / "state.json"))
    until = dt.datetime.now(dt.timezone.utc)
    since = until - dt.timedelta(hours=24)
    mi.enrol_machine("nas", "NAS")
    mi.enrol_machine("us", "US")
    for minute, source in [(1, "beszel-backfill"), (5, "0.1.0"),
                           (6, "legacy-mixed"), (7, "unknown"), (10, "0.1.0")]:
        mi.insert_samples("nas", [{"metric_name": ms.METRIC_CPU_USED_PCT, "metric_value": 10,
                                   "sampled_at": since + dt.timedelta(minutes=minute)}], source)
    before = count_rows(pg_authority, "monitoring_samples")
    payload = observation.collect_snapshot(since, until)
    rows = {row["machine_id"]: row for row in payload["machines"]}
    assert rows["nas"]["rounds"] == 2
    assert rows["nas"]["max_gap_sec"] == 300
    assert "rounds" not in rows["us"]
    assert payload["maintenance"] == {"evaluate": None, "dms": None, "retention": None}
    assert payload["source"] == "console_pg" and payload["role"] == "authority"
    assert count_rows(pg_authority, "monitoring_samples") == before
    assert count_rows(pg_authority, "monitor_events") == 0
    assert not list(tmp_path.iterdir())
