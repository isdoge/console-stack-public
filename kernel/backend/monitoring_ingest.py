# -*- coding: utf-8 -*-
"""监控样本的 PG 接收与存储（B4-2；B1 契约 §3/§4.3、B1-3 §2.2/§5 的存储侧落点）。

三张表（迁移 16 + 迁移 17 的 hourly source 维度）：

====================== ========================================================
表                     语义
====================== ========================================================
``monitoring_machines`` 机器身份。enrol 生成 256-bit secret，库里只存 SHA-256
                       哈希，明文仅 enrol 返回一次；``status='revoked'`` 后
                       拒写（吊销不删历史样本）。
``monitoring_samples`` raw 5min 样本，保留 30 天。UNIQUE(machine_id,
                       metric_name, sampled_at) 物理去重——重复上报/缓冲补传
                       不重复记样，冲突时只刷新 received_at；``sampled_at`` 是
                       源机器时钟，落库后不可变（契约 §3 规则 2）。
``monitoring_hourly``  1h 聚合，保留 1 年。``aggregate_hourly()`` 只聚合已完结
                       小时（进行中的小时不是缺口，是尚未聚合——B1-3 §2.2）；
                       重复执行对同一桶 ON CONFLICT DO UPDATE 重算，乱序补传的
                       旧样本下一轮自动并入（自愈，对应 B1-3 §5.5 后到点）。
                       ``source`` 列（迁移 17）是稳定逻辑来源：
                       ``console-agent`` / ``beszel-backfill`` / ``legacy-mixed``，
                       聚合按来源分组，不同来源不混桶（B6-3）。
====================== ========================================================

档位与缺口：``get_history`` 只有两档，auto 按 ≤24h→raw / >24h→hourly（B1-3
§2.2 冻结，显式 resolution 可越过自动档但存储层不拦保留边界——越界子区间由
查询层标 ``retention_exceeded``）。本模块只返回真实观测点：不做缺口推断、不
补 0、不插值——gaps 的判定与序列化归查询 API 层（B1-3 §3）。

**hourly 的 source 维度（B6-3 迁移 17）**：``monitoring_hourly.source`` 只存
两个逻辑来源——``console-agent``（自有 agent 全部版本，由 :data:`AGENT_SOURCE`
折叠，不按版本无限加序列）与 ``beszel-backfill``（B5-2 回填）；迁移前遗留的
混合桶标 ``legacy-mixed``。``aggregate_hourly`` 按来源分组，raw 侧未知 source
（既非 agent 版本格式、也非回填标记）聚合为 ``legacy-mixed``——不静默冒充
agent。``get_history`` 的 hourly 档默认只读 ``console-agent``；要回填或混合
视角必须显式传 ``sources=``，返回点带 ``source`` 回显。

仅 PG：B4 数据链路没有 SQLite 路径（unnest / percentile_cont / make_interval
都是 PG 专有语法），非 postgres 引擎下调用直接拒绝，不静默降级（与 D3 同款
纪律：宁失败不伪装）。
"""
import datetime
import hashlib
import hmac
import math
import re
import secrets

import storage_dispatch as sd

# B0 冻结的保留档：raw 30 天、hourly 1 年。
RAW_RETENTION_DAYS = 30
HOURLY_RETENTION_DAYS = 365

# ---- hourly 的逻辑来源（B6-3 迁移 17） ---------------------------------------
# raw 侧 source 记 agent 版本（x.y.z）或回填标记；hourly 只保留两个稳定逻辑来源，
# 不随 agent 版本无限增加序列。AGENT_VERSION_RE 与 monitoring_api 的接收校验同式。
AGENT_SOURCE = "console-agent"
LEGACY_SOURCE = "legacy-mixed"
HOURLY_SOURCES = (AGENT_SOURCE, "beszel-backfill", LEGACY_SOURCE)
# get_history hourly 档的默认读取范围：自有 agent 的正式计量。要看回填/混合视角
# 必须显式传 sources——默认不含 legacy-mixed，避免旧混合桶污染新序列。
DEFAULT_HISTORY_SOURCES = (AGENT_SOURCE,)
_AGENT_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

