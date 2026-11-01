# -*- coding: utf-8 -*-
"""Regression tests for version_sync.py — 定时版本同步脚本。

遵循 checker 测试隔离规范：不触网、不跑真实 git，subprocess 与 GitHub API
全部 monkeypatch；DB / assets.yaml 全部指向 tmp_path，不碰真实 console.db。
"""
import json
import pathlib
import sqlite3
import subprocess
import sys

import pytest
import yaml

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import version_sync  # noqa: E402

# ---------------------------------------------------------------------------
# run_git_cmd — mock subprocess.run
# ---------------------------------------------------------------------------

class TestRunGitCmd:
    def test_success_returns_stripped_stdout(self, monkeypatch):
        seen = {}

        def fake_run(cmd, stdout=None, stderr=None, text=None, check=None,
                     timeout=None):
            seen["cmd"] = cmd
            seen["timeout"] = timeout
            return subprocess.CompletedProcess(cmd, 0, stdout="  abc1234\n", stderr="")

        monkeypatch.setattr(version_sync.subprocess, "run", fake_run)
        result = version_sync.run_git("/repo", ["rev-parse", "--short", "HEAD"])
        assert result.ok is True
        assert result.value == "abc1234"
        assert seen["cmd"] == ["git", "-C", "/repo", "rev-parse", "--short", "HEAD"]
        # F16：git 子进程必须带 30 秒超时
        assert seen["timeout"] == 30

    def test_success_wrapper_returns_stripped_stdout(self, monkeypatch):
        """兼容包装 run_git_cmd 成功时返回去空白 stdout 字符串。"""
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="abc1234\n", stderr="")

        monkeypatch.setattr(version_sync.subprocess, "run", fake_run)
        assert version_sync.run_git_cmd("/repo", ["rev-parse", "HEAD"]) == "abc1234"

    def test_failure_returns_error_string(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            raise subprocess.CalledProcessError(128, cmd, stderr="fatal: not a git repo")

        monkeypatch.setattr(version_sync.subprocess, "run", fake_run)
        result = version_sync.run_git("/repo", ["fetch", "-q"])
        # F16：失败返回结构化结果，不返回错误字符串；stdout 必须为空
        assert result.ok is False
        assert result.value == ""
        assert result.category == "exit"

    def test_timeout_returns_timed_out_result(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, timeout=30)

        monkeypatch.setattr(version_sync.subprocess, "run", fake_run)
        result = version_sync.run_git("/repo", ["fetch", "-q"])
        assert result.ok is False
        assert result.timed_out is True
        assert result.category == "timeout"
        assert result.value == ""

    def test_real_subprocess_on_missing_repo(self, tmp_path):
        """真实执行：非 git 目录 check=True 抛 CalledProcessError → 结构化失败。"""
        result = version_sync.run_git(str(tmp_path), ["rev-parse", "HEAD"])
        assert result.ok is False
        assert result.value == ""
        assert result.category in ("exit", "error")


# ---------------------------------------------------------------------------
# gh_api_request — mock urllib.request.urlopen
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestGhApiRequest:
    def test_returns_parsed_json(self, monkeypatch):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["auth"] = req.get_header("Authorization")
            return _FakeResponse({"tag_name": "v1.0.0"})

        monkeypatch.setattr(version_sync.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(version_sync, "GITHUB_TOKEN", "")
        data = version_sync.gh_api_request("/repos/foo/bar/releases/latest")
        assert data == {"tag_name": "v1.0.0"}
        assert seen["url"] == "https://api.github.com/repos/foo/bar/releases/latest"
        assert seen["auth"] is None

    def test_token_attached_when_configured(self, monkeypatch):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            return _FakeResponse({})

        monkeypatch.setattr(version_sync.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(version_sync, "GITHUB_TOKEN", "tok123")
        version_sync.gh_api_request("/repos/foo/bar")
        assert seen["auth"] == "token tok123"

    def test_network_error_returns_none(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            raise version_sync.urllib.error.URLError("unreachable")

        monkeypatch.setattr(version_sync.urllib.request, "urlopen", fake_urlopen)
        assert version_sync.gh_api_request("/repos/foo/bar") is None


# ---------------------------------------------------------------------------
# get_docker_image_digest_from_github
# ---------------------------------------------------------------------------

class TestGetDockerImageDigest:
    @pytest.mark.parametrize("url", ["", "https://gitlab.com/foo/bar", "not-a-url"])
    def test_non_github_url_returns_none(self, url):
        assert version_sync.get_docker_image_digest_from_github(url) is None

    def test_returns_latest_release_tag(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            version_sync, "gh_api_request",
            lambda p: seen.update(path=p) or {"tag_name": "v3.2.1"},
        )
        tag = version_sync.get_docker_image_digest_from_github(
            "https://github.com/henrygd/beszel.git"
        )
        assert tag == "v3.2.1"
        # .git 后缀必须剥离
        assert seen["path"] == "/repos/henrygd/beszel/releases/latest"

    def test_no_release_returns_none(self, monkeypatch):
        monkeypatch.setattr(version_sync, "gh_api_request", lambda p: None)
        assert version_sync.get_docker_image_digest_from_github(
            "https://github.com/foo/bar"
        ) is None


# ---------------------------------------------------------------------------
# sync_all_versions — tmp DB + tmp assets.yaml，git/API 全 mock
# ---------------------------------------------------------------------------

def _write_assets(tmp_path, services):
    assets = tmp_path / "assets.yaml"
    assets.write_text(yaml.safe_dump(services, allow_unicode=True), encoding="utf-8")
    return str(assets)


def _read_versions(db_path):
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT svc_name, local_version, upstream_version, behind_count,"
            " update_status, recent_changes FROM versions"
        ).fetchall()
    finally:
        conn.close()
    return {r[0]: r for r in rows}


@pytest.fixture(autouse=True)
def _authority(monkeypatch):
    """版本同步受采集门禁约束（Q1）：测试显式声明 authority。"""
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authority")


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "console.db")
    monkeypatch.setattr(version_sync, "DB_PATH", db_path)
    # versions 表在 SQLite 侧由 app.init_db() 建；PG-02 起业务模块不再自己
    # CREATE TABLE（PG 运行账号没有 DDL 权限），所以测试显式建表。
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE versions (svc_name TEXT PRIMARY KEY, local_version TEXT,"
            " upstream_version TEXT, behind_count INTEGER, update_status TEXT,"
            " recent_changes TEXT, last_checked TEXT)"
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


class TestSyncAllVersions:
    def test_missing_assets_returns_early(self, tmp_path, tmp_db, monkeypatch):
        monkeypatch.setattr(version_sync, "ASSETS_PATH", str(tmp_path / "nope.yaml"))
        version_sync.sync_all_versions(db_path=tmp_db)
        assert _read_versions(tmp_db) == {}

    def test_disabled_services_are_skipped(self, tmp_path, tmp_db, monkeypatch):
        services = [
            {"name": "svc-off"},
            {"name": "svc-off2", "version": {"enabled": False}},
        ]
        monkeypatch.setattr(version_sync, "ASSETS_PATH", _write_assets(tmp_path, services))
        version_sync.sync_all_versions(db_path=tmp_db)
        assert _read_versions(tmp_db) == {}

    @staticmethod
    def _git_result(out):
        """构造成功 GitResult 假件（兼容旧的按 args 路由 stub）。"""
        if isinstance(out, str):
            return version_sync.GitResult(ok=True, stdout=out + "\n")
        return version_sync.GitResult(ok=True, stdout="\n".join(out) + "\n")

    def test_source_kind_behind_writes_row(self, tmp_path, tmp_db, monkeypatch):
        deploy = tmp_path / "deploy"
        deploy.mkdir()
        services = [{
            "name": "svc-src",
            "deploy_path": str(deploy),
            "version": {
                "enabled": True,
                "kind": "source",
                "repo_url": "https://github.com/foo/bar.git",
                "branch": "main",
            },
        }]
        monkeypatch.setattr(version_sync, "ASSETS_PATH", _write_assets(tmp_path, services))

        def fake_git(repo_path, args):
            assert repo_path == str(deploy)
            if args[:2] == ["rev-parse", "--short"] and args[-1] == "HEAD":
                return self._git_result("abc1234")
            if args[0] == "fetch":
                return self._git_result("")
            if args[0] == "rev-list":
                return self._git_result("3")
            return self._git_result("def5678")

        def fake_gh(url_path):
            if url_path.endswith("/releases/latest"):
                return {"tag_name": "v2.0.0"}
            if "/commits?" in url_path:
                return [
                    {"sha": "abcdef0123456", "commit": {"message": "feat: x\n\nbody"}},
                    {"sha": "1234567890abc", "commit": {"message": "fix: y"}},
                ]
            return None

        monkeypatch.setattr(version_sync, "run_git", fake_git)
        monkeypatch.setattr(version_sync, "gh_api_request", fake_gh)

        version_sync.sync_all_versions(db_path=tmp_db)
        rows = _read_versions(tmp_db)
        name, local, upstream, behind, status, changes = rows["svc-src"]
        assert (local, upstream, behind, status) == ("abc1234", "v2.0.0", 3, "落后")
        parsed = json.loads(changes)
        assert parsed[0] == {"sha": "abcdef0", "msg": "feat: x"}
        assert len(parsed) == 2

    def test_source_kind_up_to_date_without_release(self, tmp_path, tmp_db, monkeypatch):
        deploy = tmp_path / "deploy"
        deploy.mkdir()
        services = [{
            "name": "svc-src2",
            "deploy_path": str(deploy),
            "version": {
                "enabled": True,
                "kind": "source",
                "repo_url": "https://github.com/foo/bar",
            },
        }]
        monkeypatch.setattr(version_sync, "ASSETS_PATH", _write_assets(tmp_path, services))

        def fake_git(repo_path, args):
            if args[:2] == ["rev-parse", "--short"]:
                return self._git_result("abc1234")
            if args[0] == "rev-list":
                return self._git_result("0")
            return self._git_result("")

        monkeypatch.setattr(version_sync, "run_git", fake_git)
        # 无 Release / 无 commits → 上游版本回退到 origin 分支短 hash
        monkeypatch.setattr(version_sync, "gh_api_request", lambda p: None)

        version_sync.sync_all_versions(db_path=tmp_db)
        rows = _read_versions(tmp_db)
        _, local, upstream, behind, status, _ = rows["svc-src2"]
        assert (local, upstream, behind, status) == ("abc1234", "abc1234", 0, "最新")

    def test_source_kind_missing_deploy_path_reports_failure(
        self, tmp_path, tmp_db, monkeypatch, capsys
    ):
        """F16：source 但 deploy_path 不存在 → 不写伪装 Unknown 行，只报脱敏失败。"""
        services = [{
            "name": "svc-gone",
            "deploy_path": str(tmp_path / "not-there"),
            "version": {"enabled": True, "kind": "source", "repo_url": ""},
        }]
        monkeypatch.setattr(version_sync, "ASSETS_PATH", _write_assets(tmp_path, services))
        monkeypatch.setattr(
            version_sync, "run_git",
            lambda *a: pytest.fail("git should not run without deploy_path"),
        )
        version_sync.sync_all_versions(db_path=tmp_db)
        # 关键断言：没有可用 checkout 就不插入伪装成功的 Unknown/零行
        assert _read_versions(tmp_db) == {}
        out = capsys.readouterr().out
        assert "svc-gone" in out
        assert "1 failure" in out

    def test_image_kind_delegates_to_version_service(self, tmp_path, tmp_db, monkeypatch):
        services = [{
            "name": "svc-img",
            "type": "docker",
            "version": {
                "enabled": True,
                "kind": "image",
                "repo_url": "https://github.com/foo/bar",
            },
        }]
        monkeypatch.setattr(version_sync, "ASSETS_PATH", _write_assets(tmp_path, services))
        seen = {}
        monkeypatch.setattr(
            version_sync.vs, "sync_service_version",
            lambda svc, **kwargs: seen.update(name=svc["name"]) or {"local": "v1"},
        )
        version_sync.sync_all_versions(db_path=tmp_db)
        assert seen["name"] == "svc-img"
        # version_service 成功接管 → 本脚本不再写兜底行
        assert _read_versions(tmp_db) == {} or _read_versions(tmp_db) == {}

    def test_image_kind_without_repo_still_collects_local_version(
        self, tmp_path, tmp_db, monkeypatch
    ):
        services = [{
            "name": "mysql",
            "type": "docker",
            "version": {"enabled": True, "kind": "image"},
        }]
        monkeypatch.setattr(version_sync, "ASSETS_PATH", _write_assets(tmp_path, services))
        seen = {}
        monkeypatch.setattr(
            version_sync.vs,
            "sync_service_version",
            lambda svc, **kwargs: seen.update(name=svc["name"]) or {"local": "8.4.8"},
        )
        version_sync.sync_all_versions(db_path=tmp_db)
        assert seen["name"] == "mysql"

    def test_http_kind_delegates_to_version_service(
        self, tmp_path, tmp_db, monkeypatch
    ):
        services = [{
            "name": "lucky",
            "type": "process",
            "version": {
                "enabled": True,
                "kind": "http",
                "repo_url": "https://github.com/gdy666/lucky",
            },
        }]
        monkeypatch.setattr(version_sync, "ASSETS_PATH", _write_assets(tmp_path, services))
        seen = {}
        monkeypatch.setattr(
            version_sync.vs,
            "sync_service_version",
            lambda svc, **kwargs: seen.update(name=svc["name"]) or {"local": "v2.27.2"},
        )
        version_sync.sync_all_versions(db_path=tmp_db)
        assert seen["name"] == "lucky"

    def test_image_kind_fallback_row_when_service_returns_none(
        self, tmp_path, tmp_db, monkeypatch
    ):
        services = [{
            "name": "svc-img2",
            "version": {
                "enabled": True,
                "kind": "image",
                "repo_url": "https://github.com/foo/bar",
            },
        }]
        monkeypatch.setattr(version_sync, "ASSETS_PATH", _write_assets(tmp_path, services))
        monkeypatch.setattr(
            version_sync.vs, "sync_service_version",
            lambda svc, **kwargs: None,
        )
        version_sync.sync_all_versions(db_path=tmp_db)
        rows = _read_versions(tmp_db)
        _, local, upstream, behind, status, _ = rows["svc-img2"]
        assert (local, upstream, status) == ("Unknown", "Unknown", "Unknown")

    def test_upsert_overwrites_existing_row(self, tmp_path, tmp_db, monkeypatch):
        services = [{
            "name": "svc-up",
            "version": {"enabled": True, "kind": "image", "repo_url": "https://github.com/f/b"},
        }]
        monkeypatch.setattr(version_sync, "ASSETS_PATH", _write_assets(tmp_path, services))
        monkeypatch.setattr(
            version_sync.vs, "sync_service_version",
            lambda svc, **kwargs: None,
        )
        version_sync.sync_all_versions(db_path=tmp_db)
        version_sync.sync_all_versions(db_path=tmp_db)  # 二次同步走 ON CONFLICT 更新
        rows = _read_versions(tmp_db)
        assert len(rows) == 1
        assert "svc-up" in rows


# ---------------------------------------------------------------------------
# 逐服务错误隔离（2026-09-07 加固）
# ---------------------------------------------------------------------------

class TestPerServiceErrorIsolation:
    """一个服务出错绝不能中止整轮同步。

    2026-09-07 实测过不隔离的代价：`axonhub-oracle`（21 个开版本追踪的服务里排
    第 2）因机器 `us` 缺 docker 能力抛 `ValueError: 机器 us 不支持 docker 动作`，
    循环当场中止，**后面 19 个服务的版本数据被一起冻住**，每 900s 复现一次；
    而外层只打印一句 `background version sync failed`，从现象上完全看不出波及面。
    这组用例就是防它复发的。
    """

    def _three_services(self):
        return [
            {"name": "ok-before", "type": "docker",
             "version": {"enabled": True, "kind": "image"}},
            {"name": "boom", "type": "docker",
             "version": {"enabled": True, "kind": "image"}},
            {"name": "ok-after", "type": "docker",
             "version": {"enabled": True, "kind": "image"}},
        ]

    def _install(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            version_sync, "ASSETS_PATH", _write_assets(tmp_path, self._three_services())
        )
        seen = []

        def fake_sync(svc, **kwargs):
            seen.append(svc["name"])
            if svc["name"] == "boom":
                raise ValueError("机器 us 不支持 docker 动作")
            return {"local": "v1"}

        monkeypatch.setattr(version_sync.vs, "sync_service_version", fake_sync)
        return seen

    def test_failure_does_not_stop_later_services(self, tmp_path, tmp_db, monkeypatch):
        seen = self._install(tmp_path, monkeypatch)
        version_sync.sync_all_versions(db_path=tmp_db)
        # 关键断言：坏的那个之后的服务**仍被处理**（不隔离时这里只会有前两个）
        assert seen == ["ok-before", "boom", "ok-after"]

    def test_whole_run_still_completes_without_raising(self, tmp_path, tmp_db, monkeypatch):
        self._install(tmp_path, monkeypatch)
        version_sync.sync_all_versions(db_path=tmp_db)  # 不抛异常即通过

    def test_failures_are_reported_not_swallowed(self, tmp_path, tmp_db, monkeypatch, capsys):
        """静默跳过和中止一样坏——坏了必须能从输出看出来是谁坏了。"""
        self._install(tmp_path, monkeypatch)
        version_sync.sync_all_versions(db_path=tmp_db)
        out = capsys.readouterr().out
        assert "1 failure" in out
        assert "boom" in out
        assert "不支持 docker 动作" in out

    def test_clean_run_reports_no_failures(self, tmp_path, tmp_db, monkeypatch, capsys):
        monkeypatch.setattr(
            version_sync, "ASSETS_PATH",
            _write_assets(tmp_path, [self._three_services()[0]]),
        )
        monkeypatch.setattr(
            version_sync.vs, "sync_service_version", lambda svc, **kwargs: {"local": "v1"}
        )
        version_sync.sync_all_versions(db_path=tmp_db)
        out = capsys.readouterr().out
        assert "Version synchronization completed." in out
        assert "failure" not in out


# ---------------------------------------------------------------------------
# F16 source git 失败不得伪装成功 / 不得覆盖先前成功值（2026-09-08）
# ---------------------------------------------------------------------------

class TestF16SourceGitFailureIsolation:
    """F16：source 类型 git 失败（超时/非零/fetch 失败/非法计数）绝不能：
    1. 把错误当版本写入（伪装 local_version）；
    2. 把失败当 behind=0（伪装「最新」）；
    3. 覆盖上一次成功行的字段与 last_checked；
    4. 为一个不可用 checkout 新插入 Unknown/零行。
    失败必须只留下可追溯、脱敏的逐服务摘要，且不输出命令/remote/异常原文。
    """

    def _asset(self, tmp_path, deploy_path):
        return [{
            "name": "svc-src",
            "deploy_path": str(deploy_path),
            "version": {
                "enabled": True,
                "kind": "source",
                "repo_url": "https://github.com/acme/private.git",
                "branch": "main",
            },
        }]

    def _install_assets(self, tmp_path, tmp_db, monkeypatch, deploy=None):
        deploy = deploy or (tmp_path / "deploy")
        deploy.mkdir(exist_ok=True)
        monkeypatch.setattr(version_sync, "ASSETS_PATH",
                            _write_assets(tmp_path, self._asset(tmp_path, deploy)))
        monkeypatch.setattr(version_sync, "gh_api_request", lambda p: None)
        return deploy

    @staticmethod
    def _git_ok(out="abc1234"):
        return subprocess.CompletedProcess(["git"], 0, stdout=out + "\n", stderr="")

    def _fake_git_run(self, *, fail_at=None):
        """按命令路由的 subprocess.run 假件。

        fail_at 为 None → 全部成功；否则把阶段命令抛 TimeoutExpired。
        返回 (seen, fake_run)。命令以 [..., 'rev-parse', '--short', '<ref>']
        的形式进入，这里按 tail 关键词区分阶段。
        """
        seen = {"calls": [], "timeouts": 0}

        def fake_run(cmd, stdout=None, stderr=None, text=None, check=None,
                     timeout=None):
            seen["calls"].append(cmd)
            args = cmd[3:]  # ["git", "-C", path, *args]
            stage = None
            if args[:2] == ["fetch", "-q"]:
                stage = "fetch"
            elif args[:2] == ["rev-parse", "--short"]:
                stage = "local" if args[-1] == "HEAD" else "upstream"
            elif args[:1] == ["rev-list"]:
                stage = "behind"
            if stage == fail_at:
                seen["timeouts"] += 1
                raise subprocess.TimeoutExpired(cmd, timeout)
            if stage == "behind":
                return self._git_ok("0")
            return self._git_ok("abc1234")

        return seen, fake_run

    def _sync_source_once(self, tmp_path, tmp_db, monkeypatch, fail_at=None):
        """同步一轮；fail_at 为 'local'/'fetch'/'behind' 时该阶段超时。"""
        self._install_assets(tmp_path, tmp_db, monkeypatch)
        seen, fake_run = self._fake_git_run(fail_at=fail_at)
        monkeypatch.setattr(version_sync.subprocess, "run", fake_run)
        version_sync.sync_all_versions(db_path=tmp_db)
        return seen, _read_versions(tmp_db)

    # --- 红测 1：local rev-parse 超时 → 不写错误字符串为版本、不新插伪装行 ---
    def test_timeout_does_not_write_error_string_as_local_version(
        self, tmp_path, tmp_db, monkeypatch
    ):
        seen, rows = self._sync_source_once(tmp_path, tmp_db, monkeypatch,
                                            fail_at="local")
        assert seen["timeouts"] == 1  # local 阶段确实超时
        assert rows.get("svc-src") is None  # 不插 Unknown/0 伪装行
        assert not any(r[1].startswith("Error:") for r in rows.values())

    # --- 红测 2：fetch 失败 → 不写行、绝不报 behind=0/最新 ---
    def test_fetch_failure_never_reports_up_to_date(
        self, tmp_path, tmp_db, monkeypatch
    ):
        seen, rows = self._sync_source_once(tmp_path, tmp_db, monkeypatch,
                                            fail_at="fetch")
        assert seen["timeouts"] == 1  # fetch 确实失败
        row = rows.get("svc-src")
        # fetch 失败 → 本服务不写任何行（不得报 0/最新），也不新插 Unknown
        assert row is None

    # --- 红测 3：source 失败覆盖先前成功行（保留旧值+时间）---
    def test_failure_preserves_prior_success_row(
        self, tmp_path, tmp_db, monkeypatch
    ):
        self._sync_source_once(tmp_path, tmp_db, monkeypatch)  # 第一轮成功
        old_last_checked = self._last_checked(tmp_db)

        # 第二轮 fetch 失败
        seen, rows = self._sync_source_once(tmp_path, tmp_db, monkeypatch,
                                            fail_at="fetch")
        assert seen["timeouts"] == 1
        row = rows["svc-src"]
        # 不覆盖成功字段、不刷新 last_checked、不得变 Unknown
        assert row[1] == "abc1234"
        assert row[2] == "abc1234"
        assert row[4] == "最新"
        assert self._last_checked(tmp_db) == old_last_checked

    # --- 红测 4：非法计数（非数字）绝不当作 behind=0/最新 ---
    def test_invalid_count_never_treated_as_zero(
        self, tmp_path, tmp_db, monkeypatch
    ):
        self._sync_source_once(tmp_path, tmp_db, monkeypatch)  # 第一轮成功
        old_last_checked = self._last_checked(tmp_db)

        self._install_assets(tmp_path, tmp_db, monkeypatch)
        seen = {"invalid_counts": 0}

        def fake_run(cmd, **kwargs):
            # 命令形状是 ["git", "-C", path, ...]，阶段要看 path 之后的参数。
            if cmd[3:4] == ["rev-list"]:
                seen["invalid_counts"] += 1
                return subprocess.CompletedProcess(cmd, 0, stdout="oops\n", stderr="")
            return self._git_ok()

        monkeypatch.setattr(version_sync.subprocess, "run", fake_run)
        version_sync.sync_all_versions(db_path=tmp_db)
        assert seen["invalid_counts"] == 1  # 非法计数确实被注入
        row = _read_versions(tmp_db)["svc-src"]
        # 非法计数 → 本服务同步失败，保留 prior 行，不把 behind 当 0
        assert row[3] == 0  # prior 成功行的旧 behind 值（不新写 0）
        assert row[4] == "最新"  # prior 状态保持
        assert self._last_checked(tmp_db) == old_last_checked

    @staticmethod
    def _last_checked(tmp_db):
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(tmp_db)
        try:
            return conn.execute(
                "SELECT last_checked FROM versions WHERE svc_name='svc-src'"
            ).fetchone()[0]
        finally:
            conn.close()
