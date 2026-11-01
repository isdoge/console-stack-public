"""Cross-plugin contract regressions: immutable capability surface and failure containment."""
from dataclasses import FrozenInstanceError, replace

import pytest

import app
import collect_guard
import plugin_contributions as contributions
import plugin_runtime as runtime
from plugin_manifest import normalize


@pytest.fixture
def runtime_state(monkeypatch, tmp_path):
    settings = tmp_path / 'settings.yaml'
    settings.write_text('{}', encoding='utf-8')
    monkeypatch.setenv('CONSOLE_SETTINGS_FILE', str(settings))
    monkeypatch.setenv('CONSOLE_INSTANCE_ROLE', 'authority')
    monkeypatch.setenv('CONSOLE_MAINTENANCE_MODE', 'normal')
    monkeypatch.setattr(runtime, '_initializers', [])
    monkeypatch.setattr(runtime, '_initialized', set())
    monkeypatch.setattr(runtime, '_failures', {})
    monkeypatch.setattr(contributions, '_registry', {})
    monkeypatch.setattr(collect_guard, 'collection_allowed', lambda: True)
    return app.app.test_client()


def test_context_does_not_expose_or_allow_replacing_core():
    context = replace(app.plugin_context, plugin_id='feed')
    assert context.plugin_id == 'feed' and not hasattr(context, '_log_op')
    assert not hasattr(context, 'app') and not hasattr(app, 'ledger')
    with pytest.raises(FrozenInstanceError):
        context.audit = lambda *args: None


def test_initializer_failure_isolated_and_explicit_retry(runtime_state):
    calls = []
    def broken():
        calls.append('bad')
        raise ValueError('private secret text')
    runtime.add_initializer('feed', broken)
    runtime.add_initializer('renewals', lambda: calls.append('good'))
    runtime.initialize()
    assert calls == ['bad', 'good']
    response = runtime_state.get('/api/feed')
    assert response.status_code == 503 and response.json['code'] == 'plugin_unavailable'
    assert 'secret' not in response.get_data(as_text=True)
    assert runtime_state.get('/api/health').status_code == 200
    assert not runtime.run_job(('feed', 'refresh', 5, lambda: pytest.fail('failed plugin ran')))
    runtime.initialize()
    assert calls == ['bad', 'good']
    runtime._initializers = [('feed', lambda: calls.append('retry'))]
    response = runtime_state.put('/api/plugins/feed', json={'enabled': True})
    assert response.status_code == 200 and response.json['available'] is True
    assert calls[-1] == 'retry'


def test_extension_slots_respect_disabled_and_isolate_failure(runtime_state):
    def broken(**kwargs):
        raise RuntimeError('sensitive')
    contributions.register('feed', 'attention', 'bad', broken)
    contributions.register('renewals', 'attention', 'good', lambda **kwargs: [{'title': 'ok'}])
    assert contributions.attention_items(None) == [{'title': 'ok'}]
    runtime_state.put('/api/plugins/renewals', json={'enabled': False})
    assert contributions.attention_items(None) == []
    with pytest.raises(ValueError):
        contributions.register('feed', 'attention', 'bad', broken)


def test_manifest_default_values_and_flag_types():
    manifest = {k: v for k, v in runtime.BY_ID['feed'].items() if k not in ('order', 'client_mode', 'persistent', 'sidebar', 'immersive')}
    parsed = normalize(manifest, 'feed')
    assert parsed['order'] == 100 and parsed['client_mode'] == 'authority'
    assert parsed['persistent'] is False
    for invalid in (None, 'false', 0, []):
        with pytest.raises(ValueError):
            normalize({**manifest, 'sidebar': invalid}, 'feed')
        with pytest.raises(ValueError):
            normalize({**manifest, 'navigation': invalid}, 'feed')


def test_enable_initialization_serialized_without_blocking_other_plugins(runtime_state):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()
    calls = []
    runtime.update('feed', {'enabled': False})

    def slow_init():
        calls.append('init')
        entered.set()
        assert release.wait(5)

    runtime.add_initializer('feed', slow_init)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(runtime.update, 'feed', {'enabled': True})
        assert entered.wait(5)
        try:
            second = pool.submit(runtime.update, 'feed', {'enabled': True})
            other = pool.submit(runtime.runtime_status, 'renewals')
            assert other.result(timeout=2)['available'] is True
            assert not first.done() and not second.done()
        finally:
            release.set()
        assert first.result(timeout=5)['available'] is True
        assert second.result(timeout=5)['available'] is True
    assert calls == ['init']


def test_client_rejects_authority_initialization_failure(runtime_state, monkeypatch):
    monkeypatch.setenv('CONSOLE_INSTANCE_ROLE', 'client')
    monkeypatch.setattr(runtime, '_authority_catalog', lambda: [{'id': 'ledger', 'enabled': True, 'available': False}])
    response = runtime_state.get('/api/ledger/tx')
    assert response.status_code == 503 and response.json['code'] == 'plugin_unavailable'


def test_initializer_skips_collection_forbidden_modes(runtime_state, monkeypatch):
    monkeypatch.setattr(collect_guard, 'collection_allowed', lambda: False)
    runtime.add_initializer('feed', lambda: pytest.fail('forbidden initializer ran'))
    runtime.initialize()
    assert runtime._initialized == set() and runtime._failures == {}


def test_invalid_job_registration_fails_before_start(runtime_state, monkeypatch):
    monkeypatch.setattr(runtime, '_jobs', [])
    for invalid in (0, -1, True, float('nan'), float('inf'), '5'):
        with pytest.raises(ValueError):
            runtime.add_job('feed', 'refresh', invalid, lambda: None)
    runtime.add_job('feed', 'refresh', 5, lambda: None)
    with pytest.raises(ValueError):
        runtime.add_job('feed', 'refresh', 5, lambda: None)


def test_config_contribution_failure_keeps_settings_available(runtime_state):
    import effective_config

    def broken():
        raise RuntimeError('private details')

    contributions.register('panels', 'config_defaults', 'panels', broken)
    groups = effective_config.build_spec()
    assert groups['runtime']['entries']
    assert next(e for e in groups['ops']['entries'] if e['key'] == 'HINDSIGHT_PROXY_PORT')['value'] == '未启用或未安装'
