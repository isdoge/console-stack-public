# -*- coding: utf-8 -*-
"""Console 自有阈值告警评估器（B5-4）。

从 B4 的 ``monitoring_samples`` 读样本，按 B0 冻结的 18 条规则评估，触发时
发邮件（smtp.qq.com:465 SSL）并把告警事件写进 ``monitor_events``（只调用
``monitor_events.observe`` 公共 API，不改那张表）。

====  B0 冻结规则（不发明）  ==================================================

6 台机器 × 3 种规则（`.agents/tasks/2026-10-24-monitoring-refactor-b0/
evidence/b0-1-alerts/rules-table.md` §4 阈值矩阵）：

- Status：离线 ≥ 5 分钟（离线 = 无新样本，样本静默超阈值即视为离线）。
- Disk：``disk_used_pct`` > 85% 持续 10 分钟。
- LoadAvg5：``load_avg_5m`` > 阈值持续 10 分钟；阈值按核数——NAS/TXY/US
  （4 线程）为 4，ALY/KR/SG（2 线程）为 2。

====  告警计时五状态（总计划切换不变量）  =====================================

评估器**无内存状态**：每轮都从「PG 样本时间线 + monitor_events 活动行」
重建，进程重启天然不丢已触发事件、不重复通知（通知去重 = observe 的
transition 语义：活动事件存在时 ``observe(ok=False)`` 回 ``updated``，
只有 ``opened`` 才发信）。

============================  =================================================
派生状态                      判定
============================  =================================================
``timing`` 持续计时中         条件满足但持续时长未到，且无活动事件
``triggered`` 已触发          条件满足且持续 ≥ 阈值时长；活动事件已开（无论
                             本进程还是重启前开的）→ 不重复通知
``recovering`` 恢复中         活动事件在，最新有效观测已不违规，且距其
                             < 恢复窗口（RECOVERY_WINDOW_SEC）→ 事件保持
                             打开，不发恢复通知
``reverted`` 回退             恢复中又重新满足（恢复窗口内出现清零观测后
                             再违规）→ 接续原事件，opened_at 不动，不重发
``unknown`` 观测缺口          最新观测停更超 12 分钟：既不新触发（无法证明
                             条件此刻成立），也不恢复（无恢复证据），
                             事件保持原状；计时从缺口后的有效观测重建
                             （B0-4「标明 unknown 并从有效观测重建」）
``clear`` 正常                无违规、无活动事件
============================  =================================================

恢复窗口取 600s（B5 初值，与持续窗同宽；B6 切换前可按实测调整）。

====  回填不触发通知  =========================================================

所有样本读取排除 ``source = 'beszel-backfill'``（B5-2 的回填写入用该
source 标记）——回填是历史，不是观测。

====  dead man's switch  ======================================================

阈值 **12 分钟**（B0/B1-2 冻结 10+2，不是 17）。``check_dead_mans_switch``
是独立于 ingest 收包路径的定时检查，识别两种形态（B1-2 §2.2）：

- ``stale`` 心跳正常但指标停更：``monitoring_machines.last_seen_at`` 新鲜
  （有成功写入）但最新非回填样本 ``sampled_at`` 距今 > 12 分钟——补传旧
  样本只刷新心跳、不推进采样时间，正是这个形态。
- ``disconnected`` 完全断联：``last_seen_at`` 本身超 12 分钟（连写入都没有）。

事件 fingerprint 与 B5-1（``monitoring_compare``）共用 ``monitoring:<id>``
命名空间：两个检查 whichever 先跑都折叠进同一条活动事件，不会双报；
本模块在 ``opened`` 转移时走邮件链。

**边界**：本检查与评估器同进程，覆盖 Agent/传输停更；**不覆盖 Console/NAS
整体停机**——监控自身全挂时无人发事件，那一层靠外部拨测（B6 范围）。

====  SMTP  ===================================================================

凭据只从环境读（NAS ``.env`` 注入），绝不写死进代码、不落文档/日志/git：

- ``CONSOLE_ALERT_SMTP_HOST``（如 smtp.qq.com）
- ``CONSOLE_ALERT_SMTP_PORT``（默认 465，implicit TLS / SMTP_SSL）
- ``CONSOLE_ALERT_SMTP_USER`` / ``CONSOLE_ALERT_SMTP_PASSWORD``
- ``CONSOLE_ALERT_SMTP_FROM``（缺省 = USER；QQ 强制 MAIL FROM=登录账号）
- ``CONSOLE_ALERT_SMTP_TO``（逗号分隔收件人）

邮件正文平移 Beszel 告警邮件的字段面：机器名、规则、当前值、阈值、持续
时长、Console 链接（``CONSOLE_PUBLIC_URL``，app.py 既有约定）。未配置
SMTP 时事件照常落库、投递结果记 ``ok=False``——持久化不依赖发信可用性。

仅 PG：样本表（B4）没有 SQLite 路径，非 postgres 引擎直接拒绝（同
``monitoring_ingest`` 纪律：宁失败不伪装）。
"""
import datetime
import os
import smtplib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import composition
import monitor_events as me
import storage_dispatch as sd
from monitoring_schema import (
    BACKFILL_SOURCE,
    DISK_THRESHOLD_PCT,
    LOAD_SUSTAIN_MIN,
    METRIC_DISK_USED_PCT,
    METRIC_LOAD_AVG_5M,
    STALENESS_THRESHOLD_MIN,
)

