# -*- coding: utf-8 -*-
"""单服务升级影响分析与准备清单（S6 升级准备台的 console 收编）。

- 影响分类：对 `version_service` 已抓取的 release 正文跑关键词规则，
  标出 breaking / security 候选行（规则移植自 S6 model.mjs，仅作审阅提示）。
- 准备清单：六项检查 + 审阅状态按服务持久化在 `service_upgrade_reviews`
  表（PG 迁移 14）；状态、理由与勾选经 `/api/service/<name>/upgrade-review` 写入。
- readiness 由 blockers 判定（版本未知、无 release 正文、草稿/预发布、
  清单未齐等），不伪造「可以升级」。
"""
import json
import re
from datetime import datetime, timezone

import storage_dispatch as sd

# (key, label)：六项检查，缺失即 blocker。顺序即弹窗展示顺序。
CHECKS = (
    ("config", "配置副本与构建上下文已保留"),
    ("data", "持久数据一致性备份已审阅"),
    ("old_image", "旧镜像 / 二进制与摘要已保留"),
    ("restore", "回退入口和隔离恢复验收已审阅"),
    ("window", "写者、窗口和依赖负责人已确认"),
    ("acceptance", "真实版本、业务入口与后续任务验收已列出"),
)

STATUSES = ("pending", "prepared", "deferred", "ignored")

_RULES = (
    ("数据库", "breaking", re.compile(
        r"\b(?:migration|schema|pg_restore|database|postgresql|sqlite|downgrade)\b|数据库|迁移|表结构|回滚.*数据", re.I)),
    ("配置", "breaking", re.compile(
        r"\b(?:breaking|removed|renamed|deprecated|config(?:uration)?|environment variable)\b|配置|删除.*参数|弃用|改名", re.I)),
    ("架构", "breaking", re.compile(
        r"\b(?:amd64|arm64|architecture|unsupported|platform|minimum.*version)\b|架构|平台|最低版本|不再支持", re.I)),
    ("API / 端口", "breaking", re.compile(
        r"\b(?:endpoint|API|port|listen|route|protocol)\b|接口|端口|路径调整|协议", re.I)),
    ("安全", "security", re.compile(
        r"\b(?:security|CVE-\d|vulnerability|authentication|TLS|XSS)\b|安全|漏洞|鉴权", re.I)),
)

_UNKNOWN_LOCAL = frozenset({
    "", "unknown", "未知", "n/a", "latest", "running (docker)",
})


def extract_changes(body):
    """release 正文逐行跑关键词规则；短行与空行跳过。"""
    changes = []
    for index, line in enumerate((body or "").split("\n"), start=1):
        excerpt = line.strip()
        if len(excerpt) < 5:
            continue
        for category, severity, rule in _RULES:
            if rule.search(excerpt):
                changes.append({
                    "category": category, "severity": severity,
                    "excerpt": excerpt[:300], "line": index,
                })
    return changes


def _normalize(version):
    return str(version or "").split("+", 1)[0].lstrip("v").strip()


def _fixed_version(version):
    v = _normalize(version)
    return bool(v) and v.lower() not in _UNKNOWN_LOCAL


def _parse_date(value):
    """接受 ISO（含 Z）与 versions 表的 UTC 文本两种口径；解析不出返回 None。"""
    if not value:
        return None
    text = str(value).strip()
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            dt = datetime.fromisoformat(candidate)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _freshness_label(at, now):
    dt = _parse_date(at)
    if not dt:
        return "采样时间未知"
    age = (now - dt).total_seconds() / 3600
    if age < -0.05:
        return "时间在未来，需核对时钟"
    if age > 12:
        return f"历史 / 过期快照（{int(age)}h）"
    return "近期快照（非实时连接）"


def _freshness_recent(at, now):
    dt = _parse_date(at)
    if not dt:
        return False
    age = (now - dt).total_seconds() / 3600
    return -0.05 <= age <= 12


