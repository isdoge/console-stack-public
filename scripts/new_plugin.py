#!/usr/bin/env python3
"""Create a minimal trusted-source plugin without modifying framework files."""
import argparse
import json
import re
from pathlib import Path

from check_plugins import ROOT


def _register(parent, pid):
    """新插件必须进组合来源清单，否则后端/前端与检查器都会按未登记拒绝（B7-1）。"""
    path = parent / 'composition.json'
    document = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {'plugins': []}
    document['plugins'] = sorted(set(document['plugins']) | {pid})
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def create(root, pid, name, description):
    root = Path(root)
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,47}', pid):
        raise ValueError('ID 必须为小写字母开头的字母/数字/下划线，最长48字符')
    if not name.strip() or not description.strip():
        raise ValueError('名称和简介不能为空')
    parent = root / 'plugins'
    if parent.is_symlink():
        raise ValueError('插件根目录不能为符号链接')
    target = parent / pid
    if target.exists() or target.is_symlink():
        raise FileExistsError('插件目录已存在，拒绝覆盖')
    manifest = dict(id=pid, name=name, description=description, version='1.0.0', api_version=1,
                    routes=['/'+pid], api_prefixes=['/api/'+pid],
                    owns=dict(tables=[], settings=[], files=[], tasks=[]))
    # Validate conflicts before creating anything.
    from plugin_manifest import discover, matches, normalize
    contract = json.loads((parent / 'contract.json').read_text(encoding='utf-8'))
    normalize(manifest, pid, contract)
    for current in discover(parent, contract):
        for old in current['routes'] + current['api_prefixes']:
            for new in manifest['routes'] + manifest['api_prefixes']:
                if matches(new, old.split('/:')[0]) or matches(old.split('/:')[0], new):
                    raise ValueError('路径与已有插件冲突')
    target.mkdir()
    (target / 'backend').mkdir()
    (target / 'frontend').mkdir()
    (target / '__init__.py').write_text('', encoding='utf-8')
    _register(parent, pid)
    (target / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    (target / 'backend/__init__.py').write_text(f'''from flask import jsonify


def register(app, context):
    @app.get('/api/{pid}')
    def {pid}_data():
        """Return plugin identity."""
        return jsonify({{'plugin': context.plugin_id, 'message': 'Ready'}})
''', encoding='utf-8')
    (target / f'backend/test_{pid}.py').write_text(f'''from types import SimpleNamespace

from flask import Flask

from . import register


def test_plugin_response():
    app = Flask(__name__)
    register(app, SimpleNamespace(plugin_id='{pid}'))
    response = app.test_client().get('/api/{pid}')
    assert response.status_code == 200 and response.json['plugin'] == '{pid}'
''', encoding='utf-8')
    # json.dumps makes the display name a safe JS string; no JSX/HTML interpolation.
    (target / 'frontend/index.tsx').write_text(f'''import {{ useEffect, useState }} from 'react';
import type {{ PluginRouteProps }} from '@/lib/plugins/registry';

export default function PluginPage({{ refreshKey }}: PluginRouteProps) {{
  const [message, setMessage] = useState('读取中…');
  useEffect(() => {{
    const controller = new AbortController();
    fetch('/api/{pid}', {{ signal: controller.signal }})
      .then(async (response) => {{
        const body = await response.json();
        if (!response.ok) throw new Error(body.error || '读取失败');
        return body;
      }})
      .then((body) => setMessage(body.message))
      .catch(() => {{ if (!controller.signal.aborted) setMessage('读取失败，请刷新重试'); }});
    return () => controller.abort();
  }}, [refreshKey]);
  return <section><h2>{{{json.dumps(name, ensure_ascii=False)}}}</h2><p>{{message}}</p></section>;
}}
''', encoding='utf-8')
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('id')
    parser.add_argument('--name', required=True)
    parser.add_argument('--description', required=True)
    parser.add_argument('--root', type=Path, default=ROOT, help='Isolated test checkout or repository root')
    args = parser.parse_args()
    try:
        target = create(args.root, args.id, args.name, args.description)
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc)+'\n')
    print(f'Created {target}; update plugins/README.md, then run scripts/check_plugins.py')


if __name__ == '__main__':
    main()