# ---- B0 冻结常量 -------------------------------------------------------------
# 阈值矩阵（rules-table.md §4）：4 线程机器 LoadAvg5 阈值 4，2 线程机器为 2。
# metric 名与阈值常量统一从 monitoring_schema 取（B5-5 单一事实源）；持续
# 时长换算成秒在本地定值，schema 的 *_SUSTAIN_MIN 镜像同源（B0 冻结 10 分钟）。
# 机器表本身是**实例输入**（B7-1）：无组合文件时等于原六机冻结矩阵，
# 公共实例的 `alert_machines = {}` 得到 0 条规则（不是回退六机）。
MACHINE_SPECS = composition.legacy_alert_machine_specs()

RULE_STATUS = "Status"
RULE_DISK = "Disk"
RULE_LOAD5 = "LoadAvg5"

STATUS_OFFLINE_SEC = 300  # B0：离线 ≥ 5 分钟
# 采样节奏 5 分钟 + Agent 侧抖动余量：静默刚好压在 5 分钟边界的抖动不应
# 产生假触发（B6 可按实测调）。
# 2026-10-25 实测调（B6-3 切换轮）：六机采样周期实测 300s、样本到达延迟
# 60–68s、evaluate 每分钟跑。60s 余量下 offline_after=360s，而最坏情形
# 「采样 300s + 延迟 68s + cron 相位 1s」= 369s——aly（采样相位 xx:01:00
# 落库 xx:02:02）恰好每 5 分钟踩中边界一开一关（monitor_events 114 条锯齿，
# 2026-10-24T15:22Z 起）。余量改为整周期 300s：offline_after=600s，真离线
# 的判定延迟从「5 分钟」变为「最多 10 分钟」，与 CONTINUITY_GAP_SEC（12min）
# 的缺口判定仍留 2 分钟间隔。
SAMPLE_JITTER_GRACE_SEC = 300
SUSTAIN_SEC = LOAD_SUSTAIN_MIN * 60  # B0：Disk / LoadAvg5 持续 10 分钟
RECOVERY_WINDOW_SEC = 600  # B5 初值：恢复中 → 恢复的判定窗
# 观测连续性边界 = B1-2 冻结的 12 分钟新鲜度阈值：相邻有效观测间隔超过它
# 视为缺口（unknown），计时从缺口后的有效观测重建。
CONTINUITY_GAP_SEC = STALENESS_THRESHOLD_MIN * 60
DEAD_MAN_THRESHOLD_SEC = STALENESS_THRESHOLD_MIN * 60  # 10 + 2，不是 17
# 回看窗：持续窗 + 恢复窗 + 余量，足够重建 run 与恢复时钟。
LOOKBACK_SEC = 2400

SOURCE_ALERT = "monitoring-alert"

METRIC_FOR_RULE = {
    RULE_DISK: METRIC_DISK_USED_PCT,
    RULE_LOAD5: METRIC_LOAD_AVG_5M,
}
RULE_LABELS = {
    RULE_STATUS: "离线",
    RULE_DISK: "磁盘使用率",
    RULE_LOAD5: "5 分钟负载",
}

SMTP_ENV_PREFIX = "CONSOLE_ALERT_SMTP"
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class MonitoringAlertError(RuntimeError):
    """monitoring_alert 的基错误。"""


