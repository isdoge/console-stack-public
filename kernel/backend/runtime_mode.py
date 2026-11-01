# -*- coding: utf-8 -*-
"""运行模式判定：存储引擎、实例角色、维护模式。

提案 §6.4 的启动契约，PG-00 定版（契约版本 `pg00-1`）：

| 变量 | 取值 | 语义 |
| --- | --- | --- |
| `CONSOLE_STORAGE_ENGINE` | `postgres` / `sqlite` | 在线业务存储；`sqlite` 仅为过渡期 legacy |
| `CONSOLE_INSTANCE_ROLE` | `authority` / `client` / `test` | 只有 authority 运行自动采集（Q1） |
| `CONSOLE_MAINTENANCE_MODE` | `normal` / `read_only` | read_only 拒绝全部业务写 |

四条硬规则：

1. **非法值不静默降级**——拼错 `authorityy` 直接 `ConfigError`，否则会以错误角色采集或写入。
2. **本模块不 import app、不在 import 期建连接/起线程**——它是被所有人 import 的叶子。
3. **默认值取安全侧**：未设角色时按 `client`（不采集），未设引擎时按 `sqlite`
   （本模块的默认返回值；在线入口不接受隐式默认，见第 4 条）。部署环境按 §6.4 显式设置。
4. **默认值不等于允许隐式回落**（G06 已裁决，2026-09-10）：本模块的默认值**不变**，
   但**在线入口 `app.py` 要求显式声明**——`CONSOLE_STORAGE_ENGINE` 未设/空串 → `SystemExit(4)`；
   `=sqlite` 时**只与 `CONSOLE_INSTANCE_ROLE=test` 共存**，`authority` / `client` / 未声明
   （默认即 `client`）均 `SystemExit(4)`，不再隐式回落 SQLite。
   `sqlite` 因此只用于**明确声明**的离线工具/测试路径（§11.3.2），`test` 是它的唯一合法同伴。
   判据 2 取「非 `test` 皆拒」而非「仅拒 `authority`」：`client` 就是 Mac 在线页面，放行它
   等于允许「Mac 页面写本地 SQLite、与权威端分叉」，正是 D3/D7 要取消的双运行路径。
   **代价（部署方须知）**：本机跑 SQLite 预览也必须显式声明 `test`，靠默认角色起不来。
   **改这个 `DEFAULT_STORAGE_ENGINE` 常量不是本项的解**——闸门拒绝的是「隐式默认」，不是 SQLite。
"""
import os
import socket
from functools import lru_cache
from ipaddress import ip_address
from urllib.parse import urlsplit

STORAGE_ENGINE_POSTGRES = "postgres"
STORAGE_ENGINE_SQLITE = "sqlite"
STORAGE_ENGINES = (STORAGE_ENGINE_POSTGRES, STORAGE_ENGINE_SQLITE)

INSTANCE_ROLE_AUTHORITY = "authority"
INSTANCE_ROLE_CLIENT = "client"
INSTANCE_ROLE_TEST = "test"
INSTANCE_ROLES = (INSTANCE_ROLE_AUTHORITY, INSTANCE_ROLE_CLIENT, INSTANCE_ROLE_TEST)

MAINTENANCE_NORMAL = "normal"
MAINTENANCE_READ_ONLY = "read_only"
MAINTENANCE_MODES = (MAINTENANCE_NORMAL, MAINTENANCE_READ_ONLY)

DEFAULT_STORAGE_ENGINE = STORAGE_ENGINE_SQLITE
DEFAULT_INSTANCE_ROLE = INSTANCE_ROLE_CLIENT
DEFAULT_MAINTENANCE_MODE = MAINTENANCE_NORMAL


class ConfigError(ValueError):
    """运行配置非法：启动即失败，不静默降级到另一个角色/引擎。"""


class ReadOnlyMode(RuntimeError):
    """维护模式 `read_only` 下拒绝业务写入；HTTP 层映射为 503。"""


class AuthorityUnavailable(RuntimeError):
    """固定权威端读取失败；不回落本机 YAML，HTTP 层映射为 503。"""


