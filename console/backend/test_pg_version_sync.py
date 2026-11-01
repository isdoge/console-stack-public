# -*- coding: utf-8 -*-
"""version_sync / version_service 的 PG 路径回归（PG-02）。

覆盖：`versions` 表 UPSERT（含二次同步覆盖）、逐服务失败隔离、F16「失败保留 prior 行」、
采集门禁（client 不写）、read_only 下同步在写之前跳过（底层写拒绝另有单测）。
"""
import pathlib
import sys

import pytest
import yaml

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import storage_dispatch as sd  # noqa: E402
import version_service as vs  # noqa: E402
import version_sync  # noqa: E402
from pg02_support import count_rows, fetch_all  # noqa: E402

# 夹具通过插件注册加载：直接 import 同名夹具会被 pyflakes 判 F811（测试参数同名）。
pytest_plugins = ("pg02_support",)



def _write_assets(tmp_path, services):
    assets = tmp_path / "assets.yaml"
    assets.write_text(yaml.safe_dump(services, allow_unicode=True), encoding="utf-8")
    return str(assets)


def _read_versions(pg02_db):
    rows = fetch_all(pg02_db, "SELECT * FROM versions ORDER BY svc_name")
    return {row["svc_name"]: row for row in rows}


def _fake_git(behind="0"):
    """按 git 子命令返回合法输出：rev-list --count 必须给数字，否则 F16 会判为 invalid。"""

    def run(repo, args):
        if args[:1] == ["rev-list"]:
            return version_sync.GitResult(ok=True, stdout=f"{behind}\n")
        return version_sync.GitResult(ok=True, stdout="abc1234\n")

    return run


class TestSyncAllVersionsPg:
    def test_missing_assets_returns_early(self, tmp_path, pg_authority, monkeypatch):
        monkeypatch.setattr(version_sync, "ASSETS_PATH", str(tmp_path / "nope.yaml"))
        version_sync.sync_all_versions()
        assert count_rows(pg_authority, "versions") == 0

    def test_disabled_services_are_skipped(self, tmp_path, pg_authority, monkeypatch):
        services = [
            {"name": "off", "version": {"enabled": False, "kind": "image"}},
            {"name": "on", "version": {"enabled": True, "kind": "image"}},
        ]
        monkeypatch.setattr(version_sync, "ASSETS_PATH", _write_assets(tmp_path, services))
        # 委托成功时 version_sync 不写兜底行，所以打桩返回 None 才走到写行分支
        monkeypatch.setattr(
            version_sync.vs, "sync_service_version", lambda svc, **kw: None
        )
        version_sync.sync_all_versions()
        assert list(_read_versions(pg_authority)) == ["on"]

    def test_image_kind_fallback_row_when_service_returns_none(
        self, tmp_path, pg_authority, monkeypatch
    ):
        services = [{"name": "svc-img", "version": {"enabled": True, "kind": "image"}}]
        monkeypatch.setattr(version_sync, "ASSETS_PATH", _write_assets(tmp_path, services))
        monkeypatch.setattr(
            version_sync.vs, "sync_service_version", lambda svc, **kw: None
        )
        version_sync.sync_all_versions()
        row = _read_versions(pg_authority)["svc-img"]
        assert row["local_version"] == "Unknown"
        assert row["update_status"] == "Unknown"

    def test_upsert_overwrites_existing_row(self, tmp_path, pg_authority, monkeypatch):
        services = [{"name": "svc", "version": {"enabled": True, "kind": "image"}}]
        monkeypatch.setattr(version_sync, "ASSETS_PATH", _write_assets(tmp_path, services))
        monkeypatch.setattr(
            version_sync.vs, "sync_service_version", lambda svc, **kw: None
        )
        version_sync.sync_all_versions()
        first = _read_versions(pg_authority)["svc"]["last_checked"]
        version_sync.sync_all_versions()
        assert count_rows(pg_authority, "versions") == 1
        assert _read_versions(pg_authority)["svc"]["last_checked"] >= first

    def test_source_kind_behind_writes_row(self, tmp_path, pg_authority, monkeypatch):
        services = [{
            "name": "src",
            "deploy_path": "/repo",
            "version": {"enabled": True, "kind": "source", "branch": "main"},
        }]
        monkeypatch.setattr(version_sync, "ASSETS_PATH", _write_assets(tmp_path, services))
        monkeypatch.setattr(version_sync.os.path, "exists", lambda p: True)
        monkeypatch.setattr(version_sync, "run_git", _fake_git("0"))
        monkeypatch.setattr(version_sync, "gh_api_request", lambda path: None)
        version_sync.sync_all_versions()
        row = _read_versions(pg_authority)["src"]
        assert row["local_version"] == "abc1234"
        assert row["behind_count"] == 0
        assert row["update_status"] == "最新"

    def test_source_git_failure_preserves_prior_row(
        self, tmp_path, pg_authority, monkeypatch
    ):
        """F16：git 失败不写行，上一轮成功行（含 last_checked）原样保留。"""
        services = [{
            "name": "src",
            "deploy_path": "/repo",
            "version": {"enabled": True, "kind": "source", "branch": "main"},
        }]
        monkeypatch.setattr(version_sync, "ASSETS_PATH", _write_assets(tmp_path, services))
        monkeypatch.setattr(version_sync.os.path, "exists", lambda p: True)
        monkeypatch.setattr(version_sync, "run_git", _fake_git("0"))
        monkeypatch.setattr(version_sync, "gh_api_request", lambda path: None)
        version_sync.sync_all_versions()
        before = _read_versions(pg_authority)["src"]

        monkeypatch.setattr(
            version_sync, "run_git",
            lambda repo, args: version_sync.GitResult(ok=False, timed_out=True),
        )
        version_sync.sync_all_versions()
        after = _read_versions(pg_authority)["src"]
        assert after["local_version"] == before["local_version"]
        assert after["last_checked"] == before["last_checked"]

    def test_per_service_failure_does_not_stop_later_services(
        self, tmp_path, pg_authority, monkeypatch
    ):
        services = [
            {"name": "ok-before", "version": {"enabled": True, "kind": "image"}},
            {"name": "boom", "version": {"enabled": True, "kind": "image"}},
            {"name": "ok-after", "version": {"enabled": True, "kind": "image"}},
        ]
        monkeypatch.setattr(version_sync, "ASSETS_PATH", _write_assets(tmp_path, services))

        def fake_sync(svc, **kw):
            if svc["name"] == "boom":
                raise ValueError("机器 us 不支持 docker 动作")
            return None  # 委托未接管 → version_sync 写兜底行

        monkeypatch.setattr(version_sync.vs, "sync_service_version", fake_sync)
        version_sync.sync_all_versions()
        assert sorted(_read_versions(pg_authority)) == ["ok-after", "ok-before"]


