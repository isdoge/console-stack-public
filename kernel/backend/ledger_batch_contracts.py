# -*- coding: utf-8 -*-
"""私人 ledger 文件批次 / reference 协议（提案 §3.2）；不含通用备份或迁移报告。"""
import hashlib
import json
import re
import uuid

from backup_contracts import (
    LEGACY_ARCHIVE_MANIFEST_VERSION as REFERENCE_MANIFEST_VERSION,
)
from contract_errors import ContractError

CONTRACT_VERSION = "pg00-1"

BATCH_RECEIPT_VERSION = "batch-receipt-1"
# 提案 §3.2 的批次限额：坏行整批失败，不做部分成功。
BATCH_LIMITS = {
    "max_files": 10,
    "max_file_bytes": 20 * 1024 * 1024,
    "max_total_bytes": 50 * 1024 * 1024,
}

# 批次状态机（提案 §3.2 失败与重试矩阵）。
BATCH_STATUS_ARCHIVED = "archived"          # NAS 已归档，PG 未提交
BATCH_STATUS_COMMITTED = "committed"        # 业务变更与批次记录同事务提交
BATCH_STATUS_ORPHAN = "orphan"              # 归档 >24h 仍无 PG 记录，待人工核验
BATCH_STATUSES = (BATCH_STATUS_ARCHIVED, BATCH_STATUS_COMMITTED, BATCH_STATUS_ORPHAN)