RESOLUTIONS = ("auto", "raw", "hourly")
# B1-3 §2.2 自动档位边界：跨度 ≤ 24h → raw。
AUTO_RAW_WINDOW = datetime.timedelta(hours=24)

STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class MonitoringIngestError(RuntimeError):
    """monitoring_ingest 的基错误；B4-3 据子类映射 401/403/409。"""


class UnknownMachineError(MonitoringIngestError):
    """machine_id 未登记。B1 契约 §2.1 规则 2：未登记身份不得写入。"""


class RevokedMachineError(MonitoringIngestError):
    """机器已吊销；吊销后拒写（历史样本保留）。"""


class AlreadyEnrolledError(MonitoringIngestError):
    """重复 enrol。secret 仅首次返回，轮换走独立的密钥轮换流程（B0 冻结）。"""


def _require_pg():
    if not sd.use_postgres():
        raise MonitoringIngestError(
            "monitoring_ingest 仅支持 PostgreSQL（B4 数据链路无 SQLite 路径）"
        )


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(moment):
    return moment.strftime(TS_FORMAT)


def _to_utc_datetime(value):
    """归一化时间输入：datetime 或 ISO-8601 串 → tz-aware UTC。

    naive 一律按 UTC 解释（契约只认 ISO Z）；Python 3.9 的 fromisoformat
    不认 ``Z`` 后缀，先换写成 ``+00:00``。Go time.Time JSON 序列化走
    RFC3339Nano（纳秒），Python 3.10 fromisoformat 不支持超过 6 位小数秒，
    先截断到微秒再解析。
    """
    if isinstance(value, datetime.datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        text = _truncate_fraction(text)
        try:
            moment = datetime.datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"无法解析时间戳: {value!r}") from exc
    else:
        raise ValueError(f"时间戳必须是 datetime 或 ISO-8601 字符串: {value!r}")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc)


def _truncate_fraction(text):
    """ISO 8601 小数秒截断到微秒（fromisoformat 上限）；无小数秒原样返回。"""
    m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})\.(\d+)([+-].*|)$", text)
    if not m:
        return text
    head, frac, tail = m.groups()
    frac = frac[:6].ljust(6, "0")
    return head + "." + frac + tail


def _hash_secret(secret):
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _normalize_machine_id(machine_id):
    return str(machine_id or "").strip().lower()


# ---- 机器身份 ---------------------------------------------------------------


def enrol_machine(machine_id, display_name, public_key=None, capabilities=None,
                  conn=None):
    """登记机器并返回一次性明文 secret（256-bit）。

    库里只存 SHA-256 哈希；明文离开本函数即不可再取回（这是「仅首次」的物理
    保证，不是约定）。重复 enrol 抛 ``AlreadyEnrolledError``——secret 轮换是
    独立流程（B0 冻结密钥轮换），不借 enrol 走。
    """
    _require_pg()
    machine = _normalize_machine_id(machine_id)
    if not machine:
        raise ValueError("machine_id 不能为空")
    name = str(display_name or "").strip() or machine
    secret = secrets.token_urlsafe(32)
    with sd.write_tx(conn=conn) as tx:
        cursor = tx.execute(
            "INSERT INTO monitoring_machines "
            "       (machine_id, display_name, public_key, secret_hash, capabilities) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (machine_id) DO NOTHING",
            (machine, name, public_key, _hash_secret(secret), capabilities),
        )
        if (cursor.rowcount or 0) == 0:
            raise AlreadyEnrolledError(
                f"机器 {machine} 已登记；secret 仅 enrol 时返回一次"
            )
    return secret


def revoke_machine(machine_id, conn=None):
    """吊销机器（active → revoked）。返回是否发生本次转移。

    已吊销或不存在都返回 False；吊销只挡后续写入，历史样本不动。
    """
    _require_pg()
    machine = _normalize_machine_id(machine_id)
    with sd.write_tx(conn=conn) as tx:
        cursor = tx.execute(
            "UPDATE monitoring_machines SET status = %s "
            "WHERE machine_id = %s AND status <> %s",
            (STATUS_REVOKED, machine, STATUS_REVOKED),
        )
        return (cursor.rowcount or 0) > 0


