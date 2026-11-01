"""Pure manifest validation shared by runtime and offline plugin tooling (stdlib only)."""
import json
import re
from pathlib import Path

from plugin_imports import plugin_root

CONTRACT = json.loads((plugin_root() / 'contract.json').read_text(encoding='utf-8'))
API_VERSION = CONTRACT['api_version']
_ID = re.compile(r'^[a-z][a-z0-9_]{0,47}$')
OWNER_KEYS = ('tables', 'settings', 'files', 'tasks')


def matches(path, prefix):
    return path == prefix or path.startswith(prefix.rstrip('/') + '/')


def normalize(data, directory_id, contract=None):
    contract = CONTRACT if contract is None else contract
    if not isinstance(data, dict):
        raise ValueError('插件清单必须是对象')
    pid = data.get('id')
    if not isinstance(pid, str) or not _ID.fullmatch(pid) or pid != directory_id:
        raise ValueError('插件 ID 必须匹配目录名')
    if type(data.get('api_version')) is not int or data['api_version'] != contract['api_version']:
        raise ValueError(f'{pid}: 不兼容的插件协议')
    for key in ('name', 'description', 'version'):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f'{pid}: 缺少 {key}')
    data = {**contract['defaults'], **data}
    if data['client_mode'] not in ('authority', 'shared'):
        raise ValueError(f'{pid}: client_mode 无效')
    if type(data['order']) is not int:
        raise ValueError(f'{pid}: order 必须为整数')
    data.setdefault('navigation', False)
    for key in ('persistent', 'sidebar', 'immersive', 'navigation'):
        if type(data[key]) is not bool:
            raise ValueError(f'{pid}: {key} 必须为布尔值')
    if 'icon' in data and (not isinstance(data['icon'], str) or not data['icon'].strip()):
        raise ValueError(f'{pid}: icon 必须为非空字符串')
    if 'settings' in data:
        setting = data['settings']
        if (not isinstance(setting, dict) or not isinstance(setting.get('id'), str)
                or not _ID.fullmatch(setting['id']) or setting['id'] in contract['reserved_settings']
                or not isinstance(setting.get('label'), str) or not setting['label'].strip()):
            raise ValueError(f'{pid}: 设置入口无效或与框架冲突')
    for key in ('routes', 'api_prefixes'):
        if not isinstance(data.get(key), list):
            raise ValueError(f'{pid}: {key} 必须为数组')
        for route in data[key]:
            if (not isinstance(route, str) or not route.startswith('/') or route == '/'
                    or '\\' in route):
                raise ValueError(f'{pid}: 无效路径')
            prefix = route.split('/:')[0].rstrip('/')
            if any(matches(prefix, p) or matches(p, prefix) for p in contract['reserved_routes'] + contract['reserved_api']):
                raise ValueError(f'{pid}: 路径占用框架入口')
    ownership = data.get('owns')
    if not isinstance(ownership, dict) or set(ownership) != set(OWNER_KEYS):
        raise ValueError(f'{pid}: owns 无效')
    for key, values in ownership.items():
        if (not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values)
                or len(values) != len(set(values))):
            raise ValueError(f'{pid}: owns.{key} 必须为不重复字符串数组')
    return data


def discover(root=None, contract=None):
    root = Path(root or plugin_root())
    if contract is None:
        contract_path = root / 'contract.json'
        contract = json.loads(contract_path.read_text(encoding='utf-8')) if contract_path.is_file() else CONTRACT
    manifests, occupied, settings, owners = [], [], set(), {}
    for path in sorted(root.glob('*/manifest.json')):
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError('插件目录不能是符号链接')
        data = normalize(json.loads(path.read_text(encoding='utf-8')), path.parent.name, contract)
        pid = data['id']
        for key in ('routes', 'api_prefixes'):
            for route in data[key]:
                prefix = route.split('/:')[0].rstrip('/')
                for previous, owner in occupied:
                    if owner != pid and (matches(prefix, previous) or matches(previous, prefix)):
                        raise ValueError(f'{pid}: 路径与 {owner} 冲突')
                occupied.append((prefix, pid))
        setting = data.get('settings', {}).get('id')
        if setting and setting in settings:
            raise ValueError(f'{pid}: 设置入口重复')
        settings.add(setting)
        for kind, values in data.get('owns', {}).items():
            for value in values:
                key = (kind, value)
                if key in owners:
                    raise ValueError(f'{pid}: {kind}/{value} 与 {owners[key]} 所有权冲突')
                owners[key] = pid
        manifests.append(data)
    return sorted(manifests, key=lambda row: (row['order'], row['id']))