def _require_pg():
    if not sd.use_postgres():
        raise MonitoringAlertError(
            "monitoring_alert 仅支持 PostgreSQL（B4 样本链路无 SQLite 路径）"
        )


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(moment):
    return moment.strftime(TS_FORMAT)


def machine_specs():
    """生效的规则机器表：组合 `alert_machines`，现读（测试可 monkeypatch）。"""
    return composition.alert_machine_specs()


def build_rules():
    """每台规则机器 × Status/Disk/LoadAvg5；私人现役 6 机得 18 条（B0 冻结）。"""
    rules = []
    for machine, spec in sorted(machine_specs().items()):
        rules.append({
            "machine": machine,
            "display": spec["display"],
            "name": RULE_STATUS,
            "label": RULE_LABELS[RULE_STATUS],
            "threshold": None,
            "duration_sec": STATUS_OFFLINE_SEC,
            "metric": None,
        })
        rules.append({
            "machine": machine,
            "display": spec["display"],
            "name": RULE_DISK,
            "label": RULE_LABELS[RULE_DISK],
            "threshold": DISK_THRESHOLD_PCT,
            "duration_sec": SUSTAIN_SEC,
            "metric": METRIC_FOR_RULE[RULE_DISK],
        })
        rules.append({
            "machine": machine,
            "display": spec["display"],
            "name": RULE_LOAD5,
            "label": RULE_LABELS[RULE_LOAD5],
            "threshold": spec["load5_threshold"],
            "duration_sec": SUSTAIN_SEC,
            "metric": METRIC_FOR_RULE[RULE_LOAD5],
        })
    return rules


def rule_fingerprint(machine, rule_name):
    """告警事件幂等键，命名空间 ``machine:alert:<rule>:<machine>``。

    与机器探活（``machine:<id>``）、B5-1 采集中断（``machine:monitoring:<id>``）
    物理隔离——探活恢复不会误关阈值告警，反之亦然。
    """
    return me.fingerprint(me.KIND_MACHINE, alert_target(machine, rule_name))


def alert_target(machine, rule_name):
    return "alert:{}:{}".format(rule_name, machine)


# ---- 样本读取 ---------------------------------------------------------------


def _load_metric_samples(machine, metric, since, conn=None):
    """窗口内单指标样本（升序），排除回填。返回 ``[(datetime, value)]``。"""
    with sd.read_tx(conn=conn) as tx:
        rows = tx.execute(
            "SELECT sampled_at, metric_value FROM monitoring_samples "
            "WHERE machine_id = %s AND metric_name = %s AND sampled_at >= %s "
            "AND source <> %s ORDER BY sampled_at ASC",
            (machine, metric, since, BACKFILL_SOURCE),
        ).fetchall()
    return [(row["sampled_at"], float(row["metric_value"])) for row in rows]


def _load_sample_times(machine, since, conn=None):
    """窗口内该机全部非回填样本时间（升序、任意指标）。"""
    with sd.read_tx(conn=conn) as tx:
        rows = tx.execute(
            "SELECT DISTINCT sampled_at FROM monitoring_samples "
            "WHERE machine_id = %s AND sampled_at >= %s AND source <> %s "
            "ORDER BY sampled_at ASC",
            (machine, since, BACKFILL_SOURCE),
        ).fetchall()
    return [row["sampled_at"] for row in rows]


def _active_event_exists(fp, conn=None):
    with sd.read_tx(conn=conn) as tx:
        row = tx.execute(
            "SELECT id FROM monitor_events WHERE fingerprint = %s AND resolved_at IS NULL",
            (fp,),
        ).fetchone()
    return row is not None


# ---- 规则评估 ---------------------------------------------------------------


def _trailing_run(samples, threshold):
    """尾部连续违规 run（升序返回）。相邻违规观测间隔超连续性边界即断——
    缺口前的观测不参与本次计时（unknown 从有效观测重建）。"""
    run = []
    for ts, value in reversed(samples):
        if value <= threshold:
            break
        if run and (run[-1][0] - ts).total_seconds() > CONTINUITY_GAP_SEC:
            break
        run.append((ts, value))
    run.reverse()
    return run


