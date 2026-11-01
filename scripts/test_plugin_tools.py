"""Executable plugin developer workflow and refusal cases in isolated temporary trees."""
import json
import subprocess
import sys

import pytest

import check_plugins
import new_plugin


@pytest.fixture
def root(tmp_path):
    (tmp_path / 'plugins').mkdir()
    (tmp_path / 'plugins/contract.json').write_bytes((check_plugins.ROOT / 'plugins/contract.json').read_bytes())
    (tmp_path / 'plugins/composition.json').write_text('{"plugins": []}\n', encoding='utf-8')
    (tmp_path / 'console/backend').mkdir(parents=True)
    return tmp_path


def test_template_is_valid_and_backend_runs(root):
    target = new_plugin.create(root, 'example', '示例', '示例说明')
    assert check_plugins.check(root, check_index=False) == []
    result = subprocess.run([sys.executable, '-m', 'pytest', str(target / 'backend'), '-q'], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    original = (target / 'manifest.json').read_bytes()
    with pytest.raises(FileExistsError):
        new_plugin.create(root, 'example', 'replace', 'replace')
    assert (target / 'manifest.json').read_bytes() == original


def test_bad_id_and_symlink_are_rejected(root):
    for pid in ('../oops', 'Upper', '', 'x'*49):
        with pytest.raises(ValueError):
            new_plugin.create(root, pid, 'name', 'description')
    (root / 'plugins/example').symlink_to(root / 'outside')
    with pytest.raises(FileExistsError):
        new_plugin.create(root, 'example', 'name', 'description')


def test_checker_rejects_core_dependency_missing_export_and_owner_conflict(root):
    one = new_plugin.create(root, 'one', 'One', 'First')
    two = new_plugin.create(root, 'two', 'Two', 'Second')
    for target in (one, two):
        p = target / 'manifest.json'
        data = json.loads(p.read_text())
        data['owns']['tables'] = ['same_table']
        p.write_text(json.dumps(data))
    assert any('所有权冲突' in e for e in check_plugins.check(root, check_index=False))
    data['owns']['tables'] = []
    data['sidebar'] = True
    (two / 'manifest.json').write_text(json.dumps(data))
    with (one / 'backend/__init__.py').open('a') as stream:
        stream.write('\nimport app\n')
    errors = check_plugins.check(root, check_index=False)
    assert any('Sidebar' in e for e in errors)
    assert any('禁止 app' in e for e in errors)


def test_navigation_loader_is_required_and_accepts_async_export(root):
    target = new_plugin.create(root, 'sample', '示例', '合成导航')
    manifest = target / 'manifest.json'
    data = json.loads(manifest.read_text())
    data['navigation'] = True
    manifest.write_text(json.dumps(data))
    assert any('loadNavigation' in error for error in check_plugins.check(root, check_index=False))
    with (target / 'frontend/index.tsx').open('a') as stream:
        stream.write('\nexport async function loadNavigation() { return { links: [] }; }\n')
    assert check_plugins.check(root, check_index=False) == []


def test_repository_manifests_and_index_are_consistent():
    assert check_plugins.check() == []


def test_core_frontend_cannot_import_business_through_legacy_bridge(root):
    source = root / 'kernel/frontend/src'
    (source / 'lib').mkdir(parents=True)
    (source / 'lib/legacy.ts').write_text("export * from '@plugins/one/frontend/lib/api';\n")
    (source / 'App.tsx').write_text("import { load } from '@/lib/legacy';\n")
    assert any('框架依赖插件业务' in e for e in check_plugins.check(root, check_index=False))


def test_alternate_root_contract_and_cross_plugin_imports(root):
    path = root / 'plugins/contract.json'
    contract = json.loads(path.read_text())
    contract['reserved_routes'].append('/sample')
    path.write_text(json.dumps(contract))
    with pytest.raises(ValueError, match='框架入口'):
        new_plugin.create(root, 'sample', 'name', 'description')
    target = new_plugin.create(root, 'one', 'One', 'First')
    with (target / 'backend/__init__.py').open('a') as stream:
        stream.write('\nfrom plugins import two\nfrom ...two import backend\n')
    errors = check_plugins.check(root, check_index=False)
    assert sum('跨插件' in e for e in errors) == 2
    assert any('索引缺失' in e for e in check_plugins.check(root))
