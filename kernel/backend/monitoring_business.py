# -*- coding: utf-8 -*-
"""B5-3 业务接回：磁盘预测、流量账本、事件从 Beszel 影子切到 Console PG。

三个业务模块在新数据链上的读路径。B5 只走影子：正式计量与告警仍由 Beszel 驱动
（B5 README §1「Beszel 仍是正式数据源」），本模块的所有读取都带 ``source`` 回显，
够 B5-1 把同一窗口的 Beszel 结果与本模块输出对账。

B6-4 后正式业务只走 ``console_pg``。``business_source()`` 恒返回 PG，旧环境变量
不再驱动页面、记账或事件；归档/历史兼容工具仍单独保留。

磁盘预测（B1-3 §4.1）：只读 raw 档 30 天窗口，480m 是**读取时投影**——按 8 小时桶
分桶、桶内取均值、空桶跳过不造点。样本数/跨度/r² 门槛全部复用 ``capacity``，语义
与旧 480m 档一致；不用 hourly 聚合反推（丢桶内样本数），不与旧 Beszel 480m 点
等权混合。单位坑：PG 里存 bytes（Go agent ``disk_*_bytes``），Beszel 是 10⁹ 浮点
GB——本模块输出统一折算成 GB，趋势字段与 ``capacity`` 旧形状对齐。

流量账本（B1-3 §4.2）：``record_traffic_from_pg`` 是 flag=console_pg 时的记账
写入方（B6-3/Q3 顺延——B6-4 退役 Beszel 的硬阻塞项），喂同一写入原语
``node_traffic.record_sample``（月份翻页/归零/换卡/advisory lock/拒旧快照全在
原语里）；B6-4 后只由 PG 样本驱动，不再读取旧快照。
``traffic_rebuild`` 是配套的只读影子：从 raw 样本按 UTC 自然月分桶、复用同一
差分规则、只算出站，回答「切换那一刻新链路记的账长什么样」供 B5-1 对账；机器
重启落两次样本时中间的实际流量不可知，如实报 ``estimated=False``，不把影子账
写成无损精确计费（总计划 B5 验收原文）。

事件接回：``monitor_events`` 本身是自有表，与 Beszel 无关；真正依赖 Beszel 的
是 ``monitor_observability`` 的 ``active_alerts``/``recent_events`` 投影（Beszel
``alerts``/``alerts_history`` collections）。本模块从 Console PG 的指标连续性
观测「采集中断」事件：断流超 ``INGEST_GAP_MINUTES``（B0 冻结 12 分钟）开
``monitoring:ingest-gap:<machine>`` fingerprint 的事件，恢复时 observe 幂等关闭。
kind 用 ``machine``（K3 折叠的根语义就是「这台机器的事」），source 带前缀区分。
阈值告警评估器归 B5-4，本模块不实现。

缺口表达（B1-3 §3）：``gap_summary`` 返回每个 series 的
``{machine, first_at, last_at, samples, history_start, earliest_queryable,
insufficient}``——``first_at=None`` 表达「从未采到」，``insufficient=True``
表达「样本不足门槛」（capacity 的明确「样本不足」表达，总计划 B5 验收原文）。
B5-2 回填落地前的存储起点前区间即 B1-3 的 ``archived`` 缺口。

source 分类（B6 任务裁决，接 maintenance 线的 source 语义）：

``monitoring_samples.source`` 是自由 TEXT、**无 CHECK 约束**（B4 schema 冻结），
「非回填 ⇒ agent 实时」目前是 **writer 约定而不是 schema 约定**：当前唯一的
两个写入方是 monitoring_api 的 PG store（source = agent 上报的版本串，如
``0.1.0``/``go-agent/0.1.0``）与 monitoring_backfill（source =
``beszel-backfill``）。因此 disk_trends / traffic_rebuild 的样本选择按
**显式排除已知非 agent 来源**（``NON_AGENT_SAMPLE_SOURCES``）实现，而不是
白名单匹配版本串形状——agent 版本串格式变化不破，maintenance 线新增分类时
往该常量扩充即可。``gap_summary`` 是反例：缺口表达必须把回填点算进历史
（回填就是用来填 ``archived`` 缺口的），``_series_bounds`` 不做 source 过滤。
等权混合的禁令（B1-3 §4.1）只约束**投影/差分**类消费：480m 投影与流量差分
对采样间隔敏感，回填 480m 点与 agent 5min 点混进同一序列会失真；first/last/
count 这类粗粒度统计不受此限。
"""
import datetime

import machine_config as mc
import monitor_events
import monitoring_ingest as mi
import node_traffic as nt
import storage_dispatch as sd
from monitoring_schema import (
    METRIC_CONTAINER_PREFIX,
    METRIC_CPU_USED_PCT,
    METRIC_DISK_TOTAL_BYTES,
    METRIC_DISK_USED_BYTES,
    METRIC_DISK_USED_PCT,
    METRIC_LOAD_AVG_1M,
    METRIC_LOAD_AVG_5M,
    METRIC_LOAD_AVG_15M,
    METRIC_MEM_TOTAL_BYTES,
    METRIC_MEM_USED_BYTES,
    METRIC_MEM_USED_PCT,
    METRIC_NET_RECV_TOTAL_BYTES,
    METRIC_NET_SENT_TOTAL_BYTES,
    METRIC_UPTIME_SEC,
    nic_metric,
    parse_nic_metric,
)

GIB = 1024 ** 3

