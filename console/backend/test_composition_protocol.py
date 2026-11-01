# -*- coding: utf-8 -*-
"""B7-1 组合协议：显式组合文件的解析、根、专用 env 优先、外置插件根与规则输入。

只测边界与真实 import：组合文件损坏/缺失/跨档案一律有界失败；外置插件根按
`plugins.<id>` 从该根导入；私人 6 机 18 规则与现役等价，公共空表得 0 规则。
"""
import json
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import composition  # noqa: E402
import monitoring_alert as ma  # noqa: E402

REPO_DIR = BACKEND_DIR.parent.parent


_KEYS = (
    'CONSOLE_COMPOSITION_FILE', 'CONSOLE_INSTANCE_PROFILE',
    'CONSOLE_CONFIG_ROOT', 'CONSOLE_DATA_ROOT', 'CONSOLE_STATIC_ROOT',
    'OPS_BACKUP_DIR', 'CONSOLE_PLUGIN_ROOT',
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """bootstrap 直接写 os.environ（setdefault 语义），必须快照还原，不能只靠 monkeypatch。"""
    import machine_config as mc

    mc.invalidate()
    before = {key: os.environ.get(key) for key in _KEYS}
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    yield
    for key, value in before.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    mc.invalidate()


def _plugins_root(base, ids=('alpha',)):
    root = base / 'plugins'
    (root).mkdir(parents=True, exist_ok=True)
    (root / 'contract.json').write_text('{}', encoding='utf-8')
    for pid in ids:
        (root / pid).mkdir(exist_ok=True)
        (root / pid / 'manifest.json').write_text('{}', encoding='utf-8')
    (root / 'composition.json').write_text(json.dumps({'plugins': list(ids)}), encoding='utf-8')
    return root


def _composition(base, profile='private', name='t', **over):
    config = base / 'config'
    config.mkdir(exist_ok=True)
    document = {
        'schema_version': 1,
        'profile': profile,
        'name': name,
        'roots': {
            'config': 'config',
            'data': 'data',
            'static': 'static',
            'backup': 'backup',
            'plugins': 'plugins',
        },
        'host': {},
        'alert_machines': {},
    }
    document.update(over)
    path = base / 'composition.json'
    path.write_text(json.dumps(document), encoding='utf-8')
    return path


class TestRootsAndRootPaths:
    def test_missing_config_root_fails_closed(self, tmp_path, monkeypatch):
        _plugins_root(tmp_path)
        path = _composition(tmp_path)
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        (tmp_path / 'config').rmdir()
        with pytest.raises(composition.CompositionError, match='roots.config'):
            composition.resolve_composition()

    def test_plugins_root_must_be_real_directory_with_contract(self, tmp_path, monkeypatch):
        for body in ('missing', 'contract-missing'):
            plugins = _plugins_root(tmp_path, ids=())
            path = _composition(tmp_path)
            monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
            if body == 'missing':
                (plugins / 'contract.json').unlink()
                with pytest.raises(composition.CompositionError, match='contract.json'):
                    composition.resolve_composition()
            else:
                (tmp_path / 'plugins').rename(tmp_path / 'elsewhere')
                with pytest.raises(composition.CompositionError, match='roots.plugins'):
                    composition.resolve_composition()

    def test_data_static_backup_are_not_created_by_reads(self, tmp_path, monkeypatch):
        _plugins_root(tmp_path)
        path = _composition(tmp_path)
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        for name in ('data', 'static', 'backup'):
            assert not (tmp_path / name).exists(), name
            assert composition.root_path(name) == (tmp_path / name).resolve()
            assert not (tmp_path / name).exists(), name

    def test_relative_roots_resolve_against_composition_directory(self, tmp_path, monkeypatch):
        nested = tmp_path / 'instance'
        nested.mkdir()
        _plugins_root(nested)
        path = _composition(nested)
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        assert composition.root_path('config') == (nested / 'config').resolve()
        assert composition.root_path('backup') == (nested / 'backup').resolve()

    def test_absolute_roots_are_used_as_is(self, tmp_path, monkeypatch):
        _plugins_root(tmp_path)
        elsewhere = tmp_path / 'abs-data'
        path = _composition(tmp_path, roots={
            'config': 'config', 'data': str(elsewhere), 'static': 'static',
            'backup': 'backup', 'plugins': 'plugins',
        })
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        assert composition.root_path('data') == elsewhere.resolve()

    def test_unknown_root_name_is_rejected(self, tmp_path, monkeypatch):
        _plugins_root(tmp_path)
        path = _composition(tmp_path)
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        with pytest.raises(composition.CompositionError, match='未知组合根'):
            composition.root_path('secrets')


class TestRootSymlinkBoundary:
    @pytest.mark.parametrize('name', composition.ROOT_NAMES)
    def test_symlinked_root_is_rejected(self, tmp_path, monkeypatch, name):
        """五个根都不接受软链接，哪怕链接目标仍在组合目录内。"""
        _plugins_root(tmp_path)
        path = _composition(tmp_path)
        if name == 'plugins':
            (tmp_path / 'plugins').rename(tmp_path / 'plugins-real')
            (tmp_path / 'plugins').symlink_to(
                tmp_path / 'plugins-real', target_is_directory=True
            )
        elif name == 'config':
            (tmp_path / 'config').rmdir()
            (tmp_path / 'config').symlink_to(
                tmp_path / 'config-real', target_is_directory=True
            )
        else:
            (tmp_path / name).symlink_to(
                tmp_path / f'{name}-real', target_is_directory=True
            )
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        with pytest.raises(composition.CompositionError, match='符号链接'):
            composition.resolve_composition()

    def test_intermediate_symlink_is_rejected(self, tmp_path, monkeypatch):
        _plugins_root(tmp_path)
        (tmp_path / 'outside').mkdir()
        (tmp_path / 'linked').symlink_to(tmp_path / 'outside', target_is_directory=True)
        path = _composition(tmp_path, roots={
            'config': 'config', 'data': 'linked/data', 'static': 'static',
            'backup': 'backup', 'plugins': 'plugins',
        })
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        with pytest.raises(composition.CompositionError, match='符号链接'):
            composition.resolve_composition()

    def test_checked_in_private_relative_roots_still_resolve(self, monkeypatch):
        """现役私人实例用 `..` 指向仓库上层；拒绝软链接不能误伤这种布局。"""
        path = REPO_DIR / 'instance/private/composition.json'
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        resolved = composition.resolve_composition()
        assert resolved['roots']['config'] == str((REPO_DIR / 'console').resolve())
        assert resolved['roots']['data'] == str(
            (REPO_DIR / 'console/backend/data').resolve()
        )
        assert resolved['roots']['plugins'] == str((REPO_DIR / 'plugins').resolve())


class TestLegacyCompatibility:
    def test_no_file_returns_legacy_roots_and_policy(self, monkeypatch):
        composition_result = composition.resolve_composition()
        assert composition_result['file'] is None
        assert composition_result['profile'] == composition.PROFILE_PRIVATE
        assert set(composition_result['roots']) == set(composition.ROOT_NAMES)
        assert composition_result['roots']['config'] == str(BACKEND_DIR.parent.resolve())
        assert composition_result['host']['local_machine'] == 'nas'
        assert sorted(composition_result['alert_machines']) == ['aly', 'kr', 'nas', 'sg', 'txy', 'us']

    def test_no_file_legacy_matches_checked_in_private_instance(self):
        """显式私人实例文件与无文件 legacy 默认逐项等价，避免两套事实源漂移。"""
        document = json.loads(
            (REPO_DIR / 'instance/private/composition.json').read_text(encoding='utf-8')
        )
        legacy = composition.resolve_composition()
        assert document['alert_machines'] == legacy['alert_machines']
        assert document['host'] == legacy['host']
        for name in ('config', 'data', 'static', 'backup', 'plugins'):
            expected = (REPO_DIR / 'instance/private' / document['roots'][name]).resolve()
            assert str(expected) == legacy['roots'][name], name

    def test_bootstrap_does_not_change_env_without_file(self, monkeypatch):
        composition.bootstrap_environment()
        for key in (
            'CONSOLE_CONFIG_ROOT', 'CONSOLE_DATA_ROOT', 'CONSOLE_STATIC_ROOT',
            'OPS_BACKUP_DIR', 'CONSOLE_PLUGIN_ROOT',
        ):
            assert key not in os.environ


class TestProfileAndFileConflicts:
    def test_public_without_file_is_rejected(self, monkeypatch):
        monkeypatch.setenv('CONSOLE_INSTANCE_PROFILE', 'public')
        with pytest.raises(composition.CompositionError, match='CONSOLE_COMPOSITION_FILE'):
            composition.resolve_composition()

    @pytest.mark.parametrize('env_profile', ['private', 'public'])
    def test_env_profile_conflicting_with_file_is_rejected(self, tmp_path, monkeypatch, env_profile):
        _plugins_root(tmp_path)
        other = 'public' if env_profile == 'private' else 'private'
        path = _composition(tmp_path, profile=other)
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        monkeypatch.setenv('CONSOLE_INSTANCE_PROFILE', env_profile)
        with pytest.raises(composition.CompositionError, match='不一致'):
            composition.resolve_composition()

    def test_file_profile_wins_when_env_is_unset(self, tmp_path, monkeypatch):
        _plugins_root(tmp_path)
        path = _composition(tmp_path, profile='public')
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        assert composition.instance_profile() == composition.PROFILE_PUBLIC


class TestDocumentValidation:
    @pytest.mark.parametrize('case', json.loads(
        (REPO_DIR / 'kernel/testdata/composition-protocol.json').read_text(encoding='utf-8'),
    ), ids=lambda case: case['name'])
    def test_shared_frontend_backend_inputs(self, tmp_path, monkeypatch, case):
        _plugins_root(tmp_path)
        path = _composition(tmp_path, **case['patch'])
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        if case['accepted']:
            resolved = composition.resolve_composition()
            assert resolved['profile'] == 'private'
            assert all(value is not None for value in resolved['host'].values())
        else:
            with pytest.raises(composition.CompositionError):
                composition.resolve_composition()

    @pytest.mark.parametrize('body', [
        'not json',
        '[]',
        '{"schema_version": 2, "profile": "private", "roots": {}, "host": {}, "alert_machines": {}}',
        '{"schema_version": 1, "profile": "prod", "roots": {}, "host": {}, "alert_machines": {}}',
        '{"schema_version": 1, "profile": "private", "roots": {"config": "config"}, "host": {}, "alert_machines": {}}',
        '{"schema_version": 1, "profile": "private", "roots": {}, "host": {}, "alert_machines": {}, "extra": 1}',
        '{"schema_version": 1, "profile": "private", "roots": {}, "host": {}, "alert_machines": []}',
        '{"schema_version": 1, "profile": "private", "roots": {}, "host": {"nope": 1}, "alert_machines": {}}',
        '{"schema_version": 1, "profile": "private", "roots": {}, "host": {"home_lan_ip": "not-an-ip"}, "alert_machines": {}}',
        '{"schema_version": 1, "profile": "private", "roots": {}, "host": {}, "alert_machines": {"nas": {"display": "NAS"}}}',
        '{"schema_version": 1, "profile": "private", "roots": {}, "host": {}, "alert_machines": {"nas": {"display": "NAS", "load5_threshold": 0}}}',
        '{"schema_version": 1, "profile": "private", "profile": "public", "roots": {}, "host": {}, "alert_machines": {}}',
    ])
    def test_malformed_compositions_are_rejected(self, tmp_path, monkeypatch, body):
        path = tmp_path / 'composition.json'
        path.write_text(body, encoding='utf-8')
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        with pytest.raises(composition.CompositionError):
            composition.resolve_composition()

    @pytest.mark.parametrize('cancelled', [False, True])
    def test_composition_file_rejects_linked_ancestor(self, tmp_path, monkeypatch, cancelled):
        _plugins_root(tmp_path)
        _composition(tmp_path)
        real = tmp_path / 'real'
        real.mkdir()
        link = tmp_path / 'linked'
        link.symlink_to(real, target_is_directory=True)
        candidate = link / '..' / 'composition.json' if cancelled else link / 'composition.json'
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(candidate))
        with pytest.raises(composition.CompositionError, match='符号链接'):
            composition.resolve_composition()

    def test_symlinked_composition_file_is_rejected(self, tmp_path, monkeypatch):
        target = tmp_path / 'real.json'
        target.write_text('{}', encoding='utf-8')
        link = tmp_path / 'composition.json'
        link.symlink_to(target)
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(link))
        with pytest.raises(composition.CompositionError, match='普通文件'):
            composition.resolve_composition()


