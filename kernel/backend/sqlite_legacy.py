# -*- coding: utf-8 -*-
"""SQLite 离线源读取（提案 §4.1；契约版本 `pg00-1`）。

定位说清楚，避免被当成在线 fallback：

- **是**：迁移 preflight/apply 的源读取、旧 Pay 取证、导出旧快照对比。
- **不是**：在线业务库。D3 明确断连时不回退到可写 SQLite，所以本模块**只读**打开，
  连 `PRAGMA journal_mode` 都不改——改 WAL 会写文件。

`ledger_transfer._read_legacy()` 未来改走这里（PG-01）；PG-00 只提供接口与不变量测试。
"""
import os
import sqlite3
from urllib.parse import quote


class LegacySourceError(RuntimeError):
    """离线源不可用或不是预期形态；不是在线业务错误。"""


def open_legacy_source(path):
    """以只读 URI 打开离线 SQLite 快照。

    - 文件不存在 → `LegacySourceError`（不创建空库，避免把「路径写错」变成「空账本」）。
    - `mode=ro` 由 SQLite 强制：任何写尝试都会报 `attempt to write a readonly database`。
    - `row_factory = sqlite3.Row` 与旧代码一致，迁移器可按列名取值。
    """
    resolved = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(resolved):
        raise LegacySourceError(f"离线源不存在: {resolved}")
    uri = f"file:{quote(resolved)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def is_read_only(conn):
    """确认连接确实只读（防止有人把本模块的连接换成可写连接）。"""
    try:
        conn.execute("PRAGMA query_only")
    except sqlite3.DatabaseError:
        return False
    row = conn.execute("PRAGMA query_only").fetchone()
    return bool(row and row[0]) or _probe_readonly(conn)


def _probe_readonly(conn):
    """`query_only` 未被设置时，用只读事务探测一次（不落盘）。"""
    try:
        conn.execute("BEGIN")
        conn.execute("CREATE TEMP TABLE _ro_probe (x INTEGER)")
        conn.execute("ROLLBACK")
    except sqlite3.DatabaseError:
        return True
    return False


def table_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def columns(conn, table):
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [
        {
            "name": row[1],
            "type": row[2],
            "notnull": bool(row[3]),
            "default": row[4],
            "pk": int(row[5] or 0),
        }
        for row in rows
    ]


def indexes(conn, table):
    rows = conn.execute(f'PRAGMA index_list("{table}")').fetchall()
    result = []
    for row in rows:
        name = row[1]
        cols = conn.execute(f'PRAGMA index_info("{name}")').fetchall()
        result.append(
            {
                "name": name,
                "unique": bool(row[2]),
                "origin": row[3] if len(row) > 3 else None,
                "partial": bool(row[4]) if len(row) > 4 else False,
                "columns": [col[2] for col in cols],
            }
        )
    return result


def row_count(conn, table):
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def max_id(conn, table, column="id"):
    """表内 `column` 的最大值；列不存在时返回 None。

    必须显式检查列名：SQLite 把未知的 `"id"` 当**字符串字面量**处理（历史兼容行为），
    `SELECT MAX("id") FROM schema_migrations` 会安静地返回 `'id'` 而不是报错。
    """
    names = {item["name"] for item in columns(conn, table)}
    if column not in names:
        return None
    try:
        row = conn.execute(f'SELECT MAX("{column}") FROM "{table}"').fetchone()
    except sqlite3.DatabaseError:
        return None
    if row is None or row[0] is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        # TEXT 主键（renewals/bookmarks/versions 等）没有 sequence 语义
        return None


def sequence_high_water(conn):
    """`sqlite_sequence` 高水位；表不存在（无 AUTOINCREMENT 表）时返回空 dict。"""
    try:
        rows = conn.execute("SELECT name, seq FROM sqlite_sequence").fetchall()
    except sqlite3.DatabaseError:
        return {}
    return {row[0]: int(row[1] or 0) for row in rows}


def applied_versions(conn):
    try:
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    return [int(row[0]) for row in rows]


def integrity_check(conn):
    row = conn.execute("PRAGMA quick_check").fetchone()
    return None if row is None else str(row[0])


def schema_inventory(conn):
    """迁移 preflight 需要的源清单：表/列/索引/计数/ID 高水位/源版本。

    这是**只读**报告，PG-03 的迁移器按它做映射核对；不在这里做任何金额转换
    （金额规范值由 `money.canonical` 统一负责，避免两套编码）。
    """
    inventory = {
        "tables": {},
        "sequence_high_water": sequence_high_water(conn),
        "schema_versions": applied_versions(conn),
        "quick_check": integrity_check(conn),
    }
    for table in table_names(conn):
        inventory["tables"][table] = {
            "columns": columns(conn, table),
            "indexes": indexes(conn, table),
            "row_count": row_count(conn, table),
            "max_id": max_id(conn, table),
        }
    return inventory
