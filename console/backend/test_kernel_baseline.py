# -*- coding: utf-8 -*-
"""Kernel 基线 DDL 的结构等价证明（B7-2，真 PG16 隔离库）。

这是 `kernel_baseline.py` 存在的**唯一理由**的验收：新库从零建 kernel 的结果，
必须与「跑完冻结遗产链 12–17」得到的 kernel 部分逐字节相同。catalog 指纹比较
列（顺序/类型/非空/默认）、约束、索引，任何一处漂移都会让指纹不等。

同时反向锁定两件事：

- 基线**只**建 kernel 表：私人表（ledger/renewals/panels/feed/backup_evidence）
  一张都不能出现，否则公共新库会凭空长出私人 schema；
- 基线不写任何迁移表：建结构与登记版本是两个可分别验证的动作。
"""
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
REPO_DIR = BACKEND_DIR.parent.parent
for _path in (BACKEND_DIR, REPO_DIR / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import component_migrations as cm  # noqa: E402
import kernel_baseline as baseline  # noqa: E402
import pg_migrations  # noqa: E402
import pg_test_support  # noqa: E402

PRIVATE_TABLES = (
    "renewals", "ledger_tx", "ledger_tx_link", "ledger_categories",
    "ledger_knowledge", "ledger_imports", "ledger_file_batches",
    "bookmarks", "feed_cache", "panels", "panel_scratch",
    "backup_evidence_receipts", "backup_evidence_incidents",
)


@pytest.fixture()
def two_databases(pg_container):
    admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
    names = ["b7_legacy_chain", "b7_kernel_only"]
    for name in names:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield names
    finally:
        for name in names:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (name,))
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.close()


def _connect(pg_container, dbname):
    return pg_test_support.connect(pg_container.dsn(dbname=dbname), autocommit=True)


# --- 纯逻辑 -----------------------------------------------------------------


def test_baseline_tables_match_derived_kernel_tables():
    """基线表清单必须等于组合推导出的 kernel 表，不能各写一份真相。"""
    full = cm.declared_components(REPO_DIR / "plugins")
    assert tuple(sorted(baseline.KERNEL_BASELINE_TABLES)) == full["kernel"]
    # kernel 归属不随插件全集变化：零插件也是同样 11 张
    assert cm.component_table_map({})["kernel"] == full["kernel"]


def test_baseline_version_and_checksum_are_frozen():
    assert baseline.KERNEL_BASELINE_VERSION == 1
    assert baseline.KERNEL_BASELINE_CHECKSUM == pg_migrations.checksum(
        baseline.KERNEL_BASELINE_SQL)
    assert cm.official_expected()["kernel"] == {
        baseline.KERNEL_BASELINE_VERSION: baseline.KERNEL_BASELINE_CHECKSUM}


def test_baseline_never_creates_private_tables():
    text = "\n".join(baseline.KERNEL_BASELINE_SQL)
    for table in PRIVATE_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table} " not in text, table
        assert f"CREATE TABLE IF NOT EXISTS {table}(" not in text, table


def test_baseline_never_touches_migration_ledgers():
    """基线不写 `schema_migrations`，也不预占遗产链版本号。"""
    text = "\n".join(baseline.KERNEL_BASELINE_SQL)
    assert "schema_migrations" not in text
    assert "component_schema_migrations" not in text
    assert "INSERT INTO" not in text.upper()


def test_baseline_is_idempotent_by_construction():
    """每条语句都必须可重复执行：新库安装可能被重跑。"""
    for statement in baseline.KERNEL_BASELINE_SQL:
        upper = statement.upper()
        assert "IF NOT EXISTS" in upper, statement.strip().splitlines()[0]


# --- 真 PG16：结构等价 -------------------------------------------------------


def test_kernel_baseline_equals_legacy_chain_final_state(two_databases, pg_container):
    """核心判据：两条起点的 kernel catalog 指纹逐字节相等。"""
    legacy_name, kernel_name = two_databases
    legacy = _connect(pg_container, legacy_name)
    kernel = _connect(pg_container, kernel_name)
    try:
        pg_migrations.apply(legacy)
        baseline.apply_baseline(kernel)

        tables = baseline.KERNEL_BASELINE_TABLES
        legacy_entries = cm.catalog_entries(legacy, tables)
        kernel_entries = cm.catalog_entries(kernel, tables)
        legacy_fp = cm.catalog_fingerprint(legacy_entries)
        kernel_fp = cm.catalog_fingerprint(kernel_entries)

        if legacy_fp != kernel_fp:
            for table in tables:
                if legacy_entries[table] != kernel_entries[table]:
                    for key in ("columns", "constraints", "indexes"):
                        if legacy_entries[table][key] != kernel_entries[table][key]:
                            print(f"{table}.{key}\n  legacy={legacy_entries[table][key]}"
                                  f"\n  kernel={kernel_entries[table][key]}")
        assert legacy_fp == kernel_fp
    finally:
        legacy.close()
        kernel.close()


