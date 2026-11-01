# -*- coding: utf-8 -*-
# ruff: noqa: E402 -- 实例组合先于业务模块导入。
import datetime
import http.client
import json
import logging
import math
import os
import time
import urllib.error
import urllib.request
from decimal import Decimal
from urllib.parse import quote

import yaml
from flask import Flask, Response, jsonify, request, send_from_directory
from flask_cors import CORS

import composition

# 组合路径必须在读取模块级配置和插件发现之前确定。
composition.bootstrap_environment()

import access_helper as ah  # noqa: E402
import attention
import caddy_routes as cr
import caddy_view as cv
import capacity
import collect_guard
import composition_identity
import dashboard_layout
import db as dbmod
import effective_config
import entry_view
import inspection_report
import machine_config as mc
import monitor_events
import monitoring_api
import monitoring_business as mb
import monitoring_history
import monitoring_ingest
import node_probe
import node_traffic as nt
import ops_status
import paths
import plugin_contributions
import plugin_runtime
import probe_cache as pc
import runtime_mode
import service_manager as sm
import settings_store
import storage_dispatch as sd
import tailscale_view as tv
import upgrade_prep
import version_service as vs
from auth import register_auth_hooks
from live_snapshot import SnapshotCache
from plugin_context import PluginContext

# C10-b：settings.yaml 的白名单键注入 os.environ（env 已设的跳过 → env > yaml > 默认）。
# 必须在任何「读 env 到模块常量」的代码之前跑：现白名单三项都是每次现读的键，
# 放在 import 区末尾即可；将来纳入 import 期读取的键要重新审视这个位置。
settings_store.apply_to_env()

app = Flask(__name__, static_folder=None)
register_auth_hooks(app)
CORS(app)

logger = logging.getLogger("console.backend")

DB_PATH = paths.DB_PATH
_services_page_cache = SnapshotCache(ttl=pc.probe_interval)
STATIC_DIR = str(composition.root_path("static"))

def _resolve_assets_path():
    return str(composition.config_path("assets.yaml", "CONSOLE_ASSETS_PATH"))

ASSETS_PATH = _resolve_assets_path()


def _resolve_panels_path():
    return str(composition.config_path("panels.yaml", "CONSOLE_PANELS_PATH"))


PANELS_PATH = _resolve_panels_path()
CONSOLE_API_PATHS = {
    "/api/services", "/api/oplog", "/api/config", "/api/machines",
    "/api/machine/action",
    "/api/service/action", "/api/service", "/api/monitor", "/api/observability",
    "/api/health", "/api/plugins",
    "/api/service/suggest-access", "/api/service/access-check",
    # B7-3：`/api/panels` 与 `/api/renewals` 由 plugins/panels、plugins/renewals
    # 的 manifest `api_prefixes` 声明，kernel 不重复登记——否则零插件组合下
    # kernel 会对不存在的能力声称「这是控制台自有接口」。
    "/api/attention", "/api/ops-status",
    "/api/inspection/report",
    # C2/W2：tailnet 只读视图（LocalAPI）
    "/api/tailnet",
    # C10-a：设置页只读「有效配置」
    "/api/config/effective",
    # C10-b：设置页可写项（GET/PUT，白名单写回 settings.yaml）。
    # 刻意不叫 /api/settings——那是 Beszel（PocketBase）的端点，内嵌 Beszel 前端
    # 会经同源根路径请求它，占了名字会把它的设置页打断。
    "/api/config/settings",
    # C8/D1'：总控台布局 override 升库（GET/PUT）
    "/api/dashboard-layout",
    # C11：入口上游小卡（Caddy admin API 经 NAS crontab 导出，只读）
    "/api/caddy/entry",
    # B7-3：`/api/events` 由 plugins/audit 的 manifest `api_prefixes` 声明，
    # app.py 里没有对应路由；kernel 不重复登记。
    # 磁盘增长趋势与设备主机历史（正式监控源，只读）
    "/api/capacity", "/api/monitor/history",
    # 保留旧路径归属，防止退役的动作接口被 Beszel /api 代理接管。
    "/api/trigger",
    # B4-3：监控接收 API（Go Agent 影子路径，认证/限流/版本协商在 monitoring_api.py）
    "/api/monitoring/v1/ingest",
    "/api/monitoring/v1/enrol",
    "/api/monitoring/v1/revoke",
}


def _is_console_api(path):
    if plugin_runtime.owns_api(path) or plugin_runtime.matches(path, "/api/plugins"):
        return True
    if path in CONSOLE_API_PATHS:
        return True
    # B7-3：`/api/oplog/` 与 `/api/panels/` 分别是 plugins/audit、plugins/panels
    # 的前缀，已由函数第一行的 `plugin_runtime.owns_api()` 按 manifest 判定。
    # 这里只保留 kernel 自己的动作前缀。
    if path.startswith("/api/trigger/"):
        return True
    # /api/service/* 与 /api/services/* 全部是控制台自有接口，Beszel 无此前缀
    if path.startswith("/api/service/") or path.startswith("/api/services/"):
        return True
    # B3：/api/machines/<id> 的单条读写（Beszel 无 /api/machines 前缀）
    if path.startswith("/api/machines/"):
        return True
    # B7-3：`/api/bookmarks`、`/api/feed`、`/api/renewals`、`/api/ledger` 原先在
    # 这里写死成私人插件的保留前缀。它们由对应 manifest 的 `api_prefixes` 声明，
    # 函数第一行的 `plugin_runtime.owns_api()` 已经覆盖；再写一份会让公共候选
    # 在没有任何插件时仍然认领私人路由。
    return False

def get_db_conn():
    """legacy SQLite 连接（过渡期专用）。

    PG-02 起在线业务不再走这里；保留给 `CONSOLE_STORAGE_ENGINE=sqlite` 的过渡期
    与既有测试。PG 模式下调用它是编程错误，直接报错而不是悄悄连 SQLite（D3）。
    """
    if sd.use_postgres():
        raise RuntimeError("PG 模式下不得使用 legacy SQLite 连接（在线业务已迁移）")
    return dbmod.get_connection(DB_PATH)


