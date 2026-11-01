# -*- coding: utf-8 -*-
"""pg_contracts 冻结协议的结构与幂等语义（提案 §3.2/§8.2）。"""
import hashlib
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import backup_contracts  # noqa: E402
import pg_contracts as contracts  # noqa: E402
from contract_errors import ContractError  # noqa: E402

SHA_A = "a" * 64
SHA_B = "b" * 64


def _manifest():
    return {
        "version": contracts.REFERENCE_MANIFEST_VERSION,
        "generated_at": "2026-09-10 02:00:00",
        "files": [
            {"path": "refs/categories.xlsx", "sha256": SHA_A, "bytes": 1024,
             "format_version": "v1"},
        ],
    }


def test_backup_protocol_reexports_preserve_objects_and_error_identity():
    assert contracts.ContractError is backup_contracts.ContractError is ContractError
    assert contracts.REFERENCE_MANIFEST_VERSION == backup_contracts.LEGACY_ARCHIVE_MANIFEST_VERSION
    for name in (
        "BACKUP_MANIFEST_KEYS", "BACKUP_MANIFEST_V2_KEYS", "BACKUP_MANIFEST_VERSION",
        "BACKUP_MANIFEST_VERSION_V2", "COMPONENT_KEYS", "COMPOSITION_KEYS",
        "RETAINED_TABLE_OWNER", "build_backup_manifest", "build_backup_manifest_v2",
        "read_backup_manifest", "validate_backup_manifest_v2",
    ):
        assert getattr(contracts, name) is getattr(backup_contracts, name)


def test_batch_id_must_be_uuid():
    batch_id = contracts.new_batch_id()
    assert contracts.validate_batch_id(batch_id) == batch_id
    with pytest.raises(contracts.ContractError):
        contracts.validate_batch_id("not-a-uuid")
    with pytest.raises(contracts.ContractError):
        contracts.validate_batch_id(None)


def test_request_digest_is_stable_and_order_sensitive():
    artifacts = [
        {"name": "a.csv", "sha256": SHA_A, "bytes": 10},
        {"name": "b.csv", "sha256": SHA_B, "bytes": 20},
    ]
    first = contracts.request_digest("import", "mac", "sha1", None, artifacts)
    second = contracts.request_digest("import", "mac", "sha1", None, list(artifacts))
    assert first == second
    # 文件顺序是业务语义，颠倒顺序必须产生不同摘要
    reversed_digest = contracts.request_digest("import", "mac", "sha1", None, list(reversed(artifacts)))
    assert reversed_digest != first
    # 换 reference 版本也是不同的请求
    assert contracts.request_digest("import", "mac", "sha1", SHA_A, artifacts) != first


def test_reference_manifest_validates_and_has_stable_digest():
    manifest = _manifest()
    normalized = contracts.validate_reference_manifest(manifest)
    assert normalized["files"][0]["sha256"] == SHA_A
    digest = contracts.reference_manifest_digest(manifest)
    assert digest == contracts.reference_manifest_digest(_manifest())
    bad = _manifest()
    bad["version"] = "reference-manifest-99"
    with pytest.raises(contracts.ContractError):
        contracts.validate_reference_manifest(bad)


def test_reference_manifest_rejects_bad_digest():
    bad = _manifest()
    bad["files"][0]["sha256"] = "xyz"
    with pytest.raises(contracts.ContractError):
        contracts.validate_reference_manifest(bad)


def test_receipt_round_trip_and_mismatch_detection():
    batch_id = contracts.new_batch_id()
    artifacts = [{"name": "bill.csv", "sha256": SHA_A, "bytes": 42}]
    receipt = contracts.build_batch_receipt(
        batch_id, "import", "mac", "code-sha", artifacts,
        reference_manifest_sha256=SHA_B, stored_at="2026-09-10 02:10:00",
    )
    assert receipt["version"] == contracts.BATCH_RECEIPT_VERSION
    assert receipt["status"] == contracts.BATCH_STATUS_ARCHIVED
    validated = contracts.validate_batch_receipt(
        receipt, expected_batch_id=batch_id, expected_digest=receipt["request_sha256"]
    )
    assert validated["artifacts"][0]["sha256"] == SHA_A
    with pytest.raises(contracts.ContractError):
        contracts.validate_batch_receipt(receipt, expected_digest="c" * 64)
    with pytest.raises(contracts.ContractError):
        contracts.validate_batch_receipt(
            receipt, expected_batch_id=contracts.new_batch_id()
        )


def test_receipt_rejects_unknown_operation_and_status():
    batch_id = contracts.new_batch_id()
    with pytest.raises(contracts.ContractError):
        contracts.build_batch_receipt(batch_id, "teleport", "mac", "sha", [])
    with pytest.raises(contracts.ContractError):
        contracts.build_batch_receipt(
            batch_id, "import", "mac", "sha", [], status="maybe"
        )