def get_review(name, db_path=None, conn=None):
    """读持久化的审阅行；没有则返回 None（调用方按 pending 默认展示）。"""
    ph = sd.ph()
    try:
        with sd.read_tx(conn=conn, db_path=db_path) as own:
            row = own.execute(
                f"SELECT * FROM service_upgrade_reviews WHERE svc_name = {ph}", (name,)
            ).fetchone()
    except Exception:
        # 迁移未跑（旧部署/测试库）时按无审阅处理，不让版本详情整体失败
        return None
    if not row:
        return None
    checks = row["checks"]
    # PG jsonb 由驱动直接给 dict；SQLite/文本列给字符串
    if isinstance(checks, str):
        checks = json.loads(checks or "{}")
    return {
        "status": row["status"],
        "reason": row["reason"] or "",
        "checks": checks or {},
        "decided_at": row["decided_at"],
        "updated_at": row["updated_at"],
    }


def set_review(name, update, db_path=None, conn=None):
    """合并写入审阅状态。忽略/延后必填理由；status/checks/reason 之外字段拒绝。"""
    allowed = {"status", "reason", "checks"}
    extra = set(update) - allowed
    if extra:
        raise ValueError(f"未知字段: {','.join(sorted(extra))}")
    previous = get_review(name, db_path=db_path, conn=conn) or {
        "status": "pending", "reason": "", "checks": {},
    }
    status = update.get("status", previous["status"])
    if status not in STATUSES:
        raise ValueError("未知审阅状态")
    checks = {key: bool(val) for key, val in (update.get("checks") or {}).items()
              if key in dict(CHECKS)}
    merged_checks = {**previous["checks"], **checks}
    reason = update.get("reason", previous["reason"]) or ""
    if status in ("ignored", "deferred") and not reason.strip():
        raise ValueError("忽略或延后必须填写理由")
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    ph = sd.ph()
    with sd.write_tx(conn=conn, db_path=db_path) as own:
        own.execute(
            f"""
            INSERT INTO service_upgrade_reviews (svc_name, status, reason, checks, decided_at, updated_at)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph})
            ON CONFLICT(svc_name) DO UPDATE SET
                status=excluded.status,
                reason=excluded.reason,
                checks=excluded.checks,
                decided_at=excluded.decided_at,
                updated_at=excluded.updated_at
            """,
            (name, status, reason, json.dumps(merged_checks, ensure_ascii=False),
             now_str, now_str),
        )
    return {"status": status, "reason": reason, "checks": merged_checks,
            "decided_at": now_str, "updated_at": now_str}


def build_upgrade_prep(name, detail, svc, db_path=None, conn=None, now=None):
    """汇总版本详情 + 台账 + 持久化审阅为弹窗的 upgrade_prep 区块。

    detail 是 version_service.get_service_version_detail 的产物（可能为空 dict）；
    svc 是 assets.yaml 行。任何一侧缺失都如实进 blockers，不伪造就绪。
    """
    now = now or datetime.now(timezone.utc)
    detail = detail or {}
    svc = svc or {}
    review = get_review(name, db_path=db_path, conn=conn) or {
        "status": "pending", "reason": "", "checks": {},
        "decided_at": None, "updated_at": None,
    }

    blockers = []
    local = detail.get("local")
    upstream = detail.get("upstream")
    if not _fixed_version(local):
        blockers.append("本机版本未知，不能判定与上游差异")
    if not _freshness_recent(detail.get("last_checked"), now):
        blockers.append(_freshness_label(detail.get("last_checked"), now))

    notes = detail.get("release_notes") or ""
    changes = extract_changes(notes)
    if not detail.get("track_upstream", True):
        blockers.append("自部署服务不跟踪上游，无升级候选")
    elif not _fixed_version(upstream):
        blockers.append("上游版本未知，没有升级候选")
    elif not notes:
        blockers.append("报告只有元数据，尚未取得 Release 正文 / 迁移说明")

    missing = [label for key, label in CHECKS if not review["checks"].get(key)]
    blockers.extend(missing)

    if any(c["severity"] == "breaking" for c in changes):
        category = "breaking"
    elif any(c["severity"] == "security" for c in changes):
        category = "security"
    else:
        category = "feature"

    comparison = (
        "local_unknown" if not _fixed_version(local)
        else "candidate_unknown" if not _fixed_version(upstream)
        else "snapshot_matches" if _normalize(local) == _normalize(upstream)
        else "snapshot_differs"
    )

    return {
        "review": review,
        "checks": [{"key": key, "label": label, "done": bool(review["checks"].get(key))}
                   for key, label in CHECKS],
        "changes": changes,
        "category": category,
        "comparison": comparison,
        "blockers": blockers,
        "readiness": "needs_review" if blockers else "preparation_reviewed",
    }
