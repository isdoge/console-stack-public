# -*- coding: utf-8 -*-
"""monitoring_maintenance 的真 PG16 回归（B6-3）。

锁死的调度纪律：authority 门禁 fail closed、retention 任务序（先聚合后清理）、
**聚合失败跳过依赖它的 cleanup_raw（R2：未聚合进 hourly 的 raw 不允许被清走）**、
单实例 flock、原子状态 JSON 的生命周期字段、影子模式 notify=False、
评估器机器过滤 = active enrolled ∩ build_rules、部分失败退出非零。
"""
import datetime
import json
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import monitoring_alert as ma  # noqa: E402
import monitoring_ingest as mi  # noqa: E402
import monitoring_maintenance as mm  # noqa: E402
from pg02_support import count_rows  # noqa: E402

pytest_plugins = ("pg02_support",)


def _base():
    """基准时刻：now - 2 天、取整小时（同 test_monitoring_ingest 的锚法：
    永远在已完结小时、也在 raw 30 天保留期内，窗口不随运行时刻漂移）。"""
    return (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)
    ).replace(minute=0, second=0, microsecond=0)


def _ts(minute=0):
    return _base() + datetime.timedelta(minutes=minute)


def _sample(metric, minute, value):
    return {"metric_name": metric, "metric_value": value, "sampled_at": _ts(minute)}


def _overdue_sample(metric="cpu", value=10.0):
    """超过 30 天保留期、尚未聚合的 raw 样本（R2 的边界数据：聚合失败时
    cleanup_raw 若照跑，这行就是会被清走的未聚合数据）。"""
    return {
        "metric_name": metric,
        "metric_value": value,
        "sampled_at": datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=45),
    }


@pytest.fixture()
def status_path(tmp_path, monkeypatch):
    path = tmp_path / "monitoring" / "maintenance.json"
    monkeypatch.setenv(mm.STATUS_JSON_ENV, str(path))
    return path


@pytest.fixture()
def loaded_machines(pg_authority):
    """两台 active 机器 + 一台吊销机器，各带样本。"""
    mi.enrol_machine("nas", "NAS")
    mi.enrol_machine("txy", "TXY")
    mi.enrol_machine("ghost", "已吊销")
    mi.revoke_machine("ghost")
    mi.insert_samples("nas", [_sample("cpu", 0, 10.0)], "0.1.0")
    mi.insert_samples("txy", [_sample("cpu", 0, 20.0)], "0.1.0")
    return pg_authority


# ---- authority 门禁 ---------------------------------------------------------


def test_client_role_refuses_to_run_and_writes_nothing(pg_client, status_path):
    """client 角色：不执行任何任务、不写状态文件、退出码 0（不是故障）。"""
    mi.enrol_machine("nas", "NAS")
    mi.insert_samples("nas", [_sample("cpu", 0, 10.0)], "0.1.0")

    assert mm.run_mode("retention") is None
    assert not status_path.exists()
    assert count_rows(pg_client, "monitoring_hourly") == 0
    assert mm.main(["retention"]) == 0


def test_non_pg_engine_refuses_to_run(monkeypatch, status_path):
    """非 PG 引擎：sqlite 下任务入口拒绝，不写状态文件。"""
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "sqlite")
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authority")
    assert mm.run_mode("retention") is None
    assert not status_path.exists()


def test_read_only_mode_refuses_to_run(pg_readonly, status_path):
    """read_only 维护模式：门禁不放行（权威端维护窗口内维护也暂停）。"""
    assert mm.run_mode("retention") is None
    assert not status_path.exists()


# ---- retention：任务序与状态 JSON -------------------------------------------


def test_retention_runs_aggregate_before_cleanup(pg_authority, status_path):
    """任务序 aggregate → cleanup_raw → cleanup_hourly 全部成功落状态。"""
    mi.enrol_machine("nas", "NAS")
    mi.insert_samples("nas", [_sample("cpu", 0, 10.0)], "0.1.0")

    payload = mm.run_mode("retention")

    assert payload["errors"] == {}
    assert list(payload["tasks"]) == [
        "aggregate_hourly", "cleanup_raw", "cleanup_hourly"]
    assert payload["tasks"]["aggregate_hourly"]["buckets"] == 1
    assert count_rows(pg_authority, "monitoring_hourly") == 1
    assert payload["last_success"] == payload["finished"]
    on_disk = json.loads(status_path.read_text(encoding="utf-8"))
    assert on_disk["mode"] == "retention"
    assert on_disk["finished"] is not None
    assert on_disk["last_success"] == on_disk["finished"]
    assert on_disk["notify"] is False


