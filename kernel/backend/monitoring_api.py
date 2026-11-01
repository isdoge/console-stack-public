# -*- coding: utf-8 -*-
"""B4-3：监控接收 API（POST /api/monitoring/v1/{enrol,ingest,revoke}）。

自有 Go Agent 的影子接收路径（B4 边界：不动 Beszel 数据流）。路由体在本模块，
app.py 只 import + ``register_routes`` 一行挂载（dashboard_layout 同模式）。

身份与安全模型（.agents/contracts/backend/monitoring-provider-contract.md §2.1）：
- ``machine_id`` 只认台账（``resolve_machine_ids`` 每请求求值），enrol 白名单校验，
  未登记身份不得写入；
- enrol 校验环境持有的 ``CONSOLE_MONITORING_ENROLL_TOKEN``，发一次性明文
  secret（只在响应里出现一次），库内只存 SHA-256；
  ingest 用 ``Authorization: Bearer <secret>`` 认证，恒定时间哈希比对；
- revoke 是管理员动作：复用 auth.py 的 Basic 凭据（CONSOLE_AUTH_USER/PASSWORD
  同源，``CONSOLE_AUTH_ENABLED`` 开启时全局钩子已挡、到达即已认证）。

ingest 处理顺序：1MB 上限(413) → JSON/必填(400) → Bearer 认证(401) →
滑动窗口限流(429) → 协议/Agent 版本协商(426) → samples 逐条校验(400) →
去重落库(202)。认证通过即扣限流额度，与该请求后续成败无关。

存储是窄缝：默认 :class:`InMemoryMachineStore`（B4-2 PG 身份表落地前的
兜底/测试底座，接口对齐其机器身份表），``configure_store`` 注入同形实现
即可切换，本模块不感知 PG。审计走本模块 logger（认证失败/限流/版本拒收/
每次接收无论成败都记）；只有 revoke 进 oplog，高频 ingest 不进。
"""
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone

from flask import jsonify, request

import monitoring_payload
from auth import _check_basic_auth

logger = logging.getLogger("console.backend.monitoring")

PROTOCOL_VERSION = "v1"
AGENT_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
MAX_BODY_BYTES = 1024 * 1024
RATE_WINDOW_SEC = 300
RATE_MAX_REQUESTS = 12
_SECRET_BYTES = 32
# 内存兜底的去重键 / 样本缓冲上限：只求影子路径与测试可用，不求替 PG 存历史。
_MEMORY_SAMPLE_LIMIT = 20000
_AUDIT_VALUE_LIMIT = 64


