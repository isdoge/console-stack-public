# -*- coding: utf-8 -*-
"""数据库入口：legacy SQLite 主体在 `db_sqlite`，PG 运行入口转发 `pgdb`。

两部分互不混用（PG-00；契约版本 `pg00-1`）：

1. **legacy SQLite**（`db_sqlite`，冻结）：连接工厂、v1–v11 迁移、在线备份。
   仅用于离线取证、迁移源读取与现有测试；**不是在线 fallback**（决策 D3）。
2. **PG 运行入口**（本文件末尾，转发到 `pgdb`）：在线业务的唯一连接/事务入口。
   psycopg 延迟导入，因此没装依赖时 `import db` 仍然成功。

为什么不在 `get_connection()` 里按引擎分派：SQLite 的 `?` 占位符、`sqlite3.Row`、
`INTEGER` 布尔与 PG 的 `%s`、`dict_row`、原生类型不兼容，静默分派会让调用方在两种
语义之间漂移。PG 业务代码显式调用 `pg_transaction()`；`get_connection()` 永久保留
legacy 语义。
"""
import paths  # noqa: F401 - 旧控制器/备份入口依赖 db.paths
from db_sqlite import (  # noqa: F401 - 旧入口同对象重导出
    DB_PATH,
    MIGRATIONS,
    applied_versions,
    backup_database,
    get_connection,
    run_migrations,
)

# --- PostgreSQL 运行入口（PG-00 定版；契约版本 `pg00-1`） ---------------------
# 全部为延迟导入转发：import db 不要求 psycopg 存在，也不建立任何连接。

def _pgdb():
    import pgdb

    return pgdb


def pg_pool(config=None):
    """进程内 PG 连接池单例（`pgdb.PgConfig` 或 None 走环境变量）。"""
    return _pgdb().pool(config)


def pg_open(config=None, wait=True, timeout=None):
    """显式打开池；应用启动时调用，连接不可达即有界失败。"""
    return _pgdb().open_pool(config, wait=wait, timeout=timeout)


def pg_connection():
    """借出池连接（context manager）；不隐式提交/回滚。"""
    return _pgdb().pg_connection()


def pg_transaction(readonly=False, lock=None, locks=None, statement_timeout=None,
                   isolation=None):
    """**在线业务唯一事务入口**；写事务自动取维护共享锁。"""
    return _pgdb().pg_transaction(
        readonly=readonly,
        lock=lock,
        locks=locks,
        statement_timeout=statement_timeout,
        isolation=isolation,
    )


def pg_healthcheck():
    """脱敏只读探针（诊断入口，不抛连接异常）。"""
    return _pgdb().healthcheck()


def pg_close():
    """关闭池（测试/进程退出用）。"""
    return _pgdb().close_pool()
