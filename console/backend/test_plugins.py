"""插件生命周期、写入隔离与新增插件发现的实际 HTTP 回归。"""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml

import app as core
import collect_guard
import plugin_runtime as runtime
import settings_store
from plugin_imports import load_module  # noqa: E402

plugin_ledger = load_module("plugins.ledger.backend.ledger")


panel_routes = load_module("plugins.panels.backend.routes")



@pytest.fixture
def plugin_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "test")
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "sqlite")
    monkeypatch.setenv("CONSOLE_MAINTENANCE_MODE", "normal")
    settings = tmp_path / 'settings.yaml'
    settings.write_text('CONSOLE_PROBE_INTERVAL: 600\n', encoding='utf-8')
    monkeypatch.setenv('CONSOLE_SETTINGS_FILE', str(settings))
    monkeypatch.setenv('CONSOLE_PROBE_INTERVAL', '600')
    monkeypatch.setattr(core, 'DB_PATH', str(tmp_path / 'test.db'))
    monkeypatch.setattr(core, 'PANELS_PATH', str(tmp_path / 'panels.yaml'))
    core.init_db()
    return core.app.test_client(), settings


def test_catalog_and_hide_keep_api_active(plugin_client):
    client, path = plugin_client
    rows = client.get('/api/plugins').get_json()
    assert {row['id'] for row in rows} == {'feed', 'ledger', 'renewals', 'audit', 'panels', 'personal_workbench', 'backup_evidence'}
    response = client.put('/api/plugins/panels', json={'visible': False})
    assert response.status_code == 200
    assert response.get_json()['enabled'] is True
    assert client.get('/api/panels').status_code == 200
    assert yaml.safe_load(path.read_text())['CONSOLE_PROBE_INTERVAL'] == 600
    assert next(row for row in client.get('/api/plugins').get_json() if row['id'] == 'panels')['visible'] is False


@pytest.mark.parametrize('pid,path', [('feed', '/api/feed'), ('ledger', '/api/ledger/tx'),
                                      ('renewals', '/api/renewals'), ('audit', '/api/events'),
                                      ('panels', '/panel-proxy/1/login'),
                                      ('personal_workbench', '/api/personal-workbench/S5/api/index'),
                                      ('backup_evidence', '/api/backup-evidence')])
def test_disabled_routes_never_call_business_or_proxy(plugin_client, monkeypatch, pid, path):
    client, _ = plugin_client
    assert client.put(f'/api/plugins/{pid}', json={'enabled': False}).status_code == 200
    monkeypatch.setattr(core, '_proxy_authority', lambda *a, **kw: pytest.fail('Beszel proxy reached'))
    for method in ('get', 'post', 'put'):
        response = getattr(client, method)(path)
        assert response.status_code == 503
        assert response.get_json()['code'] == 'plugin_disabled'
        assert response.headers['Cache-Control'] == 'no-store'
    assert client.get('/api/health').status_code == 200


def test_disable_and_reenable_preserves_panel_data(plugin_client):
    client, _ = plugin_client
    panel = client.post('/api/panels', json={'name': '保存的面板', 'url': 'https://example.test'}).get_json()['panel']
    client.put('/api/plugins/panels', json={'enabled': False})
    assert client.get('/api/panels').status_code == 503
    client.put('/api/plugins/panels', json={'enabled': True})
    assert client.get('/api/panels').get_json() == [panel]


@pytest.mark.parametrize('payload', [{'enabled': 'false'}, {'enabled': 0}, {'path': '/tmp/x'}, {}, [], None])
def test_invalid_state_does_not_write(plugin_client, payload):
    client, path = plugin_client
    before = path.read_bytes()
    assert client.put('/api/plugins/feed', json=payload).status_code == 400
    assert path.read_bytes() == before


def test_workbench_maintenance_blocks_writes_before_service(plugin_client, monkeypatch):
    client, _ = plugin_client
    monkeypatch.setenv('CONSOLE_MAINTENANCE_MODE', 'read_only')
    response = client.post('/api/personal-workbench/S1/api/workspace', json={})
    assert response.status_code == 503
    assert '维护模式' in response.get_json()['error']


