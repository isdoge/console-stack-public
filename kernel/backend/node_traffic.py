# -*- coding: utf-8 -*-
"""节点月流量账本（E1）：把 Beszel 的累计网卡计数器折算成「本月已用」。

数据来源是 Beszel `system_stats.stats.ni`——每网卡一个四元组
`[上行速率, 下行速率, 累计发送, 累计接收]`，后两位是**单调累计计数器**
（实测 kr `ens5` = 714717441 / 5526032382 字节）。因此不需要在节点上装 vnstat。

三条纪律：

1. **只算计费网卡**：`stats.b` 是全网卡速率之和，含 `tailscale0`，直接拿来记配额会把
   内网流量算进去。按 `traffic_nic`（machines.yaml 显式指定）或排除表自动挑。
2. **存增量不存绝对值**：库里记「本月累计增量」与「上次读到的计数器值」，两次采样
   之差累加。采样间隔不均不影响正确性，漏采只会丢掉「漏采期间恰好发生重置」的那部分。
3. **计数器变小 = 归零**：agent 重装 / 机器重启会让计数器从 0 重来，此时把当前值整体
   计入增量（而不是算出一个负数）。
4. **配额只算出站**：`sent_bytes`（发送/TX）计入配额，`recv_bytes`（接收/RX）不计。
   跟腾讯云/阿里云轻量应用服务器、Oracle always-free 的计费口径一致——入站免费。

月份键取 **UTC 自然月**，与 Lightsail 的账单周期不一定对齐——这里的用途是「快到量了
提醒一声」，不是对账。GB 一律按 1024³ 折算，与卡片上磁盘/内存的口径一致。

存储（PG-02）：连接由 `storage_dispatch` 按 `CONSOLE_STORAGE_ENGINE` 选择。PG 下
`record_sample` 在**每机器事务锁**（`pg_locks.traffic_lock`）内完成「读→算→写」，
并拒绝时间倒退的旧快照（§4.2）——否则两个并发采样都基于同一条旧基线累加，
会把同一段流量算两遍。
"""
import datetime

import machine_config as mc
import storage_dispatch as sd

GIB = 1024 ** 3

# 不计入配额的网卡：回环、VPN/隧道、容器与网桥虚拟口，以及 NAS 上与物理口重复的 ovs 镜像。
_EXCLUDED_EXACT = frozenset({"lo", "tailscale0", "Meta"})
_EXCLUDED_PREFIXES = ("docker", "veth", "br-", "virbr", "wg", "tun", "tap", "utun", "zt")
_EXCLUDED_SUFFIXES = ("-ovs",)

# 默认告警线：用满配额的 80% 进今日关注
WARN_RATIO = 0.8

# M1：月底预测的最短观测窗口。设计 §5.7 定的 72 小时——不到三天的样本外推一个月，
# 一次大文件传输就能把预测抬到天上去。
MIN_OBSERVED_HOURS = 72


def is_billable_nic(name):
    """该网卡是否计入配额。"""
    nic = str(name or "").strip()
    if not nic or nic in _EXCLUDED_EXACT:
        return False
    if nic.startswith(_EXCLUDED_PREFIXES) or nic.endswith(_EXCLUDED_SUFFIXES):
        return False
    return True


def _totals(values):
    """`ni` 四元组的后两位（累计发送/接收），取不到返回 None。"""
    if not isinstance(values, (list, tuple)) or len(values) < 4:
        return None
    sent, recv = values[2], values[3]
    if not isinstance(sent, (int, float)) or not isinstance(recv, (int, float)):
        return None
    if isinstance(sent, bool) or isinstance(recv, bool):
        return None
    if sent < 0 or recv < 0:
        return None
    return int(sent), int(recv)


def pick_nic(ni, preferred=""):
    """从 `stats.ni` 选计费网卡，返回 (nic, sent_total, recv_total)；无可用网卡返回 None。

    `preferred`（machines.yaml 的 traffic_nic）命中就用它；没配或改名了就退回自动挑，
    避免云厂商重建实例换了网卡名之后账本静默断流。自动挑取累计量最大的那块。
    """
    if not isinstance(ni, dict) or not ni:
        return None

    preferred = str(preferred or "").strip()
    if preferred and preferred in ni:
        totals = _totals(ni[preferred])
        if totals:
            return (preferred, *totals)

    best = None
    for nic, values in ni.items():
        if not is_billable_nic(nic):
            continue
        totals = _totals(values)
        if not totals:
            continue
        if best is None or sum(totals) > sum(best[1:]):
            best = (nic, *totals)
    return best


