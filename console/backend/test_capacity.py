# -*- coding: utf-8 -*-
"""capacity.py 回归（Track M2）：线性拟合 + 四条退化闸门 + 只读契约。

这个模块最容易做错的是「太自信」，所以用例的重点在**什么时候不该给数字**：
- 样本不足 / 跨度不足 → 整条趋势 None（不是给一个全 0 的假趋势）；
- 在减少 / 持平 → 有增长率但没有耗尽日期；
- 拟合差（r² 低）→ 同上（设计 §5.7「趋势不稳定时不显示预计耗尽」）；
- 算出来超过一年 → 同上（线上实测 6/6 台都落在这一档）。
"""
import datetime
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import capacity  # noqa: E402

BASE = datetime.datetime(2026, 8, 8, 0, 0, 0)


def samples(days, total, start_used, daily, step_hours=8, noise=None):
    """按固定日增造样本；`noise` 是逐点偏移量列表（造拟合差的场景用）。"""
    rows = []
    steps = int(days * 24 / step_hours)
    for index in range(steps):
        hours = index * step_hours
        used = start_used + daily * (hours / 24.0)
        if noise:
            used += noise[index % len(noise)]
        rows.append((BASE + datetime.timedelta(hours=hours), total, used))
    return rows


# ---------------------------------------------------------------------------
# linear_trend
# ---------------------------------------------------------------------------


def test_linear_trend_recovers_known_slope():
    points = [(0.0, 10.0), (24.0, 12.0), (48.0, 14.0), (72.0, 16.0)]
    slope, r_squared = capacity.linear_trend(points)
    assert slope == pytest.approx(2.0 / 24.0)
    assert r_squared == pytest.approx(1.0)


def test_linear_trend_flat_series_is_perfect_fit_not_zero_r2():
    """y 恒定时 syy=0，硬算 r² 会 0/0；这里必须给 1.0（拟合完美，斜率 0）。"""
    slope, r_squared = capacity.linear_trend([(0.0, 5.0), (24.0, 5.0), (48.0, 5.0)])
    assert slope == pytest.approx(0.0)
    assert r_squared == pytest.approx(1.0)


def test_linear_trend_needs_two_distinct_x():
    assert capacity.linear_trend([]) == (None, None)
    assert capacity.linear_trend([(1.0, 2.0)]) == (None, None)
    # 所有样本同一时刻：斜率无定义，不许硬算出 inf
    assert capacity.linear_trend([(3.0, 1.0), (3.0, 9.0)]) == (None, None)


# ---------------------------------------------------------------------------
# build_trend 的退化闸门
# ---------------------------------------------------------------------------


def test_growing_disk_gets_days_to_full():
    """10 GB 空闲、每天涨 1 GB → 约 10 天写满，这是该给数字的场景。"""
    trend = capacity.build_trend(samples(days=10, total=100.0, start_used=80.0, daily=1.0))
    assert trend["trend"] == "rising"
    assert trend["daily_gb"] == pytest.approx(1.0, abs=0.01)
    assert trend["days_to_full"] == pytest.approx(10.0, abs=0.5)


def test_too_few_samples_returns_none():
    rows = samples(days=10, total=100.0, start_used=80.0, daily=1.0)[: capacity.MIN_SAMPLES - 1]
    assert capacity.build_trend(rows) is None


def test_too_short_span_returns_none():
    """样本够多但都挤在 1 天内 —— 跨度不足同样不出趋势。"""
    rows = samples(days=1, total=100.0, start_used=80.0, daily=1.0, step_hours=1)
    assert len(rows) >= capacity.MIN_SAMPLES
    assert capacity.build_trend(rows) is None


def test_shrinking_disk_has_rate_but_no_eta():
    """在腾空间（线上 ALY/TXY 就是这样）：报趋势，不报耗尽。"""
    trend = capacity.build_trend(samples(days=10, total=100.0, start_used=50.0, daily=-0.5))
    assert trend["trend"] == "falling"
    assert trend["daily_gb"] < 0
    assert trend["days_to_full"] is None


def test_flat_disk_has_no_eta():
    trend = capacity.build_trend(samples(days=10, total=100.0, start_used=50.0, daily=0.0))
    assert trend["trend"] == "flat"
    assert trend["days_to_full"] is None


def test_noisy_series_reports_rate_but_no_eta():
    """拟合差 = 趋势不稳定（线上 NAS 就是这样：下载与清理交替）。"""
    trend = capacity.build_trend(
        samples(days=20, total=100.0, start_used=80.0, daily=0.2, noise=[0, 9, -9, 4, -6])
    )
    assert trend["r_squared"] < capacity.MIN_R_SQUARED
    assert trend["daily_gb"] is not None
    assert trend["days_to_full"] is None


def test_eta_beyond_one_year_is_dropped():
    """线上 US：拟合很好（r²=0.86）但算出 511 天——超过一年就没有决策价值。"""
    trend = capacity.build_trend(samples(days=20, total=500.0, start_used=100.0, daily=0.2))
    assert trend["r_squared"] > capacity.MIN_R_SQUARED
    assert trend["daily_gb"] == pytest.approx(0.2, abs=0.01)
    assert trend["days_to_full"] is None


def test_bad_rows_are_skipped_not_fatal():
    rows = samples(days=10, total=100.0, start_used=80.0, daily=1.0)
    rows += [(None, 100.0, 81.0), (BASE, None, 81.0), (BASE, 0.0, 0.0)]
    assert capacity.build_trend(rows)["samples"] == len(rows) - 3


def test_trend_shape():
    trend = capacity.build_trend(samples(days=10, total=100.0, start_used=80.0, daily=1.0))
    assert set(trend) == {
        "samples", "span_hours", "first_at", "last_at",
        "disk_total_gb", "disk_used_gb", "disk_free_gb",
        "daily_gb", "r_squared", "trend", "days_to_full",
    }