def test_unknown_and_readonly(plugin_client, monkeypatch):
    client, path = plugin_client
    assert client.put('/api/plugins/missing', json={'enabled': True}).status_code == 404
    before = path.read_bytes()
    monkeypatch.setenv('CONSOLE_MAINTENANCE_MODE', 'read_only')
    assert client.put('/api/plugins/feed', json={'enabled': False}).status_code == 503
    assert path.read_bytes() == before


def test_concurrent_state_and_settings_do_not_clobber(plugin_client):
    _, path = plugin_client
    def update(index):
        if index % 3 == 0:
            settings_store.update_settings({'CONSOLE_PROBE_INTERVAL': 600 + index})
        else:
            runtime.update('feed' if index % 3 == 1 else 'ledger', {'visible': False})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(update, range(30)))
    values = yaml.safe_load(path.read_text())
    assert values['PLUGINS']['feed']['visible'] is False
    assert values['PLUGINS']['ledger']['visible'] is False
    assert int(values['CONSOLE_PROBE_INTERVAL']) >= 600


def test_corrupt_settings_fail_closed_without_overwrite(plugin_client):
    client, path = plugin_client
    path.write_text('PLUGINS: invalid\n', encoding='utf-8')
    assert client.get('/api/plugins').status_code == 503
    assert client.get('/api/ledger/tx').status_code == 503
    assert client.put('/api/plugins/ledger', json={'enabled': True}).status_code == 400
    assert path.read_text() == 'PLUGINS: invalid\n'


def test_disabled_job_stops_and_reenable_resumes(plugin_client, monkeypatch):
    client, _ = plugin_client
    monkeypatch.setattr(collect_guard, 'collection_allowed', lambda: True)
    calls = []
    job = ('feed', 'test', 900, lambda: calls.append('run'))
    assert runtime.run_job(job)
    client.put('/api/plugins/feed', json={'enabled': False})
    assert not runtime.run_job(job)
    client.put('/api/plugins/feed', json={'enabled': True, 'visible': False})
    assert runtime.run_job(job)
    assert calls == ['run', 'run']


def test_new_plugin_discovered_without_editing_core(tmp_path):
    folder = tmp_path / 'example'
    folder.mkdir()
    manifest = {**runtime.BY_ID['feed'], 'id': 'example', 'name': '示例',
                'routes': ['/example'], 'api_prefixes': ['/api/example']}
    (folder / 'manifest.json').write_text(json.dumps(manifest))
    assert runtime.discover(tmp_path)[0]['id'] == manifest['id']
    manifest['api_prefixes'] = ['/api/machines']
    (folder / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='框架入口'):
        runtime.discover(tmp_path)


def test_client_forwards_plugin_state_to_authority(plugin_client, monkeypatch):
    client, path = plugin_client
    before = path.read_bytes()
    monkeypatch.setenv('CONSOLE_INSTANCE_ROLE', 'client')
    monkeypatch.setenv('CONSOLE_AUTHORITY_ORIGIN', 'http://authority.test:8081')
    captured = []
    def proxy(origin, path):
        captured.append(path)
        return {'enabled': False}
    monkeypatch.setattr(core, '_proxy_authority', proxy)
    assert client.put('/api/plugins/feed', json={'enabled': False}).status_code == 200
    assert captured == ['api/plugins/feed']
    assert path.read_bytes() == before


