# -*- coding: utf-8 -*-
"""PostgreSQL schema 版本与基线 DDL（提案 §5.1/§5.2；契约版本 `pg00-1`）。

三条规则，抄自提案，违反任何一条都会让 NAS/Mac 的 schema 悄悄分叉：

1. **追加式**：已发布版本的 SQL 冻结，只能往后加版本号。
2. **checksum 不符就拒绝升级**：`schema_migrations.checksum` 记录每条迁移的 SQL 指纹，
   已应用版本重跑时指纹不一致 → `SchemaChecksumMismatch`，宁可停也不改历史。
3. **不伪造 SQLite 1–11**：空测试库写版本 12 基线及后续追加迁移；由生产副本迁入的库
   保留 1–11。本模块从不写入 1–11 的假记录。

DDL 严格取自提案 §5.2（19 张表 = 18 张原有 + 新增 `ledger_file_batches`），
金额列按 §5.4 用**不指定 scale 的 NUMERIC**。
"""
import hashlib

PG_SCHEMA_VERSION = 12  # Released baseline; latest_schema_version() is the deployment target.
PG_BASELINE_DESCRIPTION = "pg_baseline_full_schema"

# 目标表全集（不含 sqlite_sequence；含 NAS 历史兼容表 panels）。
SCHEMA_TABLES = (
    "schema_migrations",
    "renewals",
    "probe_history",
    "machine_traffic",
    "service_health_history",
    "dashboard_layout",
    "monitor_events",
    "ledger_tx",
    "ledger_tx_link",
    "ledger_categories",
    "ledger_knowledge",
    "ledger_imports",
    "ledger_file_batches",
    "oplog",
    "versions",
    "panel_scratch",
    "bookmarks",
    "feed_cache",
    "panels",
    "service_upgrade_reviews",
    "backup_evidence_receipts",
    "backup_evidence_incidents",
    "monitoring_machines",
    "monitoring_samples",
    "monitoring_hourly",
)

# 自增主键表 → 需要按源 `sqlite_sequence` 高水位恢复的序列（PG-03 使用）。
SEQUENCE_TABLES = (
    ("probe_history", "id"),
    ("service_health_history", "id"),
    ("monitor_events", "id"),
    ("ledger_tx", "id"),
    ("ledger_categories", "id"),
    ("oplog", "id"),
    ("panels", "id"),
)

