#!/usr/bin/env python3
"""在 NAS 宿主只读核验 24h 观察窗；缺历史、缺机器或命令失败均不能 PASS。

读取既有 cron 的分模式 JSON 日志和容器内 PG 只读摘要，不创建任何调度。
事件非零只要求人工区分真实故障与迁移缺陷，不自动触发回退或判迁移失败。
"""
import argparse
import datetime as dt
import json
import math
import subprocess
import sys
from pathlib import Path

PERIODS = {"retention": 300, "evaluate": 60, "dms": 300}
MACHINES = ("aly", "kr", "nas", "sg", "txy", "us")
MAX_SAMPLE_GAP = 660
MIN_WINDOW_HOURS = 24
TASKS = {"retention": {"aggregate_hourly", "cleanup_raw", "cleanup_hourly"},
         "evaluate": {"run_evaluation"}, "dms": {"check_dead_mans_switch"}}
SNAPSHOT_CODE = """
import datetime, json, sys
from monitoring_observation import collect_snapshot
parse = lambda s: datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))
print(json.dumps(collect_snapshot(parse(sys.argv[1]), parse(sys.argv[2])), default=str))
"""


def moment(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须带时区")
    return parsed.astimezone(dt.timezone.utc)


def check_observation(snapshot, histories, since, until):
    failed, incomplete, review = [], [], []
    mode_reports = {}
    if until <= since:
        raise ValueError("观察结束时间必须晚于开始时间")
    if (until - since).total_seconds() < MIN_WINDOW_HOURS * 3600:
        incomplete.append("window_shorter_than_24h")
    if snapshot.get("role") != "authority" or snapshot.get("source") != "console_pg":
        failed.append("wrong_authority_or_source")
    generated = moment(snapshot["generated_at"])
    if abs((generated - until).total_seconds()) > 60:
        incomplete.append("snapshot_not_at_window_end")

    for mode, period in PERIODS.items():
        records, malformed = [], 0
        for line in histories.get(mode, "").splitlines():
            if not line.strip().startswith("{"):
                continue  # stderr 诊断不充当成功证据；缺终态由逐调度槽核验捕获。
            try:
                row = json.loads(line)
                start, finish = moment(row["started"]), moment(row["finished"])
                if row["mode"] != mode or finish < start or row.get("schema_version") != 1:
                    raise ValueError("无效轮次")
                if since <= start <= until:
                    if finish > until:
                        incomplete.append(mode + ":unfinished_at_window_end")
                    records.append((start, row))
            except (KeyError, TypeError, ValueError):
                malformed += 1
        if malformed:
            incomplete.append(mode + ":invalid_terminal_record")
        # 仅要求已超过 30 秒落盘余量的调度槽；不把尚在执行的本分钟误判为漏跑。
        first = math.ceil(since.timestamp() / period) * period
        last = math.floor((until.timestamp() - 30) / period) * period
        expected = set(range(first, last + 1, period))
        observed = {int(start.timestamp()) // period * period for start, _ in records}
        missing = len(expected - observed)
        if not records or missing:
            incomplete.append(mode + ":missing_scheduled_rounds")
        current = (snapshot.get("maintenance") or {}).get(mode)
        rounds_to_verify = [(row, True) for _, row in records]
        if not isinstance(current, dict):
            incomplete.append(mode + ":missing_mode_snapshot")
        else:
            rounds_to_verify.append((current, bool(current.get("finished"))))
            started = moment(current["started"])
            finished = moment(current["finished"]) if current.get("finished") else None
            if started > generated or (finished and (finished < started or finished > generated)):
                failed.append(mode + ":invalid_mode_time")
            elif finished is None:
                if (generated - started).total_seconds() > period:
                    failed.append(mode + ":stuck_mode")
            elif (generated - finished).total_seconds() > period + 60:
                failed.append(mode + ":stale_mode")
        for row, complete in rounds_to_verify:
            tasks = row.get("tasks")
            if (row.get("schema_version") != 1 or row.get("mode") != mode
                    or row.get("role") != "authority" or row.get("source") != "console_pg"
                    or row.get("notify") is not (mode != "retention")
                    or not isinstance(row.get("errors"), dict) or row["errors"]
                    or not isinstance(tasks, dict)
                    or (complete and (set(tasks) != TASKS[mode]
                        or any(not isinstance(task, dict) or task.get("skipped")
                               for task in tasks.values())))):
                failed.append(mode + ":failed_or_wrong_mode_round")
            if isinstance(tasks, dict) and any(
                    isinstance(task, dict) and task.get("delivery_failures", 0)
                    for task in tasks.values()):
                failed.append(mode + ":delivery_failure")
        mode_reports[mode] = {"rounds": len(records), "missing_slots": missing,
                              "malformed_records": malformed}

    machines = {row["machine_id"]: row for row in snapshot.get("machines", [])}
    for machine in MACHINES:
        row = machines.get(machine)
        if not row or not row.get("rounds"):
            failed.append(machine + ":no_agent_samples")
            continue
        first, last = moment(row["first_sample"]), moment(row["last_sample"])
        heartbeat = moment(row["last_seen_at"]) if row.get("last_seen_at") else None
        if (first - since).total_seconds() > MAX_SAMPLE_GAP:
            failed.append(machine + ":late_first_sample")
        if (until - last).total_seconds() > MAX_SAMPLE_GAP:
            failed.append(machine + ":stale_samples")
        if not heartbeat or (generated - heartbeat).total_seconds() > MAX_SAMPLE_GAP:
            failed.append(machine + ":stale_heartbeat")
        if float(row.get("max_gap_sec") or 0) > MAX_SAMPLE_GAP:
            failed.append(machine + ":sample_gap")
    for group in snapshot.get("events", []):
        if group["count"]:
            review.append({"source": group["source"], "kind": group["kind"],
                           "count": group["count"]})
    status = ("fail" if failed else "evidence_incomplete" if incomplete
              else "review_required" if review else "pass")
    return {"status": status, "since": since.isoformat(), "until": until.isoformat(),
            "failures": sorted(set(failed)), "evidence_gaps": sorted(set(incomplete)),
            "modes": mode_reports, "machines": list(machines.values()), "event_review": review,
            "note": "真实故障不自动等于迁移失败；本工具不放行退役、不补造部署前日志。"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, type=moment)
    parser.add_argument("--log-dir", type=Path,
                        default=Path.home() / ".local/log/console-stack")
    args = parser.parse_args(argv)
    until = dt.datetime.now(dt.timezone.utc)
    try:
        result = subprocess.run(
            ["docker", "exec", "console-brain", "python3", "-c", SNAPSHOT_CODE,
             args.since.isoformat(), until.isoformat()],
            check=True, capture_output=True, text=True, timeout=60,
        )
        snapshot = json.loads(result.stdout)
        histories = {}
        for mode in PERIODS:
            path = args.log_dir / ("monitoring-" + mode + ".log")
            histories[mode] = path.read_text() if path.is_file() else ""
        report = check_observation(snapshot, histories, args.since, until)
    except Exception as exc:
        # 子进程 stderr/原始数据可能含配置，不把正文带入报告。
        report = {"status": "fail", "failures": [type(exc).__name__]}
    print(json.dumps(report, ensure_ascii=False, default=str))
    return {"pass": 0, "fail": 1, "evidence_incomplete": 2, "review_required": 3}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