def _row_to_machine(row):
    return {
        "machine_id": row["machine_id"],
        "display_name": row["display_name"],
        "public_key": row["public_key"],
        "secret_hash": row["secret_hash"],
        "enrolled_at": row["enrolled_at"],
        "last_seen_at": row["last_seen_at"],
        "status": row["status"],
        "agent_version": row["agent_version"],
        "capabilities": row["capabilities"],
    }


def get_machine_by_id(machine_id, conn=None):
    """按台账 id 取机器记录；不存在返回 None。"""
    _require_pg()
    machine = _normalize_machine_id(machine_id)
    with sd.read_tx(conn=conn) as tx:
        row = tx.execute(
            "SELECT * FROM monitoring_machines WHERE machine_id = %s", (machine,)
        ).fetchone()
    return _row_to_machine(row) if row else None


def get_machine_by_secret(secret_hash, conn=None):
    """按 secret 哈希取机器记录（Bearer Token 直接定位机器的路径）；无则 None。"""
    _require_pg()
    with sd.read_tx(conn=conn) as tx:
        row = tx.execute(
            "SELECT * FROM monitoring_machines WHERE secret_hash = %s",
            (str(secret_hash or "").strip(),),
        ).fetchone()
    return _row_to_machine(row) if row else None


def verify_machine(machine_id, secret, conn=None):
    """验证 machine_id + secret：哈希恒时比对 + status 必须为 active。

    任一不匹配返回 False（不区分「不存在」与「密钥错」，不给探测方信息）。
    只读，不刷新 last_seen_at——「被看到」以成功写入为准（insert_samples）。
    """
    _require_pg()
    machine = _normalize_machine_id(machine_id)
    if not machine or not secret:
        return False
    with sd.read_tx(conn=conn) as tx:
        row = tx.execute(
            "SELECT secret_hash, status FROM monitoring_machines WHERE machine_id = %s",
            (machine,),
        ).fetchone()
    if row is None or row["status"] != STATUS_ACTIVE:
        return False
    return hmac.compare_digest(row["secret_hash"], _hash_secret(secret))


# ---- 样本写入 ---------------------------------------------------------------


