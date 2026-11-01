# -*- coding: utf-8 -*-
"""组件迁移登记与遗产链接管回执（B7-2 隔离验证批次）。

两段式：前半是**纯逻辑**（不连库，任何环境都跑）；后半用 `pg_dsn` 起真 PG16，
在独立库里跑 `pg_migrations.apply()` 合成「schema17 既有库」，再验证登记、接管
回执、幂等、漂移与负例。

`pg_dsn` 找不到 PG16 时 `pg_test_support` 会 `pytest.fail` 而不是 skip——本文件
不提供任何「跳过就算过」的分支。
"""
import json
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
import kernel_baseline  # noqa: E402
import pg_migrations  # noqa: E402
import pg_test_support  # noqa: E402

PLUGIN_TABLES = {
    "audit": (),
    "backup_evidence": ("backup_evidence_receipts", "backup_evidence_incidents"),
    "feed": ("bookmarks", "feed_cache"),
    "ledger": (
        "ledger_tx", "ledger_tx_link", "ledger_categories", "ledger_knowledge",
        "ledger_imports", "ledger_file_batches",
    ),
    "panels": ("panel_scratch",),
    "personal_workbench": (),
    "renewals": ("renewals",),
}
KERNEL_TABLES = (
    "dashboard_layout", "machine_traffic", "monitor_events", "monitoring_hourly",
    "monitoring_machines", "monitoring_samples", "oplog", "probe_history",
    "service_health_history", "service_upgrade_reviews", "versions",
)


# --- 纯逻辑：组件来源与白名单 -------------------------------------------------


def test_real_plugins_derive_expected_components():
    """真实 `plugins/` 组合 → kernel 11 表 + 六个有表插件 + 两张保留表。"""
    components = cm.declared_components(REPO_DIR / "plugins")
    assert components["kernel"] == KERNEL_TABLES
    assert set(components) == {"kernel"} | {cm.plugin_component(p) for p in PLUGIN_TABLES}
    assert components["plugin:ledger"] == tuple(sorted(PLUGIN_TABLES["ledger"]))
    assert components["plugin:audit"] == ()
    # 冻结全集里不放丢表：所有声明表 + panels + 两张登记表 = 白名单
    whitelist = cm.table_whitelist(components)
    assert set(whitelist) == (
        set(pg_migrations.SCHEMA_TABLES)
        | {cm.REGISTRY_TABLE}
    )
    assert "panels" in whitelist and "panels" not in cm.table_owners(components)
    assert cm.table_owners(components)["ledger_tx"] == "plugin:ledger"
    assert cm.table_owners(components)["oplog"] == "kernel"


def test_zero_plugin_kernel_tables_are_stable():
    """零插件组合不能把私人表回收成 kernel：kernel 恒为 11 张。"""
    zero = cm.component_table_map({})
    assert zero == {"kernel": KERNEL_TABLES}
    private = set(pg_migrations.SCHEMA_TABLES) - set(KERNEL_TABLES)
    assert not (set(zero["kernel"]) & private)
    # 未安装插件的历史表 + panels + 两张登记表进 retained，且都在白名单里
    retained = set(cm.retained_tables(zero))
    assert {"ledger_tx", "renewals", "bookmarks", "panel_scratch",
            "backup_evidence_receipts", "panels",
            "schema_migrations", "component_schema_migrations"} <= retained
    assert retained.isdisjoint(set(zero["kernel"]))
    assert retained <= set(cm.table_whitelist(zero))


def test_every_installed_plugin_keeps_same_frozen_ownership():
    """装齐插件与零插件的 kernel 完全相同：归属只由冻结事实决定。"""
    full = cm.declared_components(REPO_DIR / "plugins")
    assert full["kernel"] == cm.component_table_map({})["kernel"] == KERNEL_TABLES
    assert cm.retained_tables(full) == (
        "component_schema_migrations", "panels", "schema_migrations")


