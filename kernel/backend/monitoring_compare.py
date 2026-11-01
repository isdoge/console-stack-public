# -*- coding: utf-8 -*-
"""双轨比对（B5-1）：同机同时读 Beszel 与 Console Agent 两轨，对齐语义后容差比对。

影子路径纪律（B5 README §1）：Beszel 仍是正式数据源，本模块只比对不切换，
结果不进 /api/monitor 等正式入口（B1 契约 §2.3 规则 2）。

====  双轨输入  ============================================================

Beszel 轨   ``collect_beszel_track(snapshot)``——纯投影，输入是
            ``beszel_client.get_monitor_snapshot()`` 的 payload（测试可注入
            假快照，不触网）。不修改 Beszel 调用本身。
Agent 轨    ``read_agent_track(...)``——只读 SELECT B4 的 ``monitoring_samples``
            （不改存储层），窗口内每 ``(machine, metric)`` 取最新一条。

====  Agent 轨 metric 命名约定  ============================================

命名单一事实源在 ``monitoring_schema``（B5-5 收口对齐），本模块按其读取：

- 主机指标：payload json tag 直写（``cpu_used_pct``、``mem_total_bytes``、
  ``disk_used_pct``、…）。
- 网卡计数器：``nic_metric(METRIC_NET_SENT_TOTAL_BYTES, nic)`` 即
  ``net_sent_total_bytes@<nic>`` / ``net_recv_total_bytes@<nic>``。
- 容器指标：``container_metric(name, field)`` 即
  ``container_<name>_cpu_pct`` / ``container_<name>_memory_used_bytes``。

====  指标对齐（单位 / 口径 / 时间窗）  =====================================

对齐注册表 ``METRIC_ALIGNMENT`` 是五类指标的单一事实源（cpu/内存/磁盘/容器/
网络）。要点：

- 内存百分比按 **available 口径**对齐：beszel ``memory_pct``（SSH 覆盖后）对
  agent ``(mem_total − mem_available) / mem_total`` 派生值，不比 ``mp`` 原口径。
- 绝对值单位投影到**同侧口径**：agent bytes → MiB（/2²⁰）对 beszel
  ``memory_*_mb``；bytes → GiB（/2³⁰）对 beszel ``disk_*_gb``（Beszel 的
  m/d 系列字段名是 GB、值是 GiB 浮点，B0 §6-1；按 2³⁰ 投影，B1 契约 §4.5
  规则 2「先投影到同一口径再比」）。
- 容器计数走**实查口径**（修正 B）：beszel 侧数 ``snapshot["containers"]``
  行数，不用失真的 ``info.ct``；agent 侧数 distinct 容器名。
- 容器字段 schema 固定为 ``CONTAINER_FIELDS``（cpu / memory_mb）：缺字段、
  缺容器一律显式计 missing 进 summary，不静默跳过（R1）。
- 网络计数器：两侧各用 ``node_traffic`` 的排除表挑计费网卡（台账
  ``traffic_nic`` 显式优先，否则取累计量最大的可计费网卡），挑中不同网卡
  如实报告 ``nic_mismatch``。
- 时间窗：beszel ``info.*`` 是瞬时值且无时间戳（B1 契约 §3 规则 3），agent
  是采样窗均值——口径差异由容差吸收，报告记录 agent 侧 ``sampled_at``。

====  容差（B5 定初值，B6 切换前按实测分布调）  =============================

``("abs", v)`` 绝对差 ≤ v；``("rel", r)`` 差 ≤ r × max(|两轨值|)。cpu 给
10pp（瞬时 vs 5min 均值的固有波动），内存 2.5pp，磁盘 1pp；绝对值与计数器
用相对容差。

====  同窗对齐（B6-3 对账验收，可选）========================================

``compare_tracks`` / ``run_comparison`` 的 ``align_window_seconds``（默认
None = 旧行为「窗口内取最新」）启用同窗配对：agent 轨逐指标改取与 beszel
侧机器级 ``sampled_at`` 最接近的一条样本（±窗内，等距取较晚）；配对质量
经每机 ``sampling.aligned_pairs``（metric / beszel_ts / agent_ts /
abs_diff_seconds）直接可读。机器级 ``sampled_at`` 与 dead man 判定仍看
全窗最新样本，不随对齐模式收窄。

**B6-3 第二版（几何死结修订）**：±窗内无样本时**不再**直接缺席计 missing
——六机相位互异 + 落库时延 60–70s 的现实下，「±180s 内必有样本」全天无解
（round1-failed §10 逐窗扫描证明）。改为：±窗外仍有样本时取距目标最近
的一条、标记为 out_of_window 落每机 ``sampling.out_of_window``（逐指标
``{metric, abs_diff_seconds}``），让 compare_machine 走正常比对路径；
missing 只在**整个回看窗内完全无样本**（漏采/断链）时计数。missing 的
语义由此从「不在 ±W 内」收窄到「真的没采到」——前者是节奏几何现象，后者
才是采集缺陷。三轮一致性门判「同机三轮是否都 out_of_window」：三轮全
out_of_window = 相位缺陷（应修采集），部分轮配上且 match = 节奏几何放行。

====  dead man's switch  ===================================================

B1-2 契约 §2.2 两形态，阈值 **12 分钟**（B0 冻结 10+2，不是 17）：

- 形态一 ``stale``：心跳正常但 agent 样本 ``sampled_at`` 距今 > 12 分钟。
- 形态二 ``source-failed``：Beszel 快照拉取失败（快照级 ``ok=False``，对全部
  受检机器各记一条）。

事件走 ``monitor_events.observe``，fingerprint 命名空间
``machine:monitoring:<id>`` 与机器探活（``machine:<id>``）物理隔离——探活
恢复不会误关采集中断事件，反之亦然。``emit_dead_mans_switch_events`` 幂等：
违规开/更新，恢复自动关（observe 的 transition 语义）。
"""
import datetime
import time

