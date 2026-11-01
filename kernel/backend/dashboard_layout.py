# -*- coding: utf-8 -*-
"""C8 / D1'：总控台布局 override 的持久层（localStorage → SQLite v9）。

数据模型对齐 D3 的两个 localStorage 键的实际形状（步骤 0 grep 实测）：

- ``console.dashboard.layout``：``[{key, x, y, w}]`` 稀疏 override（只存用户挪过的
  widget，12 列坐标，**不存 h**——高度由前端 GridStack sizeToContent 按内容定）；
- ``console.dashboard.hidden``：独立键的 widget key 数组。

两键合一表 ``dashboard_layout``：一行 = 一个 widget 的用户自定义，
``hidden=1`` 表示被隐藏；隐藏行的坐标仍保留（对应 D3「拖过再隐藏、找回后
位置不变」语义）。前端每次 PUT **整体替换**（不是增量），与 D3 的
「重置默认布局 = 清两个键」语义对齐：PUT 空 items 即回到 default⊕∅。

widget key 白名单 = D3 代码中的 widget 类型全集（layout-model.ts 实测）：

- 固定件六类：``inspection`` / ``attention`` / ``monitor-events`` / ``ops-status`` /
  ``tailnet`` / ``caddy-entry``（C11，2026-09-06）；
- 机器卡 ``machine-card:<台账 id>``——id 必须在 machines 台账里真实存在
  （``mc.load_machines()``），机器下架后的残留行由前端整体替换时自然清理；
- Beszel 孤儿系统 ``machine-card:sys:<beszel id>``——id 动态产生，只校验
  ``sys:`` 后非空的形态（绑 Beszel live 校验会让布局保存取决于 Beszel 可用性）。

校验三件套：未知 key 400、坐标越界 400（0≤x≤11，y≥0，1≤w≤12）、幂等 PUT。
oplog 记 ``host='console', svc='dashboard', action='dashboard_layout_replace'``。

无登录依赖（P0-3 现状照旧）：与全部 /api/* 一致，不加鉴权——写路径的
暴露面收敛靠白名单 + 边界校验，任何 tailnet 设备最多能写自己看板的布局。

存储（PG-02）：连接由 `storage_dispatch` 按 `CONSOLE_STORAGE_ENGINE` 选择。
"""
import datetime

from flask import jsonify, request

import machine_config as mc
import pg_locks
import storage_dispatch as sd

# 与前端 layout-model.ts / hidden-model.ts 的 FIXED_WIDGET_KEYS 同源（grep 实测）。
# 2026-09-06：+caddy-entry（C11 入口流量小卡）。
# 'inspection' 在 D4 收进顶栏后不再是默认布局成员，但白名单保留它——
# D1' 与 D4 之间落库的行不至于因为白名单收窄而 PUT 不回去。
FIXED_WIDGET_KEYS = ("inspection", "attention", "monitor-events", "ops-status", "tailnet", "caddy-entry")
MACHINE_WIDGET_PREFIX = "machine-card:"
ORPHAN_SYSTEM_PREFIX = "sys:"

GRID_COLUMNS = 12

OPLOG_HOST = "console"
OPLOG_SVC = "dashboard"
OPLOG_ACTION = "dashboard_layout_replace"

_MAX_KEY_LEN = 128


def validate_widget_key(key, machine_ids=None):
    """widget key 白名单校验；合法返回 True。"""
    if not isinstance(key, str) or not key or len(key) > _MAX_KEY_LEN:
        return False
    if any(ch.isspace() for ch in key):
        return False
    if key in FIXED_WIDGET_KEYS:
        return True
    if not key.startswith(MACHINE_WIDGET_PREFIX):
        return False
    ident = key[len(MACHINE_WIDGET_PREFIX) :]
    if ident.startswith(ORPHAN_SYSTEM_PREFIX):
        # Beszel 孤儿系统：id 动态，只校验形态（sys: 后非空）
        return len(ident) > len(ORPHAN_SYSTEM_PREFIX)
    # 台账机器 id 必须真实存在
    return ident in (set(mc.load_machines()) if machine_ids is None else machine_ids)


def _validate_item(raw, machine_ids=None):
    """单条 item 校验：合法返回 (widget_key, x, y, w, hidden)，否则 None。"""
    if not isinstance(raw, dict):
        return None
    key = raw.get("widget_key")
    if not validate_widget_key(key, machine_ids):
        return None
    coords = []
    for name in ("x", "y", "w"):
        value = raw.get(name)
        # bool 是 int 的子类，先排除；浮点 / 字符串一律拒（前端契约是整数）
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        coords.append(value)
    x, y, w = coords
    if not 0 <= x <= GRID_COLUMNS - 1:
        return None
    if y < 0:
        return None
    if not 1 <= w <= GRID_COLUMNS:
        return None
    hidden = raw.get("hidden", 0)
    if isinstance(hidden, bool):
        hidden = 1 if hidden else 0
    elif isinstance(hidden, int) and hidden in (0, 1):
        pass
    else:
        return None
    return (key, x, y, w, hidden)


