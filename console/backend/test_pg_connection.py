# -*- coding: utf-8 -*-
"""pgdb 连接/事务/锁/异常映射的真 PG16 回归（提案 §4.1/§4.2）。

这些测试**必须**连真 PG16：SQLite 或 mock 无法证明 advisory lock 互斥、只读事务
真的拒写、COMMIT 未知语义。夹具缺 PG 时 `pytest.fail`（不伪 skip）。
"""
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import pg_locks  # noqa: E402
import pg_migrations  # noqa: E402
import pg_test_support  # noqa: E402
import pgdb  # noqa: E402
import runtime_mode  # noqa: E402


@pytest.fixture(scope="module")
def schema(pg_dsn):
    conn = pg_test_support.connect(pg_dsn, autocommit=True)
    try:
        pg_migrations.apply(conn)
    finally:
        conn.close()
    return True


@pytest.fixture()
def pg_env(pg_container, monkeypatch):
    """把 pgdb 指向隔离容器；每个测试前后重置池，避免串味。"""
    values = {
        "CONSOLE_PG_HOST": pg_container.host,
        "CONSOLE_PG_PORT": str(pg_container.port),
        "CONSOLE_PG_DB": pg_container.dbname,
        "CONSOLE_PG_USER": pg_container.user,
        "CONSOLE_PG_PASSWORD": pg_container.password,
        "CONSOLE_STORAGE_ENGINE": "postgres",
        "CONSOLE_INSTANCE_ROLE": "test",
        "CONSOLE_MAINTENANCE_MODE": "normal",
        "CONSOLE_PG_POOL_MIN": "1",
        "CONSOLE_PG_POOL_MAX": "4",
        "CONSOLE_PG_ACQUIRE_TIMEOUT": "2",
        "CONSOLE_PG_LOCK_TIMEOUT": "1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    pgdb.close_pool()
    yield pg_container
    pgdb.close_pool()


# --- 配置 -------------------------------------------------------------------

def test_missing_config_raises_config_error_and_never_falls_back_to_sqlite(monkeypatch):
    for name in ("CONSOLE_PG_HOST", "CONSOLE_PG_USER", "CONSOLE_PG_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(pgdb.PgConfigError) as excinfo:
        pgdb.PgConfig.from_env()
    assert "CONSOLE_PG_HOST" in str(excinfo.value)


def test_config_repr_and_summary_never_leak_password():
    config = pgdb.PgConfig(
        host="127.0.0.1", port=5432, dbname="console_test",
        user="console_test_admin", password="top-secret-pw",
    )
    assert "top-secret-pw" not in repr(config)
    assert "top-secret-pw" not in config.safe_summary()
    assert "top-secret-pw" in config.conninfo()


# --- 事务 -------------------------------------------------------------------

def test_write_transaction_commits_and_reads_back(pg_env, schema):
    with pgdb.pg_transaction() as conn:
        conn.execute(
            "INSERT INTO dashboard_layout (widget_key, x, y, w) "
            "VALUES ('pg00-commit', 1, 2, 3) "
            "ON CONFLICT (widget_key) DO UPDATE SET x = EXCLUDED.x"
        )
    with pgdb.pg_transaction(readonly=True) as conn:
        row = conn.execute(
            "SELECT x, y, w FROM dashboard_layout WHERE widget_key = 'pg00-commit'"
        ).fetchone()
    assert row == {"x": 1, "y": 2, "w": 3}


def test_write_transaction_rolls_back_on_exception(pg_env, schema):
    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with pgdb.pg_transaction() as conn:
            conn.execute(
                "INSERT INTO dashboard_layout (widget_key, x, y, w) "
                "VALUES ('pg00-rollback', 9, 9, 9) "
                "ON CONFLICT (widget_key) DO UPDATE SET x = EXCLUDED.x"
            )
            raise Boom()
    with pgdb.pg_transaction(readonly=True) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM dashboard_layout WHERE widget_key = 'pg00-rollback'"
        ).fetchone()
    assert row["n"] == 0


def test_readonly_transaction_rejects_writes_at_the_database(pg_env, schema):
    import psycopg

    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        with pgdb.pg_transaction(readonly=True) as conn:
            conn.execute(
                "INSERT INTO dashboard_layout (widget_key, x, y, w) "
                "VALUES ('pg00-readonly', 1, 1, 1)"
            )


def test_readonly_transaction_uses_repeatable_read_snapshot(pg_env, schema):
    with pgdb.pg_transaction(readonly=True) as conn:
        row = conn.execute("SHOW transaction_isolation").fetchone()
        assert row["transaction_isolation"] == "repeatable read"


def test_maintenance_read_only_mode_blocks_writes_before_connecting(pg_env, schema,
                                                                    monkeypatch):
    monkeypatch.setenv("CONSOLE_MAINTENANCE_MODE", "read_only")
    with pytest.raises(runtime_mode.ReadOnlyMode):
        with pgdb.pg_transaction():
            pass
    # 只读事务在 read_only 模式下仍可用（提案 §7.3：纯读取可在冻结期做验证）
    with pgdb.pg_transaction(readonly=True) as conn:
        assert conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1


def test_statement_timeout_is_bounded_and_maps_to_storage_unavailable(pg_env, schema):
    with pytest.raises(pgdb.StorageUnavailable):
        with pgdb.pg_transaction(readonly=True, statement_timeout=1) as conn:
            conn.execute("SELECT pg_sleep(5)")


# --- 锁 ---------------------------------------------------------------------

def test_ledger_lock_serialises_two_independent_connections(pg_env, schema):
    import psycopg

    holder = pg_test_support.connect(pg_env.dsn())
    try:
        holder.execute("SET LOCAL lock_timeout = '1s'")
        holder.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                       (pg_locks.LOCK_NAMESPACE, pg_locks.LEDGER.key))
        with pytest.raises(pgdb.StorageUnavailable):
            with pgdb.pg_transaction(lock=pg_locks.LEDGER) as conn:
                conn.execute("SELECT 1")
        holder.rollback()
    finally:
        holder.close()
    # 释放后同一把锁可以正常取得
    with pgdb.pg_transaction(lock=pg_locks.LEDGER) as conn:
        assert conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
    assert issubclass(psycopg.errors.LockNotAvailable, Exception)


