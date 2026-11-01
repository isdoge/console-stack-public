# -*- coding: utf-8 -*-
"""Regression tests for checker.py — TCP/HTTP/SSL probes, plus SSH timeout exit code.

网络探测全部走本地回环或 monkeypatch，不依赖外网。
"""
import http.server
import pathlib
import socket
import sys
import threading
import time
from datetime import datetime, timedelta

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import checker  # noqa: E402
import service_manager as sm  # noqa: E402
from checker import check_http, check_tcp, get_ssl_expiry_days  # noqa: E402


def _free_port():
    """取一个刚刚释放的本地端口（用于"不可达"场景）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# check_tcp
# ---------------------------------------------------------------------------

class TestCheckTcp:
    def test_open_port_returns_true(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            ok, msg = check_tcp("127.0.0.1", port, timeout=2)
        finally:
            srv.close()
        assert ok is True
        assert msg == "Port Open"

    def test_closed_port_returns_false(self):
        ok, msg = check_tcp("127.0.0.1", _free_port(), timeout=1)
        assert ok is False
        assert isinstance(msg, str) and msg

    def test_port_accepts_string(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            ok, _ = check_tcp("127.0.0.1", str(port), timeout=2)
        finally:
            srv.close()
        assert ok is True


# ---------------------------------------------------------------------------
# check_http
# ---------------------------------------------------------------------------

class _StatusHandler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(self.status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # 静默测试输出
        pass


@pytest.fixture
def http_server():
    """启动本地 HTTP server，yield (base_url, handler_cls) 以便切换状态码。"""
    server = http.server.HTTPServer(("127.0.0.1", 0), _StatusHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", _StatusHandler
    finally:
        server.shutdown()
        server.server_close()
        _StatusHandler.status = 200


class TestCheckHttp:
    def test_200_returns_true(self, http_server):
        url, handler = http_server
        handler.status = 200
        ok, msg = check_http(url, timeout=3)
        assert ok is True
        assert msg == "HTTP 200"

    def test_non_200_returns_false(self, http_server):
        url, handler = http_server
        handler.status = 503
        ok, msg = check_http(url, timeout=3)
        assert ok is False
        assert "503" in msg

    def test_expected_code_match_returns_true(self, http_server):
        # 例如某些面板未登录返回 401，配置 expected_code 后应判定为可用
        url, handler = http_server
        handler.status = 401
        ok, msg = check_http(url, timeout=3, expected_code=401)
        assert ok is True
        assert msg == "HTTP 401"

    def test_connection_refused_returns_false(self):
        ok, msg = check_http(f"http://127.0.0.1:{_free_port()}", timeout=1)
        assert ok is False
        assert isinstance(msg, str) and msg

    def test_expected_code_none_passes_any_status(self, http_server):
        # 入口连通性口径：带鉴权的服务对匿名探测回 401 也证明活着
        url, handler = http_server
        handler.status = 401
        ok, msg = check_http(url, timeout=3, expected_code=None)
        assert ok is True
        assert msg == "HTTP 401"

    def test_expected_code_none_passes_server_error(self, http_server):
        # 5xx 也是「有 HTTP 响应」：入口探测回答的是地址活不活，不是业务对不对
        url, handler = http_server
        handler.status = 503
        ok, msg = check_http(url, timeout=3, expected_code=None)
        assert ok is True
        assert msg == "HTTP 503"


# ---------------------------------------------------------------------------
# get_ssl_expiry_days
# ---------------------------------------------------------------------------

class _FakeConn:
    """伪造 TLS 连接：记录 connect 参数，返回预设证书信息。"""

    def __init__(self, cert_dict):
        self._cert_dict = cert_dict
        self.connected_to = None

    def settimeout(self, t):
        pass

    def connect(self, addr):
        self.connected_to = addr

    def getpeercert(self, binary_form=False):
        if binary_form:
            return b"\x30\x03\x02\x01\x00"  # 任意 DER 占位
        return self._cert_dict


class TestGetSslExpiryDays:
    def _patch_tls(self, monkeypatch, not_after, connections):
        cert_dict = {"notAfter": not_after}

        class _FakeContext:
            check_hostname = True
            verify_mode = None

            def wrap_socket(self, sock, server_hostname=None):
                conn = _FakeConn(cert_dict)
                connections.append((server_hostname, conn))
                return conn

        monkeypatch.setattr(checker.ssl, "create_default_context", lambda: _FakeContext())
        monkeypatch.setattr(checker.ssl, "DER_cert_to_PEM_cert", lambda der: "PEM")

    def test_days_remaining_calculation(self, monkeypatch):
        connections = []
        expiry = datetime.utcnow() + timedelta(days=30)
        self._patch_tls(monkeypatch, expiry.strftime("%b %d %H:%M:%S %Y GMT"), connections)

        ok, days = get_ssl_expiry_days("example.test")
        assert ok is True
        # strptime 截断了微秒，剩余天数应为 29 或 30
        assert days in (29, 30)

    def test_expired_cert_returns_negative_days(self, monkeypatch):
        connections = []
        expiry = datetime.utcnow() - timedelta(days=10)
        self._patch_tls(monkeypatch, expiry.strftime("%b %d %H:%M:%S %Y GMT"), connections)

        ok, days = get_ssl_expiry_days("example.test")
        assert ok is True
        assert days < 0

    def test_url_prefix_and_port_are_parsed(self, monkeypatch):
        connections = []
        expiry = datetime.utcnow() + timedelta(days=90)
        self._patch_tls(monkeypatch, expiry.strftime("%b %d %H:%M:%S %Y GMT"), connections)

        ok, _ = get_ssl_expiry_days("https://example.test:8443/some/path")
        assert ok is True
        hostname, conn = connections[0]
        assert hostname == "example.test"
        assert conn.connected_to == ("example.test", 8443)

    def test_unreachable_host_returns_false(self):
        ok, msg = get_ssl_expiry_days("127.0.0.1", port=_free_port(), timeout=1)
        assert ok is False
        assert isinstance(msg, str) and msg


# ---------------------------------------------------------------------------
# SSH 探活/远程命令 — 超时必须返回非零退出码
# ---------------------------------------------------------------------------

class TestSshTimeoutExitCode:
    def test_run_timeout_returns_nonzero(self):
        # 模拟 SSH 挂死：命令超过 timeout 后必须返回非零（124），不能无限阻塞
        rc, out, err = sm._run(
            [sys.executable, "-c", "import time; time.sleep(10)"], timeout=1
        )
        assert rc == 124
        assert err == "timeout"

    def test_run_missing_binary_returns_nonzero(self):
        rc, _, err = sm._run(["definitely-not-a-real-binary-xyz"], timeout=5)
        assert rc != 0
        assert err

    def test_ssh_opts_enforce_connect_timeout(self):
        # SSH 命令行自带 ConnectTimeout/BatchMode，保证连不上时 ssh 自身非零退出而非挂起
        opts = " ".join(sm.SSH_OPTS)
        assert "ConnectTimeout" in opts
        assert "BatchMode=yes" in opts


# ---------------------------------------------------------------------------
# Track L1：有限语义检查（状态码 / 耗时 / 固定字符串）
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, code, body=b"", delay=0.0):
        self._code = code
        self._body = body
        self._delay = delay

    def getcode(self):
        return self._code

    def read(self, size=None):
        return self._body[:size] if size else self._body

    def __enter__(self):
        if self._delay:
            time.sleep(self._delay)
        return self

    def __exit__(self, *_exc):
        return False


class TestCheckHttpSemantic:
    def _patch(self, monkeypatch, response=None, error=None):
        def fake_urlopen(_req, timeout=None, context=None):
            if error is not None:
                raise error
            return response

        monkeypatch.setattr(checker.urllib.request, "urlopen", fake_urlopen)

    def test_status_match_passes(self, monkeypatch):
        self._patch(monkeypatch, _FakeResponse(200))
        ok, msg = checker.check_http_semantic("http://x.test", expected_status=200)
        assert ok is True
        assert msg.startswith("HTTP 200")

    def test_status_mismatch_is_the_whole_point(self, monkeypatch):
        """端口通但业务坏：TCP 探测会说 Healthy，语义检查必须说 Unhealthy。"""
        self._patch(monkeypatch, _FakeResponse(500))
        ok, msg = checker.check_http_semantic("http://x.test", expected_status=200)
        assert ok is False
        assert "HTTP 500" in msg and "期望 200" in msg

    def test_http_error_status_can_be_the_expected_one(self, monkeypatch):
        """期望值就是 404 时，urllib 抛的 HTTPError 也算通过。"""
        err = checker.urllib.error.HTTPError("http://x.test", 404, "NF", {}, None)
        self._patch(monkeypatch, error=err)
        ok, _msg = checker.check_http_semantic("http://x.test", expected_status=404)
        assert ok is True

    def test_expected_status_none_skips_status_check(self, monkeypatch):
        self._patch(monkeypatch, _FakeResponse(503))
        ok, _msg = checker.check_http_semantic("http://x.test", expected_status=None)
        assert ok is True

    def test_latency_over_limit_fails(self, monkeypatch):
        self._patch(monkeypatch, _FakeResponse(200, delay=0.05))
        ok, msg = checker.check_http_semantic(
            "http://x.test", expected_status=200, max_latency_ms=1
        )
        assert ok is False
        assert "超过上限" in msg

    def test_latency_within_limit_passes(self, monkeypatch):
        self._patch(monkeypatch, _FakeResponse(200))
        ok, _msg = checker.check_http_semantic(
            "http://x.test", expected_status=200, max_latency_ms=60000
        )
        assert ok is True

    def test_body_contains_hit(self, monkeypatch):
        self._patch(monkeypatch, _FakeResponse(200, b'{"status":"ok","x":1}'))
        ok, _msg = checker.check_http_semantic(
            "http://x.test", expected_status=200, body_contains='"status":"ok"'
        )
        assert ok is True

    def test_body_contains_miss_does_not_echo_body(self, monkeypatch):
        """失败原因要落进事件表长期保存，不能把业务数据带进去。"""
        self._patch(monkeypatch, _FakeResponse(200, b'{"secret":"leak-me","status":"bad"}'))
        ok, msg = checker.check_http_semantic(
            "http://x.test", expected_status=200, body_contains='"status":"ok"'
        )
        assert ok is False
        assert "leak-me" not in msg
        assert "不含预期内容" in msg

    def test_body_not_read_when_not_configured(self, monkeypatch):
        """没配 body_contains 就不读 body（大响应不进内存）。"""
        reads = []

        class _Watched(_FakeResponse):
            def read(self, size=None):
                reads.append(size)
                return b""

        self._patch(monkeypatch, _Watched(200))
        checker.check_http_semantic("http://x.test", expected_status=200)
        assert reads == []

    def test_body_read_is_capped(self, monkeypatch):
        reads = []

        class _Watched(_FakeResponse):
            def read(self, size=None):
                reads.append(size)
                return b"x"

        self._patch(monkeypatch, _Watched(200))
        checker.check_http_semantic("http://x.test", body_contains="x")
        assert reads == [checker.SEMANTIC_BODY_LIMIT]

    def test_redirects_are_followed_so_3xx_expectations_cannot_match(self, monkeypatch):
        """已知边界：urllib 跟随重定向，探到的是链末端状态码。

        钉死它是为了让下一个想「支持 expected_status: 301」的人先看到这条，
        而不是在线上配了半天发现永远红。
        """
        self._patch(monkeypatch, _FakeResponse(200))  # 重定向后的最终响应
        ok, _msg = checker.check_http_semantic("http://x.test", expected_status=301)
        assert ok is False
        ok, _msg = checker.check_http_semantic("http://x.test", expected_status=200)
        assert ok is True

    def test_connection_error_is_unhealthy_not_crash(self, monkeypatch):
        self._patch(monkeypatch, error=OSError("connection refused"))
        ok, msg = checker.check_http_semantic("http://x.test")
        assert ok is False
        assert "refused" in msg
