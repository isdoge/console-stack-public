# -*- coding: utf-8 -*-
"""PG-02 的测试侧夹具（提案 §8.5：模块阶段可用测试侧契约替身）。

**为什么单独一个文件**：`conftest.py` 与 `pg_test_support.py` 是 PG-00 持续维护的公共件
（§8.6），PG-02 不得把业务夹具塞进去。这里只放「把 PG-00 底座接到 PG-02 模块」的胶水：
独立库 + schema12 + 受限运行账号 + 把 `pgdb` 的进程内池指向该库。

**夹具依赖只到 `pg_container`**：pytest 只在「conftest + 本模块命名空间」里解析夹具，
`pg02_support` 不是 conftest，所以这里的夹具**不能互相依赖**（`pg_client` 依赖
`pg02_db` 会报 `fixture not found`）。每个对外夹具都自带建库/清理，只依赖 conftest 已
暴露的 `pg_container`。测试文件 `from pg02_support import pg_client` 即可。

池是**进程内单例**（`pgdb._POOL`），所以每个测试必须：建唯一库 → 写环境变量 →
`db.pg_close()` 清掉上一个测试的池。少了最后一步，第二个测试会复用第一个测试的池，
连到已被 DROP 的库上，表现为「莫名其妙的 connection closed」而不是清晰的隔离错误。
"""
import contextlib
import os
import uuid

import pytest

import pg_migrations
import pg_test_support

ROLE = "console_pg02_app"
ROLE_PASSWORD = "console_pg02_app_pw"

_ENV_KEYS = (
    "CONSOLE_PG_HOST", "CONSOLE_PG_PORT", "CONSOLE_PG_DB",
    "CONSOLE_PG_USER", "CONSOLE_PG_PASSWORD",
    "CONSOLE_STORAGE_ENGINE", "CONSOLE_INSTANCE_ROLE", "CONSOLE_MAINTENANCE_MODE",
)


@contextlib.contextmanager
def _database(pg_container):
    """建独立库 + schema12 + 受限运行账号，并指向 `pgdb`；退出时全部清理。"""
    from unittest.mock import patch

    import db as dbmod

    admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
    db_name = f"console_pg02_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{db_name}"')
    admin.close()

    schema_conn = pg_test_support.connect(pg_container.dsn(dbname=db_name), autocommit=True)
    try:
        pg_migrations.apply(schema_conn)
    finally:
        schema_conn.close()

    admin_conn = pg_test_support.connect(pg_container.dsn(dbname=db_name), autocommit=True)
    try:
        pg_test_support.create_app_role(
            admin_conn,
            ROLE,
            ROLE_PASSWORD,
            tables=list(pg_migrations.SCHEMA_TABLES),
            database=db_name,
        )
    finally:
        admin_conn.close()

    info = {
        "db_name": db_name,
        "admin_dsn": pg_container.dsn(dbname=db_name),
        "app_dsn": pg_container.dsn(dbname=db_name, user=ROLE, password=ROLE_PASSWORD),
        "host": pg_container.host,
        "port": pg_container.port,
    }
    saved = {key: os.environ.get(key) for key in _ENV_KEYS}
    os.environ.update(
        {
            "CONSOLE_PG_HOST": info["host"],
            "CONSOLE_PG_PORT": str(info["port"]),
            "CONSOLE_PG_DB": db_name,
            "CONSOLE_PG_USER": ROLE,
            "CONSOLE_PG_PASSWORD": ROLE_PASSWORD,
            "CONSOLE_STORAGE_ENGINE": "postgres",
            "CONSOLE_INSTANCE_ROLE": "client",
            "CONSOLE_MAINTENANCE_MODE": "normal",
        }
    )
    dbmod.pg_close()
    try:
        # 存储接线测试隔离 NAS 开关读取；真实权威门禁由 test_plugins 覆盖。
        with patch("plugin_runtime.authority_state", return_value={"enabled": True, "available": True}):
            yield info
    finally:
        dbmod.pg_close()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        admin_conn = pg_test_support.connect(pg_container.dsn(dbname=db_name), autocommit=True)
        admin_conn.execute(f'DROP OWNED BY "{ROLE}" CASCADE')
        admin_conn.close()
        admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
        admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        admin.execute(f'DROP ROLE IF EXISTS "{ROLE}"')
        admin.close()


@pytest.fixture()
def pg_client(pg_container):
    """`client` 角色的 PG 环境（不采集，但可业务写）。"""
    with _database(pg_container) as info:
        yield info


@pytest.fixture()
def pg_authority(pg_container):
    """`authority` 角色的 PG 环境（允许自动采集）。"""
    with _database(pg_container) as info:
        previous_role = os.environ.get("CONSOLE_INSTANCE_ROLE")
        os.environ["CONSOLE_INSTANCE_ROLE"] = "authority"
        try:
            yield info
        finally:
            # 恢复原值而非写死 client：写死会污染后续非 client 测试
            # （如 settings_store 的 PUT 会被 forward_observation_to_authority 转发）。
            if previous_role is None:
                os.environ.pop("CONSOLE_INSTANCE_ROLE", None)
            else:
                os.environ["CONSOLE_INSTANCE_ROLE"] = previous_role


@pytest.fixture()
def pg_readonly(pg_container):
    """`read_only` 维护模式的 PG 环境（全部业务写被拒）。"""
    with _database(pg_container) as info:
        previous_mode = os.environ.get("CONSOLE_MAINTENANCE_MODE")
        os.environ["CONSOLE_MAINTENANCE_MODE"] = "read_only"
        try:
            yield info
        finally:
            if previous_mode is None:
                os.environ.pop("CONSOLE_MAINTENANCE_MODE", None)
            else:
                os.environ["CONSOLE_MAINTENANCE_MODE"] = previous_mode


def admin_conn(pg02_db):
    """管理连接：测试要绕过运行账号权限直接断言表内容时用。"""
    return pg_test_support.connect(pg02_db["admin_dsn"], autocommit=True)


def count_rows(pg02_db, table, where="", params=()):
    conn = admin_conn(pg02_db)
    try:
        sql = f'SELECT COUNT(*) AS c FROM public."{table}"'
        if where:
            sql += f" WHERE {where}"
        return int(conn.execute(sql, params).fetchone()["c"])
    finally:
        conn.close()


def fetch_all(pg02_db, sql, params=()):
    conn = admin_conn(pg02_db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()