def test_write_transaction_takes_maintenance_shared_lock(pg_env, schema):
    with pgdb.pg_transaction() as conn:
        rows = conn.execute(
            "SELECT classid, objid, mode FROM pg_locks "
            "WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
        ).fetchall()
    held = {(row["classid"], row["objid"], row["mode"]) for row in rows}
    # 两参数形式在 pg_locks 里编码为 (classid=namespace, objid=key)；
    # PG 的共享 advisory lock 显示为 ShareLock（不是 "SharedLock"）。
    assert (pg_locks.LOCK_NAMESPACE, pg_locks.KEY_MAINTENANCE, "ShareLock") in held


def test_maintenance_exclusive_lock_freezes_business_write_transactions(pg_env, schema):
    """维护控制器持独占锁时，业务写事务必须在有界超时内失败（提案 §4.2）。"""
    holder = pg_test_support.connect(pg_env.dsn())
    try:
        holder.execute("SET LOCAL lock_timeout = '1s'")
        holder.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (pg_locks.LOCK_NAMESPACE, pg_locks.KEY_MAINTENANCE),
        )
        with pytest.raises(pgdb.StorageUnavailable):
            with pgdb.pg_transaction() as conn:
                conn.execute("SELECT 1")
        # 只读事务不取维护共享锁，冻结期间仍可做验证读取
        with pgdb.pg_transaction(readonly=True) as conn:
            assert conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
        holder.rollback()
    finally:
        holder.close()


