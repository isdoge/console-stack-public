# -*- coding: utf-8 -*-
"""设置页写回（C10-b）：`settings.yaml` 持久化 + 白名单 + 永不可写硬拒。

C10-a 把「每个字段的值与来源」摊开成只读视图后，剩下的问题是「哪些项允许离开
`.env`」。本模块给出的答案是一份**极窄的白名单**，其余一律 env-only：

优先级 **env > settings.yaml > 代码默认**——实现方式是启动时
``apply_to_env()`` 把 `settings.yaml` 里的白名单键注入 ``os.environ``，
**且只注入 env 里没有的键**（``_NATIVE_ENV_KEYS`` 是 import 期快照）。
于是「env 已设的项 UI 只读」不需要任何额外分支：写回时发现 key 在原生 env 里就 409。

三类拒绝（各有独立状态码，前端与测试都靠它区分）：

- **403 永不可写**：``CONSOLE_AUTH_*`` / ``CONSOLE_CORS_ORIGINS`` /
  ``CONSOLE_ALLOW_DOCKER_EXEC_PROBES`` / ``MACHINE_SSH_*`` / ``MACHINE_TAILSCALE_*`` /
  ``SECRET_KEY`` / ``CONSOLE_OPLOG_TOKEN``，外加**一切 key 名含
  token/password/secret/key 的字段**（复用 ``effective_config.is_sensitive_key``，
  secrets 永远 env-only）。这条在白名单之前判——就算将来有人手滑把某个 secret
  加进白名单，也仍然是 403。
- **400 不在白名单**：新键要进写回必须显式加 ``WRITABLE_SPEC``（保守默认）。
- **409 env 已锁定**：`.env` / compose 已经设了这个键，UI 只读，改它得改 `.env` 重启容器。

**手工编辑 `settings.yaml` 塞危险键同样无效**：``apply_to_env()`` 只注入白名单内的
键，白名单外的条目读进来也不会进 ``os.environ``（安全纵深，有测试锁死）。

白名单为什么这么窄（2026-09-07 按代码实况裁决，逐条待 owner 终审）：

============================== ==========================================================
键                              裁决
============================== ==========================================================
``CONSOLE_PROBE_INTERVAL``     **可写**。``probe_cache.probe_interval()`` 每次调用现读
                               env，compose 未注入 → 保存即生效，无需重启。
``CONSOLE_MACHINE_PROBE_INTERVAL`` **可写**（任务书默认提案外的一项，同构补入）。
                               ``machine_probe_interval()`` 同样每次现读，下一轮探针生效。
``CONSOLE_OPLOG_RETENTION_DAYS`` **可写**（C10-b 新键）。0 = 不清理（默认，与现状一致）；
                               >0 时按天清理 oplog。
``CONSOLE_EVENT_RETENTION_DAYS`` **可写**（Track K 新键，同构）。0 = 不清理（默认）；
                               >0 时按天清理**已恢复**的 ``monitor_events``；活动事件
                               永不删（见 ``monitor_events.prune``）。
``OLIVETIN_PUBLIC_URL``        **不纳入**：全仓零读取点（OliveTin 集成 2026-07-23 已移除，
                               `.env.example` / `configuration.md` 均无此键），写了没人读。
``BESZEL_URL``                 **不纳入**：compose 对 console 容器**硬注入**
                               ``BESZEL_ORIGIN: http://beszel:8090``（非 ``${}`` 形式），
                               ``beszel_api.beszel_base()`` 的解析链是 ORIGIN > URL，
                               生产容器里写 URL 永远被压过 —— UI 会显示一个不生效的值。
``BESZEL_HUB_URL``             **不纳入**：它是 **compose 层**变量（beszel 容器的
                               ``APP_URL: ${BESZEL_HUB_URL:-…}``，由 docker compose 读
                               `.env`），console 进程写 `settings.yaml` 影响不到它。
``RSS_URLS``                   **不纳入**（契约既定）：URL 可能内嵌凭据。owner 2026-09-07
                               终审**维持不放开**（终局，见 IMPROVEMENTS「不做区」）——
                               要加源仍走 compose `.env` + 重启，不经设置页。
secrets / 安全键 / ``PORT`` /  **不纳入**（契约既定）：secrets 永远 env-only，基建参数
``DOCKER_API_VERSION``         归 compose。
============================== ==========================================================

生效语义：本白名单**每一项都是「每次现读 env」的键**，保存即生效（探针间隔在下一轮
调度生效），**不需要重启容器**。将来若纳入 import 期读取的键，必须把
``requires_restart`` 标成 True 并在 UI 与 OPERATIONS 注明。

写回动作记 oplog（``host='console', svc='settings'``，照 dashboard 布局先例）。
无登录依赖（P0-3 现状照旧）：暴露面收敛靠白名单 + 永不可写硬拒 + 值域校验。
"""
import datetime
import os
import sqlite3

