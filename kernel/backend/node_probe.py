# -*- coding: utf-8 -*-
"""节点探活时序（E4）：给节点卡的「最近探测」色带提供数据。

刻意**不叫 ping**：这里记的是 `probe_cache.check_machine_probe` 每次真实 TCP connect
的结果与耗时。采样点主要来自后台半点调度（默认每半小时一个本地时间节点，
`CONSOLE_MACHINE_PROBE_INTERVAL` 可调），页面访问命中 TTL 时不会重复落点；
密度远低于 ICMP ping。因此：

- 色带表示「最近 N 次探测的可用性与耗时」，不是丢包率——丢包当前没有数据源。
- 耗时含 DNS 解析（探活打的是 public_host），首次解析会偏高，属正常。

按机器保留最近 RETAIN_PER_MACHINE 条，插入时顺带裁剪，表不会无限长。

存储（PG-02）：连接由 `storage_dispatch` 按 `CONSOLE_STORAGE_ENGINE` 选择——
PG 走 `db.pg_transaction()`（`%s` + `dict_row`），SQLite 过渡期走 legacy 连接（`?`）。
`record()` / `history()` 接受可选 `conn`：调用方已在事务里时复用同一连接，
不拆开批次原子性（提案 §8.5）。
"""
import storage_dispatch as sd

RETAIN_PER_MACHINE = 200
# 前端色带格数（Glassmorphism useNodePingDisplay 的 EMPTY_PING_BAR_COUNT 同值）
BAND_SIZE = 20


def record(machine, status, latency_ms=None, ts=None, db_path=None, conn=None):
    """记一条探测结果；status ∈ up|down。异常向上抛由调用方决定是否吞。

    `conn` 非空时复用调用方事务（不提交、不回滚）。
    """
    machine = str(machine or "").strip().lower()
    if not machine or status not in ("up", "down"):
        return False
    with sd.write_tx(conn=conn, db_path=db_path) as own:
        own.execute(
            f"INSERT INTO probe_history (machine, ts, status, latency_ms)"
            f" VALUES ({sd.ph()},{sd.ph()},{sd.ph()},{sd.ph()})",
            (machine, ts or "", status, latency_ms),
        )
        own.execute(
            f"""
            DELETE FROM probe_history
             WHERE machine = {sd.ph()}
               AND id NOT IN (
                   SELECT id FROM probe_history
                    WHERE machine = {sd.ph()}
                    ORDER BY id DESC
                    LIMIT {sd.ph()}
               )
            """,
            (machine, machine, RETAIN_PER_MACHINE),
        )
    return True


def history(machine, limit=BAND_SIZE, db_path=None, conn=None):
    """最近 limit 条，按时间从旧到新（色带左旧右新）。"""
    machine = str(machine or "").strip().lower()
    if not machine:
        return []
    try:
        limit = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        limit = BAND_SIZE
    with sd.read_tx(conn=conn, db_path=db_path) as own:
        rows = own.execute(
            f"""
            SELECT ts, status, latency_ms FROM probe_history
             WHERE machine = {sd.ph()}
             ORDER BY id DESC
             LIMIT {sd.ph()}
            """,
            (machine, limit),
        ).fetchall()
    return [
        {"ts": row["ts"], "status": row["status"], "latency_ms": row["latency_ms"]}
        for row in reversed(rows)
    ]
