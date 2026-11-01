"""插件清单、持久化状态、请求门禁与受控周期任务（仅可信仓库代码）。"""
import logging
import math
import subprocess
import threading
import time
from dataclasses import replace

import yaml
from flask import jsonify, request

import collect_guard
import composition
import plugin_imports
import runtime_mode
from plugin_manifest import API_VERSION, discover, matches  # noqa: F401
from yaml_io import mutate_yaml, read_yaml_locked

logger = logging.getLogger(__name__)
MANIFESTS = discover()
# 组合清单与磁盘清单必须是同一集合：只声明不落盘、只落盘未声明都拒绝启动（B7-1）。
composition.require_plugin_sources(plugin_imports.plugin_root(), MANIFESTS)
BY_ID = {item['id']: item for item in MANIFESTS}


def _settings_path():
    # 延迟 import，避免 settings_store / monitor_events 的导入环。
    import settings_store
    return settings_store.settings_path()


def _states(document):
    if not isinstance(document, dict):
        raise ValueError('settings.yaml 顶层必须是对象')
    states = document.get('PLUGINS', {})
    if not isinstance(states, dict):
        raise ValueError('PLUGINS 必须是对象')
    for pid, state in states.items():
        if not isinstance(state, dict) or set(state) - {'enabled', 'visible'}:
            raise ValueError(f'{pid}: 插件状态格式无效')
        if any(type(value) is not bool for value in state.values()):
            raise ValueError(f'{pid}: 插件状态必须为布尔值')
    return states


def read_states():
    document = read_yaml_locked(_settings_path())
    return _states({} if document is None else document)


def state(pid, states=None):
    if pid not in BY_ID:
        return {'enabled': False, 'visible': False}
    states = read_states() if states is None else states
    return {'enabled': True, 'visible': True, **states.get(pid, {})}


def is_enabled(pid):
    try:
        return state(pid)['enabled']
    except (OSError, ValueError, yaml.YAMLError):
        # 配置损坏不意外开启后台采集或写接口；管理 API 提供明确错误。
        return False


def runtime_status(pid):
    with _plugin_lock(pid):
        return {'available': pid not in _failures, 'error': _failures.get(pid)}


def is_available(pid):
    return is_enabled(pid) and runtime_status(pid)['available']


def catalog():
    states = read_states()
    return [{**item, **state(item['id'], states), **runtime_status(item['id'])} for item in MANIFESTS]


def update(pid, payload):
    if pid not in BY_ID:
        raise LookupError('插件未安装')
    if (not isinstance(payload, dict) or not payload or set(payload) - {'enabled', 'visible'}
            or any(type(value) is not bool for value in payload.values())):
        raise ValueError('只接受 enabled / visible 布尔字段')

    def change(document):
        document = {} if document is None else document
        states = _states(document)
        states = {**states, pid: {**state(pid, states), **payload}}
        return {**document, 'PLUGINS': states}

    # Serialize state transition and initialization for this plugin only.
    # Its requests wait for the initializer; unrelated plugins remain responsive.
    with _plugin_lock(pid):
        previous = state(pid)
        mutate_yaml(_settings_path(), change)
        if payload.get('enabled') is True and (not previous['enabled'] or not runtime_status(pid)['available']):
            _failures.pop(pid, None)
            _initialized.discard(pid)
            initialize_plugin(pid)
        return {**BY_ID[pid], **state(pid), **runtime_status(pid)}


def owner(path):
    for item in MANIFESTS:
        for prefix in item['api_prefixes']:
            if matches(path, prefix):
                return item['id']
    return None


_authority_catalog = None


def authority_state(pid):
    rows = _authority_catalog()
    for row in rows:
        if row.get('id') == pid and type(row.get('enabled')) is bool:
            if type(row.get('available', True)) is not bool:
                raise runtime_mode.AuthorityUnavailable('NAS 插件状态无效')
            return row
    raise runtime_mode.AuthorityUnavailable('NAS 插件状态缺失')


