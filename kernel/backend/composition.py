# -*- coding: utf-8 -*-
"""实例组合边界：档案、路径根、宿主策略、告警机器与插件来源（B7-1）。

B7 的目标是让同一份通用内核服务私人实例与公共实例。第一步不是搬目录，而是把
**导入期隐式约定**改成可验证的显式输入：配置/数据/静态/备份/插件五个根、宿主
身份与家庭映射、告警规则机器表，都由一份显式组合定义给出，避免“后端扫描一个
根、前端打包另一个根”或“公共空台账悄悄回退到私人默认机器”。

本模块只做边界，不搬业务代码：

1. **组合文件** `CONSOLE_COMPOSITION_FILE`：严格 JSON（重复键也拒绝），
   `schema_version=1`。
   - `profile`：`private` / `public`；环境变量显式值与文件不一致时拒绝。
   - `roots`：`config` / `data` / `static` / `backup` / `plugins`。相对路径以
     组合 JSON 所在目录为基准。`config`、`plugins` 必须存在；`data`、`static`、
     `backup` 允许尚未创建，且路径入口与解析过程**不创建目录**。
   - `host`：本机身份、家庭入口、被排除的机器 id、家庭 LAN 地址与宿主控制
     入口。文件中省略的字段取中性空值——显式文件**不暗添**私人映射。
   - `alert_machines`：`{id: {display, load5_threshold}}`，省略即空表。
   无组合文件时保持现役私人 legacy 默认（与 `instance/private/composition.json`
   逐项一致），新 checkout 与现网行为不变。

2. **实例档案** `CONSOLE_INSTANCE_PROFILE`：`private`（默认）或 `public`。
   非法值直接 `CompositionError`，不静默降级——写错档案会用错默认值。显式
   `public` **必须**提供组合文件与有效根，不能回退私人目录、三机或家庭映射。

3. **插件来源清单** `plugins/composition.json`：一次声明已安装插件，后端启动、
   前端构建与离线检查器读同一份文件。缺失、重复、越界路径、软链接、清单与
   磁盘不一致一律 fail closed。

4. **机器台账默认回退开关**：只有“无组合文件的 private”允许缺失台账时回退
   私人三机；组合文件生效时缺台账或台账损坏必须有界失败，不返回空表冒充成功；
   显式空列表 `[]` 仍是合法的空台账。

5. **路径入口** `root_path()` / `config_path()` / `bootstrap_environment()`：
   在 app 深层 import 之前验证组合并 `setdefault` 路径环境；现有专用路径环境
   变量（`CONSOLE_SETTINGS_FILE`、`CONSOLE_MACHINES_PATH` 等）优先。

本模块是叶子：只 import 标准库，不在 import 期建连接或起线程，也不 import
`plugin_manifest` / `machine_config`，避免边界模块自己变成新的环。
"""
import ipaddress
import json
import os
import re
from pathlib import Path

# 与 plugin_manifest._ID 同一形状；此处重复是为了让本模块保持叶子，不反向依赖清单实现。
_ID = re.compile(r'^[a-z][a-z0-9_]{0,47}$')
# 配置文件名：只接受单层普通文件名，杜绝 `../x` 与目录穿越。
_FILE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
# 主机控制入口：`user@host` 或裸 host；首字符不允许 `-`，否则会进 ssh 选项位。
_CONTROL_TARGET = re.compile(
    r'^(?:[A-Za-z0-9][A-Za-z0-9._-]{0,31}@)?[A-Za-z0-9][A-Za-z0-9.-]{0,252}$'
)

PROFILE_PRIVATE = 'private'
PROFILE_PUBLIC = 'public'
PROFILES = (PROFILE_PRIVATE, PROFILE_PUBLIC)
DEFAULT_PROFILE = PROFILE_PRIVATE

SOURCES_FILE = 'composition.json'
SCHEMA_VERSION = 1
COMPOSITION_ENV = 'CONSOLE_COMPOSITION_FILE'

ROOT_NAMES = ('config', 'data', 'static', 'backup', 'plugins')
HOST_KEYS = (
    'local_machine', 'home_machines', 'excluded_machine_ids',
    'home_lan_ip', 'host_control_key', 'host_control_target',
)
TOP_LEVEL_KEYS = {'schema_version', 'profile', 'roots', 'host', 'alert_machines'}
OPTIONAL_TOP_LEVEL_KEYS = {'name'}

