# -*- coding: utf-8 -*-
"""遗产 SQLite→PG 迁移/验证报告契约（提案 §8.2、§5.3）；不含私人批次或通用备份。"""
import math
from decimal import Decimal

from contract_errors import ContractError

# pg-migrate-report-2：key_ranges 的值形状由「任意文本」收紧为「数字或 null」，
# 见下方 KEY_RANGES_VALUE_RULE。PG-03 的实现与报告 consumer 都要按 -2 校验。
MIGRATION_REPORT_MANIFEST_VERSION = "pg-migrate-report-2"

MIGRATION_MODES = ("preflight", "apply", "verify")
TARGET_PURPOSES = ("rehearsal", "production")

# CLI 参数表：PG-03 实现、PG-04 调用，两侧都以此为准。
CLI_CONTRACTS = {
    "scripts/pg_schema.py": {
        "subcommands": ("check", "migrate"),
        "check": "只读核对 engine/schema/checksum/运行账号权限，脱敏输出",
        "migrate": "NAS 管理员连接、迁移锁、追加 schema；不 import app、不启动后台",
    },
    "scripts/migrate_sqlite_to_postgres.py": {
        "args": ("--mode", "--source", "--source-sha256", "--target-purpose", "--report"),
        "modes": MIGRATION_MODES,
        "purposes": TARGET_PURPOSES,
        "note": "密码只来自环境/私有凭据文件；命令行与 JSON 报告不携带 DSN",
    },
    "scripts/verify_postgres_migration.py": {
        "args": ("--source", "--target", "--report"),
        "note": "独立只读比对器，不复用搬运器的结果缓存；返回非零表示不一致",
    },
    "scripts/backup_runtime.py": {
        "subcommands": ("deploy", "rollback", "timed", "publish-runtime", "cutover"),
        "note": "保留现有命令兼容，增加 engine 识别与 PG 恢复验证",
    },
    "scripts/deploy.sh": {
        "flags": ("--yes", "--storage-cutover"),
        "note": "首次迁移模式受控编排 §9；常规 --yes 仍用于后续 PG 发布",
    },
}

# 报告 manifest 的必填键：不含 DSN、不含私人行内容。
#
# `key_ranges` 曾经只约束键名、不约束值形状，于是字典序 min/max 直接把主键原文写进报告
# （`ledger_knowledge(who, good)` 这类文本/复合文本主键会带出商户、商品原文）。
# 下面这条规则把「不含私人行内容」从注释变成可判定的形状约束。
KEY_RANGES_VALUE_RULE = (
    "key_ranges[表] 只能是 {'min': <数字或 null>, 'max': <数字或 null>}；"
    "主键含文本列（含复合文本主键）的表两个值一律为 null；"
    "禁止把主键原文或它的摘要字符串放进 min/max——低熵商户/商品名可被字典枚举，"
    "而文本主键的字典序区间本就不提供核验强度（逐行强度由 counts/row_hash 保证）。"
)

MIGRATION_REPORT_KEYS = (
    "manifest_version",
    "mode",
    "target_purpose",
    "source_sha256",
    "source_schema_version",
    "target_engine",
    "target_schema_version",
    "target_schema_checksum",
    "target_identity",
    "tables",
    "counts",
    "key_ranges",
    "amount_summary",
    "mismatches",
    "started_at",
    "finished_at",
    "status",
)


def build_report_manifest(**fields):
    """构造报告 manifest 骨架；缺失键显式留 None，避免下游 `keyError` 猜测。

    构造即校验（含 key_ranges 值形状）：报告只有这一条产出路径，在这里拦住
    就比在测试里事后断言更早、也更难绕过。
    """
    manifest = {key: None for key in MIGRATION_REPORT_KEYS}
    manifest["manifest_version"] = MIGRATION_REPORT_MANIFEST_VERSION
    for key, value in fields.items():
        if key not in manifest:
            raise ContractError(f"未知的报告字段: {key}")
        manifest[key] = value
    return validate_report_manifest(manifest)


def _is_json_number(value):
    """JSON 数字（bool 不算，NaN/Inf 不算）。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Decimal):
        return value.is_finite()
    return False


def key_range_entry(minimum=None, maximum=None):
    """构造合规的 `key_ranges[表]` 值；数值主键传 int，文本/复合文本主键传 None。

    PG-03 用它替代「直接塞规范文本」的写法——字符串会在构造处就被拒绝。
    """
    entry = {"min": minimum, "max": maximum}
    validate_key_range(entry)
    return entry


def validate_key_range(entry, table=None):
    """按 `KEY_RANGES_VALUE_RULE` 校验单个表的区间值。"""
    where = f"key_ranges[{table!r}]" if table is not None else "key_range_entry"
    if not isinstance(entry, dict) or set(entry) != {"min", "max"}:
        raise ContractError(f"{where} 必须是恰好含 min/max 两键的对象")
    for side in ("min", "max"):
        value = entry[side]
        if value is None or _is_json_number(value):
            continue
        raise ContractError(
            f"{where}.{side} 只能是数字或 null（收到 {type(value).__name__}）；"
            f"主键原文/摘要不得进入报告，见 KEY_RANGES_VALUE_RULE"
        )


def validate_key_ranges(key_ranges):
    """校验整份 `key_ranges`；`None`（preflight 不比对）合法。"""
    if key_ranges is None:
        return key_ranges
    if not isinstance(key_ranges, dict):
        raise ContractError("key_ranges 必须是 {表: {min, max}} 对象或 null")
    for table, entry in key_ranges.items():
        validate_key_range(entry, table)
    return key_ranges


def validate_report_manifest(manifest):
    """报告落盘前的强制校验：键集合 + manifest 版本 + key_ranges 值形状。

    生产侧（migrate/verify 的每条写出路径，含失败报告）与测试断言都应调用它，
    只断言键名正是 G08 漏检的成因。
    """
    if not isinstance(manifest, dict):
        raise ContractError("报告 manifest 必须是对象")
    unknown = set(manifest) - set(MIGRATION_REPORT_KEYS)
    if unknown:
        raise ContractError(f"报告含未定义字段: {sorted(unknown)}")
    missing = set(MIGRATION_REPORT_KEYS) - set(manifest)
    if missing:
        raise ContractError(f"报告缺少必填字段: {sorted(missing)}")
    if manifest.get("manifest_version") != MIGRATION_REPORT_MANIFEST_VERSION:
        raise ContractError(
            f"报告 manifest 版本必须是 {MIGRATION_REPORT_MANIFEST_VERSION}，"
            f"收到 {manifest.get('manifest_version')!r}"
        )
    validate_key_ranges(manifest.get("key_ranges"))
    return manifest