import yaml
from flask import jsonify, request

import composition
import db as dbmod
import effective_config
import monitor_events
import pg_diagnostics

# ---- 路径 ---------------------------------------------------------------


def settings_path():
    """组合配置根中的可写设置；专用环境路径优先。"""
    return str(composition.config_path("settings.yaml", "CONSOLE_SETTINGS_FILE"))


_FILE_HEADER = """\
# console 设置页写回（C10-b）。**由 UI 写入，PyYAML 不保留手写注释**——
# 说明写进 docs/configuration.md 或 proposal，不要只写在本文件行内。
# 只有 settings_store.WRITABLE_SPEC 白名单内的键会被读进 os.environ；
# 手工塞入的其它键（尤其是 CONSOLE_AUTH_* / 任何 token）一律被忽略，不会生效。
# 优先级：.env（env）> 本文件 > 代码默认。"""


# ---- 白名单与永不可写 ----------------------------------------------------

# 精确匹配的永不可写键
NEVER_WRITABLE_KEYS = frozenset(
    {
        "CONSOLE_CORS_ORIGINS",
        "CONSOLE_ALLOW_DOCKER_EXEC_PROBES",
        "SECRET_KEY",
        "CONSOLE_OPLOG_TOKEN",
    }
)

# 前缀匹配的永不可写键族
NEVER_WRITABLE_PREFIXES = ("CONSOLE_AUTH_", "MACHINE_SSH_", "MACHINE_TAILSCALE_")

# 可写白名单。type 目前只用 int；加 str 型时必须同时给出校验（见 _coerce）。
WRITABLE_SPEC = {
    "CONSOLE_PROBE_INTERVAL": {
        "group": "general",
        "title": "服务健康探测间隔",
        "description": "服务健康检查与镜像快照的缓存 TTL；探测越频繁越吃远端。",
        "type": "int",
        "min": 60,
        "max": 86400,
        "unit": "秒",
        "default": "600",
        "requires_restart": False,
    },
    "CONSOLE_MACHINE_PROBE_INTERVAL": {
        "group": "general",
        "title": "机器探针间隔",
        "description": "机器探活色带的采样节点（对齐本地整刻度），与服务健康 TTL 解耦。",
        "type": "int",
        "min": 60,
        "max": 86400,
        "unit": "秒",
        "default": "1800",
        "requires_restart": False,
    },
    "CONSOLE_OPLOG_RETENTION_DAYS": {
        "group": "data",
        "title": "操作日志保留天数",
        "description": "0 = 不清理（默认，与历史行为一致）；>0 时保存设置与每轮机器探针后按天清理 oplog。时间未知的旧行不删。",
        "type": "int",
        "min": 0,
        "max": 3650,
        "unit": "天",
        "default": "0",
        "requires_restart": False,
    },
    "CONSOLE_EVENT_RETENTION_DAYS": {
        "group": "data",
        "title": "监控事件保留天数",
        # description 直接进 UI 文本节点，前端不渲染 Markdown —— 这里不要写 ** **
        "description": "0 = 不清理（默认）；>0 时按天清理已恢复的监控事件。未恢复的活动事件永不删。",
        "type": "int",
        "min": 0,
        "max": 3650,
        "unit": "天",
        "default": "0",
        "requires_restart": False,
    },
}

GROUP_TITLES = {
    "general": "常规",
    "data": "数据管理",
}

OPLOG_HOST = "console"
OPLOG_SVC = "settings"
OPLOG_ACTION = "settings_update"

