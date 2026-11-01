# -*- coding: utf-8 -*-
"""备份 manifest v2：组合事实块 + v1 兼容（B7-2）。

三条不可退让的约束：

1. **v1 继续可读**：旧备份是现役恢复材料，不能因为加了 v2 就读不出来；
2. **v2 只增不改**：v1 的 14 个键与语义原样保留，新增的四个块必填；
3. **不伪造归属**：不属于任何组件的表标 `retained`，不塞给 kernel。
"""
import json
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
REPO_DIR = BACKEND_DIR.parent.parent
for _path in (BACKEND_DIR, REPO_DIR / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import backup_contracts as pg_contracts  # noqa: E402
import backup_identity  # noqa: E402
import component_migrations as cm  # noqa: E402

SHA_A = "a" * 64
SHA_B = "b" * 64


def _v1_fields():
    return {
        "engine": "postgres",
        "schema_version": 17,
        "schema_checksum": SHA_A,
        "source_commit": "d7b3deb",
        "pg_server_version": "PostgreSQL 16.4",
        "pg_client_version": "pg_dump (PostgreSQL) 16.4",
        "database_identity": "console",
        "started_at": "2026-10-30T12:00:00+08:00",
        "finished_at": "2026-10-30T12:00:05+08:00",
        "members": [{"path": "console.dump.t", "sha256": SHA_B, "bytes": 10}],
        "row_digest": {"algorithm": "md5-pg-row-json-1", "tables": {}, "digest": SHA_B},
        "archive_manifest_version": "reference-manifest-1",
        "status": "complete",
    }


def _composition():
    return {
        "kernel_version": "1.0.0",
        "composition_digest": SHA_A,
        "plugins": [{"id": "ledger", "version": "1.1.0", "digest": SHA_B}],
    }


def _components():
    return [
        {"name": "kernel", "version": 0, "origin": "adopted",
         "checksum": SHA_A, "catalog_fingerprint": SHA_B},
        {"name": "plugin:ledger", "version": 0, "origin": "adopted",
         "checksum": SHA_B, "catalog_fingerprint": SHA_A},
    ]


def _tables():
    return {"oplog": "kernel", "ledger_tx": "plugin:ledger", "panels": "retained"}


def _v2_fields():
    return {**_v1_fields(), "composition": _composition(),
            "components": _components(), "tables": _tables(), "attachments": []}


# --- v1 兼容 ----------------------------------------------------------------


def test_v1_manifest_stays_readable():
    v1 = pg_contracts.build_backup_manifest(**_v1_fields())
    assert v1["manifest_version"] == "pg-backup-manifest-1"
    assert set(v1) == set(pg_contracts.BACKUP_MANIFEST_KEYS)
    assert pg_contracts.read_backup_manifest(v1) is v1


def test_v1_manifest_key_set_is_unchanged():
    """v2 只增不改：v1 的键集合一个都不能变。"""
    assert pg_contracts.BACKUP_MANIFEST_KEYS == (
        "manifest_version", "engine", "schema_version", "schema_checksum",
        "source_commit", "pg_server_version", "pg_client_version",
        "database_identity", "started_at", "finished_at", "members",
        "row_digest", "archive_manifest_version", "status",
    )
    assert set(pg_contracts.BACKUP_MANIFEST_V2_KEYS) == (
        set(pg_contracts.BACKUP_MANIFEST_KEYS)
        | {"composition", "components", "tables", "attachments"}
    )


def test_unknown_manifest_version_is_refused():
    with pytest.raises(pg_contracts.ContractError, match="未知的备份 manifest 版本"):
        pg_contracts.read_backup_manifest({"manifest_version": "pg-backup-manifest-9"})
    with pytest.raises(pg_contracts.ContractError):
        pg_contracts.read_backup_manifest({})


def test_v1_with_extra_key_is_refused():
    """v1 多出未知键说明它不是 v1：不能悄悄当 v1 读。"""
    bad = {**_v1_fields(), "manifest_version": "pg-backup-manifest-1", "extra": 1}
    with pytest.raises(pg_contracts.ContractError, match="字段集合不符"):
        pg_contracts.read_backup_manifest(bad)


# --- v2 构造与校验 ----------------------------------------------------------


def test_v2_manifest_roundtrip():
    manifest = pg_contracts.build_backup_manifest_v2(**_v2_fields())
    assert manifest["manifest_version"] == "pg-backup-manifest-2"
    assert set(manifest) == set(pg_contracts.BACKUP_MANIFEST_V2_KEYS)
    assert pg_contracts.read_backup_manifest(manifest) == manifest
    json.dumps(manifest)  # 必须可安全序列化


@pytest.mark.parametrize("missing", ["composition", "components", "tables", "attachments"])
def test_v2_requires_every_new_block(missing):
    fields = _v2_fields()
    fields.pop(missing)
    with pytest.raises(pg_contracts.ContractError, match="必填字段为空"):
        pg_contracts.build_backup_manifest_v2(**fields)


def test_v2_attachments_must_be_array_not_null():
    """无附件组合必须写空数组：null 会让「没备份」和「没有附件」不可区分。"""
    fields = _v2_fields()
    fields["attachments"] = None
    with pytest.raises(pg_contracts.ContractError):
        pg_contracts.build_backup_manifest_v2(**fields)


def test_v2_adopted_component_requires_catalog_fingerprint():
    fields = _v2_fields()
    fields["components"] = [
        {"name": "kernel", "version": 0, "origin": "adopted",
         "checksum": SHA_A, "catalog_fingerprint": None},
    ]
    with pytest.raises(pg_contracts.ContractError, match="adopted 回执必须带 catalog_fingerprint"):
        pg_contracts.build_backup_manifest_v2(**fields)


def test_v2_applied_component_may_omit_fingerprint():
    fields = _v2_fields()
    fields["components"] = [
        {"name": "kernel", "version": 1, "origin": "applied",
         "checksum": SHA_A, "catalog_fingerprint": None},
    ]
    manifest = pg_contracts.build_backup_manifest_v2(**fields)
    assert manifest["components"][0]["catalog_fingerprint"] is None


def test_v2_rejects_duplicate_component_versions():
    fields = _v2_fields()
    entry = {"name": "kernel", "version": 0, "origin": "adopted",
             "checksum": SHA_A, "catalog_fingerprint": SHA_B}
    fields["components"] = [entry, dict(entry)]
    with pytest.raises(pg_contracts.ContractError, match="重复组件版本"):
        pg_contracts.build_backup_manifest_v2(**fields)


def test_v2_rejects_unknown_table_owner():
    fields = _v2_fields()
    fields["tables"] = {"oplog": "somebody_else"}
    with pytest.raises(pg_contracts.ContractError, match="归属无效"):
        pg_contracts.build_backup_manifest_v2(**fields)


def test_v2_allows_retained_table_owner():
    """`panels` 与登记表不属于任何组件，标 retained 是事实而不是缺陷。"""
    fields = _v2_fields()
    fields["tables"] = {"oplog": "kernel", "panels": "retained",
                        "schema_migrations": "retained"}
    manifest = pg_contracts.build_backup_manifest_v2(**fields)
    assert manifest["tables"]["panels"] == "retained"


def test_v2_rejects_incomplete_status():
    fields = _v2_fields()
    fields["status"] = "moving"
    with pytest.raises(pg_contracts.ContractError, match="status 必须是 complete"):
        pg_contracts.build_backup_manifest_v2(**fields)


def test_v2_rejects_tampered_plugin_digest():
    fields = _v2_fields()
    fields["composition"] = {
        "kernel_version": "1.0.0",
        "composition_digest": SHA_A,
        "plugins": [{"id": "ledger", "version": "1.1.0", "digest": "not-a-sha"}],
    }
    with pytest.raises(pg_contracts.ContractError, match="SHA256"):
        pg_contracts.build_backup_manifest_v2(**fields)


# --- 事实块收集 -------------------------------------------------------------


def test_composition_block_from_identity():
    identity = {
        "schema_version": 1,
        "kernel": {"version": "1.0.0", "digest": SHA_A},
        "plugins": [{"id": "ledger", "version": "1.1.0", "digest": SHA_B}],
        "composition_digest": SHA_B,
    }
    block = backup_identity.composition_block(identity)
    assert block == {
        "kernel_version": "1.0.0",
        "composition_digest": SHA_B,
        "plugins": [{"id": "ledger", "version": "1.1.0", "digest": SHA_B}],
    }


def test_composition_block_absent_returns_none():
    """现役私人部署还没接构建身份：不能因此让备份失败。"""
    assert backup_identity.composition_block(None) is None


def test_components_and_tables_from_registry_report():
    report = {
        "ok": True,
        "table_owners": {"oplog": "kernel", "ledger_tx": "plugin:ledger"},
        "table_whitelist": ["ledger_tx", "oplog", "panels",
                            "schema_migrations", "component_schema_migrations"],
        "present_tables": ["ledger_tx", "oplog", "panels", "schema_migrations",
                           "component_schema_migrations"],
        "retained_tables": ["panels", "schema_migrations", "component_schema_migrations"],
        "components": [
            {"component": "plugin:ledger", "version": 0, "origin": "adopted",
             "checksum": SHA_B, "catalog_fingerprint": SHA_A,
             "applied_at": "x", "description": "d", "metadata": {}},
            {"component": "kernel", "version": 0, "origin": "adopted",
             "checksum": SHA_A, "catalog_fingerprint": SHA_B,
             "applied_at": "x", "description": "d", "metadata": {}},
        ],
    }
    components = backup_identity.components_block(report)
    assert [row["name"] for row in components] == ["kernel", "plugin:ledger"]
    assert all(row["origin"] == "adopted" for row in components)

    tables = backup_identity.tables_block(report)
    assert tables["oplog"] == "kernel"
    assert tables["ledger_tx"] == "plugin:ledger"
    # 不属于任何组件的表如实标 retained，不伪造 kernel 归属
    for table in ("panels", "schema_migrations", "component_schema_migrations"):
        assert tables[table] == "retained", table


def test_tables_block_rejects_owner_outside_whitelist():
    report = {"table_owners": {"oplog": "kernel"}, "table_whitelist": [], "components": [],
              "present_tables": ["oplog"], "retained_tables": []}
    with pytest.raises(ValueError, match="不在白名单内"):
        backup_identity.tables_block(report)


def test_attachments_block_is_sorted_and_empty_when_absent():
    assert backup_identity.attachments_block(None) == []
    assert backup_identity.attachments_block([]) == []
    rows = backup_identity.attachments_block([
        {"path": "b.pay.tar", "sha256": SHA_B, "bytes": 2},
        {"path": "a.pay.tar", "sha256": SHA_A, "bytes": 1},
    ])
    assert [row["path"] for row in rows] == ["a.pay.tar", "b.pay.tar"]


def test_real_registry_report_feeds_v2_blocks(pg_container):
    """端到端：真实接管后的登记报告能直接构成合法的 v2 事实块。"""
    import pg_migrations
    import pg_test_support

    admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
    admin.execute('CREATE DATABASE "b7_manifest_v2"')
    try:
        conn = pg_test_support.connect(
            pg_container.dsn(dbname="b7_manifest_v2"), autocommit=True)
        try:
            pg_migrations.apply(conn)
            components = cm.declared_components(REPO_DIR / "plugins")
            cm.adopt_legacy(conn, components)
            report = cm.registry_report(conn, components)
            assert report["ok"] is True

            fields = _v2_fields()
            fields["components"] = backup_identity.components_block(report)
            fields["tables"] = backup_identity.tables_block(report)
            fields["composition"] = backup_identity.composition_block({
                "schema_version": 1,
                "kernel": {"version": "1.0.0", "digest": SHA_A},
                "plugins": [],
                "composition_digest": SHA_B,
            })
            manifest = pg_contracts.build_backup_manifest_v2(**fields)
            assert pg_contracts.read_backup_manifest(manifest) == manifest
            # 全表清单必须覆盖库里的每一张表，额外表才可能被发现
            assert set(manifest["tables"]) >= set(pg_migrations.table_names(conn))
        finally:
            conn.close()
    finally:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            ("b7_manifest_v2",))
        admin.execute('DROP DATABASE IF EXISTS "b7_manifest_v2"')
        admin.close()
