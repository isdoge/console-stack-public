# -*- coding: utf-8 -*-
"""监控维护的调度入口（B6-3）：聚合/保留、评估器、dead man's switch。

**为什么是独立 CLI 而不是 app.py 后台线程**：NAS 的 cron + ``docker exec``
是本项目已验证的调度形态（version_sync 同款）；后台线程会把「调度所有权」
埋进应用进程，应用重启/多实例时既不可观察也不可控。宿主 cron 每轮拉起本
脚本，退出码即健康信号，日志落宿主文件。

三种模式（cron 各自独立拉起，长聚合绝不阻塞 1min 的告警评估）：

====================== ========================================================
模式                    任务序（固定，不按参数乱序）
====================== ========================================================
``retention``           ``aggregate_hourly`` → ``cleanup_raw`` →
                       ``cleanup_hourly``。先聚合后清理：未聚合进 hourly 的
                       raw 不允许被清走（任务书：避免清掉未聚合样本）——
                       ``aggregate_hourly`` 失败时跳过依赖它的 ``cleanup_raw``
                       （状态记 ``skipped`` 原因）；``cleanup_hourly`` 只清
                       hourly 表自身、不依赖本轮 raw 聚合，仍执行。建议
                       5min 一轮（hourly 只算已完结桶、ON CONFLICT 全量重算，
                       天然幂等）。
``evaluate``           ``run_evaluation``（评估器，建议 1min 一轮）。
``dms``                ``check_dead_mans_switch``（建议 5min 一轮）。
====================== ========================================================

**纪律**：

- **authority 门禁（PG-02/Q1）**：只在 ``collect_guard.collection_allowed()``
  （authority 且非 read_only）时执行；client / 非 PG / read_only 一律 fail
  closed——打印一行说明并以状态码 0 退出（「没轮到我」不是故障），但**绝不
  写库、绝不发信**。Mac 误装 cron 不会变成第二个采集者。
- **单实例 flock**：同一模式同一时刻只允许一个进程在跑（防 cron 重入/手工
  并行）；抢不到锁直接以非零码退出，不排队等待。
- **影子模式（B5）**：``run_evaluation`` / ``check_dead_mans_switch`` 默认
  ``notify=False``——只落事件与判定，**绝不自动发信**；主控确认单发信者后
  用 ``--notify`` 显式打开。
- **原子状态 JSON**：每轮把 started/finished/last_success/逐任务错误/结果
  计数原子写（临时文件 + ``os.replace``）到 ``CONSOLE_MONITORING_MAINTENANCE_JSON``
  指定路径。容器内 compose **没有挂载 /app/runtime**，写它不会持久化还可能
  不可写——默认值取 ``/tmp/console-monitoring/maintenance.json``（容器内
  可写；重启即丢，但 cron 每轮都重写，丢的只是间隙）；要持久化就把该环境
  变量指到 /app/data 下的挂载路径（由主控在部署时决定，不改 compose 是本
  任务边界）。JSON 只含脱敏字段（计数/错误类别/UTC 时间戳），无秘密。
  **每模式独立快照**：同目录 `maintenance-<mode>.json` 保留各模式最近结果，
  兼容共享 `maintenance.json` 但不得用它替代逐模式验收。每轮终态还输出一行
  脱敏 JSON 到 stdout，宿主现有 cron 重定向持久追加；成功、任务失败、投递
  失败与抢锁失败均留痕。部署前的旧时段没有逐轮记录，不补造历史。
  **这是 cron 实际在跑的证据**——只装 cron 不等于在跑，看各模式的
  ``finished`` 时间戳和宿主 JSON 日志的连续性。
- **部分失败不伪装成功**：任一任务抛异常即记录错误并继续跑完其余任务，
  但进程退出码非零、``last_success`` 不更新；只有全部任务成功才退出 0。
  例外是**依赖失败**：retention 的 ``cleanup_raw`` 依赖本轮聚合成功——
  ``aggregate_hourly`` 失败时执行它可能清走尚未聚合的 raw（违反「未聚合
  进 hourly 的 raw 不允许被清走」），因此该任务跳过（``tasks`` 里记
  ``{"skipped": "..."}``），不执行也不算它自己的错误。
"""
import argparse
import fcntl
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

import collect_guard
import monitoring_alert as ma
import monitoring_ingest as mi
import runtime_mode
import storage_dispatch as sd

STATUS_JSON_ENV = "CONSOLE_MONITORING_MAINTENANCE_JSON"
DEFAULT_STATUS_JSON = "/tmp/console-monitoring/maintenance.json"
# flock 文件与状态 JSON 同目录；容器 /tmp 是每容器独立的，不会跨实例互斥，
# 而 authority 门禁已经保证同一时刻只有一个实例在跑维护。
LOCK_NAME = "maintenance-{mode}.lock"

