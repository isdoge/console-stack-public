# -*- coding: utf-8 -*-
"""PG-03 迁移/验证 CLI 的共享内部件（提案 §5.3/§5.4；基线契约 `pg00-1`）。

只允许 PG-03 的三个 CLI 及其测试 import。三条硬纪律：

- **不 import `app`**，不触发任何采集/后台（§8.5）。
- 源只经 `sqlite_legacy.open_legacy_source` 只读打开；目标连接配置只来自环境。
- 金额编码唯一入口是 `money.canonical`；本文件**不**维护第二套 DDL 或第二份金额规则。

verify CLI 与搬运器共享的是「比较语义」（编码/分组），双方各自**重新读取**源与目标；
不存在结果缓存或「搬运已 PASS 所以验证跳过」的路径——这是 §8.2 的独立性要求。
防「共享同一错误」的兜底是测试里手工预先算好的金额样例（§8.5）。
"""
import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal

import money
import pg_locks
import pg_migrations
import sqlite_legacy
from pg_migration_contracts import (
    build_report_manifest,
    key_range_entry,
    validate_report_manifest,
)
from pgdb import PgConfig

CREDENTIAL_FORMAT = "pg-migrate-credential-1"
TOOL_VERSION = "pg03-1"

# 迁移锁：**必须与业务写事务的维护共享锁同 key**（`pg_locks.MAINTENANCE`，key=1）。
#
# PG-05 review A-1（blocker）：原先这里自定义 `MIGRATE_LOCK_KEY = 20`，与业务写事务
# 自动追加的 `MAINTENANCE_SHARED`（key=1）互不相干——advisory lock 按 key 互斥，不同 key
# 各锁各的。实测：A 持 (ns,20) 独占时，B 仍能 0.00s 拿到 (ns,1) 共享锁，即 §4.2 要求的
# 「迁移期间冻结全部业务写者」在 PG 层根本不成立。
#
# 现在迁移路径改取 `pg_locks.MAINTENANCE`（独占，key=1），与维护控制器同一把锁。
MIGRATE_LOCK_KEY = pg_locks.KEY_MAINTENANCE

SAFE_INT_MAX = 2**53 - 1  # JSON number 前端安全整数上界（提案 §4.3）
SOURCE_SCHEMA_VERSIONS = tuple(range(1, 12))  # NAS 快照当前固定 1–11（§2.2）

# 列映射逐一对应 pg_migrations 版本 12 的 DDL（§5.2）；顺序即写入顺序。
TABLE_MAP = {
    "renewals": (
        "id", "name", "kind", "expires_at", "starts_at", "cost", "currency", "cycle",
        "auto_renew", "url", "note", "source", "created_at", "updated_at",
        "sort_order", "provider",
    ),
    "probe_history": ("id", "machine", "ts", "status", "latency_ms"),
    "machine_traffic": (
        "machine", "month", "sent_bytes", "recv_bytes", "last_sent_total",
        "last_recv_total", "nic", "resets", "started_at", "updated_at",
    ),
    "service_health_history": ("id", "name", "ts", "status"),
    "dashboard_layout": ("widget_key", "x", "y", "w", "hidden", "updated_at"),
    "monitor_events": (
        "id", "fingerprint", "kind", "source", "target", "machine", "severity",
        "title", "detail", "opened_at", "updated_at", "resolved_at",
    ),
    "ledger_tx": (
        "id", "dedup", "source", "src_file", "order_no", "time", "date", "month",
        "direction", "amount", "who", "goods", "pay", "zfb_cat", "type1", "type2",
        "basis", "pay_confirmed", "created_at", "card", "status", "note",
    ),
    "ledger_tx_link": ("a", "b", "rel"),
    "ledger_categories": ("id", "level1", "level2", "sort"),
    "ledger_knowledge": ("who", "good", "type1", "type2", "cnt"),
    "ledger_imports": ("source_sha256", "imported_at", "counts_json"),
    "oplog": (
        "id", "host", "svc", "action", "started_at", "finished_at", "result",
        "operator",
    ),
    "versions": (
        "svc_name", "local_version", "upstream_version", "behind_count",
        "update_status", "recent_changes", "last_checked",
    ),
    "panel_scratch": ("panel_id", "content", "updated_at"),
    "bookmarks": (
        "id", "title", "url", "icon", "category", "tags", "sort_order",
        "created_at", "updated_at",
    ),
    "feed_cache": (
        "id", "source", "title", "url", "summary", "icon", "published_at",
        "fetched_at",
    ),
    "schema_migrations": ("version", "applied_at", "description"),
    "panels": ("id", "name", "url", "icon", "enabled", "sort_order"),
}

# 搬运排序键（Python 侧按规范文本排序，绕开 SQLite/PG 文本排序规则差异）。
PK_COLS = {
    "renewals": ("id",),
    "probe_history": ("id",),
    "machine_traffic": ("machine",),
    "service_health_history": ("id",),
    "dashboard_layout": ("widget_key",),
    "monitor_events": ("id",),
    "ledger_tx": ("id",),
    "ledger_tx_link": ("a", "b"),
    "ledger_categories": ("id",),
    "ledger_knowledge": ("who", "good"),
    "ledger_imports": ("source_sha256",),
    "oplog": ("id",),
    "versions": ("svc_name",),
    "panel_scratch": ("panel_id",),
    "bookmarks": ("id",),
    "feed_cache": ("id",),
    "schema_migrations": ("version",),
    "panels": ("id",),
}

