# -*- coding: utf-8 -*-
"""node_traffic 的 PG 路径回归（PG-02）。

在真 PG16 上复验 SQLite 版锁住的四类坑（网卡挑错、计数器归零、月份翻页、换网卡），
另加 PG 专属的并发要求（提案 §4.2「流量按机器串行，拒绝旧快照」）：

- 每机器 advisory lock 让「读→算→写」串行，并发采样不丢增量；
- 时间倒退的旧快照被拒，不会被当成 agent 重启而重复计入。
"""
import datetime
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import machine_config as mc  # noqa: E402
import node_traffic as nt  # noqa: E402
from pg02_support import fetch_all  # noqa: E402

# 夹具通过插件注册加载：直接 import 同名夹具会被 pyflakes 判 F811（测试参数同名）。
pytest_plugins = ("pg02_support",)


GIB = 1024 ** 3


@pytest.fixture(autouse=True)
def _clean_registry():
    mc.invalidate()
    yield
    mc.invalidate()


class TestRecordSamplePg:
    def test_first_sample_only_sets_baseline(self, pg_client):
        row = nt.record_sample("kr", "ens5", 1000, 2000)
        assert (row["sent_bytes"], row["recv_bytes"]) == (0, 0)
        assert (row["last_sent_total"], row["last_recv_total"]) == (1000, 2000)

    def test_second_sample_accumulates_delta(self, pg_client):
        nt.record_sample("kr", "ens5", 1000, 2000)
        row = nt.record_sample("kr", "ens5", 1500, 2200)
        assert (row["sent_bytes"], row["recv_bytes"]) == (500, 200)

    def test_counter_reset_counts_from_zero_not_negative(self, pg_client):
        nt.record_sample("kr", "ens5", 1000, 2000)
        nt.record_sample("kr", "ens5", 1500, 2200)
        row = nt.record_sample("kr", "ens5", 30, 40)
        assert (row["sent_bytes"], row["recv_bytes"]) == (530, 240)
        assert row["resets"] == 1
        assert (row["last_sent_total"], row["last_recv_total"]) == (30, 40)

    def test_month_rollover_resets_counters(self, pg_client):
        aug = datetime.datetime(2026, 8, 31, 23, 0, tzinfo=datetime.timezone.utc)
        sep = datetime.datetime(2026, 9, 1, 1, 0, tzinfo=datetime.timezone.utc)
        nt.record_sample("kr", "ens5", 1000, 2000, now=aug)
        nt.record_sample("kr", "ens5", 5000, 6000, now=aug)
        row = nt.record_sample("kr", "ens5", 9000, 9000, now=sep)
        assert row["month"] == "2026-09"
        assert (row["sent_bytes"], row["recv_bytes"]) == (0, 0)
        assert (row["last_sent_total"], row["last_recv_total"]) == (9000, 9000)

    def test_nic_change_rebases_baseline_but_keeps_month_total(self, pg_client):
        nt.record_sample("kr", "ens5", 0, 0)
        nt.record_sample("kr", "ens5", 10_000, 20_000)
        row = nt.record_sample("kr", "eth0", 5, 5)
        assert (row["sent_bytes"], row["recv_bytes"]) == (10_000, 20_000)
        assert (row["last_sent_total"], row["last_recv_total"]) == (5, 5)
        assert row["nic"] == "eth0"
        row = nt.record_sample("kr", "eth0", 105, 205)
        assert (row["sent_bytes"], row["recv_bytes"]) == (10_100, 20_200)

    def test_blank_machine_is_ignored(self, pg_client):
        assert nt.record_sample("", "ens5", 1, 1) is None
        assert fetch_all(pg_client, "SELECT * FROM machine_traffic") == []

    def test_stale_sample_is_rejected(self, pg_client):
        """时间倒退的旧快照不能改状态：计数器「变小」会被误当成 agent 重启。"""
        new = datetime.datetime(2026, 9, 10, 12, 0, tzinfo=datetime.timezone.utc)
        old = datetime.datetime(2026, 9, 10, 11, 0, tzinfo=datetime.timezone.utc)
        nt.record_sample("kr", "ens5", 1000, 2000, now=new)
        row = nt.record_sample("kr", "ens5", 10, 20, now=old)
        # 旧样本被拒：累计量、基线、重置计数都不动
        assert (row["sent_bytes"], row["recv_bytes"]) == (0, 0)
        assert (row["last_sent_total"], row["last_recv_total"]) == (1000, 2000)
        assert row["resets"] == 0
        assert row["updated_at"] == new.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_same_sample_is_idempotent(self, pg_client):
        stamp = datetime.datetime(2026, 9, 10, 12, 0, tzinfo=datetime.timezone.utc)
        nt.record_sample("kr", "ens5", 1000, 2000, now=stamp)
        row = nt.record_sample("kr", "ens5", 1000, 2000, now=stamp)
        assert (row["sent_bytes"], row["recv_bytes"]) == (0, 0)

    def test_concurrent_samples_do_not_double_count(self, pg_client):
        """两条并发连接同时采样：每机器锁保证增量只算一次。"""
        import pg_test_support

        nt.record_sample("kr", "ens5", 1000, 2000)
        other = pg_test_support.connect(pg_client["app_dsn"])
        try:
            # 另一条连接先写入更晚的采样
            nt.record_sample("kr", "ens5", 1600, 2400)
        finally:
            other.close()
        row = nt.record_sample("kr", "ens5", 1900, 2600)
        assert (row["sent_bytes"], row["recv_bytes"]) == (900, 600)

    def test_record_reuses_caller_transaction(self, pg_client):
        import db as dbmod

        with dbmod.pg_transaction() as conn:
            nt.record_sample("kr", "ens5", 1000, 2000, conn=conn)
            conn.rollback()
        assert fetch_all(pg_client, "SELECT * FROM machine_traffic") == []


