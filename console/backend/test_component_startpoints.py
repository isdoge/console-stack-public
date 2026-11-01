# -*- coding: utf-8 -*-
"""B7-2 双起点验收：新库从零安装 vs 旧库显式接管，必须收敛到同一状态。

两条起点：

- **A 新库安装**：空库 → `kernel_baseline.apply_baseline()` → 组件登记；
- **B 旧库接管**：空库 → 冻结遗产链 12–17 → `adopt_legacy()` 写回执。

B7-2 的退出条件要求这两条路都能独立完成，且**都不改生产链**。本文件锁死：

1. 两条起点的 kernel catalog 指纹相同（结构收敛）；
2. 旧库接管**不重放** CREATE/seed、不 DROP、不改 `schema_migrations`；
3. 重复执行无副作用；中断不留半登记（事务回滚）；
4. 篡改 checksum / 缺表 / 权限不足必须失败。
"""
import pathlib
import sys
import uuid

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


@pytest.fixture()
def databases(pg_container):
    admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
    names = {}
    for label in ("new_install", "legacy_adopt"):
        name = f"b7sp_{label}_{uuid.uuid4().hex[:6]}"
        admin.execute(f'CREATE DATABASE "{name}"')
        names[label] = name
    try:
        yield names
    finally:
        for name in names.values():
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (name,))
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.close()


@pytest.fixture()
def components():
    return cm.declared_components(REPO_DIR / "plugins")


def _connect(pg_container, dbname):
    return pg_test_support.connect(pg_container.dsn(dbname=dbname), autocommit=True)


# --- 起点 A：全新库安装 ------------------------------------------------------


def test_zero_plugin_new_install_is_ok_with_non_empty_registry(
        databases, pg_container):
    """公共零插件新装：正式 CLI 起点必须 ok=true 且登记非空。"""
    components = cm.zero_plugin_components()
    conn = _connect(pg_container, databases["new_install"])
    try:
        outcome = cm.apply_components(conn, None, components)
        assert outcome["applied"] == [{"component": "kernel",
                                       "version": baseline.KERNEL_BASELINE_VERSION,
                                       "checksum": baseline.KERNEL_BASELINE_CHECKSUM}]
        report = cm.registry_report(conn, components)
        assert report["ok"] is True, report["issues"]
        assert report["components"], "新装不能是空登记"
        assert report["legacy_chain_versions"] == []
        assert report["legacy_chain_ok"] is None
        assert report["baseline_version"] == baseline.KERNEL_BASELINE_VERSION
        assert report["expected_versions"] == {
            "kernel": [baseline.KERNEL_BASELINE_VERSION]}
        # kernel 表齐了；私人表不该存在，但它们仍在允许白名单里（retained）
        present = set(pg_migrations.table_names(conn))
        assert set(baseline.KERNEL_BASELINE_TABLES) <= present
        assert "ledger_tx" not in present
        assert "ledger_tx" in report["retained_tables"]
        assert set(report["present_tables"]) <= set(report["table_whitelist"])
        assert report["table_owners"] == {
            table: "kernel" for table in baseline.KERNEL_BASELINE_TABLES}
        assert set(report["table_whitelist"]) == (
            set(pg_migrations.SCHEMA_TABLES) | {cm.REGISTRY_TABLE})
    finally:
        conn.close()


def test_new_install_does_not_adopt_existing_unregistered_structure(databases, pg_container):
    conn = _connect(pg_container, databases["new_install"])
    try:
        conn.execute("CREATE TABLE oplog (id INTEGER PRIMARY KEY, injected TEXT)")
        with pytest.raises(cm.ComponentRegistryError, match="空库"):
            with conn.transaction():
                cm.apply_components(conn, components=cm.zero_plugin_components())
        assert cm.registry_rows(conn) is None
    finally:
        conn.close()


def test_new_install_is_idempotent(databases, pg_container):
    components = cm.zero_plugin_components()
    conn = _connect(pg_container, databases["new_install"])
    try:
        cm.apply_components(conn, None, components)
        before = cm.catalog_fingerprint(
            cm.catalog_entries(conn, baseline.KERNEL_BASELINE_TABLES))
        again = cm.apply_components(conn, None, components)
        assert again["applied"] == []
        assert again["skipped"] == [{"component": "kernel",
                                     "version": baseline.KERNEL_BASELINE_VERSION}]
        after = cm.catalog_fingerprint(
            cm.catalog_entries(conn, baseline.KERNEL_BASELINE_TABLES))
        assert before == after
        assert cm.registry_report(conn, components)["ok"] is True
    finally:
        conn.close()


def test_baseline_is_refused_on_legacy_database(databases, pg_container, components):
    """既有库只能 adopt；在遗产链上重放基线 CREATE 必须拒绝（双基线）。"""
    conn = _connect(pg_container, databases["legacy_adopt"])
    try:
        pg_migrations.apply(conn)
        with pytest.raises(cm.ComponentRegistryError, match="只能装进空库"):
            cm.apply_components(conn, None, components)
        assert cm.registry_report(conn, components)["ok"] is False
    finally:
        conn.close()


