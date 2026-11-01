"""设备页主机历史：PG 只读查询、真实点与缺口，禁止混入影子/回填来源。"""
import datetime as dt
from collections import Counter

from flask import jsonify, request

import machine_config as mc
import monitor_events
import monitoring_business as mb
import monitoring_ingest as mi
import monitoring_schema as ms
import storage_dispatch as sd

UTC = dt.timezone.utc
MAX_POINTS = 50000
# 百分比仅在同一时刻的分子/分母都存在时计算，不混用两个时刻的值。
METRICS = {
    "cpu": (ms.METRIC_CPU_USED_PCT, "percent"),
    "mem_pct": ((ms.METRIC_MEM_USED_BYTES, ms.METRIC_MEM_TOTAL_BYTES), "percent"),
    "mem_used": (ms.METRIC_MEM_USED_BYTES, "bytes"),
    "mem_total": (ms.METRIC_MEM_TOTAL_BYTES, "bytes"),
    "disk_pct": (ms.METRIC_DISK_USED_PCT, "percent"),
    "disk_used": (ms.METRIC_DISK_USED_BYTES, "bytes"),
    "disk_total": (ms.METRIC_DISK_TOTAL_BYTES, "bytes"),
    "load1": (ms.METRIC_LOAD_AVG_1M, "count"),
    "load5": (ms.METRIC_LOAD_AVG_5M, "count"),
    "load15": (ms.METRIC_LOAD_AVG_15M, "count"),
    "swap_used": (ms.METRIC_SWAP_USED_BYTES, "bytes"),
    "temp": (None, "°C"),
}


def utc_now():
    return dt.datetime.now(UTC)


def iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def timestamp(value):
    if isinstance(value, dt.datetime):
        result = value
    else:
        try:
            result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            raise ValueError("时间必须是带时区的 ISO 8601") from None
    if result.tzinfo is None:
        raise ValueError("时间必须带时区")
    return result.astimezone(UTC)


