# -*- coding: utf-8 -*-
"""Regression tests for version_service.py — 纯函数解析 + DB 建库场景。

遵循 checker 测试隔离规范：不触网、不依赖真实 docker/ssh，
外部命令与网络全部 monkeypatch。
"""
import io
import pathlib
import sys
import tarfile

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import db  # noqa: E402
import version_service as vs  # noqa: E402

# ---------------------------------------------------------------------------
# _parse_image_tag
# ---------------------------------------------------------------------------

class TestParseImageTag:
    @pytest.mark.parametrize("ref,expected", [
        ("nginx:1.25", "1.25"),
        ("ghcr.io/foo/bar:v2.3.4", "v2.3.4"),
        ("registry.example.com/team/app:latest", "latest"),
        ("nginx", "latest"),
        ("", ""),
        (None, ""),
    ])
    def test_common_refs(self, ref, expected):
        assert vs._parse_image_tag(ref) == expected

    def test_digest_ref_without_tag(self):
        # name@sha256:... 无冒号 tag 时归为 latest
        assert vs._parse_image_tag("nginx@sha256:abcd") == "latest"


# ---------------------------------------------------------------------------
# _version_from_labels
# ---------------------------------------------------------------------------

class TestVersionFromLabels:
    def test_dict_with_known_key(self):
        assert vs._version_from_labels(
            {"org.opencontainers.image.version": "1.2.3"}
        ) == "1.2.3"

    def test_json_string_labels(self):
        assert vs._version_from_labels(
            '{"org.opencontainers.image.version": "9.9"}'
        ) == "9.9"

    def test_invalid_json_string_returns_none(self):
        assert vs._version_from_labels("{not json") is None

    def test_empty_and_none(self):
        assert vs._version_from_labels(None) is None
        assert vs._version_from_labels({}) is None

    def test_unknown_keys_return_none(self):
        assert vs._version_from_labels({"random": "1.0"}) is None


# ---------------------------------------------------------------------------
# _format_semver
# ---------------------------------------------------------------------------

class TestFormatSemver:
    @pytest.mark.parametrize("raw,expected", [
        ("1.2.3", "v1.2.3"),
        ("v1.2.3", "v1.2.3"),
        ("2.0", "v2.0"),
        ("latest", "latest"),
        ("2024-07-01", "2024-07-01"),
    ])
    def test_formats(self, raw, expected):
        assert vs._format_semver(raw) == expected


# ---------------------------------------------------------------------------
# _parse_github_repo
# ---------------------------------------------------------------------------


class TestNormalizeTag:
    @pytest.mark.parametrize("raw,expected", [
        ("v1.2.3", "1.2.3"),
        ("1.2.3", "1.2.3"),
        # semver build metadata（`+` 之后）不参与版本比较：上游 release tag 常带
        # 打包信息（ignis v0.8.11+obsidian.1.12.7），本地探针只报核心版本号。
        ("v0.8.11+obsidian.1.12.7", "0.8.11"),
        ("0.8.11+obsidian.1.12.7", "0.8.11"),
        ("", ""),
        (None, ""),
    ])
    def test_normalize(self, raw, expected):
        assert vs._normalize_tag(raw) == expected

    def test_build_metadata_does_not_count_as_outdated(self):
        assert not vs._versions_differ("0.8.11", "v0.8.11+obsidian.1.12.7")
        assert vs._versions_differ("0.8.10", "v0.8.11+obsidian.1.12.7")


class TestParseGithubRepo:
    @pytest.mark.parametrize("url,owner,repo", [
        ("https://github.com/henrygd/beszel", "henrygd", "beszel"),
        ("https://github.com/foo/bar.git", "foo", "bar"),
        ("https://github.com/foo/bar/", "foo", "bar"),
    ])
    def test_valid(self, url, owner, repo):
        assert vs._parse_github_repo(url) == (owner, repo)

    @pytest.mark.parametrize("url", ["", None, "https://gitlab.com/foo/bar"])
    def test_invalid(self, url):
        assert vs._parse_github_repo(url) == (None, None)


