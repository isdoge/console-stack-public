# -*- coding: utf-8 -*-
"""collect_guard 采集门禁回归（PG-02 / Q1）。

门禁是「NAS 有且只有一份采集」的第一层；第二层是 PG 事务与锁。本文件锁第一层。
"""
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import collect_guard  # noqa: E402
import runtime_mode  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_role(monkeypatch):
    monkeypatch.delenv("CONSOLE_INSTANCE_ROLE", raising=False)


def test_default_role_is_client_and_does_not_collect(monkeypatch):
    """不设角色 = client = 不采集：漏配的 Mac 不会偷偷变成第二个采集者。"""
    assert runtime_mode.instance_role() == "client"
    assert collect_guard.collection_allowed() is False


def test_authority_collects(monkeypatch):
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authority")
    assert collect_guard.collection_allowed() is True
    assert collect_guard.require_collection("探针") is True


def test_test_role_does_not_collect(monkeypatch):
    """隔离测试实例默认不采集；需要采集的用例显式声明 authority。"""
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "test")
    assert collect_guard.collection_allowed() is False


def test_require_collection_raises_for_client(monkeypatch):
    with pytest.raises(collect_guard.CollectionNotAllowed):
        collect_guard.require_collection("feed 聚合")


def test_invalid_role_is_a_config_error(monkeypatch):
    """拼错的角色必须启动即失败，不能静默按 client 处理。"""
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authorityy")
    with pytest.raises(runtime_mode.ConfigError):
        collect_guard.collection_allowed()


def test_read_only_suspends_collection_but_keeps_collector_identity(monkeypatch):
    """RO-01：维护模式**停动作、不改身份**。

    首红根因是把这两件事合成一个判断：`seed_workstation_data()` 在 read_only 下仍进
    写事务 → `ReadOnlyMode` 掀翻启动进程。修复后：
      - `collection_allowed()`（动作面）= 角色面 **且** 非 read_only → seed/采集/同步/清理跳过；
      - `is_collector()`（角色面）= 只看角色 → NAS 仍是权威端，观测 GET 本地服务、不转交。
    """
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authority")
    monkeypatch.setenv("CONSOLE_MAINTENANCE_MODE", "read_only")
    assert collect_guard.is_collector() is True
    assert collect_guard.collection_allowed() is False
    reason = collect_guard.collection_skip_reason()
    assert reason and "read_only" in reason
    with pytest.raises(collect_guard.CollectionNotAllowed, match="read_only"):
        collect_guard.require_collection("bookmark seed")
    assert "read_only" in collect_guard.collection_skipped("feed 聚合")


def test_normal_authority_still_collects(monkeypatch):
    """对照：normal 下 NAS 照旧采集——维护门禁没有把正常模式一起关掉。"""
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authority")
    monkeypatch.setenv("CONSOLE_MAINTENANCE_MODE", "normal")
    assert collect_guard.is_collector() is True
    assert collect_guard.collection_allowed() is True
    assert collect_guard.collection_skip_reason() is None
    assert collect_guard.require_collection("探针") is True


def test_client_is_never_collector_regardless_of_maintenance(monkeypatch):
    """Mac（client）：normal 与 read_only 都不采集；身份面也明确不是采集者。

    这保证「维护模式可启动」不是靠把 NAS 变成 client 实现的（§11.9.2 明令禁止）。
    """
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "client")
    monkeypatch.setenv("CONSOLE_MAINTENANCE_MODE", "normal")
    assert collect_guard.is_collector() is False
    assert collect_guard.collection_allowed() is False
    assert "authority" in (collect_guard.collection_skip_reason() or "")
    monkeypatch.setenv("CONSOLE_MAINTENANCE_MODE", "read_only")
    assert collect_guard.is_collector() is False
    assert collect_guard.collection_allowed() is False


def test_skipped_message_mentions_authority(monkeypatch):
    text = collect_guard.collection_skipped("探针")
    assert "authority" in text
    assert "探针" in text


def test_guard_module_is_leaf():
    """门禁只依赖 runtime_mode：不 import app，也不在 import 期拨号。"""
    import subprocess

    source = (
        "import sys; import collect_guard; "
        "print('app' in sys.modules, 'psycopg' in sys.modules)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True,
        cwd=str(BACKEND_DIR), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False False"
