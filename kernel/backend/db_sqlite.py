# -*- coding: utf-8 -*-
"""legacy SQLite 连接/迁移/在线备份（冻结；仅离线取证、迁移源与既有测试）。

**不是**在线业务 fallback（决策 D3）。在线业务只走 `pgdb` / `db.pg_*` 转发。
从原 `db.py` 主体抽出；`db.py` 同对象重导出以保留旧导入。
"""
import sqlite3

import paths

DB_PATH = paths.DB_PATH

# (version, description, sql_statements)
# 只追加，勿改已发布版本的 SQL。
MIGRATIONS = (
    (
        1,
        "drop_removed_workstation_tables",
        (
            "DROP TABLE IF EXISTS agents",
            "DROP TABLE IF EXISTS projects",
        ),
    ),
    (
        2,
        "create_renewals_table",
        (
            """
            CREATE TABLE IF NOT EXISTS renewals (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT CHECK(kind IN ('domain','cert','vps','subscription','other')),
                expires_at TEXT NOT NULL,
                cost REAL,
                currency TEXT,
                cycle TEXT,
                auto_renew INTEGER DEFAULT 0,
                url TEXT,
                note TEXT,
                source TEXT DEFAULT 'manual',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """,
        ),
    ),
    (
        3,
        "add_renewal_sort_order",
        (
            "ALTER TABLE renewals ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0",
            """
            UPDATE renewals AS target
            SET sort_order = (
                SELECT COUNT(*)
                FROM renewals AS candidate
                WHERE candidate.kind IS target.kind
                  AND (
                    candidate.expires_at < target.expires_at
                    OR (
                        candidate.expires_at = target.expires_at
                        AND candidate.name < target.name
                    )
                    OR (
                        candidate.expires_at = target.expires_at
                        AND candidate.name = target.name
                        AND candidate.id <= target.id
                    )
                  )
            )
            """,
        ),
    ),
    (
        4,
        "add_renewal_provider",
        (
            "ALTER TABLE renewals ADD COLUMN provider TEXT",
        ),
    ),
    (
        5,
        "rebuild_renewals_for_ai_and_open_terms",
        (
            # kind 的 CHECK 与 expires_at 的 NOT NULL 都写在 v2 建表语句里，
            # SQLite 无法 ALTER 掉，只能重建表：放开 ai 类型、允许不限时条目、加 starts_at。
            """
            CREATE TABLE renewals_v5 (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT CHECK(kind IN ('domain','cert','vps','subscription','ai','other')),
                expires_at TEXT,
                starts_at TEXT,
                cost REAL,
                currency TEXT,
                cycle TEXT,
                auto_renew INTEGER DEFAULT 0,
                url TEXT,
                note TEXT,
                source TEXT DEFAULT 'manual',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                sort_order INTEGER NOT NULL DEFAULT 0,
                provider TEXT
            )
            """,
            """
            INSERT INTO renewals_v5 (
                id, name, kind, expires_at, cost, currency, cycle, auto_renew,
                url, note, source, created_at, updated_at, sort_order, provider
            )
            SELECT
                id, name, kind, expires_at, cost, currency, cycle, auto_renew,
                url, note, source, created_at, updated_at, sort_order, provider
            FROM renewals
            """,
            "DROP TABLE renewals",
            "ALTER TABLE renewals_v5 RENAME TO renewals",
        ),
    ),
    (
        6,
        "create_probe_history_table",
        (
            # E4：机器探活时序，供节点卡「最近探测」色带。只存 node 探活结果，
            # 按机器保留最近 N 条（node_probe.RETAIN_PER_MACHINE 插入时裁剪）。
            """
            CREATE TABLE IF NOT EXISTS probe_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                machine TEXT NOT NULL,
                ts TEXT NOT NULL,
                status TEXT NOT NULL,
                latency_ms REAL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_probe_history_machine ON probe_history(machine, id)",
        ),
    ),
    (
        7,
        "create_machine_traffic_table",
        (
            # E1：月流量账本。Beszel ni 给的是**累计计数器**，这里存「本月累计增量」
            # 与「上次读到的计数器绝对值」，靠差值累加，天然容忍采样间隔不均；
            # 计数器变小 = agent 重装/机器重启导致归零，按从 0 重新计。
            """
            CREATE TABLE IF NOT EXISTS machine_traffic (
                machine TEXT PRIMARY KEY,
                month TEXT NOT NULL,
                sent_bytes INTEGER NOT NULL DEFAULT 0,
                recv_bytes INTEGER NOT NULL DEFAULT 0,
                last_sent_total INTEGER NOT NULL DEFAULT 0,
                last_recv_total INTEGER NOT NULL DEFAULT 0,
                nic TEXT DEFAULT '',
                resets INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                updated_at TEXT
            )
            """,
        ),
    ),
    (
        8,
        "create_service_health_history",
        (
            # T12 分支实验：服务健康时序（迷你色带）。只记 Healthy/Unhealthy 的真实探测，
            # Unknown（无 checker / 显式关闭）不落点。按服务保留 N 条，插入时顺带裁剪。
            """
            CREATE TABLE IF NOT EXISTS service_health_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                ts TEXT NOT NULL,
                status TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_shh_name ON service_health_history(name, id)",
        ),
    ),
    (
        9,
        "create_dashboard_layout",
        (
            # D1'：C8 布局 override 从 localStorage 升库。列集对齐 D3 override JSON 实际
            # 形状（{key,x,y,w} + 独立的 hidden 集合），**不存 h**——高度由前端
            # GridStack sizeToContent 按内容定（C8 数据模型契约）。
            # hidden 与坐标同表：一行 = 一个 widget 的用户自定义；hidden=1 的行坐标
            # 仍保留（对应 D3「拖过再隐藏、找回后位置不变」语义）。
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
        ),
    ),
    (
        10,
        "create_monitor_events",
        (
            # Track K1：事件生命周期。与 probe_history / service_health_history
            # 的**采样时序**不同——那两张表每次真实探测都落一行，这张表只在状态
            # **变化**时写（K2「只记 transition」）：失败开事件、恢复关同一条。
            # resolved_at IS NULL 即「活动事件」。
            """
            CREATE TABLE IF NOT EXISTS monitor_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            # 幂等的**物理**保证：同一 fingerprint 最多一个未恢复事件。
            # 部分索引（SQLite ≥3.8.0）——已恢复的历史行不参与唯一性，
            # 同一目标反复失败/恢复能留下多条各自闭合的历史。
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_monitor_events_open
                ON monitor_events(fingerprint) WHERE resolved_at IS NULL
            """,
            # 事件页默认「最近 N 天」窗口 + 保留策略清理都按 opened_at 扫
            "CREATE INDEX IF NOT EXISTS idx_monitor_events_opened ON monitor_events(opened_at)",
        ),
    ),
    (
        11,
        "create_personal_ledger",
        (
            # Pay 的原始值/ID 保留；独立命名空间随 console.db 一起备份和恢复。
            """
            CREATE TABLE IF NOT EXISTS ledger_tx (
                id INTEGER PRIMARY KEY,
                dedup TEXT UNIQUE,
                source TEXT, src_file TEXT, order_no TEXT,
                time TEXT, date TEXT, month TEXT,
                direction TEXT, amount REAL,
                who TEXT, goods TEXT, pay TEXT, zfb_cat TEXT,
                type1 TEXT, type2 TEXT, basis TEXT,
                pay_confirmed INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                card TEXT, status TEXT DEFAULT 'normal', note TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_ledger_tx_month ON ledger_tx(month)",
            "CREATE INDEX IF NOT EXISTS idx_ledger_tx_time ON ledger_tx(time DESC, id DESC)",
            "CREATE INDEX IF NOT EXISTS idx_ledger_tx_category ON ledger_tx(type1, type2)",
            "CREATE INDEX IF NOT EXISTS idx_ledger_tx_merchant ON ledger_tx(who, goods)",
            """
            CREATE TABLE IF NOT EXISTS ledger_tx_link (
                a INTEGER, b INTEGER, rel TEXT,
                PRIMARY KEY (a, b)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_ledger_link_b ON ledger_tx_link(b)",
            """
            CREATE TABLE IF NOT EXISTS ledger_categories (
                id INTEGER PRIMARY KEY,
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
                imported_at TEXT DEFAULT (datetime('now')),
                counts_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
        ),
    ),
)


def get_connection(db_path=None):
    """统一连接：WAL + busy_timeout，row_factory=Row。"""
    conn = sqlite3.connect(db_path or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_migrations_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now')),
            description TEXT NOT NULL DEFAULT ''
        )
        """
    )


def applied_versions(conn):
    _ensure_migrations_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return {int(row["version"]) for row in rows}


def run_migrations(db_path=None):
    """按版本顺序应用未执行的迁移；可重复调用（幂等）。"""
    conn = get_connection(db_path)
    try:
        done = applied_versions(conn)
        for version, description, statements in MIGRATIONS:
            if version in done:
                continue
            try:
                conn.execute("BEGIN")
                for stmt in statements:
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                    (version, description),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return sorted(applied_versions(conn))
    finally:
        conn.close()


def backup_database(dest_path, db_path=None):
    """用 SQLite backup API 做在线一致性备份。"""
    src = get_connection(db_path)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