def _trailing_clear_start(samples, threshold):
    """尾部连续清零 run 的起点（升序扫描，违规观测或超连续性边界的缺口都断）。

    恢复时长从这里算——**不是**从最新观测算：生产中样本持续到达，最新观测
    永远新鲜，从它算恢复窗会永远走不完，已触发事件永不 resolve。
    """
    start = None
    prev = None
    for ts, value in samples:
        if value <= threshold:
            if start is None or (prev is not None and (ts - prev).total_seconds() > CONTINUITY_GAP_SEC):
                start = ts
            prev = ts
        else:
            start = None
            prev = None
    return start


def assess_threshold_rule(rule, samples, now):
    """Disk / LoadAvg5 的单轮判定（纯函数，样本已排除回填）。

    返回 dict：``violating``（条件此刻成立）、``sustained_sec``、
    ``current_value``、``latest_ts``、``fresh``（最新观测在 12 分钟内）、
    ``clear_in_window``（恢复窗口内出现过清零观测——回退判定输入）、
    ``cleared_sec``（连续清零时长，恢复判定输入）。
    """
    threshold = rule["threshold"]
    latest_ts = samples[-1][0] if samples else None
    fresh = latest_ts is not None and (now - latest_ts).total_seconds() <= CONTINUITY_GAP_SEC
    run = _trailing_run(samples, threshold)
    violating = bool(run) and fresh
    sustained_sec = int((run[-1][0] - run[0][0]).total_seconds()) if run else 0

    clear_in_window = False
    if run:
        window_start = run[0][0] - datetime.timedelta(seconds=RECOVERY_WINDOW_SEC)
        clear_in_window = any(
            ts >= window_start and value <= threshold for ts, value in samples
        )
    clear_start = _trailing_clear_start(samples, threshold) if not violating else None
    cleared_sec = (
        max(0, int((now - clear_start).total_seconds())) if clear_start else 0
    )
    return {
        "violating": violating,
        "sustained_sec": sustained_sec if violating else 0,
        "current_value": run[-1][1] if run else (samples[-1][1] if samples else None),
        "latest_ts": latest_ts,
        "fresh": fresh,
        "clear_in_window": clear_in_window,
        "cleared_sec": cleared_sec,
    }


def assess_status_rule(rule, sample_ts_list, now):
    """Status 规则的单轮判定：离线 = 非回填样本静默超 5 分钟（含抖动余量）。

    ``sample_ts_list`` 是窗口内该机全部非回填样本时间（升序、任意指标）。
    清零时长从尾部「在线链」起点算：相邻样本间隔超离线阈值即断链——
    打破静默的那个样本就是本轮在线的起点。
    """
    offline_after = STATUS_OFFLINE_SEC + SAMPLE_JITTER_GRACE_SEC
    latest_ts = sample_ts_list[-1] if sample_ts_list else None
    silence = (now - latest_ts).total_seconds() if latest_ts is not None else None
    violating = silence is not None and silence > offline_after

    chain_start = None
    prev = None
    for ts in sample_ts_list:
        if prev is None or (ts - prev).total_seconds() > offline_after:
            chain_start = ts
        prev = ts
    cleared_sec = (
        max(0, int((now - chain_start).total_seconds()))
        if chain_start is not None and not violating else 0
    )
    return {
        "violating": violating,
        "sustained_sec": int(silence) if silence is not None else None,
        "current_value": int(silence) if silence is not None else None,
        "latest_ts": latest_ts,
        "fresh": silence is not None and silence <= CONTINUITY_GAP_SEC,
        "silence_sec": int(silence) if silence is not None else None,
        "cleared_sec": cleared_sec,
    }


# ---- SMTP -------------------------------------------------------------------


def smtp_config():
    """读 SMTP 配置；host/user/password 任缺即视为未配置（返回 None）。"""
    prefix = SMTP_ENV_PREFIX
    host = os.environ.get("{}_HOST".format(prefix), "").strip()
    user = os.environ.get("{}_USER".format(prefix), "").strip()
    password = os.environ.get("{}_PASSWORD".format(prefix), "")
    if not (host and user and password):
        return None
    try:
        port = int(os.environ.get("{}_PORT".format(prefix), "465"))
    except ValueError:
        port = 465
    recipients = [
        item.strip()
        for item in os.environ.get("{}_TO".format(prefix), "").split(",")
        if item.strip()
    ]
    sender = os.environ.get("{}_FROM".format(prefix), "").strip() or user
    return {
        "host": host,
        "port": port,
        "user": user,
        # password 只进 SMTP 会话，不进任何返回给调用方的结构（防日志泄漏）。
        "password": password,
        "sender": sender,
        "recipients": recipients,
    }


