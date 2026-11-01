# -*- coding: utf-8 -*-
"""观察窗只读取证：不运行评估、不发信、不触发探测，也不写生产库。"""
import datetime

import monitoring_business as mb
import monitoring_maintenance as mm
import monitoring_schema as ms
import runtime_mode
import storage_dispatch as sd


def collect_snapshot(since, until):
    """时间窗使用带时区 datetime；样本只取新 agent 的主机 CPU 采样轮次。"""
    if not sd.use_postgres():
        raise RuntimeError("观察核验只支持 PostgreSQL")
    with sd.read_tx() as tx:
        machines = tx.execute(
            "SELECT machine_id, last_seen_at FROM monitoring_machines "
            "WHERE status = 'active' ORDER BY machine_id"
        ).fetchall()
        samples = tx.execute(
            "WITH ticks AS (SELECT DISTINCT machine_id, sampled_at "
            "FROM monitoring_samples WHERE metric_name = %s AND source ~ %s "
            "AND sampled_at >= %s AND sampled_at <= %s), "
            "ordered AS (SELECT machine_id, sampled_at, "
            "lag(sampled_at) OVER (PARTITION BY machine_id ORDER BY sampled_at) AS previous "
            "FROM ticks) SELECT machine_id, count(*) AS rounds, "
            "min(sampled_at) AS first_sample, max(sampled_at) AS last_sample, "
            "max(extract(epoch FROM sampled_at - previous)) AS max_gap_sec "
            "FROM ordered GROUP BY machine_id ORDER BY machine_id",
            (ms.METRIC_CPU_USED_PCT, r"^\d+\.\d+\.\d+$", since, until),
        ).fetchall()
        events = tx.execute(
            "SELECT source, kind, count(*) AS count FROM monitor_events "
            "WHERE (opened_at::timestamptz >= %s AND opened_at::timestamptz <= %s) "
            "OR (resolved_at::timestamptz >= %s AND resolved_at::timestamptz <= %s) "
            "GROUP BY source, kind ORDER BY source, kind",
            (since, until, since, until),
        ).fetchall()
    by_machine = {row["machine_id"]: dict(row) for row in samples}
    return {
        "schema_version": 1,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "role": runtime_mode.instance_role(),
        "source": mb.business_source(),
        "machines": [dict(row, **{k: v for k, v in by_machine.get(row['machine_id'], {}).items()
                                   if k != 'machine_id'}) for row in machines],
        "events": [dict(row) for row in events],
        "maintenance": {mode: mm.load_status(mm.status_json_path(mode)) for mode in mm.MODES},
    }
