# -*- coding: utf-8 -*-
"""db.py 连接工厂与迁移回归。"""
import pathlib
import sqlite3
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import db  # noqa: E402

# 迁移全集从 db.MIGRATIONS 推导：加新迁移不该让这一堆「跑完是最新版」的断言集体失败
ALL_VERSIONS = [version for version, _, _ in db.MIGRATIONS]


def test_run_migrations_drops_removed_tables_and_is_idempotent(tmp_path):
    db_path = tmp_path / "console.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE agents (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO agents (id) VALUES ('a1')")
    conn.commit()
    conn.close()

    first = db.run_migrations(str(db_path))
    second = db.run_migrations(str(db_path))
    assert first == second == ALL_VERSIONS

    conn = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    versions = [
        row[0]
        for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    ]
    conn.close()
    assert "agents" not in tables
    assert "projects" not in tables
    assert "schema_migrations" in tables
    assert versions == ALL_VERSIONS


def test_run_migrations_v2_creates_renewals_on_empty_db(tmp_path):
    db_path = tmp_path / "empty.db"
    versions = db.run_migrations(str(db_path))
    assert versions == ALL_VERSIONS

    conn = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(renewals)").fetchall()
    }
    conn.close()
    assert "renewals" in tables
    assert "name" in cols
    assert "source" in cols
    assert "sort_order" in cols


def test_run_migrations_v2_upgrades_from_v1_and_is_idempotent(tmp_path):
    db_path = tmp_path / "v1.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now')),
            description TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "INSERT INTO schema_migrations (version, description) VALUES (1, 'drop_removed_workstation_tables')"
    )
    conn.commit()
    conn.close()

    first = db.run_migrations(str(db_path))
    second = db.run_migrations(str(db_path))
    assert first == second == ALL_VERSIONS

    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='renewals'"
    ).fetchone()
    versions = [
        row[0]
        for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    ]
    conn.close()
    assert versions == ALL_VERSIONS


def test_run_migrations_v3_backfills_kind_order_and_is_idempotent(tmp_path):
    db_path = tmp_path / "v2.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now')),
            description TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.executemany(
        "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
        (
            (1, "drop_removed_workstation_tables"),
            (2, "create_renewals_table"),
        ),
    )
    conn.execute(db.MIGRATIONS[1][2][0])
    conn.executemany(
        """
        INSERT INTO renewals (id, name, kind, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            ("domain-late", "later", "domain", "2027-01-01"),
            ("domain-b", "same", "domain", "2026-09-01"),
            ("cert-only", "cert", "cert", "2028-01-01"),
            ("domain-a", "same", "domain", "2026-09-01"),
        ),
    )
    conn.execute(
        """
        UPDATE renewals
        SET cost = 12.5, currency = 'USD', cycle = 'yearly', auto_renew = 1,
            url = 'https://later.example', note = '原备注', source = 'probe',
            created_at = '2026-01-02 03:04:05', updated_at = '2026-06-07 08:09:10'
        WHERE id = 'domain-late'
        """
    )
    conn.commit()
    conn.close()

    first = db.run_migrations(str(db_path))
    second = db.run_migrations(str(db_path))
    assert first == second == ALL_VERSIONS

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT id, sort_order FROM renewals ORDER BY kind, sort_order"
    ).fetchall()
    versions = [
        row[0]
        for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    ]
    preserved = conn.execute(
        """
        SELECT cost, currency, cycle, auto_renew, url, note, source, provider, created_at, updated_at
        FROM renewals WHERE id = 'domain-late'
        """
    ).fetchone()
    conn.close()

    assert rows == [
        ("cert-only", 1),
        ("domain-a", 1),
        ("domain-b", 2),
        ("domain-late", 3),
    ]
    assert preserved == (
        12.5,
        "USD",
        "yearly",
        1,
        "https://later.example",
        "原备注",
        "probe",
        None,
        "2026-01-02 03:04:05",
        "2026-06-07 08:09:10",
    )
    assert versions == ALL_VERSIONS


def test_run_migrations_v4_adds_provider_without_changing_v3_rows(tmp_path):
    db_path = tmp_path / "v3.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now')),
            description TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.executemany(
        "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
        (
            (1, "drop_removed_workstation_tables"),
            (2, "create_renewals_table"),
            (3, "add_renewal_sort_order"),
        ),
    )
    conn.execute(db.MIGRATIONS[1][2][0])
    conn.execute(db.MIGRATIONS[2][2][0])
    conn.execute(
        """
        INSERT INTO renewals (
            id, name, kind, expires_at, cost, currency, cycle, auto_renew,
            url, note, source, sort_order, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-v3",
            "legacy",
            "vps",
            "2027-01-01",
            12.5,
            "USD",
            "yearly",
            1,
            "https://legacy.example",
            "旧备注",
            "manual",
            1,
            "2026-01-02 03:04:05",
            "2026-06-07 08:09:10",
        ),
    )
    conn.commit()
    conn.close()

    assert db.run_migrations(str(db_path)) == ALL_VERSIONS
    assert db.run_migrations(str(db_path)) == ALL_VERSIONS

    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(renewals)").fetchall()}
    row = conn.execute(
        """
        SELECT name, kind, expires_at, cost, currency, cycle, auto_renew,
               url, note, source, sort_order, created_at, updated_at, provider
        FROM renewals WHERE id = 'legacy-v3'
        """
    ).fetchone()
    versions = [
        row[0]
        for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    ]
    conn.close()

    assert "provider" in cols
    assert row == (
        "legacy",
        "vps",
        "2027-01-01",
        12.5,
        "USD",
        "yearly",
        1,
        "https://legacy.example",
        "旧备注",
        "manual",
        1,
        "2026-01-02 03:04:05",
        "2026-06-07 08:09:10",
        None,
    )
    assert versions == ALL_VERSIONS


