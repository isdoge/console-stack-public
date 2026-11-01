# -*- coding: utf-8 -*-
"""PostgreSQL 运行连接 / 事务唯一入口（提案 §4.1/§4.2；契约版本 `pg00-1`）。

对 PG-01–04 的承诺：

- `pg_connection()` 借出池连接，`pg_transaction()` 是**业务读写唯一事务入口**。
- 写事务自动串行化在「维护共享锁」上：NAS 维护控制器持有同名独占锁时，
  所有写事务在有界超时内失败，而不是悄悄写进去。
- 异常只暴露 `PgConfigError` / `StorageUnavailable` / `ConcurrentConflict` /
  `PgPermissionError` / `CommitOutcomeUnknown`，HTTP 层按 §4.1 映射；**异常文本不含 DSN 或密码**。
- COMMIT 期间断连 = 提交结果未知，抛 `CommitOutcomeUnknown`，**不自动重放**。

psycopg 是延迟导入的：没装依赖时 `import db` / `import pgdb` 仍然成功，
只是调用 PG 入口才报 `PgConfigError`——现有 SQLite 测试不因新依赖变红。
"""
import os
import threading
import time
from contextlib import contextmanager

import pg_locks
import runtime_mode

_MODULES = None
_POOL = None
_CONFIG = None
_POOL_LOCK = threading.Lock()

DEFAULT_PORT = 5432
DEFAULT_DB = "console"
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_POOL_MIN = 1
DEFAULT_POOL_MAX = 6
DEFAULT_ACQUIRE_TIMEOUT = 3.0
DEFAULT_STATEMENT_TIMEOUT_S = 30
DEFAULT_LOCK_TIMEOUT_S = 5


class PgError(RuntimeError):
    """PG 基础层错误基类。"""


class PgConfigError(PgError):
    """配置缺失/非法：部署问题，不是运行时故障。"""


class StorageUnavailable(PgError):
    """连接不可达、池耗尽、维护中（HTTP 503）。"""


class ConcurrentConflict(PgError):
    """合法并发冲突：唯一约束、序列化失败、锁超时（HTTP 409）。"""


class PgPermissionError(PgError):
    """运行账号权限不足：部署/授权问题（HTTP 500，不是客户端错误）。"""


class CommitOutcomeUnknown(PgError):
    """COMMIT 期间连接断开：结果未知，**不得自动重试**，按业务 ID 重新核验。"""


def _mods():
    """延迟导入 psycopg；返回 (psycopg, psycopg_pool, dict_row)。"""
    global _MODULES
    if _MODULES is None:
        try:
            import psycopg
            import psycopg_pool
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise PgConfigError(
                "缺少 psycopg 依赖：请安装 psycopg[binary] 与 psycopg_pool"
            ) from exc
        _MODULES = (psycopg, psycopg_pool, dict_row)
    return _MODULES


def _safe_exc(exc):
    """异常文本脱敏：psycopg 的 conninfo 可能带密码。"""
    text = str(exc)
    if "password" in text.lower() or "conninfo" in text.lower():
        return type(exc).__name__
    return text