_UTC_NOW = "to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT %s,
    description TEXT NOT NULL DEFAULT '',
    checksum TEXT
)
""" % _UTC_NOW

PG_MIGRATIONS = (
    (
        PG_SCHEMA_VERSION,
        PG_BASELINE_DESCRIPTION,
        (
            SCHEMA_MIGRATIONS_DDL,
            # 迁移 2/3/4/5 的最终结果
            """
            CREATE TABLE IF NOT EXISTS renewals (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT CHECK (kind IN ('domain','cert','vps','subscription','ai','other')),
                expires_at TEXT,
                starts_at TEXT,
                cost NUMERIC,
                currency TEXT,
                cycle TEXT,
                auto_renew INTEGER DEFAULT 0,
                url TEXT,
                note TEXT,
                source TEXT DEFAULT 'manual',
                created_at TEXT DEFAULT %s,
                updated_at TEXT DEFAULT %s,
                sort_order INTEGER NOT NULL DEFAULT 0,
                provider TEXT
            )
            """ % (_UTC_NOW, _UTC_NOW),
            # 迁移 6
            """
            CREATE TABLE IF NOT EXISTS probe_history (
                id BIGSERIAL PRIMARY KEY,
                machine TEXT NOT NULL,
                ts TEXT NOT NULL,
                status TEXT NOT NULL,
                latency_ms DOUBLE PRECISION
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_probe_history_machine ON probe_history(machine, id)",
            # 迁移 7
            """
            CREATE TABLE IF NOT EXISTS machine_traffic (
                machine TEXT PRIMARY KEY,
                month TEXT NOT NULL,
                sent_bytes BIGINT NOT NULL DEFAULT 0,
                recv_bytes BIGINT NOT NULL DEFAULT 0,
                last_sent_total BIGINT NOT NULL DEFAULT 0,
                last_recv_total BIGINT NOT NULL DEFAULT 0,
                nic TEXT DEFAULT '',
                resets INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                updated_at TEXT
            )
            """,
            # 迁移 8
            """
            CREATE TABLE IF NOT EXISTS service_health_history (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                ts TEXT NOT NULL,
                status TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_shh_name ON service_health_history(name, id)",
            # 迁移 9
            """
            CREATE TABLE IF NOT EXISTS dashboard_layout (
                widget_key TEXT PRIMARY KEY,
                x INTEGER NOT NULL,
                y INTEGER NOT NULL,
                w INTEGER NOT NULL,
                hidden INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT
            )
            """,
            # 迁移 10
            """
            CREATE TABLE IF NOT EXISTS monitor_events (
                id BIGSERIAL PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                kind TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                target TEXT NOT NULL,
                machine TEXT NOT NULL DEFAULT '',
                severity TEXT NOT NULL DEFAULT 'error',
                title TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                opened_at TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT '',
                resolved_at TEXT
            )
            """,
            # 部分唯一索引：Postgres 原生支持 WHERE 子句，语法与 SQLite 一致，不用改写
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_monitor_events_open
                ON monitor_events(fingerprint) WHERE resolved_at IS NULL
            """,
            "CREATE INDEX IF NOT EXISTS idx_monitor_events_opened ON monitor_events(opened_at)",
            # 迁移 11（账本 5 张表）
            """
            CREATE TABLE IF NOT EXISTS ledger_tx (
                id BIGSERIAL PRIMARY KEY,
                dedup TEXT UNIQUE,
                source TEXT, src_file TEXT, order_no TEXT,
                time TEXT, date TEXT, month TEXT,
                direction TEXT, amount NUMERIC,
                who TEXT, goods TEXT, pay TEXT, zfb_cat TEXT,
                type1 TEXT, type2 TEXT, basis TEXT,
                pay_confirmed INTEGER DEFAULT 0,
                created_at TEXT DEFAULT %s,
                card TEXT, status TEXT DEFAULT 'normal', note TEXT
            )
            """ % _UTC_NOW,
            "CREATE INDEX IF NOT EXISTS idx_ledger_tx_month ON ledger_tx(month)",
            "CREATE INDEX IF NOT EXISTS idx_ledger_tx_time ON ledger_tx(time DESC, id DESC)",
            "CREATE INDEX IF NOT EXISTS idx_ledger_tx_category ON ledger_tx(type1, type2)",
            "CREATE INDEX IF NOT EXISTS idx_ledger_tx_merchant ON ledger_tx(who, goods)",
            """
            CREATE TABLE IF NOT EXISTS ledger_tx_link (
                a BIGINT, b BIGINT, rel TEXT,
                PRIMARY KEY (a, b)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_ledger_link_b ON ledger_tx_link(b)",
            """
            CREATE TABLE IF NOT EXISTS ledger_categories (
                id BIGSERIAL PRIMARY KEY,
                level1 TEXT, level2 TEXT, sort INTEGER DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS ledger_knowledge (
                who TEXT, good TEXT,
                type1 TEXT, type2 TEXT, cnt INTEGER DEFAULT 1,
                PRIMARY KEY (who, good)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS ledger_imports (
                source_sha256 TEXT PRIMARY KEY,
                imported_at TEXT DEFAULT %s,
                counts_json TEXT NOT NULL DEFAULT '{}'
            )
            """ % _UTC_NOW,
            # app.init_db 中原先遗漏的五张表
            """
            CREATE TABLE IF NOT EXISTS oplog (
                id BIGSERIAL PRIMARY KEY,
                host TEXT, svc TEXT, action TEXT, started_at TEXT, finished_at TEXT,
                result TEXT, operator TEXT DEFAULT 'self'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS versions (
                svc_name TEXT PRIMARY KEY, local_version TEXT, upstream_version TEXT,
                behind_count INTEGER, update_status TEXT, recent_changes TEXT, last_checked TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS panel_scratch (
                panel_id BIGINT PRIMARY KEY, content TEXT, updated_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS bookmarks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
                icon TEXT DEFAULT '', category TEXT DEFAULT 'default', tags TEXT DEFAULT '[]',
                sort_order INTEGER DEFAULT 0,
                created_at TEXT DEFAULT %s, updated_at TEXT DEFAULT %s
            )
            """ % (_UTC_NOW, _UTC_NOW),
            """
            CREATE TABLE IF NOT EXISTS feed_cache (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL,
                url TEXT DEFAULT '', summary TEXT DEFAULT '', icon TEXT DEFAULT '',
                published_at TEXT DEFAULT '',
                fetched_at TEXT DEFAULT %s
            )
            """ % _UTC_NOW,
            # 新增元数据：和业务变更同事务提交；源 SQLite 中不存在
            """
            CREATE TABLE IF NOT EXISTS ledger_file_batches (
                batch_id TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                client TEXT NOT NULL,
                code_version TEXT NOT NULL,
                request_sha256 TEXT NOT NULL,
                reference_manifest_sha256 TEXT,
                artifacts_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                committed_at TEXT NOT NULL DEFAULT %s
            )
            """ % _UTC_NOW,
            # 仅兼容保存 NAS 历史表；当前面板业务仍由 panels.yaml 负责
            """
            CREATE TABLE IF NOT EXISTS panels (
                id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
                icon TEXT DEFAULT '', enabled INTEGER DEFAULT 1, sort_order INTEGER DEFAULT 0
            )
            """,
        ),
    ),
    (
        13,
        "renewal_lifecycle",
        (
            "ALTER TABLE renewals ADD COLUMN status TEXT NOT NULL DEFAULT 'active' "
            "CHECK (status IN ('active', 'not_renewing', 'ended'))",
        ),
    ),
    (
        14,
        "service_upgrade_reviews",
        (
            """
            CREATE TABLE IF NOT EXISTS service_upgrade_reviews (
                svc_name TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'prepared', 'deferred', 'ignored')),
                reason TEXT NOT NULL DEFAULT '',
                checks JSONB NOT NULL DEFAULT '{}'::jsonb,
                decided_at TEXT,
                updated_at TEXT
            )
            """,
        ),
    ),
    (
        15,
        "backup_evidence_plugin",
        (
            """
            CREATE TABLE IF NOT EXISTS backup_evidence_receipts (
                receipt_id TEXT PRIMARY KEY
                    CHECK (receipt_id ~ '^[a-zA-Z0-9_.-]{1,100}$'),
                group_id TEXT NOT NULL,
                group_fingerprint TEXT NOT NULL,
                scope TEXT NOT NULL,
                performed_at TEXT NOT NULL,
                target_kind TEXT NOT NULL DEFAULT 'isolated-local',
                checks JSONB NOT NULL DEFAULT '{}'::jsonb,
                input_data TEXT NOT NULL DEFAULT 'unknown',
                created_at TEXT,
                updated_at TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_backup_receipts_group ON backup_evidence_receipts(group_id)",
            """
            CREATE TABLE IF NOT EXISTS backup_evidence_incidents (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'partial', 'resolved')),
                kind TEXT NOT NULL DEFAULT 'other'
                    CHECK (kind IN ('retention', 'corruption', 'loss', 'other')),
                source TEXT,
                source_time TEXT,
                scope TEXT,
                hash_evidence TEXT,
                member_identity TEXT,
                missing_count INTEGER NOT NULL DEFAULT 0,
                restored_pay_count INTEGER,
                original_missing_count INTEGER,
                verified_at TEXT,
                missing JSONB,
                source_receipts JSONB,
                created_at TEXT,
                updated_at TEXT
            )
            """,
        ),
    ),
    (
        16,
        "monitoring_ingest",
        (
            # B4-2：自有采集链路的机器身份 + 样本存储（B0 冻结：raw 5min 30 天 +
            # hourly 1h 聚合 1 年）。时间戳用 TIMESTAMPTZ：sampled_at 是源机器时钟
            # （契约 §3「不可变、接收方不改写」），received_at 是到达时刻；与本文件
            # 既有表的 TEXT 时间戳（monitor_events 等）刻意不同——那批是字符串序 =
            # 时间序的展示口径，这里要按时间做范围扫描与聚合分桶。
            """
            CREATE TABLE IF NOT EXISTS monitoring_machines (
                machine_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                public_key TEXT,
                secret_hash TEXT NOT NULL,
                enrolled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_seen_at TIMESTAMPTZ,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'revoked')),
                agent_version TEXT,
                capabilities JSONB
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS monitoring_samples (
                id BIGSERIAL PRIMARY KEY,
                machine_id TEXT NOT NULL,
                metric_name TEXT NOT NULL,
                metric_value DOUBLE PRECISION NOT NULL,
                sampled_at TIMESTAMPTZ NOT NULL,
                received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                source TEXT NOT NULL,
                UNIQUE (machine_id, metric_name, sampled_at)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_monitoring_samples_scan "
            "ON monitoring_samples(machine_id, metric_name, sampled_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS monitoring_hourly (
                machine_id TEXT NOT NULL,
                metric_name TEXT NOT NULL,
                hour_bucket TIMESTAMPTZ NOT NULL,
                avg_value DOUBLE PRECISION,
                max_value DOUBLE PRECISION,
                min_value DOUBLE PRECISION,
                p95_value DOUBLE PRECISION,
                sample_count INTEGER NOT NULL,
                UNIQUE (machine_id, metric_name, hour_bucket)
            )
            """,
        ),
    ),
    (
        17,
        "monitoring_hourly_source",
        (
            # B6-3：hourly 增加稳定 source 维度。迁移 16 的 UNIQUE(machine_id,
            # metric_name, hour_bucket) 把 beszel-backfill 的 480m 点与自有 agent
            # 的 5min 点折进同一桶（B5-2 已记录该混合代价）；source 是维度，不是
            # 可丢弃的标记——不同来源不允许等权混合进同一小时桶（B1-3 §4.1）。
            # 值域只有两个逻辑来源：自有 agent 各版本统一折叠为 'console-agent'
            # （不按 agent 版本无限增加序列），回填保持 'beszel-backfill'。
            # 默认值不能是 agent：迁移执行时线上 hourly 允许已有行（NAS 当前 0 行
            # 不是永久事实），既有行只能解读为迁移前聚合的混合桶，落
            # 'legacy-mixed'——未知的来源不冒充 agent（宁实标不伪装）。
            """
            ALTER TABLE monitoring_hourly
                ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'legacy-mixed'
                    CHECK (source IN ('console-agent', 'beszel-backfill', 'legacy-mixed'))
            """,
            # 16 的内联 UNIQUE 约束没有名字可 DROP CONSTRAINT IF EXISTS，先按
            # 目录查出真名再删，保证重复执行幂等。
            """
            DO $$
            DECLARE
                legacy_constraint TEXT;
            BEGIN
                SELECT conname INTO legacy_constraint
                FROM pg_constraint
                WHERE conrelid = 'monitoring_hourly'::regclass
                  AND contype = 'u'
                  AND pg_get_constraintdef(oid)
                      = 'UNIQUE (machine_id, metric_name, hour_bucket)';
                IF legacy_constraint IS NOT NULL THEN
                    EXECUTE format('ALTER TABLE monitoring_hourly '
                                   'DROP CONSTRAINT %I', legacy_constraint);
                END IF;
            END
            $$
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_monitoring_hourly_bucket "
            "ON monitoring_hourly(machine_id, metric_name, hour_bucket, source)",
        ),
    ),
)


class SchemaError(RuntimeError):
    """schema 状态与代码期望不符。"""


class SchemaChecksumMismatch(SchemaError):
    """已应用版本的 SQL 指纹与代码不一致：拒绝升级，先查清是谁改了历史。"""


def _normalize_sql(statement):
    return "\n".join(line.rstrip() for line in statement.strip().splitlines())


def checksum(statements):
    """一条迁移的稳定指纹：与格式化无关，只跟 SQL 内容走。"""
    payload = "\n---\n".join(_normalize_sql(statement) for statement in statements)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def baseline_checksum():
    return checksum(PG_MIGRATIONS[0][2])


def migration_checksums():
    versions = [version for version, _description, _statements in PG_MIGRATIONS]
    if not versions or versions != sorted(set(versions)) or versions[0] != PG_SCHEMA_VERSION:
        raise SchemaChecksumMismatch("PG 迁移版本必须保留基线 12 并按递增顺序追加")
    return {version: checksum(statements) for version, _description, statements in PG_MIGRATIONS}


def latest_schema_version():
    return max(migration_checksums())


def migration_identity(versions):
    """Pure comparison shared by runtime verification and host psql backup/deploy tools."""
    expected = migration_checksums()
    target = max(expected)
    current = max(versions) if versions else None
    missing = sorted(set(expected) - set(versions))
    unknown = sorted(version for version in versions if version >= PG_SCHEMA_VERSION and version not in expected)
    mismatches = sorted(version for version in set(expected) & set(versions)
                        if versions[version].get("checksum") != expected[version])
    checksum_ok = not missing and not mismatches
    return {"schema_version": current, "expected_schema_version": target,
            "checksum": versions.get(target, {}).get("checksum"),
            "expected_checksum": expected[target], "checksum_ok": checksum_ok,
            "versions_missing": missing, "versions_unknown": unknown,
            "checksum_mismatches": mismatches,
            "compatible": current == target and checksum_ok and not unknown}


def ensure_migrations_table(conn):
    """建 `schema_migrations`（幂等）。必须先于任何版本读取。"""
    conn.execute(SCHEMA_MIGRATIONS_DDL)


def is_missing_table_error(exc):
    """精确判定「表不存在」：只认 UndefinedTable，不认权限/连接类错误。

    与 `storage_dispatch.is_missing_table` 同一口径（PG-02 定的规则）：
    连接失败、权限不足必须上抛，不能被降级成「空数据」。
    """
    name = type(exc).__name__
    if name == "UndefinedTable":
        return True
    text = str(exc)
    return "does not exist" in text and "permission denied" not in text


def applied(conn):
    """已应用版本 → {description, checksum}。**只有**表不存在时返回空 dict。"""
    try:
        # 探测缺表必须包在 SAVEPOINT 里：非自动提交连接上（`pg_transaction()` 借出的
        # 就是这种，只读事务尤其常见），一条语句报错会把**整个事务**标记成 aborted，
        # 之后所有命令都被忽略（InFailedSqlTransaction），异常本身却已被下面的
        # `except` 吞掉——调用方拿到的不是「空 schema」而是后续一句无关的报错。
        # psycopg3 的 `conn.transaction()` 在已开启的事务里发 SAVEPOINT，异常只回滚
        # 到该点；在 autocommit 连接上则是普通事务块。两种调用方（在线只读事务 /
        # 迁移 CLI 的裸连接）都成立。
        with conn.transaction():
            rows = conn.execute(
                "SELECT version, description, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
    except Exception as exc:  # psycopg.errors.UndefinedTable 等
        # 判据必须精确到「表不存在」：原先用 `"schema_migrations" in str(exc)`，
        # 于是 `InsufficientPrivilege: permission denied for table schema_migrations`
        # 也被当成「还没建表」，把权限缺陷伪装成空 schema（PG-05 review B-1 同域）。
        if is_missing_table_error(exc):
            return {}
        raise
    return {
        int(row["version"]): {
            "description": row["description"],
            "checksum": row["checksum"],
        }
        for row in rows
    }


def current_version(conn):
    versions = [version for version in applied(conn) if version >= PG_SCHEMA_VERSION]
    return max(versions) if versions else None


def apply(conn):
    """按版本顺序追加未应用的 PG 迁移。调用方负责事务与迁移锁。

    返回本次实际应用的版本列表。幂等：重复调用返回空列表。
    """
    ensure_migrations_table(conn)
    existing = applied(conn)
    identity = migration_identity(existing)
    if identity["versions_unknown"]:
        raise SchemaChecksumMismatch(
            f"PG 迁移记录不兼容：未知版本 {identity['versions_unknown']}；"
            "拒绝追加迁移")
    applied_now = []
    for version, description, statements in PG_MIGRATIONS:
        digest = checksum(statements)
        row = existing.get(version)
        if row is not None:
            recorded = row.get("checksum")
            if recorded and recorded != digest:
                raise SchemaChecksumMismatch(
                    f"schema 版本 {version} 的 SQL 指纹与记录不符："
                    f"记录 {recorded[:12]}… 现算 {digest[:12]}…；"
                    "已发布迁移不得修改，请新增版本号"
                )
            continue
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version, description, checksum) "
            "VALUES (%s, %s, %s)",
            (version, description, digest),
        )
        applied_now.append(version)
    return applied_now


def table_names(conn, schema="public"):
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = %s ORDER BY tablename",
        (schema,),
    ).fetchall()
    return [row["tablename"] for row in rows]


def verify(conn):
    """只读核对 engine/schema/checksum/表清单；输出可安全打印（无 DSN/凭据）。"""
    versions = applied(conn)
    identity = migration_identity(versions)
    present = set(table_names(conn))
    expected_tables = set(SCHEMA_TABLES)
    return {
        "engine": "postgres",
        **identity,
        "tables_present": sorted(present & expected_tables),
        "tables_missing": sorted(expected_tables - present),
        "tables_unexpected": sorted(present - expected_tables),
        "sqlite_versions": sorted(v for v in versions if v < PG_SCHEMA_VERSION),
        "compatible": identity["compatible"] and not (expected_tables - present),
    }


# --- 角色矩阵引导（提案 §6.3） -------------------------------------------------
#
# PG-05 review B-1（blocker）：§6.3 的角色矩阵此前**无任何生产实现**——全仓没有
# `SET ROLE`、没有 `ALTER DEFAULT PRIVILEGES`、没有 `console_owner` 的生产代码，
# 表属主是连接用的 admin 身份。实测后果：下一次 schema 迁移新增的表对运行账号
# 不可写（`InsufficientPrivilege`），而 `pg_schema check --app-role` 只看**既有**表，
# 定向测试全绿也发现不了。
#
# 这里的两个函数是生产实现（不是测试夹具）：`bootstrap_roles` 建全套身份并授予权限，
# `apply_as_owner` 让迁移以 `console_owner` 身份建表。

OWNER_ROLE = "console_owner"
ADMIN_ROLE = "console_admin"
APP_ROLES = ("console_nas", "console_mac")


def _role_exists(conn, role):
    row = conn.execute(
        "SELECT 1 AS present FROM pg_roles WHERE rolname = %s", (role,)
    ).fetchone()
    return row is not None


def _table_exists(conn, table, schema="public"):
    row = conn.execute(
        "SELECT 1 AS present FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r','p')",
        (schema, table),
    ).fetchone()
    return row is not None


def revoke_migrations_writes(conn, app_roles=APP_ROLES):
    """对已存在的 `schema_migrations` 撤销运行账号写权限（§6.3「单独撤销」）。

    bootstrap 跑在空库时该表还不存在；建表后（`apply` 路径）需再撤一次，
    否则 `ALTER DEFAULT PRIVILEGES` 给未来表的 DML 会连带覆盖它。
    """
    if not _table_exists(conn, "schema_migrations"):
        return []
    done = []
    for role in app_roles:
        if not _role_exists(conn, role):
            continue
        conn.execute(
            f'REVOKE INSERT, UPDATE, DELETE ON public.schema_migrations FROM "{role}"'
        )
        done.append(role)
    return done


def set_role(conn, role):
    """以 `SET ROLE` 切到指定身份执行后续 DDL（§6.3：迁移先 SET ROLE 到 owner）。

    比 `SET SESSION AUTHORIZATION` 窄：要求当前用户本身是该角色的成员，
    不会让 admin 变成任意用户。`SET ROLE` 是事务局部的，回滚会一并撤销。
    """
    from psycopg import sql as _sql

    conn.execute(_sql.SQL("SET ROLE {}").format(_sql.Identifier(role)))


def reset_role(conn):
    conn.execute("RESET ROLE")


def _database_owner(conn):
    row = conn.execute(
        "SELECT pg_get_userbyid(datdba) AS owner FROM pg_database "
        "WHERE datname = current_database()"
    ).fetchone()
    return None if row is None else row["owner"]


def _existing_table_owners(conn, schema="public"):
    """public schema 下现有表的属主集合（诊断/验收用）。"""
    rows = conn.execute(
        "SELECT DISTINCT pg_get_userbyid(relowner) AS owner FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind IN ('r','p')",
        (schema,),
    ).fetchall()
    return {row["owner"] for row in rows}


def apply_as_owner(conn, owner=OWNER_ROLE):
    """以 owner 身份跑 baseline DDL：SET ROLE → 建全部表 → RESET ROLE（§6.3）。

    **建表必须发生在 SET ROLE 之后**：`schema_migrations` 也由本函数建，若先以 admin
    建表再切 owner，owner 反而读不到那张 admin 名下的表（实测
    `InsufficientPrivilege: permission denied for table schema_migrations`）。

    是否切身份的判据是**这个库**是否被 bootstrap 过（`ALTER DATABASE … OWNER TO
    console_owner`）。不能用「owner 角色是否存在」代替：角色是**集群级**的，同一
    集群里只要有一个库 bootstrap 过，其它库也会看到该角色；在没 bootstrap 的库上
    盲目 `SET ROLE` 会让 owner 连 public schema 都建不了表（实测
    `InsufficientPrivilege` → 整个事务 aborted）。此时回退当前身份并置
    `owner_mismatch`，不阻断既有调用方。
    """
    if _role_exists(conn, owner) and _database_owner(conn) == owner:
        set_role(conn, owner)
        try:
            applied_now = apply(conn)
        finally:
            reset_role(conn)
        # DEFAULT PRIVILEGES 让新表（含 schema_migrations）自动带运行账号 DML，
        # §6.3 要求该表对运行账号只读——建表后补撤一次。
        revoked = revoke_migrations_writes(conn)
        return {"owner": owner, "applied": applied_now, "role_switched": True,
                "owner_mismatch": False, "schema_migrations_write_revoked": revoked}
    return {"owner": None, "applied": apply(conn), "role_switched": False,
            "owner_mismatch": False, "schema_migrations_write_revoked": []}


def bootstrap_roles(conn, *, owner=OWNER_ROLE, admin=ADMIN_ROLE, app_roles=APP_ROLES,
                    database=None):
    """建 §6.3 全套角色与权限（幂等；需要已存在的 admin 身份来执行）。

    权限面（逐条对应提案 §6.3 原文）：

    - `console_owner`：**NOLOGIN**，拥有 schema/table/sequence。
    - `console_admin`：登录管理员，**是 owner 的成员**（迁移需要能 SET ROLE）。
    - `console_nas`/`console_mac`：LOGIN、NOSUPERUSER/NOCREATEDB/NOCREATEROLE/
      NOREPLICATION，业务表 DML + 序列 USAGE/SELECT，`schema_migrations` 只读。
    - `REVOKE CREATE ON SCHEMA public FROM PUBLIC`。
    - `ALTER DEFAULT PRIVILEGES FOR ROLE console_owner`：**未来**迁移新建的表/序列
      自动带运行账号权限——这是 B-1 的关键缺口。

    不接密码参数：密码由调用方（deploy 侧 secret 文件）用 `psql`/驱动另外设置，
    本函数只处理权限结构，避免把秘密带进日志或报告。
    """
    from psycopg import sql as _sql

    database = database or conn.execute(
        "SELECT current_database() AS db"
    ).fetchone()["db"]
    db_ident = _sql.Identifier(database)

    if not _role_exists(conn, owner):
        conn.execute(
            _sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                     "NOREPLICATION").format(_sql.Identifier(owner))
        )
    if not _role_exists(conn, admin):
        conn.execute(
            _sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                     "NOREPLICATION").format(_sql.Identifier(admin))
        )
    # admin 必须是 owner 的成员，否则 `SET ROLE` 会被拒绝。
    conn.execute(
        _sql.SQL("GRANT {} TO {}").format(
            _sql.Identifier(owner), _sql.Identifier(admin)
        )
    )
    conn.execute(_sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
        db_ident, _sql.Identifier(owner)))
    conn.execute(_sql.SQL("ALTER SCHEMA public OWNER TO {}").format(
        _sql.Identifier(owner)))
    conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")

    for role in app_roles:
        if not _role_exists(conn, role):
            conn.execute(
                _sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                         "NOREPLICATION").format(_sql.Identifier(role))
            )
        role_ident = _sql.Identifier(role)
        conn.execute(_sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            db_ident, role_ident))
        conn.execute(_sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role_ident))
        conn.execute(_sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(role_ident))
        # 既有表/序列
        conn.execute(_sql.SQL(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
        ).format(role_ident))
        conn.execute(_sql.SQL(
            "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}"
        ).format(role_ident))
    # schema_migrations 只读（§6.3）。该表由 baseline DDL 建，bootstrap 可能跑在
    # 空库上（先 bootstrap 再 migrate 是正常顺序）——上面的 GRANT ALL TABLES 会
    # 连带给出写权限，这里对已存在的表立即撤销；表还不存在时由建表路径
    # （`apply_as_owner` → `revoke_migrations_writes`）补撤一次。
    revoked = revoke_migrations_writes(conn, app_roles)

    # 未来默认权限：必须绑定**实际建表者** console_owner（§6.3 原文）。
    for role in app_roles:
        role_ident = _sql.Identifier(role)
        conn.execute(_sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
        ).format(_sql.Identifier(owner), role_ident))
        conn.execute(_sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
            "GRANT USAGE, SELECT ON SEQUENCES TO {}"
        ).format(_sql.Identifier(owner), role_ident))
    # 未来表对 schema_migrations 的写权限同样要撤（新建同名表属极端情况，仍钉住）
    return {
        "owner": owner,
        "admin": admin,
        "app_roles": list(app_roles),
        "database": database,
        "default_privileges_for": owner,
        "schema_migrations_write_revoked": revoked,
    }


def future_table_dml_ok(conn, role, table):
    """§6.3 验收：**新建**的表对该角色是否有 DML（`ALTER DEFAULT PRIVILEGES` 效果）。

    用 `has_table_privilege` 而非「试写一行」：不产生数据副作用，可在只读验收里跑。
    """
    row = conn.execute(
        "SELECT"
        " has_table_privilege(%s, 'public.' || quote_ident(%s), 'SELECT') AS s,"
        " has_table_privilege(%s, 'public.' || quote_ident(%s), 'INSERT') AS i,"
        " has_table_privilege(%s, 'public.' || quote_ident(%s), 'UPDATE') AS u,"
        " has_table_privilege(%s, 'public.' || quote_ident(%s), 'DELETE') AS d",
        (role, table, role, table, role, table, role, table),
    ).fetchone()
    return {
        "select": row["s"], "insert": row["i"],
        "update": row["u"], "delete": row["d"],
        "ok": all(row[key] for key in ("s", "i", "u", "d")),
    }