def test_report_is_read_only_inside_snapshot_transaction(
        databases, pg_container):
    """备份快照路径：REPEATABLE READ + READ ONLY 里报告必须可用且不改任何东西。"""
    components = cm.zero_plugin_components()
    conn = _connect(pg_container, databases["new_install"])
    try:
        cm.apply_components(conn, None, components)
        before = cm.registry_rows(conn)
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            report = cm.registry_report(conn, components)
            assert report["ok"] is True, report["issues"]
            assert report["components"] == [dict(row, metadata={}) for row in before]
            assert report["present_tables"]
        # 只读事务结束后登记行逐字节未变
        assert cm.registry_rows(conn) == before
    finally:
        conn.close()


def test_new_install_registry_survives_app_role_read(
        databases, pg_container):
    """受限运行账号能读登记与 catalog，但不能写登记表。"""
    components = cm.zero_plugin_components()
    conn = _connect(pg_container, databases["new_install"])
    role = f"comp_app_{uuid.uuid4().hex[:6]}"
    admin = pg_test_support.connect(
        pg_container.dsn(dbname=databases["new_install"]), autocommit=True)
    try:
        cm.apply_components(conn, None, components)
        pg_test_support.create_app_role(
            admin, role, "pw-b7-start", tables=baseline.KERNEL_BASELINE_TABLES,
            database=databases["new_install"])
        admin.execute(f'GRANT SELECT ON public.component_schema_migrations TO "{role}"')
        app = pg_test_support.connect(
            pg_container.dsn(dbname=databases["new_install"], user=role,
                             password="pw-b7-start"), autocommit=True)
        try:
            report = cm.registry_report(app, components)
            assert report["ok"] is True, report["issues"]
            assert report["registry_present"] is True
            with pytest.raises(Exception) as failure:
                app.execute(
                    "INSERT INTO component_schema_migrations "
                    "(component, version, checksum) VALUES ('kernel', 9, %s)",
                    ("0" * 64,))
            assert "permission denied" in str(failure.value).lower()
        finally:
            app.close()
    finally:
        pg_test_support.drop_app_role(admin, role)
        admin.close()
        conn.close()


# --- 起点 B：旧库显式接管 ----------------------------------------------------


def test_legacy_adoption_reaches_registry_without_touching_frozen_chain(
        databases, pg_container, components):
    conn = _connect(pg_container, databases["legacy_adopt"])
    try:
        pg_migrations.apply(conn)
        before = {
            version: row["checksum"] for version, row in pg_migrations.applied(conn).items()
        }
        outcome = cm.adopt_legacy(conn, components)
        assert outcome["legacy_versions"] == sorted(before)
        after = {
            version: row["checksum"] for version, row in pg_migrations.applied(conn).items()
        }
        # 冻结链一个字节都没被改
        assert before == after
        assert cm.registry_report(conn, components)["ok"] is True
    finally:
        conn.close()


def test_legacy_adoption_does_not_replay_create_or_seed(
        databases, pg_container, components):
    """接管只读结构：表的数据与存在性都不变（除登记表本身）。"""
    conn = _connect(pg_container, databases["legacy_adopt"])
    try:
        pg_migrations.apply(conn)
        conn.execute("INSERT INTO renewals (id, name, kind) VALUES ('r1', '域名', 'domain')")
        tables_before = set(pg_migrations.table_names(conn))
        cm.adopt_legacy(conn, components)
        tables_after = set(pg_migrations.table_names(conn))
        assert tables_after - tables_before == {cm.REGISTRY_TABLE}
        assert conn.execute("SELECT count(*) AS c FROM renewals").fetchone()["c"] == 1
    finally:
        conn.close()


def test_legacy_adoption_is_idempotent(databases, pg_container, components):
    conn = _connect(pg_container, databases["legacy_adopt"])
    try:
        pg_migrations.apply(conn)
        first = cm.adopt_legacy(conn, components)
        second = cm.adopt_legacy(conn, components)
        assert second["adopted"] == []
        assert sorted(second["skipped"]) == sorted(first["skipped"]) or second["skipped"]
        assert cm.registry_report(conn, components)["ok"] is True
    finally:
        conn.close()


# --- 两条起点收敛 ------------------------------------------------------------


def test_both_startpoints_converge_on_same_kernel_catalog(
        databases, pg_container, components):
    """B7-2 的核心退出条件：两条起点的 kernel 结构完全一致。"""
    new = _connect(pg_container, databases["new_install"])
    legacy = _connect(pg_container, databases["legacy_adopt"])
    try:
        baseline.apply_baseline(new)
        cm.apply_components(new, {}, components)

        pg_migrations.apply(legacy)
        cm.adopt_legacy(legacy, components)

        tables = baseline.KERNEL_BASELINE_TABLES
        new_fp = cm.catalog_fingerprint(cm.catalog_entries(new, tables))
        legacy_fp = cm.catalog_fingerprint(cm.catalog_entries(legacy, tables))
        assert new_fp == legacy_fp
    finally:
        new.close()
        legacy.close()


