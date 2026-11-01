# -*- coding: utf-8 -*-
"""集中校验所有可能进入 shell、资产台账或审计日志的外部输入。"""
import re

IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SERVICE_ACTIONS = frozenset({"start", "stop", "restart", "logs", "upgrade"})
# upgrade 仅对 docker/compose 服务可用，且要求台账显式登记 compose 目录；
# 未登记 compose_dir 的服务在 do_action 里拒绝该动作（不走这里放宽）。
AUDIT_ACTIONS = frozenset({*SERVICE_ACTIONS, "caddy-reload"})


def validate_identifier(value, field="identifier", allow_empty=False):
    text = "" if value is None else str(value).strip()
    if not text and allow_empty:
        return ""
    if not IDENTIFIER_RE.fullmatch(text):
        raise ValueError(f"{field} 仅允许字母、数字、点、下划线和连字符，长度 1-128")
    return text


def validate_action(value, allowed=SERVICE_ACTIONS):
    action = validate_identifier(value, "action")
    if action not in allowed:
        raise ValueError(f"不支持的动作: {action}")
    return action


def validate_port(value):
    if value in (None, ""):
        return None
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("端口必须是整数") from exc
    if not 1 <= port <= 65535:
        raise ValueError("端口范围必须为 1-65535")
    return port


def validate_panel(data):
    """校验外部面板配置，返回规范化后的字段字典。"""
    data = data or {}
    name = str(data.get("name") or "").strip()
    if not name or len(name) > 64:
        raise ValueError("面板名称不能为空且长度不超过 64")
    url = str(data.get("url") or "").strip()
    if not re.match(r"^https?://[^\s]+$", url) or len(url) > 512:
        raise ValueError("面板 URL 必须为 http/https 地址且长度不超过 512")
    icon = str(data.get("icon") or "").strip()
    if len(icon) > 32:
        raise ValueError("面板图标名长度不超过 32")
    note = str(data.get("note") or "").strip()
    if len(note) > 128:
        raise ValueError("面板账号长度不超过 128")
    note2 = str(data.get("note2") or "").strip()
    if len(note2) > 128:
        raise ValueError("面板密码长度不超过 128")
    machine = validate_identifier(data.get("machine"), "machine", allow_empty=True)
    try:
        sort_order = int(data.get("sort_order") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("面板排序必须是整数") from exc
    open_mode = data.get("open_mode", "embedded")
    if open_mode not in ("embedded", "new_tab"):
        raise ValueError("打开方式必须是 embedded 或 new_tab")
    enabled = 1 if data.get("enabled", True) in (True, 1, "1", "true", "True") else 0
    return {
        "name": name,
        "url": url,
        "open_mode": open_mode,
        "icon": icon,
        "note": note,
        "note2": note2,
        "machine": machine,
        "enabled": enabled,
        "sort_order": sort_order,
        # 同源代理开关（面板 yaml 字段）；设置页整表替换必须原样带回，否则会被静默清掉
        "proxy": bool(data.get("proxy") in (True, 1, "1", "true", "True")),
    }


# 语义检查（Track L1）：target 只能引用 access 里已有的地址，不允许另写 URL——
# 否则台账会多出第二份「这个服务在哪」的事实源，和 access 迟早对不上。
HEALTH_PROBE_TARGETS = ("domain", "tailscale", "home")


def validate_health_probe(value):
    """校验 `assets.yaml` 的 `health_probe`，返回规范化值。

    三态（与 `probe_cache.check_service_health` 的分支一一对应）：

    - ``False`` / 字符串假值 → ``False``：显式关闭，不探测（现有 10 个服务在用）；
    - ``None`` / ``True`` → ``None``：走默认推断（有 port 打 TCP，否则打 domain）；
    - ``dict`` → 规范化后的语义检查对象。

    首版只认 ``type: http``，字段限于 ``target`` / ``expected_status`` /
    ``max_latency_ms`` / ``body_contains``（设计 §5.5）。**不接受 header、
    凭据或任何脚本/表达式**——需要那些就该上独立 Gatus，而不是把 token 写进台账。
    未知字段直接拒绝，不静默丢弃：静默丢会让用户以为配置生效了。

    ``expected_status`` 的两种「空」**语义不同，别合并**：

    - **键缺失** → 默认 ``200``。``{type: http}`` 这种最小配置总得查点什么，
      否则等于配了个不做任何判断的检查。
    - **显式 ``None`` / 空串** → 不校验状态码（只看耗时/内容）。前端「留空」
      走的就是这条，所以它发的是显式 ``null`` 而不是省略该键。
    """
    if value is None or value is True:
        return None
    if value is False or (
        isinstance(value, str) and value.strip().lower() in ("0", "false", "no", "off")
    ):
        return False
    if not isinstance(value, dict):
        raise ValueError("health_probe 必须是 false 或对象")

    unknown = set(value) - {
        "type",
        "target",
        "expected_status",
        "max_latency_ms",
        "body_contains",
    }
    if unknown:
        raise ValueError("health_probe 不支持的字段: " + "、".join(sorted(unknown)))

    kind = str(value.get("type") or "http").strip().lower()
    if kind != "http":
        raise ValueError("health_probe.type 首版只支持 http")

    target = str(value.get("target") or "domain").strip().lower()
    if target not in HEALTH_PROBE_TARGETS:
        raise ValueError(
            "health_probe.target 必须是 " + " / ".join(HEALTH_PROBE_TARGETS)
        )

    probe = {"type": kind, "target": target}

    raw_status = value.get("expected_status", 200)
    if raw_status not in (None, ""):
        if isinstance(raw_status, bool):
            raise ValueError("health_probe.expected_status 必须是整数")
        try:
            status = int(raw_status)
        except (TypeError, ValueError) as exc:
            raise ValueError("health_probe.expected_status 必须是整数") from exc
        if not 100 <= status <= 599:
            raise ValueError("health_probe.expected_status 范围必须为 100-599")
        probe["expected_status"] = status

    raw_latency = value.get("max_latency_ms")
    if raw_latency not in (None, ""):
        if isinstance(raw_latency, bool):
            raise ValueError("health_probe.max_latency_ms 必须是整数")
        try:
            latency = int(raw_latency)
        except (TypeError, ValueError) as exc:
            raise ValueError("health_probe.max_latency_ms 必须是整数") from exc
        if not 1 <= latency <= 60000:
            raise ValueError("health_probe.max_latency_ms 范围必须为 1-60000")
        probe["max_latency_ms"] = latency

    raw_body = value.get("body_contains")
    if raw_body not in (None, ""):
        body = str(raw_body)
        if len(body) > 256:
            raise ValueError("health_probe.body_contains 长度不超过 256")
        probe["body_contains"] = body

    return probe


def validate_timestamp(value):
    text = "" if value is None else str(value).strip()
    if not text or len(text) > 64 or any(ch in text for ch in ("\r", "\n", "\x00")):
        raise ValueError("审计时间格式无效")
    return text
