# -*- coding: utf-8 -*-
"""monitoring_api.py 回归（B4-3）：enrol/ingest/revoke 三接口的认证、限流、版本协商。

覆盖：
- enrol：首注册发 secret、重复 409、台账外 machine_id 400、坏 JSON 400；
- ingest：成功 202（含 inserted 去重计数）、无/错密钥 401（带 WWW-Authenticate: Bearer）、
  吊销后 401、滑动窗口超频 429、协议版本越界 426、agent 版本格式 426、
  样本校验 400、请求体超 1MB 413；
- revoke：Basic 管理员认证、成功后 ingest 401、未知机器 404；
- 审计日志：成功/认证失败/限流/版本拒收各记一条；
- 三条路由登记 CONSOLE_API_PATHS，不被 Beszel 同源代理吞掉（JSON 而非 HTML）。
"""
import logging
import pathlib
import sys

import pytest
import yaml

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import machine_config as mc  # noqa: E402
import monitoring_api  # noqa: E402

pytest_plugins = ("pg02_support",)

MACHINES_YAML = [
    {"id": "nas", "tailscale": "100.64.0.1"},
    {"id": "txy", "tailscale": "100.64.0.2"},
]


def _ingest_body(machine_id="nas", **overrides):
    payload = {
        "machine_id": machine_id,
        "agent_version": "0.1.0",
        "protocol_version": "v1",
        "samples": [
            {"metric_name": "cpu_percent", "metric_value": 42.5, "sampled_at": "2026-10-24T08:00:00Z"},
        ],
    }
    payload.update(overrides)
    return payload


def _agent_batch(machine_id="nas", **overrides):
    """真实 agent 形状（agent/sender/http.go Batch + collector.Payload 嵌套）。"""
    batch = {
        "protocol": "v1",
        "agent_version": "0.1.0",
        "machine_id": machine_id,
        "sample_interval_sec": 300,
        "samples": [
            {
                "sampled_at": "2026-10-24T10:00:00Z",
                "seq": 42,
                "host": {
                    "cpu_used_pct": 12.5,
                    "mem_used_bytes": 4096,
                    "nics": {"ens5": {"sent_bytes_total": 1000, "recv_bytes_total": 2000}},
                },
                "containers": [{"name": "nginx", "cpu_pct": 1.5, "memory_used_bytes": 100}],
            }
        ],
    }
    batch.update(overrides)
    return batch


@pytest.fixture()
def client(tmp_path, monkeypatch):
    path = tmp_path / "machines.yaml"
    path.write_text(yaml.safe_dump(MACHINES_YAML), encoding="utf-8")
    monkeypatch.setenv("CONSOLE_MACHINES_PATH", str(path))
    mc.invalidate()

    monkeypatch.setenv("CONSOLE_AUTH_USER", "admin")
    monkeypatch.setenv("CONSOLE_AUTH_PASSWORD", "adminpass")
    monkeypatch.setenv("CONSOLE_MONITORING_ENROLL_TOKEN", "test-enroll-token")

    import app as console_app

    monitoring_api.reset_for_tests()
    # 注册时 app.py 传入的 resolver 读真实台账；测试直接钉死白名单，隔离环境
    monkeypatch.setattr(monitoring_api, "_resolve_machine_ids", lambda: {"nas", "txy"})
    yield console_app.app.test_client()
    monitoring_api.reset_for_tests()
    mc.invalidate()


@pytest.fixture()
def nas_secret(client):
    resp = client.post(
        "/api/monitoring/v1/enrol",
        json={"machine_id": "nas", "display_name": "NAS Demo", "public_key": "key-nas", "enroll_token": "test-enroll-token"},
    )
    assert resp.status_code == 200
    return resp.get_json()["secret"]


def _auth_post(client, path, payload, token):
    headers = {"Authorization": "Bearer " + token} if token is not None else {}
    return client.post(path, json=payload, headers=headers)


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


def test_ingest_success(client, nas_secret):
    resp = _auth_post(client, "/api/monitoring/v1/ingest", _ingest_body(), nas_secret)
    assert resp.status_code == 202
    assert resp.get_json() == {"status": "accepted", "inserted": 1}


def test_ingest_dedup_returns_zero(client, nas_secret):
    """同 machine_id + sampled_at + metric_name 的重复上报不重复记样（B4-2 契约）。"""
    body = _ingest_body()
    assert _auth_post(client, "/api/monitoring/v1/ingest", body, nas_secret).status_code == 202
    again = _auth_post(client, "/api/monitoring/v1/ingest", body, nas_secret)
    assert again.status_code == 202
    assert again.get_json()["inserted"] == 0