import monitor_events as me
import node_traffic as nt
import storage_dispatch as sd
from monitoring_schema import (
    METRIC_CONTAINER_PREFIX,
    METRIC_CPU_USED_PCT,
    METRIC_DISK_TOTAL_BYTES,
    METRIC_DISK_USED_BYTES,
    METRIC_DISK_USED_PCT,
    METRIC_MEM_TOTAL_BYTES,
    METRIC_MEM_USED_BYTES,
    METRIC_NET_RECV_TOTAL_BYTES,
    METRIC_NET_SENT_TOTAL_BYTES,
    container_metric,
    nic_metric,
    parse_nic_metric,
)

# B0 冻结：12 分钟 = 2×5min 采样周期 + 2min 余量（B1-2 §2.1 钉死 10+2，非 17）。
DEAD_MAN_THRESHOLD_SEC = 720
# Agent 轨回看窗：6 个采样周期，覆盖一次 dead man 阈值 + 缓冲重试。
AGENT_WINDOW_SEC = 1800
# 比对模块在事件 source 字段的自标识。
SOURCE_COMPARE = "monitoring-compare"
# 容器比对 schema 的单一事实源：beszel 快照与 agent 轨投影后的字段名。
# 旧验收遍历 ("cpu","memory") 而 memory 实为 memory_mb，内存全部漏检（R1）；
# 消费方一律引用此常量，不再各自硬编码字段清单。
CONTAINER_FIELDS = ("cpu", "memory_mb")

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# per-NIC 指标名的构造/解析在 monitoring_schema（B5-5 收口对齐）：
# ``net_sent_total_bytes@<nic>`` / ``net_recv_total_bytes@<nic>``。

def nic_metric_name(nic, direction):
    """Agent 轨网卡计数器的拍平 metric 名（direction: 'sent' | 'recv'）。"""
    base = METRIC_NET_SENT_TOTAL_BYTES if direction == "sent" else METRIC_NET_RECV_TOTAL_BYTES
    return nic_metric(base, nic)


def container_metric_name(name, field):
    """Agent 轨容器指标的拍平 metric 名（field: 'cpu_pct' | 'memory_used_bytes'）。"""
    return container_metric(name, field)

_MIB = 1024.0 * 1024.0
_GIB = 1024.0 * 1024.0 * 1024.0


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(moment):
    return moment.strftime(TS_FORMAT)


def _parse_ts(value):
    return nt.sample_time(value)


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# ---- 对齐注册表 ------------------------------------------------------------


def _derive_pct(metrics, total_key, part_key):
    total, part = _num(metrics.get(total_key)), _num(metrics.get(part_key))
    if not total or total <= 0 or part is None:
        return None
    return round(max(0.0, part * 100.0 / total), 2)


def _div(key, factor):
    def derive(metrics):
        value = _num(metrics.get(key))
        return round(value / factor, 1) if value is not None else None

    return derive