# 无组合文件时的私人现役默认（与 instance/private/composition.json 逐项一致）。
# machine_config 的 HOME_LAN_IP / HOME_MACHINES / 排除 id 从这里取值，避免双写。
DEFAULT_LOCAL_MACHINE = 'nas'
LEGACY_HOME_MACHINES = ('nas', 'win', 'mac')
LEGACY_EXCLUDED_MACHINES = ('mac', 'win')
DEFAULT_HOME_LAN_IP = '192.168.50.100'
DEFAULT_HOST_CONTROL_KEY = '~/.ssh/id_console_host'
_LEGACY_HOST = {
    'local_machine': DEFAULT_LOCAL_MACHINE,
    'home_machines': list(LEGACY_HOME_MACHINES),
    'excluded_machine_ids': list(LEGACY_EXCLUDED_MACHINES),
    'home_lan_ip': DEFAULT_HOME_LAN_IP,
    'host_control_key': DEFAULT_HOST_CONTROL_KEY,
    'host_control_target': '',
}
_LEGACY_ALERT_MACHINES = {
    'nas': {'display': 'NAS', 'load5_threshold': 4.0},
    'txy': {'display': 'TXY', 'load5_threshold': 4.0},
    'us': {'display': 'US', 'load5_threshold': 4.0},
    'aly': {'display': 'ALY', 'load5_threshold': 2.0},
    'kr': {'display': 'KR', 'load5_threshold': 2.0},
    'sg': {'display': 'SG', 'load5_threshold': 2.0},
}

BACKEND_DIR = Path(__file__).resolve().parent


def _layout_dirs():
    """Resolve console/repo roots for kernel layout, flat /app, or legacy console/backend.

    Physical ``kernel/backend`` must not treat ``kernel/`` as the config root: live
    yaml still lives under ``console/``. Flat Docker images copy kernel modules to
    ``/app`` so ``BACKEND_DIR`` itself is the runtime root (yaml mounted beside it).
    """
    backend = BACKEND_DIR
    if backend.name == 'backend' and backend.parent.name == 'kernel':
        repo = backend.parent.parent
        console = repo / 'console'
        return (console if console.is_dir() else repo), repo
    return backend.parent, backend.parent.parent


CONSOLE_DIR, REPO_DIR = _layout_dirs()


class CompositionError(ValueError):
    """组合输入非法：启动/构建即失败，不猜测一个“差不多”的组合。"""


# ---------------------------------------------------------------------------
# 文件读取
# ---------------------------------------------------------------------------

def _strict_object(pairs):
    """拒绝重复键：后者静默覆盖前者会掩盖配置写错。"""
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise CompositionError(f'组合 JSON 含重复键: {key}')
        seen.add(key)
    return dict(pairs)


def _reject_constant(value):
    raise CompositionError(f'组合 JSON 不允许非有限数值: {value}')


def composition_file():
    """显式组合文件路径；未设置返回 None（不判断文件是否存在）。

    只做绝对化、不解析软链接：软链接本身是拒绝理由，解析掉就看不见了。
    """
    raw = (os.environ.get(COMPOSITION_ENV) or '').strip()
    if not raw:
        return None
    try:
        return resolve_root_path(Path.cwd(), 'composition_file', raw)
    except CompositionError as exc:
        raise CompositionError('组合文件缺失或不是普通文件：路径不能经过符号链接') from exc


def _read_document(path):
    if path.is_symlink() or not path.is_file():
        raise CompositionError(f'组合文件缺失或不是普通文件: {path}')
    try:
        text = path.read_text(encoding='utf-8')
        document = json.loads(
            text, object_pairs_hook=_strict_object, parse_constant=_reject_constant,
        )
    except CompositionError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise CompositionError(f'组合文件不可读取或不是严格 JSON: {path}') from exc
    _validate_document(document)
    return document


