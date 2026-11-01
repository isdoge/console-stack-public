# -*- coding: utf-8 -*-
"""组件感知校验器：`require_component_schema` vs `require_schema12`（B7-2）。

这是 B7-2 的已知交互收口：接管后的库多了一张 `component_schema_migrations`，
旧校验器按「冻结链全表全集」判它越界。两种校验器各有明确适用阶段，不能合并：

- `require_schema12`：目标**尚未接管**（SQLite→PG 迁库链）；
- `require_component_schema`：目标已接管或已安装组件。
"""
import pathlib
import sys
import uuid

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "console/backend"))

import component_migrations as cm  # noqa: E402
import pg_migration_lib  # noqa: E402
import pg_migrations  # noqa: E402
import pg_test_support  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture()
def db(pg_container):
    admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
    name = f"b7ver_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield name
    finally:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            (name,))
        admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.close()


@pytest.fixture()
def conn(pg_container, db):
    connection = pg_test_support.connect(pg_container.dsn(dbname=db), autocommit=True)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def components():
    return cm.declared_components(REPO / "plugins")


def test_old_verifier_rejects_registry_table_after_adoption(conn, components):
    """旧校验器把登记表判成越界——这是已知交互，必须能被复现。"""
    pg_migrations.apply(conn)
    cm.adopt_legacy(conn, components)
    with pytest.raises(pg_migration_lib.TargetGuardFailure, match="预期外表"):
        pg_migration_lib.require_schema12(conn)


def test_component_verifier_accepts_adopted_database(conn, components):
    """组件感知校验器认可已接管的库。"""
    pg_migrations.apply(conn)
    cm.adopt_legacy(conn, components)
    report = pg_migration_lib.require_component_schema(conn, components)
    assert report["checksum_ok"] is True
    assert report["schema_version"] == pg_migrations.latest_schema_version()


@pytest.mark.parametrize("plugins_removed", [False, True], ids=["private", "retained"])
def test_adopted_database_requires_frozen_legacy_chain(conn, components, plugins_removed):
    """接管证明依赖冻结链；整张版本表被移走也不能伪装成公共新库。"""
    pg_migrations.apply(conn)
    cm.adopt_legacy(conn, components)
    selected = cm.zero_plugin_components() if plugins_removed else components
    assert cm.registry_report(conn, selected)["ok"] is True
    conn.execute("DROP TABLE schema_migrations")
    report = cm.registry_report(conn, selected)
    assert report["ok"] is False
    assert report["legacy_chain_ok"] is False
    assert "legacy-chain-incomplete" in {issue["kind"] for issue in report["issues"]}
    with pytest.raises(pg_migration_lib.TargetGuardFailure, match="legacy-chain-incomplete"):
        pg_migration_lib.require_component_schema(conn, selected)


def test_component_verifier_still_rejects_real_stray_tables(conn, components):
    """放宽的只有白名单内的表：真正的越界表仍然必须失败。"""
    pg_migrations.apply(conn)
    cm.adopt_legacy(conn, components)
    conn.execute("CREATE TABLE stray_table (id int)")
    with pytest.raises(pg_migration_lib.TargetGuardFailure, match="预期外表"):
        pg_migration_lib.require_component_schema(conn, components)


def test_component_verifier_rejects_incomplete_registry(conn, components):
    """遗产链在库但没接管：组件登记不完整，必须拒绝。"""
    pg_migrations.apply(conn)
    with pytest.raises(pg_migration_lib.TargetGuardFailure, match="组件登记不完整"):
        pg_migration_lib.require_component_schema(conn, components)


def test_component_verifier_rejects_catalog_drift(conn, components):
    pg_migrations.apply(conn)
    cm.adopt_legacy(conn, components)
    conn.execute("ALTER TABLE renewals ADD COLUMN surprise TEXT")
    with pytest.raises(pg_migration_lib.TargetGuardFailure, match="组件登记不完整"):
        pg_migration_lib.require_component_schema(conn, components)


def test_component_verifier_rejects_missing_tables(conn, components):
    pg_migrations.apply(conn)
    conn.execute("DROP TABLE renewals")
    with pytest.raises(pg_migration_lib.TargetGuardFailure, match="未初始化，缺表"):
        pg_migration_lib.require_component_schema(conn, components)


def test_component_verifier_without_components_is_old_behaviour(conn, components):
    """不传组件时退化为旧判据：登记表仍算越界（显式选择，不隐式放宽）。"""
    pg_migrations.apply(conn)
    cm.adopt_legacy(conn, components)
    with pytest.raises(pg_migration_lib.TargetGuardFailure, match="预期外表"):
        pg_migration_lib.require_component_schema(conn)


def test_schema12_verifier_unchanged_for_unadopted_target(conn):
    """迁库阶段的目标还没接管，旧校验器继续按原样通过。"""
    pg_migrations.apply(conn)
    report = pg_migration_lib.require_schema12(conn)
    assert report["tables_unexpected"] == []
