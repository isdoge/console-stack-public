# -*- coding: utf-8 -*-
"""Kernel 基线 DDL：新库从零建 kernel 组件，而不是重放冻结遗产链。

B7-2 的第二条迁移起点。冻结遗产链 12–17 仍是私人既有库的唯一历史，本文件
只回答「全新的公共/私人新库该怎么建 kernel 部分」：

- 只含 kernel 表，**不含** ledger/renewals/feed/panels/backup_evidence 等私人表；
- 每张表都写成**遗产链跑完后的最终态**，不是逐条重放历史。两处不能照抄：
  - `monitoring_hourly`：迁移 16 建表时带内联三列唯一约束，迁移 17 才加
    `source` 列、删掉那个约束并改建 `uq_monitoring_hourly_bucket`。照抄 16 会
    在新库留下一个旧库没有的约束。
  - `monitor_events`：部分唯一索引在独立语句里，不在 CREATE TABLE 内。
- 结构等价由 `test_kernel_baseline.py` 用 catalog 指纹逐字节证明，不靠肉眼比对。

本文件同时是**正式组件基线**的唯一来源：`KERNEL_BASELINE_VERSION` /
`KERNEL_BASELINE_CHECKSUM` 与 DDL 一起冻结，`component_migrations` 直接引用，
不在别处写第二份版本号或 SQL。改基线 SQL 必须同时加新版本，不能原地改。

接管旧库时**不重放**这条基线：接管回执（version 0）覆盖基线版本，结构与
冻结遗产链的等价性由 `component_migrations.adopt_legacy()` 在写回执前证明。
"""

# 顺序即建表顺序，便于人工比对；指纹比较按表名排序，与顺序无关。
KERNEL_BASELINE_SQL = (
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
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_monitor_events_open
        ON monitor_events(fingerprint) WHERE resolved_at IS NULL
    """,
    "CREATE INDEX IF NOT EXISTS idx_monitor_events_opened ON monitor_events(opened_at)",
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
        source TEXT NOT NULL DEFAULT 'legacy-mixed'
            CHECK (source IN ('console-agent', 'beszel-backfill', 'legacy-mixed'))
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_monitoring_hourly_bucket "
    "ON monitoring_hourly(machine_id, metric_name, hour_bucket, source)",
    """
    CREATE TABLE IF NOT EXISTS oplog (
        id BIGSERIAL PRIMARY KEY,
        host TEXT, svc TEXT, action TEXT, started_at TEXT, finished_at TEXT,
        result TEXT, operator TEXT DEFAULT 'self'
    )
    """,
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
    """
    CREATE TABLE IF NOT EXISTS service_health_history (
        id BIGSERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        ts TEXT NOT NULL,
        status TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_shh_name ON service_health_history(name, id)",
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
    """
    CREATE TABLE IF NOT EXISTS versions (
        svc_name TEXT PRIMARY KEY, local_version TEXT, upstream_version TEXT,
        behind_count INTEGER, update_status TEXT, recent_changes TEXT, last_checked TEXT
    )
    """,
)

# 与 `component_migrations.component_table_map()` 的 kernel 结果必须一致；
# 由测试交叉核对，不在这里手写第二份「真相」。
KERNEL_BASELINE_TABLES = (
    "dashboard_layout", "machine_traffic", "monitor_events", "monitoring_hourly",
    "monitoring_machines", "monitoring_samples", "oplog", "probe_history",
    "service_health_history", "service_upgrade_reviews", "versions",
)

# 正式组件基线：新库安装登记这一条，接管库用回执 v0 覆盖它（不重放 CREATE）。
KERNEL_BASELINE_VERSION = 1
# 冻结指纹：改基线 SQL 必须同时加新版本；测试断言它与现算 checksum 相等。
KERNEL_BASELINE_CHECKSUM = (
    "4a4fd47fbbb369fab8c21b99972777d270e7c57cdb4d1307a9e1584cc2c0dfa9"
)


def apply_baseline(conn):
    """在空库建 kernel 表（幂等）。调用方负责事务与维护锁。

    不写 `schema_migrations`，不写 `component_schema_migrations`：新库的组件
    登记由调用方随后按 `component_migrations.apply_components()` 完成。这样
    「建结构」与「登记版本」是两个可分别验证的动作。
    """
    for statement in KERNEL_BASELINE_SQL:
        conn.execute(statement)
