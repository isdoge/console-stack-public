# -*- coding: utf-8 -*-
"""有效配置（C10-a）：只读聚合「每个字段的值 + 来源」，供设置页「关于 / 有效配置」分区。

字段清单以 docs/configuration.md（2026-09-06 基线）为准，本文不重复维护说明文案；
若文档字段增删，这里同步改。来源三态固定 **env > yaml > 默认**，与全仓读取逻辑一致：

- env：进程环境变量；
- yaml：machines.yaml（仅 `tailscale` / `ssh` 两字段有 env 覆盖通道，configuration.md §2）；
- default：未配置时代码实际生效的默认值。默认值单一来源（集成评审 P2 收敛①）：
  读取点有模块常量 / 可调用函数的直接取值（machine_config / probe_cache /
  version_service / hindsight_proxy），本模块不留副本；读取点为
  函数内联字面量的（app.main / auth / feed_aggregator 等），副本与读取点由
  test_effective_config 逐项比对测试绑定，任一侧漂移即红。

敏感值遮蔽：key 名含 token / password / secret / key（不区分大小写）的字段，
值只回显 `***`，长度不外泄。SSH target 含 `user@host` 形态，同 token 一样
属于「回了会被抄走」的凭据，单独硬编码进遮蔽清单（`CONSOLE_SSH_KEY_DIR` 因
key 名含 key 已被兜住）。machines 段的台账 `tailscale` 行与 `MACHINE_TAILSCALE_*` /
`MACHINE_SSH_*` 覆盖行同属内网地址 / 登录入口，值恒 `***`（与 machines_for_api
不外泄 IP 同纪律；2026-09-06 集成收口，此前 `mask_value("tailscale", …)` 过不了
敏感词判定、台账行实为明文）。`HINDSIGHT_ORIGIN` 的代码默认值本身就是宿主机
tailnet IP，按 e329e27「内网地址不经 API 外泄」纪律走专用展示：env 已配置
遮蔽主机段保尾部端口，未配置显示「未配置」，绝不回显默认字面量里的 IP。

零写路径：本模块不写任何文件 / DB / 环境变量，只读 + 内存共享状态。

机器探针守护线程（app.py `_bg_machine_probes`）每轮探测后经 `mark_probe_run()`
更新 PROBE_STATE；`last_probe_run`（ISO UTC，无则 None）同时进这里、
ops_status 第 6 键与 attention 的两周期未跑告警（评审新建议 D：Console 自监控）。
"""
import datetime
import os
import sqlite3
from datetime import timezone

from flask import jsonify

import db as dbmod
import machine_config as mc
import ops_status
import pg_diagnostics
import plugin_contributions
import probe_cache
import runtime_mode
import version_service

# ---- 敏感遮蔽 -----------------------------------------------------------

# key 名含这些子串（不区分大小写）→ 值只回显 "***"
_SENSITIVE_KEY_HINTS = ("token", "password", "secret", "key")
_MASK = "***"


def is_sensitive_key(key):
    """遮蔽判定；key 名大小写不敏感（覆盖 key/KEY/Token 等边界）。"""
    lowered = str(key or "").lower()
    return any(hint in lowered for hint in _SENSITIVE_KEY_HINTS)


def mask_value(key, value):
    """敏感 key 只回显 `***`；非敏感原样。key 已在别处遮蔽时不再重复处理。"""
    if is_sensitive_key(key):
        return _MASK
    return value


def _masked_machine_values():
    """MACHINE_SSH_<ID> / MACHINE_TAILSCALE_<ID> 的已设集合（补 is_sensitive_key 的洞）。"""
    configured = set()
    for env_key, _value in os.environ.items():
        upper = env_key.upper()
        if upper.startswith("MACHINE_SSH_") or upper.startswith("MACHINE_TAILSCALE_"):
            configured.add(upper)
    return configured


# ---- 三态解析（env > yaml > default） ------------------------------------


def _detect_env_key(key, machine_id):
    """env 变量名由 <ID> 占位展开（machine_config._env_key 同规则）。"""
    return key.replace("<ID>", str(machine_id or "").strip().upper())


def _machines_section(masked_machines):
    """台账逐台的 yaml 来源展示；env 覆盖的 tailscale/ssh 追加覆盖行。

    tailscale 行的值恒遮蔽为 `***`（内网地址不经 API 外泄，见模块 docstring）；
    env 覆盖值另起行同样遮蔽。yaml 没写的 tailscale 回落到 machine_config 解析出的值
    （_DEFAULT_MACHINES 兜底），只决定该行是否出现，不影响遮蔽。
    ssh 不进 machines_for_api 投影（machine_config.py:639），只在 env 覆盖行出现。
    """
    entries = mc.load_machines()
    rows = []
    for machine_id, entry in entries.items():
        # tailscale：yaml 来源行
        tailscale_value = (entry or {}).get("tailscale") or ""
        if tailscale_value:
            rows.append(
                {
                    "key": "machines.yaml → {}[].tailscale".format(machine_id),
                    "value": _MASK,
                    "source": "yaml",
                    "machine": machine_id,
                }
            )
        # traffic_quota_gb：纯 yaml 字段
        quota = (entry or {}).get("traffic_quota_gb")
        if quota is not None:
            rows.append(
                {
                    "key": "machines.yaml → {}[].traffic_quota_gb".format(machine_id),
                    "value": int(quota),
                    "source": "yaml",
                    "machine": machine_id,
                }
            )
        # tailscale / ssh 的 env 覆盖通道（env > yaml 的唯一两字段）
        for base, env_key in (("tailscale", "MACHINE_TAILSCALE_<ID>"), ("ssh", "MACHINE_SSH_<ID>")):
            resolved = _detect_env_key(env_key, machine_id)
            if resolved in masked_machines:
                rows.append(
                    {
                        "key": resolved,
                        "value": _MASK,
                        "source": "env",
                        "machine": machine_id,
                    }
                )
    return rows