class PgConfig:
    """连接配置。密码只存在于本对象内，`__repr__` 与 `safe_summary()` 都不含它。"""

    __slots__ = ("host", "port", "dbname", "user", "password", "connect_timeout",
                 "pool_min", "pool_max", "acquire_timeout", "statement_timeout_s",
                 "lock_timeout_s")

    def __init__(self, host, port, dbname, user, password,
                 connect_timeout=DEFAULT_CONNECT_TIMEOUT,
                 pool_min=DEFAULT_POOL_MIN, pool_max=DEFAULT_POOL_MAX,
                 acquire_timeout=DEFAULT_ACQUIRE_TIMEOUT,
                 statement_timeout_s=DEFAULT_STATEMENT_TIMEOUT_S,
                 lock_timeout_s=DEFAULT_LOCK_TIMEOUT_S):
        self.host = host
        self.port = port
        self.dbname = dbname
        self.user = user
        self.password = password
        self.connect_timeout = connect_timeout
        self.pool_min = pool_min
        self.pool_max = pool_max
        self.acquire_timeout = acquire_timeout
        self.statement_timeout_s = statement_timeout_s
        self.lock_timeout_s = lock_timeout_s

    def __repr__(self):
        return f"PgConfig({self.safe_summary()})"

    def safe_summary(self):
        """可安全打印/入日志的摘要（无密码）。"""
        return (
            f"host={self.host} port={self.port} db={self.dbname} user={self.user} "
            f"pool={self.pool_min}-{self.pool_max} connect_timeout={self.connect_timeout}"
        )

    def conninfo(self):
        """psycopg 连接串（含密码，仅传给驱动，绝不打印）。"""
        parts = [
            f"host={self.host}",
            f"port={self.port}",
            f"dbname={self.dbname}",
            f"user={self.user}",
        ]
        if self.password:
            parts.append(f"password={self.password}")
        return " ".join(parts)

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        host = (env.get("CONSOLE_PG_HOST") or "").strip()
        user = (env.get("CONSOLE_PG_USER") or "").strip()
        password = env.get("CONSOLE_PG_PASSWORD") or ""
        missing = [
            name
            for name, value in (
                ("CONSOLE_PG_HOST", host),
                ("CONSOLE_PG_USER", user),
                ("CONSOLE_PG_PASSWORD", password),
            )
            if not value
        ]
        if missing:
            raise PgConfigError(
                "缺少 PostgreSQL 配置: " + "、".join(missing)
                + "（应用不会退回本地 SQLite）"
            )
        return cls(
            host=host,
            port=_int_env(env, "CONSOLE_PG_PORT", DEFAULT_PORT),
            dbname=(env.get("CONSOLE_PG_DB") or DEFAULT_DB).strip(),
            user=user,
            password=password,
            connect_timeout=_float_env(env, "CONSOLE_PG_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT),
            pool_min=_int_env(env, "CONSOLE_PG_POOL_MIN", DEFAULT_POOL_MIN),
            pool_max=_int_env(env, "CONSOLE_PG_POOL_MAX", DEFAULT_POOL_MAX),
            acquire_timeout=_float_env(env, "CONSOLE_PG_ACQUIRE_TIMEOUT", DEFAULT_ACQUIRE_TIMEOUT),
            statement_timeout_s=_int_env(
                env, "CONSOLE_PG_STATEMENT_TIMEOUT", DEFAULT_STATEMENT_TIMEOUT_S
            ),
            lock_timeout_s=_int_env(env, "CONSOLE_PG_LOCK_TIMEOUT", DEFAULT_LOCK_TIMEOUT_S),
        )


def _int_env(env, name, default):
    raw = env.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(str(raw).strip())
    except ValueError as exc:
        raise PgConfigError(f"{name} 必须是整数，实际为 {raw!r}") from exc


def _float_env(env, name, default):
    raw = env.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(str(raw).strip())
    except ValueError as exc:
        raise PgConfigError(f"{name} 必须是数字，实际为 {raw!r}") from exc


def pool(config=None):
    """进程内单例连接池。首次调用时建立并异步打开，不做连接预热。"""
    global _POOL, _CONFIG
    if _POOL is not None:
        return _POOL
    with _POOL_LOCK:
        if _POOL is None:
            cfg = config or PgConfig.from_env()
            built = _build_pool(cfg)
            built.open(wait=False)
            _CONFIG, _POOL = cfg, built
    return _POOL


def config():
    """当前生效配置（池已建立时返回建立时的配置，否则读环境变量）。"""
    if _CONFIG is not None:
        return _CONFIG
    return PgConfig.from_env()


def _build_pool(cfg):
    _, psycopg_pool, dict_row = _mods()
    options = (
        f"-c timezone=UTC -c statement_timeout={cfg.statement_timeout_s * 1000} "
        f"-c lock_timeout={cfg.lock_timeout_s * 1000}"
    )
    try:
        return psycopg_pool.ConnectionPool(
            cfg.conninfo(),
            min_size=cfg.pool_min,
            max_size=cfg.pool_max,
            timeout=cfg.acquire_timeout,
            kwargs={
                "connect_timeout": cfg.connect_timeout,
                "options": options,
                "row_factory": dict_row,
                "application_name": f"console-stack/{runtime_mode.instance_role()}",
            },
            name="console-stack",
            open=False,
        )
    except Exception as exc:  # pragma: no cover - 驱动级构造失败
        raise PgConfigError(f"连接池构造失败：{_safe_exc(exc)}") from exc


def open_pool(config=None, wait=True, timeout=None):
    """显式打开池（应用启动时调用；测试夹具也用它）。"""
    connection_pool = pool(config)
    try:
        connection_pool.open(wait=wait, timeout=timeout)
    except Exception as exc:
        raise StorageUnavailable(f"PostgreSQL 连接池打开失败：{_safe_exc(exc)}") from exc
    return connection_pool


def close_pool():
    """关闭并丢弃池（测试/进程退出用）。"""
    global _POOL, _CONFIG
    with _POOL_LOCK:
        current, _POOL, _CONFIG = _POOL, None, None
    if current is not None:
        try:
            current.close()
        except Exception:  # pragma: no cover - 关闭失败不应影响退出
            pass


@contextmanager
def pg_connection():
    """从池借出连接；退出归还。不隐式提交/回滚（交给 `pg_transaction` 或调用方）。"""
    connection_pool = pool()
    try:
        with connection_pool.connection() as conn:
            yield conn
    except Exception as exc:
        raise _map_exception(exc) from exc


@contextmanager
def pg_transaction(readonly=False, lock=None, locks=None, statement_timeout=None,
                   isolation=None):
    """显式事务入口。

    参数
    ----
    readonly : True → `SET TRANSACTION READ ONLY`（PG 侧强制拒写），且不取维护共享锁。
    lock / locks : 事务级 advisory lock；**写事务自动追加维护共享锁**。
    statement_timeout : 秒；批量工具显式延长，不要改全局。
    isolation : 只读默认 `repeatable read`（提案 §4.2 一致视图要求）。

    退出语义：正常 → COMMIT；异常 → ROLLBACK 后抛出映射后的异常。
    COMMIT 期间连接断开 → `CommitOutcomeUnknown`（调用方不得自动重试）。
    """
    if not readonly:
        runtime_mode.require_writable("数据库写事务")
    psycopg, _, _ = _mods()
    cfg = config()

    requested = list(locks or ())
    if lock is not None:
        requested.append(lock)
    if not readonly and not any(item == pg_locks.MAINTENANCE_SHARED for item in requested):
        requested.append(pg_locks.MAINTENANCE_SHARED)
    ordered = pg_locks.sort_locks(requested)

    connection_pool = pool()
    try:
        conn = connection_pool.getconn()
    except Exception as exc:
        raise _map_exception(exc) from exc

    previous = (conn.autocommit, conn.isolation_level)
    try:
        try:
            conn.autocommit = False
            if isolation is not None:
                conn.isolation_level = isolation
            elif readonly:
                conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            cur = conn.cursor()
            # SET TRANSACTION 必须是事务的第一条语句；其余用 set_config(..., true) 走事务局部。
            if readonly:
                cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(
                "SELECT set_config('lock_timeout', %s, true)", (f"{cfg.lock_timeout_s}s",)
            )
            if statement_timeout is not None:
                cur.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (f"{float(statement_timeout)}s",),
                )
            for item in ordered:
                pg_locks.acquire(conn, item)
        except Exception as exc:
            _safe_rollback(conn)
            raise _map_exception(exc) from exc

        try:
            yield conn
        except Exception as exc:
            _safe_rollback(conn)
            # 调用方在事务体内触发的 psycopg 异常（超时、约束冲突、断连）同样要映射，
            # 否则 HTTP 层会拿到原始驱动异常，既不脱敏也不符合 §4.1 的状态码契约。
            mapped = _map_exception(exc)
            if mapped is exc:
                raise
            raise mapped from exc

        try:
            conn.commit()
        except Exception as exc:
            _safe_rollback(conn)
            raise _map_commit_exception(exc) from exc
    finally:
        try:
            conn.autocommit, conn.isolation_level = previous
        except Exception:  # pragma: no cover - 连接已死时无法恢复
            pass
        try:
            connection_pool.putconn(conn)
        except Exception:  # pragma: no cover - 归还失败不应掩盖业务异常
            pass


