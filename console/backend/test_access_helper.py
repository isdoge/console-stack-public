# -*- coding: utf-8 -*-
"""Regression tests for access_helper.py — 访问入口连通性探测。

遵循 checker 测试隔离规范：不触网，check_http 全部 monkeypatch。
"""
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import access_helper  # noqa: E402

# ---------------------------------------------------------------------------
# probe_url
# ---------------------------------------------------------------------------

class TestProbeUrl:
    @pytest.mark.parametrize("url", [None, "", "   "])
    def test_blank_url_returns_unconfigured(self, url):
        ok, msg = access_helper.probe_url(url)
        assert ok is None
        assert msg == "未配置"

    def test_bare_host_gets_http_prefix(self, monkeypatch):
        seen = {}

        def fake_check_http(url, timeout=4, expected_code=200):
            seen["url"] = url
            seen["timeout"] = timeout
            return True, "Status 200"

        monkeypatch.setattr(access_helper, "check_http", fake_check_http)
        ok, msg = access_helper.probe_url("192.168.50.100:8081")
        assert ok is True
        assert msg == "Status 200"
        assert seen["url"] == "http://192.168.50.100:8081"
        assert seen["timeout"] == 5

    @pytest.mark.parametrize("url", [
        "http://example.internal:8080",
        "https://example.internal",
    ])
    def test_scheme_preserved(self, monkeypatch, url):
        seen = {}
        monkeypatch.setattr(
            access_helper, "check_http",
            lambda u, timeout=4, expected_code=200: seen.update(url=u) or (True, "ok"),
        )
        access_helper.probe_url(url)
        assert seen["url"] == url

    def test_whitespace_stripped_before_probe(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            access_helper, "check_http",
            lambda u, timeout=4, expected_code=200: seen.update(url=u) or (True, "ok"),
        )
        access_helper.probe_url("  https://example.internal  ")
        assert seen["url"] == "https://example.internal"

    def test_failure_propagated(self, monkeypatch):
        monkeypatch.setattr(
            access_helper, "check_http",
            lambda u, timeout=4, expected_code=200: (False, "Connection refused"),
        )
        ok, msg = access_helper.probe_url("http://example.internal")
        assert ok is False
        assert msg == "Connection refused"

    def test_authenticated_service_counts_as_reachable(self, monkeypatch):
        """带鉴权的入口（如 magpie 无 key 固定 401）按连通性判可达。

        回归：probe_url 曾用默认 expected_code=200，把「活着但要求登录」的
        服务错存成 access.probe=false，服务表 H/T/I 列长期显示探测不通。
        """
        seen = {}

        def fake_check_http(url, timeout=4, expected_code=200):
            seen["expected_code"] = expected_code
            return True, "HTTP 401"

        monkeypatch.setattr(access_helper, "check_http", fake_check_http)
        ok, msg = access_helper.probe_url("http://100.64.0.70:3430")
        assert ok is True
        assert msg == "HTTP 401"
        # 连通性口径：不挑状态码（None），而不是把期望硬编码成某个具体码
        assert seen["expected_code"] is None


# ---------------------------------------------------------------------------
# probe_access
# ---------------------------------------------------------------------------

class TestProbeAccess:
    def test_all_blank_entries_marked_unconfigured(self):
        result = access_helper.probe_access()
        assert set(result) == {"home", "tailscale", "domain"}
        for entry in result.values():
            assert entry == {"configured": False, "ok": None, "msg": "未配置"}

    def test_mixed_entries(self, monkeypatch):
        def fake_check_http(url, timeout=4, expected_code=200):
            if "100.125" in url:
                return True, "Status 200"
            return False, "timeout"

        monkeypatch.setattr(access_helper, "check_http", fake_check_http)
        result = access_helper.probe_access(
            home="", tailscale="100.64.0.10:8081", domain="https://bad.internal",
        )
        assert result["home"]["configured"] is False
        assert result["tailscale"] == {"configured": True, "ok": True, "msg": "Status 200"}
        assert result["domain"] == {"configured": True, "ok": False, "msg": "timeout"}

    def test_whitespace_only_value_treated_as_unconfigured(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            access_helper, "check_http",
            lambda u, timeout=4, expected_code=200: called.append(u) or (True, "ok"),
        )
        result = access_helper.probe_access(home="   ")
        assert result["home"]["configured"] is False
        assert called == []