def _format_value(rule_name, value):
    if rule_name == RULE_DISK:
        return "{:.1f}%".format(value)
    if rule_name == RULE_LOAD5:
        return "{:.2f}".format(value)
    return "{} 秒".format(int(value)) if value is not None else "未知"


def _format_threshold(rule):
    if rule["name"] == RULE_DISK:
        return "{:.0f}%".format(rule["threshold"])
    if rule["name"] == RULE_LOAD5:
        return "{:.0f}".format(rule["threshold"])
    return "{} 分钟".format(rule["duration_sec"] // 60)


def _fmt_duration(sec):
    if sec is None:
        return "未知"
    minutes = int(sec) // 60
    return "{} 分钟".format(minutes) if minutes >= 1 else "{} 秒".format(int(sec))


def render_alert_email(rule, assessment, opened_at, recovered=False):
    """告警/恢复邮件的 (subject, body)。字段面平移 Beszel：机器、规则、
    当前值、阈值、持续时长、Console 链接。"""
    url = os.environ.get("CONSOLE_PUBLIC_URL", "").rstrip("/")
    display = rule["display"]
    label = rule["label"]
    if recovered:
        subject = "[Console] {} {}告警已恢复".format(display, label)
    else:
        subject = "[Console] {} {} {} 超过阈值 {}".format(
            display, label,
            _format_value(rule["name"], assessment["current_value"]),
            _format_threshold(rule),
        )
    lines = [
        "机器：{}（{}）".format(display, rule["machine"]),
        "规则：{}".format(rule["name"]),
        "当前值：{}".format(_format_value(rule["name"], assessment["current_value"])),
        "阈值：{}".format(_format_threshold(rule)),
        "持续时长：{}".format(_fmt_duration(assessment.get("sustained_sec"))),
        "触发时间：{}".format(opened_at),
    ]
    if url:
        lines.append("控制台：{}".format(url))
    lines.append("")
    lines.append("—— Console 自有告警链（B5）")
    return subject, "\n".join(lines)


def _smtp_error_category(exc):
    """固定类别，不保留含邮箱/口令的SMTP响应正文或异常文本。"""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "authentication"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "recipients_refused"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "sender_refused"
    if isinstance(exc, smtplib.SMTPDataError):
        return "data_rejected"
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return "disconnected"
    if isinstance(exc, smtplib.SMTPException):
        return "smtp_error"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, OSError):
        return "network"
    return "smtp_error"


def smtp_send(subject, body):
    """SMTP接受正文才算提交成功；QUIT失败不否认此前接受，也不重发。

    连接/认证阶段瞬时失败最多重试一次；进入sendmail后可能已送达，失败不重试。
    返回值只含固定类别与阶段，不包含SMTP响应正文、凭据或收件地址。
    """
    config = smtp_config()
    if config is None:
        return {"ok": False, "error": "smtp-not-configured"}
    if not config["recipients"]:
        return {"ok": False, "error": "smtp-no-recipients"}
    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = Header(subject, "utf-8")
    message["From"] = formataddr((str(Header("Console", "utf-8")), config["sender"]))
    message["To"] = ", ".join(config["recipients"])
    pre_submit_failures = []
    for attempt in (1, 2):
        client = None
        stage = "connect"
        retry = False
        try:
            client = smtplib.SMTP_SSL(config["host"], config["port"], timeout=30)
            stage = "authenticate"
            client.login(config["user"], config["password"])
            stage = "submit"
            refused = client.sendmail(config["sender"], config["recipients"], message.as_string())
            if refused:
                # sendmail返回字典表示部分收件人拒收，不能当全量成功或再次群发。
                code = next(iter(refused.values()))[0]
                result = {"ok": False, "error": "smtp-send-failed", "stage": stage,
                          "category": "recipients_refused", "smtp_code": code,
                          "accepted_some": True, "attempts": attempt}
            else:
                result = {"ok": True, "error": None, "stage": "accepted", "attempts": attempt}
        except Exception as exc:  # noqa: BLE001 - 事件持久化不依赖投递可用性
            category = _smtp_error_category(exc)
            code = getattr(exc, "smtp_code", None)
            result = {"ok": False, "error": "smtp-send-failed", "stage": stage,
                      "category": category, "smtp_code": code, "attempts": attempt,
                      "delivery_unknown": stage == "submit" and category not in {
                          "recipients_refused", "sender_refused", "data_rejected"}}
            retry = (attempt == 1 and stage != "submit"
                     and (category in {"disconnected", "timeout", "network"}
                          or isinstance(code, int) and 400 <= code < 500))
            if retry:
                pre_submit_failures.append({"stage": stage, "category": category, "smtp_code": code})
        finally:
            if client is not None:
                try:
                    code, _ = client.quit()
                    if code != 221:
                        result["cleanup_warning"] = "unexpected_quit_response"
                except Exception as exc:  # noqa: BLE001 - 清理失败不覆盖正文提交结果
                    result["cleanup_warning"] = _smtp_error_category(exc)
                finally:
                    try:
                        client.close()
                    except OSError:
                        result.setdefault("cleanup_warning", "network")
        if not retry:
            if pre_submit_failures:
                result["pre_submit_failures"] = pre_submit_failures
            return result
    return result


