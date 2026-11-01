# -*- coding: utf-8 -*-
"""B5 metric 命名单一事实源（B5-5 收口对齐）。

B5-1/2/3/4 都从本模块 import 常量，不各自定义。命名规则：
- 主机标量：payload json tag 直写（cpu_used_pct / mem_used_bytes / disk_used_bytes 等）
- per-NIC：``<base>@<nic_name>`` 后缀形式（base 是主机标量名）
- 容器：``container_<name>_<field>``（name 是 Docker 容器名，field ∈ {cpu_pct, memory_used_bytes}）
- 派生指标（评估用）：``<base>_<derivation>``（disk_used_pct / load_avg_5m）

时序语义：sampled_at 是源机器实际采样时间（ISO Z）；metric_value 是数值（bytes/pct/绝对值按名后缀自描述）。
"""

# ---- 主机标量（agent payload json tag 直写）----
METRIC_CPU_USED_PCT = "cpu_used_pct"
METRIC_MEM_USED_BYTES = "mem_used_bytes"
METRIC_MEM_TOTAL_BYTES = "mem_total_bytes"
METRIC_MEM_AVAILABLE_BYTES = "mem_available_bytes"
METRIC_MEM_USED_PCT = "mem_used_pct"
METRIC_SWAP_USED_BYTES = "swap_used_bytes"
METRIC_DISK_USED_BYTES = "disk_used_bytes"
METRIC_DISK_TOTAL_BYTES = "disk_total_bytes"
METRIC_LOAD_AVG_1M = "load_avg_1m"
METRIC_LOAD_AVG_5M = "load_avg_5m"
METRIC_LOAD_AVG_15M = "load_avg_15m"
METRIC_UPTIME_SEC = "uptime_sec"

# ---- per-NIC（累计计数器，bytes）----
METRIC_NET_SENT_TOTAL_BYTES = "net_sent_total_bytes"   # 注意：是 sent 不是 send
METRIC_NET_RECV_TOTAL_BYTES = "net_recv_total_bytes"


def nic_metric(base, nic):
    """per-NIC 指标名：``net_sent_total_bytes@ens5``。"""
    return "{}@{}".format(base, str(nic or "").strip())


def parse_nic_metric(metric_name):
    """拆出 (base, nic)；无 @ 的返回 (原名, "")。"""
    name = str(metric_name or "")
    if "@" in name:
        base, nic = name.split("@", 1)
        return base, nic.strip()
    return name, ""


# ---- 容器（per-container）----
METRIC_CONTAINER_PREFIX = "container_"


def container_metric(name, field):
    """容器指标名：``container_<name>_<field>``（field ∈ {'cpu_pct', 'memory_used_bytes'}）。"""
    return "{}{}_{}".format(METRIC_CONTAINER_PREFIX, name, field)


# ---- 派生指标（评估器用）----
METRIC_DISK_USED_PCT = "disk_used_pct"     # 评估器查这个
METRIC_LOAD_AVG_5M_DERIVED = "load_avg_5m"  # 与 METRIC_LOAD_AVG_5M 同字符串

# ---- 阈值（B0 冻结，B5-4 已硬编码，此处镜像供未来查）----
DISK_THRESHOLD_PCT = 85.0
DISK_SUSTAIN_MIN = 10
LOAD_SUSTAIN_MIN = 10
STALENESS_THRESHOLD_MIN = 12  # B1-2 冻结：2 周期 + 2min

# ---- 回填来源标记（B5-2 已用，此处镜像）----
BACKFILL_SOURCE = "beszel-backfill"
