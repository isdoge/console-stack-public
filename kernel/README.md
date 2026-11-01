# kernel

本仓唯一通用 Console 源码根（B7 P2）。私人组合与过渡入口仍通过原路径的薄委托消费这里的实现，不保留第二份可编辑副本。

当前批次（进行中）：

1. `backend/` — 已迁入的无私人依赖叶模块如下；其余仍在 `console/backend/`，按导入图逐批搬迁。

已迁入 `kernel/backend/`（72）：

- `access_helper.py`
- `app.py`
- `attention.py`
- `auth.py`
- `backup_contracts.py`
- `backup_identity.py`
- `caddy_routes.py`
- `caddy_view.py`
- `capacity.py`
- `checker.py`
- `collect_guard.py`
- `component_migrations.py`
- `composition.py`
- `composition_identity.py`
- `contract_errors.py`
- `dashboard_layout.py`
- `db.py`
- `db_sqlite.py`
- `effective_config.py`
- `entry_view.py`
- `inspection_report.py`
- `kernel_baseline.py`
- `ledger_batch_contracts.py`
- `live_snapshot.py`
- `machine_config.py`
- `money.py`
- `monitor_events.py`
- `monitoring_alert.py`
- `monitoring_api.py`
- `monitoring_business.py`
- `monitoring_compare.py`
- `monitoring_history.py`
- `monitoring_ingest.py`
- `monitoring_maintenance.py`
- `monitoring_observation.py`
- `monitoring_payload.py`
- `monitoring_schema.py`
- `node_probe.py`
- `node_traffic.py`
- `ops_status.py`
- `paths.py`
- `pg01_test_support.py`
- `pg02_support.py`
- `pg_backup.py`
- `pg_contracts.py`
- `pg_diagnostics.py`
- `pg_locks.py`
- `pg_migration_contracts.py`
- `pg_migrations.py`
- `pg_test_support.py`
- `pgdb.py`
- `plugin_context.py`
- `plugin_contributions.py`
- `plugin_imports.py`
- `plugin_manifest.py`
- `plugin_runtime.py`
- `plugin_sdk.py`
- `probe_cache.py`
- `runtime_mode.py`
- `service_health_history.py`
- `service_manager.py`
- `settings_store.py`
- `snapshot_freshness.py`
- `sqlite_legacy.py`
- `storage_dispatch.py`
- `tailscale_view.py`
- `upgrade_prep.py`
- `validation.py`
- `version_service.py`
- `version_sync.py`
- `write_gate.py`
- `yaml_io.py`

`console/backend/` 非 shim 残留（私人/桥，20）：`beszel_api.py`, `beszel_client.py`, `capacity_legacy.py`, `feed_aggregator.py`, `hindsight_proxy.py`, `ledger.py`, `ledger_ai_classify.py`, `ledger_auto_classify.py`, `ledger_build_table.py`, `ledger_cli.py`, `ledger_cmb_parser.py`, `ledger_cmb_sync.py`, `ledger_file_routes.py`, `ledger_files.py`, `ledger_import.py`, `ledger_merge_bills.py`, `ledger_transfer.py`, `monitor_observability.py`, `monitoring_backfill.py`, `renewals.py`。


2. `agent/` — 已从根 `agent/` **git mv** 迁入；根目录仅留 README 指针。
3. `frontend/` — 已从 `console/frontend/` **git mv** 迁入；`console/frontend/` 仅留 README 薄入口。Docker/CI/`prepare_frontend_composition` 消费本目录；证据见 [kernel-frontend-physical](../.agents/tasks/2026-10-30-monitoring-refactor-b7/evidence/2026-11-01-kernel-frontend-physical.md)。
4. `theme/` — **暂缓**：根 `theme/`（MUI legacy）无现役 frontend/plugins 消费者；UI 主题在 `frontend` 内 CSS 变量 + `use-theme`。不因目录名强行搬入。
5. `scripts/` / `packaging/` / `contracts/` / `examples/` / `docs/` — 尚未物理迁入；归属见 B7 主记录 §3。

不要在 `console/` 再实现通用逻辑；改这里，旧路径只保留委托。

宿主备份入口 `scripts/backup_db.py` 对生产 `docker exec … psql` 经容器本机 trust 代理收集同 snapshot 组件报告（P2）；通用原语仍在本目录 `pg_backup` / `component_migrations`。