def _utc_now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_sampled_at(value):
    """ISO 8601（接受 Z 后缀）→ aware UTC datetime；不合法返回 None。

    Python 3.9 的 ``fromisoformat`` 不认 ``Z``，先替换成 +00:00；naive 一律按
    UTC 解释（B1 契约 §3：时间语义全 UTC）。原样字符串传给存储层——接收端
    不改写 ``sampled_at``（B1 契约 §3 规则 2）。
    Go `time.Time` JSON 序列化走 RFC3339Nano（如 ``...34.762665865Z``），
    Python 3.10 的 fromisoformat 不支持超过 6 位小数秒；先截断到微秒再解析。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = _truncate_fraction(text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _truncate_fraction(text):
    """ISO 8601 小数秒截断到微秒（fromisoformat 上限）；无小数秒原样返回。"""
    m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})\.(\d+)([+-].*|)$", text)
    if not m:
        return text
    head, frac, tail = m.groups()
    frac = frac[:6].ljust(6, "0")
    return head + "." + frac + tail


def _parse_json_object(raw):
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _bearer_token():
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer ") and len(header) > len("Bearer "):
        return header[len("Bearer "):].strip()
    return None


def _valid_enroll_token(value):
    """Go agent 的 enrol_token 只与环境中的管理员令牌比对，不进入审计。"""
    expected = os.environ.get("CONSOLE_MONITORING_ENROLL_TOKEN", "")
    supplied = value if isinstance(value, str) else ""
    return bool(expected) and hmac.compare_digest(supplied, expected)


def _unauthorized():
    return jsonify({"error": "unauthorized"}), 401, {"WWW-Authenticate": "Bearer"}


def _admin_unauthorized():
    return (
        jsonify({"error": "admin authorization required"}),
        401,
        {"WWW-Authenticate": 'Basic realm="Console Stack"'},
    )


def _upgrade_required(message):
    return jsonify({"error": message, "current_protocol": PROTOCOL_VERSION}), 426


def _audit_value(value):
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text[:_AUDIT_VALUE_LIMIT] if text else "-"


def _audit(event, status, **fields):
    """结构化审计行；4xx/5xx 记 warning，成功记 info。"""
    detail = " ".join("%s=%s" % (key, _audit_value(value)) for key, value in fields.items())
    message = "monitoring_%s status=%d %s" % (event, status, detail)
    if status < 400:
        logger.info(message)
    else:
        logger.warning(message)


class InMemoryMachineStore:
    """内存机器身份表 + 样本缓冲（默认窄缝实现，接口对齐 B4-2 的 PG 身份表）。

    机器行形状：display_name / public_key / secret_sha256 / enrolled_at /
    last_seen / status('active'|'revoked')。不做持久化，进程重启即清。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._machines = {}
        self._seen_keys = OrderedDict()
        self._samples = deque(maxlen=_MEMORY_SAMPLE_LIMIT)

    def enrol_machine(self, machine_id, display_name, public_key, secret=None):
        secret = secret or secrets.token_urlsafe(_SECRET_BYTES)
        with self._lock:
            if machine_id in self._machines:
                return None
            self._machines[machine_id] = {
                "display_name": display_name,
                "public_key": public_key,
                "secret_sha256": _sha256_hex(secret),
                "enrolled_at": _utc_now_text(),
                "last_seen": None,
                "status": "active",
            }
            return secret

    def verify_machine(self, machine_id, secret):
        """返回 'active' / 'revoked'；未注册或密钥不匹配返回 None（不区分，防枚举）。

        B5-5 收口：接收端传明文，内部 SHA-256 后比对——与 PG store 的
        monitoring_ingest.verify_machine 收明文的形状对齐。
        """
        with self._lock:
            row = self._machines.get(machine_id)
            if row is None:
                return None
            if not hmac.compare_digest(row["secret_sha256"], _sha256_hex(secret)):
                return None
            return row["status"]

    def revoke_machine(self, machine_id):
        with self._lock:
            row = self._machines.get(machine_id)
            if row is None:
                return False
            row["status"] = "revoked"
            return True

    def touch_last_seen(self, machine_id):
        with self._lock:
            row = self._machines.get(machine_id)
            if row is not None:
                row["last_seen"] = _utc_now_text()

    def insert_samples(self, machine_id, samples, agent_version):
        """去重插入（machine_id + sampled_at + metric_name 唯一），返回新增条数。

        样本行对齐 B4-2 表结构：machine_id / sampled_at / metric_name /
        metric_value / source(agent 版本) / received_at(到达时间)。
        """
        inserted = 0
        received_at = _utc_now_text()
        with self._lock:
            for sample in samples:
                sampled = _parse_sampled_at(sample.get("sampled_at"))
                key = (machine_id, sampled.timestamp(), str(sample.get("metric_name")))
                if key in self._seen_keys:
                    self._seen_keys.move_to_end(key)
                    continue
                self._seen_keys[key] = True
                if len(self._seen_keys) > _MEMORY_SAMPLE_LIMIT:
                    self._seen_keys.popitem(last=False)
                self._samples.append(
                    {
                        "machine_id": machine_id,
                        "sampled_at": sample.get("sampled_at"),
                        "metric_name": sample.get("metric_name"),
                        "metric_value": sample.get("metric_value"),
                        "source": agent_version,
                        "received_at": received_at,
                    }
                )
                inserted += 1
        return inserted


class SlidingWindowRateLimiter:
    """单 machine_id 滑动窗口限流（5 分钟 12 次：5 分钟采样 + 缓冲重试余量）。"""

    def __init__(self, window_sec=RATE_WINDOW_SEC, max_requests=RATE_MAX_REQUESTS):
        self._window_sec = window_sec
        self._max_requests = max_requests
        self._lock = threading.Lock()
        self._hits = {}

    def allow(self, key):
        now = time.time()
        with self._lock:
            queue = self._hits.setdefault(key, deque())
            cutoff = now - self._window_sec
            while queue and queue[0] <= cutoff:
                queue.popleft()
            if len(queue) >= self._max_requests:
                return False
            queue.append(now)
            return True


_store = InMemoryMachineStore()
_limiter = SlidingWindowRateLimiter()
_resolve_machine_ids = None
_log_op = None


def configure_store(store):
    """注入 B4-2 的持久化身份表（须实现同形方法集）；默认内存兜底。"""
    global _store
    _store = store