def test_batch_limits_reject_oversized_input():
    ok = [{"name": f"{i}.csv", "sha256": SHA_A, "bytes": 1024} for i in range(3)]
    assert contracts.check_batch_limits(ok) == {"files": 3, "total_bytes": 3072}
    too_many = [{"name": f"{i}.csv", "sha256": SHA_A, "bytes": 1} for i in range(11)]
    with pytest.raises(contracts.ContractError):
        contracts.check_batch_limits(too_many)
    with pytest.raises(contracts.ContractError):
        contracts.check_batch_limits(
            [{"name": "big.csv", "sha256": SHA_A, "bytes": 21 * 1024 * 1024}]
        )
    with pytest.raises(contracts.ContractError):
        contracts.check_batch_limits(
            [{"name": f"{i}.csv", "sha256": SHA_A, "bytes": 19 * 1024 * 1024}
             for i in range(3)]
        )


def test_report_manifest_has_all_required_keys_and_rejects_unknown():
    manifest = contracts.build_report_manifest(mode="preflight", status="ok")
    for key in contracts.MIGRATION_REPORT_KEYS:
        assert key in manifest
    assert manifest["manifest_version"] == contracts.MIGRATION_REPORT_MANIFEST_VERSION
    with pytest.raises(contracts.ContractError):
        contracts.build_report_manifest(dsn="postgres://secret")


def test_report_manifest_version_pins_key_ranges_rule():
    """G08：key_ranges 值形状收紧是一次报告契约变更，版本必须显式区分。"""
    assert contracts.MIGRATION_REPORT_MANIFEST_VERSION == "pg-migrate-report-2"


def test_key_ranges_reject_primary_key_text():
    """合成敏感标记：主键原文与它的摘要都不得进入 key_ranges。"""
    canary = "ACME-MERCHANT-CANARY"
    for forbidden in (canary, canary.lower(), hashlib.sha256(canary.encode()).hexdigest()):
        with pytest.raises(contracts.ContractError):
            contracts.key_range_entry(forbidden, forbidden)
    with pytest.raises(contracts.ContractError):
        contracts.validate_key_range(
            {"min": "1", "max": canary}, table="ledger_knowledge"
        )


def test_key_ranges_accept_numbers_and_null_only():
    assert contracts.key_range_entry(1, 8800) == {"min": 1, "max": 8800}
    assert contracts.key_range_entry() == {"min": None, "max": None}
    # 数值主键的规范文本必须先转成数字，不能原样落盘
    with pytest.raises(contracts.ContractError):
        contracts.key_range_entry("1", "8800")
    # bool 是 int 的子类，但不是数字语义
    with pytest.raises(contracts.ContractError):
        contracts.key_range_entry(True, False)
    with pytest.raises(contracts.ContractError):
        contracts.validate_key_range({"min": 1}, table="renewals")
    with pytest.raises(contracts.ContractError):
        contracts.validate_key_ranges(["not", "a", "mapping"])


def test_validate_report_manifest_catches_what_key_name_assertions_miss():
    # 构造即拦截：泄漏值根本出不了 build_report_manifest
    with pytest.raises(contracts.ContractError):
        contracts.build_report_manifest(
            mode="apply",
            status="ok",
            key_ranges={"ledger_knowledge": {"min": "ACME-CANARY", "max": "ZZZ"}},
        )

    # 手写/落盘的报告（不经过 build）也要能独立校验
    hand_written = {key: None for key in contracts.MIGRATION_REPORT_KEYS}
    hand_written["manifest_version"] = contracts.MIGRATION_REPORT_MANIFEST_VERSION
    hand_written["key_ranges"] = {"ledger_knowledge": {"min": "ACME-CANARY", "max": "Z"}}
    with pytest.raises(contracts.ContractError):
        contracts.validate_report_manifest(hand_written)

    clean = contracts.build_report_manifest(
        mode="apply",
        status="ok",
        key_ranges={
            "renewals": {"min": 1, "max": 8800},
            "ledger_knowledge": {"min": None, "max": None},
        },
    )
    assert contracts.validate_report_manifest(clean) is clean
    assert set(clean) == set(contracts.MIGRATION_REPORT_KEYS)

    del clean["status"]
    with pytest.raises(contracts.ContractError):
        contracts.validate_report_manifest(clean)


def test_cli_contracts_freeze_expected_entrypoints():
    assert set(contracts.CLI_CONTRACTS["scripts/pg_schema.py"]["subcommands"]) == {
        "check", "migrate"
    }
    migrate = contracts.CLI_CONTRACTS["scripts/migrate_sqlite_to_postgres.py"]
    assert set(migrate["modes"]) == {"preflight", "apply", "verify"}
    assert set(migrate["purposes"]) == {"rehearsal", "production"}
    assert "--storage-cutover" in contracts.CLI_CONTRACTS["scripts/deploy.sh"]["flags"]
