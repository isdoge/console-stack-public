# -*- coding: utf-8 -*-
"""yaml_io 原子写回归。"""
import pathlib
import sys

import yaml

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import yaml_io  # noqa: E402


def test_atomic_write_yaml_roundtrip(tmp_path):
    path = tmp_path / "panels.yaml"
    payload = [{"id": 1, "name": "demo", "url": "http://example.test"}]
    yaml_io.atomic_write_yaml(str(path), payload)
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert loaded == payload
    assert not list(tmp_path.glob("*.tmp.*"))


def test_read_waits_for_bind_mount_copy_fallback(tmp_path, monkeypatch):
    import errno
    import shutil
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    path = str(tmp_path / 'settings.yaml')
    yaml_io.atomic_write_yaml(path, {'PLUGINS': {}})
    entered, release = Event(), Event()
    original_copy = shutil.copyfile

    def fallback(*args):
        raise OSError(errno.EBUSY, 'synthetic bind mount')

    def slow_copy(source, destination):
        pathlib.Path(destination).write_text('')
        entered.set()
        assert release.wait(5)
        return original_copy(source, destination)

    monkeypatch.setattr(yaml_io.os, 'replace', fallback)
    monkeypatch.setattr(yaml_io.shutil, 'copyfile', slow_copy)
    with ThreadPoolExecutor(max_workers=2) as pool:
        write = pool.submit(yaml_io.mutate_yaml, path, lambda old: {'PLUGINS': {'feed': {'enabled': False}}})
        assert entered.wait(5)
        try:
            read = pool.submit(yaml_io.read_yaml_locked, path)
            assert not read.done()
        finally:
            release.set()
        write.result(timeout=5)
        assert read.result(timeout=5) == {'PLUGINS': {'feed': {'enabled': False}}}