class TestCompareChangelog:
    REPO = "https://github.com/yetone/magpie"

    def test_unknown_version_falls_back_to_releases(self, monkeypatch):
        """本机版本未知时不再拼 compare 死链（…/compare/未知...v1.2.3），落 releases 页。"""
        called = {}

        def fake_gh_api(path):
            called["path"] = path
            return {}

        monkeypatch.setattr(vs, "_gh_api", fake_gh_api)
        commits, url = vs.get_compare_changelog(self.REPO, "未知", "v0.1.1268")
        assert commits == []
        assert url == "https://github.com/yetone/magpie/releases"
        assert "compare" not in called.get("path", "")

    def test_api_failure_falls_back_to_releases(self, monkeypatch):
        def fake_gh_api(path):
            raise RuntimeError("boom")

        monkeypatch.setattr(vs, "_gh_api", fake_gh_api)
        commits, url = vs.get_compare_changelog(self.REPO, "v0.1.1257", "v0.1.1268")
        assert commits == []
        assert url == "https://github.com/yetone/magpie/releases"

    def test_same_version_no_compare_call(self, monkeypatch):
        called = {"n": 0}

        def fake_gh_api(path):
            called["n"] += 1
            return {}

        monkeypatch.setattr(vs, "_gh_api", fake_gh_api)
        commits, url = vs.get_compare_changelog(self.REPO, "v0.1.1257", "v0.1.1257")
        assert commits == []
        assert url == "https://github.com/yetone/magpie/releases"
        assert called["n"] == 0


# ---------------------------------------------------------------------------
# _probe_exec_version / _probe_file_version — mock sm._run
# ---------------------------------------------------------------------------

class TestProbes:
    def test_exec_version_pattern_match(self, monkeypatch):
        # 远端机器仍允许 exec；NAS 默认禁用见 TestExecProbeGate
        monkeypatch.setattr(vs.sm, "_docker_cmd", lambda m, args: ["docker", *args])
        monkeypatch.setattr(vs.sm, "_run", lambda cmd, timeout=15: (0, "app version 3.4.5\n", ""))
        ver = vs._probe_exec_version("txy", "app", ["app", "--version"], r"(\d+\.\d+\.\d+)")
        assert ver == "v3.4.5"

    def test_exec_version_nonzero_exit_returns_none(self, monkeypatch):
        monkeypatch.setattr(vs.sm, "_docker_cmd", lambda m, args: ["docker", *args])
        monkeypatch.setattr(vs.sm, "_run", lambda cmd, timeout=15: (1, "", "boom"))
        assert vs._probe_exec_version("txy", "app", ["app", "--version"], None) is None

    def test_file_version_json(self, monkeypatch):
        monkeypatch.setattr(
            vs,
            "_copy_container_file",
            lambda machine, container, path: b'{"version": "2.1.0"}',
        )
        assert vs._probe_file_version("txy", "app", "/app/package.json") == "v2.1.0"

    def test_file_version_bad_json_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            vs,
            "_copy_container_file",
            lambda machine, container, path: b"not-json",
        )
        assert vs._probe_file_version("txy", "app", "/app/package.json") is None

    def test_copy_container_file_reads_docker_cp_tar_without_exec(self, monkeypatch):
        payload = b'{"version": "1.269.0"}'
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            info = tarfile.TarInfo("package.json")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

        seen = {}
        monkeypatch.setattr(
            vs.sm,
            "_docker_cmd",
            lambda machine, args: seen.update(machine=machine, args=args) or ["docker", *args],
        )
        monkeypatch.setattr(
            vs,
            "_run_bytes",
            lambda cmd, timeout=20: (0, stream.getvalue(), b""),
        )

        assert vs._copy_container_file("nas", "metacubexd", "/app/package.json") == payload
        assert seen == {
            "machine": "nas",
            "args": ["cp", "metacubexd:/app/package.json", "-"],
        }

    def test_copy_container_file_rejects_unsafe_path(self, monkeypatch):
        monkeypatch.setattr(
            vs,
            "_run_bytes",
            lambda *args, **kwargs: pytest.fail("不应执行 docker cp"),
        )
        assert vs._copy_container_file(
            "nas", "metacubexd", "/app/../etc/passwd"
        ) is None

    def test_machine_local_http_version_uses_remote_loopback(self, monkeypatch):
        seen = {}
        monkeypatch.setitem(vs.sm.MACHINE_SSH, "nas", "root@100.64.0.10")

        def fake_run(cmd, timeout=15):
            seen["cmd"] = cmd
            return 0, '{"ret":0,"version":"2.27.2"}', ""

        monkeypatch.setattr(vs.sm, "_run", fake_run)
        assert vs._probe_machine_http_version(
            "nas", 16601, "/version", "version"
        ) == "v2.27.2"
        assert seen["cmd"][-2] == "root@100.64.0.10"
        assert "http://127.0.0.1:16601/version" in seen["cmd"][-1]

    def test_machine_local_http_version_falls_back_to_container_gateway(
        self, monkeypatch
    ):
        seen = []
        monkeypatch.setitem(vs.sm.MACHINE_SSH, "nas", None)
        monkeypatch.setattr(
            vs, "_default_gateway_address", lambda: "172.27.0.1"
        )

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                return b'{"ret":0,"version":"2.27.2"}'

        def fake_urlopen(request, timeout=8):
            seen.append(request.full_url)
            if "127.0.0.1" in request.full_url:
                raise vs.urllib.error.URLError("connection refused")
            return FakeResponse()

        monkeypatch.setattr(vs.urllib.request, "urlopen", fake_urlopen)
        assert vs._probe_machine_http_version(
            "nas", 16601, "/version", "version"
        ) == "v2.27.2"
        assert seen == [
            "http://127.0.0.1:16601/version",
            "http://172.27.0.1:16601/version",
        ]

    def test_machine_local_http_version_rejects_unsafe_path(self):
        assert vs._probe_machine_http_version(
            "nas", 16601, "/version;rm", "version"
        ) is None


