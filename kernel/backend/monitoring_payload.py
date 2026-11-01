# -*- coding: utf-8 -*-
"""B5-5 收口：Go Agent 嵌套 Payload → 扁平 metric_name/value 的拍平适配器。

B4-1 的 Batch.Samples 是嵌套 Payload（host/containers，agent/sender/http.go），
B4-2 的 PG 三表吃扁平 ``{metric_name, metric_value, sampled_at}`` 行
（monitoring_ingest.insert_samples）。B4 部署后两者一直没接：真实 agent 上报
会被 426（顶层字段叫 ``protocol`` 不是 ``protocol_version``）再 400（samples
形状不符）永久拒。本模块是两者之间的权威转换层（B5-5 收口裁决）。

metric 命名与消费方逐字对齐（B5-1 比对 / B5-3 业务按这些名字读 PG）：
- host.* 数值字段 → payload.go 的 JSON 字段名原样（``cpu_used_pct``…）；
- host.nics.<nic>.sent_bytes_total → ``net_sent_total_bytes@<nic>``
  （monitoring_business.nic_metric 的格式）；
- containers[] → ``container_<name>_cpu_pct`` / ``container_<name>_memory_used_bytes``
  （monitoring_compare.container_metric_name 的格式）。
命名常量定义在正被并行对齐的 B5 模块里，这里按字面值自包含，避免引入
对未收口模块的 import 依赖。

忽略与跳过：
- 顶层未知字段（sample_interval_sec / seq / dropped_total / errors）直接忽略，
  不报错——前向兼容的余量；
- 样本内 host.rates / dropped_total / errors 忽略（速率是消费端差分，不落库）；
- None / 非数值 / bool / NaN / Inf 字段跳过（真实 agent 的 json.Marshal 发不出
  NaN/Inf，这里宽容跳过与 monitoring_api._samples_error 的整批拒收都不影响
  真实流量；任务书两处规格矛盾，按测试规格「跳过」落）；
- 样本缺 host 且缺 containers（产出零条 metric）是坏形状，抛 ValueError
  ——payload.go 的「缺失=显式」用 ``host: null`` 表达，什么都不带的样本
  不是合法观测。

协议版本不符 / machine_id 缺失 / samples 非法形状（非 list、元素非 dict、
sampled_at 坏）抛 ValueError，由调用方（monitoring_api ingest）映射 400；
426 留给真正的版本协商失败。
"""
import math
import re
from datetime import datetime

PROTOCOL_VERSION = "v1"

# monitoring_business.nic_metric 的字面格式：net_<dir>_total_bytes@<nic>。
NIC_SENT_METRIC = "net_sent_total_bytes"
NIC_RECV_METRIC = "net_recv_total_bytes"
# monitoring_compare.container_metric_name 的字面格式：container_<name>_<field>。
CONTAINER_CPU_FIELD = "cpu_pct"
CONTAINER_MEMORY_FIELD = "memory_used_bytes"

# host 拍平时跳过的非数值容器字段（rates 是差分速率，消费端自己算）。
_HOST_MAP_FIELDS = frozenset({"nics", "rates"})


def _finite_number(value):
    """有限数值返回 float；None/bool/非数值/NaN/Inf 返回 None（跳过）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _valid_sampled_at(value):
    """ISO 8601 字符串才有效（与 monitoring_api._parse_sampled_at 同口径）。

    Go `time.Time` JSON 序列化走 RFC3339Nano（如 ``2026-10-24T04:36:34.762665865Z``），
    Python 3.10 的 ``fromisoformat`` 不支持超过 6 位小数秒；先截断到微秒再解析。
    """
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = _truncate_fraction(text)
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return True  # naive 一律按 UTC 解释（monitoring_api._parse_sampled_at 同口径）


def _truncate_fraction(text):
    """ISO 8601 小数秒截断到微秒（fromisoformat 上限）；无小数秒原样返回。"""
    # 匹配形如 "...T12:34:56.789123456+00:00" 的小数秒段
    m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})\.(\d+)([+-].*|)$", text)
    if not m:
        return text
    head, frac, tail = m.groups()
    frac = frac[:6].ljust(6, "0")
    return head + "." + frac + tail


def _expand_host(host, sampled_at, samples):
    """host dict → 扁平行。nics 单独处理；rates 与其余 map 字段跳过。"""
    if not isinstance(host, dict):
        return
    for key, value in host.items():
        if key in _HOST_MAP_FIELDS:
            continue
        number = _finite_number(value)
        if number is None:
            continue
        samples.append(
            {"metric_name": key, "metric_value": number, "sampled_at": sampled_at}
        )
    nics = host.get("nics")
    if not isinstance(nics, dict):
        return
    for nic, counters in nics.items():
        if not isinstance(counters, dict):
            continue
        for field, base in (
            ("sent_bytes_total", NIC_SENT_METRIC),
            ("recv_bytes_total", NIC_RECV_METRIC),
        ):
            number = _finite_number(counters.get(field))
            if number is None:
                continue
            samples.append(
                {
                    "metric_name": "%s@%s" % (base, nic),
                    "metric_value": number,
                    "sampled_at": sampled_at,
                }
            )


def _expand_containers(containers, sampled_at, samples):
    """containers[] → container_<name>_<field> 行。"""
    if not isinstance(containers, list):
        return
    for container in containers:
        if not isinstance(container, dict):
            continue
        name = container.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        for field in (CONTAINER_CPU_FIELD, CONTAINER_MEMORY_FIELD):
            number = _finite_number(container.get(field))
            if number is None:
                continue
            samples.append(
                {
                    "metric_name": "container_%s_%s" % (name, field),
                    "metric_value": number,
                    "sampled_at": sampled_at,
                }
            )


def expand_agent_payload(payload):
    """嵌套 Batch dict → 扁平形状。

    返回 ``{"machine_id", "agent_version", "protocol_version", "samples"}``，
    samples 元素是 ``{metric_name, metric_value, sampled_at}``（sampled_at
    原样字符串，不改写——契约 §3 规则 2）。协议版本不符 / machine_id 缺失 /
    samples 形状非法抛 ValueError。
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    protocol = payload.get("protocol")
    if protocol != PROTOCOL_VERSION:
        raise ValueError("protocol not supported: %r" % (protocol,))
    machine_id = payload.get("machine_id")
    if not isinstance(machine_id, str) or not machine_id.strip():
        raise ValueError("machine_id required")
    agent_version = payload.get("agent_version")
    samples_in = payload.get("samples")
    if not isinstance(samples_in, list):
        raise ValueError("samples must be a list")

    samples = []
    for index, sample in enumerate(samples_in):
        if not isinstance(sample, dict):
            raise ValueError("samples[%d] must be an object" % index)
        sampled_at = sample.get("sampled_at")
        if not _valid_sampled_at(sampled_at):
            raise ValueError("samples[%d].sampled_at must be ISO 8601" % index)
        if agent_version is None:
            per_sample = sample.get("agent")
            if isinstance(per_sample, str) and per_sample.strip():
                agent_version = per_sample
        before = len(samples)
        _expand_host(sample.get("host"), sampled_at, samples)
        _expand_containers(sample.get("containers"), sampled_at, samples)
        if len(samples) == before:
            raise ValueError(
                "samples[%d] produced no metrics (host/containers missing)" % index
            )

    return {
        "machine_id": machine_id,
        "agent_version": agent_version if isinstance(agent_version, str) else "",
        "protocol_version": PROTOCOL_VERSION,
        "samples": samples,
    }
