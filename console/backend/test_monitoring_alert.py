# -*- coding: utf-8 -*-
"""monitoring_alert 的回归（B5-4）。

两层：纯函数层（规则清单、run 重建、阈值判定、DMS 判定、邮件渲染、配置）
直接测；PG 层（五状态切换、进程重启重建、回填排除、事件落库与通知去重）
跑真 PG16（pg02_support 夹具），样本经 monitoring_ingest 落库走真实查询路径。
"""
import datetime
import os
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import monitor_events as me  # noqa: E402
import monitoring_alert as ma  # noqa: E402
import monitoring_ingest as mi  # noqa: E402
import monitoring_schema as ms  # noqa: E402
import storage_dispatch as sd  # noqa: E402

pytest_plugins = ("pg02_support",)


def _now():
    """基准时刻：now - 2 天取整 5 分钟。远离「今天」避免 raw 保留边界。"""
    return (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)
    ).replace(second=0, microsecond=0)


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def _at(base, minutes):
    return base + datetime.timedelta(minutes=minutes)


def _sample(metric, at, value):
    return {"metric_name": metric, "metric_value": value, "sampled_at": at}


class _Recorder:
    """假投递器：记录 (subject, body)，永不失败。"""

    def __init__(self):
        self.sent = []

    def __call__(self, subject, body):
        self.sent.append((subject, body))
        return {"ok": True, "error": None}


# ---- 规则清单（B0 冻结） ------------------------------------------------------


def test_build_rules_freezes_18_rules():
    rules = ma.build_rules()
    assert len(rules) == 18
    by_machine = {}
    for rule in rules:
        by_machine.setdefault(rule["machine"], []).append(rule["name"])
    assert set(by_machine) == {"nas", "txy", "aly", "us", "kr", "sg"}
    for names in by_machine.values():
        assert sorted(names) == ["Disk", "LoadAvg5", "Status"]
    load5 = {r["machine"]: r["threshold"] for r in rules if r["name"] == "LoadAvg5"}
    assert load5 == {"nas": 4.0, "txy": 4.0, "us": 4.0, "aly": 2.0, "kr": 2.0, "sg": 2.0}
    disk = {r["threshold"] for r in rules if r["name"] == "Disk"}
    assert disk == {85.0}
    duration = {r["duration_sec"] for r in rules if r["name"] != "Status"}
    assert duration == {600}
    status_min = {r["duration_sec"] for r in rules if r["name"] == "Status"}
    assert status_min == {300}


def test_rule_fingerprint_isolated_from_probe_and_dms():
    # 与探活 machine:<id>、B5-1 采集中断 machine:monitoring:<id> 物理隔离。
    assert ma.rule_fingerprint("nas", "Disk") == "machine:alert:Disk:nas"
    assert ma.rule_fingerprint("nas", "Disk") != me.fingerprint(me.KIND_MACHINE, "nas")
    assert ma.rule_fingerprint("nas", "Status") != me.fingerprint(
        me.KIND_MACHINE, "monitoring:nas")


# ---- 纯函数：run 重建与阈值判定 ----------------------------------------------


def _disk_rule(machine="nas"):
    return next(r for r in ma.build_rules() if r["machine"] == machine and r["name"] == "Disk")


def test_trailing_run_breaks_on_clear_and_gap():
    base = _now()
    samples = [
        (_at(base, 0), 90.0),   # 违规
        (_at(base, 5), 90.0),   # 违规
        (_at(base, 10), 40.0),  # 清零 → run 断
        (_at(base, 15), 86.0),  # 新 run 起点
        (_at(base, 20), 88.0),  # 违规
    ]
    run = ma._trailing_run(samples, 85.0)
    assert run == [(_at(base, 15), 86.0), (_at(base, 20), 88.0)]
    # 缺口（间隔 > 12 分钟）同样断 run。
    gapped = [
        (_at(base, 0), 90.0),
        (_at(base, 30), 90.0),  # 间隔 30 分钟 > 连续性边界
        (_at(base, 35), 90.0),
    ]
    run = ma._trailing_run(gapped, 85.0)
    assert run == [(_at(base, 30), 90.0), (_at(base, 35), 90.0)]


