"""通用备份 manifest v1/v2；不导入私人批次、reference 或遗产迁移协议。"""
import re

from contract_errors import ContractError as ContractError

BACKUP_MANIFEST_VERSION = "pg-backup-manifest-1"
# pg-backup-manifest-2：在 v1 的 14 个键之上，追加「这次备份属于哪个组合」的
# 事实——组合构建身份、组件登记、全表清单、附件清单。v1 的键与语义**一个不改**，
# 旧备份继续按 v1 读写；v2 只增不改，读取端必须两种都认。
BACKUP_MANIFEST_VERSION_V2 = "pg-backup-manifest-2"
# 原wire字段的固定兼容标记；无附件v2也保留该值，不导入私人reference实现。
LEGACY_ARCHIVE_MANIFEST_VERSION = "reference-manifest-1"

BACKUP_MANIFEST_KEYS = (
    "manifest_version",
    "engine",
    "schema_version",
    "schema_checksum",
    "source_commit",
    "pg_server_version",
    "pg_client_version",
    "database_identity",
    "started_at",
    "finished_at",
    "members",
    "row_digest",
    "archive_manifest_version",
    "status",
)


def build_backup_manifest(**fields):
    manifest = {key: None for key in BACKUP_MANIFEST_KEYS}
    manifest["manifest_version"] = BACKUP_MANIFEST_VERSION
    for key, value in fields.items():
        if key not in manifest:
            raise ContractError(f"未知的备份字段: {key}")
        manifest[key] = value
    return manifest


# --- 备份 manifest v2：组合事实 -------------------------------------------
#
# v1 只描述「库本身」：版本、指纹、成员、行摘要。它回答不了「这份备份属于哪个
# 组合、装了哪些组件、全表清单是什么、附件有哪些」。公共候选要能独立安装升级
# 恢复，就必须能从备份自证这些事实，否则恢复方只能靠猜。
#
# 设计约束：
#   - **只增不改**：v2 = v1 的 14 个键 + 4 个新键，v1 键的语义一字不动；
#   - v1 manifest 继续可读（`read_backup_manifest` 两种版本都认）；
#   - 新键是**必填**：声称 v2 却缺这些字段的 manifest 不完整，不允许发布；
#   - 不写路径、不写 DSN、不写行内容：组合身份用既有白名单字段的摘要。

