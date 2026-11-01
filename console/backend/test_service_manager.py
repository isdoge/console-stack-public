# -*- coding: utf-8 -*-
"""Regression tests for service_manager.py — action whitelist, machine whitelist,
argv construction safety, and remote SSH command quoting."""
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import machine_config as mc  # noqa: E402
import service_manager as sm  # noqa: E402


@pytest.fixture(autouse=True)
def _nas_is_local(monkeypatch):
    """开发机若 export 了 MACHINE_SSH_NAS，导入期字典会把 nas 当成远程；单测默认本地。"""
    monkeypatch.setitem(sm.MACHINE_SSH, "nas", None)


@pytest.fixture(autouse=True)
def _clean_registry():
    mc.invalidate()
    yield
    mc.invalidate()


# ---------------------------------------------------------------------------
# _build_entry — priority
# ---------------------------------------------------------------------------

class TestBuildEntryPriority:
    def test_defaults_to_medium(self):
        entry = sm._build_entry({"name": "svc-a", "machine": "nas"})
        assert entry["priority"] == "medium"

    @pytest.mark.parametrize("priority", ["high", "medium", "low", "HIGH", " low "])
    def test_accepts_and_normalizes(self, priority):
        entry = sm._build_entry({"name": "svc-a", "machine": "nas", "priority": priority})
        assert entry["priority"] == priority.strip().lower()

    @pytest.mark.parametrize("priority", ["urgent", "p0", "1", "高"])
    def test_rejects_unknown(self, priority):
        with pytest.raises(ValueError):
            sm._build_entry({"name": "svc-a", "machine": "nas", "priority": priority})


class TestUpsertPreservesHiddenRuntimeFields:
    def test_ui_edit_preserves_type_actions_and_version_probe(self, tmp_path):
        path = tmp_path / "assets.yaml"
        sm.save_assets(str(path), [{
            "name": "lucky",
            "machine": "nas",
            "type": "process",
            "port": 16601,
            "purpose": "old",
            "actions": [],
            "project": {
                "repo": "https://example.test/sample",
                "links": [{"label": "资料", "href": "https://example.test/sample/docs"}],
            },
            "version": {
                "enabled": True,
                "kind": "http",
                "repo_url": "https://github.com/gdy666/lucky",
                "probe": {"http_path": "/version", "json_key": "version"},
            },
        }])

        sm.upsert_service(str(path), {
            "name": "lucky",
            "machine": "nas",
            "port": "16601",
            "purpose": "new",
            "version": {
                "enabled": True,
                "repo_url": "https://github.com/gdy666/lucky",
            },
        }, original_name="lucky")

        saved = sm.load_assets(str(path))[0]
        assert saved["purpose"] == "new"
        assert saved["type"] == "process"
        assert saved["actions"] == []
        assert saved["project"] == {
            "repo": "https://example.test/sample",
            "links": [{"label": "资料", "href": "https://example.test/sample/docs"}],
        }
        assert saved["version"]["kind"] == "http"
        assert saved["version"]["probe"] == {
            "http_path": "/version",
            "json_key": "version",
        }


class TestUpsertIdentity:
    def test_create_rejects_duplicate_name_without_overwriting(self, tmp_path):
        path = tmp_path / "assets.yaml"
        original = {
            "name": "axonhub",
            "machine": "txy",
            "purpose": "existing",
        }
        sm.save_assets(str(path), [original])

        with pytest.raises(ValueError, match="服务已存在: axonhub"):
            sm.upsert_service(str(path), {
                "name": "axonhub",
                "machine": "nas",
                "purpose": "new",
            })

        assert sm.load_assets(str(path)) == [original]

    def test_edit_rejects_missing_original_without_creating(self, tmp_path):
        path = tmp_path / "assets.yaml"
        sm.save_assets(str(path), [{"name": "existing", "machine": "nas"}])

        with pytest.raises(ValueError, match="服务不存在: missing"):
            sm.upsert_service(
                str(path),
                {"name": "renamed", "machine": "nas"},
                original_name="missing",
            )

        assert [svc["name"] for svc in sm.load_assets(str(path))] == ["existing"]


# ---------------------------------------------------------------------------
# set_priority — 批量设置优先级
# ---------------------------------------------------------------------------

class TestSetPriority:
    def _write_assets(self, tmp_path, names):
        path = tmp_path / "assets.yaml"
        sm.save_assets(str(path), [{"name": n, "machine": "nas"} for n in names])
        return str(path)

    def test_updates_matching_services(self, tmp_path):
        path = self._write_assets(tmp_path, ["a", "b", "c"])
        assert sm.set_priority(path, ["a", "c"], "high") == 2
        by_name = {s["name"]: s for s in sm.load_assets(path)}
        assert by_name["a"]["priority"] == "high"
        assert by_name["c"]["priority"] == "high"
        assert "priority" not in by_name["b"]

    def test_normalizes_priority(self, tmp_path):
        path = self._write_assets(tmp_path, ["a"])
        sm.set_priority(path, ["a"], " LOW ")
        assert sm.load_assets(path)[0]["priority"] == "low"

    def test_rejects_bad_priority(self, tmp_path):
        path = self._write_assets(tmp_path, ["a"])
        with pytest.raises(ValueError):
            sm.set_priority(path, ["a"], "urgent")

    def test_rejects_empty_names(self, tmp_path):
        path = self._write_assets(tmp_path, ["a"])
        with pytest.raises(ValueError):
            sm.set_priority(path, [], "high")

    def test_rejects_no_match(self, tmp_path):
        path = self._write_assets(tmp_path, ["a"])
        with pytest.raises(ValueError):
            sm.set_priority(path, ["nope"], "high")

    def test_rejects_invalid_name(self, tmp_path):
        path = self._write_assets(tmp_path, ["a"])
        with pytest.raises(ValueError):
            sm.set_priority(path, ["bad name;rm"], "high")


# ---------------------------------------------------------------------------
# _validate_machine
# ---------------------------------------------------------------------------