# 操作类型：写业务流水的批次 vs 只重分类 vs 纯 DB 操作。
BATCH_OPERATIONS = (
    "import",
    "reclassify",
    "classify",
    "ai_apply",
    "relink",
    "link_repay",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BATCH_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def new_batch_id():
    """客户端首次操作生成、重试保持不变的 batch_id。"""
    return str(uuid.uuid4())


def validate_batch_id(value):
    text = "" if value is None else str(value).strip()
    if not _BATCH_ID_RE.fullmatch(text):
        raise ContractError("batch_id 必须是 UUID")
    return text.lower()


def validate_sha256(value, field="sha256"):
    text = "" if value is None else str(value).strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise ContractError(f"{field} 必须是 64 位小写十六进制 SHA256")
    return text


def canonical_json(payload):
    """协议摘要的规范化 JSON：键排序、无多余空白、UTF-8。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def request_digest(operation, client, code_version, reference_manifest_sha256, artifacts):
    """批次请求摘要。同 batch_id + 同摘要 → 返回同一回执；同 ID 不同摘要 → 409。

    `artifacts` 只取**顺序敏感的** `(name, sha256, bytes)` 三元组：文件顺序是业务语义
    （批次内按序解析），不能排序后再算摘要。
    """
    normalized_artifacts = []
    for item in artifacts or ():
        normalized_artifacts.append(
            [
                str(item.get("name", "")),
                validate_sha256(item.get("sha256"), "artifacts[].sha256"),
                int(item.get("bytes", 0)),
            ]
        )
    payload = {
        "contract": CONTRACT_VERSION,
        "operation": str(operation),
        "client": str(client),
        "code_version": str(code_version),
        "reference_manifest_sha256": reference_manifest_sha256 or None,
        "artifacts": normalized_artifacts,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def validate_reference_manifest(payload):
    """校验 NAS 返回的 reference manifest；返回规范化副本。

    只列**允许参与分类**的 reference 文件，不列私人目录（提案 §3.2）。
    """
    if not isinstance(payload, dict):
        raise ContractError("reference manifest 必须是对象")
    if payload.get("version") != REFERENCE_MANIFEST_VERSION:
        raise ContractError(
            f"reference manifest 版本不匹配: {payload.get('version')!r}，"
            f"期望 {REFERENCE_MANIFEST_VERSION}"
        )
    files = payload.get("files")
    if not isinstance(files, list):
        raise ContractError("reference manifest.files 必须是数组")
    normalized = []
    for item in files:
        if not isinstance(item, dict):
            raise ContractError("reference manifest.files[] 必须是对象")
        normalized.append(
            {
                "path": str(item.get("path", "")),
                "sha256": validate_sha256(item.get("sha256"), "files[].sha256"),
                "bytes": int(item.get("bytes", 0)),
                "format_version": str(item.get("format_version", "")),
            }
        )
    return {
        "version": REFERENCE_MANIFEST_VERSION,
        "generated_at": str(payload.get("generated_at", "")),
        "files": normalized,
    }


def reference_manifest_digest(manifest, *, legacy=False):
    """内容版本不含生成时间；legacy 仅用于严格核验已归档的旧摘要。

    回执与 manifest 字段形状不变。旧回执必须按原算法复算并保持原摘要，
    不能因新算法推出就把旧批次标记改写为当前版本。
    """
    normalized = validate_reference_manifest(manifest)
    if not legacy:
        normalized.pop("generated_at")
    return hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()


def build_batch_receipt(batch_id, operation, client, code_version, artifacts,
                        reference_manifest_sha256=None, status=BATCH_STATUS_ARCHIVED,
                        stored_at=""):
    """构造 NAS 归档回执。**只有 NAS 侧调用**；Mac 侧只校验，不构造。"""
    if status not in BATCH_STATUSES:
        raise ContractError(f"未知批次状态: {status!r}")
    if operation not in BATCH_OPERATIONS:
        raise ContractError(f"未知批次操作: {operation!r}")
    normalized_artifacts = [
        {
            "name": str(item.get("name", "")),
            "sha256": validate_sha256(item.get("sha256"), "artifacts[].sha256"),
            "bytes": int(item.get("bytes", 0)),
        }
        for item in artifacts or ()
    ]
    return {
        "version": BATCH_RECEIPT_VERSION,
        "batch_id": validate_batch_id(batch_id),
        "operation": operation,
        "client": str(client),
        "code_version": str(code_version),
        "request_sha256": request_digest(
            operation, client, code_version, reference_manifest_sha256, normalized_artifacts
        ),
        "reference_manifest_sha256": reference_manifest_sha256 or None,
        "artifacts": normalized_artifacts,
        "status": status,
        "stored_at": str(stored_at),
    }


def validate_batch_receipt(receipt, expected_batch_id=None, expected_digest=None):
    """Mac 侧校验：batch_id、请求摘要、文件顺序/hash/大小必须与本机输入一致。

    签名不是本私网模型的必需（提案 §3.2），但**不能只信任客户端自报的「已归档」**。
    """
    if not isinstance(receipt, dict):
        raise ContractError("回执必须是对象")
    if receipt.get("version") != BATCH_RECEIPT_VERSION:
        raise ContractError(f"回执版本不匹配: {receipt.get('version')!r}")
    batch_id = validate_batch_id(receipt.get("batch_id"))
    if expected_batch_id is not None and batch_id != validate_batch_id(expected_batch_id):
        raise ContractError("回执 batch_id 与请求不一致")
    if expected_digest is not None and receipt.get("request_sha256") != expected_digest:
        raise ContractError("回执请求摘要与本地输入不一致")
    for item in receipt.get("artifacts") or ():
        validate_sha256(item.get("sha256"), "artifacts[].sha256")
    return receipt


def check_batch_limits(artifacts):
    """限额校验：每批 ≤10 文件、单件 ≤20 MiB、合计 ≤50 MiB。"""
    items = list(artifacts or ())
    if len(items) > BATCH_LIMITS["max_files"]:
        raise ContractError(f"每批最多 {BATCH_LIMITS['max_files']} 个文件")
    total = 0
    for item in items:
        size = int(item.get("bytes", 0))
        if size > BATCH_LIMITS["max_file_bytes"]:
            raise ContractError("单个文件超过 20 MiB")
        if size < 0:
            raise ContractError("文件大小不能为负")
        total += size
    if total > BATCH_LIMITS["max_total_bytes"]:
        raise ContractError("批次总大小超过 50 MiB")
    return {"files": len(items), "total_bytes": total}