class TestExecProbeGate:
    def test_magpie_builtin_probe_reads_version(self, monkeypatch):
        """magpie 镜像 :latest 无版本 label，内置 exec 探针读 `magpie --version`。

        回归：本机版本曾显示「未知」，compare 链接拼成 …/compare/未知...v0.1.1268。
        """
        monkeypatch.delenv("CONSOLE_ALLOW_DOCKER_EXEC_PROBES", raising=False)
        monkeypatch.setattr(vs.sm, "_docker_cmd", lambda m, args: ["docker", *args])
        monkeypatch.setattr(
            vs.sm, "_run", lambda cmd, timeout=15: (0, "magpie v0.1.1257\n", "")
        )
        monkeypatch.setattr(
            vs, "_inspect_container",
            lambda machine, container: {"Config": {"Image": "ghcr.io/yetone/magpie:latest", "Labels": {}}},
        )
        svc = {"name": "magpie", "machine": "us", "container": "magpie", "type": "docker", "version": {}}
        assert vs.get_local_service_version(svc) == "v0.1.1257"

    def test_nas_skips_exec_without_opening_proxy(self, monkeypatch):
        called = {"run": False}

        def fake_run(cmd, timeout=15):
            called["run"] = True
            return (0, "should-not-run", "")

        monkeypatch.delenv("CONSOLE_ALLOW_DOCKER_EXEC_PROBES", raising=False)
        monkeypatch.setattr(vs.sm, "_docker_cmd", lambda m, args: ["docker", *args])
        monkeypatch.setattr(vs.sm, "_run", fake_run)
        assert vs._probe_exec_version("nas", "mihomo", ["/mihomo", "-v"], r"([\d.]+)") is None
        assert called["run"] is False

    def test_composition_local_host_skips_exec_without_opening_proxy(self, monkeypatch):
        monkeypatch.delenv("CONSOLE_ALLOW_DOCKER_EXEC_PROBES", raising=False)
        monkeypatch.setattr(vs.sm.mc, "local_machine", lambda: "vps")
        monkeypatch.setattr(
            vs.sm, "_docker_cmd", lambda *args: pytest.fail("组合本机不得自动开启 exec"),
        )

        assert vs._probe_exec_version("vps", "app", ["app", "--version"], None) is None
        assert vs._exec_probes_allowed("") is False

    def test_local_version_prefers_label_then_tag_not_latest(self, monkeypatch):
        monkeypatch.delenv("CONSOLE_ALLOW_DOCKER_EXEC_PROBES", raising=False)

        def fake_inspect(machine, container):
            return {
                "Config": {
                    "Image": "example/app:latest",
                    "Labels": {"org.opencontainers.image.version": "1.2.3"},
                }
            }

        monkeypatch.setattr(vs, "_inspect_container", fake_inspect)
        assert vs.get_local_docker_version({"name": "app", "machine": "nas"}) == "1.2.3"

    def test_local_version_generic_tag_without_probe_is_unknown(self, monkeypatch):
        monkeypatch.delenv("CONSOLE_ALLOW_DOCKER_EXEC_PROBES", raising=False)
        monkeypatch.setattr(
            vs,
            "_inspect_container",
            lambda m, c: {"Config": {"Image": "example/app:latest", "Labels": {}}},
        )
        calls = {"exec": 0}

        def boom(*args, **kwargs):
            calls["exec"] += 1
            raise AssertionError("NAS 不得调用 docker exec 探针")

        monkeypatch.setattr(vs, "_probe_exec_version", boom)
        monkeypatch.setattr(vs, "_probe_file_version", boom)
        assert vs.get_local_docker_version({"name": "app", "machine": "nas", "type": "docker"}) is None
        assert calls["exec"] == 0

    def test_local_version_non_generic_tag(self, monkeypatch):
        monkeypatch.delenv("CONSOLE_ALLOW_DOCKER_EXEC_PROBES", raising=False)
        monkeypatch.setattr(
            vs,
            "_inspect_container",
            lambda m, c: {"Config": {"Image": "example/app:v9.8.7", "Labels": {}}},
        )
        assert vs.get_local_docker_version({"name": "app", "machine": "nas"}) == "v9.8.7"

    def test_nas_file_probe_uses_read_only_copy_even_when_exec_disabled(self, monkeypatch):
        monkeypatch.delenv("CONSOLE_ALLOW_DOCKER_EXEC_PROBES", raising=False)
        monkeypatch.setattr(
            vs,
            "_inspect_container",
            lambda m, c: {
                "Config": {
                    "Image": "ghcr.io/metacubex/metacubexd:latest",
                    "Labels": {},
                }
            },
        )
        monkeypatch.setattr(
            vs,
            "_copy_container_file",
            lambda machine, container, path: b'{"version": "1.269.0"}',
        )
        assert vs.get_local_docker_version({
            "name": "metacubexd",
            "container": "metacubexd",
            "machine": "nas",
            "type": "docker",
        }) == "v1.269.0"