class TestHostPolicyAndAlertMachines:
    def test_explicit_file_does_not_add_private_host_defaults(self, tmp_path, monkeypatch):
        _plugins_root(tmp_path)
        path = _composition(tmp_path)
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        host = composition.host_policy()
        assert host['local_machine'] == ''
        assert host['home_machines'] == []
        assert host['excluded_machine_ids'] == []
        assert host['home_lan_ip'] == ''
        assert host['host_control_target'] == ''

    def test_home_lan_environment_overrides_composition(self, tmp_path, monkeypatch):
        _plugins_root(tmp_path)
        path = _composition(tmp_path, host={'home_lan_ip': '192.0.2.1'})
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        monkeypatch.setenv('CONSOLE_HOME_LAN_IP', '192.0.2.2')
        assert composition.host_policy()['home_lan_ip'] == '192.0.2.2'

    def test_empty_control_identity_does_not_restore_private_defaults(self, tmp_path, monkeypatch):
        import machine_config as mc
        import service_manager as sm

        _plugins_root(tmp_path)
        path = _composition(tmp_path, host={'local_machine': 'box'})
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        monkeypatch.setattr(mc, 'tailscale_ip', lambda machine: '192.0.2.10')
        assert mc.host_control_key() == ''
        assert mc.host_control_target() == ''
        with pytest.raises(ValueError, match='未配置'):
            sm._host_control_cmd(['status'])

    def test_alert_machines_are_copied_from_file(self, tmp_path, monkeypatch):
        _plugins_root(tmp_path)
        path = _composition(tmp_path, alert_machines={
            'box': {'display': 'BOX', 'load5_threshold': 1.5},
        })
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        assert composition.alert_machine_specs() == {
            'box': {'display': 'BOX', 'load5_threshold': 1.5},
        }

    def test_public_empty_alert_machines_yields_zero_rules(self, tmp_path, monkeypatch):
        _plugins_root(tmp_path)
        path = _composition(tmp_path, profile='public')
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        assert composition.alert_machine_specs() == {}
        assert ma.build_rules() == []

    def test_private_legacy_rules_equal_frozen_18(self, monkeypatch):
        legacy = composition.legacy_alert_machine_specs()
        rules = ma.build_rules()
        assert len(rules) == 18
        assert {r['machine'] for r in rules} == set(legacy)
        assert {r['duration_sec'] for r in rules if r['name'] == 'Status'} == {300}
        assert {r['duration_sec'] for r in rules if r['name'] != 'Status'} == {600}
        load5 = {r['machine']: r['threshold'] for r in rules if r['name'] == 'LoadAvg5'}
        assert load5 == {mid: spec['load5_threshold'] for mid, spec in legacy.items()}
        assert {r['threshold'] for r in rules if r['name'] == 'Disk'} == {85.0}
        assert ma.STATUS_OFFLINE_SEC == 300
        assert ma.SUSTAIN_SEC == 600
        assert ma.RECOVERY_WINDOW_SEC == 600
        assert ma.DEAD_MAN_THRESHOLD_SEC == 720
        assert ma.rule_fingerprint('nas', 'Disk') == 'machine:alert:Disk:nas'