def insert_samples(machine_id, samples, source, conn=None, touch=True):
    """批量写入 raw 样本；返回 ``{"inserted": n, "duplicates": m}``。

    幂等：``(machine_id, metric_name, sampled_at)`` 冲突时不重复记样、不改
    metric_value（先到的值不被动），只把 received_at 刷新为本次到达时刻——
    重复上报和旧样本补传都不产生新行（B0/B1 冻结语义）。

    ``samples`` 每项是 ``{"metric_name", "metric_value", "sampled_at"}``；
    sampled_at 接受 datetime 或 ISO-8601 串，一律归一到 UTC。同一批里同键
    重复行先在内存里合并（首到优先）——PG 的 ON CONFLICT 不允许一条语句
    影响同一行两次，缓冲重试的重叠批次在这里先塌缩。

    写入前置检查机器身份：未登记抛 ``UnknownMachineError``，已吊销抛
    ``RevokedMachineError``（B4-3 的认证是第一道门，这里是存储层的第二道）。
    成功写入顺带刷新 ``last_seen_at`` 与 ``agent_version``（source 即采集端
    版本）；``touch=False`` 跳过刷新——给历史回填（monitoring_backfill）用：
    回填不伪造「机器在线」，新鲜度与 dead man's switch 只认真实采集。
    """
    _require_pg()
    machine = _normalize_machine_id(machine_id)
    if not machine:
        raise ValueError("machine_id 不能为空")
    source = str(source or "").strip()
    if not source:
        raise ValueError("source（采集端版本）不能为空")

    batch = list(samples or ())
    deduped = {}
    for sample in batch:
        metric_name = str(sample["metric_name"] or "").strip()
        if not metric_name:
            raise ValueError("metric_name 不能为空")
        value = float(sample["metric_value"])
        if not math.isfinite(value):
            # NaN/Inf 会毒化 avg/p95 聚合且永远清不干净，入口直接拒。
            raise ValueError(f"metric_value 非有限数值: {sample['metric_value']!r}")
        moment = _to_utc_datetime(sample["sampled_at"])
        key = (metric_name, moment)
        if key not in deduped:
            deduped[key] = value

    with sd.write_tx(conn=conn) as tx:
        row = tx.execute(
            "SELECT status FROM monitoring_machines WHERE machine_id = %s",
            (machine,),
        ).fetchone()
        if row is None:
            raise UnknownMachineError(f"机器 {machine} 未登记，拒绝写入")
        if row["status"] != STATUS_ACTIVE:
            raise RevokedMachineError(f"机器 {machine} 已吊销，拒绝写入")

        inserted = 0
        if deduped:
            names = [key[0] for key in deduped]
            values = [value for value in deduped.values()]
            moments = [key[1] for key in deduped]
            # 计数不能靠 xmax=0（unnest 批量路径上 DO UPDATE 的行也报 0）。
            # 同一事务里先查已存在键：竞态窗口内的新冲突由 ON CONFLICT DO
            # UPDATE 兜底不报错，计数可能偶差一行——计数仅用于遥测，行数
            # 与值的正确性由唯一约束物理保证。
            existing = {
                (row["metric_name"], row["sampled_at"])
                for row in tx.execute(
                    "SELECT metric_name, sampled_at FROM monitoring_samples "
                    "WHERE machine_id = %s AND sampled_at = ANY(%s)",
                    (machine, moments),
                ).fetchall()
                if row["metric_name"] in set(names)
            }
            inserted = sum(1 for key in deduped if key not in existing)
            tx.execute(
                "INSERT INTO monitoring_samples "
                "       (machine_id, metric_name, metric_value, sampled_at, "
                "        received_at, source) "
                "SELECT %s, metric_name, metric_value, sampled_at, now(), %s "
                "FROM unnest(%s::text[], %s::float8[], %s::timestamptz[]) "
                "     AS input(metric_name, metric_value, sampled_at) "
                "ON CONFLICT (machine_id, metric_name, sampled_at) "
                "DO UPDATE SET received_at = EXCLUDED.received_at",
                (machine, source, names, values, moments),
            )
        if touch:
            tx.execute(
                "UPDATE monitoring_machines SET last_seen_at = now(), agent_version = %s "
                "WHERE machine_id = %s",
                (source, machine),
            )

    duplicates = (len(batch) - len(deduped)) + (len(deduped) - inserted)
    return {"inserted": inserted, "duplicates": duplicates}


# ---- 历史读取 ---------------------------------------------------------------


def hourly_source_of(raw_source):
    """raw 侧 source → hourly 逻辑来源（B6-3 迁移 17 的映射规则）。

    - ``beszel-backfill``（B5-2 回填标记）保持原样；
    - 自有 agent 的版本号（x.y.z，monitoring_api 接收校验同式）折叠为
      ``console-agent``——版本会持续增长，hourly 不按版本加序列；
    - 其余（空、未知格式、未来新来源）→ ``legacy-mixed``：未知来源不能静默
      冒充 agent，也不并入回填。
    """
    source = str(raw_source or "").strip()
    if source == "beszel-backfill":
        return source
    if _AGENT_VERSION_RE.match(source):
        return AGENT_SOURCE
    return LEGACY_SOURCE


