# -*- coding: utf-8 -*-
"""Regression tests for probe_cache.py — 健康探测结果缓存。

遵循 checker 测试隔离规范：不触网，check_tcp/check_http 全部 monkeypatch；
模块级 _cache 在每条用例前后清空，避免用例间串扰。
"""
import pathlib
import sys
from types import SimpleNamespace

import pytest
from flask import Flask

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import machine_config as mc  # noqa: E402
import plugin_contributions  # noqa: E402
import plugin_imports  # noqa: E402
import plugin_runtime  # noqa: E402
import probe_cache  # noqa: E402
import renewals  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    """隔离模块级缓存与 TTL 环境变量；健康时序与事件落库统一打桩，不碰真实库。

    `check_service_health` 没有 db_path 参数，两个落库点都走 `paths.DB_PATH`
    （= 开发机 `console/backend/data/console.db`）。不打桩的话跑一次单测就会
    往真库里塞 svc-h2 之类的假数据，所以这里一次性堵死。

    PG-02：真实探测受采集门禁约束（Q1），本文件的用例断言的是探测行为，
    所以默认按 authority 运行；「client 不采集」的用例在 `TestCollectionGate`
    里显式改成 client。
    """
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authority")
    monkeypatch.delenv("CONSOLE_PROBE_INTERVAL", raising=False)
    monkeypatch.delenv("CONSOLE_MACHINE_PROBE_INTERVAL", raising=False)
    import monitor_events
    import service_health_history as shh

    monkeypatch.setattr(shh, "record", lambda *a, **kw: False)
    monkeypatch.setattr(monitor_events, "observe_service", lambda *a, **kw: None)
    monkeypatch.setattr(monitor_events, "observe_machine", lambda *a, **kw: None)
    monkeypatch.setattr(plugin_contributions, "_registry", {})
    probe_cache.invalidate()
    mc.invalidate()
    yield
    probe_cache.invalidate()
    mc.invalidate()


# ---------------------------------------------------------------------------
# probe_interval / machine_probe_interval
# ---------------------------------------------------------------------------

class TestProbeInterval:
    def test_default_is_600(self):
        assert probe_cache.probe_interval() == 600

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CONSOLE_PROBE_INTERVAL", "120")
        assert probe_cache.probe_interval() == 120

    def test_floor_is_60(self, monkeypatch):
        monkeypatch.setenv("CONSOLE_PROBE_INTERVAL", "5")
        assert probe_cache.probe_interval() == 60

    def test_invalid_value_falls_back_to_600(self, monkeypatch):
        monkeypatch.setenv("CONSOLE_PROBE_INTERVAL", "abc")
        assert probe_cache.probe_interval() == 600


class TestMachineProbeInterval:
    """机器探针 TTL 与服务健康解耦：默认 1800s，对齐半点节点。"""

    def test_default_is_1800(self):
        assert probe_cache.machine_probe_interval() == 1800

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CONSOLE_MACHINE_PROBE_INTERVAL", "900")
        assert probe_cache.machine_probe_interval() == 900

    def test_floor_is_60(self, monkeypatch):
        monkeypatch.setenv("CONSOLE_MACHINE_PROBE_INTERVAL", "5")
        assert probe_cache.machine_probe_interval() == 60

    def test_invalid_value_falls_back_to_1800(self, monkeypatch):
        monkeypatch.setenv("CONSOLE_MACHINE_PROBE_INTERVAL", "abc")
        assert probe_cache.machine_probe_interval() == 1800

    def test_service_interval_unaffected_by_machine_env(self, monkeypatch):
        monkeypatch.setenv("CONSOLE_MACHINE_PROBE_INTERVAL", "3600")
        assert probe_cache.probe_interval() == 600


# ---------------------------------------------------------------------------
# next_probe_slot — 本地对齐的时间节点
# ---------------------------------------------------------------------------

class TestNextProbeSlot:
    def test_half_hour_nodes(self):
        from datetime import datetime as dt

        assert probe_cache.next_probe_slot(dt(2026, 9, 3, 10, 7)) == dt(2026, 9, 3, 10, 30)
        assert probe_cache.next_probe_slot(dt(2026, 9, 3, 10, 30)) == dt(2026, 9, 3, 11, 0)
        assert probe_cache.next_probe_slot(dt(2026, 9, 3, 0, 0, 1)) == dt(2026, 9, 3, 0, 30)

    def test_crosses_midnight(self):
        from datetime import datetime as dt

        assert probe_cache.next_probe_slot(dt(2026, 9, 3, 23, 45)) == dt(2026, 9, 4, 0, 0)

    def test_interval_generalizes_nodes(self):
        """间隔可调：900s → 刻钟节点，3600s → 整点节点。"""
        from datetime import datetime as dt

        assert probe_cache.next_probe_slot(dt(2026, 9, 3, 10, 7), interval=900) == dt(
            2026, 9, 3, 10, 15
        )
        assert probe_cache.next_probe_slot(dt(2026, 9, 3, 10, 7), interval=3600) == dt(
            2026, 9, 3, 11, 0
        )


# ---------------------------------------------------------------------------
# check_service_health — 探测分支
# ---------------------------------------------------------------------------

