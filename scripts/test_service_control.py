"""Forced SSH command only accepts the registered NAS process allowlist."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('service_control', Path(__file__).resolve().parents[1] / 'tools' / 'service_control.py')
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
SERVICES = [{'name': 'hermes', 'type': 'process', 'machine': 'nas',
             'systemd_unit': 'hermes-web-ui.service', 'systemd_scope': 'user',
             'actions': ['start', 'stop', 'logs']}]


def test_forced_command_uses_configured_unit_and_user_scope():
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='ok', stderr='')
    assert control.execute('stop hermes', SERVICES, run) == (0, 'ok')
    assert calls[0][0] == ['systemctl', '--user', 'stop', '--', 'hermes-web-ui.service']
    assert calls[0][1]['env']['XDG_RUNTIME_DIR'] == '/run/user/0'


@pytest.mark.parametrize('command', ['restart hermes', 'stop ssh', 'bash', 'stop hermes; id', 'logs hermes extra'])
def test_unregistered_command_never_executes(command):
    with pytest.raises(ValueError):
        control.execute(command, SERVICES, lambda *a, **k: pytest.fail('command executed'))


def test_status_is_grouped_by_scope_and_projects_service_names():
    def run(argv, **kwargs):
        assert argv == ['systemctl', '--user', 'show', '--property=Id,ActiveState', '--', 'hermes-web-ui.service']
        return SimpleNamespace(returncode=0, stdout='Id=hermes-web-ui.service\nActiveState=active\n', stderr='')
    code, output = control.execute('status', SERVICES, run)
    assert code == 0
    assert json.loads(output) == {'hermes': 'active'}


def test_user_service_logs_select_user_unit_from_host_journal():
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='log line', stderr='')
    assert control.execute('logs hermes', SERVICES, run) == (0, 'log line')
    assert calls == [['journalctl', '--user-unit', 'hermes-web-ui.service', '--no-pager', '-n', '200']]