class TestLocalServiceVersion:
    @pytest.mark.parametrize(("machine_fields", "expected"), [
        ({}, "vps"),
        ({"machine": None}, "vps"),
        ({"machine": ""}, "vps"),
        ({"machine": "txy"}, "txy"),
    ])
    def test_docker_probe_uses_composition_default_and_preserves_explicit_machine(
        self, monkeypatch, machine_fields, expected,
    ):
        seen = []
        monkeypatch.setattr(vs.sm.mc, "local_machine", lambda: "vps")
        monkeypatch.setattr(
            vs, "_inspect_container",
            lambda machine, container: seen.append((machine, container)) or {
                "Config": {"Labels": {"org.opencontainers.image.version": "1.2.3"}},
            },
        )

        assert vs.get_local_docker_version({"name": "app", **machine_fields}) == "1.2.3"
        assert seen == [(expected, "app")]

    @pytest.mark.parametrize(("machine_fields", "expected"), [
        ({}, "vps"),
        ({"machine": None}, "vps"),
        ({"machine": ""}, "vps"),
        ({"machine": "txy"}, "txy"),
    ])
    def test_http_probe_uses_composition_default_and_preserves_explicit_machine(
        self, monkeypatch, machine_fields, expected,
    ):
        seen = []
        monkeypatch.setattr(vs.sm.mc, "local_machine", lambda: "vps")
        monkeypatch.setattr(
            vs, "_probe_machine_http_version",
            lambda machine, port, path, json_key: seen.append(
                (machine, port, path, json_key)
            ) or "v1.2.3",
        )
        svc = {
            "name": "app", "type": "process", "port": 8080,
            "version": {"kind": "http", "probe": {"http_path": "/version"}},
            **machine_fields,
        }

        assert vs.get_local_service_version(svc) == "v1.2.3"
        assert seen == [(expected, 8080, "/version", "version")]

    @pytest.mark.parametrize("kind", ["image", "http"])
    def test_missing_machine_without_composition_identity_does_not_probe(
        self, monkeypatch, kind,
    ):
        monkeypatch.setattr(vs.sm.mc, "local_machine", lambda: "")
        for name in ("_inspect_container", "_probe_machine_http_version"):
            monkeypatch.setattr(
                vs, name, lambda *args: pytest.fail("未配置目标时不得探测版本"),
            )

        assert vs.get_local_service_version({
            "name": "app", "port": 8080,
            "version": {"kind": kind, "probe": {"http_path": "/version"}},
        }) is None

    def test_process_service_can_use_machine_local_http_probe(self, monkeypatch):
        monkeypatch.setattr(
            vs,
            "_probe_machine_http_version",
            lambda machine, port, path, json_key: "v2.27.2",
        )
        svc = {
            "name": "lucky",
            "machine": "nas",
            "type": "process",
            "port": 16601,
            "version": {
                "enabled": True,
                "kind": "http",
                "probe": {"http_path": "/version", "json_key": "version"},
            },
        }
        assert vs.get_local_service_version(svc) == "v2.27.2"

    def test_sync_local_only_service_is_not_marked_outdated(self, tmp_path, monkeypatch):
        # PG-02：同步受采集门禁约束（只有 authority 写 versions 表），
        # 且写库路径由 storage_dispatch 决定，测试显式传 db_path 隔离到临时库。
        monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authority")
        db_path = str(tmp_path / "versions.db")
        # versions 表在 SQLite 侧由 app.init_db() 建；PG-02 起业务模块不再自己
        # CREATE TABLE（运行账号没有 DDL 权限），所以测试显式建表。
        conn = db.get_connection(db_path)
        try:
            conn.execute(
                "CREATE TABLE versions (svc_name TEXT PRIMARY KEY, local_version TEXT,"
                " upstream_version TEXT, behind_count INTEGER, update_status TEXT,"
                " recent_changes TEXT, last_checked TEXT)"
            )
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setattr(vs, "get_local_service_version", lambda svc: "8.4.8")
        monkeypatch.setattr(vs, "get_upstream_release", lambda repo: (None, None, []))
        row = vs.sync_service_version({
            "name": "mysql",
            "type": "docker",
            "version": {"enabled": True, "kind": "image"},
        }, db_path=db_path)
        assert row["local"] == "8.4.8"
        assert row["upstream"] == "未知"
        assert row["status"] == "仅本地"
        assert row["behind"] == 0