def test_ingest_unauthorized(client):
    resp = _auth_post(client, "/api/monitoring/v1/ingest", _ingest_body(), None)
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_ingest_wrong_secret(client, nas_secret):
    resp = _auth_post(client, "/api/monitoring/v1/ingest", _ingest_body(), "wrong-token")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_ingest_rate_limit(client, nas_secret):
    """单机器 5 分钟窗口 12 次；第 13 次 429。"""
    for _ in range(12):
        assert _auth_post(client, "/api/monitoring/v1/ingest", _ingest_body(), nas_secret).status_code == 202
    blocked = _auth_post(client, "/api/monitoring/v1/ingest", _ingest_body(), nas_secret)
    assert blocked.status_code == 429
    # 限流按机器隔离：另一台（已注册）不受 nas 的窗口影响
    txy = client.post(
        "/api/monitoring/v1/enrol",
        json={"machine_id": "txy", "display_name": "TXY", "public_key": "key-txy", "enroll_token": "test-enroll-token"},
    ).get_json()["secret"]
    assert _auth_post(client, "/api/monitoring/v1/ingest", _ingest_body("txy"), txy).status_code == 202


def test_ingest_protocol_version_rejected(client, nas_secret):
    body = _ingest_body(protocol_version="v0")
    resp = _auth_post(client, "/api/monitoring/v1/ingest", body, nas_secret)
    assert resp.status_code == 426


def test_ingest_agent_version_malformed(client, nas_secret):
    body = _ingest_body(agent_version="0.1")
    resp = _auth_post(client, "/api/monitoring/v1/ingest", body, nas_secret)
    assert resp.status_code == 426


def test_ingest_bad_samples_400(client, nas_secret):
    bad = _ingest_body(samples=[{"metric_name": "cpu_percent", "metric_value": "high", "sampled_at": "2026-10-24T08:00:00Z"}])
    resp = _auth_post(client, "/api/monitoring/v1/ingest", bad, nas_secret)
    assert resp.status_code == 400


def test_ingest_body_too_large_413(client, nas_secret):
    resp = client.post(
        "/api/monitoring/v1/ingest",
        data=b"x" * (monitoring_api.MAX_BODY_BYTES + 1),
        headers={"Authorization": "Bearer " + nas_secret},
    )
    assert resp.status_code == 413


# ---------------------------------------------------------------------------
# B5-5 收口：真实 agent 嵌套形状的拍平路径
# ---------------------------------------------------------------------------


def test_ingest_agent_nested_payload_202(client, nas_secret):
    """真实 agent 形状（顶层 protocol + 嵌套 host）→ 拍平 → 202，样本可从 store 查回。"""
    resp = _auth_post(client, "/api/monitoring/v1/ingest", _agent_batch(), nas_secret)
    assert resp.status_code == 202
    # host 2 字段 + nic 2 + container 2 = 6 条扁平样本
    assert resp.get_json() == {"status": "accepted", "inserted": 6}
    store = monitoring_api._store
    stored = [
        s for s in store._samples if s["machine_id"] == "nas"
    ]
    names = {s["metric_name"] for s in stored}
    assert names == {
        "cpu_used_pct",
        "mem_used_bytes",
        "net_sent_total_bytes@ens5",
        "net_recv_total_bytes@ens5",
        "container_nginx_cpu_pct",
        "container_nginx_memory_used_bytes",
    }
    assert all(s["sampled_at"] == "2026-10-24T10:00:00Z" for s in stored)


def test_ingest_flat_shape_still_accepted(client, nas_secret):
    """curl 手工形状（protocol_version + 扁平 samples）向后兼容仍 202。"""
    resp = _auth_post(client, "/api/monitoring/v1/ingest", _ingest_body(), nas_secret)
    assert resp.status_code == 202
    assert resp.get_json() == {"status": "accepted", "inserted": 1}


def test_ingest_expand_failure_400(client, nas_secret):
    """拍平失败（samples[0] 缺 host/containers，产出零条 metric）→ 400 而非 426。"""
    bad = _agent_batch()
    del bad["samples"][0]["host"]
    del bad["samples"][0]["containers"]
    resp = _auth_post(client, "/api/monitoring/v1/ingest", bad, nas_secret)
    assert resp.status_code == 400
    assert "expand failed" in resp.get_json()["error"]