class TestCheckServiceHealth:
    def test_explicitly_disabled_probe_skips_network_and_caches_unknown(self, monkeypatch):
        monkeypatch.setattr(
            probe_cache, "check_tcp",
            lambda *a, **kw: pytest.fail("disabled service must not be probed"),
        )
        cleared = []
        monkeypatch.setattr(
            probe_cache, "_clear_stale_band", lambda name: cleared.append(name)
        )
        svc = {
            "name": "internal-db",
            "type": "docker",
            "port": 5432,
            "machine": "nas",
            "health_probe": False,
        }

        status, msg = probe_cache.check_service_health(svc)

        assert status == "Unknown"
        assert msg == "Health probe disabled by assets.yaml"
        assert probe_cache.peek_health("internal-db") == (status, msg)
        # 关闭探测同时清残留红格时序（beszel-agent-aly 陈旧 Unhealthy 色带）
        assert cleared == ["internal-db"]

    def test_disabled_probe_reclaims_stale_active_event(self, monkeypatch):
        """``health_probe: false`` 的服务没有真实探测，旧活动事件不会自愈关闭。
        关闭探测的真实路径上应补一次 ``observe_service(ok=True)`` 回收悬挂事件
        （beszel-agent-txy/aly 09-07 挂 16 天未恢复的根因）。
        """
        monkeypatch.setattr(
            probe_cache, "check_tcp",
            lambda *a, **kw: pytest.fail("disabled service must not be probed"),
        )
        calls = []
        import monitor_events

        monkeypatch.setattr(
            monitor_events,
            "observe_service",
            lambda name, status, machine="", detail="", **kw: calls.append(
                (name, status, machine)
            ),
        )
        svc = {
            "name": "beszel-agent-aly",
            "type": "docker",
            "port": 45876,
            "machine": "aly",
            "health_probe": False,
        }

        probe_cache.check_service_health(svc, force=True)

        assert calls == [("beszel-agent-aly", "Healthy", "aly")]

    def test_disabled_probe_reclaim_swallows_db_error(self, monkeypatch):
        """observe_service 抛异常时不影响探测主线（返回 Unknown，不向上抛）。"""
        monkeypatch.setattr(
            probe_cache, "check_tcp",
            lambda *a, **kw: pytest.fail("disabled service must not be probed"),
        )
        import monitor_events

        monkeypatch.setattr(
            monitor_events, "observe_service",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down")),
        )
        svc = {"name": "svc-off", "health_probe": False, "machine": "nas"}

        status, msg = probe_cache.check_service_health(svc, force=True)

        assert status == "Unknown"
        assert msg == "Health probe disabled by assets.yaml"

    def test_docker_service_probes_tcp_with_machine_ip(self, monkeypatch):
        seen = {}

        def fake_check_tcp(ip, port, timeout=3):
            seen["ip"], seen["port"] = ip, port
            return True, "Port Open"

        monkeypatch.setattr(probe_cache, "check_tcp", fake_check_tcp)
        svc = {"name": "svc-a", "type": "docker", "port": 8080, "machine": "txy"}
        status, msg = probe_cache.check_service_health(svc)
        assert status == "Healthy"
        assert msg == "Port Open"
        # 机器地址取自 registry（machines.yaml），不再是 probe_cache 本地硬编码
        assert seen == {"ip": mc.tailscale_ip("txy"), "port": 8080}

    def test_missing_machine_uses_composition_local_identity(self, monkeypatch):
        seen = []
        monkeypatch.setattr(mc, "local_machine", lambda: "vps")
        monkeypatch.setattr(mc, "probe_host", lambda machine: seen.append(machine) or "192.0.2.10")
        monkeypatch.setattr(
            probe_cache, "check_tcp",
            lambda ip, port: seen.append((ip, port)) or (True, "Port Open"),
        )

        assert probe_cache.check_service_health({
            "name": "local-service", "type": "process", "port": 8080,
        }) == ("Healthy", "Port Open")
        assert seen == ["vps", ("192.0.2.10", 8080)]

    def test_missing_machine_without_local_identity_does_not_probe_loopback(self, monkeypatch):
        monkeypatch.setattr(mc, "local_machine", lambda: "")
        monkeypatch.setattr(
            mc, "probe_host", lambda *args: pytest.fail("未配置机器时不应解析目标"),
        )
        monkeypatch.setattr(
            probe_cache, "check_tcp", lambda *args: pytest.fail("未配置目标时不应探测回环地址"),
        )
        svc = {"name": "untargeted-service", "type": "process", "port": 8080}

        status, msg = probe_cache.check_service_health(svc)

        assert status == "Unknown"
        assert "未配置" in msg
        assert probe_cache.peek_health(svc["name"]) == (status, msg)

    def test_machine_without_tailscale_falls_back_to_public_host(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            probe_cache, "check_tcp",
            lambda ip, port, timeout=3: seen.update(ip=ip) or (True, "Port Open"),
        )
        monkeypatch.setattr(mc, "tailscale_ip", lambda machine: "")
        monkeypatch.setattr(
            mc, "get_machine", lambda machine: {"public_host": "kr.example.test"},
        )
        svc = {"name": "svc-node", "type": "process", "port": 62037, "machine": "kr"}
        probe_cache.check_service_health(svc)
        assert seen["ip"] == "kr.example.test"

    def test_unknown_machine_falls_back_to_loopback(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            probe_cache, "check_tcp",
            lambda ip, port, timeout=3: seen.update(ip=ip) or (False, "refused"),
        )
        svc = {"name": "svc-b", "type": "process", "port": 9000, "machine": "router"}
        status, _ = probe_cache.check_service_health(svc)
        assert status == "Unhealthy"
        assert seen["ip"] == "127.0.0.1"

    def test_domain_fallback_uses_http(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            probe_cache, "check_http",
            lambda url, timeout=4, expected_code=200: seen.update(url=url) or (True, "Status 200"),
        )
        svc = {"name": "svc-c", "access": {"domain": "https://svc.internal"}}
        status, msg = probe_cache.check_service_health(svc)
        assert status == "Healthy"
        assert msg == "Status 200"
        assert seen["url"] == "https://svc.internal"

    def test_port_without_probeable_type_skips_tcp(self, monkeypatch):
        monkeypatch.setattr(
            probe_cache, "check_tcp",
            lambda *a, **kw: pytest.fail("check_tcp should not be called"),
        )
        svc = {"name": "svc-d", "type": "systemd", "port": 22}
        status, msg = probe_cache.check_service_health(svc)
        assert status == "Unknown"
        assert msg == "No checker configured"

    def test_no_checker_configured(self):
        status, msg = probe_cache.check_service_health({"name": "svc-e"})
        assert status == "Unknown"
        assert msg == "No checker configured"


# ---------------------------------------------------------------------------
# HTTPS 证书可信缓存
# ---------------------------------------------------------------------------