# 每项：key=报告键 / dimension=五类归属 / beszel_field 或 beszel_derive /
# agent_metric 或 agent_derive / unit / tolerance。rel 的基准取
# max(|两轨值|)，两侧接近 0 时不给相对容差放水。
METRIC_ALIGNMENT = (
    {
        "key": "cpu_pct", "dimension": "cpu",
        "beszel_field": "cpu_pct", "agent_metric": METRIC_CPU_USED_PCT,
        "unit": "%", "tolerance": ("abs", 10.0),
    },
    {
        "key": "memory_pct", "dimension": "memory",
        "beszel_field": "memory_pct",
        "agent_derive": lambda m: _derive_pct(m, METRIC_MEM_TOTAL_BYTES, METRIC_MEM_USED_BYTES),
        "unit": "%", "tolerance": ("abs", 2.5),
    },
    {
        "key": "memory_total_mb", "dimension": "memory",
        "beszel_field": "memory_total_mb",
        "agent_derive": _div(METRIC_MEM_TOTAL_BYTES, _MIB),
        "unit": "MiB", "tolerance": ("rel", 0.02),
    },
    {
        "key": "memory_used_mb", "dimension": "memory",
        "beszel_field": "memory_used_mb",
        "agent_derive": _div(METRIC_MEM_USED_BYTES, _MIB),
        "unit": "MiB", "tolerance": ("rel", 0.02),
    },
    {
        "key": "disk_pct", "dimension": "disk",
        "beszel_field": "disk_pct", "agent_metric": METRIC_DISK_USED_PCT,
        "unit": "%", "tolerance": ("abs", 1.0),
    },
    {
        "key": "disk_total_gb", "dimension": "disk",
        "beszel_field": "disk_total_gb",
        "agent_derive": _div(METRIC_DISK_TOTAL_BYTES, _GIB),
        "unit": "GiB", "tolerance": ("rel", 0.02),
    },
    {
        "key": "disk_used_gb", "dimension": "disk",
        "beszel_field": "disk_used_gb",
        "agent_derive": _div(METRIC_DISK_USED_BYTES, _GIB),
        "unit": "GiB", "tolerance": ("rel", 0.05),
    },
    {
        "key": "net_sent_total", "dimension": "network",
        "beszel_field": "net_sent_total", "agent_metric": ("nic", "sent"),
        "unit": "bytes", "tolerance": ("rel", 0.05),
    },
    {
        "key": "net_recv_total", "dimension": "network",
        "beszel_field": "net_recv_total", "agent_metric": ("nic", "recv"),
        "unit": "bytes", "tolerance": ("rel", 0.05),
    },    {
        "key": "container_count", "dimension": "containers",
        "beszel_field": "container_count", "agent_metric": "container_count",
        "unit": "count", "tolerance": ("abs", 1),
    },
)


# ---- Beszel 轨投影（纯函数，输入快照 payload） -----------------------------


def collect_beszel_track(snapshot):
    """Beszel 快照 → ``{machine_id: {"metrics", "containers", "nics"}}``。

    容器计数用 containers 实查（修正 B 口径），不读失真的 ``info.ct``；
    ``container_count`` 在这里派生，双轨同构。
    """
    track = {}
    containers_by_machine = {}
    for row in snapshot.get("containers") or []:
        machine = str(row.get("machine") or "").strip().lower()
        name = str(row.get("name") or "").strip()
        if not machine or not name:
            continue
        containers_by_machine.setdefault(machine, {})[name] = {
            "cpu": _num(row.get("cpu")),
            "memory_mb": _num(row.get("memory_mb")),
        }

    for entry in snapshot.get("systems") or []:
        machine = str(entry.get("machine") or "").strip().lower()
        if not machine:
            continue
        containers = containers_by_machine.get(machine, {})
        track[machine] = {
            "metrics": {
                "cpu_pct": _num(entry.get("cpu")),
                "memory_pct": _num(entry.get("memory_pct")),
                "memory_total_mb": _num(entry.get("memory_total_mb")),
                "memory_used_mb": _num(entry.get("memory_used_mb")),
                "disk_pct": _num(entry.get("disk_pct")),
                "disk_total_gb": _num(entry.get("disk_total_gb")),
                "disk_used_gb": _num(entry.get("disk_used_gb")),
                "net_sent_total": _num(entry.get("net_sent_total")),
                "net_recv_total": _num(entry.get("net_recv_total")),
                "container_count": float(len(containers)),
            },
            "containers": containers,
            "sampled_at": entry.get("net_sampled_at"),
        }
    return track


# ---- Agent 轨读取（只读 SELECT B4 表） -------------------------------------