def _validate_document(document):
    if not isinstance(document, dict):
        raise CompositionError('组合文件顶层必须是对象')
    keys = set(document)
    if not TOP_LEVEL_KEYS <= keys or keys - TOP_LEVEL_KEYS - OPTIONAL_TOP_LEVEL_KEYS:
        raise CompositionError(
            '组合文件顶层必须包含 schema_version/profile/roots/host/alert_machines，'
            '只允许额外字段 name'
        )
    if type(document['schema_version']) not in (int, float) or document['schema_version'] != SCHEMA_VERSION:
        raise CompositionError(f'组合文件 schema_version 必须为 {SCHEMA_VERSION}')
    if document['profile'] not in PROFILES:
        raise CompositionError('组合文件 profile 必须是 private 或 public')
    if 'name' in document and (not isinstance(document['name'], str) or not document['name'].strip()):
        raise CompositionError('组合文件 name 必须是非空字符串')
    _validate_roots(document['roots'])
    _validate_host(document['host'])
    _validate_alert_machines(document['alert_machines'])


def _validate_roots(roots):
    if not isinstance(roots, dict) or set(roots) != set(ROOT_NAMES):
        raise CompositionError('组合文件 roots 必须且只能包含 ' + ', '.join(ROOT_NAMES))
    for name, value in roots.items():
        if not isinstance(value, str) or not value.strip() or '\x00' in value:
            raise CompositionError(f'组合文件 roots.{name} 必须是非空路径字符串')


def _validate_host(host):
    if not isinstance(host, dict):
        raise CompositionError('组合文件 host 必须是对象')
    unknown = set(host) - set(HOST_KEYS)
    if unknown:
        raise CompositionError('组合文件 host 含未知字段: ' + ', '.join(sorted(unknown)))
    local = host.get('local_machine')
    if local is not None and (not isinstance(local, str) or (local and not _ID.fullmatch(local))):
        raise CompositionError('host.local_machine 必须为空或小写机器 id')
    for key in ('home_machines', 'excluded_machine_ids'):
        values = host.get(key)
        if values is None:
            continue
        if not isinstance(values, list):
            raise CompositionError(f'host.{key} 必须是数组')
        for value in values:
            if not isinstance(value, str) or not _ID.fullmatch(value):
                raise CompositionError(f'host.{key} 含无效机器 id: {value!r}')
        if len(values) != len(set(values)):
            raise CompositionError(f'host.{key} 不能重复')
    ip = host.get('home_lan_ip')
    if ip is not None and ip != '':
        if not isinstance(ip, str):
            raise CompositionError('host.home_lan_ip 必须是字符串')
        try:
            ipaddress.ip_address(ip)
        except ValueError as exc:
            raise CompositionError('host.home_lan_ip 必须是 IP 地址') from exc
    _validate_textish(host, 'home_lan_ip')
    _validate_textish(host, 'host_control_key')
    target = host.get('host_control_target')
    if target is not None and target != '':
        if not isinstance(target, str) or not _CONTROL_TARGET.fullmatch(target):
            raise CompositionError('host.host_control_target 必须是 user@host 或裸 host')
    _validate_textish(host, 'host_control_target')


def _validate_textish(host, key):
    value = host.get(key)
    if value is None or value == '':
        return
    if not isinstance(value, str) or any(ch in value for ch in ('\r', '\n', '\x00')):
        raise CompositionError(f'host.{key} 必须是无控制字符的字符串')


def _validate_alert_machines(specs):
    if not isinstance(specs, dict):
        raise CompositionError('组合文件 alert_machines 必须是对象')
    for machine, spec in specs.items():
        if not isinstance(machine, str) or not _ID.fullmatch(machine):
            raise CompositionError(f'alert_machines 含无效机器 id: {machine!r}')
        if not isinstance(spec, dict) or set(spec) != {'display', 'load5_threshold'}:
            raise CompositionError(
                f'alert_machines.{machine} 必须且只能包含 display/load5_threshold'
            )
        if not isinstance(spec['display'], str) or not spec['display'].strip():
            raise CompositionError(f'alert_machines.{machine}.display 必须是非空字符串')
        threshold = spec['load5_threshold']
        if type(threshold) not in (int, float) or not 0 < threshold < 1e6:
            raise CompositionError(
                f'alert_machines.{machine}.load5_threshold 必须是正数值'
            )


# ---------------------------------------------------------------------------
# 组合解析
# ---------------------------------------------------------------------------

