# -*- coding: utf-8 -*-
"""pg_locks 的命名稳定性与固定顺序（提案 §4.2）。不需要数据库。"""
import pathlib
import subprocess
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import pg_locks  # noqa: E402


def test_lock_keys_are_fixed_constants_not_process_random():
    assert pg_locks.LOCK_NAMESPACE == 1129270867
    assert pg_locks.LEDGER.key == 10
    assert pg_locks.RENEWALS.key == 11
    assert pg_locks.LEDGER.shared is False
    assert pg_locks.MAINTENANCE_SHARED.shared is True


def test_maintenance_exclusive_and_shared_share_one_key():
    # 共用 key 才能让「维护控制器独占」真正挡住「业务写事务共享」。
    assert pg_locks.MAINTENANCE.key == pg_locks.MAINTENANCE_SHARED.key
    assert pg_locks.MAINTENANCE.shared is False
    assert pg_locks.MAINTENANCE_SHARED.shared is True


def test_traffic_lock_is_stable_across_processes():
    # 进程随机 hash 会让同一台机器两次启动拿到不同的锁；crc32 派生必须跨进程一致。
    code = (
        "import sys; sys.path.insert(0, %r); import pg_locks; "
        "print(pg_locks.traffic_lock('nas').key)" % str(BACKEND_DIR)
    )
    first = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    second = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert first == second == str(pg_locks.traffic_lock("nas").key)


def test_traffic_lock_requires_machine_name():
    with pytest.raises(ValueError):
        pg_locks.traffic_lock("")
    with pytest.raises(ValueError):
        pg_locks.traffic_lock(None)


def test_sort_locks_orders_maintenance_before_domain_locks():
    ordered = pg_locks.sort_locks([pg_locks.RENEWALS, pg_locks.LEDGER, pg_locks.MAINTENANCE_SHARED])
    assert [lock.name for lock in ordered] == ["maintenance_shared", "ledger", "renewals"]


def test_sort_locks_deduplicates_identical_keys():
    ordered = pg_locks.sort_locks([pg_locks.LEDGER, pg_locks.LEDGER, pg_locks.LEDGER])
    assert len(ordered) == 1


def test_traffic_locks_do_not_collide_for_distinct_machines():
    machines = ["nas", "mac", "aly", "txy", "us", "hindsight-pg"]
    keys = {pg_locks.traffic_lock(machine).key for machine in machines}
    assert len(keys) == len(machines)


def test_order_index_puts_unknown_domain_locks_last():
    unknown = pg_locks.LockKey("custom", 9999)
    assert pg_locks.order_index(pg_locks.MAINTENANCE_SHARED) < pg_locks.order_index(unknown)