def get_history(machine_id, metric_name, start, end, resolution="auto",
                sources=None, conn=None):
    """单机单指标的时间序列；返回 ``{"machine_id", "metric_name",
    "resolution", "points", "sources"}``，points 按时间升序、只含真实观测。

    档位（B1-3 §2.2）：``auto`` 跨度 ≤24h 用 raw、>24h 用 hourly；显式
    ``raw``/``hourly`` 可越过自动档。hourly 点的 ``value`` 是 avg，另带
    max/min/p95/sample_count（利用率/速率类的聚合内容，B1-3 §5.3）。

    **source 语义（B6-3）**：raw 档按 raw source 精确过滤（默认不过滤，全量
    观测），``sources=`` 限定集合，返回点带 ``source`` 回显；hourly 档按逻辑
    来源过滤，**默认只读 ``console-agent``**（DEFAULT_HISTORY_SOURCES）——
    要回填/混合视角必须显式传 ``sources``（如 ``("beszel-backfill",)`` 或
    ``HOURLY_SOURCES`` 全集）。默认只读 agent 是行为变化：迁移 17 前 hourly
    是无差别混合桶，之后默认序列不含回填与 legacy-mixed 桶。

    本函数不做缺口判定、不补点——那是查询 API 层（B1-3 §3）的职责；这里
    返回的点之外就是「没有」，消费方自行表达。
    """
    _require_pg()
    machine = _normalize_machine_id(machine_id)
    metric = str(metric_name or "").strip()
    if not machine or not metric:
        raise ValueError("machine_id 与 metric_name 不能为空")
    if resolution not in RESOLUTIONS:
        raise ValueError(f"resolution 必须是 {RESOLUTIONS} 之一: {resolution!r}")
    start_ts = _to_utc_datetime(start)
    end_ts = _to_utc_datetime(end)
    if start_ts > end_ts:
        raise ValueError("start 必须不晚于 end")
    if resolution == "auto":
        resolution = "raw" if (end_ts - start_ts) <= AUTO_RAW_WINDOW else "hourly"

    if sources is not None:
        wanted = tuple(dict.fromkeys(str(item).strip() for item in sources if str(item).strip()))
        if not wanted:
            raise ValueError("sources 非空时必须至少含一个非空来源")
    elif resolution == "hourly":
        wanted = DEFAULT_HISTORY_SOURCES
    else:
        wanted = None

    with sd.read_tx(conn=conn) as tx:
        if resolution == "raw":
            if wanted is None:
                rows = tx.execute(
                    "SELECT sampled_at AS ts, metric_value AS value, source "
                    "FROM monitoring_samples "
                    "WHERE machine_id = %s AND metric_name = %s "
                    "AND sampled_at >= %s AND sampled_at <= %s "
                    "ORDER BY sampled_at ASC",
                    (machine, metric, start_ts, end_ts),
                ).fetchall()
            else:
                rows = tx.execute(
                    "SELECT sampled_at AS ts, metric_value AS value, source "
                    "FROM monitoring_samples "
                    "WHERE machine_id = %s AND metric_name = %s "
                    "AND sampled_at >= %s AND sampled_at <= %s "
                    "AND source = ANY(%s) "
                    "ORDER BY sampled_at ASC",
                    (machine, metric, start_ts, end_ts, list(wanted)),
                ).fetchall()
            points = [
                {"ts": _iso(item["ts"]), "value": float(item["value"]),
                 "source": item["source"]}
                for item in rows
            ]
        else:
            # hour 桶下界对齐到整点，让部分落窗的小时也被读到。
            start_hour = start_ts.replace(minute=0, second=0, microsecond=0)
            rows = tx.execute(
                "SELECT hour_bucket, avg_value, max_value, min_value, p95_value, "
                "       sample_count, source FROM monitoring_hourly "
                "WHERE machine_id = %s AND metric_name = %s "
                "AND hour_bucket >= %s AND hour_bucket <= %s "
                "AND source = ANY(%s) "
                "ORDER BY hour_bucket ASC",
                (machine, metric, start_hour, end_ts, list(wanted)),
            ).fetchall()
            points = [
                {
                    "ts": _iso(item["hour_bucket"]),
                    "value": float(item["avg_value"]),
                    "avg": float(item["avg_value"]),
                    "max": float(item["max_value"]),
                    "min": float(item["min_value"]),
                    "p95": float(item["p95_value"]),
                    "sample_count": int(item["sample_count"]),
                    "source": item["source"],
                }
                for item in rows
            ]

    return {
        "machine_id": machine,
        "metric_name": metric,
        "resolution": resolution,
        "points": points,
        "sources": list(wanted) if wanted is not None else None,
    }