def resolve_root_path(base, name, value):
    """逐段检查后解析根；显式 `..` 不能掩盖之前经过的符号链接。"""
    if not isinstance(value, (str, Path)) or not str(value).strip() or '\x00' in str(value):
        raise CompositionError(f'组合路径 {name} 必须是非空路径字符串')
    try:
        raw = Path(value).expanduser()
    except RuntimeError as exc:
        raise CompositionError(f'组合路径 {name} 无法展开用户目录') from exc
    current = Path(raw.anchor) if raw.is_absolute() else Path(base).resolve()
    parts = raw.parts[1:] if raw.is_absolute() else raw.parts
    for part in parts:
        if part in ('', '.'):
            continue
        if part == '..':
            current = current.parent
            continue
        current = current / part
        if current.is_symlink():
            raise CompositionError(f'组合 roots.{name} 不能经过符号链接: {current}')
    return current.resolve()


def _resolved(document, path):
    if not isinstance(document, dict):
        raise CompositionError('组合文件顶层必须是对象')
    base = path.parent.resolve()
    roots = {
        name: resolve_root_path(base, name, value)
        for name, value in document['roots'].items()
    }
    if not roots['config'].is_dir():
        raise CompositionError(f'组合 roots.config 不存在或不是目录: {roots["config"]}')
    plugins = roots['plugins']
    if not plugins.is_dir():
        raise CompositionError(f'组合 roots.plugins 不存在或不是目录: {plugins}')
    contract = plugins / 'contract.json'
    if contract.is_symlink() or not contract.is_file():
        raise CompositionError(f'组合 roots.plugins 缺少普通文件 contract.json: {plugins}')
    host = _neutral_host()
    for key in HOST_KEYS:
        if document['host'].get(key) is not None:
            host[key] = document['host'][key]
    override_ip = (os.environ.get('CONSOLE_HOME_LAN_IP') or '').strip()
    if override_ip:
        _validate_host({'home_lan_ip': override_ip})
        host['home_lan_ip'] = override_ip
    host['home_machines'] = list(host['home_machines'])
    host['excluded_machine_ids'] = list(host['excluded_machine_ids'])
    specs = {mid: dict(spec) for mid, spec in document['alert_machines'].items()}
    return {
        'schema_version': SCHEMA_VERSION,
        'profile': document['profile'],
        'name': document.get('name', ''),
        'file': str(path),
        'roots': {name: str(value) for name, value in roots.items()},
        'host': host,
        'alert_machines': specs,
    }


def _neutral_host():
    return {
        'local_machine': '',
        'home_machines': [],
        'excluded_machine_ids': [],
        'home_lan_ip': '',
        'host_control_key': '',
        'host_control_target': '',
    }


def _legacy_host():
    host = dict(_LEGACY_HOST)
    host['home_machines'] = list(_LEGACY_HOST['home_machines'])
    host['excluded_machine_ids'] = list(_LEGACY_HOST['excluded_machine_ids'])
    host['home_lan_ip'] = (
        os.environ.get('CONSOLE_HOME_LAN_IP') or _LEGACY_HOST['home_lan_ip']
    )
    return host


def _legacy_roots():
    bundled = BACKEND_DIR / 'plugins'
    # Kernel checkout: private live data/static stay under console/backend (same as
    # instance/private/composition.json). Flat /app images keep them beside modules.
    if BACKEND_DIR.parent.name == 'kernel' and (CONSOLE_DIR / 'backend').is_dir():
        data_dir = CONSOLE_DIR / 'backend' / 'data'
        static_dir = CONSOLE_DIR / 'backend' / 'static'
    else:
        data_dir = BACKEND_DIR / 'data'
        static_dir = BACKEND_DIR / 'static'
    return {
        'config': str(CONSOLE_DIR),
        'data': str(data_dir),
        'static': str(static_dir),
        'backup': str(REPO_DIR / 'backup'),
        'plugins': str(bundled if bundled.is_dir() else REPO_DIR / 'plugins'),
    }


def legacy_alert_machine_specs():
    """无组合文件时的 6 机规则输入（B0 冻结，供等价性测试与实例文件对照）。"""
    return {mid: dict(spec) for mid, spec in _LEGACY_ALERT_MACHINES.items()}


def _legacy_composition():
    return {
        'schema_version': SCHEMA_VERSION,
        'profile': DEFAULT_PROFILE,
        'name': '',
        'file': None,
        'roots': _legacy_roots(),
        'host': _legacy_host(),
        'alert_machines': legacy_alert_machine_specs(),
    }