# 指标名单一事实源在 monitoring_schema（B5-5 收口对齐）：主机标量是 Go agent
# payload 字段名直写，加 ``@<nic>`` 后缀表达 per-NIC 序列（一个 metric_name 一个
# 序列，UNIQUE(machine_id, metric_name, sampled_at) 才能物理去重）。
METRIC_CPU = METRIC_CPU_USED_PCT
METRIC_DISK_USED = METRIC_DISK_USED_BYTES
METRIC_DISK_TOTAL = METRIC_DISK_TOTAL_BYTES
METRIC_NET_SENT = METRIC_NET_SENT_TOTAL_BYTES
METRIC_NET_RECV = METRIC_NET_RECV_TOTAL_BYTES


# B0 冻结：12 分钟过期阈值（5 分钟采样 + 2 周期延迟 + 2 分钟余量）。
INGEST_GAP_MINUTES = 12

# B1-3 §4.1：raw 30 天保留恰好覆盖 ~30 天分析窗口。
ANALYSIS_WINDOW_DAYS = 30
# 480m 是分析粒度投影，不是存储档位。
PROJECTION_BUCKET_HOURS = 8

SOURCE_CONSOLE_PG = "console_pg"

# 已知「非 agent 实时」来源：本模块投影/差分类消费必须排除的 source 值。
# ``beszel-backfill``（B5-2 回填）是当前唯一实例；maintenance 线给 raw source
# 加分类时扩充此常量（module docstring「source 分类」一节）。
NON_AGENT_SAMPLE_SOURCES = ("beszel-backfill",)





def business_source():
    """当前正式业务源恒为 Console PG；旧环境开关不再参与选择。"""
    return SOURCE_CONSOLE_PG





def _to_utc(value):
    """datetime/None 直通；其余按 monitoring_ingest 的归一规则转 aware UTC。"""
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=datetime.timezone.utc)
        return value.astimezone(datetime.timezone.utc)
    if value is None:
        return None
    return mi._to_utc_datetime(value)  # noqa: SLF001 —— 同一数据链的时间归一口径


def _iso(moment):
    if moment is None:
        return None
    return moment.astimezone(datetime.timezone.utc).strftime(mi.TS_FORMAT)


def _read_tx(conn=None):
    """monitoring_* 表的只读事务；PG-only（B4 链路无 SQLite 路径）。"""
    return sd.read_tx(conn=conn)


# ---- PG 查询小件 ------------------------------------------------------------


def _fetch_samples(conn, machine, metrics, start, end):
    """按 (machine, metrics[], 时间窗) 拉原始样本，返回 {metric: [(ts, value), ...]}。

    **只取 agent 实时样本**：显式排除 ``NON_AGENT_SAMPLE_SOURCES``（当前即
    ``beszel-backfill``），回填 480m 点不与 agent 5min 点等权混合（B1-3 §4.1）。
    单机单窗的成本与窗口内样本数线性（idx_monitoring_samples_scan）。
    """
    series = {metric: [] for metric in metrics}
    ph = sd.ph()
    placeholders = ",".join([ph] * len(metrics))
    not_backfill = " AND ".join(
        ["source <> " + ph] * len(NON_AGENT_SAMPLE_SOURCES)
    )
    rows = conn.execute(
        "SELECT metric_name, sampled_at, metric_value FROM monitoring_samples "
        f"WHERE machine_id = {ph} AND metric_name IN ({placeholders}) "
        f"AND sampled_at >= {ph} AND sampled_at <= {ph} AND {not_backfill} "
        "ORDER BY sampled_at ASC",
        (machine, *metrics, start, end, *NON_AGENT_SAMPLE_SOURCES),
    ).fetchall()
    for row in rows:
        metric = row["metric_name"]
        if metric in series:
            series[metric].append((_to_utc(row["sampled_at"]), float(row["metric_value"])))
    return series


def _series_bounds(conn, machine, metric):
    """(first_at, last_at, sample_count)；无样本返回 (None, None, 0)。

    B1-3 §5.4：逐机逐指标的 first/last/count（含 0 行）必须廉价可得。
    """
    ph = sd.ph()
    row = conn.execute(
        "SELECT MIN(sampled_at) AS first_at, MAX(sampled_at) AS last_at, "
        f"COUNT(*) AS samples FROM monitoring_samples "
        f"WHERE machine_id = {ph} AND metric_name = {ph}",
        (machine, metric),
    ).fetchone()
    if row is None or row["samples"] is None:
        return None, None, 0
    return _to_utc(row["first_at"]), _to_utc(row["last_at"]), int(row["samples"])


def _enrolled_machines(conn=None):
    """monitoring_machines 里的 active 机器，{id: display_name}。"""
    with _read_tx(conn=conn) as own:
        rows = own.execute(
            "SELECT machine_id, display_name FROM monitoring_machines "
            "WHERE status = 'active' ORDER BY machine_id ASC"
        ).fetchall()
    return {row["machine_id"]: row["display_name"] for row in rows}


# ---- 0. 面板快照（B6-3：/api/monitor 的 console_pg 分支）---------------------

# 快照读窗：最近 15 分钟（300s 周期 × 3，容忍两轮丢失仍能给出读数）。
SNAPSHOT_WINDOW_MINUTES = 15

# 容器指标名的字段后缀（monitoring_schema.container_metric 的逆操作）。
_CONTAINER_FIELDS = ("cpu_pct", "memory_used_bytes")


