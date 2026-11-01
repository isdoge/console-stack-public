# -*- coding: utf-8 -*-
"""服务健康时序（T12 分支实验）：给服务表状态格的迷你色带提供数据。

与 node_probe.py 同一套约定：
- 只在 `probe_cache.check_service_health` 真实探测后落点，命中缓存不重复写；
- status 只记 Healthy / Unhealthy，Unknown（无 checker、显式 health_probe: false）不落点；
- 按服务名保留最近 RETAIN_PER_SERVICE 条，插入时顺带裁剪，表不会无限长。

存储（PG-02）：连接由 `storage_dispatch` 按 `CONSOLE_STORAGE_ENGINE` 选择——
PG 走 `db.pg_transaction()`（`%s` + `dict_row`），SQLite 过渡期走 legacy 连接（`?`）。
`record()` 支持 `conn` 复用，供调用方在既有事务里追加落点而不拆开原子性。
"""
import storage_dispatch as sd

RETAIN_PER_SERVICE = 100
# 迷你色带格数（与前端 unified-table 的 MINI_BAND_SIZE 对齐）
BAND_SIZE = 12


def record(name, status, ts=None, db_path=None, conn=None):
    """记一条服务健康结果；status ∈ Healthy|Unhealthy，其余忽略。"""
    name = str(name or "").strip()
    if not name or status not in ("Healthy", "Unhealthy"):
        return False
    with sd.write_tx(conn=conn, db_path=db_path) as own:
        own.execute(
            f"INSERT INTO service_health_history (name, ts, status)"
            f" VALUES ({sd.ph()},{sd.ph()},{sd.ph()})",
            (name, ts or "", status),
        )
        own.execute(
            f"""
            DELETE FROM service_health_history
             WHERE name = {sd.ph()}
               AND id NOT IN (
                   SELECT id FROM service_health_history
                    WHERE name = {sd.ph()}
                    ORDER BY id DESC
                    LIMIT {sd.ph()}
               )
            """,
            (name, name, RETAIN_PER_SERVICE),
        )
    return True


def clear(name, db_path=None, conn=None):
    """清掉该服务的全部时序点（色带回空白）。

    给 `health_probe: false` 的关闭路径用：探测关掉后 Unknown 不落点，
    旧的 Unhealthy 样本会作为红格永远留在色带上（实测 beszel-agent-aly 的
    9 月故障样本挂到 10 月末仍全红）。与 `_reclaim_disabled_event` 的悬挂
    事件回收同一纪律——「不探测」不该展示过期的失败历史。
    """
    name = str(name or "").strip()
    if not name:
        return False
    with sd.write_tx(conn=conn, db_path=db_path) as own:
        own.execute(
            f"DELETE FROM service_health_history WHERE name = {sd.ph()}",
            (name,),
        )
    return True


def history(name, limit=BAND_SIZE, db_path=None, conn=None):
    """最近 limit 条，按时间从旧到新（色带左旧右新）。"""
    name = str(name or "").strip()
    if not name:
        return []
    try:
        limit = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        limit = BAND_SIZE
    with sd.read_tx(conn=conn, db_path=db_path) as own:
        rows = own.execute(
            f"""
            SELECT ts, status FROM service_health_history
             WHERE name = {sd.ph()}
             ORDER BY id DESC
             LIMIT {sd.ph()}
            """,
            (name, limit),
        ).fetchall()
    return [{"ts": row["ts"], "status": row["status"]} for row in reversed(rows)]