def _env_profile():
    raw = os.environ.get('CONSOLE_INSTANCE_PROFILE')
    explicit = bool(raw is not None and str(raw).strip())
    value = DEFAULT_PROFILE if raw is None else str(raw).strip().lower()
    if not value:
        value = DEFAULT_PROFILE
    if value not in PROFILES:
        raise CompositionError(
            f'CONSOLE_INSTANCE_PROFILE 取值无效: {value!r}，只允许 {" / ".join(PROFILES)}'
        )
    return value, explicit


def _file_profile(document, path, value, explicit):
    if explicit and value != document['profile']:
        raise CompositionError(
            f'CONSOLE_INSTANCE_PROFILE={value} 与组合文件 profile='
            f'{document["profile"]} 不一致，拒绝跨档案覆盖: {path}'
        )
    return document['profile']


def instance_profile():
    """实例档案：private 沿用现役默认值，public 使用公共安全默认值。

    文件存在时以文件档案为准；环境变量显式写出不同档案时拒绝。
    """
    value, explicit = _env_profile()
    path = composition_file()
    if path is None:
        return value
    return _file_profile(_read_document(path), path, value, explicit)


def resolve_composition():
    """解析生效组合，返回 dict；无文件时返回 legacy 兼容根/策略。

    任何非法输入（文件损坏、profile 冲突、公共无文件、必需根缺失）都抛有界的
    `CompositionError`，不降级到另一个组合。
    """
    value, explicit = _env_profile()
    path = composition_file()
    if path is None:
        if value == PROFILE_PUBLIC:
            raise CompositionError(
                '公共实例必须提供 CONSOLE_COMPOSITION_FILE，不能回退私人目录与三机'
            )
        return _legacy_composition()
    document = _read_document(path)
    _file_profile(document, path, value, explicit)
    return _resolved(document, path)


def private_machine_defaults():
    """缺失台账时是否允许回退私人三机（nas/txy/aly 硬编码）。

    只有“无组合文件的 private”允许：公共候选与显式组合文件都不能因为少挂一个
    machines.yaml 就指向私人主机，也不暗添默认机器。
    """
    return instance_profile() == PROFILE_PRIVATE and composition_file() is None


def host_policy():
    """宿主策略：本机身份、家庭入口、排除机器、家庭 LAN 与控制入口。"""
    return resolve_composition()['host']


def alert_machine_specs():
    """告警规则机器表 `{id: {display, load5_threshold}}`；公共默认为空表。"""
    return resolve_composition()['alert_machines']


# ---------------------------------------------------------------------------
# 路径入口
# ---------------------------------------------------------------------------

def root_path(name):
    """组合根路径；专用环境覆盖优先，未知根名直接拒绝。"""
    if name not in ROOT_NAMES:
        raise CompositionError(f'未知组合根: {name!r}，只允许 {", ".join(ROOT_NAMES)}')
    resolved = resolve_composition()
    env_key = {
        'config': 'CONSOLE_CONFIG_ROOT', 'data': 'CONSOLE_DATA_ROOT',
        'static': 'CONSOLE_STATIC_ROOT', 'backup': 'OPS_BACKUP_DIR',
        'plugins': 'CONSOLE_PLUGIN_ROOT',
    }[name]
    override = (os.environ.get(env_key) or '').strip()
    if override:
        return resolve_root_path(Path.cwd(), name, override)
    return Path(resolved['roots'][name])


def config_path(filename, override_env=None):
    """配置文件名 → 绝对路径；专用环境变量优先于组合根。

    只返回路径，不创建文件或目录：`data` / `backup` / `static` 等允许待创建。
    """
    if not isinstance(filename, str) or not _FILE_NAME.fullmatch(filename) or '..' in filename:
        raise CompositionError(f'配置文件名无效: {filename!r}')
    if override_env:
        override = (os.environ.get(override_env) or '').strip()
        if override:
            return Path(override).expanduser().resolve()
    if composition_file() is None and not (os.environ.get('CONSOLE_CONFIG_ROOT') or '').strip():
        # 现役布局：容器内 /app/<name>，开发机 console/<name>；kernel 真身仍探 console。
        candidates = [BACKEND_DIR / filename, CONSOLE_DIR / filename]
        if BACKEND_DIR.parent.name == 'kernel':
            candidates.append(REPO_DIR / 'console' / filename)
        for candidate in candidates:
            if candidate.exists():
                return candidate.resolve()
        return (CONSOLE_DIR / filename).resolve()
    return root_path('config') / filename


