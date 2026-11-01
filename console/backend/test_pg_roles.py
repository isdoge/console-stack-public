# -*- coding: utf-8 -*-
"""两个运行角色的权限面（提案 §6.3）：DML/序列可用，DDL/角色管理不可用，迁移表只读。"""
import pathlib
import sys
import uuid

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import pg_migrations  # noqa: E402
import pg_test_support  # noqa: E402

ROLE = "console_test_app"
ROLE_PASSWORD = "console_test_app_pw"


@pytest.fixture()
def role_db(pg_container):
    """独立库 + schema12 + 一个受限运行账号（模拟 console_nas/console_mac 的权限面）。"""
    admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
    db_name = f"console_test_roles_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{db_name}"')
    admin.close()

    conn = pg_test_support.connect(pg_container.dsn(dbname=db_name), autocommit=True)
    try:
        pg_migrations.apply(conn)
        tables = list(pg_migrations.SCHEMA_TABLES)
        # schema/表授权必须连到目标库执行：连默认库会把权限授到错误的库上，
        # 之后 DROP ROLE 会因为残留依赖失败。
        admin_conn = pg_test_support.connect(pg_container.dsn(dbname=db_name), autocommit=True)
        try:
            pg_test_support.create_app_role(
                admin_conn, ROLE, ROLE_PASSWORD, tables=tables, database=db_name
            )
        finally:
            admin_conn.close()
        yield {
            "dsn": pg_container.dsn(dbname=db_name, user=ROLE, password=ROLE_PASSWORD),
            "admin_dsn": pg_container.dsn(dbname=db_name),
            "db_name": db_name,
        }
    finally:
        conn.close()
        admin = pg_test_support.connect(pg_container.dsn(dbname=db_name), autocommit=True)
        admin.execute(f'DROP OWNED BY "{ROLE}" CASCADE')
        admin.close()
        admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
        admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        admin.execute(f'DROP ROLE IF EXISTS "{ROLE}"')
        admin.close()


@pytest.fixture()
def role_conn(role_db):
    conn = pg_test_support.connect(role_db["dsn"])
    try:
        yield conn
    finally:
        conn.close()


def test_role_can_do_business_dml(role_conn):
    role_conn.execute(
        "INSERT INTO dashboard_layout (widget_key, x, y, w) VALUES ('k1', 1, 2, 3)"
    )
    role_conn.execute("UPDATE dashboard_layout SET x = 9 WHERE widget_key = 'k1'")
    row = role_conn.execute(
        "SELECT x FROM dashboard_layout WHERE widget_key = 'k1'"
    ).fetchone()
    assert row["x"] == 9
    role_conn.execute("DELETE FROM dashboard_layout WHERE widget_key = 'k1'")
    role_conn.commit()


def test_role_can_use_sequences(role_conn):
    import psycopg

    role_conn.execute(
        "INSERT INTO probe_history (machine, ts, status) VALUES ('nas', 't', 'up')"
    )
    row = role_conn.execute("SELECT id FROM probe_history").fetchone()
    assert row["id"] >= 1
    role_conn.commit()
    assert psycopg.errors.InsufficientPrivilege is not None


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE evil (x INTEGER)",
        "DROP TABLE dashboard_layout",
        "ALTER TABLE dashboard_layout ADD COLUMN evil TEXT",
        "TRUNCATE dashboard_layout",
    ],
)
def test_role_cannot_run_ddl(role_conn, sql):
    import psycopg

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        role_conn.execute(sql)


def test_role_cannot_manage_roles(role_conn):
    import psycopg

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        role_conn.execute("CREATE ROLE evil_role LOGIN")
    # 语句失败后事务进入 aborted 状态，必须先回滚
    role_conn.rollback()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        role_conn.execute("ALTER ROLE console_test_app SUPERUSER")
    role_conn.rollback()


def test_role_can_read_but_not_write_schema_migrations(role_conn):
    import psycopg

    row = role_conn.execute(
        "SELECT MAX(version) AS v FROM schema_migrations"
    ).fetchone()
    assert row["v"] == pg_migrations.latest_schema_version()
    role_conn.commit()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        role_conn.execute("INSERT INTO schema_migrations (version) VALUES (99)")


def test_role_is_not_superuser_and_cannot_create_in_public(role_conn):
    import psycopg

    row = role_conn.execute(
        "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = %s",
        (ROLE,),
    ).fetchone()
    assert row == {"rolsuper": False, "rolcreatedb": False, "rolcreaterole": False}
    role_conn.commit()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        role_conn.execute("CREATE TABLE public.not_allowed (x INTEGER)")


def test_two_running_accounts_are_independent(pg_container, role_db):
    """NAS 与 Mac 是两个独立账号：撤销一个不影响另一个（提案 D8 的按设备撤销）。"""
    # 授权必须连到目标库执行（连默认库会把权限授到别的库上）
    admin_conn = pg_test_support.connect(role_db["admin_dsn"], autocommit=True)
    second = "console_test_app2"
    try:
        pg_test_support.create_app_role(
            admin_conn, second, "pw2",
            tables=["dashboard_layout"], database=role_db["db_name"],
        )
        first = pg_test_support.connect(role_db["dsn"])
        second_conn = pg_test_support.connect(
            pg_container.dsn(dbname=role_db["db_name"], user=second, password="pw2")
        )
        try:
            first.execute(
                "INSERT INTO dashboard_layout (widget_key, x, y, w) VALUES ('a', 1, 1, 1)"
            )
            first.commit()
            second_conn.execute(
                "INSERT INTO dashboard_layout (widget_key, x, y, w) VALUES ('b', 2, 2, 2)"
            )
            second_conn.commit()
            count = first.execute(
                "SELECT COUNT(*) AS n FROM dashboard_layout"
            ).fetchone()["n"]
            assert count == 2
        finally:
            first.close()
            second_conn.close()
        admin_conn.execute(f'DROP OWNED BY "{second}" CASCADE')
        admin_conn.execute(f'DROP ROLE IF EXISTS "{second}"')
    finally:
        admin_conn.close()