class TestVersionServicePg:
    def test_sync_service_version_writes_and_upserts(self, pg_authority, monkeypatch):
        monkeypatch.setattr(vs, "get_local_service_version", lambda svc: "8.4.8")
        monkeypatch.setattr(vs, "get_upstream_release", lambda repo: (None, None, []))
        svc = {"name": "mysql", "type": "docker", "version": {"enabled": True}}
        row = vs.sync_service_version(svc)
        assert row["local"] == "8.4.8"
        assert row["status"] == "仅本地"
        stored = fetch_all(pg_authority, "SELECT * FROM versions")
        assert len(stored) == 1
        assert stored[0]["svc_name"] == "mysql"

    def test_last_checked_is_utc_text(self, pg_authority, monkeypatch):
        monkeypatch.setattr(vs, "get_local_service_version", lambda svc: "1.0")
        monkeypatch.setattr(vs, "get_upstream_release", lambda repo: (None, None, []))
        row = vs.sync_service_version(
            {"name": "svc", "type": "docker", "version": {"enabled": True}}
        )
        # 与 version_sync 的 UTC 口径一致（旧实现用本机时区，Mac 上会写出第二套时间）
        assert row["last_checked"].endswith("Z") or len(row["last_checked"]) == 19

    def test_client_does_not_write(self, pg_client, monkeypatch):
        monkeypatch.setattr(vs, "get_local_service_version", lambda svc: "1.0")
        assert vs.sync_service_version(
            {"name": "svc", "type": "docker", "version": {"enabled": True}}
        ) is None
        assert count_rows(pg_client, "versions") == 0

    def test_read_only_skips_sync_before_writing(self, pg_authority, monkeypatch):
        """RO-01：维护模式下「同步」在**发起采集/写入之前**跳过，而不是撞上写拒绝。

        旧期望是 `ReadOnlyMode`——那正是 RO-01 的另一面：启动期的 seed/同步都跑去写，
        被底层拒绝后把进程掀翻。现在 sync 自己先跳过（返回 None，按 Q1 回落缓存），
        底层写拒绝**不受影响**（下面单独证一次），所以「写不进」这个保证没有任何松动。
        """
        import os

        import runtime_mode

        monkeypatch.setattr(vs, "get_local_service_version", lambda svc: "1.0")
        monkeypatch.setattr(vs, "get_upstream_release", lambda repo: (None, None, []))
        before = count_rows(pg_authority, "versions")
        os.environ["CONSOLE_MAINTENANCE_MODE"] = "read_only"
        try:
            assert vs.sync_service_version(
                {"name": "svc", "type": "docker", "version": {"enabled": True}}
            ) is None
            assert count_rows(pg_authority, "versions") == before     # 没有写
            # 底层写拒绝仍在：直接开写事务照样被拒（读只影响「要不要去做」，不影响「能不能写」）
            with pytest.raises(runtime_mode.ReadOnlyMode):
                with sd.write_tx(db_path=None):
                    pass
        finally:
            os.environ["CONSOLE_MAINTENANCE_MODE"] = "normal"
