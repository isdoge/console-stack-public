#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组件迁移 owner CLI：`check | migrate | adopt`（B7-2 隔离验证批次）。

与 `scripts/pg_schema.py` 同一纪律，不重叠职责：

- **不 import `app`**，不加载插件业务模块，不触发采集/后台。
- 组件表归属来自组合清单与插件 manifest（`component_migrations.declared_components`），
  不在这里手写第二份表清单。
- DDL 只在这里、只经管理员连接、**只取 `pg_migration_lib` 的既有维护锁**
  （`pg_locks.KEY_MAINTENANCE`，与业务写事务的维护共享锁同 key）。插件的
  import/initializer 永远不执行 DDL。
- 遗产链 `schema_migrations` 1–17 **不动**：不写、不改 checksum、不重放 CREATE，
  也不预占下一个号。生产部署链（`pg_schema.py migrate`、`deploy.sh`）本批未接入。

子命令：

- `check`：只读核对登记表/组件声明/实际 catalog（含遗产链完整性），脱敏 JSON；
  不兼容退出 5。可在只读连接上执行。
- `migrate`：锁内建登记表并追加 `COMPONENT_MIGRATIONS`（kernel 基线 v1 + 后续
  增量）；接管回执覆盖的基线自动跳过；指纹漂移/未知版本/未接管旧库退出 5。
- `grant-app-role`：为**新库**建/更新受限运行账号（LOGIN NOSUPERUSER NOCREATEDB
  NOCREATEROLE），只授当前组合声明组件表的 DML、序列 USAGE/SELECT 与登记表 SELECT；
  密码只从环境变量 `CONSOLE_APP_ROLE_PASSWORD` 读取（不进 argv/输出）。公共独立安装用。
- `adopt`：锁内为已在库的组件写接管回执（version 0, origin=adopted）。
  写回执前先对照冻结遗产链重放的独立标准 catalog 证明结构等价；缺表/结构漂移/
  遗产链不完整/回执冲突一律失败且不留半份回执。

两个起点的分工：**新库**先 `migrate`（建表 + 登记基线 v1）；**既有库**先
`adopt`（不重放基线 CREATE，回执 v0 覆盖 v1），之后 `migrate` 只追加后续增量。