def read_agent_track(machine_ids=None, window_sec=AGENT_WINDOW_SEC, now=None,
                     conn=None, align_to=None, align_window_seconds=None):
    """PG ``monitoring_samples`` → 与 Beszel 轨同构的 track。

    默认窗口内每 ``(machine, metric)`` 取最新一条；网卡/容器指标按前缀解析
    回结构化形态。不触网、不写库——对 B4 存储层只读引用。

    ``align_to={machine_id: beszel 机器级 sampled_at(datetime)}`` 时启用同窗
    对齐（B6-3）：逐指标改取距该时刻最近的一条（±``align_window_seconds``
    内，等距取较晚；**第二版**：±窗外仍有样本时取最近一条并标 out_of_window，
    只有整个回看窗内完全无样本才视为缺席/缺指标）；机器级 ``sampled_at``/
    ``window_from`` 仍按全窗计（dead man 判定不受对齐影响）。对齐模式下每指标
    额外带 ``metric_sampled_at``（该指标实际被采用样本的时间），供对账留证；
    每机额外带 ``out_of_window_metrics``（配对失败但取最近样本的指标清单）。
    beszel 侧无时间戳的机器回退「取最新」，不整台丢样本（丢样本会让
    dead man 把该机误判成「从未上报」）。
    """
    moment = now or _utc_now()
    cutoff = moment - datetime.timedelta(seconds=max(60, int(window_sec)))
    ids = [str(m).strip().lower() for m in (machine_ids or ()) if str(m).strip()]
    with sd.read_tx(conn=conn) as tx:
        if ids:
            rows = tx.execute(
                "SELECT machine_id, metric_name, metric_value, sampled_at "
                "FROM monitoring_samples "
                "WHERE sampled_at >= %s AND machine_id = ANY(%s) "
                "ORDER BY sampled_at ASC",
                (cutoff, ids),
            ).fetchall()
        else:
            rows = tx.execute(
                "SELECT machine_id, metric_name, metric_value, sampled_at "
                "FROM monitoring_samples WHERE sampled_at >= %s "
                "ORDER BY sampled_at ASC",
                (cutoff,),
            ).fetchall()

    targets = {}
    if align_to is not None:
        # beszel 机器级 sampled_at。无目标时间的机器（beszel 快照缺时间戳）
        # 回退「取最新」——不能整台丢样本，否则 dead man 判定会把该机误判
        # 成「从未上报」（能力问题），掩盖真实的中断/正常状态。
        targets = {str(m).strip().lower(): t for m, t in align_to.items()
                   if t is not None}

    latest = {}
    machine_latest = {}
    machine_earliest = {}
    # B6-3 第二版：配对失败的指标仍取最近样本，记 (machine, metric) → diff
    # 供 read_agent_track 返回时暴露给 compare_tracks 报告 out_of_window 列表。
    out_of_window_metrics = {}
    for row in rows:
        machine = row["machine_id"]
        metric = row["metric_name"]
        sampled = row["sampled_at"]
        value = float(row["metric_value"])
        target = targets.get(machine)
        if align_to is not None and target is not None:
            # 同窗对齐（B6-3）：每 (machine, metric) 保留距 beszel 机器级
            # sampled_at 最近的一条（±align_window_seconds；行序 ASC、严格
            # < 才覆盖——等距时较晚的样本胜出，不依赖遍历顺序的额外假设）。
            diff = abs((sampled - target).total_seconds())
            if diff <= align_window_seconds:
                current = latest.get((machine, metric))
                if current is None or diff < current[2]:
                    latest[(machine, metric)] = (value, sampled, diff)
            else:
                # B6-3 第二版（几何死结修订）：±窗外仍有样本时，取距目标最近
                # 的一条但标记 out_of_window；只有窗口内完全无样本（漏采/断链）
                # 才计 missing。missing 的语义由此从「不在 ±W 内」收窄到
                # 「整个回看窗内都没样本」——前者在六机相位互异 + 落库时延
                # 60–70s 的现实下全天无解（见 round1-failed §10），后者才
                # 是采集缺陷的真实信号。
                current_oow = out_of_window_metrics.get((machine, metric))
                if current_oow is None or diff < current_oow[2]:
                    out_of_window_metrics[(machine, metric)] = (value, sampled, diff)
        else:
            latest[(machine, metric)] = (value, sampled)
        previous = machine_latest.get(machine)
        if previous is None or sampled > previous:
            machine_latest[machine] = sampled
        first = machine_earliest.get(machine)
        if first is None or sampled < first:
            machine_earliest[machine] = sampled

    if align_to is not None:
        # 把 out_of_window 的最近样本合并进 latest（保留 diff 供报告），
        # 让 compare_machine 走正常比对路径；missing 只在 latest 与
        # out_of_window 都空时发生（即窗口内完全无样本）。
        # 同时把 out_of_window 的 (machine, metric, diff) 落到 machine 级
        # 报告字段，供 compare_tracks 写出逐机 out_of_window 列表。
        # 先记下哪些 (machine, metric) 在窗内配上——out_of_window 列表只应
        # 报**真正走了回退路径**的指标：同一指标在窗内配上时，窗外那条更远
        # 的样本对报告无意义。若不剔除，out_of_window 会变成「窗外是否存在
        # 样本」的清单而非「本次比对用了窗外样本」的清单——轮 2 实测 aly/cpu
        # 窗内配上（残差 69.2s）却仍被报残差 368.3s，正是此缺陷。
        _in_window_keys = set(latest.keys())
        for key, triple in out_of_window_metrics.items():
            if key not in latest:
                latest[key] = triple
        latest = {key: (pair[0], pair[1]) for key, pair in latest.items()}
        _oow_per_machine = {}
        for (machine, metric), (value, sampled, diff) in out_of_window_metrics.items():
            if (machine, metric) in _in_window_keys:
                # 该指标窗内有更近样本，比对用的是它 → 不算 out_of_window
                continue
            _oow_per_machine.setdefault(machine, []).append({
                "metric": metric,
                "abs_diff_seconds": round(diff, 1),
            })
    else:
        _oow_per_machine = {}

    track = {}
    for (machine, metric), (value, sampled) in latest.items():
        data = track.setdefault(
            machine,
            {"metrics": {}, "containers": {}, "nics": {}, "sampled_at": None,
             "window_from": None},
        )
        if data["sampled_at"] is None or sampled > machine_latest.get(machine, sampled):
            data["sampled_at"] = machine_latest.get(machine)
        if data["window_from"] is None:
            data["window_from"] = machine_earliest.get(machine)
        if align_to is not None:
            data.setdefault("metric_sampled_at", {})[metric] = _iso(sampled)
        if metric.startswith(METRIC_CONTAINER_PREFIX):
            rest = metric[len(METRIC_CONTAINER_PREFIX):]
            if rest.endswith("_cpu_pct"):
                name = rest[: -len("_cpu_pct")]
                data["containers"].setdefault(name, {})["cpu"] = value
            elif rest.endswith("_memory_used_bytes"):
                name = rest[: -len("_memory_used_bytes")]
                data["containers"].setdefault(name, {})["memory_mb"] = round(value / _MIB, 1)
        elif "@" in metric:
            base, nic = parse_nic_metric(metric)
            if base in (METRIC_NET_SENT_TOTAL_BYTES, METRIC_NET_RECV_TOTAL_BYTES) and nic:
                data["nics"].setdefault(nic, {})["sent" if base == METRIC_NET_SENT_TOTAL_BYTES else "recv"] = value
        else:
            data["metrics"][metric] = value

    for machine, data in track.items():
        data["metrics"]["container_count"] = float(len(data["containers"]))
        data["sampled_at"] = _iso(data["sampled_at"]) if data["sampled_at"] else None
        data["window_from"] = _iso(data["window_from"]) if data["window_from"] else None
        # B6-3 第二版：暴露逐机 out_of_window 列表（无则空列表，不省略字段
        # 以保持 schema 稳定——判据侧靠字段存在性区分「跑了对齐」与「没跑」）。
        if align_to is not None:
            data["out_of_window_metrics"] = _oow_per_machine.get(machine, [])
    return track


