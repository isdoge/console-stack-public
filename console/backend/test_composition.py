# -*- coding: utf-8 -*-
"""B7-1 组合边界：实例档案、插件来源清单与空台账/后台写闸门的定向回归。

这些用例只测**边界判定**，不启动整个应用：组合错误必须在导入/启动期 fail
closed，而不是等第一个请求才暴露。
"""
import json
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import composition  # noqa: E402
import machine_config as mc  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_machine_registry():
    mc.invalidate()
    yield
    mc.invalidate()


def _root(tmp_path, ids=('alpha', 'beta')):
    root = tmp_path / 'plugins'
    root.mkdir()
    (root / 'contract.json').write_text('{}', encoding='utf-8')
    for pid in ids:
        (root / pid).mkdir()
        (root / pid / 'manifest.json').write_text('{}', encoding='utf-8')
    (root / 'composition.json').write_text(json.dumps({'plugins': list(ids)}), encoding='utf-8')
    return root


# ---------------------------------------------------------------------------
# 实例档案
# ---------------------------------------------------------------------------

class TestInstanceProfile:
    def test_default_profile_is_private(self, monkeypatch):
        monkeypatch.delenv('CONSOLE_INSTANCE_PROFILE', raising=False)
        assert composition.instance_profile() == composition.PROFILE_PRIVATE
        assert composition.private_machine_defaults() is True

    def test_public_profile_never_uses_private_defaults(self, monkeypatch):
        monkeypatch.setenv('CONSOLE_INSTANCE_PROFILE', 'public')
        assert composition.instance_profile() == composition.PROFILE_PUBLIC
        assert composition.private_machine_defaults() is False

    @pytest.mark.parametrize('value', ['prod', 'authority', '0', 'public-private'])
    def test_invalid_profile_fails_closed(self, monkeypatch, value):
        monkeypatch.setenv('CONSOLE_INSTANCE_PROFILE', value)
        with pytest.raises(composition.CompositionError, match='取值无效'):
            composition.instance_profile()

    def test_profile_is_case_and_space_insensitive(self, monkeypatch):
        monkeypatch.setenv('CONSOLE_INSTANCE_PROFILE', ' Public ')
        assert composition.instance_profile() == composition.PROFILE_PUBLIC

    def test_empty_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv('CONSOLE_INSTANCE_PROFILE', '  ')
        assert composition.instance_profile() == composition.PROFILE_PRIVATE


# ---------------------------------------------------------------------------
# 插件来源清单
# ---------------------------------------------------------------------------

class TestPluginSources:
    def test_matching_manifest_and_disk_pass(self, tmp_path):
        root = _root(tmp_path)
        assert composition.plugin_sources(root) == ('alpha', 'beta')

    def test_repository_sources_match_installed_manifests(self):
        import plugin_imports
        from plugin_manifest import discover

        declared = composition.plugin_sources(plugin_imports.plugin_root())
        assert list(declared) == sorted(row['id'] for row in discover())
        assert composition.require_plugin_sources(plugin_imports.plugin_root(), discover()) == declared

    def test_missing_sources_file_fails_closed(self, tmp_path):
        root = _root(tmp_path)
        (root / 'composition.json').unlink()
        with pytest.raises(composition.CompositionError, match='缺失'):
            composition.plugin_sources(root)

    def test_symlinked_sources_file_fails_closed(self, tmp_path):
        root = _root(tmp_path)
        real = tmp_path / 'sources.json'
        real.write_text((root / 'composition.json').read_text(), encoding='utf-8')
        (root / 'composition.json').unlink()
        (root / 'composition.json').symlink_to(real)
        with pytest.raises(composition.CompositionError, match='普通文件'):
            composition.plugin_sources(root)

    @pytest.mark.parametrize('body', [
        '{}',                                        # 顶层没有 plugins
        '{"plugins": ["alpha"], "extra": 1}',        # 多余键
        '{"plugins": "alpha"}',                      # 不是数组
        '{"plugins": ["alpha", "alpha"]}',           # 重复
        '{"plugins": ["alpha", "../beta"]}',         # 越界路径
        '{"plugins": ["/etc/alpha"]}',               # 绝对路径
        '{"plugins": ["Alpha"]}',                    # 非法 id
        'not json',                                  # 不是 JSON
    ])
    def test_invalid_sources_fail_closed(self, tmp_path, body):
        root = _root(tmp_path, ids=('alpha',))
        (root / 'composition.json').write_text(body, encoding='utf-8')
        with pytest.raises(composition.CompositionError):
            composition.plugin_sources(root)

    def test_empty_list_is_an_explicit_zero_plugin_composition(self, tmp_path):
        """零插件是合法组合：插件根没有已安装插件时整棵树通过，不是「清单坏了」。"""
        root = _root(tmp_path, ids=())
        (root / 'composition.json').write_text('{"plugins": []}', encoding='utf-8')
        assert composition.plugin_sources(root) == ()
        assert composition.require_plugin_sources(root, []) == ()

    def test_declared_but_absent_plugin_fails_closed(self, tmp_path):
        root = _root(tmp_path, ids=('alpha',))
        (root / 'composition.json').write_text(
            json.dumps({'plugins': ['alpha', 'ghost']}), encoding='utf-8'
        )
        with pytest.raises(composition.CompositionError, match='不存在'):
            composition.plugin_sources(root)

    def test_unlisted_plugin_on_disk_fails_closed(self, tmp_path):
        root = _root(tmp_path)
        (root / 'gamma').mkdir()
        (root / 'gamma' / 'manifest.json').write_text('{}', encoding='utf-8')
        with pytest.raises(composition.CompositionError, match='未在组合清单登记'):
            composition.plugin_sources(root)

    def test_symlinked_plugin_directory_fails_closed(self, tmp_path):
        root = _root(tmp_path, ids=('alpha',))
        (root / 'composition.json').write_text(
            json.dumps({'plugins': ['alpha', 'beta']}), encoding='utf-8'
        )
        (root / 'beta').symlink_to(tmp_path / 'outside')
        with pytest.raises(composition.CompositionError, match='符号链接'):
            composition.plugin_sources(root)

    def test_require_plugin_sources_rejects_drift_from_discover(self, tmp_path):
        root = _root(tmp_path)
        with pytest.raises(composition.CompositionError, match='不一致'):
            composition.require_plugin_sources(root, [{'id': 'alpha'}])


