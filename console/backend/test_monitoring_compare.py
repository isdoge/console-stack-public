# -*- coding: utf-8 -*-
"""monitoring_compare 的回归（B5-1）。

分层：
- 纯函数层（Beszel 投影 / 指标对齐 / 容差 / dead man 判定）：无 IO，直接断言；
- PG 层（agent 轨读取 / 事件落库）：真 PG16 底座（pg02_support 同款），
  复验 SELECT 只读、最新一条语义、指纹隔离与幂等 transition；
- 顶层 run_comparison：注入假快照 + 真 PG，验报告聚合（缺失率 / 资源开销）。
"""
import datetime
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import monitor_events as me  # noqa: E402
import monitoring_compare as mcmp  # noqa: E402
import monitoring_ingest as mi  # noqa: E402
import monitoring_schema as ms  # noqa: E402
from pg02_support import count_rows  # noqa: E402

pytest_plugins = ("pg02_support",)


NOW = datetime.datetime(2026, 10, 24, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(seconds):
    return _iso(NOW - datetime.timedelta(seconds=seconds))


# ---- 纯函数：Beszel 轨投影 --------------------------------------------------


def _beszel_snapshot():
    return {
        "ok": True,
        "systems": [
            {
                "machine": "nas", "cpu": 12.5, "memory_pct": 43.2,
                "memory_total_mb": 32768.0, "memory_used_mb": 14156.0,
                "disk_pct": 61.0, "disk_total_gb": 400.0, "disk_used_gb": 250.0,
                "net_sent_total": 1.2e11, "net_recv_total": 0.9e11,
                "net_sampled_at": _ago(60),
                # 失真字段：投影必须用 containers 实查，不读这个
                "container_count": 2,
            },
            {"machine": "kr", "cpu": 3.0},
        ],
        "containers": [
            {"machine": "nas", "name": "beszel-agent", "cpu": 2.1, "memory_mb": 128.0},
            {"machine": "nas", "name": "caddy", "cpu": 1.0, "memory_mb": 64.0},
        ],
    }


class TestBeszelTrack:
    def test_projects_metrics_and_real_container_count(self):
        track = mcmp.collect_beszel_track(_beszel_snapshot())
        nas = track["nas"]
        assert nas["metrics"]["cpu_pct"] == 12.5
        assert nas["metrics"]["container_count"] == 2.0  # 实查 2，不是 info.ct=2 的巧合
        assert nas["metrics"]["memory_total_mb"] == 32768.0
        assert set(nas["containers"]) == {"beszel-agent", "caddy"}

    def test_missing_fields_are_absent_not_zero(self):
        track = mcmp.collect_beszel_track(_beszel_snapshot())
        kr = track["kr"]["metrics"]
        assert kr["memory_total_mb"] is None  # 缺失显式为 None，不造 0（契约 §6）
        assert kr["container_count"] == 0.0  # 实查无行 = 真 0（合法 0 清单）


# ---- 纯函数：指标对齐（单位 / 口径） ----------------------------------------


class TestAlignment:
    def test_agent_memory_pct_uses_available_basis(self):
        metrics = {
            ms.METRIC_MEM_TOTAL_BYTES: 32 * 1024 ** 3,
            ms.METRIC_MEM_USED_BYTES: int(0.432 * 32 * 1024 ** 3),
        }
        report = mcmp.compare_machine(
            {"metrics": {"memory_pct": 43.2}}, {"metrics": metrics}, "nas"
        )
        assert report["metrics"]["memory_pct"]["status"] == "match"

    def test_agent_bytes_projected_to_mib_and_gib(self):
        agent = {
            "metrics": {
                ms.METRIC_MEM_TOTAL_BYTES: 32 * 1024 ** 3,
                ms.METRIC_MEM_USED_BYTES: int(13.8 * 1024 ** 3),
                ms.METRIC_DISK_TOTAL_BYTES: int(400.0 * 1024 ** 3),
                ms.METRIC_DISK_USED_BYTES: int(250.0 * 1024 ** 3),
            }
        }
        report = mcmp.compare_machine(
            {
                "metrics": {
                    "memory_total_mb": 32768.0, "memory_used_mb": 14131.2,
                    "disk_total_gb": 400.0, "disk_used_gb": 250.0,
                }
            },
            agent, "nas",
        )
        assert report["metrics"]["memory_total_mb"]["status"] == "match"
        assert report["metrics"]["memory_used_mb"]["status"] == "match"
        assert report["metrics"]["disk_total_gb"]["status"] == "match"
        assert report["metrics"]["disk_used_gb"]["status"] == "match"

    def test_beszel_memory_raw_basis_counts_as_divergent(self):
        # beszel 侧 memory_pct 是 available 口径；agent 若报 beszel 原口径 94.1，
        # 差异必须显式呈现，不被静默抹平。
        metrics = {
            ms.METRIC_MEM_TOTAL_BYTES: 32 * 1024 ** 3,
            ms.METRIC_MEM_USED_BYTES: int(0.941 * 32 * 1024 ** 3),
        }
        report = mcmp.compare_machine(
            {"metrics": {"memory_pct": 43.2}}, {"metrics": metrics}, "nas"
        )
        assert report["metrics"]["memory_pct"]["status"] == "divergent"


# ---- 纯函数：容差 -----------------------------------------------------------


class TestTolerance:
    def test_abs_tolerance_boundary(self):
        assert mcmp.within_tolerance(10.0, 50.0, 40.0, ("abs", 10.0))
        assert not mcmp.within_tolerance(10.01, 50.0, 39.99, ("abs", 10.0))

    def test_rel_tolerance_base_is_max_of_both_sides(self):
        # 基准取 max(|a|,|b|)：beszel≈0 时不给 5% 相对容差放水
        assert mcmp.within_tolerance(4.0, 0.0, 4.0, ("rel", 0.05)) is False
        assert mcmp.within_tolerance(4.0, 100.0, 104.0, ("rel", 0.05))

    def test_cpu_within_10pp_absorbs_instantaneous_vs_window_gap(self):
        report = mcmp.compare_machine(
            {"metrics": {"cpu_pct": 30.0}},
            {"metrics": {ms.METRIC_CPU_USED_PCT: 22.0}}, "nas",
        )
        assert report["metrics"]["cpu_pct"]["status"] == "match"

    def test_missing_sides_are_explicit(self):
        report = mcmp.compare_machine(
            {"metrics": {"cpu_pct": 30.0, "memory_pct": 40.0}}, {"metrics": {}}, "nas"
        )
        assert report["metrics"]["cpu_pct"]["status"] == "missing-agent"
        assert report["metrics"]["memory_pct"]["status"] == "missing-agent"
        empty = mcmp.compare_machine({"metrics": {}}, {"metrics": {}}, "nas")
        assert empty["metrics"]["cpu_pct"]["status"] == "missing-both"


# ---- 纯函数：容器比对 -------------------------------------------------------


class TestContainerCompare:
    def test_same_name_containers_compared_per_metric(self):
        beszel = {"metrics": {}, "containers": {
            "caddy": {"cpu": 1.0, "memory_mb": 64.0},
        }}
        agent = {"metrics": {}, "containers": {
            "caddy": {"cpu": 1.2, "memory_mb": 65.0},
        }}
        report = mcmp.compare_machine(beszel, agent, "nas")
        caddy = report["containers"]["caddy"]
        assert caddy["status"] == "both"
        assert caddy["cpu"]["status"] == "match"
        assert caddy["memory_mb"]["status"] == "match"

    def test_container_set_difference_is_explicit(self):
        beszel = {"metrics": {}, "containers": {"caddy": {"cpu": 1.0, "memory_mb": 64.0}}}
        agent = {"metrics": {}, "containers": {"redis": {"cpu": 0.0, "memory_mb": 10.0}}}
        report = mcmp.compare_machine(beszel, agent, "nas")
        assert report["containers"]["caddy"]["status"] == "missing-agent"
        assert report["containers"]["redis"]["status"] == "missing-beszel"

    def test_missing_container_metric_rows_are_side_attributed(self):
        # 单侧有行但字段缺失（如 agent CPU 窗无效未采）：missing 要能归属到侧。
        beszel = {"metrics": {}, "containers": {
            "caddy": {"cpu": 1.0, "memory_mb": 64.0},
        }}
        agent = {"metrics": {}, "containers": {
            "caddy": {"cpu": None, "memory_mb": 64.0},
        }}
        report = mcmp.compare_machine(beszel, agent, "nas")
        caddy = report["containers"]["caddy"]
        assert caddy["status"] == "both"
        assert caddy["cpu"] == {"beszel": 1.0, "agent": None, "status": "missing"}
        assert caddy["memory_mb"]["status"] == "match"


# ---- 纯函数：summary 容器分项（B6-1：主机 10/10 不得掩盖容器 3 红） --------


class TestSummaryContainers:
    def _tracks(self, containers_beszel, containers_agent):
        # 主机级指标给同值（match），隔离出容器分项的行为。agent 侧用
        # payload 原生 metric 名（与 read_agent_track 输出同构）。
        _MIB, _GIB = 1024.0**2, 1024.0**3
        beszel_metrics = {
            "cpu_pct": 10.0, "memory_pct": 40.0,
            "memory_total_mb": 32768.0, "memory_used_mb": 13107.2,
            "disk_pct": 60.0, "disk_total_gb": 400.0, "disk_used_gb": 250.0,
            "net_sent_total": 1.0e11, "net_recv_total": 0.8e11,
            "container_count": float(len(containers_beszel)),
        }
        agent_metrics = {
            ms.METRIC_CPU_USED_PCT: 10.0,
            ms.METRIC_MEM_TOTAL_BYTES: 32768.0 * _MIB,
            ms.METRIC_MEM_USED_BYTES: 13107.2 * _MIB,
            ms.METRIC_DISK_USED_PCT: 60.0,
            ms.METRIC_DISK_TOTAL_BYTES: 400.0 * _GIB,
            ms.METRIC_DISK_USED_BYTES: 250.0 * _GIB,
            "container_count": float(len(containers_agent)),
        }
        # 计费网卡计数器（read_agent_track 会把它们放 nics 结构，比对经
        # _pick_agent_nic 挑选；直接给单网卡同值即可 match）。
        agent_nics = None
        if containers_beszel or containers_agent:
            agent_nics = {"ens5": {"sent": 1.0e11, "recv": 0.8e11}}
        beszel = {"nas": {"metrics": beszel_metrics, "containers": containers_beszel,
                          "nics": agent_nics, "sampled_at": _ago(60)}}
        agent = {"nas": {"metrics": agent_metrics, "containers": containers_agent,
                         "nics": agent_nics, "sampled_at": _ago(60)}}
        return mcmp.compare_tracks(beszel, agent, ["nas"])

    def test_divergent_containers_counted_in_summary_not_hidden(self):
        # 复现试点 §10.2：主机 10/10 全 match，但逐容器 3 项超容差——
        # summary 必须同时给出 containers 分项，不能只用主机级数字过关。
        result = self._tracks(
            {
                "hindsight-pg": {"cpu": 4.15, "memory_mb": 200.0},
                "magpie": {"cpu": 8.79, "memory_mb": 120.0},
                "beszel-agent": {"cpu": 1.0, "memory_mb": 15.48},
                "caddy": {"cpu": 1.0, "memory_mb": 64.0},
            },
            {
                "hindsight-pg": {"cpu": 105.0, "memory_mb": 200.0},
                "magpie": {"cpu": 52.4, "memory_mb": 120.0},
                "beszel-agent": {"cpu": 1.0, "memory_mb": 14.5},
                "caddy": {"cpu": 1.0, "memory_mb": 64.0},
            },
        )
        host_metrics = result["machines"]["nas"]["metrics"]
        assert all(m["status"] == "match" for m in host_metrics.values())
        cs = result["summary"]["containers"]
        # 8 项可比（4 容器 × cpu/memory），hindsight-pg/magpie CPU 与
        # beszel-agent 内存超容差 → 3 divergent；不放宽容差的前提下如实计数。
        assert cs["compared"] == 8
        assert cs["divergent"] == 3
        assert cs["match"] == 5
        assert cs["missing"] == {"beszel": 0, "agent": 0}
        # 主机级 metrics 数字不变（兼容既有报告语义），容器红项只在分项里。
        assert result["summary"]["divergent"] == 0

    def test_missing_containers_counted_per_metric_per_side(self):
        result = self._tracks(
            {"caddy": {"cpu": 1.0, "memory_mb": 64.0},
             "redis": {"cpu": 1.0, "memory_mb": 32.0}},
            {"caddy": {"cpu": 1.0, "memory_mb": 64.0}},
        )
        cs = result["summary"]["containers"]
        assert cs["compared"] == 2  # caddy 两项
        assert cs["missing"] == {"beszel": 0, "agent": 2}  # redis 两项在 beszel 侧缺
        assert cs["missing_rate"]["agent"] == 0.5

    def test_whole_missing_container_counts_every_field(self):
        # R1：整容器单侧缺席不得静默跳过——每个 schema 字段各计一条
        # missing 并归属缺席侧，缺多少是可见的。
        result = self._tracks(
            {"caddy": {"cpu": 1.0, "memory_mb": 64.0},
             "redis": {"cpu": 1.0, "memory_mb": 32.0}},
            {"caddy": {"cpu": 1.0, "memory_mb": 64.0}},
        )
        machines = result["machines"]["nas"]["containers"]
        assert machines["redis"]["status"] == "missing-agent"
        assert machines["redis"]["cpu"]["status"] == "missing"
        assert machines["redis"]["memory_mb"]["status"] == "missing"

    def test_container_fields_schema_is_cpu_and_memory_mb(self):
        # R1 回归锚：旧验收遍历 ("cpu","memory") 导致内存漏检——消费方
        # 必须引用 CONTAINER_FIELDS，字段名错了测试直接红。
        assert mcmp.CONTAINER_FIELDS == ("cpu", "memory_mb")
        report = mcmp.compare_machine(
            {"metrics": {}, "containers": {"caddy": {"cpu": 1.0, "memory_mb": 64.0}}},
            {"metrics": {}, "containers": {"caddy": {"cpu": 1.0, "memory_mb": 64.0}}},
            "nas",
        )
        assert set(report["containers"]["caddy"]) == {"status", *mcmp.CONTAINER_FIELDS}
        # "memory"（不带 _mb）不是 schema 字段，读它只会得到 None——这条
        # 断言锁死正确字段名，防止再写出 cb.get("memory") 式漏检遍历。
        assert "memory" not in report["containers"]["caddy"]

    def test_summary_contains_per_machine_sampling_times(self):
        # R1：两轨采样时间必须在 summary 里可见——「未同窗对齐」不再只
        # 藏在代码路径里，差异归因时能直接读出两侧各自的时间。
        result = self._tracks(
            {"caddy": {"cpu": 1.0, "memory_mb": 64.0}},
            {"caddy": {"cpu": 1.2, "memory_mb": 65.0}},
        )
        sampling = result["summary"]["sampling"]
        assert sampling["nas"]["beszel_sampled_at"] == _ago(60)
        assert sampling["nas"]["agent_sampled_at"] == _ago(60)
        # 单侧缺席的机器：对应时间为 None，不造假时间。
        partial = mcmp.compare_tracks(
            {"nas": {"metrics": {}, "containers": {}, "sampled_at": _ago(30)}},
            {}, ["nas"],
        )
        assert partial["summary"]["sampling"]["nas"] == {
            "beszel_sampled_at": _ago(30), "agent_sampled_at": None,
            "agent_window_from": None,
        }
        assert partial["machines"]["nas"]["sampling"] == (
            partial["summary"]["sampling"]["nas"]
        )

    def test_agent_track_window_from_makes_effective_window_visible(self, pg_client,
                                                                    seeded_agent_track):
        # R1：agent 轨除最新样本时间外还带窗口内最早样本时间——summary 的
        # 时间块能看出「每指标取最新」实际引用的是多宽的窗。
        track = mcmp.read_agent_track(["nas"], now=NOW)
        assert track["nas"]["window_from"] == _ago(1200)
        assert track["nas"]["sampled_at"] == _ago(300)
        report = mcmp.run_comparison(
            machine_ids=["nas"], snapshot=_beszel_snapshot(), now=NOW,
        )
        sampling = report["summary"]["sampling"]["nas"]
        assert sampling["agent_sampled_at"] == _ago(300)
        assert sampling["agent_window_from"] == _ago(1200)
        assert sampling["beszel_sampled_at"]  # system_stats 时间挂上了

    def test_both_sides_empty_containers_do_not_dilute(self):
        # 两侧容器集合都空（无 docker 能力等）：无条目可算，分项归零不造假。
        result = self._tracks({}, {})
        cs = result["summary"]["containers"]
        assert cs == {
            "compared": 0, "match": 0, "divergent": 0,
            "missing": {"beszel": 0, "agent": 0},
            "missing_rate": {"beszel": 0.0, "agent": 0.0},
        }

    def test_machine_without_container_section_is_neutral(self):
        # 某机完全缺席（如 agent 未 enrol）：容器分项不崩、不计 missing——
        # 整机缺席已由主机级 missing 率呈现，容器分项只对真实条目负责。
        result = mcmp.compare_tracks({"nas": {"metrics": {}, "containers": {}}},
                                     {}, ["nas", "txy"])
        cs = result["summary"]["containers"]
        assert cs["compared"] == 0
        assert cs["missing"] == {"beszel": 0, "agent": 0}


# ---- 同窗对齐（B6-3：A 类窗口错位差异的对账口径）----------------------------


class TestAlignWindow:
    """align 模式三段验证：选样（最近非最新）、缺席（missing 归 agent）、
    legacy 兼容（None 不改变任何输出形状）。"""

    def _seeded_tracks(self, pg_client, samples, machine="nas"):
        """enrol 后写入给定样本行，返回 (beszel_track, machine)。"""
        mi.enrol_machine(machine, "对齐用")
        result = mi.insert_samples(machine, samples, "0.1.0")
        assert result["inserted"] == len(samples)

    def test_align_window_pairs_closest_sample(self, pg_client):
        # 同一 metric 三条样本（80@-1200、30@-60、12.5@-300），beszel_ts=-180：
        # align 必须取 |-180-(-300)|=120 的 12.5（最近），不是 -60 的 30（最新）。
        beszel_ts = NOW - datetime.timedelta(seconds=180)
        self._seeded_tracks(pg_client, [
            {"metric_name": ms.METRIC_CPU_USED_PCT, "metric_value": 80.0,
             "sampled_at": _ago(1200)},
            {"metric_name": ms.METRIC_CPU_USED_PCT, "metric_value": 12.5,
             "sampled_at": _ago(300)},
            {"metric_name": ms.METRIC_CPU_USED_PCT, "metric_value": 30.0,
             "sampled_at": _ago(60)},
        ])
        track = mcmp.read_agent_track(
            ["nas"], now=NOW, align_to={"nas": beszel_ts},
            align_window_seconds=300,
        )
        assert track["nas"]["metrics"][ms.METRIC_CPU_USED_PCT] == 12.5
        assert track["nas"]["metric_sampled_at"][ms.METRIC_CPU_USED_PCT] == _ago(300)
        # 机器级 sampled_at 仍是全窗最新（-60），不被对齐收窄——dead man 判定
        # 不受对齐模式影响。
        assert track["nas"]["sampled_at"] == _ago(60)

    def test_align_window_missing_when_no_sample_in_window(self, pg_client):
        # cpu 完全无样本（窗内窗外都没有）：该指标缺席 → missing 计数 +1 且
        # 归属 agent 侧（比对无 agent 值 → missing-agent）。B6-3 第二版起
        # missing 的语义收窄为「回看窗内完全无样本」——窗外有样本时不再
        # 缺席，而是取最近样本标 out_of_window（见下例）。
        self._seeded_tracks(pg_client, [
            # 不写任何 cpu 样本：agent 轨对 cpu_used_pct 完全无数据
            {"metric_name": ms.METRIC_MEM_TOTAL_BYTES,
             "metric_value": 32 * 1024 ** 3, "sampled_at": _ago(300)},
        ])
        report = mcmp.run_comparison(
            machine_ids=["nas"], snapshot=_beszel_snapshot(), now=NOW,
            align_window_seconds=300,
        )
        cpu = report["machines"]["nas"]["metrics"]["cpu_pct"]
        assert cpu["status"] == "missing-agent"
        assert cpu["agent"] is None
        assert report["summary"]["missing"]["agent"] >= 1

    def test_align_window_out_of_window_falls_back_to_nearest(self, pg_client):
        # B6-3 第二版：cpu 样本距 beszel_ts 400s > align 窗 300s，但回看窗内
        # 仍有样本 → 不缺席，取最近样本比对，并落 sampling.out_of_window。
        # 这是六机相位互异 + 落库时延 60–70s 现实下的关键放行机制（见
        # 2026-10-25-b63-round1-failed.md §10 的几何死结证明）。
        self._seeded_tracks(pg_client, [
            {"metric_name": ms.METRIC_CPU_USED_PCT, "metric_value": 12.5,
             "sampled_at": _ago(460)},
        ])
        report = mcmp.run_comparison(
            machine_ids=["nas"], snapshot=_beszel_snapshot(), now=NOW,
            align_window_seconds=300,
        )
        cpu = report["machines"]["nas"]["metrics"]["cpu_pct"]
        # beszel 快照 cpu=5.0，agent 12.5 → delta=7.5 ≤ abs 10.0 容差 → match
        assert cpu["status"] == "match"
        assert cpu["agent"] == 12.5
        # missing 不计（指标有值可比）
        # out_of_window 列表里能看到 cpu_used_pct + 残差 ≈ 400s
        oow = report["machines"]["nas"]["sampling"]["out_of_window"]
        cpu_oow = [x for x in oow if x["metric"] == ms.METRIC_CPU_USED_PCT]
        assert len(cpu_oow) == 1
        assert 395 <= cpu_oow[0]["abs_diff_seconds"] <= 405

    def test_out_of_window_excludes_metrics_paired_in_window(self, pg_client):
        # 同一指标窗内、窗外都有样本时，out_of_window **不得**列出它——比对
        # 用的是窗内那条，窗外样本对报告无意义。曾回归：轮 2 实测 aly/cpu
        # 窗内配上（残差 69.2s）却仍被报 368.3s，原因是 out_of_window 列表
        # 无条件写入，变成了「窗外是否存在样本」而非「本次比对用了窗外样本」。
        self._seeded_tracks(pg_client, [
            # 窗内样本：距 NOW 60s（beszel 快照 -60s → 残差 0）
            {"metric_name": ms.METRIC_CPU_USED_PCT, "metric_value": 12.5,
             "sampled_at": _ago(60)},
            # 窗外样本：距目标 ~360s（> align 窗 300s）
            {"metric_name": ms.METRIC_CPU_USED_PCT, "metric_value": 99.0,
             "sampled_at": _ago(420)},
        ])
        report = mcmp.run_comparison(
            machine_ids=["nas"], snapshot=_beszel_snapshot(), now=NOW,
            align_window_seconds=300,
        )
        cpu = report["machines"]["nas"]["metrics"]["cpu_pct"]
        # 窗内样本胜出：12.5（不是窗外那条 99.0）
        assert cpu["agent"] == 12.5
        # out_of_window 不含 cpu（它窗内配上了）
        oow = report["machines"]["nas"]["sampling"]["out_of_window"]
        assert [x for x in oow if x["metric"] == ms.METRIC_CPU_USED_PCT] == []

    def test_align_window_none_keeps_legacy_behavior(self, pg_client):
        # 不传参数：agent 轨仍是「窗口内取最新」，机器无 metric_sampled_at
        # 键，sampling 无 aligned_pairs——与既有行为逐位一致（防回归锚）。
        self._seeded_tracks(pg_client, [
            {"metric_name": ms.METRIC_CPU_USED_PCT, "metric_value": 12.5,
             "sampled_at": _ago(300)},
            {"metric_name": ms.METRIC_CPU_USED_PCT, "metric_value": 30.0,
             "sampled_at": _ago(60)},
        ])
        track = mcmp.read_agent_track(["nas"], now=NOW)
        assert track["nas"]["metrics"][ms.METRIC_CPU_USED_PCT] == 30.0
        assert "metric_sampled_at" not in track["nas"]
        report = mcmp.run_comparison(
            machine_ids=["nas"], snapshot=_beszel_snapshot(), now=NOW,
        )
        sampling = report["machines"]["nas"]["sampling"]
        assert set(sampling) == {
            "beszel_sampled_at", "agent_sampled_at", "agent_window_from",
        }

    def test_align_window_exposes_aligned_pairs_in_sampling(self, pg_client):
        # sampling 带 aligned_pairs：每条含 beszel_ts/agent_ts/abs_diff_seconds，
        # diff ≤ align_window_seconds——验收方直接读出对齐质量。beszel_ts 用
        # 自定义快照（-180），同时验证 run_comparison 从快照提取对齐目标的链路。
        self._seeded_tracks(pg_client, [
            {"metric_name": ms.METRIC_CPU_USED_PCT, "metric_value": 12.5,
             "sampled_at": _ago(300)},
            {"metric_name": ms.METRIC_MEM_TOTAL_BYTES,
             "metric_value": 32 * 1024 ** 3, "sampled_at": _ago(300)},
        ])
        snapshot = _beszel_snapshot()
        snapshot["systems"][0]["net_sampled_at"] = _ago(180)
        report = mcmp.run_comparison(
            machine_ids=["nas"], snapshot=snapshot, now=NOW,
            align_window_seconds=300,
        )
        sampling = report["machines"]["nas"]["sampling"]
        assert sampling["align_window_seconds"] == 300
        pairs = sampling["aligned_pairs"]
        by_metric = {p["metric"]: p for p in pairs}
        assert by_metric[ms.METRIC_CPU_USED_PCT] == {
            "metric": ms.METRIC_CPU_USED_PCT,
            "beszel_ts": _ago(180), "agent_ts": _ago(300),
            "abs_diff_seconds": 120.0,
        }
        assert by_metric[ms.METRIC_MEM_TOTAL_BYTES]["abs_diff_seconds"] == 120.0
        assert all(
            p["abs_diff_seconds"] <= 300 for p in pairs
        )

    def test_run_comparison_top_level_align_window_self_evidence(self, pg_client):
        # 顶层报告必须能读出 align_window_seconds（B6-3 验收 §3.1 判据 3 /
        # H5：三轮报告的窗口值可读出，否则事后无法区分同窗对齐与又一轮 R1）。
        # None 时顶层字段也应存在且为 None，让验收方能区分「没跑对齐」
        # 与「跑了但字段丢失」。
        self._seeded_tracks(pg_client, [
            {"metric_name": ms.METRIC_CPU_USED_PCT, "metric_value": 12.5,
             "sampled_at": _ago(60)},
        ])
        snapshot = _beszel_snapshot()
        aligned = mcmp.run_comparison(
            machine_ids=["nas"], snapshot=snapshot, now=NOW,
            align_window_seconds=180,
        )
        assert aligned["align_window_seconds"] == 180
        legacy = mcmp.run_comparison(
            machine_ids=["nas"], snapshot=snapshot, now=NOW,
        )
        assert legacy["align_window_seconds"] is None


# ---- 纯函数：dead man's switch 判定 ----------------------------------------


class TestDeadMansSwitch:
    def test_form_one_stale_when_samples_stop(self):
        track = {"nas": {"sampled_at": _ago(mcmp.DEAD_MAN_THRESHOLD_SEC + 60)}}
        violations = mcmp.dead_mans_switch(track, True, ["nas"], now=NOW)
        assert len(violations) == 1
        assert violations[0]["form"] == "stale"
        assert violations[0]["silence_sec"] == mcmp.DEAD_MAN_THRESHOLD_SEC + 60

    def test_fresh_machine_no_violation(self):
        track = {"nas": {"sampled_at": _ago(300)}}
        assert mcmp.dead_mans_switch(track, True, ["nas"], now=NOW) == []

    def test_threshold_is_12_minutes_not_17(self):
        # B1-2 §2.1 钉死：10+2=12。720s 恰在阈值上不算，721 才算。
        track = {"nas": {"sampled_at": _ago(720)}}
        assert mcmp.dead_mans_switch(track, True, ["nas"], now=NOW) == []
        track = {"nas": {"sampled_at": _ago(721)}}
        assert len(mcmp.dead_mans_switch(track, True, ["nas"], now=NOW)) == 1
        assert mcmp.DEAD_MAN_THRESHOLD_SEC == 720

    def test_form_two_source_failed_per_machine(self):
        track = {"nas": {"sampled_at": _ago(300)}, "txy": {"sampled_at": _ago(300)}}
        violations = mcmp.dead_mans_switch(track, False, ["nas", "txy"], now=NOW)
        assert {v["machine"] for v in violations} == {"nas", "txy"}
        assert all(v["form"] == "source-failed" for v in violations)

    def test_never_enrolled_machine_is_not_stale(self):
        # 从未上报 ≠ 中断（能力问题与中断不同词，契约 §4.4/§6）
        assert mcmp.dead_mans_switch({}, True, ["ghost"], now=NOW) == []


# ---- PG 层：agent 轨读取 ----------------------------------------------------


@pytest.fixture()
def seeded_agent_track(pg_client):
    """enrol nas 并写两轮样本：cpu 有新旧两条（取新），网卡有计费口与排除口。"""
    mi.enrol_machine("nas", "NAS 主机")
    old, new = _ago(1200), _ago(300)
    samples = [
        {"metric_name": ms.METRIC_CPU_USED_PCT, "metric_value": 80.0, "sampled_at": old},
        {"metric_name": ms.METRIC_CPU_USED_PCT, "metric_value": 12.5, "sampled_at": new},
        {"metric_name": ms.METRIC_MEM_TOTAL_BYTES, "metric_value": 32 * 1024 ** 3,
         "sampled_at": new},
        {"metric_name": ms.METRIC_MEM_USED_BYTES,
         "metric_value": int(0.432 * 32 * 1024 ** 3), "sampled_at": new},
        {"metric_name": ms.METRIC_DISK_USED_PCT, "metric_value": 61.0, "sampled_at": new},
        {"metric_name": ms.METRIC_DISK_USED_BYTES, "metric_value": 250.0 * 1024 ** 3,
         "sampled_at": new},
        {"metric_name": ms.METRIC_DISK_TOTAL_BYTES, "metric_value": 400.0 * 1024 ** 3,
         "sampled_at": new},
        {"metric_name": mcmp.nic_metric_name("ens5", "sent"),
         "metric_value": 1.2e11, "sampled_at": new},
        {"metric_name": mcmp.nic_metric_name("ens5", "recv"),
         "metric_value": 0.9e11, "sampled_at": new},
        {"metric_name": mcmp.nic_metric_name("tailscale0", "sent"),
         "metric_value": 9.9e12, "sampled_at": new},
        {"metric_name": mcmp.container_metric_name("caddy", "cpu_pct"),
         "metric_value": 1.0, "sampled_at": new},
        {"metric_name": mcmp.container_metric_name("caddy", "memory_used_bytes"),
         "metric_value": 64.0 * 1024 ** 2, "sampled_at": new},
        {"metric_name": mcmp.container_metric_name("beszel-agent", "cpu_pct"),
         "metric_value": 2.1, "sampled_at": new},
        {"metric_name": mcmp.container_metric_name("beszel-agent", "memory_used_bytes"),
         "metric_value": 128.0 * 1024 ** 2, "sampled_at": new},
    ]
    result = mi.insert_samples("nas", samples, "0.1.0")
    assert result["inserted"] == len(samples)
    return {"now": NOW, "sampled_at": new}


class TestAgentTrackRead:
    def test_latest_row_per_metric_and_structured_parse(self, pg_client, seeded_agent_track):
        track = mcmp.read_agent_track(["nas"], now=NOW)
        nas = track["nas"]
        assert nas["metrics"][ms.METRIC_CPU_USED_PCT] == 12.5  # 旧 80 被新 12.5 覆盖
        assert nas["sampled_at"] == seeded_agent_track["sampled_at"]
        assert nas["nics"]["ens5"] == {"sent": 1.2e11, "recv": 0.9e11}
        assert nas["containers"]["caddy"] == {"cpu": 1.0, "memory_mb": 64.0}
        assert nas["metrics"]["container_count"] == 2.0

    def test_read_is_readonly(self, pg_client, seeded_agent_track):
        before = count_rows(pg_client, "monitoring_samples")
        mcmp.read_agent_track(["nas"], now=NOW)
        assert count_rows(pg_client, "monitoring_samples") == before

    def test_window_excludes_old_rows(self, pg_client, seeded_agent_track):
        track = mcmp.read_agent_track(["nas"], window_sec=600, now=NOW)
        assert track["nas"]["metrics"][ms.METRIC_CPU_USED_PCT] == 12.5
        track = mcmp.read_agent_track(["nas"], window_sec=120, now=NOW)
        assert track.get("nas") is None

    def test_pick_agent_nic_prefers_billable_over_excluded(self, pg_client,
                                                           seeded_agent_track):
        track = mcmp.read_agent_track(["nas"], now=NOW)
        report = mcmp.compare_machine(
            {"metrics": {"net_sent_total": 1.2e11, "net_recv_total": 0.9e11}},
            track["nas"], "nas",
        )
        sent = report["metrics"]["net_sent_total"]
        assert sent["status"] == "match"
        assert sent["agent_nic"] == "ens5"  # tailscale0 被排除表挡住


# ---- PG 层：事件落库 --------------------------------------------------------


class TestDeadManEvents:
    def test_open_then_resolve_is_idempotent_transition(self, pg_client):
        states = {
            "nas": {"machine": "nas", "form": "stale", "last_sampled_at": _ago(800),
                    "silence_sec": 800},
        }
        first = mcmp.emit_dead_mans_switch_events(states, ts=_iso(NOW))
        assert first["nas"] == "opened"
        second = mcmp.emit_dead_mans_switch_events(states, ts=_iso(NOW))
        assert second["nas"] == "updated"  # 幂等：不新开
        third = mcmp.emit_dead_mans_switch_events({"nas": None}, ts=_iso(NOW))
        assert third["nas"] == "resolved"
        events = me.active_events()
        assert events == []

    def test_fingerprint_isolated_from_probe_namespace(self, pg_client):
        # 同机探活先开一条活动事件；监控事件不得与它互相关闭。
        me.observe_machine("nas", "down", ts=_iso(NOW))
        mcmp.emit_dead_mans_switch_events(
            {"nas": {"machine": "nas", "form": "stale",
                     "last_sampled_at": _ago(800), "silence_sec": 800}},
            ts=_iso(NOW),
        )
        active = me.active_events()
        assert len(active) == 2
        fingerprints = {e["fingerprint"] for e in active}
        assert "machine:nas" in fingerprints
        assert "machine:monitoring:nas" in fingerprints
        # 探活恢复只关探活事件
        me.observe_machine("nas", "up", ts=_iso(NOW))
        active = me.active_events()
        assert {e["fingerprint"] for e in active} == {"machine:monitoring:nas"}


# ---- 顶层：run_comparison ---------------------------------------------------


class TestRunComparison:
    def test_report_aggregates_missing_rate_and_costs(self, pg_client,
                                                      seeded_agent_track):
        report = mcmp.run_comparison(
            machine_ids=["nas"], snapshot=_beszel_snapshot(), now=NOW,
        )
        summary = report["summary"]
        assert summary["machines"] == 1
        assert summary["metrics_compared"] == len(mcmp.METRIC_ALIGNMENT)
        # 双轨数据齐全且一致 → 0 缺失、0 分歧
        assert summary["missing"]["agent"] == 0
        assert summary["missing"]["beszel"] == 0
        assert summary["divergent"] == 0
        cpu = report["machines"]["nas"]["metrics"]["cpu_pct"]
        assert cpu == {
            "dimension": "cpu", "unit": "%",
            "tolerance": {"kind": "abs", "limit": 10.0},
            "beszel": 12.5, "agent": 12.5, "delta": 0.0, "status": "match",
        }
        assert report["dead_man_switch"]["threshold_sec"] == 720
        assert report["dead_man_switch"]["violations"] == []
        costs = report["resource_cost"]
        assert {"pg_query_sec", "compare_sec", "total_sec"} <= set(costs)

    def test_source_failed_snapshot_reports_per_machine(self, pg_client,
                                                        seeded_agent_track):
        snapshot = _beszel_snapshot()
        snapshot["ok"] = False
        snapshot["error"] = "hub unreachable"
        report = mcmp.run_comparison(
            machine_ids=["nas"], snapshot=snapshot, now=NOW,
        )
        assert report["beszel"]["ok"] is False
        violations = report["dead_man_switch"]["violations"]
        assert {v["form"] for v in violations} == {"source-failed"}

    def test_missing_agent_machine_counts_in_missing_rate(self, pg_client,
                                                          seeded_agent_track):
        report = mcmp.run_comparison(
            machine_ids=["nas", "txy"], snapshot=_beszel_snapshot(), now=NOW,
        )
        summary = report["summary"]
        assert summary["machines"] == 2
        # txy 双轨全缺：beszel 侧 systems 无该机、agent 轨无样本，各计满一机
        per_machine = len(mcmp.METRIC_ALIGNMENT)
        assert summary["missing"]["agent"] == per_machine
        assert summary["missing"]["beszel"] == per_machine
        assert summary["missing_rate"]["agent"] > 0
