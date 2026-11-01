"""只读观察门禁的隔离记录；不安装 cron，不接触真实 SSH/Docker/SMTP。"""
import datetime as dt
import json
import subprocess

import pytest

import check_monitoring_observation as check

UTC = dt.timezone.utc
END = dt.datetime(2026, 10, 25, 12, 0, 45, tzinfo=UTC)
START = END - dt.timedelta(hours=24)


@pytest.fixture()
def evidence():
    histories, current = {}, {}
    for mode, period in check.PERIODS.items():
        rows = []
        slot = int(START.timestamp()) // period * period + period
        while slot <= END.timestamp() - 30:
            stamp = dt.datetime.fromtimestamp(slot, UTC)
            rows.append({"schema_version": 1, "mode": mode,
                         "started": stamp.isoformat(),
                         "finished": (stamp + dt.timedelta(seconds=2)).isoformat(),
                         "role": "authority", "source": "console_pg",
                         "notify": mode != "retention", "errors": {},
                         "tasks": {task: {"delivery_failures": 0} for task in check.TASKS[mode]}})
            slot += period
        histories[mode] = "\n".join(json.dumps(row) for row in rows)
        current[mode] = rows[-1]
    snapshot = {
        "generated_at": END.isoformat(), "source": "console_pg", "role": "authority",
        "maintenance": current, "events": [],
        "machines": [{"machine_id": machine, "rounds": 288,
                      "first_sample": (START + dt.timedelta(minutes=5)).isoformat(),
                      "last_sample": (END - dt.timedelta(minutes=3)).isoformat(),
                      "last_seen_at": END.isoformat(), "max_gap_sec": 300}
                     for machine in check.MACHINES],
    }
    return snapshot, histories


def verify(evidence):
    return check.check_observation(*evidence, START, END)


def change_round(evidence, mode, update):
    rows = [json.loads(line) for line in evidence[1][mode].splitlines()]
    update(rows[len(rows) // 2])  # 故障之后仍有很多成功轮，不能被最新成功覆盖。
    evidence[1][mode] = "\n".join(json.dumps(row) for row in rows)


def test_complete_window_passes_without_writes(evidence):
    assert verify(evidence)["status"] == "pass"


def test_deployment_cannot_backfill_missing_history(evidence):
    evidence[1]["retention"] = ""
    report = verify(evidence)
    assert report["status"] == "evidence_incomplete"
    assert "retention:missing_scheduled_rounds" in report["evidence_gaps"]


def test_one_missing_scheduled_round_is_not_hidden_by_latest_success(evidence):
    rows = evidence[1]["evaluate"].splitlines()
    evidence[1]["evaluate"] = "\n".join(rows[:10] + rows[11:])
    assert verify(evidence)["status"] == "evidence_incomplete"


@pytest.mark.parametrize("mode", ["evaluate", "dms"])
def test_delivery_failure_is_checked_even_with_empty_errors(evidence, mode):
    change_round(evidence, mode, lambda row: row["tasks"][next(iter(check.TASKS[mode]))].update(
        delivery_failures=1))
    report = verify(evidence)
    assert report["status"] == "fail"
    assert mode + ":delivery_failure" in report["failures"]


def test_old_task_failure_is_not_overwritten(evidence):
    change_round(evidence, "retention", lambda row: row.update(errors={"aggregate_hourly": "Error"}))
    assert verify(evidence)["status"] == "fail"


@pytest.mark.parametrize("mutation", [
    {"source": "beszel"}, {"notify": False}, {"tasks": {}}, {"role": "client"},
])
def test_wrong_runtime_round_cannot_pass(evidence, mutation):
    change_round(evidence, "evaluate", lambda row: row.update(mutation))
    assert verify(evidence)["status"] == "fail"


@pytest.mark.parametrize("mutation", [
    {"source": "beszel"}, {"notify": False}, {"tasks": {}}, {"role": "client"},
    {"mode": "dms"}, {"schema_version": 0}, {"errors": None},
    {"finished": (END + dt.timedelta(minutes=1)).isoformat()},
])
def test_latest_snapshot_is_validated_independently_of_good_history(evidence, mutation):
    evidence[0]["maintenance"]["evaluate"].update(mutation)
    assert verify(evidence)["status"] == "fail"


def test_latest_delivery_failure_cannot_hide_behind_good_history(evidence):
    evidence[0]["maintenance"]["dms"]["tasks"]["check_dead_mans_switch"]["delivery_failures"] = 1
    assert "dms:delivery_failure" in verify(evidence)["failures"]


def test_fresh_in_progress_snapshot_does_not_require_finished_tasks(evidence):
    evidence[0]["maintenance"]["evaluate"].update(
        started=END.isoformat(), finished=None, tasks={})
    assert verify(evidence)["status"] == "pass"


def test_many_nas_samples_do_not_hide_missing_machine(evidence):
    evidence[0]["machines"] = evidence[0]["machines"][:-1]
    evidence[0]["machines"][2]["rounds"] = 1_000_000
    assert "us:no_agent_samples" in verify(evidence)["failures"]


def test_sample_gap_and_stale_host_are_reported_per_machine(evidence):
    evidence[0]["machines"][0]["max_gap_sec"] = 900
    evidence[0]["machines"][1]["last_seen_at"] = START.isoformat()
    report = verify(evidence)
    assert "aly:sample_gap" in report["failures"]
    assert "kr:stale_heartbeat" in report["failures"]


def test_real_event_needs_review_not_automatic_migration_failure(evidence):
    evidence[0]["events"] = [{"source": "monitoring-alert", "kind": "machine", "count": 1}]
    report = verify(evidence)
    assert report["status"] == "review_required"
    assert report["failures"] == []


def test_clock_only_and_malformed_json_cannot_pass(evidence):
    evidence[1]["dms"] += '\n{"broken"'
    assert verify(evidence)["status"] == "evidence_incomplete"
    report = check.check_observation(*evidence, END - dt.timedelta(hours=1), END)
    assert "window_shorter_than_24h" in report["evidence_gaps"]


def test_child_failure_exits_nonzero_without_leaking_output(monkeypatch, capsys, tmp_path):
    def broken(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "docker", stderr="synthetic-private-value")

    monkeypatch.setattr(check.subprocess, "run", broken)
    assert check.main(["--since", START.isoformat(), "--log-dir", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "fail"
    assert "synthetic-private-value" not in output