退出码与 `pg_schema.py` 一致：0 成功 · 2 用法/配置错误 · 4 迁移锁忙 · 5 不兼容。
"""
import argparse
import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = ROOT / "console" / "backend"
for _path in (str(BACKEND), str(ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import component_migrations  # noqa: E402
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


def _components(args):
    """组件表：显式 `--plugins-root` 优先，否则走组合解析的插件根。"""
    return component_migrations.declared_components(args.plugins_root)


def cmd_check(args):
    try:
        config = target_config()
    except Exception as exc:  # PgConfigError
        return _fail(f"配置缺失：{exc}", 2)
    conn = connect_target(config, statement_timeout=30)
    try:
        report = component_migrations.registry_report(conn, _components(args))
        print(json.dumps({"ok": bool(report["ok"]), "report": report},
                         ensure_ascii=False, sort_keys=True))
        return 0 if report["ok"] else 5
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
            lock = acquire_migration_lock(conn, timeout_s=args.lock_timeout)
        except TargetGuardFailure as exc:
            return _fail(str(exc), 4)
        try:
            try:
                with conn.transaction():
                    outcome = component_migrations.apply_components(
                        conn, component_migrations.COMPONENT_MIGRATIONS, _components(args),
                    )
            except component_migrations.ComponentRegistryError as exc:
                # 指纹漂移、未知版本、在既有库上重放基线：都是「不兼容」而不是用法错误。
                return _fail(str(exc), 5)
        finally:
            release_migration_lock(conn)
        report = component_migrations.registry_report(conn, _components(args))
        print(json.dumps({
            "ok": bool(report["ok"]),
            "lock": lock,
            "applied": outcome["applied"],
            "skipped": outcome["skipped"],
            "report": report,
        }, ensure_ascii=False, sort_keys=True))
        # 迁移执行成功不等于登记完整：遗产链在库却无接管回执时必须 5，
        # 否则「一半组件登记、一半没登记」的库会被当成通过（§4.4 fail closed）。
        return 0 if report["ok"] else 5
    finally:
        conn.close()


def cmd_adopt(args):
    try:
        config = target_config()
    except Exception as exc:
        return _fail(f"配置缺失：{exc}", 2)
    conn = connect_target(config, readonly=False, statement_timeout=args.statement_timeout)
    try:
        try:
            lock = acquire_migration_lock(conn, timeout_s=args.lock_timeout)
        except TargetGuardFailure as exc:
            return _fail(str(exc), 4)
        try:
            try:
                with conn.transaction():
                    outcome = component_migrations.adopt_legacy(
                        conn, _components(args),
                    )
            except component_migrations.ComponentRegistryError as exc:
                return _fail(str(exc), 5)
        finally:
            release_migration_lock(conn)
        report = component_migrations.registry_report(conn, _components(args))
        print(json.dumps({
            "ok": bool(report["ok"]),
            "lock": lock,
            "adopted": outcome["adopted"],
            "skipped": outcome["skipped"],
            "legacy_versions": outcome["legacy_versions"],
            "report": report,
        }, ensure_ascii=False, sort_keys=True))
        return 0 if report["ok"] else 5
    finally:
        conn.close()


_ROLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def cmd_grant_app_role(args):
    """新库公共安装：受限运行账号只拿组合内组件表 DML + 登记表只读。"""
    if not _ROLE_RE.fullmatch(args.role or ""):
        return _fail("--role 必须是小写标识符（[a-z_][a-z0-9_]*）", 2)
    password = os.environ.get("CONSOLE_APP_ROLE_PASSWORD", "")
    if not password:
        return _fail("缺 CONSOLE_APP_ROLE_PASSWORD（运行账号密码只从环境变量读取）", 2)
    try:
        config = target_config()
    except Exception as exc:
        return _fail(f"配置缺失：{exc}", 2)
    if args.role == config.user:
        return _fail("运行账号不能与管理员账号相同", 2)
    from psycopg import sql
    components = _components(args)
    # 只授组合内组件真表；retained（未安装插件的历史表）不发权限。
    tables = tuple(sorted({t for owned in components.values() for t in owned}))
    conn = connect_target(config, readonly=False, statement_timeout=args.statement_timeout)
    try:
        report = component_migrations.registry_report(conn, components)
        if not report["ok"]:
            # 先 migrate（新库）或 adopt（既有库）；登记不完整时不发运行账号。
            print(json.dumps({"ok": False, "error": "组件登记不完整，先执行 migrate/adopt",
                              "report": report}, ensure_ascii=False, sort_keys=True))
            return 5
        role = sql.Identifier(args.role)
        with conn.transaction():
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s",
                                  (args.role,)).fetchone()
            verb = "ALTER" if exists else "CREATE"
            conn.execute(sql.SQL(verb + " ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                                 "NOREPLICATION PASSWORD {}").format(role, sql.Literal(password)))
            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(config.dbname), role))
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
            conn.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(role))
            conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
                sql.SQL(", ").join(sql.Identifier("public", t) for t in tables), role))
            conn.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}")
                         .format(role))
            conn.execute(sql.SQL("GRANT SELECT ON public.component_schema_migrations TO {}")
                         .format(role))
            conn.execute(sql.SQL("REVOKE INSERT, UPDATE, DELETE ON "
                                 "public.component_schema_migrations FROM {}").format(role))
        print(json.dumps({"ok": True, "role": args.role, "created": not exists,
                          "tables": list(tables)}, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    common = dict(plugins_root=None)
    check = sub.add_parser("check", help="只读核对组件登记/目录指纹，脱敏输出")
    check.add_argument("--plugins-root", default=common["plugins_root"],
                       help="显式插件根（默认按组合解析）")
    migrate = sub.add_parser("migrate", help="维护锁内建登记表并追加组件迁移；不 import app")
    migrate.add_argument("--plugins-root", default=common["plugins_root"])
    migrate.add_argument("--lock-timeout", type=int, default=10, help="迁移锁等待秒数")
    migrate.add_argument("--statement-timeout", type=int, default=300, help="语句超时秒数")
    adopt = sub.add_parser("adopt", help="为已在库的组件写接管回执（不重放 DDL）")
    adopt.add_argument("--plugins-root", default=common["plugins_root"])
    adopt.add_argument("--lock-timeout", type=int, default=10, help="迁移锁等待秒数")
    adopt.add_argument("--statement-timeout", type=int, default=300, help="语句超时秒数")
    grant = sub.add_parser("grant-app-role",
                           help="新库：建/更新受限运行账号（密码读 CONSOLE_APP_ROLE_PASSWORD）")
    grant.add_argument("--plugins-root", default=common["plugins_root"])
    grant.add_argument("--role", required=True, help="运行账号名（小写标识符）")
    grant.add_argument("--statement-timeout", type=int, default=60, help="语句超时秒数")
    args = parser.parse_args(argv)
    try:
        if args.command == "grant-app-role":
            return cmd_grant_app_role(args)
        if args.command == "check":
            return cmd_check(args)
        if args.command == "adopt":
            return cmd_adopt(args)
        return cmd_migrate(args)
    except component_migrations.UnknownComponent as exc:
        return _fail(f"组合组件不匹配：{exc}", 5)
    except Exception as exc:  # 未映射的驱动/环境错误：脱敏后按配置/环境失败
        text = str(exc)
        if "password" in text.lower():
            text = type(exc).__name__
        return _fail(f"{type(exc).__name__}: {text}", 2)


if __name__ == "__main__":
    sys.exit(main())