# import 期快照：此刻 os.environ 里有的键 = 真正来自 .env / compose 的键。
# apply_to_env() 之后 environ 会多出我们注入的键，靠这份快照才分得清谁是谁。
_NATIVE_ENV_KEYS = frozenset(os.environ)
_injected_keys = set()


def refresh_native_env_snapshot():
    """重新快照原生 env（**仅测试用**：测试要在 monkeypatch.setenv 之后重算）。"""
    global _NATIVE_ENV_KEYS
    _NATIVE_ENV_KEYS = frozenset(os.environ) - _injected_keys
    return _NATIVE_ENV_KEYS


def is_never_writable(key):
    """永不可写判定：显式集合 + 前缀族 + 一切敏感 key 名（secrets env-only）。"""
    name = str(key or "").strip().upper()
    if not name:
        return True
    if name in NEVER_WRITABLE_KEYS:
        return True
    if any(name.startswith(prefix) for prefix in NEVER_WRITABLE_PREFIXES):
        return True
    # token / password / secret / key —— 与 /api/config/effective 的遮蔽判定同源
    return effective_config.is_sensitive_key(name)


def is_whitelisted(key):
    return str(key or "").strip().upper() in WRITABLE_SPEC


def is_env_locked(key):
    """`.env` / compose 已设 → UI 只读（env > settings.yaml）。"""
    return str(key or "").strip().upper() in _NATIVE_ENV_KEYS


# ---- YAML 读写 -----------------------------------------------------------


def load_settings(path=None):
    """读 settings.yaml → dict。文件缺失 / 坏 YAML / 顶层非 mapping 一律 {}。

    坏文件不抛：设置文件读不出来只该让页面回到默认值，不该让整个后端 500。
    """
    target = path or settings_path()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            parsed = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k).strip().upper(): v for k, v in parsed.items() if str(k).strip()}


def _write_settings(values, path=None):
    """原子写（复用 yaml_io 的锁 + 临时文件 + bind mount 回退），带契约注释头。"""
    from yaml_io import atomic_write_yaml

    target = path or settings_path()
    atomic_write_yaml(target, dict(sorted(values.items())), header=_FILE_HEADER)
    return target


def apply_to_env(path=None):
    """启动时把 settings.yaml 的白名单键注入 os.environ（env 已设的跳过）。

    返回实际注入的键集合。**白名单外的键一律不注入**——手工往 settings.yaml
    塞 CONSOLE_AUTH_ENABLED 之类不会有任何效果（安全纵深，有测试锁死）。
    """
    applied = set()
    for key, value in load_settings(path).items():
        if key in _NATIVE_ENV_KEYS:
            continue  # env 优先
        if is_never_writable(key) or not is_whitelisted(key):
            continue
        os.environ[key] = str(value)
        _injected_keys.add(key)
        applied.add(key)
    return applied


# ---- 取值与校验 ----------------------------------------------------------


def _coerce(key, raw):
    """按 spec 校验并归一化；不合法抛 ValueError（文案直接进 400）。"""
    spec = WRITABLE_SPEC[key]
    title = spec["title"]
    if spec["type"] == "int":
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise ValueError(f"{title} 必须是整数")
        try:
            number = int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{title} 必须是整数")
        low, high = spec["min"], spec["max"]
        if not low <= number <= high:
            raise ValueError(f"{title} 必须在 {low}–{high} 之间（{spec['unit']}）")
        return str(number)
    text = "" if raw is None else str(raw).strip()
    if len(text) > 512:
        raise ValueError(f"{title} 过长")
    return text


def effective_value(key):
    """当前生效值：os.environ 优先（含注入），否则 spec 默认。"""
    return os.environ.get(key, WRITABLE_SPEC[key]["default"])


def source_of(key, stored):
    """三态来源：env（.env/compose）> settings（本文件）> default。"""
    if is_env_locked(key):
        return "env"
    if key in stored:
        return "settings"
    return "default"


def retention_days():
    """oplog 保留天数；非法值按 0（不清理）处理。"""
    try:
        return max(0, int(os.environ.get("CONSOLE_OPLOG_RETENTION_DAYS", "0")))
    except (TypeError, ValueError):
        return 0


# ---- 数据管理 ------------------------------------------------------------


