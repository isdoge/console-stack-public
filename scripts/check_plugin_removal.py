#!/usr/bin/env python3
"""Remove plugins only in isolated copies, then verify core startup and/or production build."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORE = shutil.ignore_patterns('node_modules', '__pycache__', '.pytest_cache', '.ruff_cache', 'data', 'static', 'dist', '*.pyc', '.env', '*.db', '*.db-*')


def run_case(removed, backend=True, frontend=True):
    with tempfile.TemporaryDirectory(prefix='console-plugin-removal-') as temporary:
        root = Path(temporary) / 'workspace'
        for relative in ('console/backend', 'kernel/backend', 'kernel/frontend', 'plugins', 'theme'):
            src = ROOT / relative
            if not src.is_dir():
                raise RuntimeError(f'missing required tree for isolation: {relative}')
            shutil.copytree(src, root / relative, ignore=IGNORE)
        for name, content in [('assets.yaml', '[]\n'), ('panels.yaml', '[]\n'), ('machines.yaml', '{}\n'), ('settings.yaml', '{}\n')]:
            (root / 'console' / name).write_text(content, encoding='utf-8')
        retired = Path(temporary) / 'removed'
        retired.mkdir()
        for pid in removed:
            shutil.move(str(root / 'plugins' / pid), str(retired / pid))
        # B7-1：组合清单是唯一来源，移出源码必须同批撤销登记，否则启动按未登记拒绝。
        sources = root / 'plugins' / 'composition.json'
        document = json.loads(sources.read_text(encoding='utf-8'))
        document['plugins'] = [pid for pid in document['plugins'] if pid not in removed]
        sources.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        # Do not inherit production DB, role, config, Pay or credential settings.
        env = {key: value for key, value in os.environ.items() if key in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL')}
        env.update(CONSOLE_STORAGE_ENGINE='sqlite', CONSOLE_INSTANCE_ROLE='test', CONSOLE_MAINTENANCE_MODE='normal', CONSOLE_AUTH_ENABLED='false', PYTHONDONTWRITEBYTECODE='1')
        if backend:
            code = '''import app
client = app.app.test_client()
assert client.get('/api/health').status_code == 200
assert client.get('/api/plugins').status_code == 200
ids = {row['id'] for row in client.get('/api/plugins').json}
import json
assert not ids.intersection(json.loads(__import__('sys').argv[1]))
assert client.get('/api/machines').status_code == 200
assert client.get('/api/dashboard-layout').status_code == 200
assert client.get('/api/ops-status').status_code == 200
'''
            result = subprocess.run([sys.executable, '-c', code, json.dumps(removed)], cwd=root / 'console/backend', env=env, capture_output=True, text=True, timeout=90)
            if result.returncode:
                raise RuntimeError(f'Backend removal {removed}:\n{result.stdout}\n{result.stderr}')
        if frontend:
            modules = ROOT / 'kernel/frontend/node_modules'
            if not modules.is_dir():
                raise RuntimeError('Run npm ci in kernel/frontend first')
            (root / 'kernel/frontend/node_modules').symlink_to(modules, target_is_directory=True)
            result = subprocess.run(['npm', 'run', 'build'], cwd=root / 'kernel/frontend', env=env, capture_output=True, text=True, timeout=180)
            if result.returncode:
                raise RuntimeError(f'Frontend removal {removed}:\n{result.stdout}\n{result.stderr}')
        print('PASS removed=' + ','.join(removed) + f' backend={backend} frontend={frontend}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--backend-only', action='store_true')
    mode.add_argument('--frontend-only', action='store_true')
    parser.add_argument('--plugin', help='One installed plugin only; default checks each and all')
    args = parser.parse_args()
    ids = sorted(p.parent.name for p in (ROOT / 'plugins').glob('*/manifest.json'))
    if args.plugin and args.plugin not in ids:
        parser.error('Unknown installed plugin')
    cases = [[args.plugin]] if args.plugin else [[pid] for pid in ids] + [ids]
    for case in cases:
        run_case(case, backend=not args.frontend_only, frontend=not args.backend_only)


if __name__ == '__main__':
    main()