def _validated_items(items_raw, machine_ids=None):
    """整批校验；返回 [(key, x, y, w, hidden)]，任何一条非法抛 ValueError。"""
    if not isinstance(items_raw, list):
        raise ValueError("items 必须是数组")
    validated = []
    seen = set()
    for raw in items_raw:
        item = _validate_item(raw, machine_ids)
        if item is None:
            key = raw.get("widget_key") if isinstance(raw, dict) else None
            raise ValueError(f"非法的布局项（widget_key={key!r}）")
        if item[0] in seen:
            raise ValueError(f"重复的 widget_key: {item[0]}")
        seen.add(item[0])
        validated.append(item)
    return validated


def get_layout(db_path=None, conn=None):
    """读布局：``{items: [{widget_key,x,y,w,hidden}], updated_at}``。

    表还不存在（未跑迁移的旧库 / 库文件缺失）按空布局处理，不抛 500。
    """
    empty = {"items": [], "updated_at": None}
    try:
        with sd.read_tx(conn=conn, db_path=db_path) as own:
            rows = own.execute(
                "SELECT widget_key, x, y, w, hidden FROM dashboard_layout"
                " ORDER BY widget_key"
            ).fetchall()
            row = own.execute(
                "SELECT MAX(updated_at) AS latest FROM dashboard_layout"
            ).fetchone()
    except Exception as exc:
        if sd.is_missing_table(exc):
            return empty
        raise
    items = [
        {
            "widget_key": r["widget_key"],
            "x": r["x"],
            "y": r["y"],
            "w": r["w"],
            "hidden": bool(r["hidden"]),
        }
        for r in rows
    ]
    return {"items": items, "updated_at": row["latest"] if row else None}


def replace_layout(items_raw, db_path=None, now=None, conn=None, *, machine_ids=None):
    """整体替换（PUT）。校验失败抛 ValueError；成功返回与 GET 同形状的 dict。

    幂等：同一份 payload 重复 PUT 结果一致（updated_at 为写入时刻，随时间推进，
    状态本身不变）。
    """
    items = _validated_items(items_raw, machine_ids)
    if now is None:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    elif isinstance(now, str):
        stamp = now  # 测试注入的预格式化时刻
    else:
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    with sd.write_tx(conn=conn, db_path=db_path) as own:
        if sd.use_postgres():
            # write_tx skips its lock arguments when reusing conn; acquire before DELETE
            # in both paths. The domain key is distinct from the shared maintenance key.
            pg_locks.acquire_many(own, (pg_locks.MAINTENANCE_SHARED, pg_locks.DASHBOARD_LAYOUT))
        own.execute("DELETE FROM dashboard_layout")
        ph = sd.ph()
        sd.executemany(
            own,
            f"INSERT INTO dashboard_layout (widget_key, x, y, w, hidden, updated_at)"
            f" VALUES ({ph},{ph},{ph},{ph},{ph},{ph})",
            [(key, x, y, w, hidden, stamp) for (key, x, y, w, hidden) in items],
        )
    return get_layout(db_path, conn=conn)


def register_routes(app, resolve_db=None, log_op=None, resolve_machine_ids=None):
    """挂到既有 app 上；app.py 只负责 import 与调用这一处。

    ``resolve_db`` 每请求求值（与 inspection_report 同模式：注册时捕获值会让
    测试的 monkeypatch 失效）；``log_op`` 复用 app.py 的 ``_log_op``，避免
    第二份 oplog SQL。
    """

    @app.route("/api/dashboard-layout", methods=["GET"])
    def get_dashboard_layout():
        """读总控台布局 override：`{items: [{widget_key, x, y, w, hidden}], updated_at}`。

        表还不存在（未跑 v9 迁移的旧库 / 库文件缺失）按空布局返回，不抛 500。
        """
        return jsonify(get_layout(resolve_db() if resolve_db else None))

    @app.route("/api/dashboard-layout", methods=["PUT"])
    def put_dashboard_layout():
        """**整体替换**总控台布局 override（不是增量）；PUT 空 items 即回到默认布局。

        校验三件套：未知 widget key 400、坐标越界 400（0≤x≤11，y≥0，1≤w≤12）、
        重复 key 400；成功与失败都记 oplog（`dashboard_layout_replace`）。
        """
        payload = request.get_json(silent=True)
        items = payload.get("items") if isinstance(payload, dict) else None
        db_path = resolve_db() if resolve_db else None
        try:
            machine_ids = None
            if resolve_machine_ids and isinstance(items, list) and any(
                isinstance(item, dict) and str(item.get("widget_key", "")).startswith(MACHINE_WIDGET_PREFIX)
                and not str(item["widget_key"]).startswith(MACHINE_WIDGET_PREFIX + ORPHAN_SYSTEM_PREFIX)
                for item in items
            ):
                machine_ids = resolve_machine_ids()
            result = replace_layout(items, db_path, machine_ids=machine_ids)
        except ValueError as exc:
            if log_op:
                log_op(OPLOG_HOST, OPLOG_SVC, OPLOG_ACTION, False, str(exc))
            return jsonify({"error": str(exc)}), 400
        if log_op:
            log_op(OPLOG_HOST, OPLOG_SVC, OPLOG_ACTION, True)
        return jsonify(result)

    return app