def _pick_agent_nic(nics, machine_id):
    """agent 轨计费网卡：台账 ``traffic_nic`` 显式优先，否则累计量最大的
    可计费网卡（复用 node_traffic 排除表；与 beszel 侧 pick_nic 同规则）。"""
    if not isinstance(nics, dict) or not nics:
        return None
    import machine_config as mc

    preferred = str((mc.get_machine(machine_id) or {}).get("traffic_nic") or "").strip()
    if preferred and preferred in nics and _nic_has_totals(nics[preferred]):
        return preferred
    best = None
    for nic, totals in nics.items():
        if not nt.is_billable_nic(nic) or not _nic_has_totals(totals):
            continue
        volume = totals.get("sent", 0) + totals.get("recv", 0)
        if best is None or volume > best[1]:
            best = (nic, volume)
    return best[0] if best else None

def _nic_has_totals(totals):
    return isinstance(totals, dict) and "sent" in totals and "recv" in totals


# ---- 容差比对 --------------------------------------------------------------


def _agent_value(alignment, agent_data, machine_id):
    """注册表项 → agent 轨数值；网卡计数器先挑计费网卡。"""
    metrics = agent_data.get("metrics") or {}
    spec = alignment.get("agent_metric")
    if isinstance(spec, tuple) and spec[0] == "nic":
        nic = _pick_agent_nic(agent_data.get("nics") or {}, machine_id)
        return nic, (agent_data.get("nics") or {}).get(nic, {}).get(spec[1])
    derive = alignment.get("agent_derive")
    if derive is not None:
        return None, derive(metrics)
    return None, metrics.get(spec)


def within_tolerance(delta, beszel_value, agent_value, tolerance):
    """容差判定。rel 基准取 max(|两轨值|)：单一侧为基会在对方接近 0 时放水。"""
    kind, limit = tolerance
    if kind == "abs":
        return abs(delta) <= limit
    base = max(abs(beszel_value), abs(agent_value))
    return abs(delta) <= limit * base


def _metric_report(alignment, beszel_value, agent_value, agent_nic=None):
    kind, limit = alignment["tolerance"]
    report = {
        "dimension": alignment["dimension"],
        "unit": alignment["unit"],
        "tolerance": {"kind": kind, "limit": limit},
        "beszel": beszel_value,
        "agent": agent_value,
    }
    if beszel_value is None and agent_value is None:
        report["status"] = "missing-both"
    elif beszel_value is None:
        report["status"] = "missing-beszel"
    elif agent_value is None:
        report["status"] = "missing-agent"
    else:
        report["delta"] = round(agent_value - beszel_value, 4)
        report["status"] = (
            "match" if within_tolerance(report["delta"], beszel_value, agent_value,
                                        alignment["tolerance"])
            else "divergent"
        )
    if agent_nic:
        report["agent_nic"] = agent_nic
    return report