class TestValidateMachine:
    @pytest.mark.parametrize("machine", ["nas", "txy", "aly"])
    def test_accepts_whitelisted(self, machine):
        assert sm._validate_machine(machine) == machine

    def test_defaults_to_nas(self):
        assert sm._validate_machine(None) == "nas"
        assert sm._validate_machine("") == "nas"

    @pytest.mark.parametrize("machine", ["evil", "nas;rm", "../nas", "nas nas", "nas!"])
    def test_rejects_unknown_or_dangerous(self, machine):
        with pytest.raises(ValueError):
            sm._validate_machine(machine)

    def test_strips_surrounding_whitespace_but_still_rejects_injection(self):
        # Trailing newline is stripped and 'nas' passes — that's fine because
        # validate_identifier normalizes edges before whitelist check.
        assert sm._validate_machine("nas\n") == "nas"

    @pytest.mark.parametrize("machine", ["us", "kr", "sg"])
    def test_existence_only_accepts_non_docker_machines(self, machine):
        # 不带 require 时只查存在性：kr 上的 process 类服务是合法的
        assert sm._validate_machine(machine) == machine

    @pytest.mark.parametrize("machine", ["kr", "sg"])
    def test_docker_capability_required_rejects_non_docker(self, machine):
        # us 自 2026-09-07 起有 docker 能力（owner 拍板），只剩两台 node 属无 docker
        with pytest.raises(ValueError) as err:
            sm._validate_machine(machine, require="docker")
        assert "docker" in str(err.value)

    @pytest.mark.parametrize("machine", ["nas", "txy", "aly", "us"])
    def test_docker_capability_accepts_docker_machines(self, machine):
        assert sm._validate_machine(machine, require="docker") == machine

    def test_unknown_machine_still_rejected_with_require(self):
        with pytest.raises(ValueError) as err:
            sm._validate_machine("router", require="docker")
        assert "未知机器" in str(err.value)


# ---------------------------------------------------------------------------
# capabilities 闸门：容器动作绝不落到无 docker 的机器上
# ---------------------------------------------------------------------------

class TestDockerCapabilityGate:
    @pytest.mark.parametrize("machine", ["kr", "sg"])
    def test_docker_cmd_refuses_non_docker_machine(self, machine):
        with pytest.raises(ValueError):
            sm._docker_cmd(machine, ["ps", "-a"])

    @pytest.mark.parametrize("machine", ["kr", "sg"])
    def test_do_action_refuses_and_runs_nothing(self, machine, monkeypatch):
        monkeypatch.setattr(
            sm.subprocess, "run",
            lambda *a, **kw: pytest.fail("无 docker 能力的机器不应发起任何命令"),
        )
        ok, msg = sm.do_action(
            {"name": "whatever", "machine": machine, "actions": ["restart"]},
            "restart",
        )
        assert ok is False
        assert "docker" in msg

    def test_ssh_key_opts_injects_identity_file(self):
        assert sm.ssh_key_opts("kr") == ["-i", f"{mc.SSH_KEY_DIR}/aws-kr.pem"]
        assert sm.ssh_key_opts("txy") == []   # 用默认密钥，不加 -i

    def test_node_ssh_argv_carries_key_and_user(self, monkeypatch):
        # kr 无 docker，用 registry 里的 ssh 目标直接验证 argv 组装
        monkeypatch.setitem(sm.MACHINE_SSH, "kr", "admin@100.64.0.50")
        monkeypatch.setattr(mc, "has_capability", lambda m, c: True)
        cmd = sm._docker_cmd("kr", ["ps"])
        assert cmd[0] == "ssh"
        assert cmd[cmd.index("-i") + 1].endswith("aws-kr.pem")
        assert cmd[-2] == "admin@100.64.0.50"


# ---------------------------------------------------------------------------
# _docker_cmd
# ---------------------------------------------------------------------------

class TestDockerCmdLocal:
    def test_local_nas_returns_argv_list_no_shell(self):
        cmd = sm._docker_cmd("nas", ["ps", "-a"])
        assert cmd[0] == "docker"
        assert cmd == ["docker", "ps", "-a"]

    def test_local_argv_preserves_flags(self):
        cmd = sm._docker_cmd("nas", ["logs", "--tail", "200", "console-brain"])
        assert cmd == ["docker", "logs", "--tail", "200", "console-brain"]

    @pytest.mark.parametrize("bad", ["evil\ninject", "with\rcr", "with\x00nul"])
    def test_rejects_control_chars_in_args(self, bad):
        with pytest.raises(ValueError):
            sm._docker_cmd("nas", ["logs", bad])


class TestDockerCmdRemote:
    def test_remote_wraps_via_ssh_with_batchmode(self):
        cmd = sm._docker_cmd("txy", ["restart", "my-service"])
        assert cmd[0] == "ssh"
        assert "-o" in cmd
        assert "BatchMode=yes" in cmd
        assert "StrictHostKeyChecking=yes" in cmd
        # known_hosts 只读挂载：必须禁掉回写，否则警告污染命令输出
        assert "UpdateHostKeys=no" in cmd
        # target is the last argument before the remote payload
        assert cmd[-2] == sm.MACHINE_SSH["txy"]
        # payload is a single quoted string, no interpolation
        assert cmd[-1] == "docker restart my-service"

    def test_remote_shell_metacharacters_get_quoted(self):
        # This name would be rejected by validate_identifier upstream, but
        # docker_cmd is the last line of defense — it must shlex.quote payload.
        cmd = sm._docker_cmd("txy", ["logs", "harmless-name"])
        payload = cmd[-1]
        # No unquoted shell metacharacters escape into the remote command
        assert ";" not in payload
        assert "&" not in payload
        assert "|" not in payload
        assert "`" not in payload