def test_assess_threshold_rule_sustained_and_clear_window():
    base = _now()
    rule = _disk_rule()
    samples = [
        (_at(base, 0), 90.0),
        (_at(base, 5), 91.0),
        (_at(base, 10), 92.0),
    ]
    assessment = ma.assess_threshold_rule(rule, samples, _at(base, 11))
    assert assessment["violating"] is True
    assert assessment["sustained_sec"] == 600  # 首 10 分钟持续 → 满足
    assert assessment["fresh"] is True
    assert assessment["clear_in_window"] is False

    # 恢复窗口内出现过清零观测 → 回退判定输入成立。
    samples_with_clear = [
        (_at(base, 0), 90.0),
        (_at(base, 5), 40.0),
        (_at(base, 10), 90.0),
        (_at(base, 15), 91.0),
    ]
    assessment = ma.assess_threshold_rule(rule, samples_with_clear, _at(base, 16))
    assert assessment["violating"] is True
    assert assessment["clear_in_window"] is True


def test_assess_threshold_rule_stale_observation_not_violating():
    base = _now()
    rule = _disk_rule()
    # 最新观测 13 分钟前：条件即便满足也不能触发（unknown，不新触发）。
    samples = [(_at(base, 0), 90.0), (_at(base, 5), 90.0)]
    assessment = ma.assess_threshold_rule(rule, samples, _at(base, 18))
    assert assessment["violating"] is False
    assert assessment["fresh"] is False


def test_assess_status_rule_offline_after_5min_plus_grace():
    base = _now()
    rule = next(r for r in ma.build_rules() if r["machine"] == "nas" and r["name"] == "Status")
    # 静默 5 分钟整（含抖动余量内）不触发。
    assert ma.assess_status_rule(rule, [_at(base, 0)], _at(base, 5))["violating"] is False
    # 2026-10-25 余量实测调为整周期 300s（六机 300s 周期 + 60-68s 到达延迟 +
    # 每分钟 cron 相位，60s 余量几何上必踩边）：静默 10 分钟仍不触发；11 分钟
    # 才越过 600s 边界。aly 锯齿复现用例：300s 周期 + 61s 延迟的静默 361s 不触发。
    assert ma.assess_status_rule(rule, [_at(base, 0)], _at(base, 6))["violating"] is False
    assert ma.assess_status_rule(rule, [_at(base, 0)], _at(base, 10))["violating"] is False
    assert ma.assess_status_rule(rule, [_at(base, 0)], _at(base, 11))["violating"] is True
    # 从未上报 → 不触发（never，不是离线）。
    assert ma.assess_status_rule(rule, [], _at(base, 0))["violating"] is False
    # 在线链：从打破静默的样本起算清零时长。
    out = ma.assess_status_rule(rule, [_at(base, 0), _at(base, 20)], _at(base, 21))
    assert out["cleared_sec"] >= 60  # 链起点 t=20，清零约 1 分钟


def test_status_rule_no_sawtooth_at_300s_period():
    """aly 锯齿回归钉子（2026-10-25）：300s 周期 + 61s 延迟的机器在每分钟
    evaluate 下，任何相位都不应触发离线。锯齿实况见 b6-3-consumers.md §8。"""
    base = _now()
    rule = next(r for r in ma.build_rules() if r["machine"] == "aly" and r["name"] == "Status")
    # 复刻实测：样本 xx:01:00，落库 xx:02:02，评估 xx:07:01 → silence 361s。
    for eval_min in range(1, 7):  # 覆盖评估落点 xx:02..xx:07
        now = _at(base, eval_min + 1)
        assert ma.assess_status_rule(rule, [_at(base, 1)], now)["violating"] is False, eval_min


# ---- 纯函数：DMS 判定与邮件 --------------------------------------------------


def test_dms_two_forms_and_never():
    base = _now()
    # 心跳正常但样本停更 13 分钟 → stale（形态一）。
    out = ma.assess_dead_mans_switch("nas", _at(base, 0), _at(base, -13), _at(base, 0))
    assert out["form"] == "stale"
    # 心跳本身停 13 分钟 → disconnected（形态二）。
    out = ma.assess_dead_mans_switch("nas", _at(base, -13), _at(base, -13), _at(base, 0))
    assert out["form"] == "disconnected"
    # 从未上报 → never（能力问题不是中断）。
    assert ma.assess_dead_mans_switch("nas", None, None, base)["form"] == "never"
    # 一切正常 → ok。
    assert ma.assess_dead_mans_switch("nas", _at(base, -1), _at(base, -2), base)["form"] == "ok"