class TestSslCacheDetails:
    @pytest.fixture(autouse=True)
    def _renewals_certificate_contribution(self, monkeypatch, tmp_path):
        settings = tmp_path / "settings.yaml"
        settings.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("CONSOLE_SETTINGS_FILE", str(settings))
        monkeypatch.setattr(plugin_runtime, "_failures", {})
        backend = plugin_imports.load_module("plugins.renewals.backend")
        context = SimpleNamespace(
            contribute=lambda slot, name, callback: plugin_contributions.register(
                "renewals", slot, name, callback,
            ),
        )
        backend.register(Flask(__name__), context)
        assert ("certificate", "renewals") in plugin_contributions._registry
        assert plugin_runtime.is_available("renewals") is True
        return settings

    @staticmethod
    def _svc():
        return {
            "name": "secure-web",
            "access": {"domain": "https://secure.example.test"},
        }

    def test_success_stores_finite_days_and_peek_returns_copy(
        self, monkeypatch
    ):
        synced = []
        monkeypatch.setattr(
            probe_cache, "check_http", lambda url: (True, "HTTP 200")
        )
        monkeypatch.setattr(
            probe_cache, "get_ssl_expiry_days", lambda url: (True, 42)
        )
        monkeypatch.setattr(
            renewals,
            "sync_probe_cert",
            lambda url, days: synced.append((url, days)),
        )

        probe_cache.check_service_health(self._svc(), force=True)
        detail = probe_cache.peek_health_detail("secure-web")

        assert detail["ssl_ok"] is True
        assert detail["ssl_days"] == 42
        assert synced == [("https://secure.example.test", 42)]
        detail["ssl_days"] = 999
        assert probe_cache.peek_health_detail("secure-web")["ssl_days"] == 42

    def test_disabled_plugin_skips_certificate_sync_but_keeps_probe_cache(
        self, monkeypatch, _renewals_certificate_contribution,
    ):
        _renewals_certificate_contribution.write_text(
            "PLUGINS:\n  renewals:\n    enabled: false\n", encoding="utf-8",
        )
        assert plugin_runtime.is_available("renewals") is False
        monkeypatch.setattr(probe_cache, "check_http", lambda url: (True, "HTTP 200"))
        monkeypatch.setattr(probe_cache, "get_ssl_expiry_days", lambda url: (True, 42))
        monkeypatch.setattr(
            renewals, "sync_probe_cert",
            lambda *args: pytest.fail("停用插件不应执行证书同步"),
        )

        assert probe_cache.check_service_health(self._svc(), force=True) == ("Healthy", "HTTP 200")
        assert probe_cache.peek_health_detail("secure-web")["ssl_days"] == 42

    def test_unavailable_plugin_skips_certificate_sync(self, monkeypatch):
        monkeypatch.setitem(plugin_runtime._failures, "renewals", "插件初始化失败")
        assert plugin_runtime.is_available("renewals") is False
        monkeypatch.setattr(probe_cache, "check_http", lambda url: (True, "HTTP 200"))
        monkeypatch.setattr(probe_cache, "get_ssl_expiry_days", lambda url: (True, 42))
        monkeypatch.setattr(
            renewals, "sync_probe_cert",
            lambda *args: pytest.fail("初始化失败的插件不应执行证书同步"),
        )

        probe_cache.check_service_health(self._svc(), force=True)
        assert probe_cache.peek_health_detail("secure-web")["ssl_days"] == 42

    def test_ssl_failure_does_not_cache_error_as_days(self, monkeypatch):
        monkeypatch.setattr(
            probe_cache, "check_http", lambda url: (False, "certificate failed")
        )
        monkeypatch.setattr(
            probe_cache,
            "get_ssl_expiry_days",
            lambda url: (False, "secret certificate error"),
        )
        monkeypatch.setattr(
            renewals,
            "sync_probe_cert",
            lambda *args: pytest.fail("failed certificate must not sync renewal"),
        )

        probe_cache.check_service_health(self._svc(), force=True)
        detail = probe_cache.peek_health_detail("secure-web")

        assert detail["msg"] == "certificate failed"
        assert "ssl_ok" not in detail
        assert "ssl_days" not in detail

    @pytest.mark.parametrize("days", ["42", float("inf"), float("nan"), True])
    def test_ssl_success_with_non_finite_or_non_numeric_days_is_not_trusted(
        self, monkeypatch, days
    ):
        monkeypatch.setattr(
            probe_cache, "check_http", lambda url: (True, "HTTP 200")
        )
        monkeypatch.setattr(
            probe_cache, "get_ssl_expiry_days", lambda url: (True, days)
        )
        monkeypatch.setattr(
            renewals,
            "sync_probe_cert",
            lambda *args: pytest.fail("invalid certificate days must not sync renewal"),
        )

        probe_cache.check_service_health(self._svc(), force=True)
        detail = probe_cache.peek_health_detail("secure-web")

        assert "ssl_ok" not in detail
        assert "ssl_days" not in detail

    def test_peek_missing_never_probes(self, monkeypatch):
        monkeypatch.setattr(
            probe_cache,
            "check_http",
            lambda *args, **kwargs: pytest.fail("peek must not probe"),
        )
        monkeypatch.setattr(
            probe_cache,
            "get_ssl_expiry_days",
            lambda *args, **kwargs: pytest.fail("peek must not inspect certificate"),
        )

        assert probe_cache.peek_health_detail("missing") is None

    def test_tcp_health_with_https_domain_still_collects_ssl(self, monkeypatch):
        """type+port 走 TCP 探活，但带 https domain 时仍采集证书可信值（§4.4）。"""
        synced = []
        monkeypatch.setattr(
            probe_cache, "check_tcp", lambda ip, port, timeout=3: (True, "Port Open")
        )
        monkeypatch.setattr(
            probe_cache, "get_ssl_expiry_days", lambda url: (True, 30)
        )
        monkeypatch.setattr(
            renewals,
            "sync_probe_cert",
            lambda url, days: synced.append((url, days)),
        )
        svc = {
            "name": "tcp-with-tls",
            "type": "docker",
            "port": 443,
            "machine": "nas",
            "access": {"domain": "https://tls.example.test"},
        }
        probe_cache.check_service_health(svc, force=True)
        detail = probe_cache.peek_health_detail("tcp-with-tls")

        assert detail["status"] == "Healthy"
        assert detail["msg"] == "Port Open"
        assert detail["ssl_ok"] is True
        assert detail["ssl_days"] == 30
        assert synced == [("https://tls.example.test", 30)]

    def test_http_domain_without_port_keeps_http_probe(self, monkeypatch):
        """无 port 的 http domain 服务仍走 check_http，不被 TCP 分支误判。"""
        monkeypatch.setattr(
            probe_cache, "check_http", lambda url: (True, "HTTP 200")
        )
        monkeypatch.setattr(
            probe_cache, "get_ssl_expiry_days", lambda url: (False, "self-signed")
        )
        monkeypatch.setattr(
            renewals, "sync_probe_cert", lambda *a: pytest.fail("must not sync")
        )
        svc = {"name": "plain-http", "access": {"domain": "https://web.example.test"}}
        probe_cache.check_service_health(svc, force=True)
        detail = probe_cache.peek_health_detail("plain-http")

        assert detail["status"] == "Healthy"
        assert detail["msg"] == "HTTP 200"
        assert "ssl_ok" not in detail
        assert "ssl_days" not in detail


# ---------------------------------------------------------------------------
# check_service_health — 缓存行为
# ---------------------------------------------------------------------------

class TestCacheBehavior:
    def _svc(self):
        return {"name": "svc-cache", "type": "docker", "port": 8080, "machine": "nas"}

    def test_second_call_hits_cache(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            probe_cache, "check_tcp",
            lambda ip, port, timeout=3: calls.append(1) or (True, "Port Open"),
        )
        probe_cache.check_service_health(self._svc())
        status, msg = probe_cache.check_service_health(self._svc())
        assert len(calls) == 1
        assert (status, msg) == ("Healthy", "Port Open")

    def test_force_bypasses_cache(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            probe_cache, "check_tcp",
            lambda ip, port, timeout=3: calls.append(1) or (True, "Port Open"),
        )
        probe_cache.check_service_health(self._svc())
        probe_cache.check_service_health(self._svc(), force=True)
        assert len(calls) == 2

    def test_expired_cache_reprobes(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            probe_cache, "check_tcp",
            lambda ip, port, timeout=3: calls.append(1) or (True, "Port Open"),
        )
        probe_cache.check_service_health(self._svc())
        # 把缓存时间戳拨回 TTL 之前，模拟过期
        probe_cache._cache["svc-cache"]["ts"] -= probe_cache.probe_interval() + 1
        probe_cache.check_service_health(self._svc())
        assert len(calls) == 2

    def test_invalidate_single_name(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            probe_cache, "check_tcp",
            lambda ip, port, timeout=3: calls.append(1) or (True, "Port Open"),
        )
        probe_cache.check_service_health(self._svc())
        probe_cache.invalidate("svc-cache")
        probe_cache.check_service_health(self._svc())
        assert len(calls) == 2

    def test_invalidate_unknown_name_is_noop(self, monkeypatch):
        monkeypatch.setattr(
            probe_cache, "check_tcp", lambda ip, port, timeout=3: (True, "Port Open"),
        )
        probe_cache.check_service_health(self._svc())
        probe_cache.invalidate("not-exists")  # 不得抛异常
        assert "svc-cache" in probe_cache._cache

    def test_invalidate_all(self, monkeypatch):
        monkeypatch.setattr(
            probe_cache, "check_tcp", lambda ip, port, timeout=3: (True, "Port Open"),
        )
        probe_cache.check_service_health(self._svc())
        probe_cache.invalidate()
        assert probe_cache._cache == {}


# ---------------------------------------------------------------------------
# check_machine_probe — 机器探活（/api/machines 用）
# ---------------------------------------------------------------------------