def _env_entry(name, default=None, sensitive=False):
    """env 字段一条：设了 → env 来源 + 遮蔽判定；没设 → default 来源。

    MACHINE_* 覆盖不在这里（由 machines 段单独出覆盖行）。`sensitive=True`
    用于 key 名不含 token/password/secret/key 但仍须遮蔽的字段（家庭机
    Tailscale IP 等内网地址，与列表 API 同纪律）。
    """
    raw = os.environ.get(name)
    if raw is not None:
        value = _MASK if sensitive else mask_value(name, raw)
        return {"key": name, "value": value, "source": "env"}
    return {"key": name, "value": default, "source": "default"}




def _mask_host_keep_port(value):
    """遮蔽 origin 主机段、保留尾部端口：http://100.x.x.x:9999 → http://***:9999。

    e329e27 纪律：内网地址不经 API 外泄，端口保留便于辨认服务实例。
    无 scheme / 无端口均可处理，尾部斜杠先去掉再识别端口。
    """
    text = str(value or "").strip().rstrip("/")
    scheme, sep, rest = text.partition("://")
    prefix = scheme + "://" if sep else ""
    if not sep:
        rest = scheme
    host, has_port, port = rest.rpartition(":")
    if has_port and port.isdigit():
        return prefix + "***:" + port
    return prefix + "***"


def _hindsight_origin_entry():
    """HINDSIGHT_ORIGIN 展示：env 已配置 → 遮蔽主机保端口；未配置 → 「未配置」。

    代码默认值是宿主机 tailnet IP（hindsight_proxy.DEFAULT_UPSTREAM），按
    e329e27「内网地址不经 API 外泄」纪律绝不回显；env 覆盖值同样遮蔽主机段。
    """
    raw = os.environ.get("HINDSIGHT_ORIGIN")
    if raw and raw.strip():
        return {"key": "HINDSIGHT_ORIGIN", "value": _mask_host_keep_port(raw), "source": "env"}
    return {"key": "HINDSIGHT_ORIGIN", "value": "未配置", "source": "default"}


# ---- 守护线程共享状态（评审新建议 D） -------------------------------------

PROBE_STATE = {
    "boot_utc": datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "last_run_utc": None,
}