def _safe_rollback(conn):
    try:
        conn.rollback()
    except Exception:  # pragma: no cover - 连接已死时回滚失败是预期
        pass


def _map_commit_exception(exc):
    """COMMIT 失败：连接类故障 → 结果未知；约束类 → 并发冲突。"""
    psycopg, _, _ = _mods()
    if isinstance(exc, (psycopg.errors.UniqueViolation, psycopg.errors.ExclusionViolation)):
        return ConcurrentConflict(f"提交违反唯一约束：{_safe_exc(exc)}")
    if isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)):
        return CommitOutcomeUnknown(
            "COMMIT 期间连接中断，提交结果未知；请按业务 ID/dedup 重新核验，不要自动重试"
        )
    return _map_exception(exc)


def _map_exception(exc):
    """psycopg 异常 → 基础层语义异常（提案 §4.1）。"""
    if isinstance(exc, (PgError, runtime_mode.ReadOnlyMode, runtime_mode.ConfigError)):
        return exc
    try:
        psycopg, psycopg_pool, _ = _mods()
    except PgConfigError as config_exc:
        return config_exc

    pool_timeout = getattr(psycopg_pool, "PoolTimeout", None)
    pool_closed = getattr(psycopg_pool, "PoolClosed", None)
    for candidate in (pool_timeout, pool_closed):
        if candidate is not None and isinstance(exc, candidate):
            return StorageUnavailable(f"连接池不可用：{_safe_exc(exc)}")
    if isinstance(exc, psycopg.errors.LockNotAvailable):
        return StorageUnavailable("系统正在维护或存在长时间写事务，请稍后重试")
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return StorageUnavailable("数据库语句超时，请稍后重试")
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return PgPermissionError(f"运行账号权限不足：{_safe_exc(exc)}")
    if isinstance(
        exc,
        (
            psycopg.errors.UniqueViolation,
            psycopg.errors.ExclusionViolation,
            psycopg.errors.SerializationFailure,
            psycopg.errors.DeadlockDetected,
            psycopg.errors.ForeignKeyViolation,
            psycopg.errors.NotNullViolation,
            psycopg.errors.CheckViolation,
        ),
    ):
        return ConcurrentConflict(f"并发冲突或约束不满足：{_safe_exc(exc)}")
    if isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)):
        return StorageUnavailable(f"PostgreSQL 不可达：{_safe_exc(exc)}")
    return exc


def healthcheck():
    """只读探针：返回脱敏状态字典，不抛连接异常（诊断入口）。"""
    started = time.monotonic()
    status = {"engine": "postgres", "reachable": False, "schema_version": None}
    try:
        with pg_transaction(readonly=True) as conn:
            row = conn.execute(
                "SELECT current_database() AS db, current_user AS usr, "
                "current_setting('server_version') AS server_version, "
                "current_setting('TimeZone') AS tz"
            ).fetchone()
            status.update(
                {
                    "reachable": True,
                    "database": row["db"],
                    "user": row["usr"],
                    "server_version": row["server_version"],
                    "timezone": row["tz"],
                }
            )
            try:
                version_row = conn.execute(
                    "SELECT MAX(version) AS v FROM schema_migrations WHERE version >= 12"
                ).fetchone()
                status["schema_version"] = version_row["v"]
            except Exception:
                status["schema_version"] = None
    except Exception as exc:
        status["error"] = type(_map_exception(exc)).__name__
    finally:
        status["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
    return status