class TestBootstrapEnvironment:
    def test_bootstrap_sets_paths_from_composition(self, tmp_path, monkeypatch):
        _plugins_root(tmp_path)
        path = _composition(tmp_path)
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        composition.bootstrap_environment()
        assert os.environ['CONSOLE_CONFIG_ROOT'] == str((tmp_path / 'config').resolve())
        assert os.environ['CONSOLE_DATA_ROOT'] == str((tmp_path / 'data').resolve())
        assert os.environ['CONSOLE_STATIC_ROOT'] == str((tmp_path / 'static').resolve())
        assert os.environ['OPS_BACKUP_DIR'] == str((tmp_path / 'backup').resolve())
        assert os.environ['CONSOLE_PLUGIN_ROOT'] == str((tmp_path / 'plugins').resolve())

    def test_static_environment_root_is_used_by_all_consumers(self, tmp_path, monkeypatch):
        _plugins_root(tmp_path)
        path = _composition(tmp_path)
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        override = tmp_path / 'other-static'
        monkeypatch.setenv('CONSOLE_STATIC_ROOT', str(override))
        composition.bootstrap_environment()
        assert composition.root_path('static') == override

    @pytest.mark.parametrize('shape', ['direct', 'ancestor', 'cancelled_ancestor'])
    def test_paths_data_override_rejects_symlinks(self, tmp_path, monkeypatch, shape):
        import paths

        real = tmp_path / 'real'
        real.mkdir()
        linked = tmp_path / 'linked'
        linked.symlink_to(real, target_is_directory=True)
        override = {
            'direct': linked,
            'ancestor': linked / 'data',
            'cancelled_ancestor': linked / '..' / 'data',
        }[shape]
        monkeypatch.setenv('CONSOLE_DATA_ROOT', str(override))
        with pytest.raises(composition.CompositionError, match='符号链接'):
            paths._data_root()

    def test_existing_dedicated_env_wins(self, tmp_path, monkeypatch):
        _plugins_root(tmp_path)
        path = _composition(tmp_path)
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        elsewhere = tmp_path / 'custom'
        monkeypatch.setenv('OPS_BACKUP_DIR', str(elsewhere))
        composition.bootstrap_environment()
        assert os.environ['OPS_BACKUP_DIR'] == str(elsewhere)

    def test_public_composition_boot_is_empty_not_private(self, tmp_path, monkeypatch):
        """公共组合真实 import：空台账、无家庭映射、0 规则、路径根未被创建。"""
        import machine_config as mc
        import monitoring_alert

        plugins = _plugins_root(tmp_path, ids=())
        (plugins / 'composition.json').write_text('{"plugins": []}', encoding='utf-8')
        path = _composition(tmp_path, profile='public')
        (tmp_path / 'config' / 'machines.yaml').write_text('[]\n', encoding='utf-8')
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        monkeypatch.setenv('CONSOLE_INSTANCE_PROFILE', 'public')

        composition.bootstrap_environment()

        mc.invalidate()
        assert mc.machine_ids() == []
        assert mc.home_machines() == set()
        assert mc.home_lan_ip() == ''
        assert mc.suggest_access('anything', 80) == {'home': '', 'tailscale': ''}
        assert monitoring_alert.build_rules() == []
        for name in ('data', 'static', 'backup'):
            assert not (tmp_path / name).exists(), name
        assert mc.machines_path() == str((tmp_path / 'config' / 'machines.yaml').resolve())