class TestCheckMachineProbe:
    @pytest.fixture(autouse=True)
    def _no_history_writes(self, monkeypatch):
        """E4 的时序落库改成收集器：单测不写真库，同时能断言记了什么。"""
        recorded = []
        monkeypatch.setattr(
            probe_cache.node_probe, "record",
            lambda machine, status, latency_ms=None, ts=None, db_path=None: recorded.append(
                (machine, status, latency_ms, ts)
            ),
        )
        return recorded

    def test_node_probe_targets_tailscale_same_port(self, monkeypatch):
        """节点探活走 Tailscale IP 打同一端口：mihomo fake-ip 会吃掉公网探测。"""
        seen = {}
        monkeypatch.setattr(mc, "tailscale_ip", lambda mid: "100.64.0.50")
        monkeypatch.setattr(
            probe_cache, "check_tcp",
            lambda host, port, timeout=3: seen.update(host=host, port=port)
            or (True, "Port Open"),
        )
        status, msg, ts = probe_cache.check_machine_probe("kr")
        assert status == "up"
        # tailnet 直连节点同一 sing-box 端口，握手真实（可用性与耗时都可信）
        assert seen == {"host": "100.64.0.50", "port": 62037}
        assert msg.startswith("tcp 100.64.0.50:62037 ok ")
        assert msg.endswith("ms")
        assert ts and ts.endswith("Z")

    def test_node_without_tailscale_falls_back_to_public_host(self, monkeypatch):
        """无 Tailscale 的 node 退回 public_host（保留旧行为兜底）。"""
        seen = {}
        monkeypatch.setattr(mc, "tailscale_ip", lambda mid: "")
        monkeypatch.setattr(
            probe_cache, "check_tcp",
            lambda host, port, timeout=3: seen.update(host=host, port=port)
            or (True, "Port Open"),
        )
        probe_cache.check_machine_probe("kr")
        assert seen == {"host": "kr.sunxc.eu.cc", "port": 62037}

    def test_probe_writes_history_with_latency(self, monkeypatch, _no_history_writes):
        monkeypatch.setattr(
            probe_cache, "check_tcp", lambda host, port, timeout=3: (True, "Port Open"),
        )
        probe_cache.check_machine_probe("kr", force=True)
        assert len(_no_history_writes) == 1
        machine, status, latency_ms, ts = _no_history_writes[0]
        assert (machine, status) == ("kr", "up")
        assert latency_ms is not None and latency_ms >= 0
        assert ts and ts.endswith("Z")

    def test_failed_probe_history_has_no_latency(self, monkeypatch, _no_history_writes):
        monkeypatch.setattr(
            probe_cache, "check_tcp", lambda host, port, timeout=3: (False, "timed out"),
        )
        probe_cache.check_machine_probe("kr", force=True)
        assert _no_history_writes[-1][1] == "down"
        # down 没有有意义的耗时，不记假数值
        assert _no_history_writes[-1][2] is None

    def test_cache_hit_does_not_write_history(self, monkeypatch, _no_history_writes):
        monkeypatch.setattr(
            probe_cache, "check_tcp", lambda host, port, timeout=3: (True, "Port Open"),
        )
        probe_cache.check_machine_probe("kr", force=True)
        probe_cache.check_machine_probe("kr")
        probe_cache.check_machine_probe("kr")
        assert len(_no_history_writes) == 1

    def test_history_write_failure_does_not_break_probe(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(probe_cache.node_probe, "record", boom)
        monkeypatch.setattr(
            probe_cache, "check_tcp", lambda host, port, timeout=3: (True, "Port Open"),
        )
        status, _msg, ts = probe_cache.check_machine_probe("kr", force=True)
        assert status == "up" and ts

    def test_failed_probe_reports_down_with_reason(self, monkeypatch):
        monkeypatch.setattr(
            probe_cache, "check_tcp",
            lambda host, port, timeout=3: (False, "timed out"),
        )
        status, msg, ts = probe_cache.check_machine_probe("sg")
        assert status == "down"
        assert "timed out" in msg
        assert ts is not None

    def test_machine_without_probe_config_is_unknown(self, monkeypatch):
        monkeypatch.setattr(
            mc, "get_machine",
            lambda machine: {"id": "x", "role": "node"},  # 无 probe 配置
        )
        monkeypatch.setattr(
            probe_cache, "check_tcp",
            lambda *a, **kw: pytest.fail("无 probe 配置的机器不应发起探测"),
        )
        assert probe_cache.check_machine_probe("x") == ("unknown", "", None)

    def test_server_probe_targets_tailscale(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            probe_cache, "check_tcp",
            lambda host, port, timeout=3: seen.update(host=host, port=port)
            or (True, "Port Open"),
        )
        status, msg, ts = probe_cache.check_machine_probe("txy")
        assert status == "up"
        # Q3：server 探活走 tailscale IP（内网），不走公网
        assert seen == {"host": "100.64.0.30", "port": 22}
        assert msg.startswith("tcp 100.64.0.30:22 ok ")

    def test_unknown_machine_is_unknown(self, monkeypatch):
        monkeypatch.setattr(
            probe_cache, "check_tcp",
            lambda *a, **kw: pytest.fail("未知机器不应发起探测"),
        )
        assert probe_cache.check_machine_probe("router") == ("unknown", "", None)

    def test_second_call_hits_cache_and_force_bypasses(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            probe_cache, "check_tcp",
            lambda host, port, timeout=3: calls.append(1) or (True, "Port Open"),
        )
        probe_cache.check_machine_probe("kr")
        probe_cache.check_machine_probe("kr")
        assert len(calls) == 1
        probe_cache.check_machine_probe("kr", force=True)
        assert len(calls) == 2

    def test_machine_cache_is_separate_from_service_cache(self, monkeypatch):
        monkeypatch.setattr(
            probe_cache, "check_tcp", lambda host, port, timeout=3: (True, "Port Open"),
        )
        # 同名 key 不得互相顶掉：服务名可以恰好等于机器 id
        probe_cache.check_service_health(
            {"name": "kr", "type": "docker", "port": 80, "machine": "nas"}
        )
        probe_cache.check_machine_probe("kr")
        assert probe_cache._cache["kr"]["status"] == "Healthy"
        assert probe_cache._machine_cache["kr"]["status"] == "up"

    def test_invalidate_all_clears_machine_cache(self, monkeypatch):
        monkeypatch.setattr(
            probe_cache, "check_tcp", lambda host, port, timeout=3: (True, "Port Open"),
        )
        probe_cache.check_machine_probe("kr")
        probe_cache.invalidate()
        assert probe_cache._machine_cache == {}

    def test_ttl_uses_machine_interval_not_service_interval(self, monkeypatch):
        """探针 TTL 走 CONSOLE_MACHINE_PROBE_INTERVAL（1800），不是服务健康的 600。"""
        monkeypatch.setattr(
            mc,
            "get_machine",
            lambda mid: {
                "id": "kr",
                "role": "node",
                "tailscale": "100.64.0.50",
                "probe": {"type": "tcp", "port": 62037},
                "public_host": "kr.example.test",
            },
        )
        calls = []
        monkeypatch.setattr(
            probe_cache,
            "check_tcp",
            lambda host, port, timeout=3: calls.append(1) or (True, "Port Open"),
        )
        probe_cache.check_machine_probe("kr", force=True)
        # 拨回 900s（> 服务 TTL 600，< 机器 TTL 1800）：不该重新探
        probe_cache._machine_cache["kr"]["ts"] -= 900
        probe_cache.check_machine_probe("kr")
        assert len(calls) == 1
        # 拨回超过机器 TTL：该重新探
        probe_cache._machine_cache["kr"]["ts"] -= (
            probe_cache.machine_probe_interval() - 900 + 1
        )
        probe_cache.check_machine_probe("kr")
        assert len(calls) == 2


# ---------------------------------------------------------------------------
# probe_all_machines — 半点调度的一轮全量探测
# ---------------------------------------------------------------------------

class TestProbeAllMachines:
    @pytest.fixture(autouse=True)
    def _no_history_writes(self, monkeypatch):
        recorded = []
        monkeypatch.setattr(
            probe_cache.node_probe, "record",
            lambda machine, status, latency_ms=None, ts=None, db_path=None: recorded.append(
                (machine, status)
            ),
        )
        return recorded

    @staticmethod
    def _fake_registry(monkeypatch):
        machines = {
            "kr": {
                "id": "kr",
                "role": "node",
                "tailscale": "100.64.0.50",
                "probe": {"type": "tcp", "port": 62037},
                "public_host": "kr.example.test",
            },
            "plain": {"id": "plain", "role": "node"},  # 无 probe 配置
        }
        monkeypatch.setattr(mc, "load_machines", lambda force=False: machines)
        monkeypatch.setattr(mc, "get_machine", lambda mid: machines.get(mid))

    def test_probes_only_machines_with_probe_config(
        self, monkeypatch, _no_history_writes
    ):
        self._fake_registry(monkeypatch)
        monkeypatch.setattr(
            probe_cache,
            "check_tcp",
            lambda host, port, timeout=3: (True, "Port Open"),
        )

        assert probe_cache.probe_all_machines() == 1
        assert _no_history_writes == [("kr", "up")]

    def test_force_round_writes_history_despite_fresh_cache(
        self, monkeypatch, _no_history_writes
    ):
        """每个节点都要有新样本：force 绕过 TTL，哪怕上一轮刚探过。"""
        self._fake_registry(monkeypatch)
        monkeypatch.setattr(
            probe_cache,
            "check_tcp",
            lambda host, port, timeout=3: (True, "Port Open"),
        )
        probe_cache.probe_all_machines()
        probe_cache.probe_all_machines()

        assert _no_history_writes == [("kr", "up"), ("kr", "up")]

    def test_down_probe_still_counts_and_records(self, monkeypatch, _no_history_writes):
        self._fake_registry(monkeypatch)
        monkeypatch.setattr(
            probe_cache,
            "check_tcp",
            lambda host, port, timeout=3: (False, "timed out"),
        )

        assert probe_cache.probe_all_machines() == 1
        assert _no_history_writes == [("kr", "down")]

    def test_single_machine_failure_does_not_break_round(
        self, monkeypatch, _no_history_writes
    ):
        machines = {
            "bad": {
                "id": "bad",
                "role": "node",
                "tailscale": "",
                "probe": {"type": "tcp", "port": 1},
                "public_host": "bad.example.test",
            },
            "kr": {
                "id": "kr",
                "role": "node",
                "tailscale": "100.64.0.50",
                "probe": {"type": "tcp", "port": 62037},
                "public_host": "kr.example.test",
            },
        }
        monkeypatch.setattr(mc, "load_machines", lambda force=False: machines)
        monkeypatch.setattr(mc, "get_machine", lambda mid: machines.get(mid))

        def flaky(host, port, timeout=3):
            if host.startswith("bad"):
                raise RuntimeError("dns boom")
            return True, "Port Open"

        monkeypatch.setattr(probe_cache, "check_tcp", flaky)

        assert probe_cache.probe_all_machines() == 1
        assert _no_history_writes == [("kr", "up")]


# ---------------------------------------------------------------------------
# 服务健康时序落库（T12 分支实验）：真实探测才记，缓存命中不记
# ---------------------------------------------------------------------------

class TestServiceHealthHistoryRecording:
    def test_real_probe_records_history(self, monkeypatch, tmp_path):
        recorded = []
        monkeypatch.setattr(
            "service_health_history.record",
            lambda name, status, ts=None, db_path=None: recorded.append((name, status)),
        )
        monkeypatch.setattr(
            probe_cache, "check_tcp", lambda ip, port, timeout=3: (True, "Port Open"),
        )
        probe_cache.check_service_health(
            {"name": "svc-h", "type": "docker", "port": 80, "machine": "nas"}
        )
        assert recorded == [("svc-h", "Healthy")]

    def test_unhealthy_probe_also_records(self, monkeypatch):
        recorded = []
        monkeypatch.setattr(
            "service_health_history.record",
            lambda name, status, ts=None, db_path=None: recorded.append((name, status)),
        )
        monkeypatch.setattr(
            probe_cache, "check_tcp", lambda ip, port, timeout=3: (False, "refused"),
        )
        probe_cache.check_service_health(
            {"name": "svc-h2", "type": "docker", "port": 80, "machine": "nas"}
        )
        assert recorded == [("svc-h2", "Unhealthy")]

    def test_unknown_status_does_not_record(self, monkeypatch):
        recorded = []
        monkeypatch.setattr(
            "service_health_history.record",
            lambda name, status, ts=None, db_path=None: recorded.append((name, status)),
        )
        probe_cache.check_service_health({"name": "svc-no-checker"})
        assert recorded == []

    def test_cache_hit_does_not_record(self, monkeypatch):
        recorded = []
        monkeypatch.setattr(
            "service_health_history.record",
            lambda name, status, ts=None, db_path=None: recorded.append((name, status)),
        )
        monkeypatch.setattr(
            probe_cache, "check_tcp",
            lambda ip, port, timeout=3: calls.append(1) or (True, "Port Open"),
        )
        calls = []
        probe_cache.check_service_health(
            {"name": "svc-h3", "type": "docker", "port": 80, "machine": "nas"}
        )
        probe_cache.check_service_health(
            {"name": "svc-h3", "type": "docker", "port": 80, "machine": "nas"}
        )
        assert recorded == [("svc-h3", "Healthy")]


# ---------------------------------------------------------------------------
# K2 事件接线：真实探测顺带推进生命周期，缓存命中 / Unknown 不推
# ---------------------------------------------------------------------------

class TestMonitorEventWiring:
    @pytest.fixture()
    def observed(self, monkeypatch):
        """把两个 observe_* 换成收集器（覆盖 _clean_cache 里的空桩）。"""
        import monitor_events

        seen = []
        monkeypatch.setattr(
            monitor_events, "observe_service",
            lambda name, status, machine="", detail="", ts=None, db_path=None: seen.append(
                ("service", name, status, machine)
            ),
        )
        monkeypatch.setattr(
            monitor_events, "observe_machine",
            lambda mid, status, label="", detail="", ts=None, db_path=None: seen.append(
                ("machine", mid, status, label)
            ),
        )
        return seen

    def test_service_probe_feeds_event(self, monkeypatch, observed):
        monkeypatch.setattr(
            probe_cache, "check_tcp", lambda ip, port, timeout=3: (False, "refused")
        )
        probe_cache.check_service_health(
            {"name": "svc-e", "type": "docker", "port": 80, "machine": "nas"}
        )
        assert observed == [("service", "svc-e", "Unhealthy", "nas")]

    def test_healthy_service_also_feeds_event(self, monkeypatch, observed):
        """恢复也要报：事件靠它关闭，只报失败会让事件永远挂着。"""
        monkeypatch.setattr(
            probe_cache, "check_tcp", lambda ip, port, timeout=3: (True, "Port Open")
        )
        probe_cache.check_service_health(
            {"name": "svc-ok", "type": "docker", "port": 80, "machine": "nas"}
        )
        assert observed == [("service", "svc-ok", "Healthy", "nas")]

    def test_unknown_service_does_not_feed_event(self, observed):
        probe_cache.check_service_health({"name": "svc-no-checker"})
        assert observed == []

    def test_disabled_probe_does_not_feed_event(self, observed):
        """台账显式 health_probe:false 的服务不当故障，但会补一次 Healthy 观测
        回收可能存在的悬挂活动事件（关闭探测后旧事件无自愈路径）。"""
        probe_cache.check_service_health({"name": "svc-off", "health_probe": False})
        assert observed == [("service", "svc-off", "Healthy", "")]

    def test_service_cache_hit_does_not_feed_event(self, monkeypatch, observed):
        monkeypatch.setattr(
            probe_cache, "check_tcp", lambda ip, port, timeout=3: (True, "Port Open")
        )
        svc = {"name": "svc-c", "type": "docker", "port": 80, "machine": "nas"}
        probe_cache.check_service_health(svc)
        probe_cache.check_service_health(svc)
        assert len(observed) == 1

    def test_machine_probe_feeds_event_with_label(self, monkeypatch, observed):
        monkeypatch.setattr(
            mc, "get_machine",
            lambda mid: {
                "id": "kr", "role": "node", "label": "光帆 KR",
                "probe": {"type": "tcp", "port": 62037},
            },
        )
        monkeypatch.setattr(mc, "tailscale_ip", lambda mid: "100.64.0.9")
        monkeypatch.setattr(
            probe_cache, "check_tcp", lambda host, port, timeout=3: (False, "timeout")
        )
        monkeypatch.setattr(probe_cache.node_probe, "record", lambda *a, **kw: True)
        probe_cache.check_machine_probe("kr")
        assert observed == [("machine", "kr", "down", "光帆 KR")]

    def test_machine_without_probe_config_feeds_nothing(self, monkeypatch, observed):
        monkeypatch.setattr(mc, "get_machine", lambda mid: {"id": "us", "role": "server"})
        assert probe_cache.check_machine_probe("us")[0] == "unknown"
        assert observed == []

    def test_event_failure_does_not_break_probe(self, monkeypatch):
        """事件写不进去是加分项失败，不该让探测主线炸。"""
        import monitor_events

        def boom(*_a, **_k):
            raise RuntimeError("db locked")

        monkeypatch.setattr(monitor_events, "observe_service", boom)
        monkeypatch.setattr(
            probe_cache, "check_tcp", lambda ip, port, timeout=3: (True, "Port Open")
        )
        status, _msg = probe_cache.check_service_health(
            {"name": "svc-boom", "type": "docker", "port": 80, "machine": "nas"}
        )
        assert status == "Healthy"


# ---------------------------------------------------------------------------
# Track L1：health_probe 三态（false / 对象语义检查 / 缺省推断）
# ---------------------------------------------------------------------------

class TestSemanticHealthProbe:
    def test_object_probe_beats_port_inference(self, monkeypatch):
        """配了语义检查就不再打 TCP —— 端口通不等于业务好，这是 L1 的全部理由。"""
        monkeypatch.setattr(
            probe_cache, "check_tcp",
            lambda *a, **kw: pytest.fail("配了 health_probe 对象就不该再打 TCP"),
        )
        seen = {}

        def fake_semantic(url, expected_status=None, max_latency_ms=None, body_contains=None, timeout=4):
            seen.update(url=url, status=expected_status, latency=max_latency_ms, body=body_contains)
            return False, "HTTP 500（期望 200）"

        monkeypatch.setattr(probe_cache, "check_http_semantic", fake_semantic)
        status, msg = probe_cache.check_service_health(
            {
                "name": "svc-sem",
                "type": "docker",
                "port": 8080,
                "machine": "nas",
                "access": {"domain": "https://svc.example.test"},
                "health_probe": {
                    "type": "http",
                    "expected_status": 200,
                    "max_latency_ms": 1500,
                    "body_contains": '"ok"',
                },
            }
        )
        assert status == "Unhealthy"
        assert "HTTP 500" in msg
        assert seen == {
            "url": "https://svc.example.test",
            "status": 200,
            "latency": 1500,
            "body": '"ok"',
        }

    def test_target_selects_access_entry(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            probe_cache, "check_http_semantic",
            lambda url, **kw: seen.update(url=url) or (True, "HTTP 200 3.1ms"),
        )
        status, _msg = probe_cache.check_service_health(
            {
                "name": "svc-ts",
                "access": {"domain": "https://d.test", "tailscale": "http://100.64.0.5:9000"},
                "health_probe": {"type": "http", "target": "tailscale"},
            }
        )
        assert status == "Healthy"
        assert seen["url"] == "http://100.64.0.5:9000"

    def test_missing_target_address_is_unknown_not_unhealthy(self, monkeypatch):
        """配置缺口不是服务故障，不能报成红的。"""
        monkeypatch.setattr(
            probe_cache, "check_http_semantic",
            lambda *a, **kw: pytest.fail("没有地址就不该发探测"),
        )
        status, msg = probe_cache.check_service_health(
            {"name": "svc-noaddr", "access": {}, "health_probe": {"type": "http"}}
        )
        assert status == "Unknown"
        assert "没有地址" in msg

    def test_broken_object_falls_back_to_default_inference(self, monkeypatch):
        """手工把 YAML 写坏了 → 退回默认推断，而不是把它显示成故障。"""
        monkeypatch.setattr(
            probe_cache, "check_tcp", lambda ip, port, timeout=3: (True, "Port Open")
        )
        monkeypatch.setattr(
            probe_cache, "check_http_semantic",
            lambda *a, **kw: pytest.fail("坏配置不该走语义分支"),
        )
        status, msg = probe_cache.check_service_health(
            {
                "name": "svc-broken",
                "type": "docker",
                "port": 80,
                "machine": "nas",
                "health_probe": {"type": "http", "headers": {"A": "b"}},
            }
        )
        assert status == "Healthy"
        assert msg == "Port Open"

    def test_false_still_wins_over_everything(self):
        """三态优先级：false 关闭 > 对象语义 > 默认推断。"""
        status, msg = probe_cache.check_service_health(
            {"name": "svc-off2", "health_probe": False, "port": 80, "type": "docker"}
        )
        assert status == "Unknown"
        assert "disabled" in msg

    def test_default_inference_unchanged_without_health_probe(self, monkeypatch):
        monkeypatch.setattr(
            probe_cache, "check_tcp", lambda ip, port, timeout=3: (True, "Port Open")
        )
        status, _msg = probe_cache.check_service_health(
            {"name": "svc-plain", "type": "docker", "port": 80, "machine": "nas"}
        )
        assert status == "Healthy"


# ---------------------------------------------------------------------------
# 家宽公网探测（public_probe）：独立缓存、独立时序键、两道防伪闸门
# ---------------------------------------------------------------------------

class TestPublicProbeInterval:
    """公网采样默认 300s，与机器探针的 1800s 解耦（封堵常是几十分钟级的间歇）。"""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.delenv("CONSOLE_PUBLIC_PROBE_INTERVAL", raising=False)

    def test_default_is_300(self):
        assert probe_cache.public_probe_interval() == 300

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CONSOLE_PUBLIC_PROBE_INTERVAL", "600")
        assert probe_cache.public_probe_interval() == 600

    def test_floor_is_60(self, monkeypatch):
        monkeypatch.setenv("CONSOLE_PUBLIC_PROBE_INTERVAL", "5")
        assert probe_cache.public_probe_interval() == 60

    def test_invalid_value_falls_back_to_300(self, monkeypatch):
        monkeypatch.setenv("CONSOLE_PUBLIC_PROBE_INTERVAL", "abc")
        assert probe_cache.public_probe_interval() == 300

    def test_machine_interval_unaffected(self, monkeypatch):
        monkeypatch.setenv("CONSOLE_PUBLIC_PROBE_INTERVAL", "120")
        assert probe_cache.machine_probe_interval() == 1800


class TestCheckMachinePublicProbe:
    """用仓库台账里的 kr（public_host kr.sunxc.eu.cc、public_probe 62037）。

    解析与 connect 全打桩：``_resolve_ipv4`` 固定回 TEST-NET 地址；``check_tcp``
    按剧本回放结果，并推进假时钟模拟握手耗时（不真睡）。
    """

    ADDR = "203.0.113.7"

    @pytest.fixture(autouse=True)
    def _stubs(self, monkeypatch):
        import monitor_events

        monkeypatch.delenv("CONSOLE_PUBLIC_PROBE_INTERVAL", raising=False)
        recorded, observed, resolved = [], [], []
        monkeypatch.setattr(
            probe_cache.node_probe, "record",
            lambda machine, status, latency_ms=None, ts=None, db_path=None: recorded.append(
                (machine, status, latency_ms)
            ),
        )
        monkeypatch.setattr(
            monitor_events, "observe_public",
            lambda machine_id, status, **kw: observed.append((machine_id, status, kw)),
        )
        monkeypatch.setattr(
            probe_cache, "_resolve_ipv4",
            lambda host: resolved.append(host) or self.ADDR,
        )
        return {"recorded": recorded, "observed": observed, "resolved": resolved}

    @staticmethod
    def _script(monkeypatch, *steps):
        """按顺序回放 connect：每步 (ok, detail, 耗时秒)。返回实际 connect 调用列表。"""
        clock = {"now": 1_000_000.0}
        monkeypatch.setattr(
            probe_cache, "time",
            type("FakeTime", (), {
                "perf_counter": staticmethod(lambda: clock["now"]),
                "time": staticmethod(lambda: clock["now"]),
            }),
        )
        queue, calls = list(steps), []

        def fake_check_tcp(ip, port, timeout=3):
            if not queue:
                pytest.fail("connect 次数超出剧本")
            ok, detail, seconds = queue.pop(0)
            calls.append((ip, port, timeout))
            clock["now"] += seconds
            return ok, detail

        monkeypatch.setattr(probe_cache, "check_tcp", fake_check_tcp)
        return calls

    def test_up_dials_resolved_ipv4_on_public_port(self, monkeypatch, _stubs):
        calls = self._script(monkeypatch, (True, "Port Open", 0.1805))
        status, msg, ts = probe_cache.check_machine_public_probe("kr")
        assert status == "up"
        assert _stubs["resolved"] == ["kr.sunxc.eu.cc"]
        assert calls == [(self.ADDR, 62037, probe_cache.PUBLIC_PROBE_TIMEOUT)]
        # msg 写域名不写解析出来的 IP
        assert msg == "tcp kr.sunxc.eu.cc:62037 ok 180.5ms"
        assert self.ADDR not in msg
        assert ts and ts.endswith("Z")

    def test_up_records_history_under_public_key_and_feeds_event(self, monkeypatch, _stubs):
        self._script(monkeypatch, (True, "Port Open", 0.17))
        probe_cache.check_machine_public_probe("kr")
        assert _stubs["recorded"] == [("kr:public", "up", 170.0)]
        (machine_id, status, kw), = _stubs["observed"]
        assert (machine_id, status) == ("kr", "up")
        assert kw["label"] == "光帆 KR"
        assert kw["detail"] == "tcp kr.sunxc.eu.cc:62037 ok 170.0ms"

    def test_retries_once_before_down(self, monkeypatch, _stubs):
        """首个 SYN 丢了不该直接判 down：第二次连上就是 up。"""
        calls = self._script(
            monkeypatch, (False, "timed out", 5.0), (True, "Port Open", 0.2),
        )
        status, msg, _ts = probe_cache.check_machine_public_probe("kr")
        assert status == "up"
        assert len(calls) == probe_cache.PUBLIC_PROBE_ATTEMPTS == 2
        assert msg == "tcp kr.sunxc.eu.cc:62037 ok 200.0ms"

    def test_two_failures_are_down_with_reason(self, monkeypatch, _stubs):
        self._script(monkeypatch, (False, "timed out", 5.0), (False, "timed out", 5.0))
        status, msg, _ts = probe_cache.check_machine_public_probe("kr")
        assert status == "down"
        assert msg == "tcp kr.sunxc.eu.cc:62037 timed out"
        assert _stubs["recorded"] == [("kr:public", "down", None)]
        assert [(m, s) for m, s, _kw in _stubs["observed"]] == [("kr", "down")]

    def test_fake_ip_resolution_is_unknown_without_connecting(self, monkeypatch, _stubs):
        """域名没进 fake-ip-filter：198.18/15 必进 TUN，连过去只会拿到本地代答。"""
        monkeypatch.setattr(probe_cache, "_resolve_ipv4", lambda host: "198.18.0.9")
        calls = self._script(monkeypatch)
        status, msg, _ts = probe_cache.check_machine_public_probe("kr")
        assert status == "unknown"
        assert "fake-ip" in msg
        assert calls == []
        assert _stubs["recorded"] == []
        assert _stubs["observed"] == []

    def test_fake_ip_guard_covers_whole_slash_15(self, monkeypatch, _stubs):
        monkeypatch.setattr(probe_cache, "_resolve_ipv4", lambda host: "198.19.255.254")
        self._script(monkeypatch)
        assert probe_cache.check_machine_public_probe("kr")[0] == "unknown"

    def test_implausibly_fast_handshake_is_unknown(self, monkeypatch, _stubs):
        """NAS 豁免失效时 TUN 本地代答（实测 0.1–5ms）：不能记成 up。"""
        self._script(monkeypatch, (True, "Port Open", 0.002))
        status, msg, _ts = probe_cache.check_machine_public_probe("kr")
        assert status == "unknown"
        assert "疑似本机代理代答" in msg
        assert _stubs["recorded"] == []
        assert _stubs["observed"] == []

    def test_plausibility_floor_is_inclusive_at_10ms(self, monkeypatch, _stubs):
        self._script(monkeypatch, (True, "Port Open", 0.010))
        assert probe_cache.check_machine_public_probe("kr")[0] == "up"

    def test_resolve_failure_is_unknown(self, monkeypatch, _stubs):
        import socket

        def gaierror(host):
            raise socket.gaierror(8, "nodename nor servname provided")

        monkeypatch.setattr(probe_cache, "_resolve_ipv4", gaierror)
        calls = self._script(monkeypatch)
        status, msg, _ts = probe_cache.check_machine_public_probe("kr")
        assert status == "unknown"
        assert "解析失败" in msg
        assert calls == []
        assert _stubs["recorded"] == []

    def test_machine_without_public_probe_is_unknown(self, monkeypatch, _stubs):
        """nas 没配 public_probe（也没有 public_host）：不解析、不连接。"""
        calls = self._script(monkeypatch)
        assert probe_cache.check_machine_public_probe("nas") == ("unknown", "", None)
        assert probe_cache.check_machine_public_probe("router") == ("unknown", "", None)
        assert calls == []
        assert _stubs["resolved"] == []

    def test_cache_hit_and_force_bypass(self, monkeypatch, _stubs):
        calls = self._script(
            monkeypatch, (True, "Port Open", 0.17), (True, "Port Open", 0.18),
        )
        first = probe_cache.check_machine_public_probe("kr")
        assert probe_cache.check_machine_public_probe("kr") == first
        assert len(calls) == 1
        probe_cache.check_machine_public_probe("kr", force=True)
        assert len(calls) == 2
        # 命中缓存的那次不落时序
        assert len(_stubs["recorded"]) == 2

    def test_ttl_uses_public_interval(self, monkeypatch, _stubs):
        calls = self._script(
            monkeypatch, (True, "Port Open", 0.17), (True, "Port Open", 0.17),
        )
        probe_cache.check_machine_public_probe("kr")
        probe_cache._public_cache["kr"]["ts"] -= probe_cache.public_probe_interval() - 1
        probe_cache.check_machine_public_probe("kr")
        assert len(calls) == 1
        probe_cache._public_cache["kr"]["ts"] -= 2
        probe_cache.check_machine_public_probe("kr")
        assert len(calls) == 2

    def test_public_cache_independent_from_machine_probe(self, monkeypatch, _stubs):
        """同一台机器的两种探测互不顶掉；node 动作的 invalidate_machine 不清公网缓存。"""
        monkeypatch.setattr(mc, "tailscale_ip", lambda mid: "100.64.0.50")
        self._script(
            monkeypatch, (True, "Port Open", 0.03), (False, "timed out", 5.0),
            (False, "timed out", 5.0),
        )
        assert probe_cache.check_machine_probe("kr")[0] == "up"
        assert probe_cache.check_machine_public_probe("kr")[0] == "down"
        assert probe_cache._machine_cache["kr"]["status"] == "up"
        assert probe_cache._public_cache["kr"]["status"] == "down"
        probe_cache.invalidate_machine("kr")
        assert "kr" in probe_cache._public_cache
        probe_cache.invalidate()
        assert probe_cache._public_cache == {}

    def test_machine_probe_path_is_unchanged(self, monkeypatch, _stubs):
        """回归：配了 public_probe 的 us，机器探活照旧打 Tailscale IP 的 probe 端口。"""
        monkeypatch.setattr(mc, "tailscale_ip", lambda mid: "100.64.0.70")
        calls = self._script(monkeypatch, (True, "Port Open", 0.17))
        status, msg, _ts = probe_cache.check_machine_probe("us")
        assert status == "up"
        assert calls == [("100.64.0.70", 22, 3)]
        assert msg.startswith("tcp 100.64.0.70:22 ok ")
        assert _stubs["resolved"] == []
        assert _stubs["recorded"] == [("us", "up", 170.0)]
        assert _stubs["observed"] == []

    def test_history_and_event_failures_do_not_break_probe(self, monkeypatch, _stubs):
        import monitor_events

        def boom(*a, **kw):
            raise RuntimeError("db down")

        monkeypatch.setattr(probe_cache.node_probe, "record", boom)
        monkeypatch.setattr(monitor_events, "observe_public", boom)
        self._script(monkeypatch, (True, "Port Open", 0.17))
        assert probe_cache.check_machine_public_probe("kr")[0] == "up"

    def test_peek_never_probes(self, monkeypatch, _stubs):
        calls = self._script(monkeypatch, (True, "Port Open", 0.17))
        assert probe_cache.peek_machine_public("kr") == ("unknown", "", None)
        assert calls == []
        probe_cache.check_machine_public_probe("kr")
        status, msg, ts = probe_cache.peek_machine_public("KR")
        assert (status, msg) == ("up", "tcp kr.sunxc.eu.cc:62037 ok 170.0ms")
        assert ts and ts.endswith("Z")
        assert len(calls) == 1

    def test_probe_all_public_covers_configured_machines_only(self, monkeypatch, _stubs):
        """仓库台账里配了 public_probe 的是 us/kr/sg；nas/txy/aly 不探。"""
        self._script(
            monkeypatch,
            (True, "Port Open", 0.17), (False, "timed out", 5.0), (False, "timed out", 5.0),
            (True, "Port Open", 0.09),
        )
        assert probe_cache.probe_all_public() == 3
        assert _stubs["resolved"] == ["us.sunxc.eu.cc", "kr.sunxc.eu.cc", "sg.sunxc.eu.cc"]
        assert [(m, s) for m, s, _l in _stubs["recorded"]] == [
            ("us:public", "up"), ("kr:public", "down"), ("sg:public", "up"),
        ]

    def test_probe_all_public_reclaims_events_of_unconfigured_machines(
        self, monkeypatch, _stubs
    ):
        """没配 public_probe 的机器不解析、不连接，只补一次 up 观测回收残留事件。"""
        self._script(monkeypatch, *([(True, "Port Open", 0.17)] * 3))
        probe_cache.probe_all_public()
        reclaimed = [
            (machine_id, kw.get("detail"))
            for machine_id, status, kw in _stubs["observed"]
            if status == "up" and machine_id in ("nas", "txy", "aly")
        ]
        assert reclaimed == [
            ("nas", "家宽公网探测已从台账撤掉"),
            ("txy", "家宽公网探测已从台账撤掉"),
            ("aly", "家宽公网探测已从台账撤掉"),
        ]
        assert _stubs["resolved"] == ["us.sunxc.eu.cc", "kr.sunxc.eu.cc", "sg.sunxc.eu.cc"]

    def test_probe_all_public_forces_fresh_samples(self, monkeypatch, _stubs):
        self._script(monkeypatch, *([(True, "Port Open", 0.17)] * 6))
        probe_cache.probe_all_public()
        probe_cache.probe_all_public()
        assert len(_stubs["recorded"]) == 6

    def test_probe_all_public_does_not_count_unknown(self, monkeypatch, _stubs):
        monkeypatch.setattr(probe_cache, "_resolve_ipv4", lambda host: "198.18.0.9")
        self._script(monkeypatch)
        assert probe_cache.probe_all_public() == 0

    def test_probe_all_public_single_failure_does_not_break_round(self, monkeypatch, _stubs):
        real = probe_cache.check_machine_public_probe

        def flaky(machine_id, force=False, db_path=None):
            if machine_id == "kr":
                raise RuntimeError("boom")
            return real(machine_id, force=force, db_path=db_path)

        monkeypatch.setattr(probe_cache, "check_machine_public_probe", flaky)
        self._script(monkeypatch, (True, "Port Open", 0.17), (True, "Port Open", 0.09))
        assert probe_cache.probe_all_public() == 2


class TestPublicHistoryKey:
    def test_key_shape(self):
        assert probe_cache.public_history_key("KR ") == "kr:public"

    def test_key_cannot_collide_with_a_machine_id(self):
        """机器 id 过 validate_identifier，不允许冒号：公网键永远撞不上真实机器。"""
        from validation import validate_identifier

        with pytest.raises(ValueError):
            validate_identifier(probe_cache.public_history_key("kr"), "机器 id")


class TestResolveIpv4:
    def test_numeric_ipv4_passes_through_without_dns(self):
        assert probe_cache._resolve_ipv4("192.0.2.10") == "192.0.2.10"