def parse_container_metric(metric_name):
    """``container_<name>_<field>`` → ``(name, field)``；形状不符返回 ``(None, None)``。

    容器名可含下划线（hindsight-pg 用连字符，但 Docker 名两态都合法），按
    **已知字段后缀切**而不是按第一个下划线切：先剥 ``container_`` 前缀，再逐个
    尝试 ``_<field>`` 后缀（cpu_pct 在前，memory_used_bytes 更长且不与 cpu 后缀
    重叠，两者无歧义）。
    """
    name = str(metric_name or "")
    if not name.startswith(METRIC_CONTAINER_PREFIX):
        return None, None
    body = name[len(METRIC_CONTAINER_PREFIX):]
    for field in _CONTAINER_FIELDS:
        suffix = "_" + field
        if body.endswith(suffix) and len(body) > len(suffix):
            return body[: -len(suffix)], field
    return None, None


def _last_value(points):
    """[(ts, value), ...] 取最近值；空序列给 None。"""
    return points[-1][1] if points else None


def _container_status(points):
    """窗口内有样本即视为 running；一个点都没有才是 unknown。

    agent 只上报它看得见的容器：容器停了样本就断流，「最近 15 分钟有无样本」
    是 PG 侧对容器存活的最强可得推断，不伪装成 Docker 原文状态。
    """
    return "running" if points else "unknown"


def monitor_snapshot(now=None, conn=None):
    """主机/容器快照（Console PG 版 ``beszel_client.get_monitor_snapshot``）。

    输出键名对齐 Beszel 载荷（前端 today-focus / unified-table / machine-card
    按可选链读，缺键不炸）；PG 无数据的字段置 None/空，不伪装：

    - ``temperature``/``net_bytes``/``host``/``status``：agent 不采，置 None；
    - 容器 ``image``/``net_io``/``ports``：agent 指标流里没有，置空；
      ``health``=unknown / ``health_code``=0（Docker health 不进指标流）；
    - 容器 ``status`` 按窗口内有无样本推 running/unknown（见 ``_container_status``）。

    与 Beszel 分支的差异是刻意的：不经页面缓存、不触发流量记账（Q3 裁决账本
    不切，仍由 Beszel 链驱动）。样本口径同 ``_fetch_samples``：只认 agent 实时
    样本，``beszel-backfill`` 回填点不进实时读数。
    """
    moment = _to_utc(now) or datetime.datetime.now(datetime.timezone.utc)
    start = moment - datetime.timedelta(minutes=SNAPSHOT_WINDOW_MINUTES)

    host_metrics = [
        METRIC_CPU,
        METRIC_MEM_USED_PCT,
        METRIC_MEM_USED_BYTES,
        METRIC_MEM_TOTAL_BYTES,
        METRIC_DISK_USED_PCT,
        METRIC_DISK_USED_BYTES,
        METRIC_DISK_TOTAL_BYTES,
        METRIC_LOAD_AVG_1M,
        METRIC_LOAD_AVG_5M,
        METRIC_LOAD_AVG_15M,
        METRIC_UPTIME_SEC,
    ]

    payload = {
        "ok": True,
        "updated_at": moment.strftime("%Y-%m-%d %H:%M:%S"),
        "probe_interval": _snapshot_probe_interval(),
        "systems": [],
        "containers": [],
        "source": SOURCE_CONSOLE_PG,
    }

    with _read_tx(conn=conn) as own:
        machines = _enrolled_machines_rows(own)
        containers = {}  # (machine, container_name) -> 行
        for machine, display_name, last_seen in machines:
            series = _fetch_samples(own, machine, host_metrics, start, moment)
            # 容器指标名开放集合：按前缀 LIKE 拉，再在内存里解析分组
            container_rows = _fetch_container_samples(own, machine, start, moment)

            cpu = _last_value(series[METRIC_CPU])
            mem_used = _last_value(series[METRIC_MEM_USED_BYTES])
            mem_total = _last_value(series[METRIC_MEM_TOTAL_BYTES])
            disk_used = _last_value(series[METRIC_DISK_USED_BYTES])
            disk_total = _last_value(series[METRIC_DISK_TOTAL_BYTES])
            memory_pct = _last_value(series[METRIC_MEM_USED_PCT])
            if memory_pct is None and mem_used is not None and mem_total:
                memory_pct = mem_used / mem_total * 100.0

            system_id = "machine:{}".format(machine)
            # status 语义对齐 Beszel（'up'/其它）：前端 countOfflineServers 按
            # status !== 'up' 数离线（partition.ts），置 None 会让全部 server
            # 误判离线。PG 侧等价判活 = agent 心跳（last_seen_at）距 now ≤
            # DMS 新鲜度阈值（12min，CONTINUITY_GAP_SEC 同源）；窗口内有样本
            # 也算（心跳与样本任一新鲜即 up）。
            up = (
                last_seen is not None
                and (moment - last_seen).total_seconds() <= 600 + 60
            ) or bool(series[METRIC_CPU])
            entry = {
                "id": system_id,
                "name": display_name or machine,
                "machine": machine,
                "host": None,
                "status": "up" if up else None,
                "cpu": cpu,
                "memory_pct": round(memory_pct, 1) if memory_pct is not None else None,
                "disk_pct": (
                    round(_last_value(series[METRIC_DISK_USED_PCT]), 1)
                    if series[METRIC_DISK_USED_PCT]
                    else None
                ),
                "load_avg": [
                    _last_value(series[METRIC_LOAD_AVG_1M]),
                    _last_value(series[METRIC_LOAD_AVG_5M]),
                    _last_value(series[METRIC_LOAD_AVG_15M]),
                ],
                "container_count": 0,
                "uptime_sec": (
                    int(_last_value(series[METRIC_UPTIME_SEC]))
                    if series[METRIC_UPTIME_SEC]
                    else None
                ),
                "net_bytes": None,
                "temperature": None,
                # 机器卡副行读的绝对值键（Beszel 分支经 _attach_absolute_metrics
                # 挂）：PG 指标流本来就有，直接折算，不因切源丢副行。
                "disk_total_gb": (
                    round(disk_total / GIB, 1) if disk_total is not None else None
                ),
                "disk_used_gb": (
                    round(disk_used / GIB, 1) if disk_used is not None else None
                ),
                "memory_total_mb": (
                    round(mem_total / (1024.0 * 1024.0), 1)
                    if mem_total is not None
                    else None
                ),
                "memory_used_mb": (
                    round(mem_used / (1024.0 * 1024.0), 1)
                    if mem_used is not None
                    else None
                ),
            }
            payload["systems"].append(entry)

            for (container_name, field), points in sorted(
                container_rows.items()
            ):
                key = (machine, container_name)
                row = containers.setdefault(
                    key,
                    {
                        "id": "container:{}:{}".format(machine, container_name),
                        "name": container_name,
                        "system_id": system_id,
                        "system_name": entry["name"],
                        "machine": machine,
                        "image": "",
                        "status": "unknown",
                        "health": "unknown",
                        "health_code": 0,
                        "cpu": None,
                        "memory_mb": None,
                        "net_io": "",
                        "ports": "",
                    },
                )
                if field == "cpu_pct":
                    row["cpu"] = _last_value(points)
                elif field == "memory_used_bytes":
                    value = _last_value(points)
                    row["memory_mb"] = (
                        round(value / (1024.0 * 1024.0), 1)
                        if value is not None
                        else None
                    )
                row["status"] = _container_status(points)

        for key in sorted(containers):
            payload["containers"].append(containers[key])
        by_machine = {}
        for row in payload["containers"]:
            by_machine[row["machine"]] = by_machine.get(row["machine"], 0) + 1
        for entry in payload["systems"]:
            entry["container_count"] = by_machine.get(entry["machine"], 0)
    return payload