def table_row_counts(db_path=None):
    """各表行数（只读展示用）。库/表读不出来一律给空列表，不抛。

    PG-04：在线 PG 经 pg_diagnostics 只读事务（information_schema 目录）；
    显式 db_path / SQLite 引擎按既有 sqlite_master 只读约定。两种目录的
    表清单口径一致：排除系统表。
    """
    try:
        with pg_diagnostics.readonly_connection(db_path) as conn:
            if pg_diagnostics.is_online_postgres(db_path):
                names = [
                    r["table_name"]
                    for r in conn.execute(
                        "SELECT table_name AS table_name FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_type='BASE TABLE' "
                        "ORDER BY table_name"
                    ).fetchall()
                ]
            else:
                names = [
                    r["name"]
                    for r in conn.execute(
                        "SELECT name AS name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                    ).fetchall()
                ]
            rows = []
            for name in names:
                try:
                    count = conn.execute(f'SELECT COUNT(*) AS n FROM "{name}"').fetchone()["n"]
                except Exception:
                    continue
                rows.append({"name": name, "rows": int(count)})
            return rows
    except (sqlite3.Error, OSError, Exception):
        return []


def prune_oplog(db_path=None, days=None, now=None):
    """按保留天数删 oplog 旧行；返回删除条数。0 天 = 不清理，直接返回 0。

    `started_at` 为空的行**不删**：时间未知就不该替用户做决定。
    库/表不存在按 0 处理（与 dashboard_layout 的防御式读取同纪律）。
    PG-04：在线 PG 走 pg_transaction 写事务（自动维护共享锁 + 异常映射）；
    显式 db_path / SQLite 引擎沿用既有 get_connection 约定。
    """
    keep = retention_days() if days is None else max(0, int(days))
    if keep <= 0:
        return 0
    moment = now or datetime.datetime.now()
    cutoff = (moment - datetime.timedelta(days=keep)).strftime("%Y-%m-%d %H:%M:%S")
    if pg_diagnostics.is_online_postgres(db_path):
        try:
            with dbmod.pg_transaction() as conn:
                cursor = conn.execute(
                    "DELETE FROM oplog WHERE started_at IS NOT NULL AND started_at < %s",
                    (cutoff,),
                )
                deleted = cursor.rowcount or 0
        except Exception:
            return 0
        return int(deleted)
    try:
        conn = dbmod.get_connection(db_path)
    except sqlite3.Error:
        return 0
    try:
        cursor = conn.execute(
            "DELETE FROM oplog WHERE started_at IS NOT NULL AND started_at < ?",
            (cutoff,),
        )
        deleted = cursor.rowcount or 0
        conn.commit()
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
    return int(deleted)


# ---- 视图与写回 ----------------------------------------------------------


def build_view(db_path=None, path=None):
    """GET 响应：分组的可写项 + 各表行数 + 永不可写清单（「安全」分区展示用）。"""
    stored = load_settings(path)
    groups = {}
    for key, spec in WRITABLE_SPEC.items():
        group_id = spec["group"]
        group = groups.setdefault(
            group_id, {"title": GROUP_TITLES.get(group_id, group_id), "entries": []}
        )
        source = source_of(key, stored)
        group["entries"].append(
            {
                "key": key,
                "title": spec["title"],
                "description": spec["description"],
                "value": effective_value(key),
                "source": source,
                "writable": source != "env",
                "readonly_reason": ".env / compose 已设置该项，改它需改 .env 后重启容器"
                if source == "env"
                else None,
                "type": spec["type"],
                "min": spec.get("min"),
                "max": spec.get("max"),
                "unit": spec.get("unit", ""),
                "default": spec["default"],
                "requires_restart": spec["requires_restart"],
            }
        )
    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "settings_file": os.path.basename(settings_path()),
        "settings_file_exists": os.path.exists(settings_path()),
        "groups": groups,
        "tables": table_row_counts(db_path),
        # 「安全」分区只展示键名（值一律不回显，敏感值本就遮蔽在 /api/config/effective）
        "never_writable": sorted(NEVER_WRITABLE_KEYS)
        + [f"{prefix}*" for prefix in NEVER_WRITABLE_PREFIXES]
        + ["*TOKEN* / *PASSWORD* / *SECRET* / *KEY*"],
    }


