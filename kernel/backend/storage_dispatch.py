# -*- coding: utf-8 -*-
"""过渡期的存储分派（PG-02 独占；提案 §4.1 / D3 / D7）。

**为什么需要它**：`runtime_mode.DEFAULT_STORAGE_ENGINE` 在 PG-00 定版里是 `sqlite`
（过渡期，见 `runtime_mode.py` docstring）。也就是说切到 PG 之前，同一份业务代码要能在
两种引擎下跑。业务模块不该各自写一套 `if postgres: ... else: ...`，所以把「按引擎选连接」
收敛到这一个叶子模块。

**这不是在线 fallback**：选择只由 `CONSOLE_STORAGE_ENGINE` 显式决定，**绝不因为 PG 连不上
就退回 SQLite**（D3）。PG 模式下任何连接失败都抛 `pgdb.StorageUnavailable`，由 HTTP 层
映射 503。要退必须改环境变量并重启。

**占位符**：PG 是 `%s`、SQLite 是 `?`。这里用 `ph()` 在**构造 SQL 时**选择，不做运行时
正则替换（提案 §4.3 明令禁止）。所有业务值仍然走参数绑定，绝不拼进 SQL 文本。

**为什么 SQL 可以共用一份**：SQLite ≥3.24 与 PG 都支持
`INSERT ... ON CONFLICT (...) DO UPDATE SET col = excluded.col`，部分唯一索引冲突目标
（`ON CONFLICT (fp) WHERE resolved_at IS NULL DO NOTHING`）两边也都支持。时间戳统一由
Python 计算 UTC 文本，不用引擎各自的 `datetime('now')` / `now()`。
"""
import contextlib
import sqlite3

import db as dbmod
import paths


def is_missing_table(exc):
    """异常是否表示「表不存在」。

    可选遥测表（事件时间线、健康/探测时序）的既有契约是：表还没建时退化为空/不清理，
    不把整页打死。这个判定跨引擎——SQLite 抛 `OperationalError: no such table`，
    PG 抛 `psycopg.errors.UndefinedTable`。**不吞其它异常**：连接不可达、权限不足
    仍然照常向上抛（D10：不伪造零值）。
    """
    if isinstance(exc, sqlite3.OperationalError):
        return "no such table" in str(exc).lower()
    name = type(exc).__name__
    if name in ("UndefinedTable", "ProgrammingError"):
        return "does not exist" in str(exc).lower() or "undefinedtable" in name.lower()
    return False


def use_postgres():
    """当前是否走 PostgreSQL（由 `CONSOLE_STORAGE_ENGINE` 显式决定）。"""
    import runtime_mode

    return runtime_mode.storage_engine() == runtime_mode.STORAGE_ENGINE_POSTGRES


def ph():
    """SQL 占位符：PG `%s` / SQLite `?`。"""
    return "%s" if use_postgres() else "?"


def like_op():
    """模糊匹配运算符：PG `ILIKE` / SQLite `LIKE`。

    SQLite 的 LIKE 对 ASCII 默认**大小写不敏感**，PG 的 LIKE 敏感——不改写的话，
    `/api/oplog?q=` 在两个引擎下会返回不同结果（§4.3 明确要求处理 LIKE 大小写）。
    """
    return "ILIKE" if use_postgres() else "LIKE"


def executemany(conn, sql, params_seq):
    """跨引擎批量执行。

    psycopg3 的 `Connection` **没有** `executemany`（只有 `Cursor` 有），sqlite3 的
    `Connection` 有——不包一层就会在 PG 路径上拿到 `AttributeError`。
    """
    if use_postgres():
        with conn.cursor() as cursor:
            cursor.executemany(sql, params_seq)
        return
    conn.executemany(sql, params_seq)


@contextlib.contextmanager
def read_tx(conn=None, db_path=None):
    """只读事务。

    `conn` 非空 → 复用调用方事务（不提交、不关闭）；否则按引擎自开一个只读事务。
    PG 下走 `repeatable read`，保证一次响应里的列表/汇总来自同一快照（§4.2）。
    """
    if conn is not None:
        yield conn
        return
    if use_postgres():
        with dbmod.pg_transaction(readonly=True) as own:
            yield own
        return
    own = dbmod.get_connection(db_path or paths.DB_PATH)
    try:
        yield own
    finally:
        own.close()


@contextlib.contextmanager
def write_tx(conn=None, db_path=None, lock=None, locks=None):
    """写事务。

    `conn` 非空 → 复用调用方事务（**不提交**，由调用方决定）；否则自开并提交。
    PG 下 `lock` / `locks` 传给 `pg_transaction()`；SQLite 没有 advisory lock，
    参数被忽略（过渡期语义，切换后由 PG 提供串行化）。
    """
    if conn is not None:
        yield conn
        return
    if use_postgres():
        with dbmod.pg_transaction(lock=lock, locks=locks) as own:
            yield own
        return
    own = dbmod.get_connection(db_path or paths.DB_PATH)
    try:
        yield own
        own.commit()
    except Exception:
        own.rollback()
        raise
    finally:
        own.close()
