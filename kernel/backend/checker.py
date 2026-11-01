# -*- coding: utf-8 -*-
import socket
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime

# 语义检查读 body 时的上限：只为匹配一个固定字符串，没必要把整个响应吃进内存。
# 超过这个长度的响应按「前 N 字节里没有」判定 —— 这是刻意的保守取舍，
# 需要在更靠后的位置匹配就说明该用真正的探测工具（Gatus）了。
SEMANTIC_BODY_LIMIT = 64 * 1024


def check_tcp(ip, port, timeout=3):
    """
    检查 TCP 端口是否开放
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, int(port)))
        s.close()
        return True, "Port Open"
    except Exception as e:
        return False, str(e)

def check_http(url, timeout=4, expected_code=200):
    """
    检查 HTTP/HTTPS 服务可用性

    ``expected_code=None`` 表示只看连通性：任何 HTTP 响应（含 4xx/5xx）都算
    服务活着——入口探测用这个口径，带鉴权的服务对匿名探测回 401/403 一样证明
    端口和 HTTP 栈在工作。
    """
    try:
        # 忽略 SSL 校验以便内网自签/IP 直接访问
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(
            url,
            headers={'User-Agent': 'OpsConsole-Checker/1.0'}
        )
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as response:
            code = response.getcode()
            if expected_code is None or code == expected_code:
                return True, f"HTTP {code}"
            else:
                return False, f"HTTP {code} (Expected {expected_code})"
    except urllib.error.HTTPError as e:
        if expected_code is None or e.code == expected_code:
            return True, f"HTTP {e.code}"
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)

def check_http_semantic(
    url,
    expected_status=200,
    max_latency_ms=None,
    body_contains=None,
    timeout=4,
):
    """有限语义检查（Track L1）：状态码 + 耗时 + 固定字符串，三选任意组合。

    存在的理由：`check_tcp` 只能回答「端口通不通」，一个返回 500 的 API 端口
    照样 Accept 连接，在服务表里会显示成 Healthy（设计 §5.5 的原话）。

    **刻意不做**：正则、JSONPath、脚本、自定义 header/凭据（那些一旦需要，
    该上真正的 Gatus，而不是把 token 写进 `assets.yaml`）。
    只在配了 `body_contains` 时才读 body，且最多读 ``SEMANTIC_BODY_LIMIT``。

    **已知边界：跟随重定向**（urllib 默认行为，这里不关）。所以 ``expected_status``
    配 3xx 是配不出来的 —— 探到的是重定向链末端的状态码。关心的是「业务最终能不能
    用」而不是「有没有跳转」，跟随是对的；真要验证跳转本身，那是 Gatus 的活。

    返回 ``(ok, msg)``；msg 说明**哪一条**没过，便于直接进事件 detail。
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(
        url, headers={"User-Agent": "OpsConsole-Checker/1.0"}
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ctx) as response:
            code = response.getcode()
            body = response.read(SEMANTIC_BODY_LIMIT) if body_contains else b""
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 也是「有响应」：期望值就是 500 时它算通过
        code = exc.code
        try:
            body = exc.read(SEMANTIC_BODY_LIMIT) if body_contains else b""
        except Exception:
            body = b""
    except Exception as exc:
        return False, str(exc)
    latency_ms = round((time.perf_counter() - started) * 1000, 1)

    if expected_status is not None and code != expected_status:
        return False, f"HTTP {code}（期望 {expected_status}）"
    if max_latency_ms is not None and latency_ms > max_latency_ms:
        return False, f"响应 {latency_ms}ms 超过上限 {max_latency_ms}ms"
    if body_contains:
        text = body.decode("utf-8", errors="replace")
        if body_contains not in text:
            # 不回显 body：它可能带业务数据，而这是要落进事件表长期保存的
            return False, f"响应体不含预期内容（HTTP {code}，{latency_ms}ms）"
    return True, f"HTTP {code} {latency_ms}ms"


def get_ssl_expiry_days(hostname, port=443, timeout=3):
    """
    获取 SSL 证书剩余有效天数
    """
    # 过滤掉 http 协议前缀
    if "://" in hostname:
        hostname = hostname.split("://")[1]
    if "/" in hostname:
        hostname = hostname.split("/")[0]
    if ":" in hostname:
        hostname, port_str = hostname.split(":")
        port = int(port_str)

    try:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        
        conn = context.wrap_socket(
            socket.socket(socket.AF_INET),
            server_hostname=hostname,
        )
        conn.settimeout(timeout)
        conn.connect((hostname, int(port)))
        conn.getpeercert(binary_form=True)
        
        # 简化版：直接从 wrapped socket 获取 peer 证书明文（必须 verify_mode=CERT_REQUIRED 才能 getpeercert() 拿到 dict）
        # 所以我们需要建立一个需要验证的握手
        conn_verify = ssl.create_default_context().wrap_socket(
            socket.socket(socket.AF_INET),
            server_hostname=hostname,
        )
        conn_verify.settimeout(timeout)
        conn_verify.connect((hostname, int(port)))
        cert_dict = conn_verify.getpeercert()
        
        pay_date_str = cert_dict['notAfter']
        # 'Jul  7 10:00:00 2026 GMT'
        expiry_date = datetime.strptime(pay_date_str, '%b %d %H:%M:%S %Y %Z')
        delta = expiry_date - datetime.utcnow()
        return True, delta.days
    except Exception as e:
        # 备用方案：尝试非严格验证拿到 dict 失败时的 fallback
        return False, str(e)
