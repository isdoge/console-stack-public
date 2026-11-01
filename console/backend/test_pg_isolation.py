# -*- coding: utf-8 -*-
"""测试底座的隔离保护（提案 §3.4）：拒绝生产目标，缺 PG 就失败而不是伪 skip。"""
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import pg_test_support  # noqa: E402

PROD_DSN = (
    "host=100.64.0.10 port=5432 dbname=console "
    "user=console_nas password=whatever"
)


@pytest.mark.parametrize(
    "host,dbname,user",
    [
        ("100.64.0.10", "console_test", "console_test_admin"),
        ("192.168.50.100", "console_test", "console_test_admin"),
        ("console-postgres", "console_test", "console_test_admin"),
        ("127.0.0.1", "console", "console_test_admin"),
        ("127.0.0.1", "console_test", "console_nas"),
        ("127.0.0.1", "console_test", "console_mac"),
        ("127.0.0.1", "console_test", "console_admin"),
        ("127.0.0.1", "console_test", "console_owner"),
    ],
)
def test_production_identities_are_rejected(host, dbname, user):
    with pytest.raises(pg_test_support.ProductionTargetError):
        pg_test_support.assert_not_production(host, dbname, user)


def test_isolation_guard_accepts_only_test_identities():
    pg_test_support.assert_not_production(
        "127.0.0.1", "console_test", "console_test_admin"
    )


def test_production_dsn_is_rejected_before_dialling():
    with pytest.raises(pg_test_support.ProductionTargetError):
        pg_test_support.assert_dsn_not_production(PROD_DSN)
    with pytest.raises(pg_test_support.ProductionTargetError):
        pg_test_support.connect(PROD_DSN)


def test_case_insensitive_rejection():
    with pytest.raises(pg_test_support.ProductionTargetError):
        pg_test_support.assert_not_production("100.64.0.10", "console_test", "x")


def test_start_pg16_fails_when_docker_is_unavailable(monkeypatch):
    monkeypatch.delenv("CONSOLE_PG_TEST_NATIVE_BIN", raising=False)
    monkeypatch.setattr(pg_test_support, "docker_available", lambda: False)
    with pytest.raises(pg_test_support.TestPgUnavailable):
        pg_test_support.start_pg16()


def test_explicit_native_backend_refuses_missing_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_PG_TEST_NATIVE_BIN", str(tmp_path))
    monkeypatch.setattr(pg_test_support, "docker_available", lambda: False)
    with pytest.raises(pg_test_support.TestPgUnavailable, match="原生 PG16 工具"):
        pg_test_support.start_pg16()


def test_started_pg_instances_do_not_share_database_or_port():
    """恢复演练要求独立实例，不能仅换同一实例的 database 名。"""
    instances = []
    try:
        for _ in range(2):
            instances.append(pg_test_support.start_pg16())
        first, second = instances
        assert first.host == second.host == "127.0.0.1"
        assert first.port != second.port
        assert first.dbname == second.dbname == pg_test_support.TEST_DB
        for instance, marker in zip(instances, (1, 2)):
            with pg_test_support.connect(instance.dsn(), autocommit=True) as conn:
                version = conn.execute("SHOW server_version_num").fetchone()
                assert 160000 <= int(version["server_version_num"]) < 170000
                conn.execute("CREATE TABLE isolation_probe (marker INTEGER)")
                conn.execute("INSERT INTO isolation_probe VALUES (%s)", (marker,))
        for instance, marker in zip(instances, (1, 2)):
            with pg_test_support.connect(instance.dsn()) as conn:
                rows = conn.execute("SELECT marker FROM isolation_probe").fetchall()
                assert rows == [{"marker": marker}]
    finally:
        for instance in reversed(instances):
            instance.stop()


def test_container_fixture_uses_test_identities_only(pg_container):
    """真起容器时，身份必须是测试身份——防止夹具本身被改成连生产。"""
    pg_test_support.assert_not_production(
        pg_container.host, pg_container.dbname, pg_container.user
    )
    assert pg_container.dbname != "console"
    assert pg_container.user not in pg_test_support.PRODUCTION_USERS


def test_two_fixtures_are_independent_sessions(pg_conn, pg_conn_two):
    """两个独立连接样例：各自的 backend pid 不同，事务互不干扰。"""
    first = pg_conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
    second = pg_conn_two.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
    assert first != second
    pg_conn.execute("CREATE TEMP TABLE only_first (x INTEGER)")
    pg_conn.commit()
    with pytest.raises(Exception):
        pg_conn_two.execute("SELECT * FROM only_first")
    pg_conn_two.rollback()


def test_subprocess_sample_runs_against_isolated_database(pg_container):
    """独立进程样例：子进程用同一隔离 DSN，证明跨进程并发可测。"""
    source = f"""
import sys
sys.path.insert(0, {str(BACKEND_DIR)!r})
import pg_test_support
conn = pg_test_support.connect({pg_container.dsn()!r})
print(conn.execute("SELECT current_database() AS db").fetchone()["db"])
"""
    code, out, err = pg_test_support.run_in_subprocess(source, timeout=60)
    assert code == 0, err
    assert out.strip() == pg_container.dbname
