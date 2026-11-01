# -*- coding: utf-8 -*-
"""`scripts/pg_schema.py` CLI 测试（真 PG16 隔离库；PG-03）。"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pg_migrations  # noqa: E402  (conftest 已把 backend 加进 sys.path)
from pg03_test_fixtures import (  # noqa: E402
    drop_database,
    fresh_database,
    pg_env,
    run_cli,
)
from pg_test_support import connect, create_app_role, drop_app_role  # noqa: E402


@pytest.fixture()
def fresh_db(pg_container, pg_dsn):
    admin = connect(pg_dsn, autocommit=True)
    name = fresh_database(admin)
    yield name
    drop_database(admin, name)
    admin.close()


def _out(proc):
    return json.loads(proc.stdout)


def test_check_on_empty_database_is_incompatible(fresh_db, pg_container):
    env = pg_env(pg_container, fresh_db)
    proc = run_cli("pg_schema.py", ["check"], env)
    assert proc.returncode == 5
    payload = _out(proc)
    assert payload["ok"] is False
    assert set(payload["report"]["tables_missing"]) >= {"ledger_tx", "renewals"}


def test_migrate_is_idempotent_and_check_passes(fresh_db, pg_container):
    env = pg_env(pg_container, fresh_db)
    first = run_cli("pg_schema.py", ["migrate"], env)
    assert first.returncode == 0, first.stderr
    assert _out(first)["applied_versions"] == [v for v, _, _ in pg_migrations.PG_MIGRATIONS]
    second = run_cli("pg_schema.py", ["migrate"], env)  # 幂等
    assert second.returncode == 0
    assert _out(second)["applied_versions"] == []
    check = run_cli("pg_schema.py", ["check"], env)
    assert check.returncode == 0
    report = _out(check)["report"]
    assert report["compatible"] is True
    assert report["checksum_ok"] is True
    assert report["sqlite_versions"] == []
    # 脱敏：输出不得含密码
    assert pg_container.password not in check.stdout


def test_migrate_refuses_tampered_checksum(fresh_db, pg_container):
    env = pg_env(pg_container, fresh_db)
    assert run_cli("pg_schema.py", ["migrate"], env).returncode == 0
    admin = connect(pg_container.dsn(dbname=fresh_db), autocommit=True)
    admin.execute("UPDATE schema_migrations SET checksum = 'deadbeef' WHERE version = 12")
    admin.close()
    proc = run_cli("pg_schema.py", ["migrate"], env)
    assert proc.returncode == 5
    assert "指纹" in _out(proc)["error"]


def test_check_app_role_privilege_surface(fresh_db, pg_container):
    """既有表的权限面核对（夹具角色）。

    PG-05 review B-5 后 `privileges_ok` **同时**包含未来表默认权限：本用例用
    `create_app_role` 夹具建的角色没有（也不该有）默认权限，所以这里断言的是
    既有表部分，并显式确认默认权限项如实报 False——不再假装整体通过。
    """
    env = pg_env(pg_container, fresh_db)
    assert run_cli("pg_schema.py", ["migrate"], env).returncode == 0
    role = f"pg03_app_{fresh_db[-6:]}"
    admin = connect(pg_container.dsn(dbname=fresh_db), autocommit=True)
    create_app_role(admin, role, "pw-pg03-test",
                    tables=pg_migrations.SCHEMA_TABLES, database=fresh_db)
    admin.close()
    try:
        proc = run_cli("pg_schema.py", ["check", "--app-role", role], env)
        payload = _out(proc)
        entry = payload["app_roles"][role]
        assert entry["superuser"] is False
        assert entry["schema_create"] is False
        assert entry["table_grants"]["ledger_tx"] == {
            "select": True, "insert": True, "update": True, "delete": True}
        # §6.3：迁移记录对运行账号只读
        assert entry["table_grants"]["schema_migrations"] == {
            "select": True, "insert": False, "update": False, "delete": False}
        # 夹具角色没有 ALTER DEFAULT PRIVILEGES：如实报不通过，不再是「假绿」
        assert entry["default_privileges"]["future_table_dml_ok"] is False
        assert payload["privileges_ok"] is False
        assert proc.returncode == 5
    finally:
        # 先删库（表级 grant 随库消失），再删角色；顺序反了会 DependentObjectsStillExist
        cleanup = connect(pg_container.dsn(), autocommit=True)
        cleanup.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            (fresh_db,),
        )
        cleanup.execute(f'DROP DATABASE IF EXISTS "{fresh_db}"')
        drop_app_role(cleanup, role)
        cleanup.close()


# --- §6.3 角色矩阵引导（PG-05 review B-1 回归） -------------------------------


def test_bootstrap_roles_creates_matrix_and_future_table_privileges(fresh_db, pg_container):
    """`bootstrap-roles` 建全套身份，且**未来**迁移表对运行账号自动可写（§6.3）。

    这是 B-1 的核心验收：缺陷原状下 19 张表属主是 admin、无 DEFAULT PRIVILEGES，
    下一次迁移新建的表对 `console_nas`/`console_mac` 直接 `InsufficientPrivilege`。
    """
    env = pg_env(pg_container, fresh_db)
    proc = run_cli("pg_schema.py", ["bootstrap-roles"], env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    summary = _out(proc)
    assert summary["ok"] is True
    assert summary["owner"] == pg_migrations.OWNER_ROLE
    assert summary["default_privileges_for"] == pg_migrations.OWNER_ROLE

    # 幂等：再跑一次仍成功（不因角色已存在而失败）
    again = run_cli("pg_schema.py", ["bootstrap-roles"], env)
    assert again.returncode == 0, again.stdout + again.stderr

    admin = connect(pg_container.dsn(dbname=fresh_db), autocommit=True)
    try:
        # owner 必须 NOLOGIN 且不可越权（§6.3）
        row = admin.execute(
            "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole "
            "FROM pg_roles WHERE rolname = %s",
            (pg_migrations.OWNER_ROLE,),
        ).fetchone()
        assert row is not None, "console_owner 未创建"
        assert row["rolcanlogin"] is False, "console_owner 必须 NOLOGIN"
        assert row["rolsuper"] is False and row["rolcreatedb"] is False
        assert row["rolcreaterole"] is False

        # admin 必须是 owner 成员，否则 SET ROLE 会被拒（迁移路径依赖）
        member = admin.execute(
            "SELECT pg_has_role(%s, %s, 'MEMBER') AS m",
            (pg_migrations.ADMIN_ROLE, pg_migrations.OWNER_ROLE),
        ).fetchone()["m"]
        assert member is True, "console_admin 必须是 console_owner 的成员"

        # schema 属主归 owner，且 PUBLIC 的 CREATE 已撤（§6.3 原文）
        owner_row = admin.execute(
            "SELECT pg_get_userbyid(nspowner) AS owner FROM pg_namespace "
            "WHERE nspname = 'public'"
        ).fetchone()
        assert owner_row["owner"] == pg_migrations.OWNER_ROLE
        pub_create = admin.execute(
            "SELECT has_schema_privilege('public', 'public', 'CREATE') AS c"
        ).fetchone()["c"]
        assert pub_create is False, "PUBLIC 不应保留 public schema 的 CREATE"
    finally:
        admin.close()

    # 关键一步：以 owner 身份**新建一张未来表**，验证默认权限（ALTER DEFAULT PRIVILEGES）
    owner_conn = connect(pg_container.dsn(dbname=fresh_db), autocommit=True)
    try:
        owner_conn.execute(f'SET ROLE "{pg_migrations.OWNER_ROLE}"')
        owner_conn.execute(
            "CREATE TABLE IF NOT EXISTS future_migration_table ("
            " id bigserial PRIMARY KEY, note TEXT)"
        )
        owner_conn.execute("RESET ROLE")
        created_owner = owner_conn.execute(
            "SELECT pg_get_userbyid(relowner) AS owner FROM pg_class "
            "WHERE relname = 'future_migration_table'"
        ).fetchone()["owner"]
        assert created_owner == pg_migrations.OWNER_ROLE

        for role in ("console_nas", "console_mac"):
            mask = pg_migrations.future_table_dml_ok(owner_conn, role,
                                                     "future_migration_table")
            assert mask["ok"] is True, f"{role} 对未来表缺 DML：{mask}"
    finally:
        owner_conn.close()


def test_migrate_creates_tables_as_owner(fresh_db, pg_container):
    """bootstrap 过的库上 `migrate` 以 console_owner 身份建表（§6.3 SET ROLE）。"""
    env = pg_env(pg_container, fresh_db)
    assert run_cli("pg_schema.py", ["bootstrap-roles"], env).returncode == 0
    proc = run_cli("pg_schema.py", ["migrate"], env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = _out(proc)
    assert payload["created_as_owner"] == pg_migrations.OWNER_ROLE

    admin = connect(pg_container.dsn(dbname=fresh_db), autocommit=True)
    try:
        owners = admin.execute(
            "SELECT DISTINCT pg_get_userbyid(relowner) AS owner FROM pg_class "
            "WHERE relkind = 'r' AND relnamespace = 'public'::regnamespace"
        ).fetchall()
        distinct = {row["owner"] for row in owners}
        assert distinct == {pg_migrations.OWNER_ROLE}, f"表属主不是 owner：{distinct}"
    finally:
        admin.close()


def test_check_reports_future_table_default_privileges(fresh_db, pg_container):
    """`check --app-role` 必须核对**未来表默认权限**（PG-05 review B-5 回归）。

    缺陷原状：只查既有表的授予情况，`ALTER DEFAULT PRIVILEGES` 缺失时仍报
    `privileges_ok: true`——B-1 的 blocker 正落在这个盲区。
    """
    env = pg_env(pg_container, fresh_db)
    # 未 bootstrap：库属主不是 owner，未来表默认权限不成立 → check 必须判不通过
    assert run_cli("pg_schema.py", ["migrate"], env).returncode == 0
    role = f"pg03_dacl_{fresh_db[-6:]}"
    admin = connect(pg_container.dsn(dbname=fresh_db), autocommit=True)
    create_app_role(admin, role, "pw-pg03-test",
                    tables=pg_migrations.SCHEMA_TABLES, database=fresh_db)
    admin.close()
    try:
        before = run_cli("pg_schema.py", ["check", "--app-role", role], env)
        payload_before = _out(before)
        entry_before = payload_before["app_roles"][role]
        assert entry_before["default_privileges"]["future_table_dml_ok"] is False, (
            "未 bootstrap 时不该报告「未来表权限正确」"
        )
    finally:
        cleanup = connect(pg_container.dsn(), autocommit=True)
        cleanup.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            (fresh_db,),
        )
        cleanup.execute(f'DROP DATABASE IF EXISTS "{fresh_db}"')
        drop_app_role(cleanup, role)
        cleanup.close()


def test_check_passes_default_privileges_after_bootstrap(fresh_db, pg_container):
    """bootstrap 之后同一条 check 必须翻转为通过——闭环证据。"""
    env = pg_env(pg_container, fresh_db)
    assert run_cli("pg_schema.py", ["bootstrap-roles"], env).returncode == 0
    assert run_cli("pg_schema.py", ["migrate"], env).returncode == 0
    proc = run_cli("pg_schema.py", ["check", "--app-role", "console_nas",
                                    "--app-role", "console_mac"], env)
    assert proc.returncode == 0, proc.stdout
    payload = _out(proc)
    assert payload["privileges_ok"] is True, payload
    for role in ("console_nas", "console_mac"):
        entry = payload["app_roles"][role]
        assert entry["default_privileges"]["future_table_dml_ok"] is True, entry
        assert entry["default_privileges"]["owner"] == pg_migrations.OWNER_ROLE
