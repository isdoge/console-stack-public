# -*- coding: utf-8 -*-
"""通用磁盘容量拟合与只读 PG 路由。

至少 8 个 8h 投影点、跨度至少 48h 才计算趋势；拟合不稳定、增长非正或
耗尽时间超过一年时不展示预计耗尽日期。预测不写库、不触发清理或扩容。
私人 Beszel 历史取数由 capacity_legacy 提供。
"""
from flask import jsonify

CACHE_TTL = 3600
HISTORY_TYPE = "480m"
MIN_SAMPLES = 8
MIN_SPAN_HOURS = 48
MIN_R_SQUARED = 0.5
MAX_MEANINGFUL_DAYS = 365


def linear_trend(points):
    """最小二乘拟合 ``[(hours, gb), ...]``，返回 ``(slope_per_hour, r_squared)``。

    点数不足或 x 全同（所有样本同一时刻）返回 ``(None, None)``：那种情况下
    「斜率」是没有定义的，硬算会得到 inf 或 0，两个都是谎话。
    """
    n = len(points)
    if n < 2:
        return None, None
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    sxx = sum((x - mean_x) ** 2 for x, _ in points)
    if sxx <= 0:
        return None, None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in points)
    slope = sxy / sxx
    syy = sum((y - mean_y) ** 2 for _, y in points)
    # syy=0 表示 y 恒定：拟合完美（斜率 0），r² 记 1.0 而不是 0/0
    r_squared = 1.0 if syy <= 0 else max(0.0, min(1.0, (sxy * sxy) / (sxx * syy)))
    return slope, r_squared


def build_trend(samples):
    """一台机器的样本 → 趋势字典；样本不够回 ``None``（不是回一个全 0 的假趋势）。

    ``samples`` 为 ``[(datetime, disk_total_gb, disk_used_gb), ...]``，顺序不限。
    """
    rows = sorted(
        (
            (moment, total, used)
            for moment, total, used in samples
            if moment is not None and total is not None and used is not None and total > 0
        ),
        key=lambda item: item[0],
    )
    if len(rows) < MIN_SAMPLES:
        return None
    span_hours = (rows[-1][0] - rows[0][0]).total_seconds() / 3600.0
    if span_hours < MIN_SPAN_HOURS:
        return None

    base = rows[0][0]
    points = [((moment - base).total_seconds() / 3600.0, used) for moment, _t, used in rows]
    slope_per_hour, r_squared = linear_trend(points)
    if slope_per_hour is None:
        return None

    daily_gb = slope_per_hour * 24.0
    total_gb = rows[-1][1]
    used_gb = rows[-1][2]
    free_gb = max(0.0, total_gb - used_gb)

    days_to_full = None
    if daily_gb > 0 and r_squared is not None and r_squared >= MIN_R_SQUARED:
        candidate = free_gb / daily_gb
        # 超过一年的「预计耗尽」没有决策价值（实测那台盘算出来 1800 天）
        if 0 < candidate <= MAX_MEANINGFUL_DAYS:
            days_to_full = round(candidate, 1)

    if daily_gb > 0.01:
        trend = "rising"
    elif daily_gb < -0.01:
        trend = "falling"
    else:
        trend = "flat"

    return {
        "samples": len(rows),
        "span_hours": round(span_hours, 1),
        "first_at": rows[0][0].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_at": rows[-1][0].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "disk_total_gb": round(total_gb, 1),
        "disk_used_gb": round(used_gb, 1),
        "disk_free_gb": round(free_gb, 1),
        "daily_gb": round(daily_gb, 3),
        "r_squared": round(r_squared, 3) if r_squared is not None else None,
        "trend": trend,
        # None 有三种含义：不在涨 / 拟合不稳 / 算出来超过一年。UI 一律显示成
        # 「暂不预测」，不要区分——区分了也没人能据此做决定。
        "days_to_full": days_to_full,
    }


def register_routes(app):
    """`GET /api/capacity`（同 monitor_events：薄封装，路由只做序列化）。

    路由只委托正式 PG raw→480m 投影，不读取历史适配器。
    """

    @app.route("/api/capacity", methods=["GET"])
    def get_capacity():
        """磁盘增长趋势：按 PG 8h 投影线性拟合，只读。

        只对 role=server 的机器计算；force 不触发历史数据源。
        `days_to_full` 为 null 表示不该预测（不在涨 / 拟合不稳 / 超过一年），
        UI 显示成「暂不预测」即可。本端点不写库、不触发任何清理动作。
        """
        # B6-4：正式端点只读 PG raw→480m 投影；force 没有额外外部数据源可刷新。
        # 延迟 import：monitoring_business 反向复用本模块的拟合与门槛常量。
        import monitoring_business as mb

        return jsonify(mb.disk_trends())

    return app