def test_cleanup_raw_respects_aggregation_boundary(pg_authority, status_path):
    """保留边界：刚聚合过的 raw 不被 cleanup_raw 误删（30 天保留期）。"""
    mi.enrol_machine("nas", "NAS")
    mi.insert_samples("nas", [_sample("cpu", 0, 10.0)], "0.1.0")
    payload = mm.run_mode("retention")
    assert payload["tasks"]["cleanup_raw"]["deleted_raw"] == 0
    assert count_rows(pg_authority, "monitoring_samples") == 1


def test_retention_failure_keeps_other_tasks_and_last_success(
        pg_authority, status_path, monkeypatch):
    """部分失败：不依赖失败任务的任务照跑、错误入 JSON、last_success 不更新；
    依赖失败的 cleanup_raw 被跳过并写明原因（R2 修复纠正了旧断言）。"""
    mi.enrol_machine("nas", "NAS")
    mi.insert_samples("nas", [_sample("cpu", 0, 10.0)], "0.1.0")
    first = mm.run_mode("retention")
    assert first["errors"] == {}

    def broken_aggregate():
        raise RuntimeError("聚合炸了（测试注入）")

    monkeypatch.setattr(mm, "_task_aggregate", broken_aggregate)
    second = mm.run_mode("retention")

    assert "aggregate_hourly" in second["errors"]
    assert "RuntimeError" in second["errors"]["aggregate_hourly"]
    # cleanup_raw 依赖聚合：跳过（记 skipped 原因），不再执行。
    assert second["tasks"]["cleanup_raw"] == {
        "skipped": "依赖任务失败: aggregate_hourly"}
    assert "cleanup_raw" not in second["errors"]
    # cleanup_hourly 不依赖 raw 聚合（只清 hourly 表自身）：照跑。
    assert second["tasks"]["cleanup_hourly"]["deleted_hourly"] == 0
    assert second["last_success"] == first["last_success"]  # 不被失败轮覆盖
    on_disk = json.loads(status_path.read_text(encoding="utf-8"))
    assert on_disk["errors"]["aggregate_hourly"]
    assert on_disk["tasks"]["cleanup_raw"]["skipped"] == "依赖任务失败: aggregate_hourly"
    assert mm.main(["retention"]) == 1  # 注入仍在：非零退出


# ---- retention：R2 聚合失败守卫（未聚合 raw 不允许被清走） ---------------------


def test_aggregate_failure_keeps_overdue_unaggregated_raw(
        pg_authority, status_path, monkeypatch):
    """R2 回归：聚合失败时，超过保留期且尚未聚合的 raw 必须保留在库里——
    cleanup_raw 不执行，数据一行不丢；cleanup_hourly（不依赖 raw 聚合）照跑。"""
    mi.enrol_machine("nas", "NAS")
    mi.insert_samples(
        "nas", [_overdue_sample(), _sample("cpu", 0, 10.0)], "0.1.0")
    assert count_rows(pg_authority, "monitoring_hourly") == 0  # 尚未聚合

    def broken_aggregate():
        raise RuntimeError("synthetic_aggregate_failure")

    monkeypatch.setattr(mm, "_task_aggregate", broken_aggregate)
    payload = mm.run_mode("retention")

    assert "aggregate_hourly" in payload["errors"]
    # 过期未聚合 raw 一行没丢：样本总数不变（过期 1 行 + 保留期内 1 行）。
    assert count_rows(pg_authority, "monitoring_samples") == 2
    # 状态明确记录 cleanup_raw 被跳过及原因。
    assert payload["tasks"]["cleanup_raw"]["skipped"] == "依赖任务失败: aggregate_hourly"
    on_disk = json.loads(status_path.read_text(encoding="utf-8"))
    assert on_disk["tasks"]["cleanup_raw"]["skipped"] == "依赖任务失败: aggregate_hourly"
    # cleanup_hourly 不依赖 raw 聚合，照常执行。
    assert payload["tasks"]["cleanup_hourly"]["deleted_hourly"] == 0