# ---------------------------------------------------------------------------
# DB 建库场景 — 2026-07 CI 事故回归：data/ 目录缺失时 sqlite 必须可建库
# ---------------------------------------------------------------------------

class TestDbBootstrap:
    def test_paths_source_creates_missing_data_dir(self, tmp_path):
        """真实执行 paths.py 源码：把它复制到 data/ 缺失的临时 checkout，
        子进程 import 后必须建目录且可建库。
        删掉 paths.py 的 makedirs 会让本测试失败——防同义反复（2026-07 CI 事故回归）。"""
        import shutil
        import subprocess

        workdir = tmp_path / "checkout"
        workdir.mkdir()
        shutil.copy(BACKEND_DIR / "paths.py", workdir / "paths.py")
        # paths 的数据根由组合叶子模块决定（B7-1），两者一起复制才是一个完整 checkout。
        shutil.copy(BACKEND_DIR / "composition.py", workdir / "composition.py")
        assert not (workdir / "data").exists()

        code = (
            "import paths, sqlite3, os\n"
            "assert os.path.isdir(os.path.dirname(paths.DB_PATH)), 'data dir not created'\n"
            "conn = sqlite3.connect(paths.DB_PATH)\n"
            "conn.execute('CREATE TABLE IF NOT EXISTS versions (svc_name TEXT)')\n"
            "conn.close()\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=workdir, capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert (workdir / "data" / "console.db").exists()

    def test_paths_module_creates_data_dir(self):
        """import paths 后 data/ 目录必须存在（模块导入即建目录）。"""
        import paths

        assert pathlib.Path(paths.DB_PATH).parent.is_dir()

    def test_db_path_single_owner(self):
        """三个消费模块的 DB_PATH 必须来自同一 owner，防止再度漂移。"""
        import app as app_module
        import paths
        import version_sync

        assert app_module.DB_PATH == paths.DB_PATH
        assert vs.DB_PATH == paths.DB_PATH
        assert version_sync.DB_PATH == paths.DB_PATH