class TestStrictMachineLedger:
    """显式组合/public 下，合法 list 里的坏条目必须按 broken 失败。"""

    @pytest.mark.parametrize('body,match', [
        ('- 123\n', '不是对象'),
        ('- label: 无 id\n', '缺少字符串 id'),
        ('- id: 123\n', '缺少字符串 id'),
        ('- id: "bad id"\n', 'id 非法'),
        ('- id: nas\n  role: wat\n', 'role 非法'),
        ('- id: nas\n  capabilities: docker\n', 'capabilities 不是数组'),
        ('- id: nas\n  capabilities: [docker, rm-rf]\n', '非法 capability'),
        ('- id: nas\n- id: NAS\n', 'id 重复'),
    ])
    def test_explicit_composition_broken_entry_fails_closed(
        self, tmp_path, monkeypatch, body, match
    ):
        _plugins_root(tmp_path, ids=())
        path = _composition(tmp_path)
        machines = tmp_path / 'machines.yaml'
        machines.write_text(body, encoding='utf-8')
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        monkeypatch.setenv('CONSOLE_MACHINES_PATH', str(machines))
        import machine_config as mc
        mc.invalidate()
        with pytest.raises(composition.CompositionError, match=match):
            mc.machine_ids()

    @pytest.mark.parametrize('body,match', [
        ('- 123\n', '不是对象'),
        ('- label: 无 id\n', '缺少字符串 id'),
        ('- id: "bad id"\n', 'id 非法'),
        ('- id: nas\n  role: wat\n', 'role 非法'),
        ('- id: nas\n  capabilities: [docker, rm-rf]\n', '非法 capability'),
        ('- id: nas\n- id: NAS\n', 'id 重复'),
    ])
    def test_public_profile_broken_entry_fails_closed(
        self, tmp_path, monkeypatch, body, match
    ):
        machines = tmp_path / 'machines.yaml'
        machines.write_text(body, encoding='utf-8')
        monkeypatch.setenv('CONSOLE_INSTANCE_PROFILE', 'public')
        monkeypatch.setenv('CONSOLE_MACHINES_PATH', str(machines))
        import machine_config as mc
        mc.invalidate()
        with pytest.raises(composition.CompositionError, match=match):
            mc.machine_ids()

    def test_explicit_composition_empty_list_is_valid(self, tmp_path, monkeypatch):
        _plugins_root(tmp_path, ids=())
        path = _composition(tmp_path)
        machines = tmp_path / 'machines.yaml'
        machines.write_text('[]\n', encoding='utf-8')
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        monkeypatch.setenv('CONSOLE_MACHINES_PATH', str(machines))
        import machine_config as mc
        mc.invalidate()
        assert mc.machine_ids() == []

    def test_explicit_composition_valid_entries_still_load(self, tmp_path, monkeypatch):
        _plugins_root(tmp_path, ids=())
        path = _composition(tmp_path)
        machines = tmp_path / 'machines.yaml'
        machines.write_text(
            '- id: sample\n  role: server\n  tailscale: 100.64.0.1\n',
            encoding='utf-8',
        )
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        monkeypatch.setenv('CONSOLE_MACHINES_PATH', str(machines))
        import machine_config as mc
        mc.invalidate()
        assert mc.machine_ids() == ['sample']