def test_plugin_manifest_drift_from_frozen_ownership_fails_closed():
    """清单声明与冻结遗产归属不一致（多退少补）必须拒绝，不能悄悄改归属。"""
    with pytest.raises(cm.ComponentRegistryError, match="冻结遗产归属"):
        cm.component_table_map({"renewals": ("renewals", "bookmarks")})
    with pytest.raises(cm.ComponentRegistryError, match="冻结遗产归属"):
        cm.component_table_map({"ledger": ("ledger_tx",)})


def test_unknown_plugin_table_fails_closed():
    with pytest.raises(cm.ComponentRegistryError):
        cm.component_table_map({"ledger": ("ledger_tx", "not_a_real_table")})


def test_two_plugins_claiming_same_table_fails_closed():
    with pytest.raises(cm.ComponentRegistryError):
        cm.component_table_map({"renewals": ("renewals",), "feed": ("renewals",)})


def test_official_baseline_version_and_checksum_are_frozen():
    """正式基线版本号与指纹一起冻结：改 DDL 必须加新版本，不能原地改。"""
    assert cm.KERNEL_BASELINE_VERSION == kernel_baseline.KERNEL_BASELINE_VERSION == 1
    assert kernel_baseline.KERNEL_BASELINE_CHECKSUM == pg_migrations.checksum(
        kernel_baseline.KERNEL_BASELINE_SQL)
    expected = cm.official_expected()
    assert expected == {
        "kernel": {1: kernel_baseline.KERNEL_BASELINE_CHECKSUM},
    }
    # 组件链只引用 baseline 的 SQL，不复制第二份 DDL
    plan = cm.COMPONENT_MIGRATIONS["kernel"]
    assert [item[0] for item in plan] == [1]
    assert plan[0][2] is kernel_baseline.KERNEL_BASELINE_SQL