AMOUNT_COLUMNS = {"ledger_tx": ("amount",), "renewals": ("cost",)}
# 分组重汇总维度（§5.4 / §10.1 财务项）。
AMOUNT_GROUPS = {
    "ledger_tx": ("month", "direction", "pay", "type1"),
    "renewals": ("currency",),
}
JSON_TEXT_COLUMNS = {"ledger_imports": ("counts_json",), "bookmarks": ("tags",)}
# 前端可见的整数 ID / 关联键（超安全整数即拒绝，提案 §4.3）。
INTEGER_ID_COLUMNS = (
    ("ledger_tx", "id"),
    ("ledger_categories", "id"),
    ("oplog", "id"),
    ("panels", "id"),
    ("panel_scratch", "panel_id"),
    ("probe_history", "id"),
    ("service_health_history", "id"),
    ("monitor_events", "id"),
    ("ledger_tx_link", "a"),
    ("ledger_tx_link", "b"),
)
SOURCE_OPTIONAL_TABLES = {"panels"}  # 允许「源不存在，目标兼容空表」
TARGET_ONLY_TABLES = ("ledger_file_batches",)  # 迁移期间必须为空


class PreflightFailure(RuntimeError):
    """源快照不是可迁移形态；问题明细在 `.issues`。"""

    def __init__(self, issues):
        super().__init__("; ".join(f"{i['kind']}:{i.get('table', '')}" for i in issues[:8]))
        self.issues = list(issues)


class TargetGuardFailure(RuntimeError):
    """目标保护拒绝：非空无凭据、schema 未就绪、身份不符、锁忙。"""


class AnomalousValue(Exception):
    """单元格不是可搬运类型（TEXT 金额、BLOB 等）。"""


def utc_now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --- 规范化编码 ---------------------------------------------------------------


def canonical_cell(value, *, amount=False):
    """SQLite/PG 两侧共用的规范文本编码；`money.canonical` 是金额唯一口径。"""
    if value is None:
        return "N"
    if isinstance(value, bool):
        raise AnomalousValue("boolean")
    if amount:
        if isinstance(value, Decimal):
            return "M" + money.canonical(value)
        if isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                raise AnomalousValue("non-finite float")
            return "M" + money.canonical(money.as_decimal(value))
        raise AnomalousValue(f"amount-as-{type(value).__name__}")
    if isinstance(value, int):
        return f"I{value}"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AnomalousValue("non-finite float")
        return f"F{value!r}"  # Python float repr 是最短可往返表示，两侧一致
    if isinstance(value, str):
        return "S" + value  # Unicode/换行原文保留
    if isinstance(value, (bytes, bytearray)):
        return "B" + bytes(value).hex()
    raise AnomalousValue(type(value).__name__)


def canonicalize_table(table, columns, rows):
    """rows: 与 columns 对齐的元组序列 → ({pk_text: [cell_text...]}, anomalies)。

    异常单元格**不静默跳过**：记入 anomalies 并用占位文本参与键集合，
    让「源含不可搬值」在 preflight 与 verify 里都是显式不一致。
    """
    amount_cols = set(AMOUNT_COLUMNS.get(table, ()))
    pk_index = [columns.index(col) for col in PK_COLS[table]]
    canonical_rows = {}
    anomalies = {}
    for row in rows:
        cells = []
        for column, value in zip(columns, row):
            try:
                cells.append(canonical_cell(value, amount=column in amount_cols))
            except AnomalousValue as exc:
                anomalies[column] = anomalies.get(column, 0) + 1
                cells.append(f"!<anomalous {exc}>")
        pk = "\x1f".join(cells[index] for index in pk_index)
        if pk in canonical_rows:
            raise PreflightFailure([{
                "kind": "duplicate-pk", "table": table, "count": 1,
                "detail": "复合主键在源中出现重复/NULL 组合，拒绝静默修补（提案 §5.2）",
            }])
        canonical_rows[pk] = cells
    return canonical_rows, anomalies


def load_source_table(source_conn, table):
    columns = TABLE_MAP[table]
    column_sql = ", ".join(f'"{column}"' for column in columns)
    rows = source_conn.execute(f'SELECT {column_sql} FROM "{table}"').fetchall()
    return [tuple(row) for row in rows]


def load_target_table(target_conn, table):
    columns = TABLE_MAP[table]
    column_sql = ", ".join(f'"{column}"' for column in columns)
    rows = target_conn.execute(f'SELECT {column_sql} FROM "{table}"').fetchall()
    return [tuple(row[column] for column in columns) for row in rows]


