# -*- coding: utf-8 -*-
"""事件生命周期与影响面折叠（Track K，设计见 IMPROVEMENTS 附录 A1 §5.4 / §7）。

和既有两张**采样时序**表的分工，先说清楚免得后来人以为重复：

============================ =========================================================
表                            写入时机
============================ =========================================================
``probe_history`` (v6)        机器每次真实探测都落一行（色带要密度）
``service_health_history``    服务每次真实探测都落一行（迷你色带要密度）
``monitor_events`` (v10)      **只在状态变化时**写一行（K2「只记 transition」）
============================ =========================================================

所以本表行数远小于前两张：一次「掉线 3 小时」在 probe_history 里是 6 行 down，
在这里是 1 行（``opened_at`` 到 ``resolved_at``），能直接读出持续时长。

**幂等**（K1）靠部分唯一索引 ``idx_monitor_events_open`` 物理保证：
同一 ``fingerprint`` 最多一个 ``resolved_at IS NULL`` 的行。于是
``observe()`` 不需要任何内存状态就自洽 —— 失败时「有活动事件就更新、没有就开」，
恢复时「有活动事件就关、没有就什么都不做」。进程重启、多线程并发都不会写重。
**上一状态存在库里而不是内存里**，这是本模块唯一的状态源。

**并发实现（PG-02）**：SELECT→INSERT 之间仍有竞态窗口，两个并发探测会同时看到
「没有活动事件」。SQLite 版靠捕获 ``IntegrityError`` 后 rollback 处理；PG 里事务内
约束冲突会**中止整个事务**（后续语句一律报 `current transaction is aborted`），
所以改用 `INSERT ... ON CONFLICT (fingerprint) WHERE resolved_at IS NULL DO NOTHING`：
冲突行不写、事务不中止，语义等价于「已经有活动事件」→ 回 ``updated``。

**影响折叠**（K3）是**查询期动态算**的，表里刻意不存 parent 列：
机器 down 时同机的服务事件挂成它的 children，机器一恢复（machine 事件不再活动）
同机服务事件自动浮回顶层，不需要在恢复时回填任何行——正是设计 §5.4
「主机恢复后再逐项检查仍未恢复的服务；仍失败的才保留为独立事件」的语义。

时间一律 UTC ``%Y-%m-%dT%H:%M:%SZ``（与 probe_history / service_health_history 的
``ts`` 同格式，字符串序 = 时间序）。**注意别和 oplog 混**：那张表的 ``started_at``
是本地时间 ``%Y-%m-%d %H:%M:%S``，两者不可直接比较。

**detail 落库前抹掉 IPv4**（``scrub_detail``）：探测器的 msg 带目标地址，而事件会
长期留在库里并在事件页展示——目标是谁由 kind/target/machine 表达，detail 只答
「为什么失败」。这条是本模块的对外契约，有测试锁死。

维护窗口（K5）**不做**：Console 自己的维护语义等真实需求出现再开迁移，不提前造表。
"""
import datetime
import os
import re

from flask import jsonify, request

import storage_dispatch as sd

# 探测器的 msg 会带目标地址（`check_machine_probe` 拼的是 `tcp <tailscale_ip>:<port> …`）。
# 事件 detail 会**落库并在事件页长期展示**，比瞬时探测缓存的暴露面大一个数量级，
# 所以这里一律把 IPv4 抹掉：故障目标是谁由 kind/target/machine 三个字段表达，
# detail 只需要回答「为什么失败」（refused / timeout / Status 500）。
# 端口与延迟保留（`tcp ***:22 ok 1.2ms`），排障需要它们。
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

KIND_MACHINE = "machine"
KIND_SERVICE = "service"
# 家宽公网不通：机器本身通常好着（Tailscale/Beszel 在线），所以既不是 machine 事件，
# 也不作 K3 折叠的根（collapse 只认 KIND_MACHINE），同机服务事件不会被它吞掉。
KIND_PUBLIC = "public"
KINDS = (KIND_MACHINE, KIND_SERVICE, KIND_PUBLIC)