def mark_probe_run(when=None):
    """app.py `_bg_machine_probes` 每轮探测后调用；只更新内存状态，不落盘。

    `when` 可传 datetime（默认现在）或 ISO 字符串（测试注入用），统一存 ISO。
    """
    if isinstance(when, str):
        stamp = when
    else:
        stamp = (when or datetime.datetime.now(timezone.utc)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    PROBE_STATE["last_run_utc"] = stamp
    return PROBE_STATE["last_run_utc"]


def last_probe_run_utc():
    return PROBE_STATE.get("last_run_utc")


def probe_run_reference_utc():
    """自监控的 staleness 基准：从未跑过时用进程启动时刻（import 期），
    否则用最近一轮探测。容器刚起来时线程在等首个半点节点，启动 2 周期内
    不算「未运行」，从 boot 起算正好消掉这段预期空窗。"""
    return PROBE_STATE.get("last_run_utc") or PROBE_STATE.get("boot_utc")


# ---- 聚合 ---------------------------------------------------------------


def build_spec(db_path=None):
    """按 CONFIG_SPEC 构建 [group → [entry]]；先机器段（含 env 覆盖），再全局 env 段。"""
    panel_defaults = plugin_contributions.collect('config_defaults').get('panels') or {}
    groups = {
        "runtime": {
            "title": "运行与端口",
            "entries": [
                _env_entry("FLASK_ENV", "production"),
                _env_entry("PORT", "8080"),
                _env_entry("CONSOLE_PUBLIC_URL", ""),
                # 以下默认值均自读取点取（单一来源）；机器常量的 import 期 env 解析
                # 只影响「env 先设后清」的测试边缘态，生产 env 固定无影响
                _env_entry("CONSOLE_HOME_LAN_IP", mc.HOME_LAN_IP),
            ],
        },
        "auth": {
            "title": "鉴权与 CORS",
            "entries": [
                _env_entry("CONSOLE_AUTH_ENABLED", "false"),
                _env_entry("CONSOLE_AUTH_USER", ""),
                _env_entry("CONSOLE_AUTH_PASSWORD", ""),
                _env_entry("CONSOLE_CORS_ORIGINS", ""),
            ],
        },
        "probes": {
            "title": "探测间隔",
            "entries": [
                _env_entry("CONSOLE_PROBE_INTERVAL", str(probe_cache.probe_interval())),
                _env_entry(
                    "CONSOLE_MACHINE_PROBE_INTERVAL",
                    str(probe_cache.machine_probe_interval()),
                ),
                # 家宽公网探测（public_probe）的采样间隔；只读展示，不进可写白名单
                _env_entry(
                    "CONSOLE_PUBLIC_PROBE_INTERVAL",
                    str(probe_cache.public_probe_interval()),
                ),
            ],
        },
        "ssh": {
            "title": "机器 SSH / Tailscale 覆盖",
            "entries": [
                _env_entry("CONSOLE_SSH_KEY_DIR", mc.SSH_KEY_DIR),
                # 家庭机器（mac/win 不进 machines.yaml）的 Tailscale 位（D5）；
                # 内网地址与列表 API 同纪律：sensitive=True 显式遮蔽
                _env_entry("MACHINE_TAILSCALE_MAC", "", sensitive=True),
                _env_entry("MACHINE_TAILSCALE_WIN", "", sensitive=True),
            ],
        },
        "workstation": {
            "title": "Workstation 数据源",
            "entries": [
                _env_entry("GITHUB_TOKEN", version_service.GITHUB_TOKEN),
                _env_entry("RAINDROP_TOKEN", ""),
                _env_entry("RSS_URLS", ""),
            ],
        },
        "ops": {
            "title": "运维注入 / 内嵌服务",
            "entries": [
                _env_entry("OPS_BACKUP_DIR", "../../backup"),
                _env_entry("CONSOLE_ALLOW_DOCKER_EXEC_PROBES", ""),
                # 内网地址纪律：默认值即 tailnet IP，绝不回显（见 _hindsight_origin_entry）
                _hindsight_origin_entry(),
                _env_entry("HINDSIGHT_UPSTREAM_PORT", panel_defaults.get('HINDSIGHT_UPSTREAM_PORT', '未启用或未安装')),
                _env_entry("HINDSIGHT_PROXY_PORT", panel_defaults.get('HINDSIGHT_PROXY_PORT', '未启用或未安装')),
            ],
        },
    }
    return groups


def _applied_schema_version(db_path=None):
    """DB 已应用迁移版本：schema_migrations 表 MAX(version)；拿不到显式 None。

    口径是「数据库实际应用到的迁移」，不是代码 MIGRATIONS 末项——main 领先
    线上一个未部署迁移时两者不一致（集成评审 P2 收敛②）。PG 引擎经
    pg_diagnostics 只读事务（REPEATABLE READ，含 PG 基线 12）；显式 db_path /
    SQLite 引擎按既有 mode=ro 约定：库缺失 / 表不存在 / 空表一律 None，
    不建库、不触发迁移，守住本模块零写路径。
    """
    try:
        with pg_diagnostics.readonly_connection(db_path) as conn:
            row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    except (sqlite3.Error, OSError, Exception):
        return None
    if not row or row["v"] is None:
        return None
    return int(row["v"])


def get_effective_config(db_path=None):
    """顶层响应：附加键 + 机器段 + 分组字段。

    PG-04 新增非敏感存储字段（提案 §6.4）：`storage_engine` 与 `database`
    （PG 时代的脱敏连接探针；SQLite 过渡期为 None）。

    **PG-05 集成修正**：探针判据从 `db_path is None` 改为
    `pg_diagnostics.is_online_postgres(db_path)`。应用接线传的是
    `lambda: DB_PATH`（在线库位置），旧判据因此**恒为 None**——`/api/config/effective`
    在真实 PG 实例上永远报不出 database，`verify_deploy.py` 核验项 19 只能一直 FAIL。
    与诊断模块的同类收紧（见 `pg_diagnostics` 模块 docstring）保持同一判据。
    """
    ops = ops_status.get_ops_status(db_path=db_path)
    engine = runtime_mode.storage_engine()
    return {
        "generated_at": datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema_version": _applied_schema_version(db_path),
        "storage_engine": engine,
        "database": dbmod.pg_healthcheck()
        if pg_diagnostics.is_online_postgres(db_path)
        else None,
        "running_commit": ops.get("running_commit"),
        "last_probe_run": last_probe_run_utc(),
        "machines": _machines_section(_masked_machine_values()),
        "groups": build_spec(db_path),
    }


def register_routes(app, resolve_paths=None):
    """挂到既有 app 上（同 inspection_report：resolve_paths 每请求求值）。"""

    @app.route("/api/config/effective", methods=["GET"])
    def get_effective():
        """设置页「关于 / 有效配置」的只读数据：逐字段的值与来源（env / yaml / 默认）。

        零写路径；敏感值遮蔽与内网地址不外泄的完整纪律见本模块 docstring。
        """
        db_path = resolve_paths() if resolve_paths else None
        return jsonify(get_effective_config(db_path=db_path))

    return app