def row_hash(canonical_rows):
    lines = [
        key + "\x1e" + "\x1f".join(canonical_rows[key]) for key in sorted(canonical_rows)
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


# --- 源检查（preflight，§5.3.1） ------------------------------------------------


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wal_issues(source_path):
    """非空 WAL = 快照不是一致的；-wal 存在但 0 字节可接受。"""
    issues = []
    for suffix in ("-wal", "-shm"):
        sidecar = source_path + suffix
        if os.path.exists(sidecar) and suffix == "-wal" and os.path.getsize(sidecar) > 0:
            issues.append({
                "kind": "non-empty-wal", "table": None, "detail":
                    f"{suffix} 非空（{os.path.getsize(sidecar)} 字节）：源不是最终一致性快照，"
                    "先做 SQLite backup API 快照再迁移",
            })
    return issues


def source_issue_sink(issues, kind, table, column=None, count=0, detail=""):
    issues.append({"kind": kind, "table": table, "column": column,
                   "count": count, "detail": detail})


def preflight_source(source_path, expected_sha256=None):
    """返回 (info, issues)。issues 非空即 preflight 失败（退出码 3）。

    info: {sha256, schema_versions, tables:{name:{row_count,columns}},
           sequence_high_water, amount_hash, counts, key_ranges}
    """
    issues = []
    source_path = os.path.abspath(os.path.expanduser(str(source_path)))
    if not os.path.isfile(source_path):
        raise PreflightFailure([{"kind": "source-missing", "table": None,
                                 "detail": f"源不存在: {source_path}"}])
    sha = file_sha256(source_path)
    if expected_sha256 and sha != expected_sha256.strip().lower():
        source_issue_sink(issues, "sha256-mismatch", None,
                          detail=f"实算 {sha[:16]}… 与给定 {expected_sha256.strip().lower()[:16]}… 不符")
    issues.extend(wal_issues(source_path))

    conn = sqlite_legacy.open_legacy_source(source_path)
    try:
        inventory = sqlite_legacy.schema_inventory(conn)
        if inventory["quick_check"] != "ok":
            source_issue_sink(issues, "quick-check", None,
                              detail=f"quick_check={inventory['quick_check']!r}")
        versions = tuple(inventory["schema_versions"])
        if versions != SOURCE_SCHEMA_VERSIONS:
            source_issue_sink(issues, "schema-versions", "schema_migrations",
                              detail=f"源版本 {list(versions)}，期望 {list(SOURCE_SCHEMA_VERSIONS)}")

        source_tables = set(inventory["tables"])
        unknown = source_tables - set(TABLE_MAP)
        if unknown:
            source_issue_sink(issues, "unknown-source-tables", None,
                              detail=f"未知表 {sorted(unknown)}：先更新映射并重新演练（§2.2）")
        for table in TABLE_MAP:
            if table not in source_tables and table not in SOURCE_OPTIONAL_TABLES:
                source_issue_sink(issues, "missing-source-table", table)

        info = {
            "sha256": sha,
            "schema_versions": list(versions),
            "sequence_high_water": inventory["sequence_high_water"],
            "tables": {},
            "absent_optional_tables": sorted(
                table for table in SOURCE_OPTIONAL_TABLES if table not in source_tables
            ),
        }
        totals = {}
        for table in TABLE_MAP:
            if table not in source_tables:
                continue
            columns = TABLE_MAP[table]
            rows = load_source_table(conn, table)
            canonical_rows, anomalies = canonicalize_table(table, columns, rows)
            for column, count in anomalies.items():
                source_issue_sink(issues, "anomalous-value", table, column=column, count=count,
                                  detail="类型异常（TEXT 金额/BLOB/布尔等），拒绝静默转换")
            totals[table] = {
                "source_rows": len(rows),
                "row_hash": row_hash(canonical_rows),
                "key_min": min(canonical_rows) if canonical_rows else None,
                "key_max": max(canonical_rows) if canonical_rows else None,
            }
            _check_json_columns(table, columns, rows, issues)
            _check_amount_values(table, columns, rows, issues)
            _check_safe_integers(table, columns, rows, issues)
            _check_legacy_applied_at(table, columns, rows, issues)
        info["tables"] = totals
        _check_dangling_links(conn, issues)
    finally:
        conn.close()
    return info, issues


def _check_legacy_applied_at(table, columns, rows, issues):
    """G07：源 `schema_migrations.applied_at` 必须是非空文本。

    目标列是 `TEXT NOT NULL DEFAULT <now>`；如果源值为空还照搬，轻则留下空时间、
    重则被目标默认值顶替成迁移时刻——两种都会让 11 条历史时间失真。这里 **fail closed**，
    在 preflight 就报出可读问题（而不是等 PG 在 NOT NULL 处炸出底层错误）。
    """
    if table != "schema_migrations":
        return
    index = columns.index("applied_at")
    bad = [(row[0], row[index]) for row in rows
           if not isinstance(row[index], str) or not row[index].strip()]
    if bad:
        versions = sorted(int(version) for version, _ in bad)
        source_issue_sink(issues, "legacy-applied-at-invalid", table,
                          column="applied_at", count=len(bad),
                          detail=f"版本 {versions} 的 applied_at 非空文本缺失；"
                                 "拒绝让目标默认时间顶替原始时刻（G07）")


def _check_json_columns(table, columns, rows, issues):
    for column in JSON_TEXT_COLUMNS.get(table, ()):
        index = columns.index(column)
        bad = 0
        for row in rows:
            value = row[index]
            if value is None:
                continue
            try:
                json.loads(value)
            except (ValueError, TypeError):
                bad += 1
        if bad:
            source_issue_sink(issues, "json-unparseable", table, column=column,
                              count=bad, detail="JSON 文本列存在不可解析值")


def _check_amount_values(table, columns, rows, issues):
    for column in AMOUNT_COLUMNS.get(table, ()):
        index = columns.index(column)
        bad = 0
        for row in rows:
            value = row[index]
            if value is None:
                continue
            try:
                decimal_value = money.as_decimal(value)
                if isinstance(value, str):
                    raise AnomalousValue("text")  # 历史金额不允许 float/str 中转重解析
                if not decimal_value.is_finite():
                    raise AnomalousValue("non-finite")
                if not money.is_json_safe(decimal_value):
                    raise AnomalousValue("json-roundtrip")
                money.validate_range(decimal_value)
            except (money.MoneyError, AnomalousValue):
                bad += 1
        if bad:
            source_issue_sink(issues, "amount-anomaly", table, column=column, count=bad,
                              detail="NaN/Infinity/TEXT 金额/超范围/JSON 不可往返；"
                                     "preflight 失败并单列报告（§5.4）")


def _check_safe_integers(table, columns, rows, issues):
    wanted = {column for other, column in INTEGER_ID_COLUMNS if other == table}
    for column in wanted & set(columns):
        index = columns.index(column)
        bad = 0
        for row in rows:
            value = row[index]
            if isinstance(value, int) and abs(value) > SAFE_INT_MAX:
                bad += 1
        if bad:
            source_issue_sink(issues, "unsafe-integer-id", table, column=column, count=bad,
                              detail="整数 ID 超出前端安全整数范围，明确拒绝（§4.3）")


def _check_dangling_links(source_conn, issues):
    for side in ("a", "b"):
        row = source_conn.execute(
            f'SELECT COUNT(*) AS c FROM ledger_tx_link l '
            f'WHERE NOT EXISTS (SELECT 1 FROM ledger_tx t WHERE t.id = l."{side}")'
        ).fetchone()
        count = int(row[0])
        if count:
            source_issue_sink(issues, "dangling-link", "ledger_tx_link", column=side,
                              count=count, detail="悬挂关联：链接目标流水不存在")


# --- 目标侧（§5.3.2/§5.3.5/§5.3.6） --------------------------------------------


def target_config(environ=None):
    return PgConfig.from_env(environ)


def connect_target(config=None, *, readonly=False, statement_timeout=600):
    """按 PgConfig 建独立连接（CLI 单进程用，不进 app 池）。脱敏：不打印 conninfo。"""
    import psycopg
    from psycopg.rows import dict_row

    cfg = config or target_config()
    conn = psycopg.connect(
        cfg.conninfo(),
        row_factory=dict_row,
        connect_timeout=cfg.connect_timeout,
        autocommit=not readonly,
        options=f"-c timezone=UTC -c statement_timeout={int(statement_timeout * 1000)}",
    )
    return conn


MAINTENANCE_HELD_ENV = "CONSOLE_CUTOVER_MAINTENANCE_HELD"


def maintenance_lock_holder(conn):
    """谁持有维护锁（脱敏：只回 pid，不回连接细节）。无人持有返回 None。"""
    row = conn.execute(
        "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND objid = %s "
        "AND granted AND mode = 'ExclusiveLock' LIMIT 1",
        (MIGRATE_LOCK_KEY,),
    ).fetchone()
    return None if row is None else row["pid"]


def acquire_migration_lock(conn, timeout_s=10):
    """会话级迁移锁（= 维护独占锁 key=1）。拿不到即拒绝，不排队干等。

    **控制器已持锁时**：cutover 控制器按 §7.3 用自己的连接持同一把锁，再由它调起
    迁移器子进程。此时本函数会和自己人冲突（实测 exit 4），所以控制器通过
    `CONSOLE_CUTOVER_MAINTENANCE_HELD=1` 显式声明「锁已在控制器手里」。该声明**不是
    免检通道**：这里仍会查 `pg_locks` 确认真有一把被持有的独占锁，确认不了就照常拒绝
    ——避免把「有人冻结」降级成「有人声称冻结」。
    """
    import os

    declared = (os.environ.get(MAINTENANCE_HELD_ENV) or "").strip() == "1"
    try:
        conn.execute("SELECT set_config('lock_timeout', %s, false)", (f"{int(timeout_s)}s",))
        if declared:
            holder = maintenance_lock_holder(conn)
            if holder is None:
                raise TargetGuardFailure(
                    f"声明了 {MAINTENANCE_HELD_ENV}=1，但 pg_locks 里没有已持有的维护锁；"
                    "拒绝在未冻结状态下迁移"
                )
            return {"mode": "controller-held", "holder_pid": holder}
        conn.execute(
            "SELECT pg_advisory_lock(%s, %s)", (pg_locks.LOCK_NAMESPACE, MIGRATE_LOCK_KEY)
        )
        return {"mode": "acquired"}
    except TargetGuardFailure:
        raise
    except Exception as exc:  # psycopg lock_timeout / 连接错误
        text = str(exc)
        if "password" in text.lower():
            text = type(exc).__name__
        raise TargetGuardFailure(
            f"迁移锁获取失败（已有其它迁移/维护在跑或权限不足）：{text}"
        ) from exc


def release_migration_lock(conn):
    """只释放**本连接自己**取的锁。

    控制器持锁模式下（`CONSOLE_CUTOVER_MAINTENANCE_HELD=1`）本连接从未取得该锁，
    调用 `pg_advisory_unlock` 会返回 false（或更糟：解锁别人的锁）。这里显式跳过，
    锁的生命周期归控制器。
    """
    import os

    if (os.environ.get(MAINTENANCE_HELD_ENV) or "").strip() == "1":
        return False
    try:
        return bool(conn.execute(
            "SELECT pg_advisory_unlock(%s, %s)",
            (pg_locks.LOCK_NAMESPACE, MIGRATE_LOCK_KEY),
        ).fetchone()["pg_advisory_unlock"])
    except Exception:
        return False  # 会话级锁随连接断开释放


def target_identity(conn):
    row = conn.execute("SELECT current_database() AS database").fetchone()
    identity = {"database": row["database"]}
    try:
        sys_row = conn.execute(
            "SELECT system_identifier FROM pg_control_system()"
        ).fetchone()
        identity["system_identifier"] = str(sys_row["system_identifier"])
    except Exception:
        identity["system_identifier"] = None  # 权限不足时降级，只凭 database
    return identity


def require_schema12(conn):
    """目标必须已由 `pg_schema.py migrate` 初始化且 checksum 匹配。"""
    report = pg_migrations.verify(conn)
    if report["tables_missing"]:
        raise TargetGuardFailure(
            f"目标 schema12 未初始化，缺表 {report['tables_missing']}；"
            "先运行 scripts/pg_schema.py migrate"
        )
    if report["tables_unexpected"]:
        raise TargetGuardFailure(
            f"目标 public schema 存在预期外表 {report['tables_unexpected']}，拒绝静默忽略"
        )
    if report["schema_version"] != pg_migrations.latest_schema_version():
        raise TargetGuardFailure(
            f"目标 schema 版本 {report['schema_version']}，期望 {pg_migrations.latest_schema_version()}"
        )
    if not report["checksum_ok"]:
        raise TargetGuardFailure("目标 schema12 checksum 与代码不符，拒绝续跑")
    return report


def require_component_schema(conn, components=None):
    """显式组件目标校验；公共新库不要求私人冻结链的表全集。

    不传组件仍沿用迁库前的旧判据。传入组件时先核组件登记，存在遗产链的目标
    再核其完整版本/checksum；新公共库只要求选中组件。两种起点均拒绝越界表。
    """
    if components is None:
        return require_schema12(conn)

    import component_migrations

    present = set(pg_migrations.table_names(conn))
    unexpected = present - set(component_migrations.table_whitelist(components))
    if unexpected:
        raise TargetGuardFailure(
            f"目标 public schema 存在预期外表 {sorted(unexpected)}，拒绝静默忽略"
        )
    legacy_report = None
    if "schema_migrations" in present:
        legacy_report = pg_migrations.verify(conn)
        if legacy_report["tables_missing"]:
            raise TargetGuardFailure(
                f"目标 schema12 未初始化，缺表 {legacy_report['tables_missing']}；"
                "先运行 scripts/pg_schema.py migrate"
            )
        if legacy_report["schema_version"] != pg_migrations.latest_schema_version():
            raise TargetGuardFailure(
                f"目标 schema 版本 {legacy_report['schema_version']}，"
                f"期望 {pg_migrations.latest_schema_version()}"
            )
        if not legacy_report["checksum_ok"]:
            raise TargetGuardFailure("目标 schema12 checksum 与代码不符，拒绝续跑")
    registry = component_migrations.registry_report(conn, components)
    if not registry["registry_present"] or not registry["ok"]:
        raise TargetGuardFailure(
            "组件登记不完整：" + "; ".join(
                f"{issue['kind']}"
                + (f"({issue.get('component')})" if issue.get("component") else "")
                for issue in registry["issues"]
            )
        )
    return legacy_report if legacy_report is not None else registry


def business_table_counts(conn):
    counts = {}
    for table in TABLE_MAP:
        if table == "schema_migrations":
            continue
        counts[table] = conn.execute(f'SELECT COUNT(*) AS c FROM "{table}"').fetchone()["c"]
    for table in TARGET_ONLY_TABLES:
        counts[table] = conn.execute(f'SELECT COUNT(*) AS c FROM "{table}"').fetchone()["c"]
    return counts


def read_credential(conn):
    """解析版本 12 行的结构化完成凭据；不存在/格式不符返回 None。"""
    row = conn.execute(
        "SELECT description FROM schema_migrations WHERE version = %s",
        (pg_migrations.PG_SCHEMA_VERSION,),
    ).fetchone()
    if not row or not row["description"]:
        return None
    try:
        payload = json.loads(row["description"])
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("format") != CREDENTIAL_FORMAT:
        return None
    return payload


def write_credential(conn, credential):
    """在同一迁移事务里把凭据写进版本 12 的 description（§5.3.6）。"""
    conn.execute(
        "UPDATE schema_migrations SET description = %s WHERE version = %s",
        (json.dumps(credential, ensure_ascii=False, sort_keys=True),
         pg_migrations.PG_SCHEMA_VERSION),
    )


def credential_matches(credential, source_sha256, identity, purpose):
    if not credential:
        return False
    return (
        credential.get("source_sha256") == source_sha256
        and credential.get("target_identity", {}).get("database") == identity["database"]
        and credential.get("target_identity", {}).get("system_identifier")
        == identity["system_identifier"]
        and credential.get("target_purpose") == purpose
    )


def restore_sequences(source_info, source_conn, target_conn):
    """§5.3.5：`max(id)` 与源 `sqlite_sequence` 二者较大者恢复；空表 is_called=false。"""
    restored = {}
    high_waters = source_info.get("sequence_high_water", {})
    for table, column in pg_migrations.SEQUENCE_TABLES:
        seq_sql = (
            "SELECT pg_get_serial_sequence(%s, %s) AS seq"
        )
        seq = target_conn.execute(seq_sql, (table, column)).fetchone()["seq"]
        if not seq:
            raise TargetGuardFailure(f"{table}.{column} 找不到序列，DDL 与映射分叉了")
        row = target_conn.execute(f'SELECT MAX("{column}") AS m FROM "{table}"').fetchone()
        target_max = row["m"]
        source_max = None
        if table in set(sqlite_legacy.table_names(source_conn)):
            source_max = sqlite_legacy.max_id(source_conn, table, column)
        candidates = [value for value in (target_max, source_max, high_waters.get(table))
                      if value is not None]
        if not candidates:
            target_conn.execute("SELECT setval(%s, 1, false)", (seq,))
            restored[table] = {"value": 1, "is_called": False}
        else:
            hi = max(candidates)
            target_conn.execute("SELECT setval(%s, %s, true)", (seq, hi))
            restored[table] = {"value": hi, "is_called": True}
    return restored


# --- 搬运写入 ------------------------------------------------------------------


def copy_table_data(source_conn, target_conn, table, rows=None):
    """显式列 INSERT；确定性 PK 文本序。返回 {rows, row_hash, key_min, key_max}。"""
    columns = TABLE_MAP[table]
    amount_cols = set(AMOUNT_COLUMNS.get(table, ()))
    if rows is None:
        rows = load_source_table(source_conn, table)
    prepared = []
    for row in rows:
        out = []
        for column, value in zip(columns, row):
            if column in amount_cols and value is not None:
                if isinstance(value, str):
                    raise PreflightFailure([{
                        "kind": "amount-anomaly", "table": table, "column": column,
                        "detail": "apply 阶段发现 TEXT 金额（preflight 漏检），拒绝写入",
                    }])
                value = money.as_decimal(value)
            out.append(value)
        prepared.append(tuple(out))
    prepared.sort(key=lambda item: "\x1f".join(
        canonical_cell(item[columns.index(pk)]) for pk in PK_COLS[table]
    ))
    column_sql = ", ".join(f'"{column}"' for column in columns)
    placeholder = ", ".join(["%s"] * len(columns))
    with target_conn.cursor() as cursor:
        cursor.executemany(
            f'INSERT INTO "{table}" ({column_sql}) VALUES ({placeholder})',
            prepared,
        )
    canonical_rows, _ = canonicalize_table(table, columns, prepared)
    return {
        "source_rows": len(prepared),
        "row_hash": row_hash(canonical_rows),
        "key_min": min(canonical_rows) if canonical_rows else None,
        "key_max": max(canonical_rows) if canonical_rows else None,
    }


def append_legacy_migrations(source_conn, target_conn):
    """源 schema_migrations 1–11（version/applied_at/description）**恒等**追加（§5.3.3，G07）。

    `applied_at` 源与目标同为 TEXT、同一时钟语义，规则是逐字节恒等搬运：
    **禁止 parse 成 datetime 再回写、禁止任何时区换算**（UTC↔本地漂移），
    也**禁止依赖目标列默认值**——那会用迁移时刻顶替原始时刻，等于丢掉 11 条历史时间。
    """
    rows = source_conn.execute(
        "SELECT version, applied_at, description FROM schema_migrations ORDER BY version"
    ).fetchall()
    added = 0
    for row in rows:
        version, applied_at, description = (
            (row["version"], row["applied_at"], row["description"])
            if hasattr(row, "keys") else tuple(row)
        )
        version = int(version)
        description = str(description)
        if version >= pg_migrations.PG_SCHEMA_VERSION:
            continue
        if not isinstance(applied_at, str) or not applied_at.strip():
            # preflight 已拦；这里是「绕过 preflight」时的兜底：拒绝，不静默顶替
            raise PreflightFailure([{
                "kind": "legacy-applied-at-invalid", "table": "schema_migrations",
                "column": "applied_at", "count": 1,
                "detail": f"源版本 {version} 的 applied_at 非空文本缺失；"
                          "拒绝以目标默认时间顶替（G07）",
            }])
        existing = target_conn.execute(
            "SELECT applied_at, description FROM schema_migrations WHERE version = %s",
            (version,),
        ).fetchone()
        if existing is None:
            target_conn.execute(
                "INSERT INTO schema_migrations (version, applied_at, description) "
                "VALUES (%s, %s, %s)",
                (version, applied_at, description),
            )
            added += 1
        elif existing["applied_at"] != applied_at or existing["description"] != description:
            raise TargetGuardFailure(
                f"目标已有 SQLite 版本 {version} 且 applied_at/description 与源不符，"
                "拒绝静默覆盖"
            )
    return added


# --- 独立比对核心（verify CLI 与 apply 自检共用编码语义，但两侧数据都重新读） ----


def compare_source_target(source_path, target_conn):
    """逐字段/金额/汇总比较 → {mismatches, counts, key_ranges, amount_summary, tables}。"""
    mismatches = []
    counts = {}
    key_ranges = {}
    amount_summary = {}
    tables = []
    source_conn = sqlite_legacy.open_legacy_source(source_path)
    try:
        source_tables = set(sqlite_legacy.table_names(source_conn))
        for table, columns in TABLE_MAP.items():
            if table == "schema_migrations":
                _compare_schema_history(source_conn, target_conn, mismatches)
                continue
            in_source = table in source_tables
            src_rows = load_source_table(source_conn, table) if in_source else []
            tgt_rows = load_target_table(target_conn, table)
            src_canon, src_anom = canonicalize_table(table, columns, src_rows)
            tgt_canon, tgt_anom = canonicalize_table(table, columns, tgt_rows)
            _compare_canon_rows(table, columns, src_canon, tgt_canon, mismatches)
            for column, count in src_anom.items():
                mismatches.append({"table": table, "column": column, "count": count,
                                   "reason": "source-anomalous-value"})
            for column, count in tgt_anom.items():
                mismatches.append({"table": table, "column": column, "count": count,
                                   "reason": "target-anomalous-value"})
            counts[table] = {"source": len(src_canon), "target": len(tgt_canon)}
            all_keys = sorted(set(src_canon) | set(tgt_canon))
            key_ranges[table] = _key_range_entry(table, all_keys)
            status = "copy"
            if not in_source and table in SOURCE_OPTIONAL_TABLES:
                status = "absent-from-source"
            elif table in ("renewals", "ledger_tx"):
                amount_summary[table] = _compare_amounts(
                    table, columns, src_rows, tgt_rows, mismatches
                )
                status = "copy+amount-groups"
            tables.append({"table": table, "source_rows": len(src_canon),
                           "target_rows": len(tgt_canon), "action": status})
        for table in TARGET_ONLY_TABLES:
            count = target_conn.execute(f'SELECT COUNT(*) AS c FROM "{table}"').fetchone()["c"]
            if count:
                mismatches.append({"table": table, "column": None, "count": count,
                                   "reason": "target-only-table-not-empty"})
            counts[table] = {"source": 0, "target": count}
            tables.append({"table": table, "source_rows": 0, "target_rows": count,
                           "action": "must-be-empty"})
    finally:
        source_conn.close()
    return {
        "mismatches": mismatches,
        "counts": counts,
        "key_ranges": key_ranges,
        "amount_summary": amount_summary,
        "tables": tables,
    }


def _compare_canon_rows(table, columns, src, tgt, mismatches):
    only_source = sorted(set(src) - set(tgt))
    only_target = sorted(set(tgt) - set(src))
    if only_source:
        mismatches.append({"table": table, "column": None, "count": len(only_source),
                           "reason": "row-missing-in-target"})
    if only_target:
        mismatches.append({"table": table, "column": None, "count": len(only_target),
                           "reason": "row-extra-in-target"})
    per_column = {}
    for key in set(src) & set(tgt):
        left, right = src[key], tgt[key]
        for index, column in enumerate(columns):
            if left[index] != right[index]:
                per_column[column] = per_column.get(column, 0) + 1
    for column, count in sorted(per_column.items()):
        mismatches.append({"table": table, "column": column, "count": count,
                           "reason": "value-mismatch"})


def _compare_schema_history(source_conn, target_conn, mismatches):
    """三列（version/applied_at/description）恒等比对（G07）。

    比较与搬运同规则、两侧各自重新读（不采信搬运侧的自述）：`applied_at` 是原始 TEXT，
    **不做 parse、不做时区归一**，直接字符串相等。版本 12 仍按既有豁免整体跳过——
    它的 description 会被完成凭据改写（§5.3.6），applied_at 是 PG 基线自己的时刻。
    """
    lookup = "SELECT version, applied_at, description FROM schema_migrations"
    source = {int(row["version"]): (row["applied_at"], str(row["description"]))
              for row in source_conn.execute(lookup).fetchall()}
    target = {int(row["version"]): (row["applied_at"], str(row["description"]))
              for row in target_conn.execute(lookup).fetchall()}
    missing = sorted(set(source) - set(target))
    if missing:
        mismatches.append({"table": "schema_migrations", "column": "version",
                           "count": len(missing),
                           "reason": "legacy-history-rows-missing"})
    per_column = {}
    for version in sorted(set(source) & set(target)):
        if version >= pg_migrations.PG_SCHEMA_VERSION:
            continue
        left, right = source[version], target[version]
        for index, column in enumerate(("applied_at", "description")):
            if left[index] != right[index]:
                per_column[column] = per_column.get(column, 0) + 1
    for column, count in sorted(per_column.items()):
        mismatches.append({"table": "schema_migrations", "column": column, "count": count,
                           "reason": "value-mismatch"})
    if pg_migrations.PG_SCHEMA_VERSION not in target:
        mismatches.append({"table": "schema_migrations", "column": "version",
                           "count": 1, "reason": "missing-pg-baseline-12"})


def _compare_amounts(table, columns, src_rows, tgt_rows, mismatches):
    """按月/方向/渠道/分类（及币种）分组重汇总，源与目标必须全等（§5.4）。"""
    group_cols = AMOUNT_GROUPS[table]
    amount_col = AMOUNT_COLUMNS[table][0]
    gi = [columns.index(col) for col in group_cols]
    ai = columns.index(amount_col)
    pi = [columns.index(col) for col in PK_COLS[table]]

    def aggregate(rows):
        groups = {}
        per_row = []
        for row in rows:
            value = row[ai]
            if value is None or isinstance(value, str):
                continue  # 异常值已由 canonicalize 记为 mismatch，不在这里静默吞
            decimal_value = value if isinstance(value, Decimal) else money.as_decimal(value)
            key = tuple("N" if row[index] is None else str(row[index]) for index in gi)
            groups[key] = groups.get(key, Decimal(0)) + decimal_value
            pk = "\x1f".join(canonical_cell(row[index]) for index in pi)
            per_row.append((pk, money.canonical(decimal_value)))
        return groups, sorted(per_row)

    src_groups, src_per_row = aggregate(src_rows)
    tgt_groups, tgt_per_row = aggregate(tgt_rows)
    if src_groups != tgt_groups:
        diff_keys = sorted(
            key for key in set(src_groups) | set(tgt_groups)
            if src_groups.get(key) != tgt_groups.get(key)
        )
        mismatches.append({"table": table, "column": amount_col, "count": len(diff_keys),
                           "reason": "amount-group-total-mismatch"})
    if src_per_row != tgt_per_row:
        mismatch_count = sum(1 for a, b in zip(src_per_row, tgt_per_row) if a != b) + abs(
            len(src_per_row) - len(tgt_per_row))
        mismatches.append({"table": table, "column": amount_col,
                           "count": mismatch_count, "reason": "amount-canonical-row-mismatch"})
    # 旧浮点累加差额：只报告存在性，不是需要回写的错误（§5.4）
    values = [money.as_decimal(row[ai]) for row in src_rows if row[ai] is not None
              and not isinstance(row[ai], str)]
    drift_count = 0
    if values:
        _, binary_total, difference = money.differs_from_binary_sum(values)
        drift_count = 0 if difference == 0 else 1
        _ = binary_total
    return {
        "groups": len(src_groups),
        "rows_aggregated": len(src_per_row),
        "decimal_total": money.canonical(money.sum_decimals(values)) if values else None,
        "binary_drift_present": drift_count == 1,
    }



# `canonical_cell()` 的类型标签：N=null、I=整数、F=浮点、S=文本、B=字节。
# 主键规范文本就是这些标签 + 内容（单列文本键形如 "Sr1"、"S山田商店"），
# 复合键再用 0x1f 连接。**判数值看标签，不看列名**——列名同为 `id` 的
# `renewals` 存的是 'r1' 文本键（标签 S），而 `panel_scratch.panel_id`
# 这种非 `id` 名的整数主键（标签 I）照样是数值主键。
_CANON_INT_PREFIX = "I"


def _key_range_entry(table, all_keys):
    """`key_ranges[table]`：单列整数主键给 int 区间，其余一律 null。

    报告曾直接塞 `canonicalize_table` 的**主键规范文本**，于是文本/复合文本主键把
    商户、商品原文带进报告（G08）。契约只允许数字或 null
    （`pg_contracts.KEY_RANGES_VALUE_RULE`），且文本主键的字典序区间本就不提供核验
    强度——逐行强度由 counts/row_hash 保证。

    任一键解析不出整数就**整体退回 null**（fail closed），绝不把原文写出去。
    """
    numbers = []
    if len(PK_COLS[table]) == 1:
        try:
            numbers = [int(key[len(_CANON_INT_PREFIX):]) for key in all_keys
                       if key.startswith(_CANON_INT_PREFIX)]
        except (TypeError, ValueError):
            numbers = []
        if len(numbers) != len(all_keys):
            numbers = []
    if not numbers:
        return key_range_entry()
    return key_range_entry(min(numbers), max(numbers))


# --- 报告与凭据 ----------------------------------------------------------------


def publish_report(path, manifest):
    """原子发布：同目录 tmp → fsync → os.replace（§5.3.6）。"""
    # 写出路径的最后一道闸：键集合 + manifest 版本 + key_ranges 值形状。
    # `new_report()` 已在构造处校验；这里再挡一次，覆盖任何手工拼出的 manifest
    # ——G08 的缺口正是「报告带着主键原文落盘而无人拦」。
    validate_report_manifest(manifest)
    path = os.path.abspath(os.path.expanduser(str(path)))
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".report-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def new_report(mode, purpose, status, started_at, info=None, result=None):
    info = info or {}
    result = result or {}
    return build_report_manifest(
        mode=mode,
        target_purpose=purpose,
        source_sha256=info.get("sha256"),
        source_schema_version=(info.get("schema_versions") or [None])[-1],
        target_engine="postgres",
        target_schema_version=result.get("target_schema_version"),
        target_schema_checksum=result.get("target_schema_checksum"),
        target_identity=result.get("target_identity"),
        tables=result.get("tables"),
        counts=result.get("counts"),
        key_ranges=result.get("key_ranges"),
        amount_summary=result.get("amount_summary"),
        mismatches=result.get("mismatches"),
        started_at=started_at,
        finished_at=utc_now_text(),
        status=status,
    )