# ---------------------------------------------------------------------------
# 机器台账：公共空台账 vs 私人默认
# ---------------------------------------------------------------------------

class TestMachineDefaults:
    def test_private_profile_keeps_hardcoded_fallback(self, tmp_path, monkeypatch):
        monkeypatch.delenv('CONSOLE_INSTANCE_PROFILE', raising=False)
        monkeypatch.setenv('CONSOLE_MACHINES_PATH', str(tmp_path / 'nope.yaml'))
        assert mc.machine_ids() == ['nas', 'txy', 'aly']

    def test_public_profile_missing_ledger_fails_instead_of_empty_success(self, tmp_path, monkeypatch):
        """缺台账是配置错误，不能返回 [] 冒充“合法空台账”（B7-1 P2）。"""
        monkeypatch.setenv('CONSOLE_INSTANCE_PROFILE', 'public')
        monkeypatch.setenv('CONSOLE_MACHINES_PATH', str(tmp_path / 'nope.yaml'))
        with pytest.raises(composition.CompositionError, match='不存在'):
            mc.machine_ids()
        with pytest.raises(composition.CompositionError):
            mc.get_machine('nas')

    def test_public_profile_unusable_ledger_fails_instead_of_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setenv('CONSOLE_INSTANCE_PROFILE', 'public')
        path = tmp_path / 'machines.yaml'
        path.write_text('{}', encoding='utf-8')
        monkeypatch.setenv('CONSOLE_MACHINES_PATH', str(path))
        with pytest.raises(composition.CompositionError, match='不可解析'):
            mc.machine_ids()

    def test_public_profile_still_reads_real_ledger(self, tmp_path, monkeypatch):
        monkeypatch.setenv('CONSOLE_INSTANCE_PROFILE', 'public')
        path = tmp_path / 'machines.yaml'
        path.write_text('- id: sample\n  role: server\n  tailscale: 100.64.0.1\n', encoding='utf-8')
        monkeypatch.setenv('CONSOLE_MACHINES_PATH', str(path))
        assert mc.machine_ids() == ['sample']

    def test_public_profile_empty_list_is_a_valid_empty_ledger(self, tmp_path, monkeypatch):
        monkeypatch.setenv('CONSOLE_INSTANCE_PROFILE', 'public')
        path = tmp_path / 'machines.yaml'
        path.write_text('[]\n', encoding='utf-8')
        monkeypatch.setenv('CONSOLE_MACHINES_PATH', str(path))
        assert mc.machine_ids() == []

    def test_public_profile_never_leaks_private_hosts(self, tmp_path, monkeypatch):
        monkeypatch.setenv('CONSOLE_INSTANCE_PROFILE', 'public')
        path = tmp_path / 'machines.yaml'
        path.write_text('[]\n', encoding='utf-8')
        monkeypatch.setenv('CONSOLE_MACHINES_PATH', str(path))
        assert mc.machines_for_api() == []
        assert mc.ssh_targets() == {}
