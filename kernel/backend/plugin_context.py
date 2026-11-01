"""Stable capabilities exposed to trusted plugins; no application module or mutation hook."""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class PluginContext:
    plugin_id: str
    database_path: Callable
    panel_config_path: Callable
    backend_dir: str
    logger: object
    audit: Callable
    money_payload: Callable
    storage_error: Callable
    authority_rows: Callable
    utc_now: Callable
    parse_json: Callable

    @property
    def db_path(self):
        return self.database_path()

    @property
    def panels_path(self):
        return self.panel_config_path()

    def add_initializer(self, callback):
        import plugin_runtime
        plugin_runtime.add_initializer(self.plugin_id, callback)

    def add_job(self, name, interval_seconds, callback):
        import plugin_runtime
        plugin_runtime.add_job(self.plugin_id, name, interval_seconds, callback)

    def add_service(self, start):
        import plugin_runtime
        plugin_runtime.add_service(self.plugin_id, start)

    def contribute(self, slot, name, callback):
        import plugin_contributions
        plugin_contributions.register(self.plugin_id, slot, name, callback)