def nic_rates(ni, nic):
    """指定网卡的瞬时上下行速率（字节/秒），取不到返回 (None, None)。

    刻意不用 `stats.b`：那是全网卡之和，节点上会把 tailscale0 的流量混进来。
    """
    if not isinstance(ni, dict):
        return None, None
    values = ni.get(nic)
    if not isinstance(values, (list, tuple)) or len(values) < 2:
        return None, None
    def ok(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0

    up, down = values[0], values[1]
    return (up if ok(up) else None), (down if ok(down) else None)


def _month_key(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m")


def _iso(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def sample_time(value):
    """Parse an upstream UTC/offset timestamp without inventing a processing time."""
    if isinstance(value, str):
        try:
            value = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime.datetime) or value.tzinfo is None:
        return None
    return value.astimezone(datetime.timezone.utc)


def record_sample(machine_id, nic, sent_total, recv_total, db_path=None, now=None,
                  conn=None):
    """记一次采样，返回该机器当月账本行（dict）。

    月份翻页、计数器归零、换网卡都在这里收敛：都视作「重新起算」，只把当前绝对值
    存成新基线，不产生假的巨额增量。

    **并发与旧快照**（提案 §4.2）：PG 下整个「读→算→写」在同一事务、同一把
    每机器 advisory lock 内完成，并按 `updated_at` 拒绝时间倒退的采样——旧快照若被
    接受，计数器看起来会「变小」，被当成 agent 重启把整段流量重复计入。
    """
    machine = str(machine_id or "").strip().lower()
    if not machine:
        return None
    sent_total = max(0, int(sent_total))
    recv_total = max(0, int(recv_total))
    now = now or datetime.datetime.now(datetime.timezone.utc)
    month = _month_key(now)
    stamp = _iso(now)

    import pg_locks

    with sd.write_tx(conn=conn, db_path=db_path,
                     lock=pg_locks.traffic_lock(machine)) as own:
        if sd.use_postgres() and conn is not None:
            pg_locks.acquire_many(own, (pg_locks.MAINTENANCE_SHARED, pg_locks.traffic_lock(machine)))
        row = own.execute(
            f"SELECT * FROM machine_traffic WHERE machine = {sd.ph()}", (machine,)
        ).fetchone()

        if row is None:
            own.execute(
                f"""
                INSERT INTO machine_traffic
                    (machine, month, sent_bytes, recv_bytes,
                     last_sent_total, last_recv_total, nic, resets, started_at, updated_at)
                VALUES ({sd.ph()}, {sd.ph()}, 0, 0, {sd.ph()}, {sd.ph()}, {sd.ph()}, 0,
                        {sd.ph()}, {sd.ph()})
                """,
                (machine, month, sent_total, recv_total, nic or "", stamp, stamp),
            )
            return _reload(own, machine)

        previous_time = sample_time(row["updated_at"])
        if previous_time is not None and sample_time(stamp) <= previous_time:
            # 旧快照：不改任何状态，返回当前行（幂等），绝不按「计数器变小」处理
            return _row_to_dict(row)

        month_changed = row["month"] != month
        nic_changed = (row["nic"] or "") != (nic or "")
        reset = sent_total < row["last_sent_total"] or recv_total < row["last_recv_total"]

        if month_changed:
            sent_bytes, recv_bytes = 0, 0
            resets = 0
            started_at = stamp
        elif nic_changed:
            # 换网卡：新旧计数器不可比，只把基线换成新网卡的当前值，
            # 本月已累计的量必须留着（否则实例重建会白丢当月数据）
            sent_bytes, recv_bytes = row["sent_bytes"], row["recv_bytes"]
            resets = row["resets"]
            started_at = row["started_at"]
        elif reset:
            # 计数器从 0 重来：当前绝对值就是重启以来的增量，全部计入本月
            sent_bytes = row["sent_bytes"] + sent_total
            recv_bytes = row["recv_bytes"] + recv_total
            resets = row["resets"] + 1
            started_at = row["started_at"]
        else:
            sent_bytes = row["sent_bytes"] + (sent_total - row["last_sent_total"])
            recv_bytes = row["recv_bytes"] + (recv_total - row["last_recv_total"])
            resets = row["resets"]
            started_at = row["started_at"]

        own.execute(
            f"""
            UPDATE machine_traffic
               SET month = {sd.ph()}, sent_bytes = {sd.ph()}, recv_bytes = {sd.ph()},
                   last_sent_total = {sd.ph()}, last_recv_total = {sd.ph()}, nic = {sd.ph()},
                   resets = {sd.ph()}, started_at = {sd.ph()}, updated_at = {sd.ph()}
             WHERE machine = {sd.ph()}
            """,
            (
                month, sent_bytes, recv_bytes, sent_total, recv_total,
                nic or "", resets, started_at or stamp, stamp, machine,
            ),
        )
        return _reload(own, machine)


def _reload(conn, machine):
    return _row_to_dict(
        conn.execute(
            f"SELECT * FROM machine_traffic WHERE machine = {sd.ph()}", (machine,)
        ).fetchone()
    )


def _row_to_dict(row):
    return dict(row) if row is not None else None


def record_from_systems(systems, db_path=None, now=None, conn=None):
    """从 /api/monitor 快照批量记账；**所有**机器都记本月累计。

    只有配了 traffic_quota_gb 的机器才算百分比与告警（usage/quota_alerts 里判断），
    但累计量本身对 server 也有价值（卡片「本月累计」格），所以这里不再按配额过滤。
    快照里的 `net_nic`/`net_sent_total`/`net_recv_total` 由 beszel_client 用 pick_nic 挑好。
    单台失败不影响其它机器。
    """
    recorded = []
    for entry in systems or []:
        machine = entry.get("machine")
        if not machine:
            continue
        if not mc.get_machine(machine):
            continue
        sent = entry.get("net_sent_total")
        recv = entry.get("net_recv_total")
        if sent is None or recv is None:
            continue
        # now is an explicit offline/test injection only, never an implicit clock fallback.
        sampled_at = sample_time(entry.get("net_sampled_at")) if now is None else sample_time(now)
        if sampled_at is None:
            continue
        try:
            row = record_sample(
                machine, entry.get("net_nic") or "", sent, recv,
                db_path=db_path, now=sampled_at, conn=conn,
            )
        except Exception:  # noqa: BLE001 —— 记账失败不该拖垮监控快照
            if conn is not None:
                # 共用调用方事务时不能吞：PG 里语句失败会中止整个事务，
                # 吞掉异常只会让调用方在后续语句上拿到更难懂的报错。
                raise
            continue
        if row:
            recorded.append(row)
    return recorded


def usage(machine_id, db_path=None, conn=None):
    """本月用量摘要；无采样记录返回 None。

    无配额（nas）也返回累计量，只是 `quota_gb=None`、`pct=None`——卡片据此只显
    本月累计、不画条、不告警（§7.2 契约 4）。
    """
    registry = mc.get_machine(machine_id)
    if not registry:
        return None

    with sd.read_tx(conn=conn, db_path=db_path) as own:
        row = own.execute(
            f"SELECT * FROM machine_traffic WHERE machine = {sd.ph()}", (registry["id"],)
        ).fetchone()
    if row is None:
        return None

    quota_gb = registry.get("traffic_quota_gb")
    # 配额只算出站（sent/TX）：腾讯云/阿里云轻量应用服务器的流量包只扣外网出流量，
    # Oracle always-free 标称亦是「10TB/月出站」，入站普遍免费——按 sent+recv 算会把
    # 免费的入站流量也计进配额，白白抬高 pct、更容易误触 80% 告警。sent_gb/recv_gb 两个
    # 分量原样保留在返回值里，前端仍能看到完整双向流量，只是配额百分比不含入站。
    used_bytes = int(row["sent_bytes"])
    used_gb = used_bytes / GIB
    summary = {
        "month": row["month"],
        "nic": row["nic"] or "",
        "sent_gb": round(int(row["sent_bytes"]) / GIB, 2),
        "recv_gb": round(int(row["recv_bytes"]) / GIB, 2),
        "used_gb": round(used_gb, 2),
        "quota_gb": int(quota_gb) if quota_gb else None,
        "pct": round(min(999.0, used_gb * 100.0 / quota_gb), 1) if quota_gb else None,
        # 计数从装 agent 那天起算，首月天然偏小；重置次数也一并暴露，便于判断数据可信度
        "since": row["started_at"],
        "updated_at": row["updated_at"],
        "resets": int(row["resets"]),
    }
    # M1：观测不足 72 小时时为 None，前端据此不显示预测（不是显示 0）
    summary["projection"] = month_projection(summary)
    return summary


def _days_in_month(moment):
    if moment.month == 12:
        return 31
    nxt = moment.replace(month=moment.month + 1, day=1)
    return (nxt - moment.replace(day=1)).days


def month_projection(summary, now=None):
    """M1 月底出站预测：按已观测速率线性外推到本月最后一天。

    返回 ``None`` 表示**不该显示预测**，四种情况：没有 ``since``、观测不足
    ``MIN_OBSERVED_HOURS``（设计 §5.7）、本月已经过完（剩余 0 天，此时
    ``used_gb`` 就是终值，再给个「预测」只会让人以为还会涨）、以及**账本月份不是
    当前月**——agent 掉线多日或刚跨月还没采到新样本时，``used_gb`` 是上个月的量，
    拿它推算本月会给出一个安静的错数字。

    ``partial_month=True`` 表示账本不是从月初起算的（月中装的 agent、或本月首次
    采样晚于 1 号）——此时预测**只覆盖 since 之后**的部分，UI 必须标注「从首次
    采样起」，否则用户会拿它跟云厂商的整月账单对不上。

    刻意用「已观测速率 × 整月天数」而不是「已用量 + 日均 × 剩余天数」：后者在
    partial_month 下会把 since 之前那段空白当成 0 流量，系统性低估。
    """
    if not isinstance(summary, dict):
        return None
    since_raw = str(summary.get("since") or "").strip()
    if not since_raw:
        return None
    since = sample_time(since_raw)
    if since is None:
        return None

    moment = now or datetime.datetime.now(datetime.timezone.utc)
    # 账本停在上个月（agent 掉线 / 刚跨月还没采样）→ used_gb 不属于本月，不推算
    if str(summary.get("month") or "") != _month_key(moment):
        return None
    observed_hours = (moment - since).total_seconds() / 3600.0
    if observed_hours < MIN_OBSERVED_HOURS:
        return None

    used_gb = float(summary.get("used_gb") or 0.0)
    daily_gb = used_gb / (observed_hours / 24.0)
    total_days = _days_in_month(moment)
    month_start = moment.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    # 「本月还剩几天」按自然日算：跨月那一刻剩余为 0，不做预测
    remaining_days = total_days - (moment - month_start).total_seconds() / 86400.0
    if remaining_days <= 0:
        return None

    projected_gb = daily_gb * total_days
    quota_gb = summary.get("quota_gb")
    return {
        "projected_gb": round(projected_gb, 2),
        "daily_gb": round(daily_gb, 2),
        "observed_hours": round(observed_hours, 1),
        "remaining_days": round(remaining_days, 1),
        # since 晚于月初 = 账本没覆盖整月，预测是「按这个速率跑满一个月」的意思
        "partial_month": since > month_start,
        "quota_gb": int(quota_gb) if quota_gb else None,
        "projected_pct": (
            round(min(999.0, projected_gb * 100.0 / quota_gb), 1) if quota_gb else None
        ),
        "over_quota": bool(quota_gb and projected_gb > quota_gb),
    }


def quota_alerts(db_path=None, warn_ratio=WARN_RATIO):
    """超阈值的机器列表，供 attention 消费。只读库，不发起任何探测。"""
    alerts = []
    for machine in mc.load_machines().values():
        if not machine.get("traffic_quota_gb"):
            continue
        try:
            summary = usage(machine["id"], db_path=db_path)
        except Exception as exc:  # noqa: BLE001
            # 表未建时今日关注不该整块塌掉；连接不可达/权限不足仍然向上抛（D10）。
            if sd.is_missing_table(exc):
                continue
            raise
        if not summary or summary["pct"] is None:
            continue
        if summary["pct"] < warn_ratio * 100:
            continue
        alerts.append({"machine": machine, "usage": summary})
    return alerts