class TestServiceCompositionEndpoints:
    def test_access_suggestion_uses_local_identity_and_preserves_remote(self, monkeypatch):
        import app

        seen = []
        monkeypatch.setattr(app.mc, 'local_machine', lambda: 'box')
        monkeypatch.setattr(
            app.mc, 'suggest_access',
            lambda machine, port, suffix: seen.append(machine) or {'home': '', 'tailscale': ''},
        )
        client = app.app.test_client()
        assert client.get('/api/service/suggest-access?port=8080').status_code == 200
        assert client.get('/api/service/suggest-access?machine=remote&port=8080').status_code == 200
        assert seen == ['box', 'remote']


class TestConfigPath:
    def test_dedicated_override_wins_over_composition(self, tmp_path, monkeypatch):
        _plugins_root(tmp_path)
        path = _composition(tmp_path)
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        monkeypatch.setenv('CONSOLE_SETTINGS_FILE', str(tmp_path / 'custom-settings.yaml'))
        assert composition.config_path('settings.yaml', 'CONSOLE_SETTINGS_FILE') == (
            tmp_path / 'custom-settings.yaml'
        ).resolve()

    def test_config_environment_root_applies_without_composition_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv('CONSOLE_CONFIG_ROOT', str(tmp_path))
        assert composition.config_path('assets.yaml') == tmp_path / 'assets.yaml'

    def test_composition_config_root_used_when_no_override(self, tmp_path, monkeypatch):
        _plugins_root(tmp_path)
        path = _composition(tmp_path)
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        assert composition.config_path('assets.yaml') == (tmp_path / 'config' / 'assets.yaml').resolve()
        assert not (tmp_path / 'config' / 'assets.yaml').exists()

    @pytest.mark.parametrize('name', ['../escape.yaml', '/etc/passwd', 'sub/dir.yaml', ''])
    def test_bad_file_names_are_rejected(self, name):
        with pytest.raises(composition.CompositionError):
            composition.config_path(name)


