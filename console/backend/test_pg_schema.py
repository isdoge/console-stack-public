# -*- coding: utf-8 -*-
"""schema12 基线 DDL 与 checksum 的真 PG16 回归（提案 §5.1/§5.2/§5.4）。"""
import pathlib
import sys
import uuid

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import pg_migrations  # noqa: E402
import pg_test_support  # noqa: E402


@pytest.fixture()
def empty_db(pg_container):
    """每个测试一个全新数据库，互不共享 schema 状态。"""
    admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
    name = f"console_test_schema_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    admin.close()
    dsn = pg_container.dsn(dbname=name)
    try:
        yield dsn
    finally:
        admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


@pytest.fixture()
def schema_conn(empty_db):
    conn = pg_test_support.connect(empty_db, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


def test_apply_creates_every_target_table_in_one_transaction(schema_conn):
    applied = pg_migrations.apply(schema_conn)
    assert applied == [v for v, _, _ in pg_migrations.PG_MIGRATIONS]
    present = set(pg_migrations.table_names(schema_conn))
    assert set(pg_migrations.SCHEMA_TABLES) <= present
    assert len(pg_migrations.SCHEMA_TABLES) == 25


def test_upgrade_12_preserves_existing_renewal(schema_conn, monkeypatch):
    migrations = pg_migrations.PG_MIGRATIONS
    with monkeypatch.context() as patch:
        patch.setattr(pg_migrations, "PG_MIGRATIONS", migrations[:1])
        assert pg_migrations.apply(schema_conn) == [12]
    schema_conn.execute(
        "INSERT INTO renewals (id, name, kind, expires_at, cost, note) "
        "VALUES ('old', 'old subscription', 'subscription', '2026-01-01', 19.99, 'keep')"
    )
    before = dict(schema_conn.execute("SELECT * FROM renewals WHERE id='old'").fetchone())
    assert pg_migrations.apply(schema_conn) == [13, 14, 15, 16, 17]
    after = dict(schema_conn.execute("SELECT * FROM renewals WHERE id='old'").fetchone())
    assert after.pop("status") == "active"
    assert after == before
    assert pg_migrations.apply(schema_conn) == []


def test_apply_is_idempotent_and_keeps_single_version_row(schema_conn):
    assert pg_migrations.apply(schema_conn) == [v for v, _, _ in pg_migrations.PG_MIGRATIONS]
    assert pg_migrations.apply(schema_conn) == []
    rows = schema_conn.execute(
        "SELECT version, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert [row["version"] for row in rows] == [v for v, _, _ in pg_migrations.PG_MIGRATIONS]
    assert rows[0]["checksum"] == pg_migrations.baseline_checksum()


def test_baseline_never_fakes_sqlite_versions_1_to_11(schema_conn):
    pg_migrations.apply(schema_conn)
    rows = schema_conn.execute("SELECT version FROM schema_migrations").fetchall()
    assert [row["version"] for row in rows] == [v for v, _, _ in pg_migrations.PG_MIGRATIONS]


def test_checksum_mismatch_is_refused_instead_of_silently_reapplied(schema_conn):
    pg_migrations.apply(schema_conn)
    schema_conn.execute(
        "UPDATE schema_migrations SET checksum = %s WHERE version = %s",
        ("0" * 64, pg_migrations.PG_SCHEMA_VERSION),
    )
    with pytest.raises(pg_migrations.SchemaChecksumMismatch):
        pg_migrations.apply(schema_conn)


def test_checksum_ignores_surrounding_whitespace_but_not_content():
    # 只归一化首尾与行尾空白；SQL 内部差异（含字符串字面量里的空格）必须改变指纹。
    assert pg_migrations.checksum(("SELECT 1   \n",)) == pg_migrations.checksum(
        ("  SELECT 1\n\n",)
    )
    assert pg_migrations.checksum(("SELECT 1",)) != pg_migrations.checksum(("SELECT 2",))
    assert pg_migrations.checksum(("SELECT 'a b'",)) != pg_migrations.checksum(
        ("SELECT 'ab'",)
    )


def test_verify_reports_compatible_after_apply(schema_conn):
    pg_migrations.apply(schema_conn)
    report = pg_migrations.verify(schema_conn)
    assert report["compatible"] is True
    assert report["schema_version"] == pg_migrations.latest_schema_version()
    assert report["checksum_ok"] is True
    assert report["tables_missing"] == []
    assert report["sqlite_versions"] == []


def test_appended_migration_verifies_latest_and_every_checksum(schema_conn, monkeypatch):
    pg_migrations.apply(schema_conn)
    original = pg_migrations.baseline_checksum()
    next_version = pg_migrations.latest_schema_version() + 1
    monkeypatch.setattr(pg_migrations, "PG_MIGRATIONS", (*pg_migrations.PG_MIGRATIONS,
                        (next_version, "test_append", ("ALTER TABLE bookmarks ADD COLUMN test_append TEXT",))))
    with schema_conn.transaction():
        assert pg_migrations.apply(schema_conn) == [next_version]
    report = pg_migrations.verify(schema_conn)
    assert report["compatible"] is True
    assert report["schema_version"] == report["expected_schema_version"] == next_version
    assert pg_migrations.baseline_checksum() == original
    assert pg_migrations.apply(schema_conn) == []
    schema_conn.execute("UPDATE schema_migrations SET checksum = %s WHERE version = 12", ("0" * 64,))
    assert pg_migrations.verify(schema_conn)["compatible"] is False
    with pytest.raises(pg_migrations.SchemaChecksumMismatch):
        pg_migrations.apply(schema_conn)


def test_unknown_future_schema_rejects_before_any_append(schema_conn, monkeypatch):
    pg_migrations.apply(schema_conn)
    next_version = pg_migrations.latest_schema_version() + 2
    schema_conn.execute(
        "INSERT INTO schema_migrations (version, checksum) VALUES (%s, 'unknown')",
        (next_version,),
    )
    monkeypatch.setattr(pg_migrations, "PG_MIGRATIONS", (*pg_migrations.PG_MIGRATIONS,
                        (pg_migrations.latest_schema_version() + 1, "test_append",
                         ("ALTER TABLE bookmarks ADD COLUMN test_append TEXT",))))
    assert pg_migrations.verify(schema_conn)["compatible"] is False
    with pytest.raises(pg_migrations.SchemaChecksumMismatch):
        pg_migrations.apply(schema_conn)
    assert schema_conn.execute(
        "SELECT count(*) AS n FROM schema_migrations WHERE version = %s", (next_version - 1,)
    ).fetchone()["n"] == 0


def test_verify_reports_missing_tables_on_empty_database(schema_conn):
    report = pg_migrations.verify(schema_conn)
    assert report["compatible"] is False
    assert set(report["tables_missing"]) == set(pg_migrations.SCHEMA_TABLES)


def test_migration_17_labels_preexisting_hourly_rows_legacy_mixed(schema_conn, monkeypatch):
    """迁移 17 升级路径：迁移 16 时代的既有 hourly 行落 legacy-mixed，绝不冒充 agent。

    迁移时线上 hourly 允许已有行（迁移前聚合的混合桶）；默认值
    'legacy-mixed' 是唯一如实解读——未知的来源不静默冒充 agent（B6-3）。
    """
    migrations = pg_migrations.PG_MIGRATIONS
    with monkeypatch.context() as patch:
        # 只跑到迁移 16：模拟尚未升级 17 的线上库。
        up_to_16 = [item for item in migrations if item[0] <= 16]
        patch.setattr(pg_migrations, "PG_MIGRATIONS", tuple(up_to_16))
        assert pg_migrations.apply(schema_conn) == [v for v, _, _ in up_to_16]
    schema_conn.execute(
        "INSERT INTO monitoring_machines (machine_id, display_name, secret_hash) "
        "VALUES ('nas', 'NAS', %s)", ("x" * 64,)
    )
    schema_conn.execute(
        "INSERT INTO monitoring_hourly "
        "       (machine_id, metric_name, hour_bucket, avg_value, max_value, "
        "        min_value, p95_value, sample_count) "
        "VALUES ('nas', 'cpu_used_pct', '2026-10-01T02:00:00Z', 1.0, 2.0, 0.5, 1.5, 12)"
    )
    # 恢复全量迁移再补 17：既有行必须被标 legacy-mixed，且唯一索引容纳 source。
    assert pg_migrations.apply(schema_conn) == [17]
    row = schema_conn.execute(
        "SELECT source FROM monitoring_hourly WHERE machine_id = 'nas'"
    ).fetchone()
    assert row["source"] == "legacy-mixed"
    # 重复应用幂等。
    assert pg_migrations.apply(schema_conn) == []


def test_migration_17_replaces_bucket_unique_with_source_dimension(schema_conn):
    """16 的三列 UNIQUE 被四列唯一索引替换：同桶不同 source 可共存。"""
    pg_migrations.apply(schema_conn)
    schema_conn.execute(
        "INSERT INTO monitoring_machines (machine_id, display_name, secret_hash) "
        "VALUES ('nas', 'NAS', %s)", ("x" * 64,)
    )
    insert = (
        "INSERT INTO monitoring_hourly "
        "       (machine_id, metric_name, hour_bucket, source, avg_value, max_value, "
        "        min_value, p95_value, sample_count) "
        "VALUES ('nas', 'cpu_used_pct', '2026-10-01T02:00:00Z', %s, 1.0, 2.0, 0.5, 1.5, 12)"
    )
    schema_conn.execute(insert, ("console-agent",))
    schema_conn.execute(insert, ("beszel-backfill",))
    rows = schema_conn.execute(
        "SELECT source FROM monitoring_hourly WHERE machine_id = 'nas' "
        "ORDER BY source"
    ).fetchall()
    assert [row["source"] for row in rows] == ["beszel-backfill", "console-agent"]
    # 同 source 同桶仍然互斥（幂等重算的物理基础）。
    with pytest.raises(Exception):
        schema_conn.execute(insert, ("console-agent",))
    # CHECK 收紧值域：表外的 source 不接受。
    with pytest.raises(Exception):
        schema_conn.execute(insert, ("mystery-collector",))


def test_amount_columns_are_unscaled_numeric(schema_conn):
    pg_migrations.apply(schema_conn)
    for table, column in (("ledger_tx", "amount"), ("renewals", "cost")):
        row = schema_conn.execute(
            "SELECT data_type, numeric_precision, numeric_scale FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
            (table, column),
        ).fetchone()
        assert row["data_type"] == "numeric"
        # 不指定 scale：历史有效小数不能被固定两位截断
        assert row["numeric_precision"] is None
        assert row["numeric_scale"] is None


def test_partial_unique_index_allows_one_open_event_per_fingerprint(schema_conn):
    import psycopg

    pg_migrations.apply(schema_conn)
    schema_conn.execute(
        "INSERT INTO monitor_events (fingerprint, kind, target, opened_at) "
        "VALUES ('fp-1', 'down', 'svc', '2026-01-01 00:00:00')"
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        schema_conn.execute(
            "INSERT INTO monitor_events (fingerprint, kind, target, opened_at) "
            "VALUES ('fp-1', 'down', 'svc', '2026-01-01 00:00:05')"
        )
    # 恢复后同一 fingerprint 可以再开一条（历史各自闭合）
    schema_conn.execute(
        "UPDATE monitor_events SET resolved_at = '2026-01-01 00:01:00' "
        "WHERE fingerprint = 'fp-1'"
    )
    schema_conn.execute(
        "INSERT INTO monitor_events (fingerprint, kind, target, opened_at) "
        "VALUES ('fp-1', 'down', 'svc', '2026-01-01 00:02:00')"
    )
    count = schema_conn.execute(
        "SELECT COUNT(*) AS n FROM monitor_events WHERE fingerprint = 'fp-1'"
    ).fetchone()["n"]
    assert count == 2


def test_ledger_file_batches_metadata_table_is_empty_after_baseline(schema_conn):
    pg_migrations.apply(schema_conn)
    row = schema_conn.execute(
        "SELECT COUNT(*) AS n FROM ledger_file_batches"
    ).fetchone()
    assert row["n"] == 0


def test_sequence_tables_use_bigserial(schema_conn):
    pg_migrations.apply(schema_conn)
    for table, column in pg_migrations.SEQUENCE_TABLES:
        row = schema_conn.execute(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
            (table, column),
        ).fetchone()
        assert row["column_default"] and "nextval" in row["column_default"], table