def delivery_summary(results):
    """维护日志只投影白名单字段，既留失败阶段，也记录服务器接受与清理告警。"""
    summaries = []
    fields = ("ok", "stage", "category", "smtp_code", "attempts", "delivery_unknown",
              "accepted_some", "cleanup_warning", "pre_submit_failures")
    for item in results:
        delivery = item.get("delivery")
        if delivery is None:
            continue
        summary = {key: item[key] for key in ("machine", "rule", "action") if key in item}
        summary.update({key: delivery[key] for key in fields if key in delivery})
        summaries.append(summary)
    return summaries


# ---- 评估入口 ---------------------------------------------------------------


def evaluate_rule(rule, now=None, send=None, notify=True, conn=None):
    """评估单条规则一轮；返回本轮判定与动作。

    ``send(subject, body) -> {"ok", "error"}`` 可注入假投递器（测试）；
    缺省用 :func:`smtp_send`。``notify=False`` 是影子模式开关（B5 影子
    结果不发送通知——只落事件与判定，不发信）。``opened`` 才发告警信、
    ``resolved`` 才发恢复信：这是通知去重的唯一机制，进程重启后天然成立。
    """
    _require_pg()
    moment = now or _utc_now()
    sender = send or smtp_send
    fp = rule_fingerprint(rule["machine"], rule["name"])
    since = moment - datetime.timedelta(seconds=LOOKBACK_SEC)

    if rule["name"] == RULE_STATUS:
        assessment = assess_status_rule(
            rule, _load_sample_times(rule["machine"], since, conn=conn), moment)
    else:
        samples = _load_metric_samples(rule["machine"], rule["metric"], since, conn=conn)
        assessment = assess_threshold_rule(rule, samples, moment)

    active = _active_event_exists(fp, conn=conn)
    result = {
        "machine": rule["machine"],
        "rule": rule["name"],
        "state": "clear",
        "action": None,
        "notified": False,
        "delivery": None,
        "assessment": assessment,
    }

    if assessment["violating"]:
        if rule["name"] == RULE_STATUS:
            sustained = assessment["sustained_sec"] or 0
        else:
            sustained = assessment["sustained_sec"]
        if active:
            # 已触发（事件在，无论本进程还是重启前开的）或回退（恢复窗口内
            # 清零后又违规）——都接续原事件，不重复通知。
            result["state"] = (
                "reverted" if assessment.get("clear_in_window") else "triggered"
            )
            result["action"] = me.observe(
                me.KIND_MACHINE, alert_target(rule["machine"], rule["name"]),
                False, source=SOURCE_ALERT, machine=rule["machine"],
                title=_alert_title(rule, assessment), detail=_alert_detail(rule, assessment),
                severity=_severity(rule), ts=_iso(moment), conn=conn,
            )
            return result
        if sustained >= rule["duration_sec"]:
            transition = me.observe(
                me.KIND_MACHINE, alert_target(rule["machine"], rule["name"]),
                False, source=SOURCE_ALERT, machine=rule["machine"],
                title=_alert_title(rule, assessment), detail=_alert_detail(rule, assessment),
                severity=_severity(rule), ts=_iso(moment), conn=conn,
            )
            result["state"] = "triggered"
            result["action"] = transition
            if transition == "opened" and notify:
                result["notified"] = True
                result["delivery"] = sender(*render_alert_email(rule, assessment, _iso(moment)))
            return result
        result["state"] = "timing"
        return result

    # 条件此刻不成立
    if not assessment["fresh"]:
        # 观测缺口：既不新触发也不恢复，活动事件保持原状（unknown）。
        if active:
            result["state"] = "unknown"
        return result
    if active:
        cleared = assessment.get("cleared_sec") or 0
        if cleared < RECOVERY_WINDOW_SEC:
            result["state"] = "recovering"
            return result
        transition = me.observe(
            me.KIND_MACHINE, alert_target(rule["machine"], rule["name"]),
            True, source=SOURCE_ALERT, machine=rule["machine"], ts=_iso(moment), conn=conn,
        )
        result["state"] = "clear"
        result["action"] = transition
        if transition == "resolved" and notify:
            result["notified"] = True
            result["delivery"] = sender(*render_alert_email(rule, assessment, _iso(moment), recovered=True))
        return result
    return result