def test_manifest_owners_reject_conflicting_manifest(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    (root / "contract.json").write_text(json.dumps({"api_version": 1}), encoding="utf-8")
    (root / "composition.json").write_text(
        json.dumps({"plugins": ["a", "b"]}), encoding="utf-8")
    for pid in ("a", "b"):
        (root / pid).mkdir()
        (root / pid / "manifest.json").write_text(
            json.dumps({"id": pid, "owns": {"tables": ["renewals"]}}), encoding="utf-8")
    with pytest.raises(cm.ComponentRegistryError):
        cm.manifest_table_owners(root)


def test_component_names_are_bounded():
    assert cm.is_component_name("kernel")
    assert cm.is_component_name("plugin:ledger")
    for bad in ("", "Kernel", "plugin:", "plugin:Bad", "other:ledger", None, 3):
        assert not cm.is_component_name(bad), bad
    with pytest.raises(cm.UnknownComponent):
        cm.plugin_component("../ledger")


# --- 纯逻辑：登记校验 ---------------------------------------------------------


def _components():
    return cm.component_table_map(PLUGIN_TABLES)


def _receipt(component=None, tables=None, **overrides):
    components = _components()
    component = component or "kernel"
    tables = sorted(tables or components[component])
    versions, checksums = [12, 13], {"12": "a" * 64, "13": "b" * 64}
    receipt = cm.adoption_receipt(
        component, tables, versions, checksums, "f" * 64,
    )
    receipt["applied_at"] = "2026-10-30 00:00:00"
    receipt.update(overrides)
    return receipt


def _all_tables():
    return sorted(set(pg_migrations.SCHEMA_TABLES) | {cm.REGISTRY_TABLE})


def test_receipt_roundtrip_is_valid():
    normalized = cm.validate_receipt(_receipt())
    assert normalized["version"] == cm.ADOPTION_VERSION == 0
    assert normalized["metadata"]["adopted_from"] == "legacy-pg-chain"
    assert normalized["metadata"]["legacy_checksums"] == {"12": "a" * 64, "13": "b" * 64}


def test_tampered_receipt_checksum_is_rejected():
    receipt = _receipt()
    receipt["metadata"] = dict(receipt["metadata"], legacy_versions=[12])
    with pytest.raises(cm.ReceiptError):
        cm.validate_receipt(receipt)


def test_receipt_that_was_never_adopted_is_rejected():
    """`applied` 行不能伪装成回执（oriented check 由 REGISTRY_DDL 兜住）。"""
    receipt = _receipt()
    receipt["version"] = 1
    with pytest.raises(cm.ReceiptError):
        cm.validate_receipt(receipt)


def test_plugin_removed_after_adoption_becomes_retained_not_unknown():
    """移出插件源码后，合法接管回执覆盖的冻结表是 retained，不是未知组件。"""
    components = _components()
    receipts = [_receipt(name) for name in components if components[name]]
    without = {k: v for k, v in components.items() if k != "plugin:ledger"}
    check = cm.check_registry(
        receipts, without, _all_tables(), legacy_chain_present=True, expected={})
    assert check == {"ok": True, "issues": []}
    assert cm.retained_component_tables(receipts, without) == {
        "plugin:ledger": sorted(cm.FROZEN_PLUGIN_TABLES["ledger"])}


def test_component_without_valid_receipt_is_still_unknown():
    """没有任何合法回执的额外组件仍然 fail closed（不能借 retained 通道放过）。"""
    ghost = {"component": "plugin:ghost", "version": 1, "origin": cm.ORIGIN_APPLIED,
             "checksum": "0" * 64, "catalog_fingerprint": None, "metadata": {}}
    check = cm.check_registry(
        [_receipt()] + [ghost], _components(), _all_tables(), legacy_chain_present=True)
    assert {"kind": "unknown-component", "component": "plugin:ghost"} in check["issues"]


def test_check_reports_missing_and_extra_tables():
    present = [t for t in _all_tables() if t != "oplog"] + ["stray_table"]
    check = cm.check_registry([], _components(), present, legacy_chain_present=False,
                              expected={})
    assert {issue["kind"] for issue in check["issues"]} == {"missing-table", "extra-table"}
    assert {"kind": "missing-table", "component": "kernel", "table": "oplog"} in check["issues"]
    assert {"kind": "extra-table", "table": "stray_table"} in check["issues"]


def test_check_without_legacy_chain_needs_no_receipts():
    check = cm.check_registry([], _components(), _all_tables(), legacy_chain_present=False,
                              expected={})
    assert check == {"ok": True, "issues": []}


def test_missing_registry_is_explicit_failure():
    """空库没跑 migrate：报告必须点明登记表缺失，不能靠别的 issue 顺带失败。"""
    check = cm.check_registry([], cm.zero_plugin_components(), [], legacy_chain_present=False,
                              expected={}, registry_present=False)
    assert {"kind": "registry-missing"} in check["issues"]
    assert check["ok"] is False


def test_check_flags_missing_receipt_on_legacy_chain():
    """有遗产链但某组件没回执 → 不能把「没登记」当成「已接管」。"""
    only_kernel = _receipt()
    check = cm.check_registry(
        [only_kernel], _components(), _all_tables(), legacy_chain_present=True,
        expected={})
    missing = {issue["component"] for issue in check["issues"]
               if issue["kind"] == "adoption-receipt-missing"}
    assert "plugin:ledger" in missing
    assert "kernel" not in missing


def test_check_detects_catalog_drift_only_for_coverage():
    receipt = _receipt()
    catalogs = {"kernel": "0" * 64}
    check = cm.check_registry(
        [receipt], _components(), _all_tables(), legacy_chain_present=True,
        catalogs=catalogs, expected={})
    assert {"kind": "catalog-drift", "component": "kernel"} in check["issues"]


def test_adopted_plus_baseline_applied_is_double_baseline():
    """接管库再登记基线 v1 就是双基线：回执已经覆盖它，不允许重放。"""
    receipt = _receipt()
    applied = {"component": "kernel", "version": cm.KERNEL_BASELINE_VERSION,
               "origin": cm.ORIGIN_APPLIED, "checksum": "0" * 64,
               "catalog_fingerprint": None, "metadata": {}}
    check = cm.check_registry(
        [receipt, applied], _components(), _all_tables(), legacy_chain_present=True,
        expected={"kernel": {cm.KERNEL_BASELINE_VERSION: "1" * 64}})
    kinds = {issue["kind"] for issue in check["issues"]}
    assert "double-baseline" in kinds
    assert "checksum-drift" in kinds


def _all_receipts():
    components = _components()
    return [_receipt(name) for name in components if components[name]]


def test_adopted_plus_legal_increment_from_v2_is_clean():
    """接管回执 + 后续合法增量（v2 起）必须通过，不能把合法升级当双基线。"""
    increment = {"component": "kernel", "version": 2, "origin": cm.ORIGIN_APPLIED,
                 "checksum": "2" * 64, "catalog_fingerprint": None, "metadata": {}}
    check = cm.check_registry(
        _all_receipts() + [increment], _components(), _all_tables(),
        legacy_chain_present=True,
        expected={"kernel": {cm.KERNEL_BASELINE_VERSION: "1" * 64, 2: "2" * 64}})
    assert check == {"ok": True, "issues": []}


def test_unknown_applied_version_is_rejected():
    """登记了期望版本集之外的版本（伪造 999）必须拒绝，不能只看缺失版本。"""
    bogus = {"component": "kernel", "version": 999, "origin": cm.ORIGIN_APPLIED,
             "checksum": "0" * 64, "catalog_fingerprint": None, "metadata": {}}
    check = cm.check_registry(
        _all_receipts() + [bogus], _components(), _all_tables(),
        legacy_chain_present=True, expected={})
    assert {"kind": "unknown-version", "component": "kernel", "version": 999} in check["issues"]
    assert check["ok"] is False


def test_checksum_drift_on_expected_version_is_rejected():
    receipt = _receipt()
    applied = {"component": "kernel", "version": 2, "origin": cm.ORIGIN_APPLIED,
               "checksum": "9" * 64, "catalog_fingerprint": None, "metadata": {}}
    check = cm.check_registry(
        [receipt, applied], _components(), _all_tables(), legacy_chain_present=True,
        expected={"kernel": {2: "2" * 64}})
    assert {"kind": "checksum-drift", "component": "kernel", "version": 2} in check["issues"]


def test_legacy_chain_identity_is_reported_as_not_ok():
    """遗产链版本/指纹与冻结代码不符时，报告必须失败而不是只列版本号。"""
    receipt = _receipt()
    check = cm.check_registry(
        [receipt], _components(), _all_tables(), legacy_chain_present=True, expected={},
        legacy_identity={"versions_missing": [17], "versions_unknown": [],
                         "checksum_mismatches": [16]})
    kinds = {issue["kind"] for issue in check["issues"]}
    assert {"legacy-chain-incomplete", "legacy-chain-drift"} <= kinds
    assert check["ok"] is False


def test_check_flags_invalid_receipt_and_receipt_table_drift():
    tampered = dict(_receipt(), checksum="0" * 64)
    drifted = _receipt("plugin:feed", tables=["bookmarks"])
    check = cm.check_registry(
        [tampered, drifted], _components(), _all_tables(), legacy_chain_present=True,
        expected={})
    kinds = {issue["kind"] for issue in check["issues"]}
    assert "invalid-receipt" in kinds
    assert "receipt-tables-drift" in kinds


def test_migration_plan_rejects_undeclared_component_and_bad_versions():
    with pytest.raises(cm.UnknownComponent):
        cm._plan({"plugin:ghost": ((1, "x", ("CREATE TABLE x (id int)",)),)}, _components())
    with pytest.raises(cm.ComponentRegistryError):
        cm._plan({"kernel": ((0, "x", ("SELECT 1",)),)}, _components())
    with pytest.raises(cm.ComponentRegistryError):
        cm._plan({"kernel": ((2, "x", ("SELECT 1",)), (1, "y", ("SELECT 1",)))}, _components())
    ok = cm._plan({"kernel": ((1, "x", ("SELECT 1",)),)}, _components())
    assert ok["kernel"][0][0] == 1


def test_expected_from_migrations_is_pure_and_matches_released_chain():
    """冻结遗产链的指纹仍由 `pg_migrations` 唯一提供，本模块不改也不重算它。"""
    expected = cm.expected_from_migrations(component_migrations_plan())
    assert set(expected["kernel"]) == {1, 2}
    assert expected["kernel"][1] == pg_migrations.checksum(("SELECT 1",))


def component_migrations_plan():
    return {"kernel": ((1, "a", ("SELECT 1",)), (2, "b", ("SELECT 2",)))}


def test_catalog_fingerprint_is_order_insensitive_but_content_sensitive():
    a = {"t": {"columns": [["id", "integer", True, None]],
               "constraints": [], "indexes": []}}
    b = json.loads(json.dumps(a))
    assert cm.catalog_fingerprint(a) == cm.catalog_fingerprint(b)
    b["t"]["columns"][0][1] = "bigint"
    assert cm.catalog_fingerprint(a) != cm.catalog_fingerprint(b)


def test_registry_ddl_freezes_origin_and_receipt_contract():
    text = cm.REGISTRY_DDL
    assert "PRIMARY KEY (component, version)" in text
    assert "CHECK ((version = 0) = (origin = 'adopted'))" in text
    assert "component <> ''" in text
    assert "catalog_fingerprint IS NOT NULL" in text
    # 本批不预占遗产链下一号：登记表与继承版本号无关
    assert "18" not in text
    assert "schema_migrations" not in text.replace("component_schema_migrations", "")


# --- 真 PG16：合成 schema17 既有库 --------------------------------------------


@pytest.fixture()
def fresh_db(pg_container):
    admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
    name = f"component_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield name
    finally:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            (name,),
        )
        admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.close()


