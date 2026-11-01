# -*- coding: utf-8 -*-
"""诊断模块的统一只读数据库入口（PG-04）。

诊断五模块（ops_status / effective_config / settings_store / attention /
inspection_report）原本各自直接 `dbmod.get_connection(db_path)` 或
`sqlite3.connect(file:...?mode=ro)`。存储引擎切到 PostgreSQL 后（提案 §8.1 W5：
「PG 诊断不读旧 SQLite」），它们必须经 PG-00 定版的连接/事务门面访问在线库，
不允许保留会静默读到旧 SQLite 的路径。

语义（过渡期兼容，最终态 postgres）：

- 引擎为 `postgres` 且 `db_path` 缺省**或就是在线库位置**（`paths.DB_PATH`）→
  在线 PG 只读事务（`db.pg_transaction(readonly=True)`，REPEATABLE READ，一致快照，
  不取维护共享锁）。
- 显式传**别的** `db_path`（str/Path）→ 按既有离线/测试约定打开该 SQLite 文件
  （只读 URI）；这保持现有测试的 `tmp_path` 注入能力与离线取证入口
  （提案 §4.1：SQLite 离线读取明确使用 sqlite3，不是在线 fallback）。
- 引擎为 `sqlite` 且未给 `db_path` → 既有在线 SQLite 工厂。

只读语义由调用方保证：本入口返回的上下文管理器在 PG 分支下事务体只允许
SELECT（PG 只读事务会拒绝写），SQLite 分支沿用各模块原有的 ro/读写习惯。

**PG-05 集成修正**：判据从「有没有传 db_path」收紧为「传的是不是在线库位置」。
原来只看 `db_path is None`，而生产接线（`app.py` 的 `lambda: DB_PATH`）与模块层的
`db_path or paths.DB_PATH` 归一都会把在线库路径送进来，于是 PG 引擎下诊断**静默
读到迁移前的旧 SQLite 快照**（`/api/attention`、`/api/ops-status`、`/api/config/*`），
正是 §8.1 W5 明令禁止的「PG 诊断读旧 SQLite」。`paths.DB_PATH` 在 PG 引擎下只是
历史位置，不再表示「读这个文件」；真正的离线取证传的是备份/快照路径，不受影响。
"""
import os
import sqlite3
from contextlib import contextmanager

import db as dbmod
import runtime_mode
from paths import DB_PATH


def _is_online_db_path(db_path):
    """该路径是否指「在线库」的历史位置（`paths.DB_PATH`）；缺省也算。"""
    if db_path is None:
        return True
    try:
        return os.path.abspath(os.fspath(db_path)) == os.path.abspath(DB_PATH)
    except TypeError:
        return False


@contextmanager
def readonly_connection(db_path=None):
    """诊断只读连接：PG 引擎走门面只读事务，SQLite 路径/引擎按既有约定。"""
    engine = runtime_mode.storage_engine()
    if engine == "postgres" and _is_online_db_path(db_path):
        with dbmod.pg_transaction(readonly=True) as conn:
            yield conn
        return
    target = db_path or DB_PATH
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row  # 具名列：与 PG dict_row、db 工厂同一约定
    try:
        yield conn
    finally:
        conn.close()


def is_online_postgres(db_path=None):
    """该次调用是否面向在线 PG（引擎 postgres 且路径是在线库位置或缺省）。"""
    return runtime_mode.storage_engine() == "postgres" and _is_online_db_path(db_path)