def compare_machine(beszel_data, agent_data, machine_id):
    """单机全注册表指标比对；附带容器名集合差（同名容器才逐项比）。"""
    metrics = {}
    beszel_metrics = (beszel_data or {}).get("metrics") or {}
    for alignment in METRIC_ALIGNMENT:
        agent_nic, agent_value = _agent_value(alignment, agent_data or {}, machine_id)
        metrics[alignment["key"]] = _metric_report(
            alignment, beszel_metrics.get(alignment["beszel_field"]), agent_value,
            agent_nic=agent_nic,
        )

    containers = {}
    beszel_containers = (beszel_data or {}).get("containers") or {}
    agent_containers = (agent_data or {}).get("containers") or {}
    for name in sorted(set(beszel_containers) | set(agent_containers)):
        b_row, a_row = beszel_containers.get(name), agent_containers.get(name)
        containers[name] = {
            "status": "both" if b_row and a_row else
                      ("missing-beszel" if a_row else "missing-agent"),
            "cpu": _container_metric_diff(b_row, a_row, "cpu", ("abs", 5.0)),
            "memory_mb": _container_metric_diff(b_row, a_row, "memory_mb", ("rel", 0.05)),
        }
    return {"metrics": metrics, "containers": containers}


def summarize_containers(machines):
    """逐机容器明细 → 容器级分项统计（B6-1：summary 不再只数主机维度）。

    单位是「容器 × 指标」条目（cpu/memory_mb 各算一项，与试点 §10.2 的
    34 项口径一致），字段清单以 ``CONTAINER_FIELDS`` 为准。单侧缺容器
    （整机比对里 ``missing-agent`` / ``missing-beszel``）按每字段各计一条
    missing 并归属对应侧，不静默跳过（R1：缺容器必须显式进 summary）。
    两侧都缺的机器不产生条目（无从比对，不稀释分母）。
    """
    compared = match = divergent = 0
    missing = {"beszel": 0, "agent": 0}
    for report in machines.values():
        for container in (report.get("containers") or {}).values():
            side = container.get("status")
            for field in CONTAINER_FIELDS:
                diff = container.get(field) or {}
                status = diff.get("status")
                if status == "missing":
                    # missing 条目带 side 区分单侧缺席；两侧同缺不会出现
                    #（容器行至少一侧存在才进循环）。字段级 missing 沿用
                    # 容器级 side 归属（单侧有行但该字段缺值）。
                    if side == "missing-agent":
                        missing["agent"] += 1
                    else:
                        missing["beszel"] += 1
                    continue
                if side != "both":
                    # 整容器单侧缺席：每字段各计一条 missing，归属缺席侧。
                    if side == "missing-agent":
                        missing["agent"] += 1
                    else:
                        missing["beszel"] += 1
                    continue
                compared += 1
                if status == "match":
                    match += 1
                elif status == "divergent":
                    divergent += 1
    total = max(1, compared + missing["beszel"] + missing["agent"])
    return {
        "compared": compared,
        "match": match,
        "divergent": divergent,
        "missing": dict(missing),
        "missing_rate": {
            "beszel": round(missing["beszel"] / total, 4),
            "agent": round(missing["agent"] / total, 4),
        },
    }


def _container_metric_diff(b_row, a_row, field, tolerance):
    b_value = (b_row or {}).get(field)
    a_value = (a_row or {}).get(field)
    if b_value is None or a_value is None:
        return {"beszel": b_value, "agent": a_value, "status": "missing"}
    delta = round(a_value - b_value, 4)
    return {
        "beszel": b_value, "agent": a_value, "delta": delta,
        "status": "match" if within_tolerance(delta, b_value, a_value, tolerance)
                  else "divergent",
    }


def _aligned_pairs(beszel_data, agent_data):
    """对齐模式的配对留证：逐指标 ``{metric, beszel_ts, agent_ts,
    abs_diff_seconds}``（beszel 无时间戳或解析失败时无配对，空列表）。"""
    pairs = []
    beszel_ts = (beszel_data or {}).get("sampled_at")
    target = _parse_ts(beszel_ts) if beszel_ts else None
    if target is None:
        return pairs
    metric_times = (agent_data or {}).get("metric_sampled_at") or {}
    for metric in sorted(metric_times):
        moment = _parse_ts(metric_times[metric])
        if moment is None:
            continue
        pairs.append({
            "metric": metric,
            "beszel_ts": beszel_ts,
            "agent_ts": metric_times[metric],
            "abs_diff_seconds": round(abs((moment - target).total_seconds()), 1),
        })
    return pairs