def test_render_emails_carry_beszel_fields_no_credentials():
    rule = _disk_rule()
    assessment = {"current_value": 91.2, "sustained_sec": 600}
    subject, body = ma.render_alert_email(rule, assessment, "2026-10-24T00:00:00Z")
    assert "NAS" in subject and "磁盘" in subject
    for field in ("机器", "规则", "当前值", "阈值", "持续时长", "触发时间"):
        assert field in body
    assert "91" in body and "85%" in body and "10 分钟" in body
    subject, body = ma.render_dms_email("NAS", "nas", {
        "form": "stale", "last_sampled_at": "2026-10-24T00:00:00Z", "silence_sec": 800})
    assert "采集中断" in subject and "NAS" in body


def test_smtp_config_reads_env_and_masks_absence(monkeypatch):
    for key in list(os_environ_alert_keys()):
        monkeypatch.delenv(key, raising=False)
    assert ma.smtp_config() is None
    monkeypatch.setenv("CONSOLE_ALERT_SMTP_HOST", "smtp.qq.com")
    monkeypatch.setenv("CONSOLE_ALERT_SMTP_USER", "user@example.com")
    monkeypatch.setenv("CONSOLE_ALERT_SMTP_PASSWORD", "secret")
    config = ma.smtp_config()
    assert config["host"] == "smtp.qq.com"
    assert config["port"] == 465
    assert config["sender"] == "user@example.com"  # 缺省 FROM = USER
    assert config["recipients"] == []
    monkeypatch.setenv("CONSOLE_ALERT_SMTP_TO", "a@x.com, b@x.com")
    assert ma.smtp_config()["recipients"] == ["a@x.com", "b@x.com"]


def os_environ_alert_keys():
    return [k for k in os.environ if k.startswith(ma.SMTP_ENV_PREFIX)]


def test_smtp_send_without_config_reports_not_configured(monkeypatch):
    for key in os_environ_alert_keys():
        monkeypatch.delenv(key, raising=False)
    out = ma.smtp_send("s", "b")
    assert out == {"ok": False, "error": "smtp-not-configured"}


# ---- PG 层：五状态 + 事件持久化 + 回填排除 ------------------------------------


@pytest.fixture()
def alert_env(pg_client):
    """PG 库 + 已 enrol 的机器 + 假投递器。"""
    mi.enrol_machine("nas", "NAS 主机")
    mi.enrol_machine("kr", "KR 主机")
    recorder = _Recorder()
    return recorder


def _write(machine, samples, source="go-agent/0.1.0"):
    return mi.insert_samples(machine, samples, source)


def test_pg_evaluation_full_lifecycle(pg_client, alert_env):
    recorder = alert_env
    base = _now()
    rule = _disk_rule()

    # t=0/5/10：持续 10 分钟违规 → 首轮评估（t=11）即触发，opened + 通知。
    _write("nas", [_sample(ms.METRIC_DISK_USED_PCT, _at(base, 0), 90.0),
                   _sample(ms.METRIC_DISK_USED_PCT, _at(base, 5), 91.0),
                   _sample(ms.METRIC_DISK_USED_PCT, _at(base, 10), 92.0)])
    out = ma.evaluate_rule(rule, now=_at(base, 11), send=recorder)
    assert out["state"] == "triggered" and out["action"] == "opened"
    assert out["notified"] is True and len(recorder.sent) == 1

    # 再评估：事件已活动 → triggered 接续，不重复通知。
    out = ma.evaluate_rule(rule, now=_at(base, 12), send=recorder)
    assert out["state"] == "triggered" and out["action"] == "updated"
    assert out["notified"] is False and len(recorder.sent) == 1

    # t=15 清零：恢复窗口（10 分钟）内 → recovering，事件保持打开。
    _write("nas", [_sample(ms.METRIC_DISK_USED_PCT, _at(base, 15), 40.0)])
    out = ma.evaluate_rule(rule, now=_at(base, 16), send=recorder)
    assert out["state"] == "recovering" and out["action"] is None
    assert me.active_events()[0]["fingerprint"] == ma.rule_fingerprint("nas", "Disk")

    # t=20 又违规（恢复窗口内）：reverted，接续原事件不重发。
    _write("nas", [_sample(ms.METRIC_DISK_USED_PCT, _at(base, 20), 93.0)])
    out = ma.evaluate_rule(rule, now=_at(base, 21), send=recorder)
    assert out["state"] == "reverted" and out["notified"] is False
    assert len(recorder.sent) == 1
    events = me.active_events()
    assert len(events) == 1 and events[0]["opened_at"] == ma._iso(_at(base, 11))

    # t=30 清零，稳定超恢复窗后（t=41）→ resolved + 恢复通知。
    _write("nas", [_sample(ms.METRIC_DISK_USED_PCT, _at(base, 30), 41.0)])
    out = ma.evaluate_rule(rule, now=_at(base, 31))
    assert out["state"] == "recovering"
    out = ma.evaluate_rule(rule, now=_at(base, 41), send=recorder)
    assert out["state"] == "clear" and out["action"] == "resolved"
    assert out["notified"] is True and len(recorder.sent) == 2
    assert me.active_events() == []