def test_service_lifecycle_honors_role_and_enabled(plugin_client, monkeypatch):
    from unittest.mock import Mock

    client, _ = plugin_client
    process = Mock()
    process.poll.return_value = None
    start = Mock(return_value=process)
    service = {'id': 'panels', 'start': start, 'process': None, 'retry_at': 0}
    monkeypatch.setenv('CONSOLE_INSTANCE_ROLE', 'client')
    runtime.sync_service(service)
    start.assert_not_called()
    monkeypatch.setenv('CONSOLE_INSTANCE_ROLE', 'authority')
    client.put('/api/plugins/panels', json={'enabled': False})
    runtime.sync_service(service)
    start.assert_not_called()
    client.put('/api/plugins/panels', json={'enabled': True})
    runtime.sync_service(service)
    start.assert_called_once()
    client.put('/api/plugins/panels', json={'visible': False})
    runtime.sync_service(service)
    process.terminate.assert_not_called()
    client.put('/api/plugins/panels', json={'enabled': False})
    runtime.sync_service(service)
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=5)
    assert service['process'] is None
    client.put('/api/plugins/panels', json={'enabled': True})
    runtime.sync_service(service)
    assert start.call_count == 2


def test_service_start_failure_retries_with_backoff(plugin_client, monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setenv('CONSOLE_INSTANCE_ROLE', 'authority')
    start = Mock(side_effect=OSError('address already in use'))
    service = {'id': 'panels', 'start': start, 'process': None, 'retry_at': 0}
    runtime.sync_service(service)
    runtime.sync_service(service)
    start.assert_called_once()


@pytest.mark.parametrize('enabled,expected', [(True, 200), (False, 503)])
def test_client_shared_api_checks_authority_state(plugin_client, monkeypatch, enabled, expected):
    client, _ = plugin_client
    monkeypatch.setenv('CONSOLE_INSTANCE_ROLE', 'client')
    monkeypatch.setenv('CONSOLE_AUTHORITY_ORIGIN', 'http://authority.test:8081')
    calls = []
    def authority(origin, path):
        calls.append(path)
        return core.jsonify([{'id': 'ledger', 'enabled': enabled}])
    monkeypatch.setattr(core, '_request_authority', authority)
    monkeypatch.setattr(plugin_ledger, 'transactions', lambda *args, **kwargs: {'items': []})
    response = client.get('/api/ledger/tx')
    assert response.status_code == expected
    assert calls == ['/api/plugins']
    if not enabled:
        assert response.get_json()['code'] == 'plugin_disabled'


def test_client_unknown_authority_state_fails_closed(plugin_client, monkeypatch):
    client, _ = plugin_client
    monkeypatch.setenv('CONSOLE_INSTANCE_ROLE', 'client')
    monkeypatch.setenv('CONSOLE_AUTHORITY_ORIGIN', 'http://authority.test:8081')
    monkeypatch.setattr(core, '_request_authority', lambda *args: (core.jsonify({'error': 'offline'}), 503))
    monkeypatch.setattr(plugin_ledger, 'manual', lambda *a, **kw: pytest.fail('business write reached'))
    assert client.post('/api/ledger/manual', json={}).status_code == 503


def test_panel_listener_releases_port_and_parent_pipe(plugin_client, monkeypatch):
    import socket
    import time

    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv('HINDSIGHT_PROXY_PORT', str(port))
    process = panel_routes._bg_hindsight_proxy(core.plugin_context)
    try:
        deadline = time.monotonic() + 10
        while True:
            with socket.socket() as sock:
                if sock.connect_ex(('127.0.0.1', port)) == 0:
                    break
            assert time.monotonic() < deadline and process.poll() is None
            time.sleep(0.05)
        # 父进程死亡同样会关闭这个管道；监听器必须自行退出而非成为孤儿。
        process.stdin.close()
        assert process.wait(timeout=5) == 0
        with socket.socket() as sock:
            assert sock.connect_ex(('127.0.0.1', port)) != 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_client_panel_proxy_resolves_authority_not_stale_local(plugin_client, monkeypatch):
    client, _ = plugin_client
    monkeypatch.setenv('CONSOLE_INSTANCE_ROLE', 'client')
    monkeypatch.setattr(panel_routes, '_load_panels', lambda context: pytest.fail('stale local panel config reached'))
    monkeypatch.setattr(core, '_authority_config_rows', lambda path: [{'id': 7, 'url': 'https://authority-panel.test'}])
    with core.app.test_request_context('/panel-proxy/7/'):
        assert panel_routes._panel_proxy_origin(core.plugin_context, 7) == 'https://authority-panel.test'