def _severity(rule):
    # 机器离线是 error；磁盘/负载超阈与采集中断（B5-1 同判例）是 warn。
    return "error" if rule["name"] == RULE_STATUS else "warn"


def _alert_title(rule, assessment):
    return "{} {} {}".format(
        rule["display"], rule["label"],
        _format_value(rule["name"], assessment["current_value"]),
    )


def _alert_detail(rule, assessment):
    if rule["name"] == RULE_STATUS:
        return "样本静默 {}，超过离线阈值 {} 分钟".format(
            _fmt_duration(assessment.get("silence_sec")),
            STATUS_OFFLINE_SEC // 60,
        )
    return "当前 {}，阈值 {}，已持续 {}".format(
        _format_value(rule["name"], assessment["current_value"]),
        _format_threshold(rule),
        _fmt_duration(assessment.get("sustained_sec")),
    )


def run_evaluation(now=None, send=None, notify=True, machines=None, conn=None):
    """评估全部（或指定机器的）规则一轮。返回 ``{"results": [...],
    "opened": n, "resolved": n, "notified": n, "delivery_failures": n}``。

    B5 影子模式：``notify=False`` 只比对不发送（B5 README §1「影子结果
    不发送通知」）；正式发信权移交在 B6 切换时打开。
    """
    _require_pg()
    moment = now or _utc_now()
    rules = build_rules()
    if machines:
        wanted = {str(item).strip().lower() for item in machines}
        rules = [rule for rule in rules if rule["machine"] in wanted]
    results = []
    opened = resolved = notified = failures = 0
    for rule in rules:
        outcome = evaluate_rule(rule, now=moment, send=send, notify=notify, conn=conn)
        results.append(outcome)
        if outcome["action"] == "opened":
            opened += 1
        if outcome["action"] == "resolved":
            resolved += 1
        if outcome["notified"]:
            notified += 1
        delivery = outcome.get("delivery")
        if delivery and not delivery.get("ok"):
            failures += 1
    return {
        "generated_at": _iso(moment),
        "results": results,
        "opened": opened,
        "resolved": resolved,
        "notified": notified,
        "delivery_failures": failures,
    }


# ---- dead man's switch ------------------------------------------------------


def assess_dead_mans_switch(machine, last_seen_at, latest_sample, now,
                            threshold_sec=DEAD_MAN_THRESHOLD_SEC):
    """单机两形态判定（B1-2 §2.2）。返回 ``{"form": "ok"|"stale"|"disconnected",
    ...}``；从未上报（无心跳无样本）是能力问题不是中断，回 ``never``。"""
    if last_seen_at is None and latest_sample is None:
        return {"form": "never"}
    heartbeat_age = (
        (now - last_seen_at).total_seconds() if last_seen_at is not None else None
    )
    if heartbeat_age is None or heartbeat_age > threshold_sec:
        return {
            "form": "disconnected",
            "heartbeat_age_sec": int(heartbeat_age) if heartbeat_age is not None else None,
            "last_sampled_at": _iso(latest_sample) if latest_sample else None,
            "threshold_sec": threshold_sec,
        }
    if latest_sample is None:
        # 有心跳但从未有非回填样本：写入的全是回填——按 stale 报，采样链没活过。
        return {
            "form": "stale",
            "heartbeat_age_sec": int(heartbeat_age),
            "last_sampled_at": None,
            "silence_sec": None,
            "threshold_sec": threshold_sec,
        }
    silence = (now - latest_sample).total_seconds()
    if silence > threshold_sec:
        return {
            "form": "stale",
            "heartbeat_age_sec": int(heartbeat_age),
            "last_sampled_at": _iso(latest_sample),
            "silence_sec": int(silence),
            "threshold_sec": threshold_sec,
        }
    return {"form": "ok", "heartbeat_age_sec": int(heartbeat_age)}


