# -*- coding: utf-8 -*-
"""node_traffic 月流量账本回归（E1）。

重点是三类「坑」：网卡挑错（把 tailscale0 算进配额）、计数器归零算成负数、
月份翻页没清零。全部离线，不碰 Beszel。
"""
import datetime
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import db  # noqa: E402
import machine_config as mc  # noqa: E402
import node_traffic as nt  # noqa: E402

GIB = 1024 ** 3


@pytest.fixture()
def dbp(tmp_path):
    path = str(tmp_path / "console.db")
    db.run_migrations(path)
    return path


@pytest.fixture(autouse=True)
def _clean_registry():
    mc.invalidate()
    yield
    mc.invalidate()


# 实测形状（2026-08-29 kr）：{nic: [上行速率, 下行速率, 累计发送, 累计接收]}
KR_NI = {
    "ens5": [516, 327, 714717441, 5526032382],
    "tailscale0": [12, 7, 131228, 83247],
}


class TestNicSelection:
    def test_excludes_vpn_loopback_and_virtual(self):
        assert nt.is_billable_nic("ens5")
        assert nt.is_billable_nic("eth0")
        for nic in ("lo", "tailscale0", "docker0", "veth1a2b", "br-abc", "wg0",
                    "tun0", "utun3", "Meta", "enp1s0-ovs"):
            assert not nt.is_billable_nic(nic), nic

    def test_picks_public_nic_not_tailscale(self):
        assert nt.pick_nic(KR_NI) == ("ens5", 714717441, 5526032382)

    def test_preferred_nic_wins(self):
        ni = {"ens5": [0, 0, 10, 20], "eth1": [0, 0, 999, 999]}
        assert nt.pick_nic(ni, "ens5")[0] == "ens5"

    def test_preferred_missing_falls_back_to_auto(self):
        """云厂商重建实例改了网卡名时，账本不能静默断流。"""
        assert nt.pick_nic(KR_NI, "eth0")[0] == "ens5"

    def test_auto_pick_takes_largest_billable(self):
        ni = {"eth0": [0, 0, 1, 1], "eth1": [0, 0, 100, 100]}
        assert nt.pick_nic(ni)[0] == "eth1"

    @pytest.mark.parametrize("bad", [None, {}, {"lo": [1, 2, 3, 4]}, {"ens5": [1, 2]}, "x"])
    def test_no_usable_nic_returns_none(self, bad):
        assert nt.pick_nic(bad) is None

    def test_rates_come_from_the_billable_nic_only(self):
        assert nt.nic_rates(KR_NI, "ens5") == (516, 327)
        assert nt.nic_rates(KR_NI, "nope") == (None, None)


class TestRecordSample:
    def test_first_sample_only_sets_baseline(self, dbp):
        row = nt.record_sample("kr", "ens5", 1000, 2000, db_path=dbp)
        assert (row["sent_bytes"], row["recv_bytes"]) == (0, 0)
        assert (row["last_sent_total"], row["last_recv_total"]) == (1000, 2000)

    def test_second_sample_accumulates_delta(self, dbp):
        nt.record_sample("kr", "ens5", 1000, 2000, db_path=dbp)
        row = nt.record_sample("kr", "ens5", 1500, 2200, db_path=dbp)
        assert (row["sent_bytes"], row["recv_bytes"]) == (500, 200)

    def test_counter_reset_counts_from_zero_not_negative(self, dbp):
        """agent 重装 / 机器重启会让计数器归零，绝不能算出负增量。"""
        nt.record_sample("kr", "ens5", 1000, 2000, db_path=dbp)
        nt.record_sample("kr", "ens5", 1500, 2200, db_path=dbp)
        row = nt.record_sample("kr", "ens5", 30, 40, db_path=dbp)
        assert (row["sent_bytes"], row["recv_bytes"]) == (530, 240)
        assert row["resets"] == 1
        # 新基线跟上重启后的计数器
        assert (row["last_sent_total"], row["last_recv_total"]) == (30, 40)

    def test_month_rollover_resets_counters(self, dbp):
        import datetime

        aug = datetime.datetime(2026, 8, 31, 23, 0, tzinfo=datetime.timezone.utc)
        sep = datetime.datetime(2026, 9, 1, 1, 0, tzinfo=datetime.timezone.utc)
        nt.record_sample("kr", "ens5", 1000, 2000, db_path=dbp, now=aug)
        nt.record_sample("kr", "ens5", 5000, 6000, db_path=dbp, now=aug)
        row = nt.record_sample("kr", "ens5", 9000, 9000, db_path=dbp, now=sep)
        assert row["month"] == "2026-09"
        assert (row["sent_bytes"], row["recv_bytes"]) == (0, 0)
        assert (row["last_sent_total"], row["last_recv_total"]) == (9000, 9000)

    def test_nic_change_rebases_baseline_but_keeps_month_total(self, dbp):
        """换网卡时新旧计数器不可比，但本月已累计的量不能丢。"""
        nt.record_sample("kr", "ens5", 0, 0, db_path=dbp)
        nt.record_sample("kr", "ens5", 10_000, 20_000, db_path=dbp)
        row = nt.record_sample("kr", "eth0", 5, 5, db_path=dbp)
        assert (row["sent_bytes"], row["recv_bytes"]) == (10_000, 20_000)
        assert (row["last_sent_total"], row["last_recv_total"]) == (5, 5)
        assert row["nic"] == "eth0"
        # 换卡后继续正常累加
        row = nt.record_sample("kr", "eth0", 105, 205, db_path=dbp)
        assert (row["sent_bytes"], row["recv_bytes"]) == (10_100, 20_200)

    def test_blank_machine_is_ignored(self, dbp):
        assert nt.record_sample("", "ens5", 1, 1, db_path=dbp) is None