def test_overdue_unaggregated_raw_cleaned_only_after_successful_aggregate(
        pg_authority, status_path, monkeypatch):
    """R2 回归的后半程：聚合恢复成功后，同一批过期 raw 才允许被清走——
    证明守卫挡的是「未聚合就清」，不是永久禁止清理。"""
    mi.enrol_machine("nas", "NAS")
    mi.insert_samples(
        "nas", [_overdue_sample(), _sample("cpu", 0, 10.0)], "0.1.0")

    def broken_aggregate():
        raise RuntimeError("synthetic_aggregate_failure")

    monkeypatch.setattr(mm, "_task_aggregate", broken_aggregate)
    failed = mm.run_mode("retention")
    assert "aggregate_hourly" in failed["errors"]
    assert count_rows(pg_authority, "monitoring_samples") == 2  # 数据未丢

    monkeypatch.undo()  # 聚合恢复
    payload = mm.run_mode("retention")

    assert payload["errors"] == {}
    assert payload["tasks"]["aggregate_hourly"]["buckets"] == 2
    # 聚合成功后：过期样本（45 天前，已进 hourly）被清走，保留期内的留下。
    assert payload["tasks"]["cleanup_raw"]["deleted_raw"] == 1
    assert count_rows(pg_authority, "monitoring_samples") == 1
    assert count_rows(pg_authority, "monitoring_hourly") == 2
    assert payload["last_success"] == payload["finished"]


# ---- evaluate / dms：影子模式与机器过滤 --------------------------------------


def test_evaluate_defaults_to_shadow_notify_false(loaded_machines, status_path):
    """默认影子：evaluate 跑完规则面但 notified==0，没有发信动作。"""
    payload = mm.run_mode("evaluate")
    assert payload["errors"] == {}
    task = payload["tasks"]["run_evaluation"]
    assert task["rules_evaluated"] == 6  # nas + txy 各 3 条（ghost 已吊销被过滤）
    assert task["notified"] == 0
    assert payload["notify"] is False


def test_evaluate_machine_filter_intersects_active_and_rules(
        loaded_machines, status_path):
    """机器过滤：active enrolled ∩ build_rules——吊销的 ghost 不进评估。"""
    machines = mm.evaluation_machines()
    assert machines == ["nas", "txy"]  # 每机一条（Status），无重复
    rule_machines = sorted({rule["machine"] for rule in ma.build_rules()})
    assert set(machines) <= set(rule_machines)


def test_evaluate_unknown_machine_in_registry_is_not_evaluated(
        pg_authority, status_path):
    """登记了规则面之外的机器：build_rules 没有它的规则，交集自动排除。"""
    mi.enrol_machine("unknown-box", "不在 B0 规则矩阵里")
    assert mm.evaluation_machines() == []


def test_dms_defaults_to_shadow_notify_false(loaded_machines, status_path):
    payload = mm.run_mode("dms")
    assert payload["errors"] == {}
    task = payload["tasks"]["check_dead_mans_switch"]
    assert task["machines_checked"] == 2  # DMS 走 active 机器面（含无规则的）
    assert task["notified"] == 0


def test_notify_flag_reaches_tasks(loaded_machines, status_path, monkeypatch):
    """--notify 是显式开关：传了才把 notify=True 传进评估入口。"""
    captured = {}

    def fake_evaluate(notify, machines):
        captured["notify"] = notify
        captured["machines"] = machines
        return {"results": [], "opened": 0, "resolved": 0,
                "notified": 0, "delivery_failures": 0}

    monkeypatch.setattr(ma, "run_evaluation", fake_evaluate)
    mm.run_mode("evaluate", notify=True)
    assert captured["notify"] is True

    mm.run_mode("evaluate")
    assert captured["notify"] is False


# ---- 单实例 flock ------------------------------------------------------------


def test_second_instance_of_same_mode_is_refused(loaded_machines, status_path):
    with mm._SingleInstance("retention"):
        with pytest.raises(mm.MaintenanceLockBusy):
            with mm._SingleInstance("retention"):
                pass


def test_different_modes_do_not_block_each_other(loaded_machines, status_path):
    """长聚合不阻塞告警：retention 与 evaluate/dms 的锁分开。"""
    with mm._SingleInstance("retention"):
        with mm._SingleInstance("evaluate"):
            pass  # 不抛即通过


def test_main_returns_nonzero_when_lock_busy(
        loaded_machines, status_path, monkeypatch):
    mm.run_mode("retention")  # 先留一个 last_success 在状态里
    held = mm._SingleInstance("retention")
    held.__enter__()
    try:
        assert mm.main(["retention"]) == 3
    finally:
        held.__exit__(None, None, None)


# ---- 状态 JSON 原子性与内容 ---------------------------------------------------


def test_status_json_has_no_secrets_and_required_fields(
        loaded_machines, status_path):
    payload = mm.run_mode("retention")
    on_disk = json.loads(status_path.read_text(encoding="utf-8"))
    assert on_disk["mode"] == "retention"
    assert on_disk["role"] == "authority"
    for field in ("started", "finished", "last_success"):
        assert on_disk[field]
    # 顶层与嵌套都不得出现任何环境变量式秘密字段。
    text = status_path.read_text(encoding="utf-8")
    for secret_key in ("PASSWORD", "SECRET", "TOKEN", "SMTP"):
        assert secret_key not in text
    assert on_disk["tasks"] == payload["tasks"]