def _origin_parts(value):
    """Only an HTTP(S) origin, never a path, credentials or request parameters."""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or any(ch.isspace() or ord(ch) < 32 for ch in value)
            or "\\" in value or parsed.netloc.endswith(":") or parsed.port == 0
            or parsed.hostname in ("0.0.0.0", "::")
        ):
            raise ValueError
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.hostname.lower().rstrip("."), port
    except (ValueError, TypeError) as exc:
        raise ConfigError("CONSOLE_AUTHORITY_ORIGIN 必须是无凭据、路径或参数的 HTTP(S) origin") from exc


@lru_cache(maxsize=1)
def _local_hosts():
    hosts = {"localhost", "127.0.0.1", "::1", socket.gethostname().lower().rstrip(".")}
    try:
        hosts.update(row[4][0] for row in socket.getaddrinfo(socket.gethostname(), None))
    except OSError:
        pass
    return hosts


def _is_local_ip(host):
    """Check local interface addresses (including Tailscale) without sending packets."""
    try:
        address = ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.bind((str(address), 0))
        return True
    except OSError:
        return False


def authority_origin(local_origin=None):
    """Validate the fixed NAS origin; errors never include its configured value."""
    raw = (os.environ.get("CONSOLE_AUTHORITY_ORIGIN") or "").strip()
    if not raw:
        raise ConfigError("client 未配置 CONSOLE_AUTHORITY_ORIGIN，无法读取或修改 NAS 权威配置")
    target = _origin_parts(raw)
    self_origins = (local_origin, os.environ.get("CONSOLE_PUBLIC_URL"))
    for own in self_origins:
        if own and target == _origin_parts(own.strip()):
            raise ConfigError("CONSOLE_AUTHORITY_ORIGIN 指向自身，拒绝转发")
    try:
        local_port = int(os.environ.get("PORT", "8080"))
    except ValueError as exc:
        raise ConfigError("PORT 必须是有效端口") from exc
    if target[1] == local_port and (target[0] in _local_hosts() or _is_local_ip(target[0])):
        raise ConfigError("CONSOLE_AUTHORITY_ORIGIN 指向自身，拒绝转发")
    return raw.rstrip("/")


def require_client_authority(local_origin=None):
    """Startup/read-only acceptance gate, before any connection or seed."""
    if instance_role() == INSTANCE_ROLE_CLIENT:
        return authority_origin(local_origin)
    return None


def _read(env, name, allowed, default):
    raw = os.environ.get(name)
    value = default if raw is None else str(raw).strip().lower()
    if not value:
        value = default
    if value not in allowed:
        raise ConfigError(
            f"{name} 取值无效: {value!r}，只允许 {' / '.join(allowed)}"
        )
    return value


def storage_engine():
    """在线业务存储引擎。"""
    return _read(
        os.environ, "CONSOLE_STORAGE_ENGINE", STORAGE_ENGINES, DEFAULT_STORAGE_ENGINE
    )


def instance_role():
    """实例角色：authority 采集、client 只消费、test 隔离。"""
    return _read(
        os.environ, "CONSOLE_INSTANCE_ROLE", INSTANCE_ROLES, DEFAULT_INSTANCE_ROLE
    )


def maintenance_mode():
    """维护模式：normal 可写、read_only 全拒写。"""
    return _read(
        os.environ, "CONSOLE_MAINTENANCE_MODE", MAINTENANCE_MODES, DEFAULT_MAINTENANCE_MODE
    )


def is_authority():
    """本实例是否为权威采集者（Q1：自动采集只由 NAS 运行）。"""
    return instance_role() == INSTANCE_ROLE_AUTHORITY


def is_read_only():
    return maintenance_mode() == MAINTENANCE_READ_ONLY


def writes_allowed():
    """业务写是否放行。`read_only` 一律拒绝，与角色无关（NAS/Mac 都要设）。"""
    return not is_read_only()


def require_writable(what="写操作"):
    """写入前的统一门禁入口；read_only 时抛 `ReadOnlyMode`。

    调用时机：进入写事务之前（拿到维护共享锁之前也可以），不要等 SQL 已经发出去才查。
    """
    if is_read_only():
        raise ReadOnlyMode(f"维护模式 read_only：{what} 当前不可用")
    return True


def snapshot():
    """脱敏诊断快照：只含枚举值，不含 host/凭据。"""
    return {
        "storage_engine": storage_engine(),
        "instance_role": instance_role(),
        "maintenance_mode": maintenance_mode(),
        "writes_allowed": writes_allowed(),
    }
