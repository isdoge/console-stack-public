# -*- coding: utf-8 -*-
"""运维可见性：只读聚合 running commit / 版本采集 / feed 新鲜度 / 备份 tag / 部署留痕。

2026-08-03 的只读核验曾判定容器内 running_commit 与 last_backup_tag 恒 null
（镜像无 .git、无 GIT_COMMIT 注入、宿主 backup/ 未挂入），当时结论是「不做构建注入、
不挂 backup/」。C1-D0 推翻了这两条——半残的运维状态卡才是真问题：

| 字段 | 容器内 | 依据 |
|---|---|---|
| running_commit | 可得 | D0-1：Dockerfile `ARG GIT_COMMIT`，deploy.sh 在 build 前导出 |
| versions_last_checked | 可得 | versions.last_checked，DB 已挂载 → MAX(last_checked) |
| feed_last_fetched | 可得 | feed_cache.fetched_at → MAX(fetched_at) |
| last_backup_tag | 可得 | D0-2：compose 将宿主备份目录只读挂到 `/app/backup` |
| last_deploy | 可得 | D0-3：deploy.sh 重建后写 `backup/last_deploy.json` |
| last_probe_run | 可得 | C10-a：`_bg_machine_probes` 每轮探测后写内存时间戳 |

拿不到的字段一律显式 None，不猜、不用当前时间冒充采样时间。
"""
import datetime
import json
import os
import re
import subprocess

import composition
import composition_identity
import monitoring_business
import paths
import pg_diagnostics
import plugin_contributions

# 可选覆盖：若将来由编排层注入则优先使用；当前生产镜像无此注入。
_COMMIT_ENV_KEYS = ("GIT_COMMIT", "RUNNING_COMMIT")

# F17：宿主定时备份（backup_db.py）落在可见 backup 目录的 timed/ 子目录里，
# 与它写的成功标记 status.json。容器只读挂载统一宿主备份目录到 /app/backup，
# 所以这里只读标记与文件名，不查库、不跑 quick_check、不读快照内容。
TIMED_SUBDIR = "timed"
TIMED_STATUS_NAME = "status.json"
TIMED_STALE_SECONDS = 26 * 3600
# PG-04：PG 时代 timed 快照是 console.dump.YYYYMMDD（pg_dump -Fc）。
_TIMED_SNAPSHOT_RE = re.compile(r"console\.(?:db|dump)\.\d{8}")


def _resolve_backup_dir(backup_dir=None):
    if backup_dir is not None:
        return backup_dir
    env = (os.environ.get("OPS_BACKUP_DIR") or "").strip()
    if env:
        return str(composition.root_path("backup"))
    if os.environ.get("CONSOLE_COMPOSITION_FILE"):
        return str(composition.root_path("backup"))
    # 与 assets.yaml / panels.yaml 同模式：容器内是 /app/backup（D0-2 的只读挂载点），
    # 开发机直跑时退到仓库根 backup/。取第一个真实存在的，两个都没有就返回后者，
    # 让 _last_backup_tag 走「目录不存在 → None」的既有分支。
    candidates = (
        os.path.join(paths.BACKEND_DIR, "backup"),
        os.path.normpath(os.path.join(paths.BACKEND_DIR, "..", "..", "backup")),
    )
    for path in candidates:
        if os.path.isdir(path):
            return path
    return candidates[-1]


def _running_commit(repo_dir=None):
    for key in _COMMIT_ENV_KEYS:
        val = (os.environ.get(key) or "").strip()
        if val:
            return val

    root = repo_dir or os.path.normpath(os.path.join(paths.BACKEND_DIR, "..", ".."))
    git_dir = os.path.join(root, ".git")
    if not os.path.isdir(git_dir):
        return None
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        text = out.decode("utf-8", errors="replace").strip()
        return text or None
    except (OSError, subprocess.SubprocessError):
        return None


def _max_text(conn, sql):
    """取单值 MAX；SQL 必须以 `AS v` 具名列（PG dict_row 不支持 row[0]）。"""
    try:
        row = conn.execute(sql).fetchone()
    except Exception:
        return None
    if not row:
        return None
    val = row["v"]
    if val is None:
        return None
    text = str(val).strip()
    return text or None


def _last_backup_tag(backup_dir):
    """从可见的 backup/gitsha.<tag> 取最新 tag；目录不可读或不存在则 null。

    跳过 ``preroll-`` 前缀：那是部署前的演练备份（2026-07-30 预演遗留），
    字典序里 p > 数字会让它永远压过真实部署 tag。
    """
    if not backup_dir or not os.path.isdir(backup_dir):
        return None
    try:
        names = os.listdir(backup_dir)
    except OSError:
        return None
    tags = []
    for name in names:
        if name.startswith("gitsha.") and len(name) > len("gitsha."):
            tags.append(name[len("gitsha.") :])
    tags = [tag for tag in tags if not tag.startswith("preroll-")]
    if not tags:
        return None
    tags.sort(reverse=True)
    return tags[0]


_LAST_DEPLOY_KEYS = ("commit", "backup_tag", "deployed_at", "migrations_applied")