def _seed_v4_db(db_path, rows=()):
    """建一个停在 v4 的库，用于验证 v5 重建表不丢数据。"""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now')),
            description TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.executemany(
        "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
        (
            (1, "drop_removed_workstation_tables"),
            (2, "create_renewals_table"),
            (3, "add_renewal_sort_order"),
            (4, "add_renewal_provider"),
        ),
    )
    conn.execute(db.MIGRATIONS[1][2][0])
    conn.execute(db.MIGRATIONS[2][2][0])
    conn.execute(db.MIGRATIONS[3][2][0])
    conn.executemany(
        """
        INSERT INTO renewals (
            id, name, kind, expires_at, cost, currency, cycle, auto_renew,
            url, note, source, sort_order, created_at, updated_at, provider
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()


def test_run_migrations_v5_rebuilds_renewals_without_losing_rows(tmp_path):
    db_path = tmp_path / "v4.db"
    _seed_v4_db(
        db_path,
        rows=(
            (
                "keep-1", "域名", "domain", "2027-03-13", 42.0, "CNY", "yearly", 1,
                "https://example.test", "备注一", "manual", 1,
                "2026-01-02 03:04:05", "2026-06-07 08:09:10", "阿里云",
            ),
            (
                "keep-2", "机场", "other", "2029-03-03", 415.8, "CNY", "yearly", 0,
                None, None, "manual", 3,
                "2026-02-03 04:05:06", "2026-07-08 09:10:11", None,
            ),
        ),
    )

    assert db.run_migrations(str(db_path)) == ALL_VERSIONS
    assert db.run_migrations(str(db_path)) == ALL_VERSIONS

    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(renewals)").fetchall()}
    rows = conn.execute(
        """
        SELECT id, name, kind, expires_at, starts_at, cost, currency, cycle,
               auto_renew, url, note, source, sort_order, created_at, updated_at, provider
        FROM renewals ORDER BY id
        """
    ).fetchall()
    leftovers = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()

    assert "starts_at" in cols
    assert "renewals_v5" not in leftovers
    assert rows == [
        (
            "keep-1", "域名", "domain", "2027-03-13", None, 42.0, "CNY", "yearly",
            1, "https://example.test", "备注一", "manual", 1,
            "2026-01-02 03:04:05", "2026-06-07 08:09:10", "阿里云",
        ),
        (
            "keep-2", "机场", "other", "2029-03-03", None, 415.8, "CNY", "yearly",
            0, None, None, "manual", 3,
            "2026-02-03 04:05:06", "2026-07-08 09:10:11", None,
        ),
    ]


def test_run_migrations_v5_accepts_ai_kind_and_open_term_rows(tmp_path):
    db_path = tmp_path / "v5.db"
    assert db.run_migrations(str(db_path)) == ALL_VERSIONS

    conn = db.get_connection(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO renewals (id, name, kind, expires_at, cycle, sort_order)
            VALUES ('ai-1', 'GPT Plus', 'ai', '2026-09-17', 'monthly', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO renewals (id, name, kind, expires_at, starts_at, cycle, sort_order)
            VALUES ('topup-1', '米白云', 'other', NULL, '2026-03-01', 'topup', 2)
            """
        )
        conn.commit()

        assert conn.execute(
            "SELECT kind FROM renewals WHERE id = 'ai-1'"
        ).fetchone()[0] == "ai"
        assert conn.execute(
            "SELECT expires_at FROM renewals WHERE id = 'topup-1'"
        ).fetchone()[0] is None

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO renewals (id, name, kind, expires_at, sort_order)
                VALUES ('bad-1', '未知类型', 'nonsense', '2027-01-01', 3)
                """
            )
    finally:
        conn.close()


def test_backup_database_copies_rows(tmp_path):
    src = tmp_path / "src.db"
    dst = tmp_path / "dst.db"
    conn = db.get_connection(str(src))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('hello')")
    conn.commit()
    conn.close()

    db.backup_database(str(dst), str(src))
    conn = sqlite3.connect(dst)
    row = conn.execute("SELECT v FROM t").fetchone()
    conn.close()
    assert row[0] == "hello"


def test_get_connection_enables_wal(tmp_path):
    conn = db.get_connection(str(tmp_path / "wal.db"))
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode.lower() == "wal"