class TestRecordFromSystems:
    def test_late_response_uses_upstream_time_and_does_not_reset_counters(self, dbp):
        def sample(stamp, count):
            return [{"machine": "kr", "net_nic": "ens5", "net_sent_total": count,
                     "net_recv_total": count, "net_sampled_at": stamp}]

        nt.record_from_systems(sample("2026-09-12T10:00:00Z", 100), db_path=dbp)
        newest = nt.record_from_systems(sample("2026-09-12T10:02:00Z", 300), db_path=dbp)[0]
        late = nt.record_from_systems(sample("2026-09-12T10:01:00Z", 200), db_path=dbp)[0]
        duplicate = nt.record_from_systems(sample("2026-09-12T10:02:00Z", 999), db_path=dbp)[0]
        assert late == duplicate == newest
        assert newest["sent_bytes"] == 200
        assert newest["updated_at"] == "2026-09-12T10:02:00Z"
        assert newest["resets"] == 0

    @pytest.mark.parametrize("stamp", [None, "", "bad-time", "2026-09-12T10:00:00"])
    def test_missing_or_invalid_upstream_time_is_not_invented(self, dbp, stamp):
        systems = [{"machine": "kr", "net_nic": "ens5", "net_sent_total": 1,
                    "net_recv_total": 2, "net_sampled_at": stamp}]
        assert nt.record_from_systems(systems, db_path=dbp) == []

    def test_records_all_machines_not_just_quota(self, dbp):
        systems = [
            {"machine": "kr", "net_nic": "ens5", "net_sent_total": 1, "net_recv_total": 2, "net_sampled_at": "2026-09-10T10:00:00Z"},
            {"machine": "nas", "net_nic": "enp1s0", "net_sent_total": 9, "net_recv_total": 9, "net_sampled_at": "2026-09-10T10:00:00Z"},
        ]
        rows = nt.record_from_systems(systems, db_path=dbp)
        # B0：放开配额过滤，所有机器都记本月累计（server 卡片也要「本月累计」格）
        assert [r["machine"] for r in rows] == ["kr", "nas"]

    def test_skips_systems_without_counters(self, dbp):
        systems = [{"machine": "kr"}, {"machine": "sg", "net_sent_total": 1}]
        assert nt.record_from_systems(systems, db_path=dbp) == []


class TestUsage:
    def test_summary_math_and_percent(self, dbp):
        nt.record_sample("kr", "ens5", 0, 0, db_path=dbp)
        # sent(出站) 400GiB + recv(入站) 200GiB：配额只算出站，pct 应只按 sent 算
        nt.record_sample("kr", "ens5", 400 * GIB, 200 * GIB, db_path=dbp)
        summary = nt.usage("kr", db_path=dbp)
        assert summary["used_gb"] == 400.0
        assert summary["sent_gb"] == 400.0
        assert summary["recv_gb"] == 200.0
        assert summary["quota_gb"] == 1000
        assert summary["pct"] == 40.0
        assert summary["nic"] == "ens5"

    def test_none_without_samples_or_unknown(self, dbp):
        assert nt.usage("nas", db_path=dbp) is None      # 无采样记录
        assert nt.usage("kr", db_path=dbp) is None       # 有配额但没采过样
        assert nt.usage("router", db_path=dbp) is None   # 不在台账

    def test_usage_without_quota_returns_cumulative_only(self, dbp):
        # Q2：nas 无配额，有采样时仍返回累计量，但 quota_gb/pct 为 None——只显累计、不画条不告警
        nt.record_sample("nas", "enp1s0", 0, 0, db_path=dbp)
        nt.record_sample("nas", "enp1s0", 100 * GIB, 60 * GIB, db_path=dbp)
        summary = nt.usage("nas", db_path=dbp)
        assert summary is not None
        assert summary["used_gb"] == 100.0
        assert summary["sent_gb"] == 100.0
        assert summary["recv_gb"] == 60.0
        assert summary["quota_gb"] is None
        assert summary["pct"] is None