def test_pg_evaluation_timing_then_trigger(pg_client, alert_env):
    """计时中 → 已触发的完整路径（首轮只有 5 分钟持续）。"""
    alert_env
    base = _now()
    rule = _disk_rule()
    _write("nas", [_sample(ms.METRIC_DISK_USED_PCT, _at(base, 0), 90.0),
                   _sample(ms.METRIC_DISK_USED_PCT, _at(base, 5), 91.0)])
    out = ma.evaluate_rule(rule, now=_at(base, 6))
    assert out["state"] == "timing" and out["action"] is None
    assert me.active_events() == []

    _write("nas", [_sample(ms.METRIC_DISK_USED_PCT, _at(base, 10), 92.0)])
    out = ma.evaluate_rule(rule, now=_at(base, 11))
    assert out["state"] == "triggered" and out["action"] == "opened"


def test_pg_restart_does_not_renotify_or_resolve(pg_client, alert_env):
    """进程重启语义：无内存状态，从 PG 重建；不重复通知、不自动恢复。"""
    recorder = alert_env
    base = _now()
    rule = _disk_rule()
    _write("nas", [_sample(ms.METRIC_DISK_USED_PCT, _at(base, 0), 90.0),
                   _sample(ms.METRIC_DISK_USED_PCT, _at(base, 5), 91.0),
                   _sample(ms.METRIC_DISK_USED_PCT, _at(base, 10), 92.0)])
    assert ma.evaluate_rule(rule, now=_at(base, 11), send=recorder)["notified"] is True

    # 「重启」= 模块状态全丢（本来就无内存状态），新一轮评估同 DB。
    fresh = _Recorder()
    out = ma.evaluate_rule(rule, now=_at(base, 12), send=fresh)
    assert out["state"] == "triggered" and out["action"] == "updated"
    assert fresh.sent == []  # 已触发事件不因重启重复通知

    # 观测缺口（静默 13 分钟）：unknown——不自动恢复、不发信。
    out = ma.evaluate_rule(rule, now=_at(base, 25))
    assert out["state"] == "unknown" and out["action"] is None
    assert len(me.active_events()) == 1
    # 缺口后有效观测恢复清零，稳定超恢复窗后才 resolve。
    _write("nas", [_sample(ms.METRIC_DISK_USED_PCT, _at(base, 40), 40.0)])
    out = ma.evaluate_rule(rule, now=_at(base, 41))
    assert out["state"] == "recovering"
    out = ma.evaluate_rule(rule, now=_at(base, 51))
    assert out["action"] == "resolved"


def test_pg_backfill_samples_never_trigger_or_sustain(pg_client, alert_env):
    """回填样本不参与评估：不触发、不断 run、不推进离线计时。"""
    alert_env
    base = _now()
    rule = _disk_rule()
    # 只有回填的高磁盘样本：满窗也不触发。
    _write("nas", [_sample(ms.METRIC_DISK_USED_PCT, _at(base, 0), 99.0),
                   _sample(ms.METRIC_DISK_USED_PCT, _at(base, 5), 99.0),
                   _sample(ms.METRIC_DISK_USED_PCT, _at(base, 10), 99.0)],
           source=ms.BACKFILL_SOURCE)
    out = ma.evaluate_rule(rule, now=_at(base, 11))
    assert out["state"] == "clear" and out["action"] is None
    assert me.active_events() == []
    # 回填也不刷新样本时钟：非回填样本时间线为空（Status 按此判离线）。
    assert ma._load_sample_times("nas", _at(base, -60)) == []


