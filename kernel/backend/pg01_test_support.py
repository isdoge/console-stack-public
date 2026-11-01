# -*- coding: utf-8 -*-
"""PG-01 的账本域测试夹具（合成数据 + 真 PG16 隔离库）。

放在这里而不是 `conftest.py`：公共夹具归 PG-00 owner（§8.6），本文件只服务 PG-01 的
测试模块，随本任务一起交付。

每个测试拿一个**全新数据库**（schema12 + 基础字典），因此测试之间不会互相污染；
池在测试前后重置，避免上一个库的连接被复用。
"""
import uuid

import pytest

import pg_migrations
import pg_test_support
import pgdb

# 基础分类字典：多数账本测试都要用，统一在这里建。
BASE_CATEGORIES = (
    ("餐饮", "三餐", 1),
    ("生活", "日用", 2),
    ("收入", "工资", 3),
    ("测试支出", "日用", 4),
)


def _create_database(pg_container, prefix, categories=()):
    admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
    name = f"console_test_{prefix}_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    admin.close()
    conn = pg_test_support.connect(pg_container.dsn(dbname=name), autocommit=True)
    try:
        pg_migrations.apply(conn)
        if categories:
            conn.execute(
                "INSERT INTO ledger_categories(level1,level2,sort) VALUES "
                + ",".join(["(%s,%s,%s)"] * len(categories)),
                [value for row in categories for value in row],
            )
    finally:
        conn.close()
    return name


def _drop_database(pg_container, name):
    admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.close()


@pytest.fixture()
def empty_ledger_env(pg_container, monkeypatch):
    """账本域环境，但**不预置任何业务数据**（供「目标必须为空」的迁移测试用）。"""
    name = _create_database(pg_container, "pg01empty")
    values = {
        "CONSOLE_PG_HOST": pg_container.host,
        "CONSOLE_PG_PORT": str(pg_container.port),
        "CONSOLE_PG_DB": name,
        "CONSOLE_PG_USER": pg_container.user,
        "CONSOLE_PG_PASSWORD": pg_container.password,
        "CONSOLE_STORAGE_ENGINE": "postgres",
        "CONSOLE_INSTANCE_ROLE": "test",
        "CONSOLE_MAINTENANCE_MODE": "normal",
        "CONSOLE_PG_POOL_MIN": "1",
        "CONSOLE_PG_POOL_MAX": "4",
        "CONSOLE_PG_LOCK_TIMEOUT": "1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    pgdb.close_pool()
    try:
        yield pg_container.dsn(dbname=name)
    finally:
        pgdb.close_pool()
        _drop_database(pg_container, name)


@pytest.fixture()
def ledger_env(pg_container, monkeypatch):
    """账本域环境：独立库 + 环境变量指向它；`yield` 出本测试库的 DSN。"""
    name = _create_database(pg_container, "pg01", BASE_CATEGORIES)
    values = {
        "CONSOLE_PG_HOST": pg_container.host,
        "CONSOLE_PG_PORT": str(pg_container.port),
        "CONSOLE_PG_DB": name,
        "CONSOLE_PG_USER": pg_container.user,
        "CONSOLE_PG_PASSWORD": pg_container.password,
        "CONSOLE_STORAGE_ENGINE": "postgres",
        "CONSOLE_INSTANCE_ROLE": "test",
        "CONSOLE_MAINTENANCE_MODE": "normal",
        "CONSOLE_PG_POOL_MIN": "1",
        "CONSOLE_PG_POOL_MAX": "4",
        "CONSOLE_PG_LOCK_TIMEOUT": "1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    pgdb.close_pool()
    try:
        yield pg_container.dsn(dbname=name)
    finally:
        pgdb.close_pool()
        _drop_database(pg_container, name)


@pytest.fixture()
def files_env(pg_container, tmp_path, monkeypatch):
    """文件批次环境：独立库 + 独立私有归档目录（不与其它任务共享目录）。

    同时把 `paths.PAY_DATA_DIR` 指向该目录：被测代码里有不显式传 `data_dir` 的调用点，
    默认值必须落在测试目录而不是开发机真账本的 `pay/`。
    """
    import paths

    monkeypatch.setattr(paths, "PAY_DATA_DIR", str(tmp_path / "pay"))
    name = _create_database(pg_container, "pg01files")
    values = {
        "CONSOLE_PG_HOST": pg_container.host,
        "CONSOLE_PG_PORT": str(pg_container.port),
        "CONSOLE_PG_DB": name,
        "CONSOLE_PG_USER": pg_container.user,
        "CONSOLE_PG_PASSWORD": pg_container.password,
        "CONSOLE_STORAGE_ENGINE": "postgres",
        "CONSOLE_INSTANCE_ROLE": "test",
        "CONSOLE_MAINTENANCE_MODE": "normal",
        "CONSOLE_PG_POOL_MIN": "1",
        "CONSOLE_PG_POOL_MAX": "4",
        "CONSOLE_PG_LOCK_TIMEOUT": "1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    pgdb.close_pool()
    try:
        yield {
            "data_dir": tmp_path / "pay",
            "dsn": pg_container.dsn(dbname=name),
            "database": name,
        }
    finally:
        pgdb.close_pool()
        _drop_database(pg_container, name)


@pytest.fixture()
def holder(pg_container):
    """第二条独立连接：用于持锁/并发断言。

    调用方必须自己 `SET LOCAL lock_timeout` 并显式 rollback 释放事务级锁。
    """
    connections = []

    def _open(dsn):
        conn = pg_test_support.connect(dsn)
        connections.append(conn)
        return conn

    yield _open
    for conn in connections:
        try:
            conn.close()
        except Exception:  # pragma: no cover - 已关闭时忽略
            pass