def bootstrap_environment():
    """app 深层 import 前的组合闸门。

    验证档案/组合文件/插件根；显式组合文件生效时 `setdefault` 五个路径环境
    变量，现有专用路径环境变量（含 `CONSOLE_PLUGIN_ROOT`）优先，不被覆盖。
    无组合文件时保持现役 legacy 行为，不改动任何路径环境。
    """
    composition = resolve_composition()
    if composition['file'] is not None:
        for env_key, root_name in (
            ('CONSOLE_CONFIG_ROOT', 'config'),
            ('CONSOLE_DATA_ROOT', 'data'),
            ('CONSOLE_STATIC_ROOT', 'static'),
            ('OPS_BACKUP_DIR', 'backup'),
            ('CONSOLE_PLUGIN_ROOT', 'plugins'),
        ):
            if not (os.environ.get(env_key) or '').strip():
                os.environ[env_key] = composition['roots'][root_name]
    return composition


# ---------------------------------------------------------------------------
# 插件来源清单
# ---------------------------------------------------------------------------

def _read_sources(root):
    path = root / SOURCES_FILE
    if not path.is_file() or path.is_symlink():
        raise CompositionError(f'插件组合清单缺失或不是普通文件: {SOURCES_FILE}')
    try:
        document = json.loads(
            path.read_text(encoding='utf-8'), object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except CompositionError:
        raise
    except (OSError, ValueError) as exc:
        raise CompositionError(f'插件组合清单不可读取: {SOURCES_FILE}') from exc
    if not isinstance(document, dict) or set(document) != {'plugins'}:
        raise CompositionError(f'{SOURCES_FILE} 顶层必须只含 plugins 数组')
    declared = document['plugins']
    if not isinstance(declared, list):
        raise CompositionError(f'{SOURCES_FILE} 的 plugins 必须是数组')
    # 空数组是合法的显式「零插件」组合；缺失文件才是错误，两者不能混为一谈。
    seen = set()
    for pid in declared:
        if not isinstance(pid, str) or not _ID.fullmatch(pid):
            # 路径分隔符、绝对路径与 `..` 都被这个正则挡在清单之外。
            raise CompositionError(f'{SOURCES_FILE} 含无效插件 id: {pid!r}')
        if pid in seen:
            raise CompositionError(f'{SOURCES_FILE} 插件 id 重复: {pid}')
        seen.add(pid)
    return tuple(declared)


def _on_disk(root):
    ids = []
    for child in sorted(root.iterdir()):
        # 符号链接先判：指向不存在的目标时 `manifest.json` 的 is_file 只是 False，
        # 那会被误读成「没有这个插件」而掩盖越界来源。
        if child.is_symlink():
            raise CompositionError(f'插件来源不能是符号链接: {child.name}')
        manifest = child / 'manifest.json'
        if manifest.is_symlink():
            raise CompositionError(f'插件清单不能是符号链接: {child.name}/manifest.json')
        if manifest.is_file():
            ids.append(child.name)
    return tuple(ids)


def plugin_sources(root):
    """读取并校验插件来源清单，返回按清单顺序的插件 id 元组。

    校验两层：清单自身（形状/重复/越界）与清单对磁盘（声明缺失、磁盘多余）。
    只声明不落盘的“幽灵插件”和只落盘不声明的“影子插件”都不能启动。
    """
    root = Path(root)
    declared = _read_sources(root)
    on_disk = _on_disk(root)
    missing = sorted(set(declared) - set(on_disk))
    unlisted = sorted(set(on_disk) - set(declared))
    if missing:
        raise CompositionError('组合清单声明了不存在的插件: ' + ', '.join(missing))
    if unlisted:
        raise CompositionError('插件目录未在组合清单登记: ' + ', '.join(unlisted))
    return declared


def require_plugin_sources(root, manifests):
    """启动期交叉校验：组合清单与已发现清单必须是同一集合。"""
    declared = plugin_sources(root)
    installed = [row['id'] for row in manifests]
    if sorted(declared) != sorted(installed):
        raise CompositionError(
            '组合清单与已发现插件不一致: 声明=' + ','.join(sorted(declared))
            + ' 实际=' + ','.join(sorted(installed))
        )
    return declared