SOURCE_PROBE = "probe"  # probe_cache.check_machine_probe
SOURCE_HEALTH = "health"  # probe_cache.check_service_health
SOURCE_PUBLIC_PROBE = "public_probe"  # probe_cache.check_machine_public_probe

SEVERITIES = ("error", "warn")
STATES = ("active", "resolved", "all")

# 事件页默认窗口与分页上限（K4）
DEFAULT_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 90
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(moment):
    return moment.strftime(TS_FORMAT)


def _parse(value):
    """解析本表写出的 UTC 时间戳；坏值返回 None（单行坏数据不该打死整页）。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.datetime.strptime(text, TS_FORMAT).replace(
            tzinfo=datetime.timezone.utc
        )
    except ValueError:
        return None


def fingerprint(kind, target):
    """事件幂等键。同一 (kind, target) 的连续失败合成一条事件。"""
    return "{}:{}".format(kind, str(target or "").strip())


def scrub_detail(text):
    """落库前抹掉 detail 里的 IPv4（含 tailscale 100.x）。见 ``_IPV4_RE`` 注释。"""
    return _IPV4_RE.sub("***", str(text or "").strip())


def retention_days():
    """事件保留天数；非法值按 0（不清理）处理。

    与 ``settings_store.retention_days()`` 同构但读自己的键 —— 刻意不 import
    settings_store：那边的写回路径要反过来调本模块的 ``prune()``，共用会成环。
    """
    try:
        return max(0, int(os.environ.get("CONSOLE_EVENT_RETENTION_DAYS", "0")))
    except (TypeError, ValueError):
        return 0


# ---- 写入：状态观测 → 生命周期 -------------------------------------------


def observe(
    kind,
    target,
    ok,
    source="",
    machine="",
    title="",
    detail="",
    severity="error",
    ts=None,
    db_path=None,
    conn=None,
):
    """一次状态观测。返回 ``opened`` / ``updated`` / ``resolved`` / ``None``。

    ``ok=True`` 关闭该 fingerprint 的活动事件（本来就没有 → ``None``，不写空事件）；
    ``ok=False`` 有活动事件则刷新 detail（幂等，不新开），否则开一条新事件。
    ``ok`` 非 bool（例如探测结果 unknown）一律 ``None``：**没有数据源就不造事件**。

    `conn` 非空时复用调用方事务（不提交）；`db_path` 仅为兼容既有签名保留，PG 下忽略。
    """
    target = str(target or "").strip()
    if not target or kind not in KINDS or not isinstance(ok, bool):
        return None
    if severity not in SEVERITIES:
        severity = "error"
    fp = fingerprint(kind, target)
    moment = str(ts or "").strip() or _iso(_utc_now())
    machine = str(machine or "").strip().lower()
    title = str(title or "").strip()
    detail = scrub_detail(detail)

    with sd.write_tx(conn=conn, db_path=db_path) as own:
        return _observe(own, fp, kind, target, ok, source, machine, title, detail,
                        severity, moment)


def _observe(conn, fp, kind, target, ok, source, machine, title, detail, severity, moment):
    ph = sd.ph()
    if ok:
        # 关闭时刻意不动 detail/title：那是**这次故障**的原因，恢复了也该留着
        cursor = conn.execute(
            f"UPDATE monitor_events SET resolved_at = {ph}, updated_at = {ph} "
            f" WHERE fingerprint = {ph} AND resolved_at IS NULL",
            (moment, moment, fp),
        )
        return "resolved" if (cursor.rowcount or 0) > 0 else None

    row = conn.execute(
        f"SELECT id FROM monitor_events WHERE fingerprint = {ph} AND resolved_at IS NULL",
        (fp,),
    ).fetchone()
    if row:
        # 归属与文案跟随最新观测（机器改了 label、服务换了机器都能自愈），
        # opened_at 不动——那是这次故障的起点。
        conn.execute(
            f"UPDATE monitor_events SET machine = {ph}, severity = {ph}, title = {ph}, "
            f"       detail = {ph}, updated_at = {ph} WHERE id = {ph}",
            (machine, severity, title, detail, moment, row["id"]),
        )
        return "updated"

    cursor = conn.execute(
        "INSERT INTO monitor_events (fingerprint, kind, source, target, machine, "
        "       severity, title, detail, opened_at, updated_at) "
        f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph}) "
        "ON CONFLICT (fingerprint) WHERE resolved_at IS NULL DO NOTHING",
        (fp, kind, source, target, machine, severity, title, detail, moment, moment),
    )
    # SELECT→INSERT 之间另一个线程先开了同一事件（Flask 多线程下两个并发
    # `/api/services?force=1` 就够）。部分唯一索引挡住了重复行；DO NOTHING 不中止事务，
    # 语义等价于「已经有活动事件」——回 updated，别把它当失败往上抛。
    return "opened" if (cursor.rowcount or 0) > 0 else "updated"


def observe_machine(machine_id, status, label="", detail="", ts=None, db_path=None, conn=None):
    """机器探活结果 → 事件。``status`` ∈ up|down；unknown 不产生事件。"""
    target = str(machine_id or "").strip().lower()
    if not target or status not in ("up", "down"):
        return None
    name = str(label or "").strip() or target.upper()
    return observe(
        KIND_MACHINE,
        target,
        status == "up",
        source=SOURCE_PROBE,
        machine=target,
        title="{} 探测失败".format(name),
        detail=detail,
        ts=ts,
        db_path=db_path,
        conn=conn,
    )


def observe_public(machine_id, status, label="", detail="", ts=None, db_path=None, conn=None):
    """家宽公网探测结果 → 事件。``status`` ∈ up|down；unknown 不产生事件。

    unknown 也包括防伪闸门拦下的样本（解析到 fake-ip、握手快得不像公网）：
    那是「本机代理没放行」，不是节点不通，造事件会把配置问题误报成封堵。
    severity 用 warn：代理用不了，但机器和它上面的服务不受影响。
    """
    target = str(machine_id or "").strip().lower()
    if not target or status not in ("up", "down"):
        return None
    name = str(label or "").strip() or target.upper()
    return observe(
        KIND_PUBLIC,
        target,
        status == "up",
        source=SOURCE_PUBLIC_PROBE,
        machine=target,
        title="{} 家宽公网不通".format(name),
        detail=detail,
        severity="warn",
        ts=ts,
        db_path=db_path,
        conn=conn,
    )


def observe_service(name, status, machine="", detail="", ts=None, db_path=None, conn=None):
    """服务健康结果 → 事件。``status`` ∈ Healthy|Unhealthy；Unknown 不产生事件。

    Unknown 包含「无 checker」与台账显式 ``health_probe: false`` 两种，
    与 ``service_health_history`` 同纪律：探不到不等于坏了。
    """
    target = str(name or "").strip()
    if not target or status not in ("Healthy", "Unhealthy"):
        return None
    return observe(
        KIND_SERVICE,
        target,
        status == "Healthy",
        source=SOURCE_HEALTH,
        machine=machine,
        title="{} 健康检查失败".format(target),
        detail=detail,
        ts=ts,
        db_path=db_path,
        conn=conn,
    )


# ---- 读取 ----------------------------------------------------------------


def _duration_sec(opened_at, resolved_at, now=None):
    """持续时长。**活动事件给的是「截至此刻」**，会随每次请求变大。"""
    opened = _parse(opened_at)
    if opened is None:
        return None
    end = _parse(resolved_at) or now or _utc_now()
    return max(0, int((end - opened).total_seconds()))


def _row_to_event(row, now=None):
    resolved = row["resolved_at"] or None
    return {
        "id": int(row["id"]),
        "fingerprint": row["fingerprint"],
        "kind": row["kind"],
        "source": row["source"],
        "target": row["target"],
        "machine": row["machine"],
        "severity": row["severity"],
        "title": row["title"],
        "detail": row["detail"],
        "opened_at": row["opened_at"],
        "updated_at": row["updated_at"],
        "resolved_at": resolved,
        "state": "resolved" if resolved else "active",
        "duration_sec": _duration_sec(row["opened_at"], resolved, now=now),
    }


def _active_rows(conn, now=None):
    rows = conn.execute(
        "SELECT * FROM monitor_events WHERE resolved_at IS NULL ORDER BY opened_at ASC, id ASC"
    ).fetchall()
    return [_row_to_event(row, now=now) for row in rows]


def active_events(db_path=None, now=None, conn=None):
    """全部未恢复事件，按 opened_at 升序（先坏的在前，根因通常更早）。

    表还没建（迁移未跑）时退化为空——事件是加分项，不该让调用方炸；
    连接不可达/权限不足仍然向上抛（`storage_dispatch.is_missing_table` 只放行前者）。
    """
    try:
        with sd.read_tx(conn=conn, db_path=db_path) as own:
            return _active_rows(own, now=now)
    except Exception as exc:
        if sd.is_missing_table(exc):
            return []
        raise


def collapse(events):
    """K3 影响折叠：机器事件作根，同机的服务事件挂成 ``children``。

    返回新的顶层列表；每项带 ``children``（列表）与 ``impact_count``。
    没有对应机器根因的服务事件保持顶层 —— 「机器好着但这个服务坏了」
    本来就是独立故障，不该被折叠掉。

    **就地改写传入的事件 dict**（``children`` / ``impact_count`` 每次重置，
    重复调用不会累加）。传进来的通常是 ``active_events()`` 新建的副本。
    """
    roots = {}
    for event in events:
        event["children"] = []
        if event.get("kind") == KIND_MACHINE and event.get("machine"):
            roots.setdefault(event["machine"], event)

    top = []
    for event in events:
        if event.get("kind") == KIND_SERVICE:
            root = roots.get(event.get("machine") or "")
            if root is not None:
                root["children"].append(event)
                continue
        top.append(event)

    for event in top:
        event["impact_count"] = len(event["children"])
    return top


def _clamp(value, fallback, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def list_events(
    days=DEFAULT_WINDOW_DAYS,
    kind=None,
    machine=None,
    severity=None,
    state="all",
    limit=DEFAULT_LIMIT,
    offset=0,
    db_path=None,
    now=None,
    conn=None,
):
    """事件页数据源（K4）：时间窗 + 筛选 + 分页，附带折叠关系标记。

    返回扁平列表而不是嵌套树 —— 分页和嵌套天然冲突（一个根的 children 会跨页）。
    折叠关系用两个标记表达，前端想折叠想平铺都行：

    - ``impact_count``：**活动**机器事件上，同机**活动**服务事件的条数；
    - ``collapsed_by``：**活动**服务事件上，覆盖它的活动机器事件 fingerprint。

    ``days`` 窗口按 ``opened_at`` 裁剪，但**活动事件永远不被窗口裁掉**：
    一个开了 30 天还没恢复的故障，在「最近 7 天」视图里必须仍然看得见，
    否则页面会把持续故障显示成一切正常。
    """
    window = _clamp(days, DEFAULT_WINDOW_DAYS, 1, MAX_WINDOW_DAYS)
    limit = _clamp(limit, DEFAULT_LIMIT, 1, MAX_LIMIT)
    offset = _clamp(offset, 0, 0, 10**6)
    state = state if state in STATES else "all"
    moment = now or _utc_now()
    cutoff = _iso(moment - datetime.timedelta(days=window))

    ph = sd.ph()
    where = [f"(resolved_at IS NULL OR opened_at >= {ph})"]
    args = [cutoff]
    if kind in KINDS:
        where.append(f"kind = {ph}")
        args.append(kind)
    if severity in SEVERITIES:
        where.append(f"severity = {ph}")
        args.append(severity)
    target_machine = str(machine or "").strip().lower()
    if target_machine:
        where.append(f"machine = {ph}")
        args.append(target_machine)
    if state == "active":
        where.append("resolved_at IS NULL")
    elif state == "resolved":
        where.append("resolved_at IS NOT NULL")
    clause = " AND ".join(where)

    empty = {
        "events": [],
        "total": 0,
        "limit": limit,
        "offset": offset,
        "active_count": 0,
        "window_days": window,
        "generated_at": _iso(moment),
    }
    try:
        with sd.read_tx(conn=conn, db_path=db_path) as own:
            total, rows, active = _query(own, clause, args, limit, offset, moment)
    except Exception as exc:
        if sd.is_missing_table(exc):
            return empty
        raise

    # 折叠关系只在活动事件之间成立：已恢复的历史条目平铺展示更好读。
    # 走 collapse() 而不是在这儿重算一遍，K3 的归组规则只有那一处实现。
    machine_roots, impact = {}, {}
    for root in collapse(active):
        if root["kind"] != KIND_MACHINE or not root["machine"]:
            continue
        machine_roots[root["machine"]] = root["fingerprint"]
        impact[root["fingerprint"]] = root["impact_count"]

    events = []
    for row in rows:
        event = _row_to_event(row, now=moment)
        if event["state"] == "active":
            if event["kind"] == KIND_MACHINE:
                event["impact_count"] = impact.get(event["fingerprint"], 0)
            elif event["kind"] == KIND_SERVICE:
                # 与 collapse() 同口径：只有服务事件会挂到机器根下；公网事件不折叠
                event["collapsed_by"] = machine_roots.get(event["machine"])
        events.append(event)

    return {
        "events": events,
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "active_count": len(active),
        "window_days": window,
        "generated_at": _iso(moment),
    }


def _query(conn, clause, args, limit, offset, moment):
    """一次只读事务里取「总数 + 当前页 + 全部活动事件」，保证三者同一快照。"""
    ph = sd.ph()
    total = conn.execute(
        "SELECT COUNT(*) AS cnt FROM monitor_events WHERE " + clause, args
    ).fetchone()["cnt"]
    rows = conn.execute(
        "SELECT * FROM monitor_events WHERE "
        + clause
        + " ORDER BY (resolved_at IS NULL) DESC, opened_at DESC, id DESC"
        f" LIMIT {ph} OFFSET {ph}",
        args + [limit, offset],
    ).fetchall()
    return total, rows, _active_rows(conn, now=moment)


# ---- 保留策略 ------------------------------------------------------------


def prune(db_path=None, days=None, now=None, conn=None):
    """按保留天数删**已恢复**的旧事件；返回删除条数。0 天 = 不清理（默认）。

    活动事件永不删：还没恢复的故障不该因为「开得久」被清掉
    （与 oplog 按 ``started_at`` 一刀切的语义刻意不同）。
    """
    keep = retention_days() if days is None else max(0, int(days))
    if keep <= 0:
        return 0
    cutoff = _iso((now or _utc_now()) - datetime.timedelta(days=keep))
    try:
        with sd.write_tx(conn=conn, db_path=db_path) as own:
            cursor = own.execute(
                f"DELETE FROM monitor_events"
                f" WHERE resolved_at IS NOT NULL AND resolved_at < {sd.ph()}",
                (cutoff,),
            )
            return int(cursor.rowcount or 0)
    except Exception as exc:
        if sd.is_missing_table(exc):
            return 0
        raise


# ---- 路由 ----------------------------------------------------------------


def register_routes(app, resolve_db=None):
    """挂到既有 app 上（同 dashboard_layout / settings_store：resolve_db 每请求求值）。"""

    @app.route("/api/events", methods=["GET"])
    def get_monitor_events():
        """事件时间线（Track K）：机器/服务的状态转移，只读。

        query：`days`（默认 7，1–90）、`kind`（machine|service）、`machine`、
        `severity`（error|warn）、`state`（active|resolved|all，默认 all）、
        `limit`（默认 100，1–500）、`offset`。非法值一律退化为默认，不 400。

        活动事件不受 `days` 窗口裁剪；折叠关系见响应里的 `impact_count` /
        `collapsed_by`。本端点不触发任何探测，也不写库。
        """
        db_path = resolve_db() if resolve_db else None
        return jsonify(
            list_events(
                days=request.args.get("days", DEFAULT_WINDOW_DAYS),
                kind=request.args.get("kind"),
                machine=request.args.get("machine"),
                severity=request.args.get("severity"),
                state=request.args.get("state", "all"),
                limit=request.args.get("limit", DEFAULT_LIMIT),
                offset=request.args.get("offset", 0),
                db_path=db_path,
            )
        )

    return app
