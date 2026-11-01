# -*- coding: utf-8 -*-
"""monitoring_schema 的回归（B5-5 收口）：锁死 metric 命名，不再漂移。

B5-1/2/3/4 各自定义命名的教训：四个模块四种写法，真机 agent 上线后互相查不到
对方写的数据。本套用例把每个常量的字面值钉死——任何改名都要先过这里（改这里
等于承认改名，评审可见）。
"""
import pathlib
import sys

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import monitoring_schema as ms  # noqa: E402

# ---- 常量值锁死 ---------------------------------------------------------------


def test_host_scalar_metric_names_frozen():
    assert ms.METRIC_CPU_USED_PCT == "cpu_used_pct"
    assert ms.METRIC_MEM_USED_BYTES == "mem_used_bytes"
    assert ms.METRIC_MEM_TOTAL_BYTES == "mem_total_bytes"
    assert ms.METRIC_MEM_AVAILABLE_BYTES == "mem_available_bytes"
    assert ms.METRIC_MEM_USED_PCT == "mem_used_pct"
    assert ms.METRIC_SWAP_USED_BYTES == "swap_used_bytes"
    assert ms.METRIC_DISK_USED_BYTES == "disk_used_bytes"
    assert ms.METRIC_DISK_TOTAL_BYTES == "disk_total_bytes"
    assert ms.METRIC_LOAD_AVG_1M == "load_avg_1m"
    assert ms.METRIC_LOAD_AVG_5M == "load_avg_5m"
    assert ms.METRIC_LOAD_AVG_15M == "load_avg_15m"
    assert ms.METRIC_UPTIME_SEC == "uptime_sec"


def test_net_and_derived_metric_names_frozen():
    # sent 不是 send：与 agent payload 的 NIC 结构字段对齐。
    assert ms.METRIC_NET_SENT_TOTAL_BYTES == "net_sent_total_bytes"
    assert ms.METRIC_NET_RECV_TOTAL_BYTES == "net_recv_total_bytes"
    assert ms.METRIC_DISK_USED_PCT == "disk_used_pct"
    assert ms.METRIC_LOAD_AVG_5M_DERIVED == ms.METRIC_LOAD_AVG_5M == "load_avg_5m"
    assert ms.METRIC_CONTAINER_PREFIX == "container_"


def test_thresholds_and_backfill_marker_frozen():
    assert ms.DISK_THRESHOLD_PCT == 85.0
    assert ms.DISK_SUSTAIN_MIN == 10
    assert ms.LOAD_SUSTAIN_MIN == 10
    assert ms.STALENESS_THRESHOLD_MIN == 12  # B1-2 冻结：2 周期 + 2min
    assert ms.BACKFILL_SOURCE == "beszel-backfill"


# ---- nic_metric / parse_nic_metric 往返 --------------------------------------


def test_nic_metric_roundtrip():
    name = ms.nic_metric(ms.METRIC_NET_SENT_TOTAL_BYTES, "ens5")
    assert name == "net_sent_total_bytes@ens5"
    assert ms.parse_nic_metric(name) == ("net_sent_total_bytes", "ens5")


def test_nic_metric_strips_and_empty():
    assert ms.nic_metric("net_sent_total_bytes", " ens5 ") == "net_sent_total_bytes@ens5"
    assert ms.nic_metric("net_sent_total_bytes", None) == "net_sent_total_bytes@"
    assert ms.nic_metric("net_sent_total_bytes", "") == "net_sent_total_bytes@"


def test_parse_nic_metric_without_at_returns_original():
    assert ms.parse_nic_metric("disk_used_bytes") == ("disk_used_bytes", "")
    assert ms.parse_nic_metric("") == ("", "")
    assert ms.parse_nic_metric(None) == ("", "")


# ---- container_metric ---------------------------------------------------------


def test_container_metric_names():
    assert ms.container_metric("caddy", "cpu_pct") == "container_caddy_cpu_pct"
    assert ms.container_metric(
        "caddy", "memory_used_bytes"
    ) == "container_caddy_memory_used_bytes"
