# -*- coding: utf-8 -*-
"""B7-1：client / read_only 不启动后台写任务（周期 job 线程）。

只测**线程启动判定**，不真的等一个 900 秒循环：把 `threading.Thread` 换成一
个记录调用的桩，就能证明闸门在启动点生效，而不是靠每个 job 内部再判断。
服务（监听器）保持 authority/启用状态的既有语义，不在本文件的断言范围。
"""
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import plugin_runtime as runtime  # noqa: E402


@pytest.fixture
def job_registry(monkeypatch):
    started = []
    monkeypatch.setattr(runtime, '_jobs', [('feed', 'refresh', 900, lambda: None)])
    monkeypatch.setattr(runtime.threading, 'Thread', lambda **kwargs: _Record(started, kwargs))
    return started


class _Record:
    """Thread 的替身：start() 只记录标题，不真的起线程。"""

    def __init__(self, started, kwargs):
        self.started = started
        self.kwargs = kwargs

    def start(self):
        self.started.append(self.kwargs.get('name'))


@pytest.mark.parametrize('role,mode', [
    ('client', 'normal'),
    ('authority', 'read_only'),
    ('client', 'read_only'),
    ('test', 'normal'),
])
def test_background_jobs_do_not_start_for_non_collectors(job_registry, monkeypatch, role, mode):
    monkeypatch.setenv('CONSOLE_INSTANCE_ROLE', role)
    monkeypatch.setenv('CONSOLE_MAINTENANCE_MODE', mode)
    runtime.start_jobs()
    assert job_registry == []


def test_background_jobs_start_only_for_writable_authority(job_registry, monkeypatch):
    monkeypatch.setenv('CONSOLE_INSTANCE_ROLE', 'authority')
    monkeypatch.setenv('CONSOLE_MAINTENANCE_MODE', 'normal')
    runtime.start_jobs()
    assert job_registry == ['plugin-feed-refresh']


def test_background_writes_gate_matches_collection_gate(monkeypatch):
    monkeypatch.setenv('CONSOLE_INSTANCE_ROLE', 'client')
    monkeypatch.setenv('CONSOLE_MAINTENANCE_MODE', 'normal')
    assert runtime.background_writes_allowed() is False
    monkeypatch.setenv('CONSOLE_INSTANCE_ROLE', 'authority')
    assert runtime.background_writes_allowed() is True
    monkeypatch.setenv('CONSOLE_MAINTENANCE_MODE', 'read_only')
    assert runtime.background_writes_allowed() is False