class TestExternalPluginRoot:
    def _tree(self, base, ids=('alpha',)):
        root = _plugins_root(base, ids=ids)
        for pid in ids:
            (root / pid / '__init__.py').write_text('', encoding='utf-8')
            (root / pid / 'backend').mkdir()
            (root / pid / 'backend' / '__init__.py').write_text(
                'VALUE = "{pkg}"\n'.format(pkg=pid), encoding='utf-8',
            )
        return root

    @pytest.mark.parametrize('dirname', ['ext-plugins', 'plugins'])
    def test_external_root_imports_only_from_that_root(self, tmp_path, dirname):
        """外置根不受同名包优先级影响，缺根级 __init__ 时也不取私人仓库的 plugins。"""
        bundle = tmp_path / 'bundle' / dirname
        bundle.mkdir(parents=True)
        previous = tmp_path / 'decoy'
        _plugins_root(previous, ids=('alpha',))
        (previous / 'plugins' / 'alpha' / '__init__.py').write_text('', encoding='utf-8')
        (previous / 'plugins' / 'alpha' / 'backend').mkdir()
        (previous / 'plugins' / 'alpha' / 'backend' / '__init__.py').write_text(
            'VALUE = "decoy"\n', encoding='utf-8',
        )
        (bundle / 'contract.json').write_text('{}', encoding='utf-8')
        (bundle / 'alpha').mkdir()
        (bundle / 'alpha' / 'manifest.json').write_text('{}', encoding='utf-8')
        (bundle / 'alpha' / '__init__.py').write_text('', encoding='utf-8')
        (bundle / 'alpha' / 'backend').mkdir()
        (bundle / 'alpha' / 'backend' / '__init__.py').write_text('VALUE = "real"\n', encoding='utf-8')
        (bundle / 'composition.json').write_text('{"plugins": ["alpha"]}', encoding='utf-8')

        code = textwrap.dedent(f'''
            import sys
            sys.path.insert(0, {str(BACKEND_DIR)!r})
            import plugin_imports
            module = plugin_imports.load_module("plugins.alpha.backend")
            print(module.VALUE)
        ''')
        proc = subprocess.run(
            [sys.executable, '-c', code],
            capture_output=True, text=True, timeout=30,
            env={
                'PATH': '/usr/bin:/bin',
                'PYTHONPATH': str(previous),
                'CONSOLE_PLUGIN_ROOT': str(bundle),
            },
            cwd=str(BACKEND_DIR),
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == 'real'

    @pytest.mark.parametrize('dirname', ['ext-plugins', 'plugins'])
    def test_existing_other_plugin_namespace_is_rejected(self, tmp_path, dirname):
        bundle = tmp_path / dirname
        bundle.mkdir()
        (bundle / 'contract.json').write_text('{}', encoding='utf-8')
        code = textwrap.dedent(f'''
            import sys, types
            sys.path.insert(0, {str(BACKEND_DIR)!r})
            import plugin_imports
            existing = types.ModuleType('plugins')
            existing.__path__ = [{str(tmp_path / 'decoy')!r}]
            sys.modules['plugins'] = existing
            try:
                plugin_imports.load_module('plugins.alpha')
            except plugin_imports.CompositionError:
                pass
            else:
                raise AssertionError('mixed plugin roots')
        ''')
        proc = subprocess.run(
            [sys.executable, '-c', code], capture_output=True, text=True, timeout=30,
            env={'PATH': os.environ.get('PATH', ''), 'CONSOLE_PLUGIN_ROOT': str(bundle)},
            cwd=str(BACKEND_DIR),
        )
        assert proc.returncode == 0, proc.stderr

    def test_plugin_composition_rejects_duplicate_json_keys(self, tmp_path, monkeypatch):
        root = self._tree(tmp_path)
        (root / 'composition.json').write_text(
            '{"plugins": ["alpha"], "plugins": []}', encoding='utf-8',
        )
        monkeypatch.setenv('CONSOLE_PLUGIN_ROOT', str(root))
        with pytest.raises(composition.CompositionError, match='重复键'):
            composition.plugin_sources(root)

    @pytest.mark.parametrize('spelling', ['direct', 'ancestor', 'cancelled_ancestor'])
    def test_plugin_root_rejects_symlinks_before_normalizing(self, tmp_path, monkeypatch, spelling):
        import plugin_imports

        root = self._tree(tmp_path)
        link = tmp_path / 'linked'
        link.symlink_to(root)
        candidate = {
            'direct': str(link),
            'ancestor': str(link / 'child'),
            'cancelled_ancestor': str(link) + '/../plugins',
        }[spelling]
        monkeypatch.setenv('CONSOLE_PLUGIN_ROOT', candidate)
        with pytest.raises(composition.CompositionError, match='符号链接'):
            plugin_imports.plugin_root()

    def test_file_root_is_used_without_app_bootstrap(self, tmp_path, monkeypatch):
        import plugin_imports

        root = self._tree(tmp_path)
        path = _composition(tmp_path)
        monkeypatch.setenv('CONSOLE_COMPOSITION_FILE', str(path))
        assert plugin_imports.plugin_root() == root

    def test_external_root_without_contract_is_rejected(self, tmp_path, monkeypatch):
        root = self._tree(tmp_path)
        (root / 'contract.json').unlink()
        monkeypatch.setenv('CONSOLE_PLUGIN_ROOT', str(root))
        with pytest.raises(composition.CompositionError, match='contract.json'):
            composition.plugin_sources  # noqa: B018 - 仅为文档化导入面
            import plugin_imports
            plugin_imports.plugin_root()