def _enrolled_machines_rows(conn):
    rows = conn.execute(
        "SELECT machine_id, display_name, last_seen_at FROM monitoring_machines "
        "WHERE status = 'active' ORDER BY machine_id ASC"
    ).fetchall()
    return [
        (row["machine_id"], row["display_name"], _to_utc(row["last_seen_at"]))
        for row in rows
    ]


def _fetch_container_samples(conn, machine, start, end):
    """窗口内该机的容器指标，{(container_name, field): [(ts, value), ...]}。

    与 ``_fetch_samples`` 同口径排除回填样本。容器指标名是开放集合（随容器
    增减变化），走 LIKE 前缀匹配后内存解析。
    """
    ph = sd.ph()
    not_backfill = " AND ".join(
        ["source <> " + ph] * len(NON_AGENT_SAMPLE_SOURCES)
    )
    rows = conn.execute(
        "SELECT metric_name, sampled_at, metric_value FROM monitoring_samples "
        f"WHERE machine_id = {ph} AND metric_name LIKE {ph} "
        f"AND sampled_at >= {ph} AND sampled_at <= {ph} AND {not_backfill} "
        "ORDER BY sampled_at ASC",
        (
            machine,
            METRIC_CONTAINER_PREFIX + "%",
            start,
            end,
            *NON_AGENT_SAMPLE_SOURCES,
        ),
    ).fetchall()
    series = {}
    for row in rows:
        container_name, field = parse_container_metric(row["metric_name"])
        if container_name is None:
            continue
        series.setdefault((container_name, field), []).append(
            (_to_utc(row["sampled_at"]), float(row["metric_value"]))
        )
    return series


def _snapshot_probe_interval():
    """复用 Console 探测间隔配置，避免前端把缺省 0 误读成每秒刷新。"""
    import probe_cache

    return probe_cache.probe_interval()


# ---- 0b. 可观测性快照（B6-3：/api/observability 的 console_pg 分支）---------


def observability_snapshot(now=None, conn=None):
    """Console PG 版 ``monitor_observability.get_observability_snapshot``。

    键名对齐 Beszel 侧 ``_empty_payload``（前端按同一契约读）：
    ``active_alerts`` ← ``monitor_events`` 活动事件、``recent_events`` ← 近
    50 条（同 ``console_events_snapshot`` 的窗口/上限），其余分块（systemd/
    SMART/machine_links）PG 无数据，保留空结构并用 capabilities 显式标记未覆盖。
    事件行 → 告警行的映射：

    - ``name`` ← ``title``（事件的人类可读名，最接近 Beszel alert name 语义）；
    - ``severity`` 透传（monitor_events 的 error/warn 枚举与 Beszel 投影
      ``_severity`` 的输出枚举一致，无需映射表）；
    - ``threshold``/``duration_min`` 置 None（PG 事件没有阈值语义）；
    - ``triggered_at`` ← ``opened_at``；recent_events 的 opened_at/
      resolved_at/state/duration_sec 两边键名本就一致，直传。
    """
    moment = _to_utc(now) or datetime.datetime.now(datetime.timezone.utc)
    listed = monitor_events.list_events(days=7, state="all", limit=50, conn=conn)
    rows = listed.get("events") or []

    def to_alert(row):
        return {
            "machine": row.get("machine") or "",
            "name": row.get("title") or "",
            "severity": row.get("severity") or "error",
            "threshold": None,
            "duration_min": None,
            "triggered_at": row.get("opened_at"),
        }

    active_alerts = [to_alert(row) for row in rows if row.get("state") == "active"]
    recent_events = [
        {
            "machine": row.get("machine") or "",
            "name": row.get("title") or "",
            "severity": row.get("severity") or "error",
            "threshold": None,
            "opened_at": row.get("opened_at"),
            "resolved_at": row.get("resolved_at"),
            "state": row.get("state") or "active",
            "duration_sec": row.get("duration_sec"),
        }
        for row in rows
    ]
    return {
        "ok": True,
        "updated_at": _iso(moment),
        "probe_interval": _snapshot_probe_interval(),
        "source_errors": [],
        "machine_links": {},
        "active_alerts": active_alerts,
        "recent_events": recent_events,
        "systemd_services": [],
        "smart_devices": [],
        "systemd_unit_names": {},
        "capabilities": {"systemd": "unsupported", "smart": "unsupported"},
        "source": SOURCE_CONSOLE_PG,
    }