MODES = ("retention", "evaluate", "dms")


class MaintenanceLockBusy(RuntimeError):
    """同模式已有实例在跑（flock 抢不到）。"""


def _utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def status_json_path(mode=None):
    """兼容共享状态；分模式文件防止一次成功覆盖另一模式的失败。"""
    path = os.environ.get(STATUS_JSON_ENV, DEFAULT_STATUS_JSON)
    if mode is None:
        return path
    stem, suffix = os.path.splitext(path)
    return "{}-{}{}".format(stem, mode, suffix or ".json")


def _publish_status(payload):
    _write_status_atomic(status_json_path(payload["mode"]), payload)
    _write_status_atomic(status_json_path(), payload)


def _emit_report(payload):
    # cron 已将 stdout 追加到宿主分模式日志；只输出计数/错误类型，不带异常正文。
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def _write_status_atomic(path, payload):
    """临时文件 + os.replace 原子写；目录不存在先建。"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory,
        prefix=".maintenance-", suffix=".json", delete=False,
    )
    try:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)


def load_status(path=None):
    """读当前状态 JSON；不存在/损坏返回 None（调用方据 started 重写）。"""
    target = path or status_json_path()
    try:
        with open(target, encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def _active_machines(conn=None):
    """active 已登记机器 id 集合（评估器的机器过滤输入）。"""
    with mi.sd.read_tx(conn=conn) as tx:
        rows = tx.execute(
            "SELECT machine_id FROM monitoring_machines "
            "WHERE status = 'active' ORDER BY machine_id ASC"
        ).fetchall()
    return [row["machine_id"] for row in rows]


def evaluation_machines(conn=None):
    """评估器机器过滤：active 已登记 ∩ build_rules 的机器面。

    ``build_rules`` 是 B0 冻结的 6 机规则矩阵；只登记了其中一部分（或登记了
    规则面之外的机器）时，交集保证不评估「没规则的机器」也不评估「已吊销
    的机器」——与任务书「机器过滤只读 active enrolled 与现有 build_rules
    交集」一致。
    """
    active = set(_active_machines(conn=conn))
    seen = set()
    machines = []
    for rule in ma.build_rules():
        machine = rule["machine"]
        if machine in active and machine not in seen:
            seen.add(machine)
            machines.append(machine)
    return machines


# ---- 任务定义 ---------------------------------------------------------------
# 每个任务是 (名称, 依赖集, 可调用)；可调用返回 dict（结果计数），抛异常即失败。
# retention 的顺序在 TASKS 里固定为 aggregate → cleanup_raw → cleanup_hourly。
# 依赖集：本任务执行的前提任务；前提失败时本任务跳过（cleanup_raw 依赖
# aggregate——聚合失败后按时间清 raw 会清走未聚合样本）。


def _task_aggregate():
    buckets = mi.aggregate_hourly()
    return {"buckets": buckets}


def _task_cleanup_raw():
    deleted = mi.cleanup_raw()
    return {"deleted_raw": deleted}


def _task_cleanup_hourly():
    deleted = mi.cleanup_hourly()
    return {"deleted_hourly": deleted}


def _task_evaluate(notify):
    report = ma.run_evaluation(
        notify=notify, machines=evaluation_machines())
    return {
        "rules_evaluated": len(report["results"]),
        "opened": report["opened"],
        "resolved": report["resolved"],
        "notified": report["notified"],
        "delivery_failures": report["delivery_failures"],
        "deliveries": ma.delivery_summary(report["results"]),
    }


def _task_dms(notify):
    report = ma.check_dead_mans_switch(notify=notify)
    return {
        "machines_checked": len(report["results"]),
        "opened": report["opened"],
        "resolved": report["resolved"],
        "notified": report["notified"],
        "delivery_failures": report["delivery_failures"],
        "deliveries": ma.delivery_summary(report["results"]),
    }


def tasks_for(mode, notify=False):
    """模式的任务序列。evaluate/dms 只有一个任务但保持同一结构。"""
    if mode == "retention":
        return [
            ("aggregate_hourly", frozenset(), _task_aggregate),
            ("cleanup_raw", frozenset({"aggregate_hourly"}), _task_cleanup_raw),
            ("cleanup_hourly", frozenset(), _task_cleanup_hourly),
        ]
    if mode == "evaluate":
        return [("run_evaluation", frozenset(),
                 lambda: _task_evaluate(notify))]
    if mode == "dms":
        return [("check_dead_mans_switch", frozenset(),
                 lambda: _task_dms(notify))]
    raise ValueError(f"未知模式: {mode!r}")


# ---- 执行入口 ---------------------------------------------------------------


class _SingleInstance:
    """同模式 flock：抢不到立刻抛 MaintenanceLockBusy，不排队。"""

    def __init__(self, mode):
        directory = os.path.dirname(status_json_path()) or "."
        self.path = os.path.join(directory, LOCK_NAME.format(mode=mode))
        self.handle = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.handle = open(self.path, "a")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise MaintenanceLockBusy(
                f"另一个 {os.path.basename(self.path)} 实例正在运行"
            )
        return self

    def __exit__(self, *_exc):
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
        return False


def run_mode(mode, notify=False):
    """执行一个模式的全部任务；返回状态 payload（调用方负责退出码）。

    authority 门禁在任务之前：不允许时**不写状态文件**（client 上的
    maintenance.json 会停留在 authority 侧的最后真实状态，不会被 client
    的「跳过」覆盖——那是别人的证据）。
    """
    if mode not in MODES:
        raise ValueError(f"未知模式: {mode!r}")
    if not collect_guard.collection_allowed():
        print(collect_guard.collection_skipped(f"监控维护 {mode}"))
        return None
    if not sd.use_postgres():
        # 非 PG：B4 链路无 SQLite 路径，fail closed——不写库也不写状态文件。
        print(f"[maintenance] skip {mode}: 本实例不是 PostgreSQL 存储")
        return None

    import monitoring_business

    started_at = _utc_now_iso()
    previous = load_status(status_json_path(mode)) or {}
    payload = {
        "schema_version": 1,
        "mode": mode,
        "started": started_at,
        "finished": None,
        "notify": bool(notify),
        "role": runtime_mode.instance_role(),
        "source": monitoring_business.business_source(),
        "tasks": {},
        "errors": {},
        "last_success": previous.get("last_success"),
    }
    _publish_status(payload)

    failed = False
    for name, depends_on, task in tasks_for(mode, notify=notify):
        failed_deps = sorted(depends_on & payload["errors"].keys())
        if failed_deps:
            # 依赖失败：执行可能违反保留纪律（如聚合失败后清走未聚合
            # raw），跳过并写明原因；退出码仍非零（错误已在依赖任务上）。
            payload["tasks"][name] = {
                "skipped": "依赖任务失败: " + ", ".join(failed_deps)}
            print(f"[maintenance] task {name} skipped: "
                  f"依赖任务失败 {failed_deps}", file=sys.stderr)
            continue
        payload["tasks"][name] = None
        try:
            payload["tasks"][name] = task()
            if payload["tasks"][name].get("delivery_failures", 0):
                failed = True
                payload["errors"][name] = "DeliveryFailure"
        except Exception as exc:  # noqa: BLE001 - 逐任务隔离，跑完再报非零
            failed = True
            payload["errors"][name] = type(exc).__name__
            print(f"[maintenance] task {name} failed: "
                  f"{type(exc).__name__}", file=sys.stderr)

    payload["finished"] = _utc_now_iso()
    if not failed:
        payload["last_success"] = payload["finished"]
    _publish_status(payload)
    _emit_report(payload)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="监控维护调度入口（B6-3）：authority 才执行，默认影子不发信。")
    parser.add_argument(
        "mode", choices=MODES,
        help="retention=聚合+保留（aggregate→cleanup_raw→cleanup_hourly，"
             "聚合失败跳过依赖它的 cleanup_raw）；"
             "evaluate=评估器；dms=dead man's switch")
    parser.add_argument(
        "--notify", action="store_true",
        help="显式打开发信（默认影子模式 notify=False，绝不自动发信）")
    args = parser.parse_args(argv)

    if not collect_guard.collection_allowed():
        print(collect_guard.collection_skipped(f"监控维护 {args.mode}"))
        return 0

    try:
        with _SingleInstance(args.mode):
            payload = run_mode(args.mode, notify=args.notify)
    except MaintenanceLockBusy:
        moment = _utc_now_iso()
        _emit_report({"schema_version": 1, "mode": args.mode,
                      "started": moment, "finished": moment,
                      "notify": args.notify, "role": runtime_mode.instance_role(),
                      "tasks": {}, "errors": {"lock": "MaintenanceLockBusy"},
                      "last_success": None})
        return 3
    if payload is None:  # 门禁在 run_mode 内二次判定（防御 TOCTOU 窗口）
        return 0
    return 1 if payload["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