def history_gaps(points, start, end, interval, cutoff, first, gap_end):
    """逐槽缺口；先保留边界，再存储起点，最后缺采，尾缘不补零。"""
    gaps = []
    cursor = start

    def append(until, reason):
        nonlocal cursor
        until = min(until, end)
        if until > cursor:
            gaps.append({"start": iso(cursor), "end": iso(until), "reason": reason})
            cursor = until

    append(cutoff, "retention_exceeded")
    if first is not None:
        append(first, "archived")
    stop = min(end, gap_end)
    if stop <= cursor:
        return gaps
    step = dt.timedelta(seconds=interval)
    # 存储起点可能来自小时桶，不代表 raw 采样相位；原始档跟随窗内真实点。
    anchor = (timestamp(points[0]["ts"]) if points and interval < 3600 else first) or start
    # 采样定时器存在毫秒抖动；按最近名义槽归属，避免整周期边界附近误报缺采。
    occupied = {int(((timestamp(p["ts"]) - anchor).total_seconds() + interval / 2) // interval) for p in points}
    slot = int((cursor - anchor).total_seconds() // interval)
    while cursor < stop:
        slot_start = anchor + slot * step
        edge = min(slot_start + step, stop)
        # 查询未读取窗口前样本，首个不完整槽不推断为缺采。
        if slot not in occupied and not (cursor == start and slot_start < start):
            if gaps and gaps[-1]["reason"] == "no_samples" and gaps[-1]["end"] == iso(cursor):
                gaps[-1]["end"] = iso(edge)
            else:
                gaps.append({"start": iso(cursor), "end": iso(edge), "reason": "no_samples"})
        cursor = edge
        slot += 1
    return gaps


def _read_rows(conn, machine, names, start, end, resolution):
    if resolution == "raw":
        sql = ("SELECT metric_name, sampled_at AS ts, metric_value AS value "
               "FROM monitoring_samples WHERE machine_id=%s AND metric_name=ANY(%s) "
               "AND sampled_at >= %s AND sampled_at <= %s AND source ~ %s "
               "ORDER BY sampled_at LIMIT %s")
        args = (machine, names, start, end, r"^\d+\.\d+\.\d+$", MAX_POINTS * 2 + 1)
    else:
        sql = ("SELECT metric_name, hour_bucket AS ts, avg_value AS value "
               "FROM monitoring_hourly WHERE machine_id=%s AND metric_name=ANY(%s) "
               "AND hour_bucket >= %s AND hour_bucket < %s AND source=%s "
               "ORDER BY hour_bucket LIMIT %s")
        args = (machine, names, start.replace(minute=0, second=0, microsecond=0),
                end, mi.AGENT_SOURCE, MAX_POINTS * 2 + 1)
    rows = conn.execute(sql, args).fetchall()
    if len(rows) > MAX_POINTS * 2:
        raise ValueError("历史点数过多，请缩短时间窗或减少指标")
    grouped = {name: {} for name in names}
    for row in rows:
        grouped[row["metric_name"]][row["ts"]] = float(row["value"])
    return grouped


def _first_sample(conn, machine):
    row = conn.execute(
        "SELECT min(sampled_at) AS first_at FROM monitoring_samples "
        "WHERE machine_id=%s AND metric_name=%s AND source ~ %s",
        (machine, ms.METRIC_CPU_USED_PCT, r"^\d+\.\d+\.\d+$"),
    ).fetchone()
    hourly = conn.execute(
        "SELECT min(hour_bucket) AS first_at FROM monitoring_hourly "
        "WHERE machine_id=%s AND metric_name=%s AND source=%s",
        (machine, ms.METRIC_CPU_USED_PCT, mi.AGENT_SOURCE),
    ).fetchone()
    starts = [r["first_at"] for r in (row, hourly) if r["first_at"] is not None]
    return min(starts) if starts else None


def read_history(machine, metrics, start, end, resolution="auto", max_points=MAX_POINTS):
    start, end = timestamp(start), timestamp(end)
    if start >= end:
        raise ValueError("start 必须早于 end")
    if resolution not in mi.RESOLUTIONS:
        raise ValueError("resolution 必须是 auto、raw 或 hourly")
    if not metrics or any(key not in METRICS for key in metrics):
        raise ValueError("metrics 含未覆盖的指标，请选择主机标量")
    if not 1 <= max_points <= MAX_POINTS:
        raise ValueError("max_points 必须介于 1 与 50000")
    moment = utc_now()
    resolved = resolution if resolution != "auto" else ("raw" if end - start <= dt.timedelta(days=1) else "hourly")
    effective_end = min(end, moment)
    if resolved == "hourly":
        effective_end = effective_end.replace(minute=0, second=0, microsecond=0)
    cutoff = moment - dt.timedelta(days=mi.RAW_RETENTION_DAYS if resolved == "raw" else mi.HOURLY_RETENTION_DAYS)
    names = sorted({name for key in metrics for name in (
        METRICS[key][0] if isinstance(METRICS[key][0], tuple) else (METRICS[key][0],)
    ) if name})
    payload = {
        "ok": True, "machine": machine, "source": "console_pg", "resolution": resolved,
        "window": {"start": iso(start), "end": iso(end), "effective_end": iso(effective_end)},
        "series": [], "events": [],
    }
    if start >= effective_end:
        payload["series"] = [{"metric": key, "unit": METRICS[key][1], "interval_sec": 3600 if resolved == "hourly" else 300,
                              "points": [], "gaps": []} for key in metrics]
        return payload
    with sd.read_tx() as conn:
        grouped = _read_rows(conn, machine, names, max(start, cutoff), effective_end, resolved) if names else {}
        first = _first_sample(conn, machine)
        if first is not None and resolved == "hourly":
            first = first.replace(minute=0, second=0, microsecond=0)
        # 优先用 CPU 连续样本估计实际采样周期；未持久化的配置不伪称已读取。
        moments = sorted(grouped.get(ms.METRIC_CPU_USED_PCT, {}))
        steps = [round((b - a).total_seconds() / 60) * 60 for a, b in zip(moments, moments[1:])]
        steps = [step for step in steps if 60 <= step <= 300]
        interval = 3600 if resolved == "hourly" else (Counter(steps).most_common(1)[0][0] if steps else 300)
        count = 0
        for key in metrics:
            name, unit = METRICS[key]
            if isinstance(name, tuple):
                used, total = (grouped[item] for item in name)
                values = {at: value / total[at] * 100 for at, value in used.items() if total.get(at, 0) > 0}
            else:
                values = grouped.get(name, {})
            bucket_points = [{"ts": iso(at), "value": value} for at, value in sorted(values.items())]
            points = [point for point in bucket_points if max(start, cutoff) <= timestamp(point["ts"]) < effective_end
                      or (resolved == "raw" and timestamp(point["ts"]) == effective_end)]
            count += len(points)
            if count > max_points:
                raise ValueError("历史点数超过 max_points，请缩短时间窗或减少指标")
            # hourly 首桶可能只有一部分落在窗口内，仍有样本，不能误标整段缺采。
            gaps = ([{"start": iso(start), "end": iso(effective_end), "reason": "unsupported"}] if name is None else
                    history_gaps(bucket_points, start, effective_end, interval, cutoff, first,
                                 effective_end if resolved == "hourly" else min(effective_end, moment - dt.timedelta(minutes=12))))
            payload["series"].append({"metric": key, "unit": unit, "interval_sec": interval, "points": points, "gaps": gaps})
        # 一次响应取全同窗重叠事件，包含窗口之前开启、窗口内恢复的事件。
        rows = conn.execute(
            "SELECT * FROM monitor_events WHERE machine=%s AND opened_at::timestamptz <= %s "
            "AND (resolved_at IS NULL OR resolved_at::timestamptz >= %s) ORDER BY opened_at LIMIT %s",
            (machine, effective_end, start, MAX_POINTS + 1),
        ).fetchall()
        if len(rows) > MAX_POINTS:
            raise ValueError("事件过多，请缩短时间窗")
        payload["events"] = [monitor_events._row_to_event(row, now=moment) for row in rows]
    return payload


def register_routes(app):
    @app.route("/api/monitor/history", methods=["GET"])
    def get_monitor_history():
        """设备页 PG 主机历史；machine/metrics/start/end 必填，raw/hourly、缺口与同窗事件只读返回。"""
        if not sd.use_postgres() or mb.business_source() != mb.SOURCE_CONSOLE_PG:
            return jsonify({"ok": False, "error": "当前来源未提供 PG 历史查询"}), 503
        try:
            machine = request.args.get("machine", "").strip().lower()
            if machine not in mc.load_machines():
                raise ValueError("machine 不在台账中")
            if request.args.get("scope", "host") != "host" or request.args.get("nic"):
                raise ValueError("当前只提供主机标量历史，尚未覆盖容器与网卡历史")
            metrics = list(dict.fromkeys(request.args.get("metrics", "").split(",")))
            payload = read_history(machine, metrics, request.args.get("start"), request.args.get("end"),
                                   request.args.get("resolution", "auto"), int(request.args.get("max_points", MAX_POINTS)))
            return jsonify(payload)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001 —— 历史读取失败不泄露连接信息或返回 HTML
            app.logger.warning("历史查询失败：%s", type(exc).__name__)
            return jsonify({"ok": False, "error": "历史存储暂时不可用，请稍后重试"}), 503