class TestQuotaAlerts:
    def _fill(self, dbp, machine, gib):
        nt.record_sample(machine, "ens5", 0, 0, db_path=dbp)
        nt.record_sample(machine, "ens5", gib * GIB, 0, db_path=dbp)

    def test_under_threshold_is_quiet(self, dbp):
        self._fill(dbp, "kr", 700)
        assert nt.quota_alerts(db_path=dbp) == []

    def test_at_and_over_threshold_alerts(self, dbp):
        self._fill(dbp, "kr", 800)
        self._fill(dbp, "sg", 1200)
        alerts = {a["machine"]["id"]: a["usage"]["pct"] for a in nt.quota_alerts(db_path=dbp)}
        assert alerts == {"kr": 80.0, "sg": 120.0}

    def test_missing_table_does_not_raise(self, tmp_path):
        assert nt.quota_alerts(db_path=str(tmp_path / "no-migrations.db")) == []


# ---------------------------------------------------------------------------
# Track M1：月底出站预测
# ---------------------------------------------------------------------------


class TestMonthProjection:
    @staticmethod
    def _summary(since, used_gb=30.0, quota_gb=1000, month="2026-09"):
        return {
            "month": month,
            "since": since,
            "used_gb": used_gb,
            "quota_gb": quota_gb,
        }

    @staticmethod
    def _now(day, hour=0):
        return datetime.datetime(2026, 9, day, hour, tzinfo=datetime.timezone.utc)

    def test_under_72_hours_gives_nothing(self):
        """设计 §5.7 定的门槛：不到三天的样本外推一个月，一次大传输就能吹上天。"""
        assert nt.month_projection(
            self._summary("2026-09-08T00:00:00Z"), now=self._now(10, 23)
        ) is None

    def test_exactly_72_hours_starts_projecting(self):
        result = nt.month_projection(
            self._summary("2026-09-07T00:00:00Z", used_gb=30.0), now=self._now(10)
        )
        assert result is not None
        assert result["observed_hours"] == 72.0
        # 3 天 30 GB → 10 GB/天 → 9 月 30 天 → 300 GB
        assert result["daily_gb"] == 10.0
        assert result["projected_gb"] == 300.0

    def test_projection_uses_full_month_not_remaining_days(self):
        """partial_month 下「已用 + 日均×剩余」会把 since 之前当成 0 流量，系统性低估。"""
        result = nt.month_projection(
            self._summary("2026-09-20T00:00:00Z", used_gb=50.0), now=self._now(25)
        )
        # 5 天 50 GB = 10 GB/天 → 整月 300 GB（而不是 50 + 10×5 = 100）
        assert result["projected_gb"] == 300.0
        assert result["partial_month"] is True

    def test_full_month_ledger_is_not_partial(self):
        result = nt.month_projection(
            self._summary("2026-09-01T00:00:00Z", used_gb=100.0), now=self._now(11)
        )
        assert result["partial_month"] is False

    def test_quota_fields(self):
        result = nt.month_projection(
            self._summary("2026-09-01T00:00:00Z", used_gb=100.0, quota_gb=200),
            now=self._now(11),
        )
        # 10 天 100 GB → 10 GB/天 → 整月 300 GB，超 200 配额
        assert result["projected_pct"] == 150.0
        assert result["over_quota"] is True

    def test_no_quota_leaves_pct_none(self):
        result = nt.month_projection(
            self._summary("2026-09-01T00:00:00Z", used_gb=100.0, quota_gb=None),
            now=self._now(11),
        )
        assert result["projected_pct"] is None
        assert result["over_quota"] is False

    @pytest.mark.parametrize("bad", [None, {}, {"since": ""}, {"since": "昨天"}])
    def test_bad_input_gives_nothing(self, bad):
        assert nt.month_projection(bad) is None

    def test_stale_ledger_month_gives_nothing(self):
        """账本停在上个月（agent 掉线 / 刚跨月未采样）：used_gb 不属于本月，不许推算。

        不加这条闸门时它会安静地拿 8 月的量算 9 月的预测——数字看着完全正常，
        只是错的，比不给数字更糟。
        """
        assert nt.month_projection(
            self._summary("2026-08-01T00:00:00Z", used_gb=300.0, month="2026-08"),
            now=self._now(11),
        ) is None

    def test_days_in_month_covers_february_and_december(self):
        # 2 月 28 天 / 闰年 29 / 12 月 31（跨年分支）
        assert nt._days_in_month(datetime.datetime(2026, 2, 10)) == 28
        assert nt._days_in_month(datetime.datetime(2028, 2, 10)) == 29
        assert nt._days_in_month(datetime.datetime(2026, 12, 10)) == 31

    def test_usage_carries_projection(self, dbp):
        """usage() 直接带上预测，前端不必再算一遍。"""
        nt.record_sample(
            "kr", "ens5", 0, 0, db_path=dbp,
            now=datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc),
        )
        summary = nt.usage("kr", db_path=dbp)
        assert "projection" in summary