def test_status_survives_corrupt_previous_file(loaded_machines, status_path):
    """上一轮 JSON 损坏：load_status 回 None，本轮照常重写，不崩。"""
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text("{not json", encoding="utf-8")
    payload = mm.run_mode("retention")
    assert payload["errors"] == {}
    on_disk = json.loads(status_path.read_text(encoding="utf-8"))
    assert on_disk["last_success"] == on_disk["finished"]


def test_non_object_previous_mode_state_is_ignored(loaded_machines, status_path):
    path = pathlib.Path(mm.status_json_path("retention"))
    path.parent.mkdir(parents=True)
    path.write_text("[]")
    assert mm.run_mode("retention")["errors"] == {}


def test_unknown_mode_is_rejected(status_path):
    with pytest.raises(ValueError):
        mm.run_mode("nonsense")


def test_each_mode_keeps_its_own_result_and_last_success(
        loaded_machines, status_path, monkeypatch):
    first = mm.run_mode("retention")
    monkeypatch.setattr(mm, "tasks_for", lambda *a, **k: [
        ("run_evaluation", frozenset(), lambda: {"delivery_failures": 1})])
    second = mm.run_mode("evaluate", notify=True)
    assert second["last_success"] is None
    assert second["errors"] == {"run_evaluation": "DeliveryFailure"}
    retention = json.loads(pathlib.Path(mm.status_json_path("retention")).read_text())
    assert retention == first
    assert json.loads(status_path.read_text()) == second


@pytest.mark.parametrize("mode", ["evaluate", "dms"])
def test_delivery_failure_is_logged_and_exits_nonzero(
        loaded_machines, status_path, monkeypatch, capsys, mode):
    monkeypatch.setattr(mm, "tasks_for", lambda *a, **k: [
        ("send", frozenset(), lambda: {"notified": 1, "delivery_failures": 1})])
    assert mm.main([mode, "--notify"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == mode and report["finished"]
    assert report["errors"] == {"send": "DeliveryFailure"}
    assert report["last_success"] is None
    assert pathlib.Path(mm.status_json_path(mode)).exists()


@pytest.mark.parametrize("mode", ["evaluate", "dms"])
def test_delivery_stage_reaches_host_terminal_log(
        loaded_machines, status_path, monkeypatch, capsys, mode):
    report = {
        "results": [{"machine": "nas", "action": "opened", "delivery": {
            "ok": False, "stage": "authenticate", "category": "authentication",
            "smtp_code": 535, "attempts": 1, "error": "private-password",
        }}],
        "opened": 1, "resolved": 0, "notified": 1, "delivery_failures": 1,
    }
    monkeypatch.setattr(ma, "run_evaluation", lambda **kwargs: report)
    monkeypatch.setattr(ma, "check_dead_mans_switch", lambda **kwargs: report)
    assert mm.main([mode, "--notify"]) == 1
    output = capsys.readouterr().out
    payload = json.loads(output)
    task = next(iter(payload["tasks"].values()))
    assert task["deliveries"][0]["stage"] == "authenticate"
    assert task["deliveries"][0]["smtp_code"] == 535
    assert payload["errors"]
    assert "private-password" not in output + pathlib.Path(mm.status_json_path(mode)).read_text()


def test_successful_round_is_emitted_to_host_log(loaded_machines, status_path, capsys):
    payload = mm.run_mode("retention")
    assert json.loads(capsys.readouterr().out) == payload


def test_failure_report_never_exposes_exception_message(
        loaded_machines, status_path, monkeypatch, capsys):
    def broken():
        raise RuntimeError("synthetic-private-value-must-not-appear")

    monkeypatch.setattr(mm, "_task_aggregate", broken)
    assert mm.main(["retention"]) == 1
    captured = capsys.readouterr()
    assert "synthetic-private-value" not in captured.out + captured.err + status_path.read_text()
    assert json.loads(captured.out)["errors"]["aggregate_hourly"] == "RuntimeError"


def test_lock_failure_emits_terminal_report_without_clobbering_current_state(
        loaded_machines, status_path, capsys):
    payload = mm.run_mode("retention")
    capsys.readouterr()
    with mm._SingleInstance("retention"):
        assert mm.main(["retention"]) == 3
    assert json.loads(capsys.readouterr().out)["errors"] == {"lock": "MaintenanceLockBusy"}
    assert json.loads(status_path.read_text()) == payload