def forwards_api(path):
    pid = owner(path)
    return pid is not None and BY_ID[pid].get('client_mode', 'authority') == 'authority'


def owns_api(path):
    return owner(path) is not None


def disabled_response(pid):
    response = jsonify({'error': '插件已停用', 'code': 'plugin_disabled', 'plugin': pid})
    response.status_code = 503
    response.headers['Cache-Control'] = 'no-store'
    return response


_initializers = []


def add_initializer(pid, callback):
    _initializers.append((pid, callback))


_lifecycle_lock = threading.RLock()
_plugin_locks = {}


def _plugin_lock(pid):
    with _lifecycle_lock:
        return _plugin_locks.setdefault(pid, threading.RLock())


_initialized = set()
_failures = {}


def initialize_plugin(pid):
    # Serialize initialization and explicit retries; no implicit retry on each request.
    with _plugin_lock(pid):
        if pid in _initialized or pid in _failures:
            return
        if not collect_guard.collection_allowed() or not is_enabled(pid):
            return
        try:
            for owner, callback in _initializers:
                if owner == pid:
                    callback()
        except Exception as exc:
            _failures[pid] = '插件初始化失败，请重新启用后重试'
            logger.error('Plugin initialization failed: %s (%s)', pid, type(exc).__name__)
        else:
            _initialized.add(pid)


def initialize():
    for pid in BY_ID:
        initialize_plugin(pid)


_jobs = []


def add_job(pid, name, interval, callback):
    """只登记可信 Python 回调；停用阻止后续轮次，在途轮次允许结束。"""
    if not name or not callable(callback) or type(interval) not in (int, float) or not math.isfinite(interval) or interval <= 0:
        raise ValueError('插件任务需要名称、正有限间隔和可调用回调')
    if any(owner == pid and key == name for owner, key, _, _ in _jobs):
        raise ValueError('插件任务名称重复')
    _jobs.append((pid, name, interval, callback))


def run_job(job):
    pid, name, _interval, callback = job
    if not collect_guard.collection_allowed() or not is_available(pid):
        return False
    try:
        callback()
    except Exception:
        logger.exception('plugin job failed: %s/%s', pid, name)
    return True


def background_writes_allowed():
    """后台写任务（job）是否允许启动。

    `collect_guard.collection_allowed()` 已经要求 authority 且非 read_only；
    B7-1 把这条闸门收成一个可测试的命名入口，`client` / `read_only` 都不启动
    后台写线程——不是靠每个 job 自己再判断一次。
    """
    return collect_guard.collection_allowed()


def start_jobs():
    if not background_writes_allowed():
        return
    for job in _jobs:
        def loop(job=job):
            deadline = 0.0
            was_enabled = False
            while True:
                enabled = is_available(job[0])
                now = time.monotonic()
                if enabled and (not was_enabled or now >= deadline):
                    run_job(job)
                    deadline = time.monotonic() + job[2]
                was_enabled = enabled
                time.sleep(1)
        threading.Thread(target=loop, name=f'plugin-{job[0]}-{job[1]}', daemon=True).start()


