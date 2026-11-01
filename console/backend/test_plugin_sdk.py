# -*- coding: utf-8 -*-
"""插件 SDK 导入边界：插件后端只能经 `plugin_sdk` 取共享框架能力。

`plugin_sdk` 导出的是同一批模块对象，不是复制或包装；插件改用 SDK 后
monkeypatch 行为、异常类型和函数返回都不变。这里同时扫描源码，防止以后
在插件里恢复对框架私有模块的直接 import。
"""
import ast
import pathlib
import sys

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
REPO_DIR = BACKEND_DIR.parent.parent
PLUGINS_DIR = REPO_DIR / 'plugins'
sys.path.insert(0, str(BACKEND_DIR))

import plugin_sdk  # noqa: E402

SDK_MODULES = (
    'money', 'pg_locks', 'storage_dispatch', 'runtime_mode', 'validation', 'yaml',
)


def _direct_imports(path):
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        for name in names:
            root = name.split('.')[0]
            if root in SDK_MODULES:
                yield node.lineno, name


def test_plugin_backends_use_plugin_sdk_for_shared_framework_modules():
    offenders = []
    for path in sorted(PLUGINS_DIR.glob('*/backend/**/*.py')):
        for lineno, name in _direct_imports(path):
            offenders.append(f'{path.relative_to(REPO_DIR)}:{lineno}: {name}')
    assert not offenders, '插件应经 plugin_sdk 导入共享框架模块:\n' + '\n'.join(offenders)


def test_plugin_sdk_reexports_the_same_module_objects():
    import yaml

    import money
    import pg_locks
    import runtime_mode
    import storage_dispatch
    import validation

    assert plugin_sdk.money is money
    assert plugin_sdk.locks is pg_locks
    assert plugin_sdk.runtime_mode is runtime_mode
    assert plugin_sdk.storage is storage_dispatch
    assert plugin_sdk.validation is validation
    assert plugin_sdk.yaml is yaml
