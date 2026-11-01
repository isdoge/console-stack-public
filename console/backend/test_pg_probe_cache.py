# -*- coding: utf-8 -*-
"""probe_cache 采集门禁回归（PG-02 / Q1）。

断言「Mac 启动/GET/刷新不会采样」：client 下不发 TCP/HTTP 请求、不落历史、不推事件。
探测分支本身由既有 `test_probe_cache.py` 覆盖（那批用例声明 authority）。
"""
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import monitor_events  # noqa: E402
import probe_cache as pc  # noqa: E402
import service_health_history as shh  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("CONSOLE_PROBE_INTERVAL", raising=False)
    monkeypatch.delenv("CONSOLE_MACHINE_PROBE_INTERVAL", raising=False)
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "client")
    pc.invalidate()
    yield
    pc.invalidate()


def _boom(*args, **kwargs):
    raise AssertionError("client 实例不得发起探测")


def test_client_service_health_does_not_probe(monkeypatch):
    monkeypatch.setattr(pc, "check_tcp", _boom)
    monkeypatch.setattr(pc, "check_http", _boom)
    monkeypatch.setattr(pc, "check_http_semantic", _boom)
    monkeypatch.setattr(pc, "get_ssl_expiry_days", _boom)

    status, msg = pc.check_service_health(
        {"name": "svc", "type": "docker", "port": 8080, "machine": "nas"}
    )
    assert status == "Unknown"
    assert "不采集" in msg


def test_client_service_health_returns_cached_value(monkeypatch):
    """本地已有缓存时返回缓存（不因门禁把页面打成一片 Unknown）。"""
    monkeypatch.setattr(pc, "check_tcp", lambda ip, port: (True, "Port Open"))
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authority")
    pc.check_service_health({"name": "svc", "type": "docker", "port": 8080})
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "client")
    monkeypatch.setattr(pc, "check_tcp", _boom)
    status, msg = pc.check_service_health(
        {"name": "svc", "type": "docker", "port": 8080}, force=True
    )
    assert status == "Healthy"
    assert msg == "Port Open"


def test_client_machine_probe_does_not_probe(monkeypatch):
    monkeypatch.setattr(pc, "check_tcp", _boom)
    monkeypatch.setattr(
        pc.mc, "get_machine",
        lambda mid: {"id": "kr", "role": "node", "probe": {"port": 62037}},
    )
    monkeypatch.setattr(pc.mc, "tailscale_ip", lambda mid: "100.64.0.9")
    status, msg, ts = pc.check_machine_probe("kr", force=True)
    assert (status, msg, ts) == ("unknown", "", None)


def test_client_does_not_write_history_or_events(monkeypatch):
    """即使探测分支被绕过，也不该有落库调用：本用例断言 client 走不到落库点。"""
    writes = []
    monkeypatch.setattr(shh, "record", lambda *a, **kw: writes.append("history"))
    monkeypatch.setattr(monitor_events, "observe_service", lambda *a, **kw: writes.append("event"))
    monkeypatch.setattr(pc, "check_tcp", _boom)

    pc.check_service_health({"name": "svc", "type": "docker", "port": 8080})
    assert writes == []


def test_probe_all_machines_is_noop_for_client(monkeypatch):
    monkeypatch.setattr(pc, "check_machine_probe", _boom)
    assert pc.probe_all_machines() == 0


def test_client_public_probe_does_not_resolve_or_connect(monkeypatch):
    """家宽公网探测同一条门禁：Mac 的出口是 Surge，探出来的也不是家宽。"""
    monkeypatch.setattr(pc, "check_tcp", _boom)
    monkeypatch.setattr(pc, "_resolve_ipv4", _boom)
    assert pc.check_machine_public_probe("kr", force=True) == ("unknown", "", None)


def test_probe_all_public_is_noop_for_client(monkeypatch):
    monkeypatch.setattr(pc, "check_machine_public_probe", _boom)
    assert pc.probe_all_public() == 0


def test_authority_still_probes_and_records(monkeypatch):
    """对照组：authority 照常探测并落库（门禁不是把功能关掉）。"""
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authority")
    recorded = []
    monkeypatch.setattr(shh, "record", lambda name, status, **kw: recorded.append((name, status)))
    monkeypatch.setattr(monitor_events, "observe_service", lambda *a, **kw: None)
    monkeypatch.setattr(pc, "check_tcp", lambda ip, port: (True, "Port Open"))

    status, _ = pc.check_service_health({"name": "svc", "type": "docker", "port": 8080})
    assert status == "Healthy"
    assert recorded == [("svc", "Healthy")]


def test_gate_does_not_touch_db_module(monkeypatch):
    """门禁是叶子逻辑：client 分支不该尝试连接数据库。"""
    import storage_dispatch

    monkeypatch.setattr(
        storage_dispatch, "write_tx", _boom
    )
    status, _ = pc.check_service_health({"name": "svc", "type": "docker", "port": 1})
    assert status == "Unknown"