COMPOSITION_KEYS = (
    "kernel_version",
    "composition_digest",
    "plugins",
)
COMPONENT_KEYS = (
    "name",
    "version",
    "origin",
    "checksum",
    "catalog_fingerprint",
)
BACKUP_MANIFEST_V2_KEYS = BACKUP_MANIFEST_KEYS + (
    "composition",
    "components",
    "tables",
    "attachments",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMPONENT_NAME_RE = re.compile(r"^(kernel|plugin:[a-z][a-z0-9_]{0,47})$")
# 不属于任何组件、但必须备份的表（遗产保留表与迁移登记表）的归属标记。
RETAINED_TABLE_OWNER = "retained"


def build_backup_manifest_v2(**fields):
    """构造 v2 manifest；`composition/components/tables/attachments` 必填。

    先查必填再逐块校验：缺块时直接说「哪个字段为空」，而不是让调用方从
    「attachments 必须是数组」这种次级报错里反推根因。

    两处例外：`manifest_version` 由本函数填，调用方不必传；`attachments`
    的**空数组是合法值**（无附件组合就该是空的），只有缺失/None 才算未填。
    """
    required = [key for key in BACKUP_MANIFEST_V2_KEYS
                if key not in ("manifest_version", "attachments")]
    missing = [key for key in required if fields.get(key) in (None, "", [], {})]
    if "attachments" not in fields or fields["attachments"] is None:
        missing.append("attachments")
    if missing:
        raise ContractError(f"备份 manifest-2 必填字段为空: {', '.join(missing)}")
    manifest = {key: None for key in BACKUP_MANIFEST_V2_KEYS}
    manifest["manifest_version"] = BACKUP_MANIFEST_VERSION_V2
    for key, value in fields.items():
        if key not in manifest:
            raise ContractError(f"未知的备份字段: {key}")
        manifest[key] = value
    return validate_backup_manifest_v2(manifest)


def _require_sha256(value, field):
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ContractError(f"{field} 必须是小写十六进制 SHA256")
    return value


def _validate_composition_block(value):
    if not isinstance(value, dict) or set(value) != set(COMPOSITION_KEYS):
        raise ContractError("composition 必须且只能含 " + "/".join(COMPOSITION_KEYS))
    if not isinstance(value["kernel_version"], str) or not value["kernel_version"].strip():
        raise ContractError("composition.kernel_version 不能为空")
    _require_sha256(value["composition_digest"], "composition.composition_digest")
    plugins = value["plugins"]
    if not isinstance(plugins, list):
        raise ContractError("composition.plugins 必须是数组")
    seen = set()
    for entry in plugins:
        if not isinstance(entry, dict) or set(entry) != {"id", "version", "digest"}:
            raise ContractError("composition.plugins 条目字段无效")
        pid = entry["id"]
        if not isinstance(pid, str) or not pid.strip() or pid in seen:
            raise ContractError("composition.plugins 含重复或无效 id")
        if not isinstance(entry["version"], str) or not entry["version"].strip():
            raise ContractError("composition.plugins 版本不能为空")
        _require_sha256(entry["digest"], "composition.plugins.digest")
        seen.add(pid)
    return value


def _validate_components_block(value):
    """组件登记：每个组件一条，含接管回执（version 0 / origin=adopted）。"""
    if not isinstance(value, list) or not value:
        raise ContractError("components 必须是非空数组")
    seen = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != set(COMPONENT_KEYS):
            raise ContractError("components 条目必须且只能含 " + "/".join(COMPONENT_KEYS))
        name = entry["name"]
        if not isinstance(name, str) or not _COMPONENT_NAME_RE.fullmatch(name):
            raise ContractError(f"components 组件名无效: {name!r}")
        key = (name, entry["version"])
        if type(entry["version"]) is not int or entry["version"] < 0:
            raise ContractError("components.version 必须是 ≥0 的整数")
        if key in seen:
            raise ContractError(f"components 含重复组件版本: {key}")
        if entry["origin"] not in ("applied", "adopted"):
            raise ContractError("components.origin 只允许 applied 或 adopted")
        _require_sha256(entry["checksum"], "components.checksum")
        fingerprint = entry["catalog_fingerprint"]
        if fingerprint is not None:
            _require_sha256(fingerprint, "components.catalog_fingerprint")
        # 接管回执的意义就是「记下接管时刻的结构」；没有指纹的回执证明不了任何事。
        if entry["origin"] == "adopted" and fingerprint is None:
            raise ContractError(f"{name}: adopted 回执必须带 catalog_fingerprint")
        seen.add(key)
    return value


def _validate_tables_block(value):
    """全表清单：表名 → 归属。额外表不能悄悄漏备，所以清单必须是完整集合。

    归属只有三类：`kernel`、`plugin:<id>`，以及 `retained`——后者用于不属于
    任何组件但必须备份的表（遗产保留的 `panels`、两张迁移登记表）。不允许
    伪造组件归属。
    """
    if not isinstance(value, dict) or not value:
        raise ContractError("tables 必须是非空映射（表名 → 归属）")
    for table, owner in value.items():
        if not isinstance(table, str) or not table.strip():
            raise ContractError("tables 的表名不能为空")
        if owner == RETAINED_TABLE_OWNER:
            continue
        if not isinstance(owner, str) or not _COMPONENT_NAME_RE.fullmatch(owner):
            raise ContractError(f"tables[{table}] 的归属无效: {owner!r}")
    return value


def _validate_attachments_block(value):
    """附件清单：无附件组合必须是**空数组**，不能是 null 或省略。"""
    if not isinstance(value, list):
        raise ContractError("attachments 必须是数组（无附件时为空数组）")
    seen = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "bytes"}:
            raise ContractError("attachments 条目必须且只能含 path/sha256/bytes")
        path = entry["path"]
        if not isinstance(path, str) or not path.strip():
            raise ContractError("attachments.path 不能为空")
        if path in seen:
            raise ContractError(f"attachments 含重复成员: {path}")
        _require_sha256(entry["sha256"], "attachments.sha256")
        if type(entry["bytes"]) is not int or entry["bytes"] < 0:
            raise ContractError("attachments.bytes 必须是 ≥0 的整数")
        seen.add(path)
    return value


def validate_backup_manifest_v2(manifest):
    """v2 校验：先按 v1 必填语义检查，再检查四个新块。"""
    if not isinstance(manifest, dict):
        raise ContractError("备份 manifest 必须是映射")
    if manifest.get("manifest_version") != BACKUP_MANIFEST_VERSION_V2:
        raise ContractError("不是 pg-backup-manifest-2")
    if set(manifest) != set(BACKUP_MANIFEST_V2_KEYS):
        raise ContractError("备份 manifest-2 字段集合不符")
    for key in BACKUP_MANIFEST_KEYS:
        if key == "manifest_version":
            continue
        if manifest.get(key) in (None, "", [], {}):
            raise ContractError(f"备份 manifest-2 必填字段为空: {key}")
    if manifest["status"] != "complete":
        raise ContractError("备份 manifest-2 的 status 必须是 complete")
    if manifest["engine"] != "postgres":
        raise ContractError("备份 manifest-2 只支持 postgres")
    _validate_composition_block(manifest["composition"])
    _validate_components_block(manifest["components"])
    _validate_tables_block(manifest["tables"])
    _validate_attachments_block(manifest["attachments"])
    return manifest


def read_backup_manifest(manifest):
    """读取端统一入口：v1 与 v2 都认，未知版本明确拒绝（不猜）。"""
    if not isinstance(manifest, dict):
        raise ContractError("备份 manifest 必须是映射")
    version = manifest.get("manifest_version")
    if version == BACKUP_MANIFEST_VERSION:
        if set(manifest) != set(BACKUP_MANIFEST_KEYS):
            raise ContractError("备份 manifest-1 字段集合不符")
        return manifest
    if version == BACKUP_MANIFEST_VERSION_V2:
        return validate_backup_manifest_v2(manifest)
    raise ContractError(f"未知的备份 manifest 版本: {version!r}")