def test_ingest_agent_protocol_mismatch_426(client, nas_secret):
    """真实 agent 形状但协议版本不符 → 仍走 426（expand 抛错映射 400 的边界之外）。"""
    resp = _auth_post(client, "/api/monitoring/v1/ingest", _agent_batch(protocol="v0"), nas_secret)
    assert resp.status_code == 426


def test_ingest_agent_payload_lands_in_pg(pg_client, monkeypatch):
    """B5-5 PG store 接线全链路：真实 agent 形状 → 拍平 → PG monitoring_samples 可查。"""
    import app as console_app

    monitoring_api.reset_for_tests()
    monkeypatch.setattr(monitoring_api, "_resolve_machine_ids", lambda: {"nas"})
    monitoring_api.configure_store(console_app._PgMachineStore())
    try:
        # API 注册由 PG 生成并保存同一 secret；返回后可直接用于真实上报。
        monkeypatch.setenv("CONSOLE_MONITORING_ENROLL_TOKEN", "test-enroll-token")
        client_pg = console_app.app.test_client()
        enrolled = client_pg.post("/api/monitoring/v1/enrol", json={
            "machine_id": "nas",
            "enroll_token": "test-enroll-token",
            "agent_version": "0.1.0",
            "protocol_version": "v1",
        })
        assert enrolled.status_code == 200
        secret = enrolled.get_json()["secret"]
        resp = client_pg.post(
            "/api/monitoring/v1/ingest",
            json=_agent_batch(),
            headers={"Authorization": "Bearer " + secret},
        )
        assert resp.status_code == 202
        assert resp.get_json() == {"status": "accepted", "inserted": 6}
        from pg02_support import fetch_all

        rows = fetch_all(
            pg_client,
            "SELECT metric_name, metric_value FROM monitoring_samples "
            "WHERE machine_id = 'nas' ORDER BY metric_name",
        )
        names = {row["metric_name"] for row in rows}
        assert names == {
            "cpu_used_pct",
            "mem_used_bytes",
            "net_sent_total_bytes@ens5",
            "net_recv_total_bytes@ens5",
            "container_nginx_cpu_pct",
            "container_nginx_memory_used_bytes",
        }
        values = {row["metric_name"]: row["metric_value"] for row in rows}
        assert values["cpu_used_pct"] == pytest.approx(12.5)
        assert values["net_sent_total_bytes@ens5"] == pytest.approx(1000)
    finally:
        monitoring_api.reset_for_tests()


# ---------------------------------------------------------------------------
# enrol
# ---------------------------------------------------------------------------


def test_enrol_requires_configured_token(client, monkeypatch):
    monkeypatch.delenv("CONSOLE_MONITORING_ENROLL_TOKEN")
    resp = client.post("/api/monitoring/v1/enrol", json={"machine_id": "nas"})
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("payload", [
    {"machine_id": "nas"},
    {"machine_id": "nas", "enroll_token": "wrong-token"},
])
def test_enrol_rejects_missing_or_wrong_token(client, payload):
    resp = client.post("/api/monitoring/v1/enrol", json=payload)
    assert resp.status_code == 401


def test_enrol_accepts_real_go_shape(client, monkeypatch):
    monkeypatch.setattr(monitoring_api, "_resolve_machine_ids", lambda: {"nas"})
    response = client.post("/api/monitoring/v1/enrol", json={
        "machine_id": "nas",
        "enroll_token": "test-enroll-token",
        "agent_version": "0.1.0",
        "protocol_version": "v1",
    })
    assert response.status_code == 200
    secret = response.get_json()["secret"]
    stored = monitoring_api._store._machines["nas"]
    assert stored["secret_sha256"] == monitoring_api._sha256_hex(secret)


@pytest.mark.parametrize("payload", [
    {"protocol": "v0"},
    {"protocol_version": "v0"},
    {"agent_version": "0.1"},
])
def test_enrol_rejects_unsupported_agent_version(client, payload):
    payload.update(machine_id="nas", enroll_token="test-enroll-token")
    resp = client.post("/api/monitoring/v1/enrol", json=payload)
    assert resp.status_code == 426