def reset_for_tests():
    """测试夹具用：换干净的内存 store 与限流器，隔离模块级单例状态。"""
    global _store, _limiter
    _store = InMemoryMachineStore()
    _limiter = SlidingWindowRateLimiter()


def _samples_error(samples):
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            return "samples[%d] must be an object" % index
        name = sample.get("metric_name")
        if not isinstance(name, str) or not name.strip():
            return "samples[%d].metric_name required" % index
        value = sample.get("metric_value")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            return "samples[%d].metric_value must be a finite number" % index
        if _parse_sampled_at(sample.get("sampled_at")) is None:
            return "samples[%d].sampled_at must be ISO 8601" % index
    return None


def register_routes(app, log_op=None, resolve_machine_ids=None):
    """挂 /api/monitoring/v1/* 三条路由；app.py 只负责 import 与这一处调用。

    ``resolve_machine_ids`` 每请求求值（dashboard_layout 同模式：注册时捕获
    值会让测试的 monkeypatch 失效），返回台账 machine_id 集合；``log_op``
    复用 app.py 的 oplog，只记 revoke 这类管理动作。
    """
    global _log_op, _resolve_machine_ids
    _log_op = log_op
    _resolve_machine_ids = resolve_machine_ids

    @app.route("/api/monitoring/v1/enrol", methods=["POST"])
    def monitoring_enrol():
        remote = request.remote_addr or "-"
        payload = _parse_json_object(request.get_data(cache=True))
        if payload is None:
            _audit("enrol", 400, remote=remote, reason="bad_json")
            return jsonify({"error": "invalid JSON body"}), 400
        machine_id = payload.get("machine_id")
        display_name = payload.get("display_name")
        public_key = payload.get("public_key")
        if not isinstance(machine_id, str) or not machine_id.strip():
            _audit("enrol", 400, remote=remote, reason="machine_id_required")
            return jsonify({"error": "machine_id required"}), 400
        if not _valid_enroll_token(payload.get("enroll_token")):
            _audit("enrol", 401, machine=machine_id, remote=remote, reason="enroll_auth_failed")
            return _unauthorized()
        if "protocol" in payload and "protocol_version" not in payload:
            payload["protocol_version"] = payload["protocol"]
        protocol = payload.get("protocol_version")
        agent_version = payload.get("agent_version")
        if protocol is not None and protocol != PROTOCOL_VERSION:
            _audit("enrol", 426, machine=machine_id, remote=remote, reason="protocol_unsupported")
            return _upgrade_required("protocol_version not supported")
        if agent_version is not None and (
            not isinstance(agent_version, str) or not AGENT_VERSION_RE.match(agent_version)
        ):
            _audit("enrol", 426, machine=machine_id, remote=remote, reason="agent_version_invalid")
            return _upgrade_required("agent_version must be x.y.z")
        if display_name is not None and not isinstance(display_name, str):
            _audit("enrol", 400, machine=machine_id, remote=remote, reason="display_name_invalid")
            return jsonify({"error": "display_name must be a string"}), 400
        if public_key is not None and not isinstance(public_key, str):
            _audit("enrol", 400, machine=machine_id, remote=remote, reason="public_key_invalid")
            return jsonify({"error": "public_key must be a string"}), 400
        known = _resolve_machine_ids() if _resolve_machine_ids else set()
        if machine_id not in known:
            _audit("enrol", 400, machine=machine_id, remote=remote, reason="not_in_ledger")
            return jsonify({"error": "machine_id not in ledger"}), 400
        try:
            secret = _store.enrol_machine(machine_id, display_name, public_key)
        except Exception as exc:
            # PG store（monitoring_ingest）对「已 enrol」抛 AlreadyEnrolledError
            # 而非返回 None（InMemory 契约）；只把这一类映射 409，其余异常照旧上浮 500。
            if type(exc).__name__ == "AlreadyEnrolledError":
                secret = None
            else:
                raise
        if not secret:
            _audit("enrol", 409, machine=machine_id, remote=remote, reason="already_enrolled")
            return jsonify({"error": "machine already enrolled"}), 409
        _audit("enrol", 200, machine=machine_id, remote=remote)
        return jsonify({"status": "enrolled", "secret": secret})

    @app.route("/api/monitoring/v1/ingest", methods=["POST"])
    def monitoring_ingest():
        remote = request.remote_addr or "-"
        raw = request.get_data(cache=True)
        if len(raw) > MAX_BODY_BYTES:
            _audit("ingest", 413, remote=remote, reason="body_too_large")
            return jsonify({"error": "request body too large"}), 413
        payload = _parse_json_object(raw)
        if payload is None:
            _audit("ingest", 400, remote=remote, reason="bad_json")
            return jsonify({"error": "invalid JSON body"}), 400
        machine_id = payload.get("machine_id")
        if not isinstance(machine_id, str) or not machine_id.strip():
            _audit("ingest", 400, remote=remote, reason="machine_id_required")
            return jsonify({"error": "machine_id required"}), 400
        agent_version = payload.get("agent_version")

        token = _bearer_token()
        if not token:
            _audit("ingest", 401, machine=machine_id, remote=remote, reason="missing_token")
            return _unauthorized()
        status = _store.verify_machine(machine_id, token)
        if status != "active":
            # 未注册 / 密钥不符 / 已吊销统一 401，不给探测面。
            _audit("ingest", 401, machine=machine_id, remote=remote, reason="auth_failed")
            return _unauthorized()
        if not _limiter.allow(machine_id):
            _audit("ingest", 429, machine=machine_id, agent=agent_version, remote=remote, reason="rate_limited")
            return jsonify({"error": "rate limit exceeded"}), 429
        # B5-5 收口：真实 agent 形状（顶层 protocol + 嵌套 samples，agent/sender/http.go）
        # 先拍平再校验；版本不符仍走 426（与手工形状同语义），其余拍平失败 400。
        # curl 手工形状（protocol_version + 扁平 samples）向后兼容直走。
        if "protocol" in payload and "protocol_version" not in payload:
            if payload.get("protocol") != PROTOCOL_VERSION:
                _audit("ingest", 426, machine=machine_id, agent=agent_version, remote=remote, reason="protocol_unsupported")
                return _upgrade_required("protocol_version not supported")
            try:
                expanded = monitoring_payload.expand_agent_payload(payload)
            except ValueError as exc:
                _audit("ingest", 400, machine=machine_id, agent=agent_version, remote=remote, reason="expand_failed:%s" % exc)
                return jsonify({"error": "agent payload expand failed: %s" % exc}), 400
            payload["protocol_version"] = expanded["protocol_version"]
            payload["samples"] = expanded["samples"]
            agent_version = expanded["agent_version"] or agent_version
        if payload.get("protocol_version") != PROTOCOL_VERSION:
            _audit("ingest", 426, machine=machine_id, agent=agent_version, remote=remote, reason="protocol_unsupported")
            return _upgrade_required("protocol_version not supported")
        if not isinstance(agent_version, str) or not AGENT_VERSION_RE.match(agent_version):
            _audit("ingest", 426, machine=machine_id, agent=agent_version, remote=remote, reason="agent_version_invalid")
            return _upgrade_required("agent_version must be x.y.z")
        samples = payload.get("samples")
        if not isinstance(samples, list):
            _audit("ingest", 400, machine=machine_id, agent=agent_version, remote=remote, reason="samples_invalid")
            return jsonify({"error": "samples must be a list"}), 400
        sample_error = _samples_error(samples)
        if sample_error:
            _audit("ingest", 400, machine=machine_id, agent=agent_version, remote=remote, reason=sample_error)
            return jsonify({"error": sample_error}), 400
        inserted = _store.insert_samples(machine_id, samples, agent_version)
        _store.touch_last_seen(machine_id)
        _audit("ingest", 202, machine=machine_id, agent=agent_version, samples=len(samples), remote=remote)
        return jsonify({"status": "accepted", "inserted": inserted}), 202

    @app.route("/api/monitoring/v1/revoke", methods=["POST"])
    def monitoring_revoke():
        remote = request.remote_addr or "-"
        if not _check_basic_auth():
            _audit("revoke", 401, remote=remote, reason="admin_auth_failed")
            return _admin_unauthorized()
        payload = _parse_json_object(request.get_data(cache=True))
        if payload is None:
            _audit("revoke", 400, remote=remote, reason="bad_json")
            return jsonify({"error": "invalid JSON body"}), 400
        machine_id = payload.get("machine_id")
        if not isinstance(machine_id, str) or not machine_id.strip():
            _audit("revoke", 400, remote=remote, reason="machine_id_required")
            return jsonify({"error": "machine_id required"}), 400
        if not _store.revoke_machine(machine_id):
            _audit("revoke", 404, machine=machine_id, remote=remote, reason="unknown_machine")
            return jsonify({"error": "unknown machine"}), 404
        if _log_op:
            _log_op("monitoring", machine_id, "machine_revoke", True)
        _audit("revoke", 200, machine=machine_id, remote=remote)
        return jsonify({"status": "revoked"})

    return app
