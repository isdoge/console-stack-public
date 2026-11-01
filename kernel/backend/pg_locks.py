# -*- coding: utf-8 -*-
"""PostgreSQL advisory lock 的统一命名与固定获取顺序（提案 §4.2；契约版本 `pg00-1`）。

为什么用 advisory lock 而不是表锁：账本写入要串行化的是**应用层业务不变量**
（一笔流水只能有一个有效配对），不是某一行数据；advisory lock 不占表、不阻塞读、
事务结束自动释放，正好匹配「读已提交事务里，锁要在读之前拿到」的语义。

两条硬规则：

1. **namespace/key 是稳定常量**，不使用 Python 进程随机 `hash()`（`PYTHONHASHSEED`
   会让同一台机器两次启动拿到不同的锁，等于没锁）。需要按名字派生时用 `zlib.crc32`。
2. **固定获取顺序** `维护共享锁 → 业务域锁 → 行锁`，任何多锁路径都按 `LOCK_ORDER` 排序，
   禁止反向取锁——反向是死锁的标准配方。
"""
import zlib

# 0x434F4E53 == 'CONS'，console-stack 专属命名空间，避免与同库其他应用的 advisory lock 撞车。
LOCK_NAMESPACE = 1129270867

# 维护锁的独占/共享模式**必须共用同一个 key**：PG 的 advisory lock 按 key 互斥，
# 用两个 key 会让「维护控制器独占」与「业务写事务共享」各锁各的，等于没有冻结。
KEY_MAINTENANCE = 1
KEY_MAINTENANCE_SHARED = 1
KEY_LEDGER = 10              # 账本域锁：流水/分类/对冲/关联/重分类
KEY_RENEWALS = 11            # 续费域锁：新增排序、重排、删除
KEY_DASHBOARD_LAYOUT = 12    # 看板整体替换：DELETE/INSERT 必须同域串行
KEY_TRAFFIC_BASE = 1000      # 流量按机器派生：KEY_TRAFFIC_BASE + crc32(machine)

_TRAFFIC_SPACE = 0x7FFFFF    # 派生 key 的取值空间，保证 base + hash 不溢出 int4


class LockKey:
    """一把 advisory lock 的稳定标识。`shared=True` 用共享模式（多个读者可同时持有）。"""

    __slots__ = ("name", "key", "shared")

    def __init__(self, name, key, shared=False):
        self.name = name
        self.key = key
        self.shared = shared

    def __repr__(self):
        mode = "shared" if self.shared else "exclusive"
        return f"LockKey({self.name!r}, key={self.key}, {mode})"

    def __eq__(self, other):
        return (
            isinstance(other, LockKey)
            and other.key == self.key
            and other.shared == self.shared
        )

    def __hash__(self):
        return hash((self.key, self.shared))


MAINTENANCE = LockKey("maintenance", KEY_MAINTENANCE, shared=False)
MAINTENANCE_SHARED = LockKey("maintenance_shared", KEY_MAINTENANCE_SHARED, shared=True)
LEDGER = LockKey("ledger", KEY_LEDGER, shared=False)
RENEWALS = LockKey("renewals", KEY_RENEWALS, shared=False)
DASHBOARD_LAYOUT = LockKey("dashboard_layout", KEY_DASHBOARD_LAYOUT, shared=False)

# 固定获取顺序：维护锁 → 账本 → 续费 → 其它域锁。多锁路径必须按此排序。
LOCK_ORDER = (MAINTENANCE, MAINTENANCE_SHARED, LEDGER, RENEWALS, DASHBOARD_LAYOUT)


def traffic_lock(machine):
    """每机器一把流量锁（提案 §4.2：流量按机器串行）。"""
    name = "" if machine is None else str(machine)
    if not name:
        raise ValueError("traffic_lock 需要 machine 名")
    key = KEY_TRAFFIC_BASE + (zlib.crc32(name.encode("utf-8")) % _TRAFFIC_SPACE)
    return LockKey(f"traffic:{name}", key, shared=False)


def order_index(lock):
    """返回锁在固定顺序中的位置；未登记的域锁排在最后（由调用方保证不反向）。"""
    for index, known in enumerate(LOCK_ORDER):
        if known == lock:
            return index
    return len(LOCK_ORDER)


def sort_locks(locks):
    """按固定顺序排序；同 key 的锁只保留一次（避免自己和自己死锁）。"""
    unique = []
    seen = set()
    for lock in locks:
        marker = (lock.key, lock.shared)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(lock)
    return sorted(unique, key=order_index)


def acquire(conn, lock):
    """在**当前事务**内获取 advisory lock；事务结束自动释放，无需手工解锁。

    必须在读取业务数据**之前**调用：锁后读才能保证「读到的版本是最新的」。
    """
    if lock.shared:
        conn.execute("SELECT pg_advisory_xact_lock_shared(%s, %s)", (LOCK_NAMESPACE, lock.key))
    else:
        conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (LOCK_NAMESPACE, lock.key))
    return lock


def acquire_many(conn, locks):
    """按固定顺序依次获取多把锁。"""
    acquired = []
    for lock in sort_locks(locks):
        acquire(conn, lock)
        acquired.append(lock)
    return acquired


def held_locks(conn):
    """诊断用：本连接当前持有的 advisory lock（脱敏，不含业务数据）。"""
    rows = conn.execute(
        """
        SELECT objid, mode, granted
        FROM pg_locks
        WHERE locktype = 'advisory' AND pid = pg_backend_pid()
        ORDER BY objid
        """
    ).fetchall()
    return [dict(row) if not isinstance(row, dict) else row for row in rows]
