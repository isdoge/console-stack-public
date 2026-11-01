# -*- coding: utf-8 -*-
"""YAML 原子写与锁内读改写，保护 UI / Git 与不同设置的并发保存。"""
import fcntl
import os
import shutil
import tempfile
from contextlib import contextmanager

import yaml


@contextmanager
def _locked(path):
    directory = os.path.dirname(os.path.abspath(path)) or '.'
    os.makedirs(directory, exist_ok=True)
    with open(f'{path}.lock', 'a+', encoding='utf-8') as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield directory
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _write_locked(path, directory, data, header):
    # 唯一临时文件避免同进程多线程覆盖；失败保留原文件。
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory,
                                     prefix=os.path.basename(path) + '.tmp.', delete=False) as stream:
        tmp_path = stream.name
        try:
            if header:
                stream.write(header.rstrip('\n') + '\n')
            yaml.safe_dump(data, stream, allow_unicode=True, sort_keys=False, default_flow_style=False)
            stream.flush()
            os.fsync(stream.fileno())
            try:
                os.replace(tmp_path, path)
            except OSError:
                # 单文件 bind mount 不允许 replace（EBUSY），沿用覆盖语义。
                shutil.copyfile(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)


def atomic_write_yaml(path, data, header=None):
    """独占锁下写 YAML；header 每行须自带 #。"""
    with _locked(path) as directory:
        _write_locked(path, directory, data, header)


def mutate_yaml(path, transform, header=None):
    """同一把锁包围读取、校验、变更与落盘；变换失败不写原文件。"""
    with _locked(path) as directory:
        try:
            with open(path, encoding='utf-8') as stream:
                original = yaml.safe_load(stream)
        except FileNotFoundError:
            original = None
        updated = transform(original)
        _write_locked(path, directory, updated, header)
        return updated


def read_yaml_locked(path):
    """Share the writer lock, including its single-file bind-mount copy fallback."""
    with _locked(path):
        try:
            with open(path, encoding='utf-8') as stream:
                return yaml.safe_load(stream)
        except FileNotFoundError:
            return None