# ---- 聚合与保留清理 ---------------------------------------------------------

# 小时桶按 epoch 取整：不依赖会话时区，任何 offset 下都是同一批 UTC 整点桶。
_HOUR_BUCKET = "to_timestamp(floor(extract(epoch FROM sampled_at) / 3600.0) * 3600)"

_AGGREGATE_SQL = f"""
INSERT INTO monitoring_hourly
    (machine_id, metric_name, hour_bucket, source,
     avg_value, max_value, min_value, p95_value, sample_count)
SELECT machine_id, metric_name, hour_bucket, source,
       avg(metric_value), max(metric_value), min(metric_value),
       percentile_cont(0.95) WITHIN GROUP (ORDER BY metric_value),
       count(*)::integer
FROM (
    SELECT machine_id, metric_name, metric_value,
           {_HOUR_BUCKET} AS hour_bucket,
           CASE
               WHEN source = 'beszel-backfill' THEN 'beszel-backfill'
               WHEN source ~ '^\\d+\\.\\d+\\.\\d+$' THEN '{AGENT_SOURCE}'
               ELSE '{LEGACY_SOURCE}'
           END AS source
    FROM monitoring_samples
    WHERE sampled_at < to_timestamp(floor(extract(epoch FROM now()) / 3600.0) * 3600)
) completed
GROUP BY machine_id, metric_name, hour_bucket, source
ON CONFLICT (machine_id, metric_name, hour_bucket, source) DO UPDATE SET
    avg_value = EXCLUDED.avg_value,
    max_value = EXCLUDED.max_value,
    min_value = EXCLUDED.min_value,
    p95_value = EXCLUDED.p95_value,
    sample_count = EXCLUDED.sample_count
"""


def aggregate_hourly(conn=None):
    """raw → hourly 聚合（每 5 分钟 cron / 后台任务调用）。返回写入的桶数。

    只聚合**已完结小时**（进行中的小时不参与，B1-3 §2.2）。对同一桶
    ON CONFLICT DO UPDATE 全量重算：重复执行幂等，且乱序补传进旧小时的
    样本下一轮自动并入该桶——不需要「哪些桶脏」的簿记。扫描上界天然是
    raw 保留期（30 天），稳态下每次全量重算的代价有界。

    **按 source 分桶（B6-3 迁移 17）**：CASE 折叠与 :func:`hourly_source_of`
    同规则——agent 版本 → ``console-agent``、回填保持 ``beszel-backfill``、
    未知来源 → ``legacy-mixed``。不同来源落不同桶，绝不混进同一小时桶
    （B1-3 §4.1 禁止等权混合）。
    """
    _require_pg()
    with sd.write_tx(conn=conn) as tx:
        cursor = tx.execute(_AGGREGATE_SQL)
        return int(cursor.rowcount or 0)


def cleanup_raw(days=None, conn=None):
    """删除 raw 保留期（默认 30 天）之前的样本；返回删除行数。按
    ``sampled_at`` 判龄：补传的远古样本落地即过期，立即清走。"""
    _require_pg()
    keep = RAW_RETENTION_DAYS if days is None else max(0, int(days))
    if keep == 0:
        return 0
    with sd.write_tx(conn=conn) as tx:
        cursor = tx.execute(
            "DELETE FROM monitoring_samples "
            "WHERE sampled_at < now() - make_interval(days => %s)",
            (keep,),
        )
        return int(cursor.rowcount or 0)


def cleanup_hourly(days=None, conn=None):
    """删除 hourly 保留期（默认 1 年）之前的聚合桶；返回删除行数。"""
    _require_pg()
    keep = HOURLY_RETENTION_DAYS if days is None else max(0, int(days))
    if keep == 0:
        return 0
    with sd.write_tx(conn=conn) as tx:
        cursor = tx.execute(
            "DELETE FROM monitoring_hourly "
            "WHERE hour_bucket < now() - make_interval(days => %s)",
            (keep,),
        )
        return int(cursor.rowcount or 0)
