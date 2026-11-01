#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""迁移 workstation 旧库数据到 console-stack DB（console/backend/data/console.db）。

- 迁移表：bookmarks / feed_cache（projects / agents 已从产品面移除，不再迁移）
- 幂等：按主键 id 判断，目标库已存在的记录直接跳过（不覆盖本地已变更数据），
  重复运行结果一致，可放心重跑；对新记录用 INSERT OR REPLACE 兜底。
- 源库只读打开（mode=ro），不会改动 workstation.db。

用法（任意目录均可运行）：
    python3 console/scripts/migrate_workstation_data.py
    # 指定源库
    WORKSTATION_DB=/path/to/workstation.db python3 console/scripts/migrate_workstation_data.py
"""
import os
import sqlite3
import sys

# backend/ 下的模块以 `import paths` 相互引用（cwd 约定为 backend），
# 此处把 backend 目录显式加入 sys.path，保证从任意 cwd 启动都能 import paths。
BACKEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, BACKEND_DIR)

import paths  # noqa: E402

SRC_DB = os.environ.get(
    "WORKSTATION_DB",
    "/Users/doge/Workspace/project/workstation/server/data/workstation.db",
)
DST_DB = paths.DB_PATH
TABLES = ("bookmarks", "feed_cache")


def _table_columns(conn, table):
    """返回表的所有列名（按声明顺序）。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [row[1] for row in rows]


def migrate_table(src_conn, dst_conn, table):
    """迁移单张表：仅插入目标库中不存在的 id，已存在则跳过。

    返回 {inserted, skipped, ignored} 统计。
    """
    src_cols = _table_columns(src_conn, table)
    dst_cols = _table_columns(dst_conn, table)
    cols = [c for c in src_cols if c in dst_cols]
    ignored = [c for c in src_cols if c not in dst_cols]

    existing = {
        row[0] for row in dst_conn.execute(f"SELECT id FROM {table}").fetchall()
    }

    sql = (
        f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})"
    )
    rows = src_conn.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()

    inserted = 0
    skipped = 0
    for row in rows:
        if row[0] in existing:
            skipped += 1
            continue
        dst_conn.execute(sql, row)
        inserted += 1

    return {"inserted": inserted, "skipped": skipped, "ignored": ignored}


def main():
    print(f"源库: {SRC_DB}")
    print(f"目标: {DST_DB}")
    if not os.path.isfile(SRC_DB):
        print(f"错误: 源库不存在: {SRC_DB}")
        sys.exit(1)

    src = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True)
    dst = sqlite3.connect(DST_DB)
    try:
        total = 0
        for table in TABLES:
            stats = migrate_table(src, dst, table)
            total += stats["inserted"]
            note = f"，忽略目标表不存在的列: {', '.join(stats['ignored'])}" if stats["ignored"] else ""
            print(
                f"[{table}] 迁移 {stats['inserted']} 条，"
                f"跳过已存在 {stats['skipped']} 条{note}"
            )
        dst.commit()
        print(f"完成：共迁移 {total} 条。脚本幂等，重复运行全部跳过，不会产生重复数据。")
    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    main()