@pytest.fixture()
def schema_conn(pg_container, fresh_db):
    """合成「私人既有库」：只跑冻结遗产链 12–17，不跑本模块任何 DDL。"""
    conn = pg_test_support.connect(pg_container.dsn(dbname=fresh_db), autocommit=True)
    pg_migrations.apply(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def components():
    return cm.declared_components(REPO_DIR / "plugins")


def test_empty_database_has_no_registry_not_a_false_pass(schema_conn, components):
    report = cm.registry_report(schema_conn, components)
    assert report["registry_present"] is False
    # 遗产链已完整 → 组件还没接管回执，必须明确失败而不是空表通过
    assert report["ok"] is False
    assert "adoption-receipt-missing" in {i["kind"] for i in report["issues"]}
    assert report["legacy_chain_versions"] == [v for v, _, _ in pg_migrations.PG_MIGRATIONS]


def test_apply_components_is_idempotent_and_matches_released_legacy_chain(
        schema_conn, components):
    first = cm.apply_components(schema_conn, {}, components)
    assert first["applied"] == []
    second = cm.apply_components(schema_conn, {}, components)
    assert second["applied"] == []
    rows = cm.registry_rows(schema_conn)
    assert rows == []  # 只建了登记表，没有登记任何组件版本
    # 冻结遗产链没有被写：版本与指纹仍是 `pg_migrations` 的
    assert cm._legacy_checksums(schema_conn)[1] == {
        str(version): pg_migrations.checksum(statements)
        for version, _description, statements in pg_migrations.PG_MIGRATIONS
    }


def test_adopt_records_receipts_without_replaying_ddl(schema_conn, components):
    before = pg_migrations.verify(schema_conn)
    outcome = cm.adopt_legacy(schema_conn, components)
    assert {row["component"] for row in outcome["adopted"]} == {
        component for component, tables in components.items() if tables
    }
    after = pg_migrations.verify(schema_conn)
    # 不 DROP、不重放 CREATE/seed：表清单只多了登记表本身，其余逐项相同。
    # `pg_migrations.verify()` 是冻结遗产链的视角，它不认识组件登记表，
    # 因此会把它报成 `tables_unexpected`——`scripts/pg_migration_lib.require_schema12`
    # 也按这一项拒绝。这是 B7-2 剩余接线（切换校验器）必须显式处理的已知交互。
    assert after["tables_unexpected"] == [cm.REGISTRY_TABLE]
    assert {k: v for k, v in after.items() if k != "tables_unexpected"} == {
        k: v for k, v in before.items() if k != "tables_unexpected"}
    rows = cm.registry_rows(schema_conn)
    for row in rows:
        assert row["origin"] == cm.ORIGIN_ADOPTED
        assert row["version"] == cm.ADOPTION_VERSION
        assert row["catalog_fingerprint"]
    report = cm.registry_report(schema_conn, components)
    assert report["ok"] is True, report["issues"]
    assert report["legacy_chain_versions"] == [v for v, _, _ in pg_migrations.PG_MIGRATIONS]


def test_adopt_is_idempotent(schema_conn, components):
    assert len(cm.adopt_legacy(schema_conn, components)["adopted"]) == 6
    again = cm.adopt_legacy(schema_conn, components)
    assert again["adopted"] == []
    assert sorted(again["skipped"]) == sorted(
        component for component, tables in components.items() if tables)
    assert cm.registry_report(schema_conn, components)["ok"] is True


def test_adopt_fails_closed_when_a_table_is_missing(schema_conn, components):
    schema_conn.execute("DROP TABLE renewals")  # 隔离库内负例，不是生产
    with pytest.raises(cm.ComponentRegistryError):
        cm.adopt_legacy(schema_conn, components)


@pytest.mark.parametrize("drift", [
    "ALTER TABLE renewals ADD COLUMN b7_unexpected TEXT",
    "ALTER TABLE ledger_tx ADD COLUMN b7_extra NUMERIC",
    "ALTER TABLE oplog ALTER COLUMN operator DROP DEFAULT",
    "ALTER TABLE monitoring_samples ALTER COLUMN source SET DEFAULT 'x'",
    "DROP INDEX idx_probe_history_machine",
    "ALTER TABLE service_health_history ALTER COLUMN ts DROP NOT NULL",
    "ALTER TABLE monitor_events ALTER COLUMN severity TYPE VARCHAR(32)",
])
def test_adopt_rejects_pre_existing_structural_drift_before_receipts(
        schema_conn, components, drift):
    """接管**之前**就已漂移的结构必须拒绝，且不写任何回执。

    这是 R02 的核心：旧测试只覆盖「接管之后再改」，而真实风险是首次接管把
    已经偏离标准的结构合法化。
    """
    schema_conn.execute(drift)  # 隔离库内负例，不是生产
    with pytest.raises(cm.ComponentRegistryError, match="结构与冻结遗产链标准不一致"):
        cm.adopt_legacy(schema_conn, components)
    assert cm.registry_rows(schema_conn) == []


def test_verify_legacy_structure_reports_component_scoped_differences(
        schema_conn, components):
    schema_conn.execute("ALTER TABLE renewals ADD COLUMN b7_unexpected TEXT")
    result = cm.verify_legacy_structure(schema_conn, components)
    assert result["ok"] is False
    assert {(d["component"], d["table"], d["key"]) for d in result["differences"]} == {
        ("plugin:renewals", "renewals", "columns")}
    # 比对结束后没有残留的一次性 schema
    leftovers = schema_conn.execute(
        "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'b7_standard_%'").fetchall()
    assert leftovers == []


def test_zero_plugin_ownership_on_legacy_database_reports_retained(
        schema_conn, components):
    """零插件组合在既有私人库上：kernel 仍 11 表，其余历史表显式 retained。

    接管必须用完整组合（否则缺回执）；这里验证「接管后移出全部插件」的报告
    与备份 owners 仍然完整，不把私人表回收成 kernel、也不判未知组件。
    """
    cm.adopt_legacy(schema_conn, components)
    zero = cm.component_table_map({})
    report = cm.registry_report(schema_conn, zero)
    assert report["ok"] is True, report["issues"]
    owners = report["table_owners"]
    assert set(owners) == set(cm.table_whitelist(zero)) - set(report["retained_tables"])
    assert set(owners) & set(KERNEL_TABLES) == set(KERNEL_TABLES)
    assert "ledger_tx" not in owners and "ledger_tx" in report["retained_tables"]
    # 已移出插件的接管组件仍保留归属：不判 unknown，也不回收成 kernel
    retained = report["retained_components"]
    assert set(retained) == {
        "plugin:ledger", "plugin:renewals", "plugin:feed", "plugin:panels",
        "plugin:backup_evidence"}
    assert retained["plugin:ledger"] == sorted(cm.FROZEN_PLUGIN_TABLES["ledger"])
    assert report["present_tables"], "present_tables 必须反映实表"
    assert set(report["present_tables"]) <= set(report["table_whitelist"])
    # 备份消费面：每个现存表要么有组件归属，要么在 retained
    retained_all = set(report["retained_tables"])
    assert set(report["present_tables"]) <= set(owners) | retained_all
    assert not (set(owners) & retained_all)


def test_report_feeds_backup_tables_block_after_plugin_removal(
        schema_conn, components):
    """接管后移出插件：备份 v2 的表归属仍覆盖每一张实表，不伪造 kernel 归属。"""
    import backup_identity

    cm.adopt_legacy(schema_conn, components)
    zero = cm.component_table_map({})
    report = cm.registry_report(schema_conn, zero)
    assert report["ok"] is True, report["issues"]
    tables = backup_identity.tables_block(report)
    assert set(tables) >= set(report["present_tables"])
    assert tables["ledger_tx"] == "retained"
    assert tables["oplog"] == "kernel"
    assert tables["panels"] == "retained"
    assert set(tables.values()) - {"retained"} == {"kernel"}
    components_block = backup_identity.components_block(report)
    assert [row["name"] for row in components_block] == sorted(
        row["component"] for row in report["components"])


@pytest.mark.parametrize("defect", ["missing-table", "unknown-version", "legacy-chain-incomplete"])
def test_retained_components_are_not_exempt_from_registry_validation(
        schema_conn, components, defect):
    """有合法回执的移出插件仍必须保留全部表，并拒绝未知迁移。"""
    cm.adopt_legacy(schema_conn, components)
    zero = cm.zero_plugin_components()
    assert cm.registry_report(schema_conn, zero)["ok"] is True
    if defect == "missing-table":
        schema_conn.execute("DROP TABLE renewals")
    elif defect == "legacy-chain-incomplete":
        schema_conn.execute("DELETE FROM schema_migrations")
    else:
        schema_conn.execute(
            "INSERT INTO component_schema_migrations(component, version, checksum, catalog_fingerprint) "
            "SELECT 'plugin:renewals', 999, %s, catalog_fingerprint "
            "FROM component_schema_migrations WHERE component='plugin:renewals' AND version=0",
            ("0" * 64,))
    report = cm.registry_report(schema_conn, zero)
    assert report["ok"] is False
    assert {issue["kind"] for issue in report["issues"]} >= {defect}


def test_retained_component_structure_drift_is_still_detected(
        schema_conn, components):
    """插件移出后仍要核对它保留表的结构：retained 不是免检通道。"""
    cm.adopt_legacy(schema_conn, components)
    zero = cm.component_table_map({})
    assert cm.registry_report(schema_conn, zero)["ok"] is True
    schema_conn.execute("ALTER TABLE ledger_tx ADD COLUMN b7_after_removal TEXT")
    report = cm.registry_report(schema_conn, zero)
    assert report["ok"] is False
    assert {"kind": "catalog-drift", "component": "plugin:ledger"} in report["issues"]


def test_adopt_refuses_incompatible_legacy_checksum(schema_conn, components):
    schema_conn.execute(
        "UPDATE schema_migrations SET checksum = 'deadbeef' WHERE version = 15")
    with pytest.raises(cm.ComponentRegistryError):
        cm.adopt_legacy(schema_conn, components)
    assert cm.registry_rows(schema_conn) == []


def test_adopt_refuses_missing_legacy_version(schema_conn, components):
    schema_conn.execute("DELETE FROM schema_migrations WHERE version = 17")
    with pytest.raises(cm.ComponentRegistryError):
        cm.adopt_legacy(schema_conn, components)


def test_post_adoption_structure_change_is_detected(schema_conn, components):
    cm.adopt_legacy(schema_conn, components)
    assert cm.registry_report(schema_conn, components)["ok"] is True
    schema_conn.execute("ALTER TABLE renewals ADD COLUMN surprise TEXT")
    report = cm.registry_report(schema_conn, components)
    assert report["ok"] is False
    assert {"kind": "catalog-drift", "component": "plugin:renewals"} in report["issues"]


def test_conflicting_receipt_is_not_overwritten(schema_conn, components):
    cm.adopt_legacy(schema_conn, components)
    schema_conn.execute(
        "UPDATE component_schema_migrations SET checksum = %s "
        "WHERE component = 'kernel' AND version = 0", ("0" * 64,))
    with pytest.raises(cm.ComponentRegistryError):
        cm.adopt_legacy(schema_conn, components)


def test_app_role_cannot_write_registry(schema_conn, pg_container, fresh_db, components):
    """登记表与 `schema_migrations` 同一纪律：运行账号只读。"""
    cm.apply_components(schema_conn, {}, components)
    role = f"comp_app_{uuid.uuid4().hex[:6]}"
    admin = pg_test_support.connect(pg_container.dsn(dbname=fresh_db), autocommit=True)
    try:
        pg_test_support.create_app_role(
            admin, role, "pw-b7-test", tables=pg_migrations.SCHEMA_TABLES,
            database=fresh_db)
        admin.execute(
            f'GRANT SELECT ON public.component_schema_migrations TO "{role}"')
        app = pg_test_support.connect(
            pg_container.dsn(dbname=fresh_db, user=role, password="pw-b7-test"),
            autocommit=True)
        try:
            assert app.execute(
                "SELECT count(*) AS c FROM component_schema_migrations").fetchone()["c"] == 0
            with pytest.raises(Exception) as failure:
                app.execute(
                    "INSERT INTO component_schema_migrations "
                    "(component, version, checksum) VALUES ('kernel', 1, %s)",
                    ("0" * 64,))
            assert "permission denied" in str(failure.value).lower()
        finally:
            app.close()
    finally:
        pg_test_support.drop_app_role(admin, role)
        admin.close()


def test_concurrent_adoption_is_serialized_by_existing_maintenance_lock(
        pg_container, fresh_db, components, pg_dsn):
    """接管必须在既有维护锁下：第二连接拿锁失败即拒绝，不并发写登记表。"""
    import pg_migration_lib

    first = pg_test_support.connect(pg_container.dsn(dbname=fresh_db), autocommit=True)
    second = pg_test_support.connect(pg_container.dsn(dbname=fresh_db), autocommit=True)
    try:
        pg_migrations.apply(first)
        assert pg_migration_lib.acquire_migration_lock(first, timeout_s=10) == {"mode": "acquired"}
        with pytest.raises(pg_migration_lib.TargetGuardFailure):
            pg_migration_lib.acquire_migration_lock(second, timeout_s=1)
        cm.adopt_legacy(first, components)
        pg_migration_lib.release_migration_lock(first)
        assert pg_migration_lib.acquire_migration_lock(second, timeout_s=10) == {"mode": "acquired"}
        pg_migration_lib.release_migration_lock(second)
    finally:
        first.close()
        second.close()


def test_backup_manifest_shape_from_registry_report(schema_conn, components):
    """备份协调方直接消费的形状（不用再读注册表）。"""
    cm.adopt_legacy(schema_conn, components)
    report = cm.registry_report(schema_conn, components)
    assert set(report) >= {
        "ok", "issues", "engine", "components", "tables", "table_owners",
        "table_whitelist", "legacy_chain_versions", "registry_present",
    }
    for entry in report["components"]:
        assert set(entry) == {
            "component", "version", "origin", "description", "checksum",
            "catalog_fingerprint", "applied_at", "metadata",
        }
        assert entry["origin"] == "adopted"
        assert entry["catalog_fingerprint"]
    # 全表白名单覆盖目录全部表，额外表才可能被发现
    assert set(report["table_whitelist"]) >= set(pg_migrations.table_names(schema_conn))
    json.dumps(report)  # 可安全序列化，无 date/Decimal 之类