class SettingsError(Exception):
    """写回被拒；`status` 直接作为 HTTP 状态码（403 / 409 / 400）。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def update_settings(values, path=None):
    """校验 → 写 settings.yaml → 同步 os.environ。任何一项不合法则整批不写。

    幂等：同一份 payload 重复 PUT 结果一致。值给空串 / None 表示**恢复默认**——
    从 settings.yaml 删掉该键，并撤掉我们注入的 env（原生 env 不碰）。
    """
    if not isinstance(values, dict) or not values:
        raise SettingsError("values 必须是非空对象", 400)

    normalized = {}
    removals = []
    for raw_key, raw_value in values.items():
        key = str(raw_key or "").strip().upper()
        if is_never_writable(key):
            raise SettingsError(f"{key} 永不可写：该项只能经 .env 配置", 403)
        if not is_whitelisted(key):
            raise SettingsError(f"{key} 不在可写白名单内", 400)
        if is_env_locked(key):
            raise SettingsError(
                f"{key} 已由 .env / compose 设置，UI 只读（改它需改 .env 后重启容器）", 409
            )
        if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
            removals.append(key)
            continue
        normalized[key] = _coerce(key, raw_value)

    from yaml_io import mutate_yaml

    def change(document):
        if document is not None and not isinstance(document, dict):
            raise SettingsError("settings.yaml 顶层必须是对象", 400)
        stored = {str(k).strip().upper(): v for k, v in (document or {}).items()}
        stored.update(normalized)
        for key in removals:
            stored.pop(key, None)
        return dict(sorted(stored.items()))

    mutate_yaml(path or settings_path(), change, header=_FILE_HEADER)

    for key, value in normalized.items():
        os.environ[key] = value
        _injected_keys.add(key)
    for key in removals:
        if key in _injected_keys:
            os.environ.pop(key, None)
            _injected_keys.discard(key)
    return sorted(set(normalized) | set(removals))


def register_routes(app, resolve_db=None, log_op=None):
    """挂到既有 app 上（同 dashboard_layout：resolve_db 每请求求值）。"""

    @app.route("/api/config/settings", methods=["GET"])
    def get_console_settings():
        """设置页可写项（C10-b）：分组的白名单配置 + 各表行数 + 永不可写清单。

        `source` 三态 env / settings / default；`writable=false` 即 .env 已锁定。
        本端点不回显任何 secrets——它们根本不在白名单里。
        """
        db_path = resolve_db() if resolve_db else None
        return jsonify(build_view(db_path))

    @app.route("/api/config/settings", methods=["PUT"])
    def put_console_settings():
        """写回白名单配置到 `settings.yaml`（增量：只改 values 里给到的键）。

        拒绝三态：永不可写 403（`CONSOLE_AUTH_*` / `MACHINE_SSH_*` / 一切
        token/password/secret/key 等）、不在白名单 400、`.env` 已锁定 409。
        值给空串即恢复默认。成功与失败都记 oplog（`settings_update`）。
        """
        payload = request.get_json(silent=True)
        values = payload.get("values") if isinstance(payload, dict) else None
        db_path = resolve_db() if resolve_db else None
        try:
            changed = update_settings(values)
        except SettingsError as exc:
            if log_op:
                log_op(OPLOG_HOST, OPLOG_SVC, OPLOG_ACTION, False, str(exc))
            return jsonify({"error": str(exc)}), exc.status
        except ValueError as exc:  # _coerce 的值域/类型拒绝
            if log_op:
                log_op(OPLOG_HOST, OPLOG_SVC, OPLOG_ACTION, False, str(exc))
            return jsonify({"error": str(exc)}), 400
        # 保留期改小后要立刻看得见效果（行数展示同一响应里就更新了）
        pruned = prune_oplog(db_path)
        pruned_events = monitor_events.prune(db_path=db_path)
        if log_op:
            log_op(OPLOG_HOST, OPLOG_SVC, OPLOG_ACTION, True, ",".join(changed))
        result = build_view(db_path)
        result["changed"] = changed
        result["pruned_oplog"] = pruned
        result["pruned_events"] = pruned_events
        return jsonify(result)

    return app
