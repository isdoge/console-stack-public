# -*- coding: utf-8 -*-
"""monitoring_payload 回归（B5-5 收口）：Go Agent 嵌套 Payload 的拍平适配器。

覆盖：host 数值字段原样拍平、nics 用 business 的 ``@<nic>`` 格式、containers
用 compare 的 ``container_<name>_<field>`` 格式、顶层 protocol→protocol_version
映射、machine_id/协议版本缺失拒收、NaN/Inf/None/bool 跳过、未知顶层字段忽略。
"""
import math
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import monitoring_payload as mp  # noqa: E402


def _batch(**overrides):
    batch = {
        "protocol": "v1",
        "agent_version": "0.1.0",
        "machine_id": "nas",
        "sample_interval_sec": 300,
        "samples": [
            {
                "protocol": "v1",
                "agent": "0.1.0",
                "machine_id": "nas",
                "sampled_at": "2026-10-24T10:00:00Z",
                "seq": 42,
                "host": {
                    "cpu_used_pct": 12.5,
                    "mem_total_bytes": 8192,
                    "mem_used_bytes": 4096,
                    "disk_used_bytes": 100,
                    "disk_total_bytes": 200,
                    "load_avg_5m": 0.5,
                },
                "containers": [
                    {"name": "nginx", "cpu_pct": 1.5, "memory_used_bytes": 100}
                ],
            }
        ],
    }
    batch.update(overrides)
    return batch


def _names(result):
    return [s["metric_name"] for s in result["samples"]]


def test_expand_host_fields_flat():
    result = mp.expand_agent_payload(_batch())
    by_name = {s["metric_name"]: s for s in result["samples"]}
    assert by_name["cpu_used_pct"] == {"metric_name": "cpu_used_pct", "metric_value": 12.5, "sampled_at": "2026-10-24T10:00:00Z"}
    assert by_name["mem_total_bytes"]["metric_value"] == 8192
    assert by_name["mem_used_bytes"]["metric_value"] == 4096
    assert by_name["disk_used_bytes"]["metric_value"] == 100
    assert by_name["load_avg_5m"]["metric_value"] == 0.5
    # sampled_at 逐字段继承
    assert all(s["sampled_at"] == "2026-10-24T10:00:00Z" for s in result["samples"])


def test_expand_nics_use_business_at_format():
    batch = _batch()
    batch["samples"][0]["host"]["nics"] = {
        "ens5": {"sent_bytes_total": 1000, "recv_bytes_total": 2000}
    }
    result = mp.expand_agent_payload(batch)
    by_name = {s["metric_name"]: s["metric_value"] for s in result["samples"]}
    assert by_name["net_sent_total_bytes@ens5"] == 1000
    assert by_name["net_recv_total_bytes@ens5"] == 2000


def test_expand_containers_use_compare_prefix_format():
    result = mp.expand_agent_payload(_batch())
    by_name = {s["metric_name"]: s["metric_value"] for s in result["samples"]}
    assert by_name["container_nginx_cpu_pct"] == 1.5
    assert by_name["container_nginx_memory_used_bytes"] == 100


def test_protocol_field_maps_to_protocol_version():
    result = mp.expand_agent_payload(_batch())
    assert result["protocol_version"] == "v1"
    assert result["machine_id"] == "nas"
    assert result["agent_version"] == "0.1.0"


def test_missing_machine_id_raises():
    batch = _batch()
    del batch["machine_id"]
    with pytest.raises(ValueError, match="machine_id"):
        mp.expand_agent_payload(batch)


def test_protocol_mismatch_raises():
    with pytest.raises(ValueError, match="protocol"):
        mp.expand_agent_payload(_batch(protocol="v0"))


def test_non_finite_and_non_numeric_skipped():
    batch = _batch()
    batch["samples"][0]["host"].update(
        {
            "cpu_used_pct": float("nan"),
            "mem_total_bytes": float("inf"),
            "mem_used_bytes": None,
            "disk_used_bytes": True,
            "load_avg_1m": "0.4",
        }
    )
    result = mp.expand_agent_payload(batch)
    names = _names(result)
    assert "cpu_used_pct" not in names
    assert "mem_total_bytes" not in names
    assert "mem_used_bytes" not in names
    assert "disk_used_bytes" not in names
    assert "load_avg_1m" not in names
    # 其余合法字段照常
    assert "disk_total_bytes" in names


def test_unknown_top_level_fields_ignored():
    batch = _batch(dropped_total=7, errors={"docker": "boom"}, seq=99)
    batch["samples"][0]["dropped_total"] = 3
    batch["samples"][0]["errors"] = {"docker": "boom"}
    result = mp.expand_agent_payload(batch)
    assert len(result["samples"]) == 8  # 6 host 字段 + 2 container 字段
    assert all("dropped" not in s["metric_name"] and "errors" not in s["metric_name"] for s in result["samples"])


def test_container_cpu_delta_sec_is_transport_metadata_not_a_metric():
    # B6-1：agent 容器样本新增 cpu_delta_sec（CPU 差分窗秒数）。它是采样窗
    # 元数据，不进 metric 流——flatten 行数不变、无 container_*_cpu_delta_sec。
    batch = _batch()
    batch["samples"][0]["containers"][0]["cpu_delta_sec"] = 1.0
    result = mp.expand_agent_payload(batch)
    names = _names(result)
    assert len(result["samples"]) == 8
    assert not any("cpu_delta_sec" in n for n in names)


def test_agent_version_falls_back_to_sample_level():
    batch = _batch()
    del batch["agent_version"]
    result = mp.expand_agent_payload(batch)
    assert result["agent_version"] == "0.1.0"


def test_sample_without_host_or_containers_raises():
    batch = _batch()
    del batch["samples"][0]["host"]
    del batch["samples"][0]["containers"]
    with pytest.raises(ValueError, match="no metrics"):
        mp.expand_agent_payload(batch)


def test_samples_shape_errors():
    with pytest.raises(ValueError, match="list"):
        mp.expand_agent_payload(_batch(samples="nope"))
    batch = _batch()
    batch["samples"][0]["sampled_at"] = "not-a-time"
    with pytest.raises(ValueError, match="sampled_at"):
        mp.expand_agent_payload(batch)


def test_nanosecond_sampled_at_accepted():
    """Go time.Time JSON 序列化是 RFC3339Nano（纳秒，如 ...34.762665865Z），
    Python 3.10 fromisoformat 不支持超过 6 位小数秒——必须截断接受。"""
    batch = _batch()
    batch["samples"][0]["sampled_at"] = "2026-10-24T04:36:34.762665865Z"
    result = mp.expand_agent_payload(batch)
    assert result["samples"][0]["sampled_at"] == "2026-10-24T04:36:34.762665865Z"

    # 纳秒截断到微秒后 PG timestamptz 精度一致（原始字符串原样保留，但插入 OK）
    batch["samples"][0]["sampled_at"] = "2026-10-24T04:36:34.999999999Z"
    result = mp.expand_agent_payload(batch)
    assert result["samples"][0]["sampled_at"].endswith(".999999999Z")


def test_rates_not_flattened():
    """rates 是差分速率（消费端自算），不落库。"""
    batch = _batch()
    batch["samples"][0]["host"]["rates"] = {"up_bytes_sec": {"ens5": 1.25}}
    result = mp.expand_agent_payload(batch)
    assert all("rates" not in s["metric_name"] and "up_bytes_sec" not in s["metric_name"] for s in result["samples"])
    assert math.isfinite(sum(s["metric_value"] for s in result["samples"]))
