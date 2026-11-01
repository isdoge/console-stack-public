# -*- coding: utf-8 -*-
"""upgrade_prep 的回归：影响分类纯函数 + 审阅持久化 + readiness 判定（PG）。

分类规则与六项检查移植自 S6 model.mjs；持久化走 `service_upgrade_reviews`（迁移 14）。
"""
import pathlib
import sys
from datetime import datetime, timezone

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import upgrade_prep as up  # noqa: E402

pytest_plugins = ("pg02_support",)

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
FRESH = "2026-10-17 11:00:00"
STALE = "2026-10-16 20:00:00"  # 16h 前


class TestExtractChanges:
    def test_keyword_rules_hit_breaking_and_security(self):
        body = "\n".join([
            "## Breaking",
            "Removed the deprecated legacy config option",   # 配置 breaking
            "short",                                          # <5 字跳过
            "database migration required for schema 12",      # 数据库 breaking
            "Fixes CVE-2026-1234 authentication bypass",      # 安全
            "added a nice button",                            # 无命中
        ])
        changes = up.extract_changes(body)
        cats = {(c["category"], c["severity"], c["line"]) for c in changes}
        assert ("配置", "breaking", 2) in cats
        assert ("数据库", "breaking", 4) in cats
        assert ("安全", "security", 5) in cats
        assert all("nice button" not in c["excerpt"] for c in changes)

    def test_empty_body(self):
        assert up.extract_changes("") == []
        assert up.extract_changes(None) == []


class TestReviewPersistence:
    def test_roundtrip_and_merge(self, pg_authority):
        saved = up.set_review("svc", {"checks": {"config": True}, "status": "pending"})
        assert saved["checks"] == {"config": True}
        again = up.set_review("svc", {"checks": {"data": True}})
        # 增量勾选合并而不是覆盖
        assert again["checks"] == {"config": True, "data": True}
        loaded = up.get_review("svc")
        assert loaded["status"] == "pending"
        assert loaded["checks"] == {"config": True, "data": True}
        assert loaded["updated_at"]

    def test_ignore_requires_reason(self, pg_authority):
        with pytest.raises(ValueError, match="理由"):
            up.set_review("svc", {"status": "ignored"})
        ok = up.set_review("svc", {"status": "ignored", "reason": "暂不升级"})
        assert ok["status"] == "ignored"

    def test_rejects_unknown_status_and_fields(self, pg_authority):
        with pytest.raises(ValueError, match="状态"):
            up.set_review("svc", {"status": "done"})
        with pytest.raises(ValueError, match="未知字段"):
            up.set_review("svc", {"svc_name": "other"})
        # 未知 check key 被丢弃但不报错
        saved = up.set_review("svc", {"checks": {"bogus": True, "config": True}})
        assert saved["checks"] == {"config": True}

    def test_missing_table_falls_back_to_none(self, pg_authority):
        # 表被人工删掉时不让版本详情整体失败（旧部署边界）
        import pg_test_support
        admin = pg_test_support.connect(pg_authority["admin_dsn"], autocommit=True)
        admin.execute("DROP TABLE service_upgrade_reviews")
        admin.close()
        assert up.get_review("svc") is None


def _detail(**kw):
    base = {"local": "1.0.0", "upstream": "1.1.0", "track_upstream": True,
            "release_notes": "fixes and improvements", "last_checked": FRESH}
    base.update(kw)
    return base


def _all_checks():
    return {key: True for key, _ in up.CHECKS}


class TestBuildUpgradePrep:
    def test_ready_when_clean_release_and_all_checks(self, pg_authority):
        up.set_review("svc", {"checks": _all_checks(), "status": "prepared"})
        prep = up.build_upgrade_prep("svc", _detail(), {}, now=NOW)
        assert prep["blockers"] == []
        assert prep["readiness"] == "preparation_reviewed"
        assert prep["category"] == "feature"
        assert prep["comparison"] == "snapshot_differs"
        assert all(c["done"] for c in prep["checks"])

    def test_blockers_collect_unknowns_and_missing_checks(self, pg_authority):
        prep = up.build_upgrade_prep("svc", _detail(local="latest", last_checked=STALE,
                                                    release_notes=""), {}, now=NOW)
        text = "\n".join(prep["blockers"])
        assert "本机版本未知" in text
        assert "过期快照" in text
        assert "尚未取得 Release 正文" in text
        assert "配置副本" in text  # 六项全缺
        assert prep["readiness"] == "needs_review"

    def test_breaking_and_security_category(self, pg_authority):
        notes = "### Changed\n- removed deprecated environment variable CONFIG_X"
        prep = up.build_upgrade_prep("svc", _detail(release_notes=notes), {}, now=NOW)
        assert prep["category"] == "breaking"
        assert prep["changes"][0]["category"] == "配置"

    def test_self_hosted_no_candidate(self, pg_authority):
        prep = up.build_upgrade_prep("svc", _detail(track_upstream=False), {}, now=NOW)
        assert "自部署服务" in "\n".join(prep["blockers"])

    def test_comparison_variants(self, pg_authority):
        assert up.build_upgrade_prep("s", _detail(local="v1.1.0"), {}, now=NOW)["comparison"] == "snapshot_matches"
        assert up.build_upgrade_prep("s", _detail(local="N/A"), {}, now=NOW)["comparison"] == "local_unknown"
        assert up.build_upgrade_prep("s", _detail(upstream="latest"), {}, now=NOW)["comparison"] == "candidate_unknown"

    def test_detail_none_is_safe(self, pg_authority):
        prep = up.build_upgrade_prep("svc", None, None, now=NOW)
        assert prep["readiness"] == "needs_review"
        assert prep["comparison"] == "local_unknown"