def compare_tracks(beszel_track, agent_track, machine_ids,
                   align_window_seconds=None):
    """双轨全量比对 → ``{machines, summary}``（summary 含缺失率与两轨采样时间）。

    每机报告带 ``sampling``：beszel 侧快照时间（system_stats 的
    ``net_sampled_at``）与 agent 侧最新样本时间。两轨本就各自采样（比对
    不强制同窗对齐），时间差在报告里可见，供差异归因区分采样窗口错位
    与真实口径差异（R1）。

    ``align_window_seconds`` 非 None 时（B6-3 同窗对账）：要求 agent 轨
    已按同窗口径读取（``read_agent_track(align_to=...)``），并在每机
    ``sampling`` 额外暴露 ``aligned_pairs``（逐指标配对的
    ``(beszel_ts, agent_ts, abs_diff_seconds)``）与 ``out_of_window``
    （**第二版**：配对失败但取最近样本的指标清单，``{metric,
    abs_diff_seconds}``；对齐质量直接可读）。**第二版起**，±窗外仍有
    样本的指标不再计入 missing——missing 只在整个回看窗内完全无样本
    （漏采/断链）时计数。
    """
    machines = {}
    missing = {"beszel": 0, "agent": 0}
    compared = match = divergent = 0
    for machine_id in machine_ids:
        beszel_data = beszel_track.get(machine_id)
        agent_data = agent_track.get(machine_id)
        report = compare_machine(beszel_data, agent_data, machine_id)
        report["sampling"] = {
            "beszel_sampled_at": (beszel_data or {}).get("sampled_at"),
            "agent_sampled_at": (agent_data or {}).get("sampled_at"),
            "agent_window_from": (agent_data or {}).get("window_from"),
        }
        if align_window_seconds is not None:
            report["sampling"]["align_window_seconds"] = align_window_seconds
            report["sampling"]["aligned_pairs"] = _aligned_pairs(
                beszel_data, agent_data
            )
            # B6-3 第二版：暴露逐机 out_of_window（配对失败但取最近样本的
            # 指标清单），供三轮一致性门判「同机三轮是否都 out_of_window」。
            report["sampling"]["out_of_window"] = (
                agent_data or {}
            ).get("out_of_window_metrics", [])
        machines[machine_id] = report
        for metric in report["metrics"].values():
            compared += 1
            status = metric["status"]
            if status == "match":
                match += 1
            elif status == "divergent":
                divergent += 1
            if status in ("missing-beszel", "missing-both"):
                missing["beszel"] += 1
            if status in ("missing-agent", "missing-both"):
                missing["agent"] += 1

    total = max(1, compared)
    summary = {
        "machines": len(machine_ids),
        "metrics_compared": compared,
        "match": match,
        "divergent": divergent,
        "missing": dict(missing),
        "missing_rate": {
            "beszel": round(missing["beszel"] / total, 4),
            "agent": round(missing["agent"] / total, 4),
        },
        # B6-1：容器级分项。主机级 match/divergent 不含逐容器条目（口径
        # 不混算）；「10/10 全 match」必须同时看 containers 分项才算全绿。
        "containers": summarize_containers(machines),
        # R1：两轨采样时间逐机可见——「未同窗对齐」不再只藏在代码路径里。
        "sampling": {
            m: machines[m]["sampling"] for m in machine_ids
        },
    }
    return {"machines": machines, "summary": summary}


# ---- dead man's switch -----------------------------------------------------


def dead_mans_switch(agent_track, beszel_ok, machine_ids, now=None,
                     threshold_sec=DEAD_MAN_THRESHOLD_SEC):
    """两形态采集中断检测（B1-2 §2.2）。

    形态一 ``stale``：agent 最新样本 ``sampled_at`` 距今超阈值（心跳正常但
    指标停更）。形态二 ``source-failed``：Beszel 快照拉取失败，对每台受检
    机器各记一条（快照级故障逐机呈现，事件才有 machine 归属）。
    未 enrol / 从未上报的机器不在 agent_track 里，不产生 stale 误报——
    「从未采到」是能力问题，不是中断。
    """
    moment = now or _utc_now()
    violations = []
    if not beszel_ok:
        for machine_id in machine_ids:
            violations.append({
                "machine": machine_id,
                "form": "source-failed",
                "last_sampled_at": (agent_track.get(machine_id) or {}).get("sampled_at"),
                "silence_sec": None,
            })
    for machine_id in machine_ids:
        data = agent_track.get(machine_id) or {}
        sampled = _parse_ts(data.get("sampled_at"))
        if sampled is None:
            continue
        silence = (moment - sampled).total_seconds()
        if silence > threshold_sec:
            violations.append({
                "machine": machine_id,
                "form": "stale",
                "last_sampled_at": _iso(sampled),
                "silence_sec": int(silence),
            })
    return violations


