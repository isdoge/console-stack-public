"""Small in-process extension slots. No business imports and no plugin-to-plugin calls."""
import logging

logger = logging.getLogger(__name__)
SLOTS = frozenset({'attention', 'machine_summary', 'inspection', 'certificate', 'ops_status', 'config_defaults'})
_registry = {}


def register(plugin_id, slot, name, callback):
    if slot not in SLOTS or not name or not callable(callback):
        raise ValueError('Invalid plugin contribution')
    key = (slot, name)
    if key in _registry:
        raise ValueError(f'Duplicate plugin contribution: {slot}/{name}')
    _registry[key] = (plugin_id, callback)


def collect(slot, **kwargs):
    import plugin_runtime
    values = {}
    for (kind, name), (pid, callback) in tuple(_registry.items()):
        if slot == 'certificate':
            import collect_guard
            if not collect_guard.collection_allowed():
                continue
        if kind != slot or not plugin_runtime.is_available(pid):
            continue
        try:
            values[name] = callback(**kwargs)
        except Exception as exc:
            logger.error('Plugin contribution failed: %s/%s/%s (%s)', pid, slot, name, type(exc).__name__)
            values[name] = {'error': '插件数据暂不可用', 'items': [], 'sampled_at': None} if slot == 'inspection' else None
    return values


def attention_items(db_path):
    return [item for items in collect('attention', db_path=db_path).values()
            if isinstance(items, list) for item in items]
