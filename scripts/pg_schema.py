#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`scripts/pg_schema.py check|migrate|bootstrap-roles`（提案 §8.2/§6.3；契约 `pg00-1`）。

- `check`：只读核对 engine / schema12 / checksum / 表清单，可选 `--app-role` 权限面核对；
  输出脱敏 JSON（无 host/密码）。兼容 → 退出 0，不兼容 → 5。
- `migrate`：管理员连接 + 会话级迁移锁，追加 schema（不 import app、不启动后台）。
  幂等：已应用版本返回空列表；checksum 不符 → 拒绝升级（5）。
  bootstrap 过的库上以 `console_owner` 身份建表（§6.3：迁移先 SET ROLE 到 owner）。
- `bootstrap-roles`：建 §6.3 全套角色与权限（**幂等**），含未来表的
  `ALTER DEFAULT PRIVILEGES FOR ROLE console_owner`。密码不在此处设置——
  由部署侧 secret 文件另行配置，本命令不出现在任何日志里。

退出码（交接 PG-04 冻结）：0 成功 · 2 用法/配置错误 · 4 迁移锁忙 · 5 不兼容/checksum 不符。
"""
import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = ROOT / "console" / "backend"
for _path in (str(BACKEND), str(ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pg_migrations  # noqa: E402
from pg_migration_lib import (  # noqa: E402
    TargetGuardFailure,
    acquire_migration_lock,
    connect_target,
    release_migration_lock,
    target_config,
)


def _fail(message, code):
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), file=sys.stdout)
    return code


def _role_privileges(conn, role, tables):
    row = conn.execute(
        "SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = %s",
        (role,),
    ).fetchone()
    if row is None:
        return {"role": role, "exists": False}
    grants = {}
    for table in tables:
        check = conn.execute(
            "SELECT"
            " has_table_privilege(%s, 'public.' || quote_ident(%s), 'SELECT') AS s,"
            " has_table_privilege(%s, 'public.' || quote_ident(%s), 'INSERT') AS i,"
            " has_table_privilege(%s, 'public.' || quote_ident(%s), 'UPDATE') AS u,"
            " has_table_privilege(%s, 'public.' || quote_ident(%s), 'DELETE') AS d,"
            " has_schema_privilege(%s, 'public', 'CREATE') AS schema_create",
            (role, table, role, table, role, table, role, table, role),
        ).fetchone()
        grants[table] = {
            "select": check["s"], "insert": check["i"],
            "update": check["u"], "delete": check["d"],
        }
    schema_create = conn.execute(
        "SELECT has_schema_privilege(%s, 'public', 'CREATE') AS c", (role,)
    ).fetchone()["c"]
    return {
        "role": role,
        "exists": True,
        "can_login": row["rolcanlogin"],
        "superuser": row["rolsuper"],
        "schema_create": schema_create,
        "table_grants": grants,
    }


def _default_privileges(conn, roles=()):
    """核对 `ALTER DEFAULT PRIVILEGES FOR ROLE console_owner` 是否覆盖运行账号（§6.3）。

    **只读**：直接查 `pg_default_acl` 目录，不建探测表、不改任何对象——
    `check` 的契约是只读核对，不能为了自检而写库。

    返回 `owner_ok`（库属主是否 owner）与 `roles_missing_dml`：**每一个被查询的
    角色**都必须有完整的 r/a/w/d，缺失或缺登录角色本身都如实记入——对某个角色
    『查不到』绝不能等同于『没问题』（这正是 B-1 的缺陷形态）。
    """
    owner = pg_migrations.OWNER_ROLE
    result = {"owner": owner, "owner_ok": False, "roles_missing_dml": {}}

    row = conn.execute(
        "SELECT pg_get_userbyid(datdba) AS owner FROM pg_database "
        "WHERE datname = current_database()"
    ).fetchone()
    result["owner_ok"] = bool(row) and row["owner"] == owner

    # `defaclacl` 形如 {console_owner=arwdDxt/console_owner,console_nas=arwd/...}
    # 只看 TABLES（defaclobjtype='r'）。缺 a(INSERT)/w(UPDATE)/d(DELETE)/r(SELECT)
    # 任一项即视为未来表不可写。
    grants = {}
    for entry in conn.execute(
        "SELECT defaclacl::text[] AS acl FROM pg_default_acl d "
        "JOIN pg_namespace n ON n.oid = d.defaclnamespace "
        "JOIN pg_roles r ON r.oid = d.defaclrole "
        "WHERE n.nspname = 'public' AND d.defaclobjtype = 'r' AND r.rolname = %s",
        (owner,),
    ).fetchall():
        for item in entry["acl"] or []:
            grantee, _, privs = item.partition("=")
            grants[grantee] = privs
    for role in roles:
        exists = conn.execute(
            "SELECT 1 AS p FROM pg_roles WHERE rolname = %s", (role,)
        ).fetchone()
        if exists is None:
            result["roles_missing_dml"][role] = ["role-missing"]
            continue
        privs = grants.get(role, "")
        missing = [flag for flag in ("r", "a", "w", "d") if flag not in privs]
        if missing:
            result["roles_missing_dml"][role] = missing
    return result


def cmd_check(args):
    try:
        config = target_config()
    except Exception as exc:  # PgConfigError
        return _fail(f"配置缺失：{exc}", 2)
    conn = connect_target(config, statement_timeout=30)
    try:
        report = pg_migrations.verify(conn)
        engine_row = conn.execute("SELECT version() AS v, current_database() AS db").fetchone()
        output = {
            "ok": bool(report["compatible"]),
            "engine": "postgres",
            "server": "postgres",
            "database": engine_row["db"],
            "report": report,
        }
        if args.app_role:
            privileges = {}
            all_ok = True
            for role in args.app_role:
                entry = _role_privileges(conn, role, pg_migrations.SCHEMA_TABLES)
                privileges[role] = entry
                if not entry.get("exists") or entry.get("superuser") or entry.get("schema_create"):
                    all_ok = False
                for table, mask in entry.get("table_grants", {}).items():
                    if table == "schema_migrations":
                        if not mask["select"] or mask["insert"] or mask["update"]:
                            all_ok = False
                    elif not all(mask.get(key) for key in ("select", "insert", "update", "delete")):
                        all_ok = False
            # §6.3：既有权限与**未来默认权限**都要验证，不能只 GRANT 后凭推测报告
            # 成功（PG-05 review B-5：此前这一项完全没覆盖，B-1 的缺陷正落在此盲区）。
            defaults = _default_privileges(conn, roles=args.app_role)
            for role in args.app_role:
                entry = privileges.get(role)
                if not entry or not entry.get("exists"):
                    continue
                lacks = defaults["roles_missing_dml"].get(role, [])
                entry["default_privileges"] = {
                    "owner": defaults["owner"],
                    "future_table_dml_ok": not lacks,
                    "missing_privileges": lacks,
                }
                if lacks or not defaults["owner_ok"]:
                    all_ok = False
            output["app_roles"] = privileges
            output["privileges_ok"] = all_ok
            output["ok"] = bool(report["compatible"]) and all_ok
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0 if output["ok"] else 5
    finally:
        conn.close()


def cmd_migrate(args):
    try:
        config = target_config()
    except Exception as exc:
        return _fail(f"配置缺失：{exc}", 2)
    conn = connect_target(config, readonly=False, statement_timeout=args.statement_timeout)
    try:
        try:
            acquire_migration_lock(conn, timeout_s=args.lock_timeout)
        except TargetGuardFailure as exc:
            return _fail(str(exc), 4)
        try:
            try:
                with conn.transaction():
                    # §6.3：迁移先 SET ROLE 到实际建表者 console_owner。`apply_as_owner`
                    # 内部会建 `schema_migrations`（含在其中），**不能**在这里先以 admin
                    # 建表——那样切到 owner 后反而读不到该表。
                    outcome = pg_migrations.apply_as_owner(conn)
                    applied_now = outcome["applied"]
            except pg_migrations.SchemaChecksumMismatch as exc:
                return _fail(str(exc), 5)
            report = pg_migrations.verify(conn)
            print(json.dumps({
                "ok": bool(report["compatible"]),
                "applied_versions": applied_now,
                "created_as_owner": outcome["owner"],
                "report": report,
            }, ensure_ascii=False, sort_keys=True))
            return 0 if report["compatible"] else 5
        finally:
            release_migration_lock(conn)
    finally:
        conn.close()


def cmd_bootstrap_roles(args):
    """建 §6.3 全套角色与权限（幂等）。需要能建角色/授 DEFAULT PRIVILEGES 的身份。"""
    try:
        config = target_config()
    except Exception as exc:
        return _fail(f"配置缺失：{exc}", 2)
    conn = connect_target(config, readonly=False, statement_timeout=args.statement_timeout)
    try:
        try:
            with conn.transaction():
                summary = pg_migrations.bootstrap_roles(conn)
        except Exception as exc:
            text = str(exc)
            if "password" in text.lower():
                text = type(exc).__name__
            return _fail(f"角色引导失败：{type(exc).__name__}: {text}", 5)
        print(json.dumps({"ok": True, **summary}, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="只读核对 engine/schema/checksum/权限，脱敏输出")
    check.add_argument("--app-role", action="append", default=[],
                       help="额外核对某个运行账号的权限面（可重复）")
    migrate = sub.add_parser("migrate", help="管理员迁移锁 + 追加 schema；不 import app")
    migrate.add_argument("--lock-timeout", type=int, default=10, help="迁移锁等待秒数")
    migrate.add_argument("--statement-timeout", type=int, default=300, help="语句超时秒数")
    bootstrap = sub.add_parser(
        "bootstrap-roles",
        help="建 §6.3 角色矩阵与未来表默认权限（幂等；不设密码）",
    )
    bootstrap.add_argument("--statement-timeout", type=int, default=300,
                           help="语句超时秒数")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            return cmd_check(args)
        if args.command == "bootstrap-roles":
            return cmd_bootstrap_roles(args)
        return cmd_migrate(args)
    except Exception as exc:  # 未映射的驱动/环境错误：脱敏后按配置/环境失败
        text = str(exc)
        if "password" in text.lower():
            text = type(exc).__name__
        return _fail(f"{type(exc).__name__}: {text}", 2)


if __name__ == "__main__":
    sys.exit(main())
