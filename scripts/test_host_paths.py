from pathlib import Path

import pytest

from scripts import host_paths


def test_nas_defaults_and_boundary(monkeypatch):
    monkeypatch.setattr(Path, 'is_file', lambda self: False)
    assert host_paths.storage_paths('/opt/console-stack', {}) == (
        Path('/opt/.bak/console-stack'), Path('/opt/.trash/console-stack'))
    with pytest.raises(ValueError, match='NAS'):
        host_paths.storage_paths('/opt/console-stack', {'CONSOLE_HOST_BACKUP_DIR': '/tmp/backup'})


def configure(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    backup, trash = tmp_path / '.bak/repo', tmp_path / '.trash/repo'
    (root / '.env').write_text(f'CONSOLE_HOST_BACKUP_DIR={backup}\nCONSOLE_HOST_TRASH_DIR={trash}\n')
    return root, backup, trash


def test_external_paths_and_cutover_guard(tmp_path):
    from scripts import backup_runtime as runtime
    root, backup, trash = configure(tmp_path)
    assert host_paths.storage_paths(root, {}) == (backup, trash)
    (backup / 'cutover').mkdir(parents=True)
    (backup / 'cutover/state.json').write_text('{"state":"completed"}')
    assert runtime.cutover_writes_opened(root)
    with pytest.raises(ValueError, match='PG'):
        runtime.deployment_environment(root, {'CONSOLE_STORAGE_ENGINE': 'sqlite'})
    assert host_paths.timed_backup_dir(root) == backup / 'timed'
    assert host_paths.backup_path(root, runtime.CUTOVER_SOURCE_RELATIVE) == backup / 'cutover/console.db'
    assert 'scripts/host_paths.py' in runtime.controller_sources()


@pytest.mark.parametrize('value', ['relative', '/tmp/../backup', '', '/tmp/a\nCONSOLE_HOST_BACKUP_DIR=/tmp/b'])
def test_invalid_configuration(tmp_path, value):
    root, _, _ = configure(tmp_path)
    (root / '.env').write_text(f'CONSOLE_HOST_BACKUP_DIR={value}\n')
    with pytest.raises(ValueError):
        host_paths.storage_paths(root, {})


def test_conflict_overlap_and_checkout_rejected(tmp_path):
    root, backup, _ = configure(tmp_path)
    with pytest.raises(ValueError, match='冲突'):
        host_paths.storage_paths(root, {'CONSOLE_HOST_BACKUP_DIR': '/different'})
    for target in (root / 'backup', root, root.parent):
        (root / '.env').write_text(f'CONSOLE_HOST_BACKUP_DIR={target}\n')
        with pytest.raises(ValueError, match='之外'):
            host_paths.storage_paths(root, {})
    (root / '.env').write_text(f'CONSOLE_HOST_BACKUP_DIR={backup}\nCONSOLE_HOST_TRASH_DIR={backup}/trash\n')
    with pytest.raises(ValueError, match='重叠'):
        host_paths.storage_paths(root, {})


def test_external_runtime_executable_and_retention(tmp_path, monkeypatch):
    from scripts import backup_runtime as runtime
    root, backup, trash = configure(tmp_path)
    (root / 'console/backend/data').mkdir(parents=True)
    published = runtime.publish_runtime(root)
    assert Path(published['entry']) == backup / '.runtime/entry.py'
    assert runtime.validate_runtime(root)['version'] == published['version']
    monkeypatch.setattr(runtime, 'read_crontab', lambda root: '')
    plan = runtime.cron_migration_plan(root)
    assert str(backup / '.runtime/entry.py') in plan['command']
    for tag in ('20261001-000000', '20261002-000000'):
        for member in runtime.MEMBERS:
            (backup / f'{member}.{tag}').write_text('fixture')
    runtime.retire_groups(root, keep=1)
    assert (backup / 'gitsha.20261002-000000').exists()
    assert not (backup / 'gitsha.20261001-000000').exists()
    assert list(trash.rglob('gitsha.20261001-000000'))
    assert not (root / 'backup').exists()