def emit_dead_mans_switch_events(machine_states, ts=None, conn=None):
    """violations → monitor_events（幂等 transition）。

    ``machine_states`` 是 ``{machine_id: violation | None}`` 全量状态：None
    调 ``observe(ok=True)`` 自动关事件（恢复），violation 开/更新。返回每机
    的 observe 结果（opened/updated/resolved/None）。fingerprint 命名空间
    ``machine:monitoring:<id>``，与探活的 ``machine:<id>`` 物理隔离。
    severity 用 warn：数据断流不是机器本身故障（与 observe_public 同判例）。
    """
    results = {}
    for machine_id, violation in machine_states.items():
        target = "monitoring:{}".format(machine_id)
        if violation is None:
            results[machine_id] = me.observe(
                me.KIND_MACHINE, target, True, source=SOURCE_COMPARE,
                machine=machine_id, ts=ts, conn=conn,
            )
            continue
        form_label = "采集中断" if violation["form"] == "stale" else "Beszel 断联"
        detail = "最后样本 {}，静默 {} 秒".format(
            violation.get("last_sampled_at") or "无",
            violation.get("silence_sec") if violation.get("silence_sec") is not None else "未知",
        )
        results[machine_id] = me.observe(
            me.KIND_MACHINE, target, False, source=SOURCE_COMPARE,
            machine=machine_id,
            title="{} 监控{}".format(machine_id.upper(), form_label),
            detail=detail, severity="warn", ts=ts, conn=conn,
        )
    return results


# ---- 顶层入口 --------------------------------------------------------------


def run_comparison(machine_ids=None, snapshot=None, window_sec=AGENT_WINDOW_SEC,
                   now=None, conn=None, align_window_seconds=None):
    """双轨采样 → 对齐 → 容差比对 → 报告（含缺失率、资源开销、dead man）。

    ``snapshot=None`` 时现场拉 Beszel 快照（生产路径）；测试注入假快照不触
    网。``machine_ids=None`` 默认比对台账全集与两轨可见机器的交集——没有
    双侧数据的机器在报告里如实呈现 missing，不静默跳过。

    ``align_window_seconds`` 非 None 时启用 B6-3 同窗对账：agent 轨按各机
    beszel 机器级 ``sampled_at`` 就近取样本（±窗），报告 ``sampling`` 带
    ``aligned_pairs``；None 保持旧行为（窗口内取最新）。
    """
    moment = now or _utc_now()
    costs = {}
    if machine_ids is None:
        import machine_config as mc

        machine_ids = sorted(mc.load_machines() or ())
    machine_ids = [str(m).strip().lower() for m in machine_ids]

    if snapshot is None:
        try:
            import beszel_client
        except ImportError as exc:  # public tree has no private Beszel bridge
            raise RuntimeError(
                "live Beszel snapshot fetch requires the private beszel_client "
                "bridge; inject snapshot=... for public/kernel-only runs"
            ) from exc

        started = time.monotonic()
        snapshot = beszel_client.get_monitor_snapshot()
        costs["beszel_fetch_sec"] = round(time.monotonic() - started, 4)

    beszel_ok = bool(snapshot.get("ok"))
    started = time.monotonic()
    beszel_track = collect_beszel_track(snapshot)
    costs["beszel_project_sec"] = round(time.monotonic() - started, 4)

    align_to = None
    if align_window_seconds is not None:
        # beszel 机器级采样时间即对齐目标（net_sampled_at，B1 契约 §3 机器级
        # sampled_at 的现状对应）；parse 失败/缺失的机器在 read_agent_track
        # 内回退取最新。
        align_to = {
            machine: _parse_ts(data.get("sampled_at"))
            for machine, data in beszel_track.items()
        }

    started = time.monotonic()
    agent_track = read_agent_track(machine_ids=machine_ids, window_sec=window_sec,
                                   now=moment, conn=conn, align_to=align_to,
                                   align_window_seconds=align_window_seconds)
    costs["pg_query_sec"] = round(time.monotonic() - started, 4)

    started = time.monotonic()
    comparison = compare_tracks(beszel_track, agent_track, machine_ids,
                                align_window_seconds=align_window_seconds)
    violations = dead_mans_switch(agent_track, beszel_ok, machine_ids, now=moment)
    costs["compare_sec"] = round(time.monotonic() - started, 4)

    machine_states = {m: None for m in machine_ids}
    for violation in violations:
        machine_states[violation["machine"]] = violation

    costs["total_sec"] = round(sum(v for k, v in costs.items()), 4)
    return {
        "generated_at": _iso(moment),
        "window_sec": int(window_sec),
        "align_window_seconds": align_window_seconds,
        "beszel": {
            "ok": beszel_ok,
            "error": snapshot.get("error"),
            "systems": len(snapshot.get("systems") or []),
        },
        "machines": comparison["machines"],
        "summary": comparison["summary"],
        "dead_man_switch": {
            "threshold_sec": DEAD_MAN_THRESHOLD_SEC,
            "violations": violations,
        },
        "resource_cost": costs,
        "agent_track": agent_track,
    }