def test_maintenance_exclusive_lock_blocks_writers_from_another_process(pg_env, schema):
    child = f"""
import sys
sys.path.insert(0, {str(BACKEND_DIR)!r})
import pgdb, pg_locks
try:
    with pgdb.pg_transaction(lock=pg_locks.LEDGER) as conn:
        conn.execute("SELECT 1")
    print("ACQUIRED")
except pgdb.StorageUnavailable:
    print("BLOCKED")
"""
    with pgdb.pg_transaction(lock=pg_locks.MAINTENANCE) as conn:
        conn.execute("SELECT 1")
        code, out, err = pg_test_support.run_in_subprocess(child, timeout=60)
    assert code == 0, err
    assert "BLOCKED" in out


def test_traffic_locks_are_per_machine(pg_env, schema):
    nas = pg_locks.traffic_lock("nas")
    mac = pg_locks.traffic_lock("mac")
    assert nas.key != mac.key
    holder = pg_test_support.connect(pg_env.dsn())
    try:
        holder.execute("SET lock_timeout = '1s'")
        holder.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                       (pg_locks.LOCK_NAMESPACE, nas.key))
        # 另一台机器的锁不受影响
        with pgdb.pg_transaction(lock=mac) as conn:
            assert conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
        with pytest.raises(pgdb.StorageUnavailable):
            with pgdb.pg_transaction(lock=nas) as conn:
                conn.execute("SELECT 1")
        holder.rollback()
    finally:
        holder.close()


# --- 异常与诊断 -------------------------------------------------------------

def test_pool_exhaustion_fails_bounded_not_hangs(pg_env, schema, monkeypatch):
    monkeypatch.setenv("CONSOLE_PG_POOL_MAX", "1")
    monkeypatch.setenv("CONSOLE_PG_ACQUIRE_TIMEOUT", "1")
    pgdb.close_pool()
    import time

    with pgdb.pg_connection() as conn:
        conn.execute("SELECT 1")
        started = time.monotonic()
        with pytest.raises(pgdb.StorageUnavailable):
            with pgdb.pg_connection() as second:
                second.execute("SELECT 1")
        assert time.monotonic() - started < 10


def test_unreachable_database_maps_to_storage_unavailable(monkeypatch):
    monkeypatch.setenv("CONSOLE_PG_HOST", "127.0.0.1")
    monkeypatch.setenv("CONSOLE_PG_PORT", "1")  # 不监听的端口
    monkeypatch.setenv("CONSOLE_PG_DB", "console_test")
    monkeypatch.setenv("CONSOLE_PG_USER", "console_test_admin")
    monkeypatch.setenv("CONSOLE_PG_PASSWORD", "pw")
    monkeypatch.setenv("CONSOLE_PG_ACQUIRE_TIMEOUT", "1")
    monkeypatch.setenv("CONSOLE_PG_CONNECT_TIMEOUT", "1")
    pgdb.close_pool()
    try:
        with pytest.raises(pgdb.StorageUnavailable):
            with pgdb.pg_transaction(readonly=True) as conn:
                conn.execute("SELECT 1")
    finally:
        pgdb.close_pool()


def test_healthcheck_is_redacted_and_reports_schema(pg_env, schema):
    status = pgdb.healthcheck()
    assert status["reachable"] is True
    assert status["timezone"] == "UTC"
    assert status["schema_version"] == pg_migrations.latest_schema_version()
    assert pg_env.password not in repr(status)


def test_healthcheck_reports_unreachable_without_raising(monkeypatch):
    monkeypatch.setenv("CONSOLE_PG_HOST", "127.0.0.1")
    monkeypatch.setenv("CONSOLE_PG_PORT", "1")
    monkeypatch.setenv("CONSOLE_PG_DB", "console_test")
    monkeypatch.setenv("CONSOLE_PG_USER", "console_test_admin")
    monkeypatch.setenv("CONSOLE_PG_PASSWORD", "pw")
    monkeypatch.setenv("CONSOLE_PG_ACQUIRE_TIMEOUT", "1")
    monkeypatch.setenv("CONSOLE_PG_CONNECT_TIMEOUT", "1")
    pgdb.close_pool()
    try:
        status = pgdb.healthcheck()
        assert status["reachable"] is False
        assert status["error"] in {"StorageUnavailable", "PgConfigError"}
    finally:
        pgdb.close_pool()