def register(app, context):
    """注册固定目录插件；主入口不列举功能名。请求门禁先于业务及代理。"""
    @app.before_request
    def plugin_gate():
        pid = owner(request.path)
        if not pid:
            return None
        # 已转发的 YAML/观测请求在前面的权威钩子返回；本地共享 PG 请求仍核实 NAS 开关。
        if runtime_mode.instance_role() == runtime_mode.INSTANCE_ROLE_CLIENT:
            try:
                remote = authority_state(pid)
                enabled = remote['enabled']
                available = remote.get('available', True)
            except runtime_mode.AuthorityUnavailable:
                return jsonify({'error': '无法确认 NAS 插件状态，请稍后重试'}), 503
        else:
            enabled = is_enabled(pid)
            available = runtime_status(pid)['available']
        if not enabled:
            return disabled_response(pid)
        if not available:
            response = jsonify({'error': '插件初始化失败，请重新启用后重试', 'code': 'plugin_unavailable', 'plugin': pid})
            response.status_code = 503
            response.headers['Cache-Control'] = 'no-store'
            return response
        return None

    global _authority_catalog
    def read_authority_catalog():
        return context.authority_rows('/api/plugins')
    _authority_catalog = read_authority_catalog

    @app.route('/api/plugins', methods=['GET'])
    def get_plugins():
        """已安装插件清单及启用/显隐状态；配置损坏返回 503，不回落为全启用。"""
        try:
            response = jsonify(catalog())
            response.headers['Cache-Control'] = 'no-store'
            return response
        except (OSError, ValueError, yaml.YAMLError):
            return jsonify({'error': '插件设置不可读，请检查 settings.yaml'}), 503

    @app.route('/api/plugins/<plugin_id>', methods=['PUT'])
    def put_plugin(plugin_id):
        """原子更新一个插件的 enabled / visible；保留业务数据、立即影响新请求。"""
        if runtime_mode.is_read_only():
            return jsonify({'error': '维护只读模式不能修改插件'}), 503
        try:
            result = update(plugin_id, request.get_json(silent=True))
        except LookupError:
            return jsonify({'error': '插件未安装'}), 404
        except (ValueError, yaml.YAMLError) as exc:
            return jsonify({'error': str(exc)}), 400
        except OSError:
            return jsonify({'error': '插件设置保存失败'}), 503
        context.audit('console', plugin_id, 'plugin_update', True)
        return jsonify(result)

    for item in MANIFESTS:
        module = plugin_imports.load_module(f"plugins.{item['id']}.backend")
        before = {id(rule) for rule in app.url_map.iter_rules()}
        if any(owner(rule.rule) == item['id'] and not rule.endpoint.startswith('retired_')
               for rule in app.url_map.iter_rules()):
            raise ValueError(f"{item['id']}: API 前缀占用已有框架路由")
        module.register(app, replace(context, plugin_id=item['id']))
        for rule in app.url_map.iter_rules():
            if id(rule) in before:
                continue
            if owner(rule.rule) != item['id']:
                raise ValueError(f"{item['id']}: 路由 {rule.rule} 未归属清单 api_prefixes")


_services = []


def add_service(pid, start):
    """登记受控服务，start 返回带 poll/terminate/wait/kill 的子进程句柄。"""
    _services.append({'id': pid, 'start': start, 'process': None, 'retry_at': 0})


def sync_service(service):
    """每轮协调状态；仅 authority 启动，停用关闭监听并回收进程。"""
    enabled = runtime_mode.is_authority() and is_available(service['id'])
    process = service['process']
    if process is not None and process.poll() is not None:
        process.wait()
        if process.stdin:
            process.stdin.close()
        service['process'] = process = None
    if not enabled:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if process.stdin:
                process.stdin.close()
            service['process'] = None
        service['retry_at'] = 0
    elif process is None and time.monotonic() >= service['retry_at']:
        service['retry_at'] = time.monotonic() + 30
        try:
            service['process'] = service['start']()
        except Exception:
            logger.exception('plugin service failed: %s', service['id'])


def start_services():
    # 独立服务是监听器（面板代理、工作台网关），契约按 authority/启用状态协调；
    # 这里保持原判据，不借用只针对「写任务」的 background_writes_allowed。
    if not runtime_mode.is_authority():
        return
    for service in _services:
        def loop(service=service):
            while True:
                try:
                    sync_service(service)
                except Exception as exc:
                    logger.error('Plugin service coordination failed: %s (%s)', service['id'], type(exc).__name__)
                time.sleep(1)
        threading.Thread(target=loop, name=f"plugin-{service['id']}-service", daemon=True).start()