def test_pg_status_rule_offline_event_and_severity(pg_client, alert_env):
    alert_env
    base = _now()
    rule = next(r for r in ma.build_rules() if r["machine"] == "kr" and r["name"] == "Status")
    # kr 最后样本在 t=0，t=12 评估（5min 阈值 + 300s 整周期余量，2026-10-25
    # 实测调）→ 离线触发，error 级。
    _write("kr", [_sample(ms.METRIC_CPU_USED_PCT, _at(base, 0), 12.0)])
    out = ma.evaluate_rule(rule, now=_at(base, 12), send=alert_env)
    assert out["state"] == "triggered" and out["action"] == "opened"
    events = me.active_events()
    assert events[0]["severity"] == "error"
    assert events[0]["fingerprint"] == ma.rule_fingerprint("kr", "Status")
    # 新样本持续到来（在线链）：链内静默 < 离线阈值 → 恢复窗口语义适用。
    # t=25/31 两笔样本维持在线，t=36 评估时静默 5 分钟 → 非离线；
    # 清零链起点 t=25、清零 11 分钟 ≥ 恢复窗 → resolved。
    _write("kr", [_sample(ms.METRIC_CPU_USED_PCT, _at(base, 25), 12.0),
                  _sample(ms.METRIC_CPU_USED_PCT, _at(base, 31), 12.0)])
    out = ma.evaluate_rule(rule, now=_at(base, 36))
    assert out["state"] == "clear" and out["action"] == "resolved"


def test_pg_load5_uses_per_machine_threshold(pg_client, alert_env):
    alert_env
    base = _now()
    # kr 阈值 2：load 2.5 触发；nas 阈值 4：同值不触发。
    _write("kr", [_sample(ms.METRIC_LOAD_AVG_5M, _at(base, 0), 2.5),
                  _sample(ms.METRIC_LOAD_AVG_5M, _at(base, 5), 2.6),
                  _sample(ms.METRIC_LOAD_AVG_5M, _at(base, 10), 2.7)])
    _write("nas", [_sample(ms.METRIC_LOAD_AVG_5M, _at(base, 0), 2.5),
                   _sample(ms.METRIC_LOAD_AVG_5M, _at(base, 5), 2.6),
                   _sample(ms.METRIC_LOAD_AVG_5M, _at(base, 10), 2.7)])
    out = ma.run_evaluation(now=_at(base, 11), machines=["kr", "nas"])
    states = {(r["machine"], r["rule"]): r["state"] for r in out["results"]}
    assert states[("kr", "LoadAvg5")] == "triggered"
    assert states[("nas", "LoadAvg5")] == "clear"
    assert out["opened"] == 1


def test_pg_run_evaluation_shadow_mode_sends_nothing(pg_client, alert_env):
    """影子模式（notify=False）：事件照落，信一封不发。"""
    recorder = alert_env
    base = _now()
    _write("nas", [_sample(ms.METRIC_DISK_USED_PCT, _at(base, 0), 90.0),
                   _sample(ms.METRIC_DISK_USED_PCT, _at(base, 5), 91.0),
                   _sample(ms.METRIC_DISK_USED_PCT, _at(base, 10), 92.0)])
    out = ma.run_evaluation(now=_at(base, 11), send=recorder, notify=False)
    assert out["opened"] == 1 and out["notified"] == 0
    assert recorder.sent == []
    assert len(me.active_events()) == 1


def test_pg_smtp_failure_does_not_block_persistence(pg_client, alert_env, monkeypatch):
    """发信失败：事件仍持久化，失败如实上报（不抛、不丢事件）。"""
    monkeypatch.setenv("CONSOLE_ALERT_SMTP_HOST", "")
    base = _now()
    rule = _disk_rule()

    def broken(subject, body):
        return {"ok": False, "error": "smtp-send-failed"}

    _write("nas", [_sample(ms.METRIC_DISK_USED_PCT, _at(base, 0), 90.0),
                   _sample(ms.METRIC_DISK_USED_PCT, _at(base, 5), 91.0),
                   _sample(ms.METRIC_DISK_USED_PCT, _at(base, 10), 92.0)])
    out = ma.evaluate_rule(rule, now=_at(base, 11), send=broken)
    assert out["action"] == "opened" and out["delivery"]["ok"] is False
    assert len(me.active_events()) == 1