def test_enrol_success(client):
    resp = client.post(
        "/api/monitoring/v1/enrol",
        json={"machine_id": "nas", "display_name": "NAS Demo", "public_key": "key-nas", "enroll_token": "test-enroll-token"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "enrolled"
    assert isinstance(body["secret"], str) and body["secret"]


def test_enrol_conflict(client, nas_secret):
    resp = client.post(
        "/api/monitoring/v1/enrol",
        json={"machine_id": "nas", "display_name": "again", "public_key": "key2", "enroll_token": "test-enroll-token"},
    )
    assert resp.status_code == 409


def test_enrol_conflict_pg_store(pg_client, monkeypatch):
    """B6-2 修复：PG store 对已 enrol 机器抛 AlreadyEnrolledError（非返回 None），
    路由须映射为 409 而非 500——txy 实测撞出 500 后补的回归。"""
    import app as console_app

    monitoring_api.reset_for_tests()
    monkeypatch.setattr(monitoring_api, "_resolve_machine_ids", lambda: {"nas"})
    monitoring_api.configure_store(console_app._PgMachineStore())
    try:
        monkeypatch.setenv("CONSOLE_MONITORING_ENROLL_TOKEN", "test-enroll-token")
        client_pg = console_app.app.test_client()
        first = client_pg.post("/api/monitoring/v1/enrol", json={
            "machine_id": "nas", "enroll_token": "test-enroll-token",
            "agent_version": "0.1.0", "protocol_version": "v1",
        })
        assert first.status_code == 200
        again = client_pg.post("/api/monitoring/v1/enrol", json={
            "machine_id": "nas", "enroll_token": "test-enroll-token",
            "agent_version": "0.1.0", "protocol_version": "v1",
        })
        assert again.status_code == 409
        assert again.get_json() == {"error": "machine already enrolled"}
    finally:
        monitoring_api.reset_for_tests()


def test_enrol_unknown_machine_400(client):
    resp = client.post(
        "/api/monitoring/v1/enrol",
        json={"machine_id": "ghost", "display_name": "ghost", "public_key": "k", "enroll_token": "test-enroll-token"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# revoke
# ---------------------------------------------------------------------------


def _revoke(client, machine_id, auth=("admin", "adminpass")):
    headers = {}
    if auth is not None:
        headers["Authorization"] = "Basic " + _b64("%s:%s" % auth)
    return client.post("/api/monitoring/v1/revoke", json={"machine_id": machine_id}, headers=headers)


def _b64(text):
    import base64

    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def test_revoke_requires_admin(client):
    resp = _revoke(client, "nas", auth=None)
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"].startswith("Basic")


def test_revoke_unknown_machine_404(client):
    resp = _revoke(client, "ghost")
    assert resp.status_code == 404


def test_revoke_success(client, nas_secret):
    resp = _revoke(client, "nas")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "revoked"}


def test_ingest_revoked(client, nas_secret):
    assert _revoke(client, "nas").status_code == 200
    after = _auth_post(client, "/api/monitoring/v1/ingest", _ingest_body(), nas_secret)
    assert after.status_code == 401


# ---------------------------------------------------------------------------
# 审计与反代理
# ---------------------------------------------------------------------------


def test_audit_log_records_events(client, nas_secret, caplog):
    with caplog.at_level(logging.INFO, logger="console.backend.monitoring"):
        _auth_post(client, "/api/monitoring/v1/ingest", _ingest_body(), nas_secret)
        _auth_post(client, "/api/monitoring/v1/ingest", _ingest_body(), None)
        _auth_post(client, "/api/monitoring/v1/ingest", _ingest_body(protocol_version="v9"), nas_secret)
        _revoke(client, "nas")
        _auth_post(client, "/api/monitoring/v1/ingest", _ingest_body(), nas_secret)
    events = [(r.module if hasattr(r, "module") else "", r.getMessage()) for r in caplog.records]
    text = "\n".join(message for _mod, message in events)
    assert "monitoring_ingest status=202" in text
    assert "reason=missing_token" in text
    assert "reason=protocol_unsupported" in text
    assert "monitoring_revoke status=200" in text
    assert "reason=auth_failed" in text


def test_routes_not_swallowed_by_beszel_proxy(client, monkeypatch):
    """漏登记 CONSOLE_API_PATHS 的路由会被 Beszel 代理吞成 HTML+200，这里必须 JSON。"""
    import app as console_app

    def boom(*args, **kwargs):
        raise AssertionError("monitoring 路由被 Beszel 代理接管了")

    monkeypatch.setattr(console_app, "_proxy_authority", boom)
    for path in (
        "/api/monitoring/v1/ingest",
        "/api/monitoring/v1/enrol",
        "/api/monitoring/v1/revoke",
    ):
        assert console_app._is_console_api(path)
    resp = client.post("/api/monitoring/v1/ingest", json=_ingest_body())
    assert resp.mimetype == "application/json"