# ---- 1. 磁盘预测接回 ---------------------------------------------------------


def project_480m(points, bucket_hours=PROJECTION_BUCKET_HOURS):
    """raw 点 → 480m 投影：8 小时桶、桶内均值、空桶跳过不造点（B1-3 §4.1）。

    ``points`` 是 ``[(ts, value), ...]``（升序或乱序均可），返回同构列表。
    旧 Beszel 480m 档的语义等价物——B5-1 对账时新旧来源都过本函数再比。
    """
    if not points:
        return []
    seconds = bucket_hours * 3600
    buckets = {}
    for ts, value in points:
        if ts is None or value is None:
            continue
        epoch = int(ts.timestamp())
        buckets.setdefault(epoch // seconds, []).append(float(value))
    projected = []
    for key in sorted(buckets):
        values = buckets[key]
        projected.append(
            (
                datetime.datetime.fromtimestamp(
                    key * seconds, tz=datetime.timezone.utc
                ),
                sum(values) / len(values),
            )
        )
    return projected


def _bytes_to_gb(points):
    """[(ts, bytes)] → [(ts, GB)]。GiB 二进制口径（与卡片磁盘/内存口径一致）。"""
    return [(ts, value / GIB) for ts, value in points]


def disk_trends(now=None, conn=None):
    """各 role=server 机器的磁盘趋势（Console PG 版 ``capacity.get_disk_trends``）。

    输出形状对齐 ``capacity``：``{ok, ttl, history_type, min_r_squared, machines,
    source}``；单机样本不足门槛时该机缺席（capacity 的既有语义：不造全 0 假趋势，
    「暂不预测」由样本数/跨度门槛自然表达）。新增 ``source`` 字段供 B5-1 对账。
    """
    moment = _to_utc(now) or datetime.datetime.now(datetime.timezone.utc)
    start = moment - datetime.timedelta(days=ANALYSIS_WINDOW_DAYS)

    machines = mc.load_machines() or {}
    targets = {mid for mid, entry in machines.items() if entry.get("role") == "server"}

    payload = {
        "ok": True,
        "ttl": 3600,
        "history_type": "raw->{}h".format(PROJECTION_BUCKET_HOURS),
        "min_r_squared": None,  # 下面按 capacity 常量填充（阈值随载荷下发，不另抄一份）
        "machines": {},
        # capacity._empty 恒带 error（成功 null）；verify_deploy 核验项 15 按六键
        # 断言，PG 载荷缺它会在切换后被验收拦下（2026-10-25 切换轮实测）。
        "error": None,
        "source": SOURCE_CONSOLE_PG,
        "generated_at": _iso(moment),
    }
    import capacity

    payload["min_r_squared"] = capacity.MIN_R_SQUARED

    if not targets:
        return payload

    with _read_tx(conn=conn) as own:
        for machine in sorted(targets):
            series = _fetch_samples(
                own,
                machine,
                [METRIC_DISK_USED, METRIC_DISK_TOTAL],
                start,
                moment,
            )
            used = project_480m(_bytes_to_gb(series[METRIC_DISK_USED]))
            # 总量与已用共享时间轴：按 480m 桶对齐后拼 (moment, total, used)
            totals = {
                ts: value for ts, value in project_480m(_bytes_to_gb(series[METRIC_DISK_TOTAL]))
            }
            samples = [
                (ts, totals.get(ts), used_gb)
                for ts, used_gb in used
                if totals.get(ts) is not None
            ]
            trend = capacity.build_trend(samples)
            if trend:
                payload["machines"][machine] = trend
    return payload


# ---- 2. 流量账本接回 ---------------------------------------------------------


def _pick_pg_nic(conn, machine, month_start):
    """挑计费网卡：台账 traffic_nic 显式优先，否则取窗口内累计量最大的可计费网卡。

    挑选规则与 ``node_traffic.pick_nic`` 同纪律，但作用在 PG 样本上——per-NIC
    metric 按 ``net_*_bytes@<nic>`` 存，先查有哪些再挑。与 ``_fetch_samples``
    同口径只认 agent 实时样本：回填点是旧 Beszel 计数器的搬运，累计量与 agent
    计数器不同基线，混进峰值比较会选错网卡。
    """
    ph = sd.ph()
    not_backfill = " AND ".join(
        ["source <> " + ph] * len(NON_AGENT_SAMPLE_SOURCES)
    )
    rows = conn.execute(
        "SELECT metric_name, MAX(metric_value) AS peak FROM monitoring_samples "
        f"WHERE machine_id = {ph} AND metric_name LIKE {ph} "
        f"AND sampled_at >= {ph} AND {not_backfill} GROUP BY metric_name",
        (machine, nic_metric(METRIC_NET_SENT, "%"), month_start, *NON_AGENT_SAMPLE_SOURCES),
    ).fetchall()
    candidates = []
    for row in rows:
        base, nic = parse_nic_metric(row["metric_name"])
        if base != METRIC_NET_SENT or not nic or not nt.is_billable_nic(nic):
            continue
        if row["peak"] is None:
            continue
        candidates.append((nic, float(row["peak"])))
    if not candidates:
        return None
    preferred = (mc.get_machine(machine) or {}).get("traffic_nic") or ""
    if preferred:
        for nic, peak in candidates:
            if nic == preferred:
                return nic
    return max(candidates, key=lambda item: item[1])[0]


def traffic_rebuild(machine_id, now=None, conn=None):
    """从 Console PG raw 样本重建单机本月出站账目（影子对账用，不写 machine_traffic）。

    B1-3 §4.2 的口径：UTC 自然月分桶、计数器变小 = 归零、换网卡 = 换基线、只算
    出站。差分规则逐条复用 ``node_traffic.record_sample``（不重写第二份规则），
    差分顺序按 ``sampled_at``（源机器时钟，契约 §3 不可变），乱序补传先排序再算。

    样本口径：只认 agent 实时样本（排除 ``beszel-backfill``）——回填计数器与
    agent 计数器不同基线，混入差分会把旧账算进本月。边界声明：

    - **不是正式账本**：输出带 ``source=console_pg``，只回答「新链路记的账长
      什么样」供对账；正式计量仍由 Beszel 驱动（``record_from_systems``），
      本函数零写入。
    - **month**：UTC 自然月（``now`` 所在月的 1 号 0 点起）；不跨月截断补偿，
      窗口就是本月。
    - **reset**：计数器变小即归零，当前绝对值整体计入（``node_traffic`` 纪律）；
      重启落点在两次采样之间时中间流量不可知，``estimated=False`` 如实表达
      下界近似。
    - **nic**：``_pick_pg_nic`` 按 agent 样本挑（traffic_nic 显式优先，否则
      窗口内累计量最大的可计费网卡），窗口内固定一块；换网卡（换基线）场景
      由差分状态机按归零处理。
    - **缺口**：窗口开头到首个样本之间、末样本到 ``now`` 之间的流量不计入
      （``since`` 与 ``samples`` 字段如实回显覆盖）；采集中断不插值。
    """
    machine = str(machine_id or "").strip().lower()
    if not machine:
        return None
    moment = _to_utc(now) or datetime.datetime.now(datetime.timezone.utc)
    month_start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    with _read_tx(conn=conn) as own:
        nic = _pick_pg_nic(own, machine, month_start)
        result = {
            "machine": machine,
            "month": month_start.strftime("%Y-%m"),
            "nic": nic or "",
            "sent_bytes": 0,
            "recv_bytes": 0,
            "resets": 0,
            "since": None,
            "samples": 0,
            # 机器重启落在两次采样之间时，归零那跳的实际流量不可知——影子账是
            # 对账口径的下界近似，不是无损计费（总计划 B5 验收原文）。
            "estimated": False,
            "source": SOURCE_CONSOLE_PG,
            "generated_at": _iso(moment),
        }
        if not nic:
            return result

        series = _fetch_samples(
            own,
            machine,
            [nic_metric(METRIC_NET_SENT, nic), nic_metric(METRIC_NET_RECV, nic)],
            month_start,
            moment,
        )
        sent_points = series[nic_metric(METRIC_NET_SENT, nic)]
        recv_points = series[nic_metric(METRIC_NET_RECV, nic)]
        recv_by_ts = dict(recv_points)
        result["since"] = _iso(sent_points[0][0]) if sent_points else None
        result["samples"] = len(sent_points)

        # 差分状态机与 node_traffic.record_sample 同规则，但在内存回放——
        # 影子路径不写 machine_traffic（正式账本仍由 Beszel 快照驱动）。
        ledger = _LedgerReplay(month=result["month"])
        for ts, sent_total in sent_points:
            recv_total = recv_by_ts.get(ts, 0)
            ledger.step(ts, sent_total, recv_total)
        result.update(ledger.summary())
        return result


class _LedgerReplay:
    """``node_traffic.record_sample`` 差分规则的内联回放（不写 machine_traffic）。

    规则来源（node_traffic.py docstring 三条纪律）：月份翻页不适用（窗口就是本月）、
    计数器变小 = 归零（当前绝对值整体计入）、换网卡不适用（窗口内固定一块）。
    """

    def __init__(self, month):
        self.month = month
        self.sent_bytes = 0
        self.recv_bytes = 0
        self.resets = 0
        self.last_sent_total = None
        self.last_recv_total = None

    def step(self, ts, sent_total, recv_total):
        if self.last_sent_total is None:
            self.last_sent_total, self.last_recv_total = sent_total, recv_total
            return
        if sent_total < self.last_sent_total or recv_total < self.last_recv_total:
            # 计数器归零：当前绝对值就是重启以来的增量，全部计入
            self.sent_bytes += sent_total
            self.recv_bytes += recv_total
            self.resets += 1
        else:
            self.sent_bytes += sent_total - self.last_sent_total
            self.recv_bytes += recv_total - self.last_recv_total
        self.last_sent_total, self.last_recv_total = sent_total, recv_total

    def summary(self):
        return {
            "sent_bytes": int(self.sent_bytes),
            "recv_bytes": int(self.recv_bytes),
            "resets": self.resets,
        }


# ---- 2b. 记账写入方（B6-3/Q3 顺延）--------------------------------------------


def record_traffic_from_pg(now=None, conn=None):
    """flag=console_pg 时的流量账本记账写入方（``record_from_systems`` 的等价物）。

    与 ``record_from_systems`` 的关系：输入从「Beszel 快照 systems 四键」换成
    「monitoring_samples 的 per-NIC counter 样本」，写入原语同一个是
    ``node_traffic.record_sample``（月份翻页/计数器归零/换网卡/advisory lock/
    拒时间倒退全在原语里收敛，本函数零差分逻辑）。``now`` 用样本的
    ``sampled_at``（源机器时钟，与 ``net_sampled_at`` 语义一致），不用当前时刻。

    机器范围与 ``record_from_systems`` 同口径：**台账里有的才记**——遍历
    ``monitoring_machines`` active 机器，不在 machine_config 台账的跳过。

    nic 挑选与批次对齐：

    - ``_pick_pg_nic``（month_start = now 的 UTC 月 1 号；traffic_nic 显式优先，
      否则窗口内累计量最大的可计费网卡；只认 agent 实时样本）；无卡跳过该机。
    - sent/recv 两个 counter 必须取**同一 sampled_at 批次**：agent 一轮上报里
      两 metric 落同一时间戳，但个别轮次可能只有一侧（缺样本/部分失败），跨批次
      拼数会把不可比的两个计数器当成同批差分。实现是一次查询把窗口内两 metric
      的样本拉回来，按 sampled_at 对齐后取**两边都有值的最大时刻**；窗口内找不到
      共同批次就不记该机（宁可少记一轮，不造错账）。

    事务纪律：读（挑卡 + 拉样本）走只读事务；写入**不**把读事务包进去——每机
    ``record_sample(conn=None)`` 自开 write_tx + 每机器 advisory lock（原语冻结
    语义），避免读写事务嵌套。单机失败不影响其余机器（``record_from_systems``
    同纪律）；``conn`` 非空（共用调用方事务）时例外——PG 语句失败会中止整个
    事务，吞掉只会让后续机器静默全失败，此时直接 raise。

    返回 recorded 行列表（形状对齐 ``record_from_systems``：每机一行
    machine_traffic 字典，没记的机器不在列表里）。
    """
    moment = _to_utc(now) or datetime.datetime.now(datetime.timezone.utc)
    month_start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    with _read_tx(conn=conn) as own:
        machines = _enrolled_machines_rows(own)

    recorded = []
    for machine, _display, _last_seen in machines:
        if not mc.get_machine(machine):
            continue
        try:
            with _read_tx(conn=conn) as own:
                nic = _pick_pg_nic(own, machine, month_start)
                if not nic:
                    continue
                batch = _latest_paired_batch(
                    own, machine, nic, month_start, moment
                )
            if batch is None:
                continue
            sampled_at, sent_total, recv_total = batch
            row = nt.record_sample(
                machine,
                nic,
                sent_total,
                recv_total,
                now=sampled_at,
            )
        except Exception:  # noqa: BLE001 —— 单机失败不拖垮整轮记账
            if conn is not None:
                # 共用调用方事务时不能吞：PG 里语句失败会中止整个事务，
                # 吞掉异常只会让后续机器在坏事务上静默全失败。
                raise
            continue
        if row:
            recorded.append(row)
    return recorded


def _latest_paired_batch(conn, machine, nic, start, end):
    """两 counter 同批次的最新样本：``(sampled_at, sent_total, recv_total)``。

    ``_fetch_samples`` 同口径只认 agent 实时样本。一次查询拉两个 metric 的窗口
    样本，在内存里按 ``sampled_at`` 对齐：取**两边都有值的最大时刻**；任一侧
    缺席的时刻直接丢弃（不跨批次拼数，见 ``record_traffic_from_pg`` docstring）。
    窗口内没有共同批次返回 None。
    """
    series = _fetch_samples(
        conn,
        machine,
        [nic_metric(METRIC_NET_SENT, nic), nic_metric(METRIC_NET_RECV, nic)],
        start,
        end,
    )
    sent_by_ts = dict(series[nic_metric(METRIC_NET_SENT, nic)])
    paired = [
        (ts, sent_by_ts[ts], recv_total)
        for ts, recv_total in series[nic_metric(METRIC_NET_RECV, nic)]
        if ts in sent_by_ts
    ]
    if not paired:
        return None
    return max(paired, key=lambda item: item[0])


# ---- 3. 事件接回 ------------------------------------------------------------


GAP_EVENT_SOURCE = "monitoring:ingest-gap"


def ingest_gap_events(now=None, conn=None, gap_minutes=INGEST_GAP_MINUTES):
    """采集中断（dead man's switch 前哨）：断流超阈值开事件，恢复幂等关闭。

    只看 ``monitoring_machines.last_seen_at``（成功写入才刷新，insert_samples
    既有语义）：距 ``now`` 超过阈值的机器开 ``monitoring:ingest-gap:<machine>``
    事件；重新恢复在线的机器由 observe(ok=True) 关闭活动事件。enrolled 但从未
    上报的机器不造事件——那是「装了没跑」，归部署排障，不是数据中断。
    kind=machine：K3 折叠的根语义就是「这台机器的事」，同机服务事件挂它下面。
    样本口径：本函数不做 source 过滤——它是机器活性的二值判断，且回填写入走
    ``touch=False`` 本就刷不了 ``last_seen_at``；events 写入仍走 monitor_events
    自有表（与 Beszel 无关）。
    """
    moment = _to_utc(now) or datetime.datetime.now(datetime.timezone.utc)
    threshold = datetime.timedelta(minutes=gap_minutes)
    observed = []
    with _read_tx(conn=conn) as own:
        rows = own.execute(
            "SELECT machine_id, display_name, last_seen_at FROM monitoring_machines "
            "WHERE status = 'active' ORDER BY machine_id ASC"
        ).fetchall()

    for row in rows:
        machine = str(row["machine_id"] or "").strip().lower()
        last_seen = _to_utc(row["last_seen_at"])
        if not machine or last_seen is None:
            continue
        silent_for = moment - last_seen
        down = silent_for > threshold
        label = str(row["display_name"] or "").strip() or machine.upper()
        title = "{} 采集断流 {} 分钟".format(
            label, int(silent_for.total_seconds() // 60)
        ) if down else "{} 采集恢复".format(label)
        try:
            outcome = monitor_events.observe(
                monitor_events.KIND_MACHINE,
                machine,
                not down,  # ok=True 关闭活动事件
                source=GAP_EVENT_SOURCE,
                machine=machine,
                title=title,
                detail="last_seen={} silent={}s".format(
                    _iso(last_seen), int(silent_for.total_seconds())
                ),
                severity="warn",
                ts=_iso(moment),
            )
        except Exception:  # noqa: BLE001 —— 断流检查不该被事件写入失败拖死
            outcome = None
        observed.append(
            {
                "machine": machine,
                "last_seen_at": _iso(last_seen),
                "silent_sec": int(silent_for.total_seconds()),
                "down": down,
                "observe": outcome,
            }
        )
    return observed


# ---- 4. 缺口表达（B1-3 §3）---------------------------------------------------


def gap_summary(machine_id, metric, now=None, conn=None):
    """单机单指标的缺口摘要：历史起点、可查最早点、样本是否足够。

    ``insufficient`` 按 capacity 门槛（样本数 < MIN_SAMPLES 或跨度 < MIN_SPAN_HOURS）
    判——这是「样本不足」的明确表达（B5 验收原文），不是缺口补 0。B5-2 回填
    落地前，``earliest_queryable`` 之前的区间就是 B1-3 §3.1 的 ``archived``
    缺口（存在于 Beszel，未回填进 PG）。与 disk_trends/traffic_rebuild 刻意
    **不做 source 过滤**：回填点就是用来填 ``archived`` 缺口的，历史起点/
    样本数必须把回填算进去（module docstring「source 分类」一节）。
    """
    import capacity

    machine = str(machine_id or "").strip().lower()
    metric = str(metric or "").strip()
    moment = _to_utc(now) or datetime.datetime.now(datetime.timezone.utc)
    raw_cutoff = moment - datetime.timedelta(days=mi.RAW_RETENTION_DAYS)

    summary = {
        "machine": machine,
        "metric": metric,
        "first_at": None,
        "last_at": None,
        "samples": 0,
        "history_start": None,
        "earliest_queryable": _iso(raw_cutoff),
        "insufficient": True,
        "reason": "no_samples",
        "source": SOURCE_CONSOLE_PG,
    }
    with _read_tx(conn=conn) as own:
        first_at, last_at, samples = _series_bounds(own, machine, metric)
    if first_at is not None:
        span_hours = (last_at - first_at).total_seconds() / 3600.0
        summary.update(
            {
                "first_at": _iso(first_at),
                "last_at": _iso(last_at),
                "samples": samples,
                "history_start": _iso(first_at),
                "insufficient": (
                    samples < capacity.MIN_SAMPLES
                    or span_hours < capacity.MIN_SPAN_HOURS
                ),
                "reason": "ok",
            }
        )
    return summary


def shadow_snapshot(now=None, conn=None):
    """三个业务消费方的一站式影子快照（B5-1 比对与 B5-5 收口的输入）。

    只读；任何一个分块失败都不拖垮其余分块（对齐 monitor_observability 的
    source_errors 纪律：失败进 ``errors``，不伪装成空数据）。
    """
    errors = []
    snapshot = {"source": SOURCE_CONSOLE_PG, "generated_at": None, "errors": errors}
    moment = _to_utc(now) or datetime.datetime.now(datetime.timezone.utc)
    snapshot["generated_at"] = _iso(moment)

    for name, fn in (
        ("disk_trends", lambda: disk_trends(now=moment, conn=conn)),
        ("traffic", lambda: {
            machine: traffic_rebuild(machine, now=moment, conn=conn)
            for machine in sorted((mc.load_machines() or {}).keys())
        }),
        ("ingest_gaps", lambda: ingest_gap_events(now=moment, conn=conn)),
    ):
        try:
            snapshot[name] = fn()
        except Exception as exc:  # noqa: BLE001 —— 快照分块失败不拖垮整体
            errors.append("{}: {}".format(name, type(exc).__name__))
            snapshot[name] = None
    return snapshot