@pytest.mark.parametrize("start", ["new_install", "legacy_adopt"])
def test_real_v2_upgrade_preserves_receipts_and_rejects_later_drift(
        databases, pg_container, components, monkeypatch, start):
    """真实 ALTER 升级两种起点；不重放基线、不覆盖接管回执，升级后仍能查漂移。"""
    declared = cm.zero_plugin_components() if start == "new_install" else components
    conn = _connect(pg_container, databases[start])
    try:
        if start == "legacy_adopt":
            pg_migrations.apply(conn)
            cm.adopt_legacy(conn, declared)
        else:
            cm.apply_components(conn, components=declared)
        receipts = [row for row in cm.registry_rows(conn) if row["origin"] == "adopted"]
        increment = (2, "synthetic_upgrade", (
            "ALTER TABLE oplog ADD COLUMN synthetic_upgrade TEXT NOT NULL DEFAULT ''",
        ))
        plan = {"kernel": (*cm.COMPONENT_MIGRATIONS["kernel"], increment)}
        monkeypatch.setattr(cm, "COMPONENT_MIGRATIONS", plan)
        with conn.transaction():
            outcome = cm.apply_components(conn, components=declared)
        assert [row["version"] for row in outcome["applied"]] == [2]
        assert [row for row in cm.registry_rows(conn) if row["origin"] == "adopted"] == receipts
        report = cm.registry_report(conn, declared)
        assert report["ok"] is True, report["issues"]
        assert cm.apply_components(conn, components=declared)["applied"] == []
        conn.execute("ALTER TABLE oplog ADD COLUMN unauthorized_drift TEXT")
        report = cm.registry_report(conn, declared)
        assert report["ok"] is False
        assert {issue["kind"] for issue in report["issues"]} >= {"catalog-drift"}
        rows_before = cm.registry_rows(conn)
        monkeypatch.setattr(cm, "COMPONENT_MIGRATIONS", {"kernel": (
            *plan["kernel"], (3, "must_not_apply", ("SELECT 1",)),
        )})
        with pytest.raises(cm.ComponentRegistryError, match="迁移前"):
            with conn.transaction():
                cm.apply_components(conn, components=declared)
        assert cm.registry_rows(conn) == rows_before
    finally:
        conn.close()


# --- fail closed -------------------------------------------------------------


def test_adoption_refuses_when_legacy_checksum_is_tampered(
        databases, pg_container, components):
    conn = _connect(pg_container, databases["legacy_adopt"])
    try:
        pg_migrations.apply(conn)
        conn.execute("UPDATE schema_migrations SET checksum = 'deadbeef' WHERE version = 16")
        with pytest.raises(cm.ComponentRegistryError):
            cm.adopt_legacy(conn, components)
        assert cm.registry_rows(conn) in (None, [])
    finally:
        conn.close()


def test_adoption_refuses_missing_plugin_table(databases, pg_container, components):
    """少一张表就不能登记：回执必须证明「接管时结构是齐的」。"""
    conn = _connect(pg_container, databases["legacy_adopt"])
    try:
        pg_migrations.apply(conn)
        conn.execute("DROP TABLE renewals")  # 隔离库内负例
        with pytest.raises(cm.ComponentRegistryError, match="接管缺表"):
            cm.adopt_legacy(conn, components)
    finally:
        conn.close()


def test_interrupted_adoption_leaves_no_half_registration(
        databases, pg_container, components):
    """接管在事务里：中途失败必须整体回滚，不留一半组件已登记。"""
    conn = _connect(pg_container, databases["legacy_adopt"])
    try:
        pg_migrations.apply(conn)
        original = cm.adoption_receipt
        calls = []

        def failing(*args, **kwargs):
            calls.append(args[0])
            if len(calls) == 3:
                raise KeyboardInterrupt("injected interruption")
            return original(*args, **kwargs)

        cm.adoption_receipt = failing
        try:
            with pytest.raises(KeyboardInterrupt):
                with conn.transaction():
                    cm.adopt_legacy(conn, components)
        finally:
            cm.adoption_receipt = original
        # 事务回滚后不能留下任何组件行。注意 `ensure_registry_table` 也在同一事务
        # 里，所以回滚得比「表在但空」更彻底：`registry_rows` 返回 None（表不存在）。
        # 两种都算「没有半登记」，断言按真实语义写。
        assert cm.registry_rows(conn) in (None, [])
    finally:
        conn.close()


def test_registry_rejects_unknown_component_in_table(databases, pg_container, components):
    conn = _connect(pg_container, databases["legacy_adopt"])
    try:
        pg_migrations.apply(conn)
        cm.adopt_legacy(conn, components)
        conn.execute(
            "INSERT INTO component_schema_migrations "
            "(component, version, description, checksum) VALUES ('plugin:ghost', 1, 'x', %s)",
            ("0" * 64,))
        report = cm.registry_report(conn, components)
        assert report["ok"] is False
        assert {"kind": "unknown-component", "component": "plugin:ghost"} in report["issues"]
    finally:
        conn.close()