def test_kernel_baseline_materializes_exactly_kernel_tables(two_databases, pg_container):
    """只建 kernel 表，且必须全部建出来（少表比多表更难发现）。"""
    _legacy_name, kernel_name = two_databases
    kernel = _connect(pg_container, kernel_name)
    try:
        baseline.apply_baseline(kernel)
        present = set(pg_migrations.table_names(kernel))
        assert present == set(baseline.KERNEL_BASELINE_TABLES)
        assert not (present & set(PRIVATE_TABLES))
    finally:
        kernel.close()


def test_kernel_baseline_writes_no_migration_rows(two_databases, pg_container):
    _legacy_name, kernel_name = two_databases
    kernel = _connect(pg_container, kernel_name)
    try:
        baseline.apply_baseline(kernel)
        assert pg_migrations.applied(kernel) == {}
        assert cm.registry_rows(kernel) is None
    finally:
        kernel.close()


def test_kernel_baseline_is_rerunnable(two_databases, pg_container):
    """重跑基线不报错、不改变指纹（新库安装/升级重入）。"""
    _legacy_name, kernel_name = two_databases
    kernel = _connect(pg_container, kernel_name)
    try:
        baseline.apply_baseline(kernel)
        before = cm.catalog_fingerprint(
            cm.catalog_entries(kernel, baseline.KERNEL_BASELINE_TABLES))
        baseline.apply_baseline(kernel)
        after = cm.catalog_fingerprint(
            cm.catalog_entries(kernel, baseline.KERNEL_BASELINE_TABLES))
        assert before == after
    finally:
        kernel.close()


def test_kernel_baseline_sequences_match_legacy(two_databases, pg_container):
    """序列定义与 ownership 也是结构：基线不能只对上列/约束/索引。"""
    legacy_name, kernel_name = two_databases
    legacy = _connect(pg_container, legacy_name)
    kernel = _connect(pg_container, kernel_name)
    try:
        pg_migrations.apply(legacy)
        baseline.apply_baseline(kernel)
        tables = baseline.KERNEL_BASELINE_TABLES
        legacy_rows = legacy.execute(
            cm._SEQUENCES_SQL, ("public", list(tables))).fetchall()
        kernel_rows = kernel.execute(
            cm._SEQUENCES_SQL, ("public", list(tables))).fetchall()
        assert [tuple(row.values()) for row in kernel_rows] == [
            tuple(row.values()) for row in legacy_rows]
        assert {row["table_name"] for row in kernel_rows} == {
            "monitor_events", "monitoring_samples", "oplog", "probe_history",
            "service_health_history"}
    finally:
        legacy.close()
        kernel.close()


def test_kernel_baseline_hourly_matches_legacy_after_migration_17(two_databases, pg_container):
    """单独锁死最容易被照抄错的那张表。

    `monitoring_hourly` 的最终态是迁移 17 之后的样子：有 `source` 列、有
    `uq_monitoring_hourly_bucket`、**没有**迁移 16 内联的三列 UNIQUE 约束。
    这条用例失败时给出的是可读差异，而不是一个指纹。
    """
    legacy_name, kernel_name = two_databases
    legacy = _connect(pg_container, legacy_name)
    kernel = _connect(pg_container, kernel_name)
    try:
        pg_migrations.apply(legacy)
        baseline.apply_baseline(kernel)
        legacy_entry = cm.catalog_entries(legacy, ("monitoring_hourly",))["monitoring_hourly"]
        kernel_entry = cm.catalog_entries(kernel, ("monitoring_hourly",))["monitoring_hourly"]
        assert kernel_entry == legacy_entry

        columns = [column[0] for column in kernel_entry["columns"]]
        assert "source" in columns and columns[-1] == "source"
        indexes = {item[0] for item in kernel_entry["indexes"]}
        assert "uq_monitoring_hourly_bucket" in indexes
        constraints = {item[0]: item for item in kernel_entry["constraints"]}
        assert "monitoring_hourly_machine_id_metric_name_hour_bucket_key" not in constraints
        source_check = constraints["monitoring_hourly_source_check"]
        assert source_check[1] == "c"
        assert "legacy-mixed" in source_check[2]
    finally:
        legacy.close()
        kernel.close()