def init_db():
    """SQLite 过渡期建表 + 迁移。

    PG 下**什么都不做**：schema 由部署阶段的 `scripts/pg_schema.py migrate` 应用
    （§6.3 运行账号没有 DDL 权限），应用启动不建库、不建表。
    """
    if sd.use_postgres():
        return
    conn = get_db_conn()
    cursor = conn.cursor()
    # 操作日志表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS oplog (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        host TEXT,
        svc TEXT,
        action TEXT,
        started_at TEXT,
        finished_at TEXT,
        result TEXT,
        operator TEXT DEFAULT 'self'
    )""")
    # 运行态版本表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS versions (
        svc_name TEXT PRIMARY KEY,
        local_version TEXT,
        upstream_version TEXT,
        behind_count INTEGER,
        update_status TEXT,
        recent_changes TEXT,
        last_checked TEXT
    )""")
    # 面板独立剪切板（高频变更运行时数据，不进 git，随 console.db 备份）
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS panel_scratch (
        panel_id INTEGER PRIMARY KEY,
        content TEXT,
        updated_at TEXT
    )""")
    # workstation 保留：bookmarks / feed_cache（agents / projects 由迁移 drop）
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bookmarks (
      id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
      icon TEXT DEFAULT '', category TEXT DEFAULT 'default', tags TEXT DEFAULT '[]',
      sort_order INTEGER DEFAULT 0,
      created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now'))
    )""")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS feed_cache (
      id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL,
      url TEXT DEFAULT '', summary TEXT DEFAULT '', icon TEXT DEFAULT '',
      published_at TEXT DEFAULT '', fetched_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.commit()
    conn.close()
    dbmod.run_migrations(DB_PATH)


def _utc_now_text():
    """`YYYY-MM-DD HH:MM:SS` UTC 文本，与两套 schema 的默认值同格式。"""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _bg_sync_versions():
    """周期版本同步：只由 authority 运行（Q1）。"""
    if not collect_guard.collection_allowed():
        logger.info(collect_guard.collection_skipped("后台版本同步"))
        return
    import threading

    def run():
        try:
            from version_sync import sync_all_versions
            sync_all_versions(db_path=DB_PATH)
            _services_page_cache.invalidate()
        except Exception:
            logger.exception("background version sync failed")

    threading.Thread(target=run, daemon=True).start()


def _schema_gate():
    """启动期 schema 兼容性闸门：不兼容就**有界失败**（非零退出，不重试）。

    提案 §5.1：app 启动仅检查 engine/schema 兼容性，不建表、不自动 seed、不触发迁移；
    「Mac 旧代码遇不兼容版本必须拒绝业务写入并提示更新」。§7 失败矩阵把
    「schema 不兼容有界失败」与「DB 不可达」并列为启动期必判的两类故障。

    只有 NAS 部署/迁移账号执行版本升级；这里**从不**尝试修复目标库——旧代码对
    新版 schema 写入可能损坏数据，正确动作是停下等人升级镜像，而不是连上去接着写。

    退出码与 `_startup()` 的既有约定区分：2 = PG 不可达，3 = schema 不兼容，
    4 = 存储引擎未声明/错配（见 `_require_declared_storage_engine`）。
    读法：`verify()` 只读且不吞异常，任何抛错都视为「无法确认兼容」（fail closed）。
    """
    import pg_migrations

    component_report = None
    try:
        # 显式组合选择组件谱系；默认私人遗产入口在 B7-4 接回前沿用旧闸门。
        component_mode = composition.composition_file() is not None
        with dbmod.pg_transaction(readonly=True) as conn:
            if component_mode:
                import component_migrations

                component_report = component_migrations.registry_report(conn)
            else:
                report = pg_migrations.verify(conn)
    except Exception as exc:
        logger.critical(
            "PostgreSQL schema 兼容性无法确认，拒绝启动（只读核对失败：%s）",
            type(exc).__name__,
        )
        raise SystemExit(3) from exc
    if component_mode:
        if (component_report["registry_present"] and component_report["ok"]
                and component_report["components"]):
            logger.info("PostgreSQL 组件 schema 兼容（登记行=%s）", len(component_report["components"]))
            return
        kinds = sorted({issue["kind"] for issue in component_report["issues"]})
        logger.critical(
            "PostgreSQL schema 不兼容，拒绝启动：组件登记核验失败（%s）；"
            "请由部署/迁移账号运行 scripts/pg_component_schema.py check/migrate/adopt，"
            "本进程不自动迁移。", ",".join(kinds) or "登记缺失",
        )
        raise SystemExit(3)
    if report["compatible"]:
        logger.info(
            "PostgreSQL schema 兼容（version=%s checksum=%s）",
            report["schema_version"],
            (report["checksum"] or "")[:12],
        )
        return
    # 提示必须可读且**不含 DSN/凭据**：只说期望与实际，指到修复工具。
    reasons = []
    if report["schema_version"] != report["expected_schema_version"]:
        reasons.append(
            f"schema 版本不匹配：代码期望 {report['expected_schema_version']}，"
            f"目标库为 {report['schema_version']}"
        )
    if not report["checksum_ok"]:
        reasons.append("schema12 checksum 与代码不符（已发布迁移被改动或迁到别的库）")
    if report["tables_missing"]:
        reasons.append(f"缺表 {report['tables_missing']}")
    logger.critical(
        "PostgreSQL schema 不兼容，拒绝启动：%s。本进程代码只支持 schema %s；"
        "请先用部署/迁移账号运行 scripts/pg_schema.py migrate 升级目标库，"
        "或把本进程升级到与目标库匹配的版本。**不自动迁移、不降级到 SQLite**。",
        "；".join(reasons) or "未知原因",
        report["expected_schema_version"],
    )
    raise SystemExit(3)


# 在线入口的存储引擎闸门（§11.3.2 G06；决策 D3 / D7）。
# 退出码 4 与 2（PG 不可达）、3（schema 不兼容）并列：它们都是启动期故障，
# 但「配置根本没说」与「说了连不上」要能分开看，否则运维会去查网络而问题在 env。
STORAGE_ENGINE_DECLARATION_EXIT_CODE = 4


def _require_declared_storage_engine():
    """在线入口必须**显式声明** `CONSOLE_STORAGE_ENGINE`；未声明一律拒绝启动。

    §11.3.2 的 G06 关闭标准：生产在线入口强制 PG，缺失/错配配置 fail closed，
    SQLite 只作**明确声明**的离线工具/测试路径。此前 `runtime_mode` 的默认值是
    `sqlite`，于是「忘记配置」被静默解释成「用本地 SQLite 跑在线服务」——
    G06b 首红实测：无任何 PG 配置时 app 启动成功、`/api/health` 返回 200，
    且启动过程在副本里创建了 163840 字节的 `console.db`。

    这里**只做声明与共存检查**，不替 `runtime_mode` 决定路由（那是 PG-00 的面，
    `DEFAULT_STORAGE_ENGINE` 保持不动）。两条判据：

    1. 未声明（含空串）→ 拒绝。SQLite 仍可用，但必须写出来。
    2. `sqlite` + **非 `test`** 角色共存 → 拒绝。SQLite 只服务**明确声明的**
       离线工具/测试路径；`authority`（NAS，采集并写业务流水）与 `client`
       （Mac 在线页面）都必须在线上指向同一 PostgreSQL —— D3 原文：「NAS/Mac
       指向同一 PostgreSQL；断连时不回退到可写 SQLite」。放行非 `test` 角色
       等于允许「Mac 页面写本地 SQLite、与权威端分叉」，正是 D7 要取消的
       双运行路径。`client` 是 `instance_role()` 的默认值，所以「不写角色」
       同样落进本判据——这与判据 1 同一个道理：**必须显式声明**。
    """
    declared = (os.environ.get("CONSOLE_STORAGE_ENGINE") or "").strip()
    if not declared:
        logger.critical(
            "未声明 CONSOLE_STORAGE_ENGINE，拒绝启动：在线入口不再隐式回落到 SQLite（D3/D7）。"
            "生产部署在环境/Compose 里显式设 `CONSOLE_STORAGE_ENGINE=postgres`"
            "（并设 `CONSOLE_INSTANCE_ROLE=authority`）；离线工具与测试显式设 `sqlite`。"
        )
        raise SystemExit(STORAGE_ENGINE_DECLARATION_EXIT_CODE)
    if (
        runtime_mode.storage_engine() == runtime_mode.STORAGE_ENGINE_SQLITE
        and runtime_mode.instance_role() != runtime_mode.INSTANCE_ROLE_TEST
    ):
        logger.critical(
            "CONSOLE_STORAGE_ENGINE=sqlite 只允许与 CONSOLE_INSTANCE_ROLE=test 共存，拒绝启动："
            "D3 要求 NAS 与 Mac 指向同一 PostgreSQL、断连时也不回退到可写 SQLite（当前角色：%s）。"
            "在线运行请设 `CONSOLE_STORAGE_ENGINE=postgres`；"
            "离线预览/测试请显式声明 `CONSOLE_INSTANCE_ROLE=test`。",
            runtime_mode.instance_role(),
        )
        raise SystemExit(STORAGE_ENGINE_DECLARATION_EXIT_CODE)


def _startup():
    """进程启动序列：引擎声明闸门 → PG 池 → schema 闸门 → 建表（仅 SQLite）→ seed → 采集线程（仅 authority）。

    PG 配置缺失或连不上时**直接失败**，不退回本地 SQLite（D3/D10）。
    """
    _require_declared_storage_engine()
    composition_identity.verify_static(STATIC_DIR)
    if composition.instance_profile() == composition.PROFILE_PUBLIC:
        if (os.environ.get("CONSOLE_INSTANCE_ROLE") or "").strip() not in {"authority", "client"}:
            raise composition.CompositionError("公共实例必须显式声明 authority 或 client")
        if runtime_mode.storage_engine() != runtime_mode.STORAGE_ENGINE_POSTGRES:
            raise composition.CompositionError("公共实例只支持 PostgreSQL")
    try:
        runtime_mode.require_client_authority()
    except runtime_mode.ConfigError as exc:
        logger.critical("client 权威端配置无效，拒绝启动：%s", exc)
        raise SystemExit(STORAGE_ENGINE_DECLARATION_EXIT_CODE) from exc
    if sd.use_postgres():
        try:
            dbmod.pg_open(wait=True, timeout=10)
            logger.info("PostgreSQL 连接池已就绪（role=%s）", runtime_mode.instance_role())
        except Exception as exc:
            logger.critical("PostgreSQL 不可用，拒绝以 SQLite 降级启动：%s", type(exc).__name__)
            raise SystemExit(2) from exc
        # 池就绪后、任何业务读写之前先核对 schema（§5.1/§7）。
        _schema_gate()
    init_db()
    plugin_runtime.initialize()
    _bg_sync_versions()
    _bg_workstation_tasks()
    _bg_machine_probes()
    _bg_public_probes()
    plugin_runtime.start_jobs()
    plugin_runtime.start_services()


def _sample_node_traffic(payload):
    """把监控快照里的网卡累计量记进流量账本（E1）。

    命中缓存时计数器和上次一样，增量为 0，重复调用无副作用；记账失败绝不影响监控返回。
    **非 authority 不采样**（Q1）：GET /api/monitor 的隐式写在这里被挡掉。
    """
    if not collect_guard.collection_allowed():
        return
    try:
        nt.record_from_systems(payload.get("systems") or [], db_path=DB_PATH)
    except Exception:
        logger.exception("node traffic sampling failed")


def _sample_node_traffic_pg():
    """console_pg 源的流量记账（B6-3/Q3 顺延）：从 PG 样本记 machine_traffic。

    与 ``_sample_node_traffic``（Beszel 链）互斥，由 ``_traffic_sampler`` 按
    ``console_pg`` 链的独立后台记账；旧 Beszel 运行分支已退役。
    页面钩子（/api/monitor 缓存命中后）和后台 900s 循环两处触发；PG 链**只有
    后台循环**——console_pg 下页面钩子不补记（Q3 页面分支既成语义），15min
    一轮的后台循环已覆盖无人开页面的计数器推进，页面少了这一跳不缺账。
    异常纪律与 Beszel 链一致：logger.exception，不拖垮循环线程。
    """
    if not collect_guard.collection_allowed():
        return
    try:
        mb.record_traffic_from_pg()
    except Exception:
        logger.exception("node traffic sampling (console_pg) failed")


def _traffic_sampler():
    """后台流量记账只由 Console PG 样本驱动。"""
    _sample_node_traffic_pg()


def _bg_workstation_tasks():
    """周期任务：feed 15min、节点流量记账 15min。只由 authority 运行（Q1）。"""
    if not collect_guard.collection_allowed():
        logger.info(collect_guard.collection_skipped("后台 feed/流量任务"))
        return
    import threading

    def loop(interval_seconds, fn):
        def run():
            while True:
                try:
                    fn()
                except Exception:
                    # 循环内吃异常保线程存活，但失败必须可观测（不再静默 pass）
                    logger.exception("background workstation task failed: %s", getattr(fn, "__name__", fn))
                time.sleep(interval_seconds)

        threading.Thread(target=run, daemon=True).start()

    # 流量账本不能只靠 /api/monitor 被动触发：没人开页面时计数器照样在涨，
    # 中间遇上 agent 重装就丢一段。900s > 快照 TTL 600s，每轮都是新鲜数据。
    # B6-4：PG 是唯一正式来源；agent 采样周期 300s < 900s。
    loop(900, _traffic_sampler)


def _bg_machine_probes():
    """探针半点调度（E4）：按 machine_probe_interval 对齐到本地时间节点
    （默认每半小时 00:00 / 00:30 / …）强制探一轮台账机器。

    探测不再依赖有人调 /api/machines：没人开页面色带也按节点推进，
    probe_history 的采样点从此落在整刻度上，悬浮能直接报「00:30」这样的节点。

    只由 authority 运行（Q1）：Mac 不调度探针，也就不会采样或清理。
    """
    if not collect_guard.collection_allowed():
        logger.info(collect_guard.collection_skipped("后台探针调度"))
        return
    import threading

    def run():
        while True:
            try:
                nxt = pc.next_probe_slot()
                # 多睡 0.5s 保证醒来时已越过节点线，记录的 ts 展示成节点本身
                time.sleep(max(0.5, (nxt - datetime.datetime.now()).total_seconds() + 0.5))
                pc.probe_all_machines(db_path=DB_PATH)
                # C10-a 自监控：每轮探测完更新内存时间戳，供 /api/config/effective
                # 与 attention 两周期未跑告警消费（评审新建议 D）
                effective_config.mark_probe_run()
                # C10-b 数据管理：oplog 是唯一没有清理逻辑的表。保留天数默认 0
                # （不清理，与历史行为一致），设了正数才在这里顺带删旧行——
                # 不为它单起线程，也不在 GET 里做副作用。
                settings_store.prune_oplog(db_path=DB_PATH)
                # Track K 同款保留策略：删已恢复的旧事件（活动事件永不删），
                # 默认 0 = 不清理。与 oplog 共用这一轮，不为它单起线程。
                monitor_events.prune(db_path=DB_PATH)
            except Exception:
                # 循环内吃异常保线程存活，失败必须可观测
                logger.exception("background machine probe failed")
                time.sleep(60)

    threading.Thread(target=run, daemon=True).start()


def _bg_public_probes():
    """家宽公网探测调度：每 public_probe_interval（默认 300s）强制探一轮
    配了 public_probe 的机器。先探再睡，启动后第一轮立即出结果。

    不与半点探针合并：两者间隔不同（公网封堵常是几十分钟级的间歇），且公网
    不通时单台要等满超时，不能拖慢机器探活那一轮。只由 authority 运行（Q1）。
    """
    if not collect_guard.collection_allowed():
        logger.info(collect_guard.collection_skipped("家宽公网探测调度"))
        return
    import threading

    def run():
        while True:
            try:
                pc.probe_all_public(db_path=DB_PATH)
            except Exception:
                logger.exception("background public probe failed")
            time.sleep(pc.public_probe_interval())

    threading.Thread(target=run, daemon=True).start()


@app.route("/api/config", methods=["GET"])
def get_config():
    """前端启动期读的全局配置：鉴权开关、Beszel 同源入口、对外地址、探测间隔与机器下拉项。"""
    from auth import auth_enabled
    public = os.environ.get("CONSOLE_PUBLIC_URL", "").rstrip("/")
    return jsonify({
        "auth_enabled": auth_enabled(),
        "public_url": public or None,
        "probe_interval": pc.probe_interval(),
        "machines": mc.machines_for_config(),
    })

def _assets_machine_warnings():
    """assets.yaml 引用了台账里没有的机器 id → 两份 yaml 漂移，健康检查里点出来。"""
    try:
        with open(ASSETS_PATH, "r", encoding="utf-8") as file:
            services = yaml.safe_load(file) or []
        unknown = mc.unknown_machine_ids(services)
    except (OSError, yaml.YAMLError):
        return []
    if not unknown:
        return []
    return [
        "assets.yaml 引用了 machines.yaml 未定义的机器: " + ", ".join(unknown)
    ]


@app.route("/api/health", methods=["GET"])
def health():
    """存活探针。assets.yaml 引用了 machines.yaml 未定义的机器时附 warnings（两份 yaml 漂移点名）。"""
    try:
        runtime_mode.require_client_authority(request.host_url)
    except runtime_mode.ConfigError as exc:
        return jsonify({"error": str(exc)}), 503
    payload = {"message": "API is healthy.", "code": 200, "data": {}}
    warnings = _assets_machine_warnings()
    if warnings:
        payload["warnings"] = warnings
    return jsonify(payload)


@app.route("/api/machines", methods=["GET"])
def get_machines():
    """机器台账 + node 探活 + 续费关联（契约见 proposals 2026-08-29 §4.2）。"""
    force = request.args.get("force") == "1"
    result = []
    for machine in mc.machines_for_api():
        status, msg, ts = pc.check_machine_probe(
            machine["id"], force=force, db_path=DB_PATH
        )
        result.append({
            **machine,
            "probe_status": status,
            "probe_msg": monitor_events.scrub_detail(msg),  # P1：client 投影脱敏 IPv4
            "probe_ts": ts,
            "renewal": _machine_renewal(machine["id"]),
            # E1/E4：月流量账本与探测时序，都是「有就带、没有就 null/空」
            "traffic": _machine_traffic(machine["id"]),
            "probe_history": _machine_probe_history(machine["id"]),
            # 家宽公网探测：只读后台调度的缓存，请求路径不发起探测
            **_machine_public_probe(machine["id"]),
            # E3：该机器可用的白名单动作（只有 key + 文案，命令不外泄）
            "actions": sm.node_actions_for(machine["id"]),
        })
    return jsonify(result)


@app.route("/api/machines/<machine_id>", methods=["GET"])
def get_machine_detail(machine_id):
    """单台机器。`?full=1` 给编辑表单，含 tailscale/ssh/ssh_key（私钥仍只是文件名）。"""
    if request.args.get("full") == "1":
        entry = mc.machine_for_edit(machine_id)
        if not entry:
            return jsonify({"ok": False, "error": f"机器不存在: {machine_id}"}), 404
        # Hub 已退役，没有在线 unit 发现源；保留白名单文本编辑与返回字段。
        entry["discovered_units"] = []
        return jsonify(entry)

    target = str(machine_id or "").strip().lower()
    entry = next((m for m in mc.machines_for_api() if m["id"] == target), None)
    if not entry:
        return jsonify({"ok": False, "error": f"机器不存在: {machine_id}"}), 404
    status, msg, ts = pc.check_machine_probe(target, db_path=DB_PATH)
    return jsonify({
        **entry,
        "probe_status": status,
        "probe_msg": monitor_events.scrub_detail(msg),  # 与列表投影同一条脱敏契约
        "probe_ts": ts,
        "renewal": _machine_renewal(target),
        "traffic": _machine_traffic(target),
        "probe_history": _machine_probe_history(target),
        **_machine_public_probe(target),
        "actions": sm.node_actions_for(target),
    })


@app.route("/api/machines", methods=["POST"])
def machine_create():
    """新增机器台账（写回 machines.yaml）。"""
    try:
        entry = mc.upsert_machine(request.json or {}, create=True)
        _log_op("machines", entry["id"], "machine_create", True)
        return jsonify({"ok": True, "machine": mc.machine_for_edit(entry["id"])})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/machines/<machine_id>", methods=["PUT"])
def machine_update(machine_id):
    """更新机器台账。id 不可改名（关联记录会孤儿化），由 machine_config 拒绝。"""
    try:
        entry = mc.upsert_machine(request.json or {}, machine_id=machine_id)
        _log_op("machines", machine_id, "machine_update", True)
        return jsonify({"ok": True, "machine": mc.machine_for_edit(entry["id"])})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/machines/<machine_id>", methods=["DELETE"])
def machine_delete(machine_id):
    """删除机器台账。机器上还有服务时以中文 400 拒绝并列名（§5.3 删除防呆）。"""
    try:
        target = str(machine_id or "").strip().lower()
        services = sm.load_assets(ASSETS_PATH)
        attached = mc.services_on_machine(services, target)
        if attached:
            return jsonify({
                "ok": False,
                "error": (
                    f"机器 {target} 上还有 {len(attached)} 个服务，请先迁移或删除："
                    + "、".join(attached)
                ),
            }), 400
        mc.delete_machine(target)
        _log_op("machines", target, "machine_delete", True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/machines/<machine_id>/discover", methods=["GET"])
def machine_discover(machine_id):
    """只读扫描本机容器，标记台账未登记的候选（前端勾选后走既有 POST /api/services 登记）。

    扫描命令在 service_manager 里写死（docker ps -a），不接收任何参数；
    无 docker 能力的机器 400。本端点只读，不写台账、不记 oplog（登记走原有端点）。
    """
    try:
        items = sm.discover_containers(machine_id)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    try:
        services = sm.load_assets(ASSETS_PATH)
    except Exception:
        services = []
    registered = {sm.normalize_container_name(s.get("container") or s.get("name")) for s in services}
    candidates = [c for c in items if sm.normalize_container_name(c["name"]) not in registered]
    return jsonify({
        "ok": True,
        "machine": machine_id,
        "total": len(items),
        "registered": len(items) - len(candidates),
        "candidates": candidates,
    })


def _machine_traffic(machine_id):
    try:
        return nt.usage(machine_id, db_path=DB_PATH)
    except Exception as exc:
        # 表缺失按「没有账本」处理；连接不可达/权限不足向上抛 → 503（不伪造零值）
        if sd.is_missing_table(exc):
            return None
        raise


def _machine_probe_history(machine_id):
    # 没配 probe 的机器永远没有时序，不必为它开一次连接
    if not (mc.get_machine(machine_id) or {}).get("probe"):
        return []
    try:
        return node_probe.history(machine_id, db_path=DB_PATH)
    except Exception as exc:
        if sd.is_missing_table(exc):
            return []
        raise


def _machine_public_probe(machine_id):
    """家宽公网探测的 API 投影（四个键恒在，没配就是 unknown/空/None/[]）。

    结果只取 ``pc.peek_machine_public`` 的缓存：公网不通时一次探测要等满超时，
    放进请求路径会拖住整页。msg 与 probe_msg 走同一条 IPv4 脱敏契约。
    """
    if not (mc.get_machine(machine_id) or {}).get("public_probe"):
        return {
            "public_probe_status": "unknown",
            "public_probe_msg": "",
            "public_probe_ts": None,
            "public_probe_history": [],
        }
    status, msg, ts = pc.peek_machine_public(machine_id)
    try:
        history = node_probe.history(pc.public_history_key(machine_id), db_path=DB_PATH)
    except Exception as exc:
        if not sd.is_missing_table(exc):
            raise
        history = []
    return {
        "public_probe_status": status,
        "public_probe_msg": monitor_events.scrub_detail(msg),
        "public_probe_ts": ts,
        "public_probe_history": history,
    }


def _machine_renewal(machine_id):
    # Compatibility response field; business ownership remains in the contribution.
    return plugin_contributions.collect('machine_summary', machine_id=machine_id, db_path=DB_PATH).get('renewal')


@app.route("/api/attention", methods=["GET"])
def get_attention():
    """今日关注：聚合 probe / 版本 / feed / 流量 / 续费的既有状态，不新探测、不写库。"""
    return jsonify(attention.collect_attention(ASSETS_PATH, DB_PATH))


@app.route("/api/tailnet", methods=["GET"])
def get_tailnet():
    """C2/W2：tailnet 只读视图（NAS crontab 导出的 status.json）。

    数据源不可用（文件缺失 / 坏 JSON）返回 available=false 显式降级，不抛 500；
    tailscale IP 只用于服务端映射机器 id，不下发（e329e27）。
    2026-09-06：数据源由 LocalAPI socket 直连改为 cron 导出文件（socket 直连仅作
    未配置文件源时的回退路径），口径以 tailscale_view 模块 docstring 为准。
    """
    return jsonify(tv.get_tailnet_snapshot())


@app.route("/api/ops-status", methods=["GET"])
def get_ops_status():
    """运维状态七键：running commit、版本采集、feed 新鲜度、备份 tag、部署留痕、探针上次运行、定时备份。

    拿不到的字段一律显式 None，不猜、不用当前时间冒充采样时间（口径见 ops_status 模块）。
    """
    return jsonify(ops_status.get_ops_status(DB_PATH))


# ==================== workstation 移植 API ====================

def _parse_json_field(value, fallback):
    """DB 中 JSON 字符串字段解析为对象，解析失败降级为 fallback。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return fallback
    return value


@app.route("/api/monitor", methods=["GET"])
def get_monitor():
    """Console PG 主机/容器监控快照。"""
    return jsonify(mb.monitor_snapshot())


def _snapshot_response(snapshot):
    response = jsonify(snapshot.data)
    response.headers["X-Snapshot-At"] = snapshot.updated_at or ""
    response.headers["X-Snapshot-Refreshing"] = "1" if snapshot.refreshing else "0"
    response.headers["X-Snapshot-Error"] = "1" if snapshot.error else "0"
    return response


@app.route("/api/observability", methods=["GET"])
def get_observability():
    """Console PG 事件快照；未覆盖能力显式标为 unsupported。"""
    return jsonify(mb.observability_snapshot())


@app.route("/api/service/suggest-access", methods=["GET"])
def suggest_access():
    """按机器与端口推导建议的访问地址 `{home, tailscale}`，供服务表单预填。

    只有家庭网段机器才给 `home`，只有台账里有 tailscale 地址的机器才给 `tailscale`，
    推不出的那项返回空串；端口缺失或非整数时两项都空。
    """
    machine = request.args.get("machine") or mc.local_machine()
    port = request.args.get("port", "")
    path_suffix = request.args.get("path_suffix", "")
    return jsonify(mc.suggest_access(machine, port, path_suffix))


@app.route("/api/service/access-check", methods=["POST"])
def access_check():
    """对 home / tailscale / domain 三个访问入口逐个探活，供服务表单即时校验。

    每项返回 `{configured, ok, msg}`：未填的项 `configured=False` 且 `ok=None`，
    与「填了但探不通」（`ok=False`）区分开。
    """
    data = request.json or {}
    return jsonify(ah.probe_access(
        data.get("home", ""),
        data.get("tailscale", ""),
        data.get("domain", ""),
    ))


@app.route("/api/service/<name>/version", methods=["GET"])
def service_version_detail(name):
    """单个服务的版本详情（本地 / 上游 / 落后数 / 状态 / 最近变更）。

    默认读 versions 表缓存；`?sync=1` 或缓存里的本地版本是占位值（`N/A` / `latest` /
    `Running (Docker)` 等）时强制拉一次上游。服务不在台账里 404。
    """
    force = request.args.get("sync") == "1"
    detail = vs.get_service_version_detail(name, force_sync=force)
    if not detail:
        return jsonify({"error": f"未找到服务 {name}"}), 404
    svc = _find_service(name)
    detail["upgrade_prep"] = upgrade_prep.build_upgrade_prep(name, detail, svc)
    return jsonify(detail)


@app.route("/api/service/<name>/upgrade-review", methods=["POST"])
def service_upgrade_review(name):
    """持久化单服务的升级准备审阅：六项检查勾选 + 状态（忽略/延后必填理由）。

    状态在 `service_upgrade_reviews` 表按服务名 upsert；不触发任何升级动作。
    """
    svc = _find_service(name)
    if not svc:
        return jsonify({"error": f"未找到服务 {name}"}), 404
    data = request.json or {}
    try:
        review = upgrade_prep.set_review(name, data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "review": review})


@app.route("/api/services", methods=["GET"])
def get_services():
    """获取所有服务台账，混入健康检查、版本状态、运行态。"""
    force = request.args.get("force") == "1"
    if not os.path.exists(ASSETS_PATH):
        return jsonify({"error": "assets.yaml not found"}), 404

    stat = os.stat(ASSETS_PATH)
    key = (ASSETS_PATH, DB_PATH, stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
    snapshot = _services_page_cache.get(
        lambda: _build_services(force), key=key, force=force
    )
    return _snapshot_response(snapshot)


def _build_services(force=False):
    """Produce one complete service list for the page snapshot cache."""

    with open(ASSETS_PATH, "r", encoding="utf-8") as f:
        services = yaml.safe_load(f) or []

    with sd.read_tx(db_path=DB_PATH) as conn:
        v_rows = {
            row["svc_name"]: dict(row)
            for row in conn.execute("SELECT * FROM versions").fetchall()
        }

    # 批量查询容器运行状态
    status_map = sm.get_all_status(services, force=force)

    result = []
    for svc in services:
        name = svc.get("name")

        # 健康检查（缓存，默认 10 分钟）
        health_status, health_msg = pc.check_service_health(svc, force=force)

        # 版本信息
        v_info = v_rows.get(name, {})

        # 运行态
        running_status = sm.service_running(svc, status_map)

        health = {"status": health_status, "msg": health_msg}
        health_detail = pc.peek_health_detail(name) or {}
        ssl_ok = health_detail.get("ssl_ok")
        ssl_days = health_detail.get("ssl_days")
        if (
            ssl_ok is True
            and not isinstance(ssl_days, bool)
            and isinstance(ssl_days, (int, float))
            and math.isfinite(ssl_days)
        ):
            health["ssl_ok"] = True
            health["ssl_days"] = ssl_days

        svc_data = {
            **svc,
            "health": health,
            "running": running_status,
            # T12 分支实验：服务健康时序（迷你色带）；无记录给空数组
            "health_history": _service_health_history(name),
            "version_info": {
                "local": v_info.get("local_version", "N/A"),
                "upstream": v_info.get("upstream_version", "N/A"),
                "behind": v_info.get("behind_count", 0),
                "status": v_info.get("update_status", "Unknown"),
                "changes": json.loads(v_info.get("recent_changes", "[]")) if v_info.get("recent_changes") else []
            }
        }
        if cr.is_caddy_service(svc):
            proxy_routes, proxy_error = cr.get_service_routes(svc, force=force)
            svc_data["proxy_routes"] = proxy_routes
            if proxy_error:
                svc_data["proxy_error"] = proxy_error
        result.append(svc_data)

    # C5 → L4 入口维度：域名 → 边缘 → 内网。零新探测——只用台账 access 字段
    # 与 C11 已有的 Caddy upstream 快照（600s TTL 的 cron 导出文件，整批只读一次）。
    entry_view.attach_entries(result, cv.get_caddy_snapshot())

    return result


def _service_health_history(name):
    """服务健康时序（T12 分支实验）；查询失败给空数组，不影响服务列表。"""
    try:
        import service_health_history as shh

        return shh.history(name, db_path=DB_PATH)
    except Exception:
        return []


@app.route("/api/oplog", methods=["POST", "PUT", "PATCH", "DELETE"])
@app.route("/api/oplog/", defaults={"subpath": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.route("/api/oplog/<path:subpath>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def retired_oplog_callback(subpath=""):
    """OliveTin 审计回调已退役，避免旧请求被其他 /api 代理接管。"""
    return jsonify({"error": "audit callback retired"}), 410


@app.route("/api/trigger", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.route("/api/trigger/", defaults={"subpath": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.route("/api/trigger/<path:subpath>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def retired_action_trigger(subpath=""):
    """OliveTin 动作触发接口已退役。"""
    return jsonify({"error": "action trigger retired"}), 410


def _log_op(host, svc, action, result_ok, detail=""):
    """写入操作审计日志。

    **失败不向上抛**（提案 §4.2）：业务已经提交后再因为审计写不进去而 500，
    会让调用方以为业务失败并重试。审计失败只记日志。
    """
    now = _utc_now_text()
    try:
        ph = sd.ph()
        with sd.write_tx(db_path=DB_PATH) as conn:
            conn.execute(
                f"INSERT INTO oplog (host, svc, action, started_at, finished_at, result)"
                f" VALUES ({ph},{ph},{ph},{ph},{ph},{ph})",
                (host, svc, action, now, now,
                 "Success" if result_ok else f"Failed: {detail[:120]}"),
            )
    except Exception as exc:
        logger.error("oplog write failed: action=%s category=%s", action, type(exc).__name__)


# ---- 存储异常 → HTTP 状态码（提案 §4.1 的映射表，只此一处） ----------------


def _storage_error_response(exc):
    """把存储层异常映射成 (payload, status)。

    映射表见 PG-00 `interfaces.md` §3：不可达/维护 → 503、并发冲突 → 409、
    配置/权限 → 500、金额/协议 → 400。**响应不含 DSN、密码或堆栈**。
    """
    import pgdb
    from runtime_mode import ReadOnlyMode

    if isinstance(exc, ReadOnlyMode):
        return {"error": "系统处于维护模式，暂时不能写入，请稍后重试"}, 503
    if isinstance(exc, runtime_mode.AuthorityUnavailable):
        return {"error": str(exc)}, 503
    if isinstance(exc, pgdb.StorageUnavailable):
        return {"error": "数据库暂时不可用，请稍后重试"}, 503
    if isinstance(exc, pgdb.ConcurrentConflict):
        return {"error": "操作与其它请求冲突，请刷新后重试"}, 409
    if isinstance(exc, (pgdb.PgConfigError, pgdb.PgPermissionError)):
        return {"error": "数据库配置或权限异常，请联系管理员"}, 500
    if isinstance(exc, pgdb.CommitOutcomeUnknown):
        return {"error": "提交结果未知，请刷新页面确认后再操作"}, 503
    try:
        from contract_errors import ContractError
        from money import MoneyError
    except ImportError:  # pragma: no cover - 依赖缺失时不吞异常
        MoneyError = ContractError = ()
    if MoneyError and isinstance(exc, MoneyError):
        return {"error": str(exc)}, 400
    if ContractError and isinstance(exc, ContractError):
        return {"error": str(exc)}, 400
    raise exc


def _handle_storage_error(exc):
    """存储/维护异常的统一出口。

    只注册 `pgdb.PgError`（含全部子类）与 `ReadOnlyMode`，**不注册 `Exception`**——
    那会把 404/405 之类的 HTTPException 一起吞掉再抛，破坏既有路由语义。
    """
    payload, status = _storage_error_response(exc)
    logger.error(
        "storage error mapped: path=%s category=%s", request.path, type(exc).__name__
    )
    return jsonify(payload), status


def _register_storage_error_handlers(flask_app):
    import pgdb

    flask_app.register_error_handler(pgdb.PgError, _handle_storage_error)
    flask_app.register_error_handler(runtime_mode.ReadOnlyMode, _handle_storage_error)
    flask_app.register_error_handler(runtime_mode.AuthorityUnavailable, _handle_storage_error)
    return flask_app


def _money_payload(field, *, silent=False):
    """Preserve only the money field's original JSON token; other types stay unchanged."""
    # Retain Flask's existing malformed JSON/content-type behavior and int/bool contracts.
    payload = request.get_json(silent=silent)
    if isinstance(payload, dict) and field in payload:
        exact = json.loads(request.get_data(), parse_float=Decimal)
        payload = {**payload, field: exact[field]}
    return payload


def _find_service(name):
    services = sm.load_assets(ASSETS_PATH)
    for svc in services:
        if svc.get("name") == name:
            return svc
    return None


@app.route("/api/service/action", methods=["POST"])
def service_action():
    """对服务执行 start/stop/restart/logs/upgrade。

    upgrade 成功后同步该服务的版本，再失效服务列表快照。版本同步失败仍返回
    `ok=true`，附安全的 `warning` 提示，避免用户把已完成的升级当作失败重试。
    """
    data = request.json or {}
    name = data.get("name")
    action = data.get("action")
    svc = _find_service(name)
    if not svc:
        return jsonify({"ok": False, "error": f"未找到服务 {name}"}), 404

    ok, output = sm.do_action(svc, action)
    result = {"ok": ok, "output": output}
    # start/stop/restart/upgrade 记录审计；logs 不记录
    if action in ("start", "stop", "restart", "upgrade"):
        _log_op(svc.get("machine") or mc.local_machine(), name, action, ok, output)
        if ok:
            if action == "upgrade":
                try:
                    vs.sync_service_version(svc, db_path=DB_PATH)
                except Exception as exc:
                    logger.warning(
                        "post-upgrade version sync failed: service=%s category=%s",
                        name, type(exc).__name__,
                    )
                    result["warning"] = "升级成功，但版本刷新失败，请在版本详情中重试刷新"
            _services_page_cache.invalidate()
    return jsonify(result)


@app.route("/api/machine/action", methods=["POST"])
def machine_action():
    """对 node 机器执行白名单动作（E3）。当前白名单只有重启 sing-box。"""
    data = request.json or {}
    machine = data.get("machine")
    action = data.get("action")
    spec = sm.node_action_spec(action)
    if not spec:
        return jsonify({"ok": False, "error": f"未授权的节点动作: {action}"}), 400

    ok, output = sm.node_action(machine, action)
    _log_op(str(machine or "?"), spec["unit"], spec["verb"], ok, output)
    if ok:
        # 重启后端口会短暂不可用，清掉探活缓存让下一次 /api/machines 重新探
        pc.invalidate_machine(machine)
    return jsonify({"ok": ok, "output": output})


@app.route("/api/service", methods=["POST", "PUT"])
def service_upsert():
    """新增或更新服务台账。"""
    data = request.json or {}
    original = data.get("_original_name")
    home = (data.get("access") or {}).get("home") or data.get("home", "")
    tailscale = (data.get("access") or {}).get("tailscale") or data.get("tailscale", "")
    domain = (data.get("access") or {}).get("domain") or data.get("domain", "")
    probe = ah.probe_access(home, tailscale, domain)
    try:
        entry = sm.upsert_service(ASSETS_PATH, data, original_name=original, access_probe=probe)
        _services_page_cache.invalidate()
        _log_op("services", entry.get("name", "?"),
                "service_update" if original else "service_create", True)
        return jsonify({"ok": True, "service": entry, "access_probe": probe})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/service/<name>", methods=["DELETE"])
def service_delete(name):
    """删除服务台账。"""
    try:
        sm.delete_service(ASSETS_PATH, name)
        _services_page_cache.invalidate()
        _log_op("services", name, "service_delete", True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/services/priority", methods=["POST"])
def services_priority():
    """批量设置服务优先级。"""
    data = request.json or {}
    try:
        count = sm.set_priority(ASSETS_PATH, data.get("names"), data.get("priority"))
        _services_page_cache.invalidate()
        names = ",".join(str(n) for n in (data.get("names") or []))[:120]
        _log_op("services", names or "?", "service_priority", True)
        return jsonify({"ok": True, "updated": count})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

# C1：GET /api/inspection/report?format=markdown|json（只读聚合，路由体在模块内）
inspection_report.register_routes(app, lambda: (ASSETS_PATH, DB_PATH))

# C10-a：GET /api/config/effective（设置页只读「有效配置」，路由体在模块内）
effective_config.register_routes(app, lambda: DB_PATH)

# C8/D1'：GET/PUT /api/dashboard-layout（布局 override 升库，校验与整体替换在模块内）
dashboard_layout.register_routes(app, lambda: DB_PATH, _log_op, lambda: _dashboard_machine_ids())

# C10-b：GET/PUT /api/config/settings（白名单写回 settings.yaml，永不可写硬拒在模块内）
settings_store.register_routes(app, lambda: DB_PATH, _log_op)


# 账本文件协作（提案 §3.2；PG-01 提供模块，PG-05 集成接线）：
# 四个固定接口只在 authority（NAS）注册，client 侧不注册以免出现第二份权威归档目录。


# Track K：事件时间线（只读，不新探测）


# Track M2：磁盘增长趋势（只读 Beszel 历史，自带 1 小时缓存）
capacity.register_routes(app)

# B4-3：监控接收 API（enrol/ingest/revoke，认证/限流/版本协商在模块内；
# machine_id 白名单每请求读台账，oplog 只记 revoke 这类管理动作）
monitoring_api.register_routes(app, _log_op, lambda: set(mc.load_machines()))
monitoring_history.register_routes(app)


class _PgMachineStore:
    """B5-5 收口：把 monitoring_api 的 store 接到 B4-2 的 PG 三表。

    形状差与裁决（monitoring_api 默认 InMemoryMachineStore 的接口 vs
    monitoring_ingest 的函数签名）：
    - verify：两边都收明文、内部 SHA-256（monitoring_api 侧 B5-5 已改为传
      明文）；PG 端不区分「未注册/密钥错/已吊销」一律 False——API 层本来
      就把非 active 统一 401，无需细分。
    - enrol：由存储层生成并哈希保存 secret，明文只经本次 API 响应返回；
      路由先验证环境持有的 enrol_token，不再自行生成另一份不匹配的密钥。
    - touch_last_seen：PG insert_samples 默认 touch=True 已刷 last_seen_at
      与 agent_version，no-op 即可。
    """

    def enrol_machine(self, machine_id, display_name, public_key):
        return monitoring_ingest.enrol_machine(machine_id, display_name, public_key)

    def verify_machine(self, machine_id, secret):
        return "active" if monitoring_ingest.verify_machine(machine_id, secret) else None

    def revoke_machine(self, machine_id):
        return monitoring_ingest.revoke_machine(machine_id)

    def touch_last_seen(self, machine_id):
        pass

    def insert_samples(self, machine_id, samples, agent_version):
        return monitoring_ingest.insert_samples(machine_id, samples, agent_version).get("inserted", 0)


if runtime_mode.storage_engine() == runtime_mode.STORAGE_ENGINE_POSTGRES:
    # 仅 PG 在线入口接线；SQLite 测试底座保留 InMemory 兜底（monitoring_ingest
    # 仅支持 PG，B4 数据链路无 SQLite 路径）。
    monitoring_api.configure_store(_PgMachineStore())


@app.route("/api/caddy/entry", methods=["GET"])
def get_caddy_entry():
    """C11：入口上游只读小卡（Caddy admin API 经 NAS crontab 导出文件）。
    文件缺失/坏 JSON 返回 available=false 显式降级，不抛 500；
    upstream address 全是 tailnet IP，只在服务端反查服务名，绝不下发（e329e27）。"""
    return jsonify(cv.get_caddy_snapshot())




# ---- 维护模式挡住 YAML / 配置文件写入（提案 §6.4） ------------------------
# `read_only` 必须挡住「业务写、YAML 写、文件发布与 seed」。业务写由
# `pgdb.pg_transaction()` 统一拒绝（写事务自动过 `require_writable`）；
# YAML 写不经数据库，所以在 HTTP 入口按 method+route 白名单挡。
#
# 白名单必须**显式**列出：默认拒绝所有非 GET 会连登录等无关入口一起挡掉，
# 而默认放行又会漏掉将来新增的配置写路由。这里列的是当前会写 YAML 的入口。
_YAML_WRITE_ROUTES = frozenset(
    {
        ("POST", "/api/machines"),
        ("POST", "/api/service"),
        ("PUT", "/api/service"),
        ("POST", "/api/services/priority"),
        ("POST", "/api/panels"),
        ("PUT", "/api/panels"),
        ("PUT", "/api/config/settings"),
        ("PUT", "/api/plugins/<plugin_id>"),
        ("PUT", "/api/machines/<machine_id>"),
        ("DELETE", "/api/machines/<machine_id>"),
        ("DELETE", "/api/service/<name>"),
        ("PUT", "/api/panels/<int:panel_id>"),
        ("DELETE", "/api/panels/<int:panel_id>"),
    }
)

# Flask's matched route (including its converter), never a permissive URL prefix.
# PG business routes such as scratch/layout/bookmarks/renewals remain local.
_AUTHORITY_READ_ROUTES = frozenset({
    "/api/config", "/api/config/settings", "/api/panels", "/api/plugins",
    "/api/machines", "/api/machines/<machine_id>", "/api/machines/<machine_id>/discover",
    "/api/services", "/api/service/<name>/version", "/api/service/suggest-access",
    "/api/monitor", "/api/monitor/history", "/api/observability", "/api/feed", "/api/feed/sources",
    "/api/tailnet", "/api/caddy/entry",
    # client 只消费权威端聚合视图：这三条只读、不写库，应交权威端执行一次。
    # ops-status 尤其必须转发——client 本机无 backup/ 挂载、不跑探针、无部署留痕，
    # 本机算出来七项里四项恒空，总控台会整片显示「未知」而非权威端实况。
    "/api/attention", "/api/capacity", "/api/ops-status",
})
_AUTHORITY_FORWARD_ROUTES = _YAML_WRITE_ROUTES | frozenset(
    (method, route) for route in _AUTHORITY_READ_ROUTES for method in ("GET", "HEAD")
) | frozenset({("POST", "/api/service/access-check")})


def _matched_route():
    return request.url_rule.rule if request.url_rule is not None else None


class _AuthorityNoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # An authority response must never turn the fixed origin into an arbitrary proxy.
        return None


def _request_authority(origin, path, *, method="GET", query=b"", body=None, headers=None, timeout=60):
    """Bounded fixed-origin transport, without browser credentials or ambient proxies."""
    target = origin + "/" + quote(path.lstrip("/"), safe="/")
    if query:
        target += "?" + query.decode("ascii")
    allowed_request = {"accept", "content-type", "if-none-match", "if-modified-since"}
    forwarded = {k: v for k, v in (headers or {}).items() if k.lower() in allowed_request}
    forwarded["X-Console-Authority-Hop"] = "1"
    req = urllib.request.Request(target, data=body, headers=forwarded, method=method)
    allowed_response = {
        "content-type", "cache-control", "etag", "last-modified", "retry-after",
        "content-disposition", "x-snapshot-at", "x-snapshot-refreshing", "x-snapshot-error",
    }
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _AuthorityNoRedirectHandler())
    try:
        upstream = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        upstream = exc
    except (OSError, ValueError):
        return jsonify({"error": "NAS 权威端不可达，请稍后重试"}), 503
    try:
        with upstream:
            return Response(
                upstream.read(), upstream.status,
                [(k, v) for k, v in upstream.headers.items() if k.lower() in allowed_response],
            )
    except (OSError, http.client.HTTPException):
        # OSError 覆盖连接中断；http.client.HTTPException 覆盖 IncompleteRead（响应体截断，
        # 声明的 Content-Length 没收全）。两者都是「权威端响应不完整」，映射为 503 而非 500。
        return jsonify({"error": "NAS 权威端响应中断，请刷新确认结果"}), 503


def _proxy_authority(origin, subpath):
    timeout = 60
    if request.method == "POST" and subpath == "api/service/action":
        data = request.get_json(silent=True)
        if isinstance(data, dict) and data.get("action") == "upgrade":
            # pull/up 最多 300s，完成后还需探测本地版本并同步上游。
            timeout = 420
    return _request_authority(
        origin, subpath, method=request.method, query=request.query_string,
        body=request.get_data() if request.method in ("POST", "PUT", "PATCH", "DELETE") else None,
        headers=dict(request.headers),
        timeout=timeout,
    )


def _authority_config_rows(path):
    """Only the two YAML collections needed to validate local shared-PG writes."""
    if path not in ("/api/panels", "/api/machines", "/api/plugins"):
        raise ValueError("不支持的权威配置读取")
    try:
        origin = runtime_mode.authority_origin(request.host_url)
    except runtime_mode.ConfigError as exc:
        raise runtime_mode.AuthorityUnavailable(str(exc)) from exc
    response = app.make_response(_request_authority(origin, path))
    rows = response.get_json(silent=True)
    if response.status_code != 200 or not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise runtime_mode.AuthorityUnavailable("无法读取 NAS 权威配置，请稍后重试")
    return rows


def _dashboard_machine_ids():
    if runtime_mode.instance_role() == runtime_mode.INSTANCE_ROLE_CLIENT:
        return {row.get("id") for row in _authority_config_rows("/api/machines")}
    return set(mc.load_machines())


@app.before_request
def block_yaml_writes_in_maintenance():
    """维护模式下拒绝会写 YAML/配置的请求；返回 503 而不是 400（与 PG 写一致）。"""
    if not runtime_mode.is_read_only():
        return None
    method = request.method
    if method in ("GET", "HEAD", "OPTIONS"):
        return None
    if (method, _matched_route()) in _YAML_WRITE_ROUTES or plugin_runtime.owns_api(request.path):
        return jsonify({"error": "系统处于维护模式，暂时不能修改配置，请稍后重试"}), 503
    return None


@app.before_request
def forward_observation_to_authority():
    """client 的匹配路由交 NAS 执行一次；失败不回落本机 handler/审计。"""
    if runtime_mode.instance_role() != runtime_mode.INSTANCE_ROLE_CLIENT:
        return None
    if (request.method, _matched_route()) not in _AUTHORITY_FORWARD_ROUTES and not plugin_runtime.forwards_api(request.path):
        return None
    if request.headers.get("X-Console-Authority-Hop"):
        return jsonify({"error": "权威端角色配置错误，拒绝递归转发"}), 503
    if any(str(value) in (".", "..") or "\\" in str(value) for value in (request.view_args or {}).values()):
        return jsonify({"error": "无效的权威路由参数"}), 400
    try:
        origin = runtime_mode.authority_origin(request.host_url)
    except runtime_mode.ConfigError as exc:
        return jsonify({"error": str(exc)}), 503
    return _proxy_authority(origin, request.path.lstrip("/"))




@app.route("/actions", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.route("/actions/", defaults={"subpath": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.route("/actions/<path:subpath>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def retired_actions_page(subpath=""):
    """OliveTin 页面已退役。"""
    return jsonify({"error": "actions page retired"}), 410




@app.route("/assets/<path:filename>", methods=["GET"])
def serve_assets(filename):
    """只提供 Console 自有静态资源；不转发到已退役的 Hub。"""
    return send_from_directory(os.path.join(STATIC_DIR, "assets"), filename)

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    """前端 SPA 托管：命中静态文件直接发，其余回 index.html 交给前端路由。

    `api/` / `actions/` / `beszel/` 前缀在这里显式 404，不能把漏配路由或已退役 Hub 伪装成前端首页。
    前端未构建（无 static 目录）返回 503。
    """
    # 先判 API/退役前缀：未注册 API 与前端是否构建无关，必须稳定 404（CI 无 static 时曾误报 503）。
    if path.startswith("api/"):
        return jsonify({"error": "not found"}), 404
    if path.startswith("actions/") or path.startswith("beszel/"):
        return jsonify({"error": "not found"}), 404
    if not os.path.isdir(STATIC_DIR):
        return jsonify({"error": "frontend not built"}), 503
    file_path = os.path.join(STATIC_DIR, path)
    if path and os.path.isfile(file_path):
        if path == "manifest.webmanifest":
            return send_from_directory(
                STATIC_DIR, path, mimetype="application/manifest+json"
            )
        return send_from_directory(STATIC_DIR, path)
    return send_from_directory(STATIC_DIR, "index.html")

# 启动序列放在文件末尾：它依赖 `_bg_*` 与异常处理器的定义，且必须在所有路由
# 注册完成之后才拨号/起线程（PG 连不上时以非零码退出，不降级到 SQLite）。
plugin_context = PluginContext(
    plugin_id="", database_path=lambda: DB_PATH, panel_config_path=lambda: PANELS_PATH,
    backend_dir=os.path.dirname(os.path.abspath(__file__)), logger=logger,
    audit=lambda *a, **kw: _log_op(*a, **kw),
    money_payload=lambda *a, **kw: _money_payload(*a, **kw),
    storage_error=lambda exc: _storage_error_response(exc),
    authority_rows=lambda path: _authority_config_rows(path),
    utc_now=lambda: _utc_now_text(), parse_json=lambda *a: _parse_json_field(*a),
)
plugin_runtime.register(app, plugin_context)
_register_storage_error_handlers(app)
_startup()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    if os.environ.get("FLASK_ENV", "production") != "production":
        # 开发模式保留 Flask 自带 server（debug/热重载）
        app.run(host="0.0.0.0", port=port, debug=True)
    else:
        # 生产/本地预览统一走 waitress，多线程避免探测/代理请求串行阻塞
        from waitress import serve

        serve(app, host="0.0.0.0", port=port, threads=8)