def _last_deploy(backup_dir):
    """读 deploy.sh 写的 backup/last_deploy.json（D0-3）；缺文件/坏 JSON 一律 None。

    只取白名单四键，多余字段丢弃：这份文件由部署脚本生成，但它落在挂载目录里，
    没必要把未知内容原样透给 API。
    """
    if not backup_dir:
        return None
    path = os.path.join(backup_dir, "last_deploy.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {key: data.get(key) for key in _LAST_DEPLOY_KEYS}


def _timed_result(state, **fields):
    result = {
        "state": state,
        "last_attempt": None,
        "last_success": None,
        "snapshot": None,
        "stage": None,
        "engine": None,
    }
    result.update(fields)
    return result


def _parse_timestamp(text):
    """带时区的 ISO8601 → UTC；裸时间戳/坏格式一律 None（不猜时区）。"""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        moment = datetime.datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(datetime.timezone.utc)


def _text_or_none(value):
    return value if isinstance(value, str) else None


def _has_timed_snapshot(timed_dir):
    try:
        names = os.listdir(timed_dir)
    except OSError:
        return False
    return any(_TIMED_SNAPSHOT_RE.fullmatch(name) for name in names)


def _timed_backup(backup_dir):
    """定时备份状态：只读 `timed/status.json` 与它引用的文件名。

    判据（F17）：never=可读空目录且无记录；unknown=标记/文件不可读、损坏，
    或只有旧快照没有成功标记；failed=标记里最近一次尝试明确失败；
    ok=成功标记且引用文件存在、last_success 年龄 ≤26 小时；stale=超过 26 小时。
    failed/unknown/stale 仍原样带出已知 last_success，不伪装成本次成功。
    """
    if not backup_dir:
        return _timed_result("never")
    timed_dir = os.path.join(backup_dir, TIMED_SUBDIR)
    if not os.path.isdir(timed_dir):
        return _timed_result("never")
    try:
        with open(
            os.path.join(timed_dir, TIMED_STATUS_NAME), "r", encoding="utf-8"
        ) as handle:
            data = json.load(handle)
    except OSError:
        # 标记缺失/不可读：有本脚本命名的快照说明曾经跑过但没留痕 → unknown。
        return _timed_result("unknown" if _has_timed_snapshot(timed_dir) else "never")
    except ValueError:
        return _timed_result("unknown")
    if not isinstance(data, dict):
        return _timed_result("unknown")

    known = {
        "last_attempt": _text_or_none(data.get("last_attempt")),
        "last_success": _text_or_none(data.get("last_success")),
        "snapshot": _text_or_none(data.get("snapshot")),
        "stage": _text_or_none(data.get("stage")),
        # PG-04：engine 标明该组备份属于哪个存储引擎（v2 status.json）。
        "engine": _text_or_none(data.get("engine")),
    }
    if data.get("ok") is False:
        return _timed_result("failed", **known)
    if data.get("ok") is not True:
        return _timed_result("unknown", **known)

    moment = _parse_timestamp(known["last_success"])
    if moment is None:
        return _timed_result("unknown", **known)
    name = known["snapshot"]
    if (
        not name
        or os.path.basename(name) != name
        or not os.path.isfile(os.path.join(timed_dir, name))
    ):
        return _timed_result("unknown", **known)
    age = (datetime.datetime.now(datetime.timezone.utc) - moment).total_seconds()
    return _timed_result("ok" if age <= TIMED_STALE_SECONDS else "stale", **known)


def _last_probe_run():
    """机器探针守护线程上次跑完的时刻（C10-a 自监控）。导入放函数内，
    避免 ops_status ← effective_config 的循环导入。"""
    try:
        import effective_config as ec

        return ec.last_probe_run_utc()
    except Exception:
        return None


def get_ops_status(db_path=None, backup_dir=None, repo_dir=None):
    """返回七键字典；拿不到的字段显式 None，不猜（F17 起含 `timed_backup`）。

    在线读取经 pg_diagnostics 只读入口：PG 引擎走门面只读事务，显式
    db_path/SQLite 引擎沿用既有约定（PG-04）。
    """
    with pg_diagnostics.readonly_connection(db_path) as conn:
        versions_last_checked = _max_text(
            conn, "SELECT MAX(last_checked) AS v FROM versions"
        )
    feed_last_fetched = plugin_contributions.collect('ops_status', db_path=db_path).get('feed_last_fetched')

    resolved_backup = _resolve_backup_dir(backup_dir)
    composition_identity.verify_static(str(composition.root_path("static")))
    return {
        "running_commit": _running_commit(repo_dir=repo_dir),
        "versions_last_checked": versions_last_checked,
        "feed_last_fetched": feed_last_fetched,
        "last_backup_tag": _last_backup_tag(resolved_backup),
        "last_deploy": _last_deploy(resolved_backup),
        # C10-a 自监控（评审新建议 D）：守护线程每轮 mark_probe_run() 更新内存时间戳
        "last_probe_run": _last_probe_run(),
        # F17：宿主定时备份的成功标记（只读 status.json，不逐请求查全库）
        "timed_backup": _timed_backup(resolved_backup),
        # B6-4：正式监控业务源恒为 Console PG。
        "monitoring_business_source": monitoring_business.business_source(),
    }