class TestUsagePg:
    def test_summary_math_and_percent(self, pg_client):
        nt.record_sample("kr", "ens5", 0, 0)
        nt.record_sample("kr", "ens5", 400 * GIB, 200 * GIB)
        summary = nt.usage("kr")
        assert summary["used_gb"] == 400.0
        assert summary["sent_gb"] == 400.0
        assert summary["recv_gb"] == 200.0
        assert summary["quota_gb"] == 1000
        assert summary["pct"] == 40.0
        assert summary["nic"] == "ens5"

    def test_none_without_samples_or_unknown(self, pg_client):
        assert nt.usage("nas") is None
        assert nt.usage("kr") is None
        assert nt.usage("router") is None

    def test_usage_without_quota_returns_cumulative_only(self, pg_client):
        nt.record_sample("nas", "enp1s0", 0, 0)
        nt.record_sample("nas", "enp1s0", 100 * GIB, 60 * GIB)
        summary = nt.usage("nas")
        assert summary["used_gb"] == 100.0
        assert summary["quota_gb"] is None
        assert summary["pct"] is None

    def test_usage_carries_projection(self, pg_client):
        nt.record_sample(
            "kr", "ens5", 0, 0,
            now=datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc),
        )
        assert "projection" in nt.usage("kr")


class TestQuotaAlertsPg:
    def _fill(self, machine, gib):
        nt.record_sample(machine, "ens5", 0, 0)
        nt.record_sample(machine, "ens5", gib * GIB, 0)

    def test_under_threshold_is_quiet(self, pg_client):
        self._fill("kr", 700)
        assert nt.quota_alerts() == []

    def test_at_and_over_threshold_alerts(self, pg_client):
        self._fill("kr", 800)
        self._fill("sg", 1200)
        alerts = {a["machine"]["id"]: a["usage"]["pct"] for a in nt.quota_alerts()}
        assert alerts == {"kr": 80.0, "sg": 120.0}


class TestRecordFromSystemsPg:
    def test_reused_transaction_takes_per_machine_lock_before_read(self, pg_client):
        import psycopg.errors

        import db as dbmod
        import pg_locks
        import pg_test_support

        holder = pg_test_support.connect(pg_client["app_dsn"])
        try:
            pg_locks.acquire(holder, pg_locks.traffic_lock("kr"))
            with dbmod.pg_transaction() as conn:
                conn.execute("SET LOCAL lock_timeout = '100ms'")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    nt.record_sample("kr", "ens5", 1, 2, conn=conn)
                conn.rollback()
        finally:
            holder.rollback()
            holder.close()
        assert fetch_all(pg_client, "SELECT * FROM machine_traffic") == []

    def test_records_all_machines_not_just_quota(self, pg_client):
        systems = [
            {"machine": "kr", "net_nic": "ens5", "net_sent_total": 1, "net_recv_total": 2, "net_sampled_at": "2026-09-10T10:00:00Z"},
            {"machine": "nas", "net_nic": "enp1s0", "net_sent_total": 9, "net_recv_total": 9, "net_sampled_at": "2026-09-10T10:00:00Z"},
        ]
        rows = nt.record_from_systems(systems)
        assert [r["machine"] for r in rows] == ["kr", "nas"]

    def test_skips_systems_without_counters(self, pg_client):
        systems = [{"machine": "kr"}, {"machine": "sg", "net_sent_total": 1}]
        assert nt.record_from_systems(systems) == []

    def test_unknown_machine_is_skipped(self, pg_client):
        systems = [
            {"machine": "ghost", "net_nic": "ens5", "net_sent_total": 1, "net_recv_total": 2, "net_sampled_at": "2026-09-10T10:00:00Z"}
        ]
        assert nt.record_from_systems(systems) == []
