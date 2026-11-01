# -*- coding: utf-8 -*-
"""runtime_mode 的运行角色/维护模式判定（提案 §6.4）。"""
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import runtime_mode  # noqa: E402


def test_defaults_are_safe_side(monkeypatch):
    for name in (
        "CONSOLE_STORAGE_ENGINE",
        "CONSOLE_INSTANCE_ROLE",
        "CONSOLE_MAINTENANCE_MODE",
    ):
        monkeypatch.delenv(name, raising=False)
    assert runtime_mode.storage_engine() == "sqlite"
    assert runtime_mode.instance_role() == "client"
    assert runtime_mode.maintenance_mode() == "normal"
    assert runtime_mode.is_authority() is False
    assert runtime_mode.writes_allowed() is True


def test_authority_role_is_recognised(monkeypatch):
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authority")
    assert runtime_mode.is_authority() is True
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "AUTHORITY")  # 大小写不敏感
    assert runtime_mode.is_authority() is True


def test_invalid_values_fail_fast_instead_of_silently_downgrading(monkeypatch):
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authorityy")
    with pytest.raises(runtime_mode.ConfigError):
        runtime_mode.instance_role()
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "mysql")
    with pytest.raises(runtime_mode.ConfigError):
        runtime_mode.storage_engine()
    monkeypatch.setenv("CONSOLE_MAINTENANCE_MODE", "readonly")
    with pytest.raises(runtime_mode.ConfigError):
        runtime_mode.maintenance_mode()


def test_blank_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "   ")
    assert runtime_mode.instance_role() == "client"


def test_read_only_blocks_writes_and_reports_reason(monkeypatch):
    monkeypatch.setenv("CONSOLE_MAINTENANCE_MODE", "read_only")
    assert runtime_mode.is_read_only() is True
    assert runtime_mode.writes_allowed() is False
    with pytest.raises(runtime_mode.ReadOnlyMode) as excinfo:
        runtime_mode.require_writable("账本写入")
    assert "账本写入" in str(excinfo.value)


def test_snapshot_is_redacted(monkeypatch):
    monkeypatch.setenv("CONSOLE_PG_PASSWORD", "super-secret")
    snapshot = runtime_mode.snapshot()
    assert set(snapshot) == {
        "storage_engine",
        "instance_role",
        "maintenance_mode",
        "writes_allowed",
    }
    assert "super-secret" not in repr(snapshot)