def render_dms_email(display, machine, violation, recovered=False):
    form_label = "采集中断（心跳正常但指标停更）" if violation["form"] == "stale" else "完全断联"
    url = os.environ.get("CONSOLE_PUBLIC_URL", "").rstrip("/")
    if recovered:
        subject = "[Console] {} 监控{}已恢复".format(display, form_label)
    else:
        subject = "[Console] {} 监控{}".format(display, form_label)
    lines = [
        "机器：{}（{}）".format(display, machine),
        "形态：{}".format(form_label),
        "最后样本：{}".format(violation.get("last_sampled_at") or "无"),
        "静默时长：{}".format(
            _fmt_duration(violation.get("silence_sec"))
            if violation.get("silence_sec") is not None else "未知"
        ),
        "判定阈值：{} 分钟".format(DEAD_MAN_THRESHOLD_SEC // 60),
    ]
    if url:
        lines.append("控制台：{}".format(url))
    lines.append("")
    lines.append("—— Console 自有告警链（B5 dead man's switch）")
    return subject, "\n".join(lines)


def check_dead_mans_switch(now=None, send=None, notify=True, conn=None):
    """独立于收包路径的定时检查（B5 必做）：逐台判定两形态并落事件/发信。

    fingerprint 与 B5-1 共用 ``machine:monitoring:<id>``：两个检查折叠进
    同一条活动事件（observe 幂等），本模块在 ``opened`` 转移发信、
    ``resolved`` 转移发恢复信。返回逐机结果摘要。
    """
    _require_pg()
    moment = now or _utc_now()
    sender = send or smtp_send
    with sd.read_tx(conn=conn) as tx:
        rows = tx.execute(
            "SELECT machine_id, display_name, last_seen_at FROM monitoring_machines "
            "WHERE status = 'active' ORDER BY machine_id ASC"
        ).fetchall()
    machines = [
        (row["machine_id"], row["display_name"] or row["machine_id"], row["last_seen_at"])
        for row in rows
    ]

    results = []
    for machine, display, last_seen in machines:
        times = _load_sample_times(
            machine, moment - datetime.timedelta(seconds=LOOKBACK_SEC), conn=conn)
        latest = times[-1] if times else None
        violation = assess_dead_mans_switch(machine, last_seen, latest, moment)
        entry = {"machine": machine, "form": violation["form"], "action": None,
                 "notified": False, "delivery": None}
        if violation["form"] == "ok":
            action = me.observe(
                me.KIND_MACHINE, "monitoring:{}".format(machine), True,
                source=SOURCE_ALERT, machine=machine, ts=_iso(moment), conn=conn,
            )
            entry["action"] = action
            if action == "resolved" and notify:
                entry["notified"] = True
                entry["delivery"] = sender(*render_dms_email(
                    display, machine, {"form": "disconnected"}, recovered=True))
            results.append(entry)
            continue
        if violation["form"] == "never":
            results.append(entry)
            continue
        detail = "最后样本 {}，静默 {}".format(
            violation.get("last_sampled_at") or "无",
            _fmt_duration(violation.get("silence_sec"))
            if violation.get("silence_sec") is not None else "未知",
        )
        entry["action"] = me.observe(
            me.KIND_MACHINE, "monitoring:{}".format(machine), False,
            source=SOURCE_ALERT, machine=machine,
            title="{} 监控{}".format(
                display, "采集中断" if violation["form"] == "stale" else "断联"),
            detail=detail, severity="warn", ts=_iso(moment), conn=conn,
        )
        if entry["action"] == "opened" and notify:
            entry["notified"] = True
            entry["delivery"] = sender(*render_dms_email(display, machine, violation))
        results.append(entry)
    return {
        "generated_at": _iso(moment),
        "results": results,
        "opened": sum(1 for item in results if item["action"] == "opened"),
        "resolved": sum(1 for item in results if item["action"] == "resolved"),
        "notified": sum(1 for item in results if item["notified"]),
        "delivery_failures": sum(
            1 for item in results
            if item.get("delivery") and not item["delivery"].get("ok")
        ),
    }