# ---- PG 层：DMS 事件与邮件链 --------------------------------------------------


def test_pg_dms_stale_form_opens_event_and_sends(pg_client, alert_env):
    recorder = alert_env
    # 形态一：写入刷新 last_seen（心跳新鲜），但 sampled_at 停在 13 分钟前。
    _write("nas", [_sample(ms.METRIC_CPU_USED_PCT, _utc_now() - datetime.timedelta(minutes=13), 10.0)])
    out = ma.check_dead_mans_switch(now=_utc_now(), send=recorder)
    nas = next(item for item in out["results"] if item["machine"] == "nas")
    assert nas["form"] == "stale" and nas["action"] == "opened"
    assert nas["notified"] is True
    assert "采集中断" in recorder.sent[0][0]
    assert me.active_events()[0]["fingerprint"] == me.fingerprint(
        me.KIND_MACHINE, "monitoring:nas")
    # 再检查：幂等（updated），不重复发信。
    out = ma.check_dead_mans_switch(now=_utc_now(), send=recorder)
    nas = next(item for item in out["results"] if item["machine"] == "nas")
    assert nas["action"] == "updated" and nas["notified"] is False
    assert len(recorder.sent) == 1


def test_pg_dms_delivery_failure_is_counted_without_real_mail(pg_client, alert_env):
    _write("nas", [_sample(ms.METRIC_CPU_USED_PCT,
                           _utc_now() - datetime.timedelta(minutes=13), 10.0)])

    def failed_delivery(subject, body):
        return {"ok": False, "error": "synthetic-delivery-failure"}

    out = ma.check_dead_mans_switch(now=_utc_now(), send=failed_delivery)
    assert out["opened"] == 1
    assert out["notified"] == 1  # 既有字段是尝试数，不能据此推定实收成功。
    assert out["delivery_failures"] == 1
    assert len(me.active_events()) == 1


def test_pg_dms_recovery_resolves_and_notifies(pg_client, alert_env):
    recorder = alert_env
    _write("nas", [_sample(ms.METRIC_CPU_USED_PCT, _utc_now() - datetime.timedelta(minutes=13), 10.0)])
    ma.check_dead_mans_switch(now=_utc_now(), send=recorder)
    # 新样本到来 → ok → resolved + 恢复信。
    _write("nas", [_sample(ms.METRIC_CPU_USED_PCT, _utc_now(), 10.0)])
    out = ma.check_dead_mans_switch(now=_utc_now(), send=recorder)
    nas = next(item for item in out["results"] if item["machine"] == "nas")
    assert nas["action"] == "resolved" and nas["notified"] is True
    assert "恢复" in recorder.sent[-1][0]


def test_pg_dms_disconnected_when_no_write_at_all(pg_client, alert_env):
    """形态二：完全断联（无成功写入 → last_seen 停更）。

    insert_samples 会把 last_seen 刷成 now，无法用真实写入构造「心跳也停了」；
    直接把 last_seen_at 拨回 13 分钟前（等价于当时写入后一直没再来）。
    """
    recorder = alert_env
    _write("kr", [_sample(ms.METRIC_CPU_USED_PCT, _utc_now() - datetime.timedelta(minutes=13), 10.0)])
    with sd.write_tx() as tx:
        tx.execute(
            "UPDATE monitoring_machines SET last_seen_at = %s WHERE machine_id = %s",
            (_utc_now() - datetime.timedelta(minutes=13), "kr"),
        )
    out = ma.check_dead_mans_switch(now=_utc_now(), send=recorder)
    kr = next(item for item in out["results"] if item["machine"] == "kr")
    assert kr["form"] == "disconnected" and kr["action"] == "opened"


def test_pg_dms_never_machine_no_event(pg_client):
    """未 enrol 机器不进检查；enrol 但从未写入 → never 不开事件。"""
    mi.enrol_machine("us", "US 主机")
    out = ma.check_dead_mans_switch(now=_now(), send=_Recorder())
    us = next(item for item in out["results"] if item["machine"] == "us")
    assert us["form"] == "never" and us["action"] is None
    assert me.active_events() == []
