# -*- coding: utf-8 -*-
"""PG-00 冻结协议的兼容入口（契约版本见 ledger_batch_contracts.CONTRACT_VERSION）。

备份 manifest → backup_contracts；私人批次/reference → ledger_batch_contracts；
遗产迁移报告 → pg_migration_contracts；异常 → contract_errors。
本模块只做同对象重导出，保留旧 `import pg_contracts`；字段增删仍先改对应正文模块。
"""
from backup_contracts import (  # noqa: F401
    BACKUP_MANIFEST_KEYS,
    BACKUP_MANIFEST_V2_KEYS,
    BACKUP_MANIFEST_VERSION,
    BACKUP_MANIFEST_VERSION_V2,
    COMPONENT_KEYS,
    COMPOSITION_KEYS,
    RETAINED_TABLE_OWNER,
    build_backup_manifest,
    build_backup_manifest_v2,
    read_backup_manifest,
    validate_backup_manifest_v2,
)
from contract_errors import ContractError  # noqa: F401
from ledger_batch_contracts import (  # noqa: F401
    BATCH_LIMITS,
    BATCH_OPERATIONS,
    BATCH_RECEIPT_VERSION,
    BATCH_STATUS_ARCHIVED,
    BATCH_STATUS_COMMITTED,
    BATCH_STATUS_ORPHAN,
    BATCH_STATUSES,
    CONTRACT_VERSION,
    REFERENCE_MANIFEST_VERSION,
    build_batch_receipt,
    canonical_json,
    check_batch_limits,
    new_batch_id,
    reference_manifest_digest,
    request_digest,
    validate_batch_id,
    validate_batch_receipt,
    validate_reference_manifest,
    validate_sha256,
)
from pg_migration_contracts import (  # noqa: F401
    CLI_CONTRACTS,
    KEY_RANGES_VALUE_RULE,
    MIGRATION_MODES,
    MIGRATION_REPORT_KEYS,
    MIGRATION_REPORT_MANIFEST_VERSION,
    TARGET_PURPOSES,
    build_report_manifest,
    key_range_entry,
    validate_key_range,
    validate_key_ranges,
    validate_report_manifest,
)