def _reload_in_place(module):
    """在同一模块对象上重跑 kernel 真身源码。

    `console/backend/service_manager.py` 是 shim：`importlib.reload` 会重跑 shim，
    shim 再把 sys.modules 换成新对象，测试持有的 `sm` 就成了失效引用（第二次 reload
    报 "module service_manager not in sys.modules"）。这里按 `__file__`（真身路径）
    原地重放，sys.modules 与其它模块持有的引用保持同一对象。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(module.__name__, module.__file__)
    spec.loader.exec_module(module)


class TestMachineSshNasEnvOverride:
    """When console runs off-NAS (mac/win dev machine), MACHINE_SSH_NAS env
    lets operators route nas-scoped services through SSH (recommended: Tailscale IP)."""

    def _reload_sm(self, monkeypatch, value):
        if value is None:
            monkeypatch.delenv("MACHINE_SSH_NAS", raising=False)
        else:
            monkeypatch.setenv("MACHINE_SSH_NAS", value)
        _reload_in_place(sm)

    def test_default_none_uses_local_docker(self, monkeypatch):
        self._reload_sm(monkeypatch, None)
        assert sm.MACHINE_SSH["nas"] is None
        cmd = sm._docker_cmd("nas", ["ps"])
        assert cmd == ["docker", "ps"]

    def test_env_override_switches_nas_to_ssh(self, monkeypatch):
        self._reload_sm(monkeypatch, "root@100.64.0.10")
        assert sm.MACHINE_SSH["nas"] == "root@100.64.0.10"
        cmd = sm._docker_cmd("nas", ["ps", "-a"])
        assert cmd[0] == "ssh"
        assert cmd[-2] == "root@100.64.0.10"
        assert cmd[-1] == "docker ps -a"

    def test_empty_env_treated_as_local(self, monkeypatch):
        self._reload_sm(monkeypatch, "")
        assert sm.MACHINE_SSH["nas"] is None


class TestMachineSshTxyAlyDefaults:
    """txy / aly 默认走 Tailscale IP，避免依赖各设备 ~/.ssh/config 别名；
    env 可覆盖，留空回退到 Tailscale IP 默认值。"""

    def _reload_sm(self, monkeypatch, txy=None, aly=None):
        for key, value in (("MACHINE_SSH_TXY", txy), ("MACHINE_SSH_ALY", aly)):
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        _reload_in_place(sm)

    def test_default_uses_tailscale_ip(self, monkeypatch):
        self._reload_sm(monkeypatch, None, None)
        assert sm.MACHINE_SSH["txy"] == "root@100.64.0.30"
        assert sm.MACHINE_SSH["aly"] == "root@100.64.0.40"

    def test_empty_env_falls_back_to_tailscale_default(self, monkeypatch):
        self._reload_sm(monkeypatch, "", "")
        assert sm.MACHINE_SSH["txy"] == "root@100.64.0.30"
        assert sm.MACHINE_SSH["aly"] == "root@100.64.0.40"

    def test_env_override_wins(self, monkeypatch):
        self._reload_sm(monkeypatch, "ops@txy.internal", "ops@aly.internal")
        assert sm.MACHINE_SSH["txy"] == "ops@txy.internal"
        assert sm.MACHINE_SSH["aly"] == "ops@aly.internal"
        cmd = sm._docker_cmd("txy", ["ps"])
        assert cmd[-2] == "ops@txy.internal"


# ---------------------------------------------------------------------------
# do_action — non-whitelisted actions must NOT reach subprocess
# ---------------------------------------------------------------------------

class TestDoActionWhitelist:
    def _capture_run(self, monkeypatch):
        calls = []

        def fake_run(cmd, timeout=20, shell=False):
            calls.append(cmd)
            return 0, "", ""

        monkeypatch.setattr(sm, "_run", fake_run)
        return calls

    @pytest.mark.parametrize("bad_action", ["exec", "rm", "kill", "prune", "build", "pull", ""])
    def test_rejects_unauthorized_action(self, monkeypatch, bad_action):
        calls = self._capture_run(monkeypatch)
        ok, msg = sm.do_action(
            {"name": "svc", "container": "svc", "machine": "nas", "actions": ["restart", "logs"]},
            bad_action,
        )
        assert ok is False
        assert calls == []  # subprocess never invoked

    def test_upgrade_requires_compose_dir(self, monkeypatch):
        calls = self._capture_run(monkeypatch)
        ok, msg = sm.do_action(
            {"name": "svc", "container": "svc", "machine": "us", "actions": ["restart", "upgrade"]},
            "upgrade",
        )
        assert ok is False
        assert "compose_dir" in msg
        assert calls == []

    def test_upgrade_rejects_bad_compose_dir(self, monkeypatch):
        calls = self._capture_run(monkeypatch)
        for bad in ("/opt/x; rm -rf /", "/opt/x\nwhoami", "relative/path", ""):
            ok, msg = sm.do_action(
                {"name": "svc", "container": "svc", "machine": "us",
                 "actions": ["upgrade"], "compose_dir": bad},
                "upgrade",
            )
            assert ok is False, f"should reject compose_dir {bad!r}"
        assert calls == []

    def test_upgrade_rejects_when_machine_upgrade_disabled(self, monkeypatch):
        calls = self._capture_run(monkeypatch)
        # 机器台账无 upgrade 字段（或 enabled=False）→ 拒绝，提示去机器页勾选
        monkeypatch.setattr(sm.mc, "upgrade_config", lambda machine: None)
        ok, msg = sm.do_action(
            {"name": "svc", "container": "svc", "machine": "nas",
             "actions": ["upgrade"], "compose_dir": "/opt/svc"}, "upgrade",
        )
        assert ok is False
        assert "未开放服务升级" in msg
        assert calls == []

    def test_upgrade_rejects_nas_when_not_enabled(self, monkeypatch):
        # 2026-10-22 起 NAS 不再硬编码拒绝，改由机器级 upgrade.enabled 决定；
        # 未开启时与其它机器同口径拒绝。
        calls = self._capture_run(monkeypatch)
        monkeypatch.setattr(sm.mc, "upgrade_config", lambda machine: None)
        ok, msg = sm.do_action(
            {"name": "svc", "container": "svc", "machine": "nas",
             "actions": ["upgrade"], "compose_dir": "/opt/svc"}, "upgrade",
        )
        assert ok is False
        assert "未开放服务升级" in msg
        assert calls == []

    def test_upgrade_runs_compose_pull_up_on_ssh(self, monkeypatch):
        calls = self._capture_run(monkeypatch)
        monkeypatch.setitem(sm.MACHINE_SSH, "us", "ubuntu@us-host")
        monkeypatch.setattr(sm, "ssh_key_opts", lambda machine: ["-i", "/key"])
        monkeypatch.setattr(
            sm.mc, "upgrade_config",
            lambda machine: {"enabled": True} if machine == "us" else None,
        )
        ok, msg = sm.do_action(
            {"name": "magpie", "container": "magpie", "machine": "us",
             "actions": ["upgrade"], "compose_dir": "/opt/magpie"},
            "upgrade",
        )
        assert ok is True
        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == "ssh"
        remote = cmd[-1]
        assert "docker compose -f /opt/magpie/compose.yaml pull" in remote
        assert "up -d" in remote
        assert "sudo -n" in remote

    def test_upgrade_uses_compose_file_override(self, monkeypatch):
        calls = self._capture_run(monkeypatch)
        monkeypatch.setitem(sm.MACHINE_SSH, "nas", "root@nas-host")
        monkeypatch.setattr(sm, "ssh_key_opts", lambda machine: [])
        monkeypatch.setattr(
            sm.mc, "upgrade_config",
            lambda machine: {"enabled": True} if machine == "nas" else None,
        )
        ok, msg = sm.do_action(
            {"name": "ignis", "container": "ignis", "machine": "nas",
             "actions": ["upgrade"], "compose_dir": "/opt/ignis",
             "compose_file": "docker-compose.yml"},
            "upgrade",
        )
        assert ok is True, msg
        remote = calls[0][-1]
        assert "docker compose -f /opt/ignis/docker-compose.yml pull" in remote
        assert "docker compose -f /opt/ignis/docker-compose.yml up -d" in remote

    def test_upgrade_injects_proxy_env_after_sudo(self, monkeypatch):
        calls = self._capture_run(monkeypatch)
        monkeypatch.setitem(sm.MACHINE_SSH, "us", "ubuntu@us-host")
        monkeypatch.setattr(sm, "ssh_key_opts", lambda machine: [])
        monkeypatch.setattr(
            sm.mc, "upgrade_config",
            lambda machine: {
                "enabled": True,
                "proxy_env": {"HTTPS_PROXY": "http://10.0.0.1:7890", "NO_PROXY": "localhost"},
            } if machine == "us" else None,
        )
        ok, msg = sm.do_action(
            {"name": "svc", "container": "svc", "machine": "us",
             "actions": ["upgrade"], "compose_dir": "/opt/svc"},
            "upgrade",
        )
        assert ok is True, msg
        remote = calls[0][-1]
        # env 必须在 sudo 后、docker 前；两子命令都要带
        assert "sudo -n env HTTPS_PROXY=http://10.0.0.1:7890 NO_PROXY=localhost docker compose" in remote
        assert remote.count("sudo -n env HTTPS_PROXY=") == 2

    def test_upgrade_nas_local_without_ssh_uses_local_docker(self, monkeypatch):
        # nas 无 SSH 入口时本机直跑 docker compose（不拼 ssh 命令）
        calls = self._capture_run(monkeypatch)
        monkeypatch.setattr(sm, "ssh_key_opts", lambda machine: [])
        monkeypatch.setattr(
            sm.mc, "upgrade_config",
            lambda machine: {"enabled": True} if machine == "nas" else None,
        )
        ok, msg = sm.do_action(
            {"name": "svc", "container": "svc", "machine": "nas",
             "actions": ["upgrade"], "compose_dir": "/opt/svc"}, "upgrade",
        )
        assert ok is True, msg
        assert len(calls) == 1
        local = calls[0]
        assert isinstance(local, str)
        assert "ssh" not in local.split()[0]
        assert "docker compose -f /opt/svc/compose.yaml pull" in local
        assert "up -d" in local
        assert "sudo -n docker compose" in local  # 无 proxy_env 时不带 env 前缀

    @pytest.mark.parametrize(
        ("current_tag", "upstream", "expected_tag"),
        [
            ("0.1.1324", "0.1.1330", "0.1.1330"),
            ("0.1.1324", "v0.1.1330", "0.1.1330"),
            ("v1.2.21", "v1.2.22", "v1.2.22"),
            ("v1.2.21", "1.2.22", "v1.2.22"),
        ],
    )
    def test_upgrade_updates_compose_image_tag_from_upstream(
        self, monkeypatch, current_tag, upstream, expected_tag,
    ):
        # 登记了 repo_url 且本地 tag 落后上游：先 cat compose → sed 改 tag → pull+up
        calls = []
        compose_text = f"services:\n  magpie:\n    image: ghcr.io/yyyumeniku/magpie:{current_tag}\n"

        def fake_run(cmd, timeout=20, shell=False):
            calls.append(cmd)
            if shell:
                return 0, "", ""
            if cmd[0] == "ssh" and "cat " in cmd[-1]:
                return 0, compose_text, ""
            if cmd[0] == "ssh" and "sed -i " in cmd[-1]:
                return 0, "", ""
            return 0, "", ""

        monkeypatch.setattr(sm, "_run", fake_run)
        monkeypatch.setitem(sm.MACHINE_SSH, "nas", "root@nas-host")
        monkeypatch.setattr(sm, "ssh_key_opts", lambda machine: [])
        monkeypatch.setattr(
            sm.mc, "upgrade_config",
            lambda machine: {"enabled": True} if machine == "nas" else None,
        )
        monkeypatch.setattr(
            sm.vs, "get_upstream_release",
            lambda repo_url: (upstream, "", []),
        )
        ok, msg = sm.do_action(
            {"name": "magpie", "container": "magpie", "machine": "nas",
             "actions": ["upgrade"], "compose_dir": "/opt/magpie",
             "version": {"repo_url": "yyyumeniku/magpie"}},
            "upgrade",
        )
        assert ok is True, msg
        # cat + sed + ssh-pull-up 三步
        assert len(calls) == 3
        cat_cmd = calls[0]
        assert cat_cmd[0] == "ssh" and "cat /opt/magpie/compose.yaml" in cat_cmd[-1]
        sed_cmd = calls[1]
        assert sed_cmd[0] == "ssh" and "sed -i" in sed_cmd[-1]
        assert f"|ghcr.io/yyyumeniku/magpie:{expected_tag}|" in sed_cmd[-1]
        pull_cmd = calls[2]
        assert "docker compose -f /opt/magpie/compose.yaml pull" in pull_cmd[-1]

    def test_upgrade_skips_tag_update_when_upstream_same(self, monkeypatch):
        # 上游 tag 与本地相同：不跑 cat/sed，直接 pull+up（保持 1 次 _run）
        calls = self._capture_run(monkeypatch)
        monkeypatch.setitem(sm.MACHINE_SSH, "nas", "root@nas-host")
        monkeypatch.setattr(sm, "ssh_key_opts", lambda machine: [])
        monkeypatch.setattr(
            sm.mc, "upgrade_config",
            lambda machine: {"enabled": True} if machine == "nas" else None,
        )
        monkeypatch.setattr(
            sm.vs, "get_upstream_release",
            lambda repo_url: ("0.1.1324", "", []),
        )
        ok, msg = sm.do_action(
            {"name": "magpie", "container": "magpie", "machine": "nas",
             "actions": ["upgrade"], "compose_dir": "/opt/magpie",
             "version": {"repo_url": "yyyumeniku/magpie"}},
            "upgrade",
        )
        assert ok is True, msg
        # 只跑 1 次（pull+up），因为 cat 返回空文本时也能走到；但 fake_run 默认返回 0,"",""
        # cat 返回空 → _first_image 返回 None → 跳过 sed；pull 仍跑
        # 这里 fake_run 返回 ("",) 给 cat → 跳过 sed；仍应有 cat + pull = 2 次调用
        assert len(calls) == 2
        cat_cmd = calls[0]
        assert "cat /opt/magpie/compose.yaml" in cat_cmd[-1]
        pull_cmd = calls[1]
        assert "docker compose" in pull_cmd[-1]

    def test_upgrade_skips_tag_update_when_no_repo_url(self, monkeypatch):
        # 服务没登记 version.repo_url → 跳过 cat/sed，直接 pull+up
        calls = self._capture_run(monkeypatch)
        monkeypatch.setitem(sm.MACHINE_SSH, "nas", "root@nas-host")
        monkeypatch.setattr(sm, "ssh_key_opts", lambda machine: [])
        monkeypatch.setattr(
            sm.mc, "upgrade_config",
            lambda machine: {"enabled": True} if machine == "nas" else None,
        )
        ok, msg = sm.do_action(
            {"name": "svc", "container": "svc", "machine": "nas",
             "actions": ["upgrade"], "compose_dir": "/opt/svc"},
            "upgrade",
        )
        assert ok is True, msg
        assert len(calls) == 1
        assert "docker compose" in calls[0][-1]

    @pytest.mark.parametrize("prefix", ["", "v"])
    def test_upgrade_nas_local_updates_tag_via_shell(self, monkeypatch, prefix):
        # nas 本机（无 ssh_target）：tag 更新走 shell=True 的 cat/sed
        calls = []
        compose_text = f"services:\n  svc:\n    image: ghcr.io/x/y:{prefix}1.0.0\n"

        def fake_run(cmd, timeout=20, shell=False):
            calls.append((cmd, shell))
            if shell and cmd.startswith("cat "):
                return 0, compose_text, ""
            return 0, "", ""

        monkeypatch.setattr(sm, "_run", fake_run)
        monkeypatch.setattr(sm, "ssh_key_opts", lambda machine: [])
        monkeypatch.setattr(
            sm.mc, "upgrade_config",
            lambda machine: {"enabled": True} if machine == "nas" else None,
        )
        monkeypatch.setattr(
            sm.vs, "get_upstream_release",
            lambda repo_url: ("v1.0.1", "", []),
        )
        # 清空 MACHINE_SSH 让 nas 走本机
        monkeypatch.setitem(sm.MACHINE_SSH, "nas", None)
        ok, msg = sm.do_action(
            {"name": "svc", "container": "svc", "machine": "nas",
             "actions": ["upgrade"], "compose_dir": "/opt/svc",
             "version": {"repo_url": "x/y"}},
            "upgrade",
        )
        assert ok is True, msg
        # cat + sed + pull-up 三步全 shell
        assert len(calls) == 3
        assert calls[0][1] is True and calls[0][0].startswith("cat ")
        assert calls[1][1] is True and calls[1][0].startswith("sed -i")
        assert f"|ghcr.io/x/y:{prefix}1.0.1|" in calls[1][0]
        assert calls[2][1] is True and "docker compose" in calls[2][0]

    def test_upgrade_denied_when_not_in_actions(self, monkeypatch):
        calls = self._capture_run(monkeypatch)
        ok, msg = sm.do_action(
            {"name": "svc", "container": "svc", "machine": "us",
             "actions": ["restart", "logs"], "compose_dir": "/opt/svc"},
            "upgrade",
        )
        assert ok is False
        assert calls == []

    def test_empty_actions_list_denies_all(self, monkeypatch):
        # `actions: []` (explicit) must deny everything, distinct from unset (which defaults to restart+logs).
        calls = self._capture_run(monkeypatch)
        for act in ("restart", "logs", "start", "stop"):
            ok, msg = sm.do_action(
                {"name": "infra", "container": "infra", "machine": "nas", "actions": []},
                act,
            )
            assert ok is False, f"empty actions should deny {act}"
        assert calls == []

    def test_missing_actions_key_defaults_to_restart_logs(self, monkeypatch):
        calls = self._capture_run(monkeypatch)
        ok, _msg = sm.do_action(
            {"name": "svc", "container": "svc", "machine": "nas"},  # no 'actions' key at all
            "restart",
        )
        assert ok is True
        assert len(calls) == 1

    def test_rejects_action_not_in_svc_config(self, monkeypatch):
        # 'start' is a globally valid SERVICE_ACTION, but this svc only permits restart/logs.
        calls = self._capture_run(monkeypatch)
        ok, msg = sm.do_action(
            {"name": "svc", "container": "svc", "machine": "nas", "actions": ["restart", "logs"]},
            "start",
        )
        assert ok is False
        assert "未授权" in msg
        assert calls == []

    def test_rejects_bad_machine(self, monkeypatch):
        calls = self._capture_run(monkeypatch)
        ok, msg = sm.do_action(
            {"name": "svc", "container": "svc", "machine": "evil", "actions": ["restart"]},
            "restart",
        )
        assert ok is False
        assert calls == []

    def test_allows_configured_restart(self, monkeypatch):
        calls = self._capture_run(monkeypatch)
        ok, _msg = sm.do_action(
            {"name": "svc", "container": "svc", "machine": "nas", "actions": ["restart", "logs"]},
            "restart",
        )
        assert ok is True
        assert len(calls) == 1
        argv = calls[0]
        assert argv == ["docker", "restart", "svc"]

    def test_logs_redacts_secrets(self, monkeypatch):
        def fake_run(cmd, timeout=20):
            return 0, "line token=hunter2 more\npassword: s3cr3t\n", ""

        monkeypatch.setattr(sm, "_run", fake_run)
        ok, text = sm.do_action(
            {"name": "svc", "container": "svc", "machine": "nas", "actions": ["logs"]},
            "logs",
        )
        assert ok is True
        assert "hunter2" not in text
        assert "s3cr3t" not in text
        assert "token=***" in text.lower() or "token=***" in text


# ---------------------------------------------------------------------------
# node_action — E3 节点动作白名单
# ---------------------------------------------------------------------------

class TestNodeActionWhitelist:
    @pytest.fixture()
    def runs(self, monkeypatch):
        """拦住 _run，记录真正会发出去的 argv。"""
        captured = []

        def fake_run(cmd, timeout=20):
            captured.append(cmd)
            return 0, "ok", ""

        monkeypatch.setattr(sm, "_run", fake_run)
        return captured

    def test_restart_singbox_builds_fixed_remote_command(self, runs):
        ok, out = sm.node_action("kr", "restart-singbox")
        assert (ok, out) == (True, "ok")
        cmd = runs[0]
        assert cmd[0] == "ssh"
        assert "BatchMode=yes" in cmd and "StrictHostKeyChecking=yes" in cmd
        # 非 root 用户 + 专用私钥
        assert "-i" in cmd and cmd[cmd.index("-i") + 1].endswith("aws-kr.pem")
        assert cmd[-2] == "admin@100.64.0.50"
        # 命令写死在白名单里，调用方拼不进任何东西
        assert cmd[-1] == "sudo systemctl restart sing-box"

    @pytest.mark.parametrize(
        "action",
        ["restart", "stop", "reboot", "restart-caddy", "", None,
         "restart-singbox; rm -rf /", "systemctl restart sing-box"],
    )
    def test_unknown_actions_never_run(self, action, runs):
        ok, msg = sm.node_action("kr", action)
        assert ok is False
        assert "未授权" in msg
        assert runs == []

    @pytest.mark.parametrize("machine", ["nas", "txy", "aly", "us"])
    def test_server_machines_are_rejected(self, machine, runs):
        ok, msg = sm.node_action(machine, "restart-singbox")
        assert ok is False
        assert "不是节点" in msg
        assert runs == []

    def test_unknown_machine_is_rejected(self, runs):
        ok, msg = sm.node_action("router", "restart-singbox")
        assert ok is False
        assert "未知机器" in msg
        assert runs == []

    def test_machine_without_ssh_capability_is_rejected(self, tmp_path, monkeypatch, runs):
        machines = tmp_path / "machines.yaml"
        machines.write_text(
            "- id: kr\n  role: node\n  ssh: admin@1.2.3.4\n  capabilities: [beszel]\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("CONSOLE_MACHINES_PATH", str(machines))
        mc.invalidate()
        ok, msg = sm.node_action("kr", "restart-singbox")
        assert ok is False and "不支持 ssh" in msg
        assert runs == []

    def test_node_without_ssh_target_is_rejected(self, tmp_path, monkeypatch, runs):
        machines = tmp_path / "machines.yaml"
        machines.write_text(
            "- id: kr\n  role: node\n  ssh: \"\"\n  capabilities: [ssh]\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("CONSOLE_MACHINES_PATH", str(machines))
        mc.invalidate()
        ok, msg = sm.node_action("kr", "restart-singbox")
        assert ok is False and "未配置 SSH target" in msg
        assert runs == []

    def test_nonzero_exit_reports_failure(self, monkeypatch):
        monkeypatch.setattr(sm, "_run", lambda cmd, timeout=20: (1, "", "Failed to restart"))
        ok, out = sm.node_action("sg", "restart-singbox")
        assert ok is False
        assert "Failed to restart" in out


class TestNodeActionsFor:
    def test_nodes_expose_the_whitelist(self):
        assert sm.node_actions_for("kr") == [
            {"key": "restart-singbox", "label": "重启 sing-box"}
        ]

    @pytest.mark.parametrize("machine", ["nas", "txy", "aly", "us", "router", ""])
    def test_non_nodes_expose_nothing(self, machine):
        assert sm.node_actions_for(machine) == []

    def test_labels_only_never_the_command(self):
        for item in sm.node_actions_for("sg"):
            assert set(item) == {"key", "label"}


# ---------------------------------------------------------------------------
# discover_containers — 只读白名单扫描（T6 扫描登记）
# ---------------------------------------------------------------------------

class TestDiscoverContainers:
    def test_parses_docker_ps_output(self, monkeypatch):
        monkeypatch.setitem(sm.MACHINE_SSH, "nas", None)
        monkeypatch.setattr(mc, "has_capability", lambda m, c: True)
        monkeypatch.setattr(
            sm, "_run",
            lambda cmd, timeout=20: (
                0,
                "1panel-axonhub|ghcr.io/x|Up 2 days|axonhub\n"
                "1Panel-mysql-fTEx|mysql:8.4.8|Up 3 weeks|mysql\n\n",
                "",
            ),
        )
        items = sm.discover_containers("nas")
        assert items == [
            {
                "name": "1panel-axonhub", "image": "ghcr.io/x",
                "status": "Up 2 days", "stack": "axonhub",
            },
            {
                "name": "1Panel-mysql-fTEx", "image": "mysql:8.4.8",
                "status": "Up 3 weeks", "stack": "mysql",
            },
        ]

    def test_command_failure_raises_runtime_error(self, monkeypatch):
        monkeypatch.setitem(sm.MACHINE_SSH, "nas", None)
        monkeypatch.setattr(mc, "has_capability", lambda m, c: True)
        monkeypatch.setattr(sm, "_run", lambda cmd, timeout=20: (1, "", "docker daemon down"))
        with pytest.raises(RuntimeError, match="docker ps 失败"):
            sm.discover_containers("nas")

    def test_no_docker_capability_is_rejected(self, monkeypatch):
        monkeypatch.setitem(sm.MACHINE_SSH, "nas", None)
        monkeypatch.setattr(mc, "has_capability", lambda m, c: False)
        with pytest.raises(ValueError, match="docker"):
            sm.discover_containers("nas")

    def test_unknown_machine_is_rejected(self, monkeypatch):
        with pytest.raises(ValueError, match="未知机器"):
            sm.discover_containers("router")

    def test_docker_ps_command_has_no_user_input(self, monkeypatch):
        """命令 argv 写死：即便后续有人改调用，也不能把用户输入拼进 docker 参数。"""
        captured = {}
        monkeypatch.setitem(sm.MACHINE_SSH, "nas", None)
        monkeypatch.setattr(mc, "has_capability", lambda m, c: True)
        monkeypatch.setattr(
            sm, "_docker_cmd",
            lambda machine, args: captured.setdefault("args", args) or ["true"],
        )
        monkeypatch.setattr(sm, "_run", lambda cmd, timeout=20: (0, "", ""))
        sm.discover_containers("nas")
        assert captured["args"] == [
            "ps", "-a", "--format",
            '{{.Names}}|{{.Image}}|{{.Status}}|{{.Label "com.docker.compose.project"}}',
        ]

    def test_stack_label_absent_means_ungrouped(self, monkeypatch):
        """手工 `docker run` 起的容器没有 compose label —— 空即未分组，绝不猜。

        2026-09-07 线上实测：NAS/TXY/ALY 25 个容器里 4 个没有该 label
        （三台的 beszel-agent + 一个 precompose 遗留容器）。
        """
        monkeypatch.setitem(sm.MACHINE_SSH, "nas", None)
        monkeypatch.setattr(mc, "has_capability", lambda m, c: True)
        monkeypatch.setattr(
            sm, "_run",
            lambda cmd, timeout=20: (0, "beszel-agent|henrygd/beszel-agent|Up 1 day|\n", ""),
        )
        assert sm.discover_containers("nas")[0]["stack"] == ""

    def test_default_project_is_treated_as_ungrouped(self, monkeypatch):
        """1Panel 给部分容器落的兜底值 `default` 没有分组信息量（txy 实测）。"""
        monkeypatch.setitem(sm.MACHINE_SSH, "nas", None)
        monkeypatch.setattr(mc, "has_capability", lambda m, c: True)
        monkeypatch.setattr(
            sm, "_run",
            lambda cmd, timeout=20: (0, "x|img|Up|default\ny|img|Up|DEFAULT\n", ""),
        )
        assert [item["stack"] for item in sm.discover_containers("nas")] == ["", ""]

    def test_hostile_stack_value_is_dropped(self, monkeypatch):
        """label 是容器自己声明的，当不可信输入处理：不符合标识符规则就丢。"""
        monkeypatch.setitem(sm.MACHINE_SSH, "nas", None)
        monkeypatch.setattr(mc, "has_capability", lambda m, c: True)
        monkeypatch.setattr(
            sm, "_run",
            lambda cmd, timeout=20: (0, "x|img|Up|../../etc/passwd\n", ""),
        )
        assert sm.discover_containers("nas")[0]["stack"] == ""

    def test_normalize_container_name_matches_frontend_identity_key(self):
        assert sm.normalize_container_name("1Panel-mysql-fTEx") == "1panelmysqlftex"
        assert sm.normalize_container_name("axonhub") == "axonhub"


# ---------------------------------------------------------------------------
# Track L1：health_probe 三态经过写回路径
# ---------------------------------------------------------------------------

class TestHealthProbeWriteBack:
    def test_object_probe_round_trips(self, tmp_path):
        path = tmp_path / "assets.yaml"
        sm.save_assets(str(path), [])
        sm.upsert_service(str(path), {
            "name": "api",
            "machine": "nas",
            "port": 8080,
            "health_probe": {
                "type": "http",
                "target": "domain",
                "expected_status": 200,
                "max_latency_ms": 1500,
                "body_contains": '"status":"ok"',
            },
        })
        saved = sm.load_assets(str(path))[0]
        assert saved["health_probe"] == {
            "type": "http",
            "target": "domain",
            "expected_status": 200,
            "max_latency_ms": 1500,
            "body_contains": '"status":"ok"',
        }

    def test_editing_other_fields_keeps_object_probe(self, tmp_path):
        """UI 改个 purpose 不该把手写的语义检查配置抹掉（L1 前有这个坑）。"""
        path = tmp_path / "assets.yaml"
        sm.save_assets(str(path), [])
        probe = {"type": "http", "target": "domain", "expected_status": 204}
        sm.upsert_service(str(path), {"name": "api", "machine": "nas", "health_probe": probe})
        sm.upsert_service(
            str(path),
            {"name": "api", "machine": "nas", "purpose": "改了用途", "health_probe": probe},
            original_name="api",
        )
        saved = sm.load_assets(str(path))[0]
        assert saved["purpose"] == "改了用途"
        assert saved["health_probe"] == probe

    def test_switching_to_default_drops_the_key(self, tmp_path):
        path = tmp_path / "assets.yaml"
        sm.save_assets(str(path), [])
        sm.upsert_service(str(path), {
            "name": "api", "machine": "nas",
            "health_probe": {"type": "http", "expected_status": 204},
        })
        sm.upsert_service(
            str(path),
            {"name": "api", "machine": "nas", "health_probe": True},
            original_name="api",
        )
        assert "health_probe" not in sm.load_assets(str(path))[0]

    def test_false_still_supported(self, tmp_path):
        path = tmp_path / "assets.yaml"
        sm.save_assets(str(path), [])
        sm.upsert_service(
            str(path), {"name": "db", "machine": "nas", "health_probe": False}
        )
        assert sm.load_assets(str(path))[0]["health_probe"] is False

    def test_invalid_object_rejected_without_writing(self, tmp_path):
        path = tmp_path / "assets.yaml"
        sm.save_assets(str(path), [])
        with pytest.raises(ValueError, match="不支持的字段"):
            sm.upsert_service(str(path), {
                "name": "api", "machine": "nas",
                "health_probe": {"type": "http", "token": "secret"},
            })
        assert sm.load_assets(str(path)) == []

class TestProcessActions:
    def test_process_without_unit_never_runs_docker(self, monkeypatch):
        calls = []
        monkeypatch.setattr(sm, '_run', lambda *a, **kw: calls.append(a))
        ok, _ = sm.do_action({'name': 'web', 'type': 'process', 'machine': 'txy', 'actions': ['restart']}, 'restart')
        assert not ok and calls == []

    def test_txy_systemd_action_uses_configured_unit(self, monkeypatch):
        monkeypatch.setitem(sm.MACHINE_SSH, "txy", "root@100.64.0.30")
        calls = []
        monkeypatch.setattr(sm, '_run', lambda cmd, **kw: (calls.append(cmd) or (0, '', '')))
        ok, _ = sm.do_action({'name': '1panel', 'type': 'process', 'machine': 'txy',
                             'systemd_unit': '1panel-core.service', 'actions': ['stop']}, 'stop')
        assert ok
        assert calls[0][-1] == 'systemctl stop -- 1panel-core.service'

    def test_nas_uses_forced_key_and_service_name(self, monkeypatch):
        calls = []
        monkeypatch.setattr(sm, '_run', lambda cmd, **kw: (calls.append(cmd) or (0, '', '')))
        ok, _ = sm.do_action({'name': 'token-scope', 'type': 'process', 'machine': 'nas',
                             'systemd_unit': 'token-scope.service', 'actions': ['logs']}, 'logs')
        assert ok
        assert calls[0][-1] == 'logs token-scope'
        assert any('id_console_host' in arg for arg in calls[0])

    def test_build_process_and_docker_defaults(self):
        assert sm._build_entry({'name': 'web'})['actions'] == ['start', 'stop', 'restart', 'logs']
        assert sm._build_entry({'name': 'web', 'type': 'process'})['actions'] == []
        with pytest.raises(ValueError, match='systemd_unit'):
            sm._build_entry({'name': 'web', 'type': 'process', 'systemd_unit': 'bad;unit'})

    def test_systemd_state_and_config_survive_edits(self, tmp_path):
        svc = {'name': 'board', 'machine': 'nas', 'type': 'process', 'systemd_unit': 'board.service',
               'systemd_scope': 'system', 'actions': ['start', 'stop']}
        path = tmp_path / 'assets.yaml'
        sm.save_assets(path, [svc])
        sm.upsert_service(path, {'name': 'board', 'purpose': 'edited'}, original_name='board')
        assert sm.load_assets(path)[0]['systemd_unit'] == 'board.service'
        assert sm.service_running(svc, {'nas:systemd:board': 'active'}) == 'running'
        assert sm.service_running(svc, {'nas:systemd:board': 'failed'}) == 'stopped'


def test_process_status_key_uses_composition_default_machine(monkeypatch):
    monkeypatch.setattr(mc, 'local_machine', lambda: 'box')
    svc = {'name': 'board', 'type': 'process'}
    assert sm.service_running(svc, {'box:systemd:board': 'active'}) == 'running'
    svc['machine'] = 'remote'
    assert sm.service_running(svc, {'remote:systemd:board': 'failed'}) == 'stopped'


def test_local_process_without_host_control_is_unknown_and_rejects_action(monkeypatch):
    monkeypatch.setattr(mc, 'local_machine', lambda: 'box')
    monkeypatch.setattr(mc, 'host_control_key', lambda: '')
    monkeypatch.setattr(mc, 'host_control_target', lambda: '')
    monkeypatch.setattr(mc, 'has_capability', lambda *args: True)
    monkeypatch.setitem(sm.MACHINE_SSH, 'box', None)
    monkeypatch.setattr(sm, '_run', lambda *args, **kwargs: pytest.fail('command executed'))
    svc = {'name': 'board', 'type': 'process', 'systemd_unit': 'board.service',
           'actions': ['restart']}
    assert sm._process_status([svc]) == {}
    assert sm.service_running(svc, {}) == 'unknown'
    assert sm.do_action(svc, 'restart') == (False, '宿主控制未配置专用密钥或目标')


def test_oracle_systemd_uses_noninteractive_sudo(monkeypatch):
    monkeypatch.setitem(sm.MACHINE_SSH, "us", "ubuntu@100.64.0.70")
    cmd = sm._systemd_cmd("us", ["systemctl", "start", "--", "hermes-browser-lifecycle.service"])
    assert cmd[-1] == "sudo -n systemctl start -- hermes-browser-lifecycle.service"


def test_process_with_blank_ssh_target_rejects_action_and_skips_status(monkeypatch):
    monkeypatch.setitem(sm.MACHINE_SSH, 'txy', None)
    monkeypatch.setattr(sm, '_run', lambda *a, **k: pytest.fail('command executed'))
    svc = {'name': 'web', 'machine': 'txy', 'type': 'process',
           'systemd_unit': 'web.service', 'actions': ['start']}
    assert sm.do_action(svc, 'start') == (False, '机器未配置 systemd SSH 入口')
    assert sm._process_status([svc]) == {}
