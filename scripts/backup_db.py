#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""console.db 定时备份（NAS 宿主 crontab 每日调用）。

活动库按显式部署引擎备份（NAS 为 PG）。宿主目录由 host_paths 统一解析：
NAS 定时备份写 /opt/.bak/console-stack/timed，部署组在其父目录；
本脚本只按保留期回收自己前缀，真实 DB 目录中的操作锁不随备份路径移动。

手动用法：`cd /opt/console-stack && python3 scripts/backup_db.py`。
宿主 cron 使用 `backup_runtime.py publish-runtime ROOT` 发布的稳定入口，避免回滚
checkout 恢复旧脚本后绕过互斥锁；`cron-migration ROOT --dry-run` 只生成迁移计划。
修改备份实现后重新 publish-runtime，固定 cron 命令不变；本脚本不安装 cron。
备份用 `db.backup_database`（SQLite backup API，在线一致性快照，无需停容器）先写到
最终文件**同目录**的唯一临时文件，跑一次 `PRAGMA quick_check` 确认结果是 `ok`，
通过才 `os.replace` 到最终文件名 `console.db.YYYYMMDD`（同目录才能保证 `os.replace`
是同一文件系统内的原子重命名）。备份异常、quick_check 未通过或替换失败时：旧的
最终文件（如存在）原样保留，临时文件被清理，脚本非零退出并打印可读原因——不会
再出现「进程中断留下半份最终备份」。保留 `KEEP_DAYS`（默认 14）天，时间戳取文件
名里的日期；retention 可恢复归档且只精确匹配本脚本命名（`console.db.` + 8 位日期），deploy.sh
的 `console.db.$TAG`（含时分秒后缀）和任何临时文件都不会被误删。
"""
import datetime
import fcntl
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "console" / "backend"))

import db as dbmod  # noqa: E402
import pg_backup  # noqa: E402
import pg_migrations  # noqa: E402

if __package__:
    from . import backup_attachments
    from .host_paths import timed_backup_dir, trash_dir
else:
    import backup_attachments
    from host_paths import timed_backup_dir, trash_dir

# Pay 适配器是私人组合的附件实现（R04）：公共无附件路径绝不 import 它。
# 顶层引用改为惰性函数，公共 controller 固化副本因此不需要这个模块也能工作；
# legacy/私人路径在 `_pay_module()` 里按需导入。
SUFFIX = ".pay.tar"


def _pay_module():
    if __package__:
        from . import backup_pay
    else:
        import backup_pay
    return backup_pay


def __getattr__(name):
    # 兼容过渡（PEP 562）：既有消费者（backup_runtime 的清单/校验路径与旧测试）
    # 仍按 `backup_db.backup_pay` 取模块；属性访问才触发导入，import backup_db
    # 本身（公共无附件 controller 路径）不再连带私人模块。新代码用 `_pay_module()`。
    if name == "backup_pay":
        return _pay_module()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

BACKUP_ROOT = timed_backup_dir(ROOT)
KEEP_DAYS = 14
DB_RELATIVE = "console/backend/data/console.db"
# F17：状态标记与快照同目录原子发布；容器内 ops_status 只读它判新鲜度。
STATUS_NAME = "status.json"
# v2：增加 engine 字段（sqlite/postgres）。v1 的消费者按 dict.get 读取，未知键被忽略，
# 新消费者在 engine 缺失时按 sqlite 处理（旧标记只有 SQLite 时代产生）。
STATUS_VERSION = 2

# --- G05：备份 manifest 的内容身份 -------------------------------------------------
#
# 备份不是「有个 dump 文件」就算数：manifest 必须描述**这一份 dump 的内容**，而
# 描述只能来自实库现查。`pg-backup-manifest-1` 的必填语义字段里，原先
# schema_version/schema_checksum/pg_client_version/row_digest/archive_manifest_version
# 五项可空却照样写 `status="complete"`——演练能过只是因为调用方手工补了参数，
# 标准链（cron/rollout 的标准调用）本身不成立。现在缺一即整次备份失败。
PG_TABLES = tuple(pg_migrations.SCHEMA_TABLES)
# 金额列白名单。`backup_db` **不能** import `pg_migration_lib`（runtime 控制器只捆绑
# `backup_runtime.controller_sources()` 里那几个模块），所以这份副本由
# `test_backup_db_pg.py::test_pg_amount_columns_match_migration_lib` 钉死与
# `pg_migration_lib.AMOUNT_COLUMNS` 全等——不靠人记，也不在这里造第二套金额语义
# （本文件只对整列求和，金额的规范编码仍是 `money.canonical` 的职责）。
PG_AMOUNT_COLUMNS = {"ledger_tx": ("amount",), "renewals": ("cost",)}
ROW_DIGEST_ALGORITHM = pg_backup.ROW_DIGEST_ALGORITHM
REQUIRED_MANIFEST_FIELDS = (
    "manifest_version", "engine", "schema_version", "schema_checksum", "source_commit",
    "pg_server_version", "pg_client_version", "database_identity", "started_at",
    "finished_at", "members", "row_digest", "archive_manifest_version", "status",
)


def operation_lock_path(db_path):
    """All entrypoints lock the same real data directory, including symlink aliases."""
    return Path(db_path).resolve().parent / ".console-stack-operation.lock"


@contextmanager
def operation_lock(db_path):
    path = operation_lock_path(db_path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"部署/回滚/定时备份正在运行，互斥锁: {path}") from None
        yield fd
    finally:
        os.close(fd)  # Never unlink a lock file: that would allow a second inode/lock.


class BackupIntegrityError(RuntimeError):
    """临时快照未通过 PRAGMA quick_check 时抛出。

    backup_database 自身的异常（IO 错误、源库不可读等）不包装，原样上抛，
    调用方看到的异常类型即真实故障类型。
    """


def _checkpoint_wal(path):
    """对备份目标做 TRUNCATE checkpoint，合并 WAL 并删除 sidecar。

    源库可能是 WAL 模式；SQLite backup API 会让 dest 继承 journal_mode，
    在某些平台（如 CI 的 Alpine musl）上 dest 关闭后残留 -wal/-shm sidecar，
    导致 ``glob("console.db.*")`` 误匹配。checkpoint 后再切回 DELETE 模式彻底清理，
    并显式删除残留的 sidecar 文件（DELETE 模式切换不一定立即删除已存在的 -shm）。
    """
    path = Path(path)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.commit()
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        sidecar.unlink(missing_ok=True)


def _quick_check(path):
    """对刚写好的临时快照跑一次 PRAGMA quick_check。

    只读 URI 连接（`file:...?mode=ro`），与 `export_data.py` / `effective_config.py`
    / `settings_store.py` 已有的只读读取惯例一致，额外加 `immutable=1`：
    `db.backup_database` 用 SQLite backup API 逐页复制，源库在 WAL 模式下连接
    （`db.get_connection` 固定 `PRAGMA journal_mode=WAL`），页 1 头部的
    journal 模式标记会一并被复制到临时快照——即使快照本身没有配套 `-wal` 文件。
    纯 `mode=ro` 打开这种「头部写着 WAL、但无 `-wal` 伴生文件」的库会直接
    `OperationalError: unable to open database file`（实测复现）；`immutable=1`
    告诉 SQLite 该文件在打开期间不会变化，从而跳过 WAL/共享内存握手，是
    SQLite 官方文档给出的只读打开方案，不修改临时文件、不产生 `-shm`/`-wal`
    边车文件。返回值拼接所有结果行；健康库精确返回 `'ok'`。
    """
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True, timeout=5)
    try:
        rows = conn.execute("PRAGMA quick_check").fetchall()
    finally:
        conn.close()
    return "; ".join(str(row[0]) for row in rows) if rows else ""


def write_snapshot(dest, db_path):
    """在 dest 同目录写唯一临时文件、备份、quick_check，通过才原子替换到 dest。

    - 临时文件与 `dest` 同目录，保证 `os.replace` 是同一文件系统内的原子重命名。
    - quick_check 结果不等于 `'ok'` 时抛 `BackupIntegrityError`；`backup_database`
      自身异常原样上抛。两种失败路径都会清理临时文件，`dest`（若已存在）保持不变。
    - 首次备份（`dest` 尚不存在）同样适用：`os.replace` 在目标不存在时直接创建。

    返回成功写入的 `dest`（`Path`）。
    """
    if not Path(db_path).is_file():
        raise FileNotFoundError(f"源数据库不存在: {db_path}")
    dest = Path(dest)
    tmp = tempfile.NamedTemporaryFile(
        delete=False, dir=str(dest.parent), prefix=f"{dest.name}.tmp-"
    )
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        dbmod.backup_database(str(tmp_path), str(db_path))
        # 源库可能是 WAL 模式；SQLite backup API 会让 dest 继承 journal_mode，
        # 在某些平台（如 CI 的 Alpine musl）上 dest 关闭后残留 -wal/-shm sidecar。
        # 对 dest 做 TRUNCATE checkpoint，把 WAL 合并进主文件并删除 sidecar。
        _checkpoint_wal(tmp_path)
        result = _quick_check(tmp_path)
        if result != "ok":
            raise BackupIntegrityError(
                f"临时快照 quick_check 未通过（{result!r}），已保留旧备份并丢弃临时文件"
            )
        os.replace(str(tmp_path), str(dest))
        # 替换后再次 checkpoint：os.replace 不带 sidecar，但 dest 若被前一步标记为 WAL。
        _checkpoint_wal(dest)
    except BaseException:
        # BaseException 而非 Exception：Ctrl-C（KeyboardInterrupt）中断时也要清理临时文件。
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    return dest


def prune_old_backups(backup_root, keep_days, *, trash_root=None):
    """Archive expired timer groups without permanently removing their contents.

    Only exact console.db/dump.YYYYMMDD names and their companions qualify.
    The DB member moves last, so an interrupted group remains discoverable on retry.
    Direct callers default to an isolated local trash; production supplies host trash.
    """
    if __package__:
        from .backup_runtime import atomic_json, file_digest, move_preserving
    else:
        from backup_runtime import atomic_json, file_digest, move_preserving
    backup_root = Path(backup_root).resolve()
    trash_root = Path(trash_root) if trash_root is not None else backup_root / ".trash"
    if trash_root.is_symlink():
        raise ValueError("定时备份回收根不能是 symlink")
    cutoff = datetime.date.today() - datetime.timedelta(days=keep_days)
    removed = 0
    sources = set(backup_root.glob("console.*"))
    pending = set()
    # The DB may already have moved when the completion journal write is interrupted.
    for journal in trash_root.glob("console.*/manifest.json"):
        if not re.fullmatch(r"console\.(?:db|dump)\.\d{8}", journal.parent.name):
            continue
        if json.loads(journal.read_text()).get("status") != "complete":
            pending.add(backup_root / journal.parent.name)
    sources.update(pending)
    for source in sorted(sources):
        match = re.fullmatch(r"console\.(?:db|dump)\.(\d{8})", source.name)
        if not match:
            continue
        if source not in pending and datetime.datetime.strptime(match.group(1), "%Y%m%d").date() >= cutoff:
            continue
        target = trash_root / source.name
        if target.is_symlink():
            raise ValueError("定时备份回收目标不能是 symlink")
        journal = target / "manifest.json"
        # Companions precede the database completion marker.
        names = [source.name + suffix for suffix in (MANIFEST_SUFFIX, SUFFIX)] + [source.name]
        if journal.exists():
            manifest = json.loads(journal.read_text())
            members = manifest.get("members", {})
            if (manifest.get("source") != str(source) or manifest.get("status") not in {"moving", "complete"}
                    or not isinstance(members, dict)
                    or source.name not in members or not set(members).issubset(names)
                    or any(not isinstance(d, str) or not re.fullmatch(r"[0-9a-f]{64}", d)
                           for d in members.values())):
                raise ValueError("定时备份回收清单不匹配")
        else:
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            if any(target.iterdir()):
                raise FileExistsError("定时备份回收目录非空且没有清单")
            members = {}
            for name in names:
                path = backup_root / name
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise ValueError("定时备份成员必须是普通文件")
                if path.exists():
                    members[name] = file_digest(path)
            manifest = {"source": str(source), "status": "moving", "members": members}
            atomic_json(journal, manifest)
        for name in names:
            if name in members:
                move_preserving(backup_root / name, target / name, members[name])
            elif (backup_root / name).exists() or (target / name).exists():
                raise ValueError("定时备份清单外出现成员")
        manifest["status"] = "complete"
        atomic_json(journal, manifest)
        removed += 1
    return removed


def _now_iso():
    """带本地时区偏移的 ISO8601（秒精度）；容器内按同一格式解析年龄。"""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_write_json(path, payload):
    """同目录唯一临时文件 + os.replace：读者永远看不到半份 JSON。"""
    path = Path(path)
    tmp = tempfile.NamedTemporaryFile(
        delete=False, dir=str(path.parent), prefix=f"{path.name}.tmp-"
    )
    tmp_path = Path(tmp.name)
    try:
        with tmp:
            tmp.write(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(str(tmp_path), str(path))
    except BaseException:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    return path


def read_status(backup_root):
    """读已发布的状态标记；缺失/坏 JSON/非对象一律 None（不猜）。"""
    try:
        with open(Path(backup_root) / STATUS_NAME, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _known_text(value):
    return value if isinstance(value, str) else None


def write_status(backup_root, *, ok, attempt, stage=None, last_success=None, snapshot=None,
                 engine=None):
    """原子发布状态标记。成功与失败都写：消费者靠它区分「本次失败」与「上次成功」。

    engine 标明该组备份属于哪个存储引擎（v2 新增）；缺省按当前配置判定，
    失败留痕时沿用上一次标记的 engine，避免消费者把两次引擎混为一谈。
    """
    backup_root = Path(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    if engine is None:
        engine = active_engine()
    return _atomic_write_json(
        backup_root / STATUS_NAME,
        {
            "version": STATUS_VERSION,
            "engine": engine,
            "last_attempt": attempt,
            "last_success": last_success,
            "snapshot": snapshot,
            "ok": ok,
            "stage": stage,
        },
    )


def _record_failure(backup_root, stage, attempt):
    """失败也留痕，并保留上一次已知成功；标记写不进去时只放弃留痕，不改退出码。"""
    try:
        prior = read_status(backup_root) or {}
        write_status(
            backup_root,
            ok=False,
            stage=stage,
            attempt=attempt,
            last_success=_known_text(prior.get("last_success")),
            snapshot=_known_text(prior.get("snapshot")),
            engine=_known_text(prior.get("engine")),
        )
    except Exception:
        pass


# --- PG 引擎支持（PG-04；提案 §7.1/§7.2、契约 pg-backup-manifest-1） ----------------
# SQLite 路径保持冻结：旧备份仍可读/可恢复（提案 §7.1），恢复入口按 manifest.engine
# 或成员名分派，永不按扩展名猜。

PG_DUMP_PREFIX = "console.dump"
MANIFEST_SUFFIX = ".manifest.json"
DEFAULT_PG_CONTAINER = "console-postgres"
DEFAULT_PG_USER = "console_admin"
DEFAULT_PG_DATABASE = "console"


def active_engine():
    """定时备份针对的在线存储引擎；与 runtime_mode 同一环境变量来源。"""
    import runtime_mode

    return runtime_mode.storage_engine()


def default_pg_commands(container=None, user=None, database=None):
    """NAS 宿主侧执行 PG 备份的 docker exec 命令前缀。

    dump 在 console-postgres 容器内执行（提案 §7.1），密码走容器内配置，
    不进命令行参数、不进日志；宿主只负责把 stdout 原子落盘。
    """
    container = container or os.environ.get("CONSOLE_PG_BACKUP_CONTAINER", DEFAULT_PG_CONTAINER)
    user = user or os.environ.get("CONSOLE_PG_BACKUP_USER", DEFAULT_PG_USER)
    database = database or os.environ.get("CONSOLE_PG_BACKUP_DB", DEFAULT_PG_DATABASE)
    dump_cmd = ["docker", "exec", container, "pg_dump", "-U", user, "-Fc", database]
    list_cmd = ["docker", "exec", "-i", container, "pg_restore", "--list"]
    psql_base = ["docker", "exec", container, "psql", "-U", user, "-d", database, "-Atc"]
    return {
        "dump": dump_cmd,
        "list": list_cmd,
        "psql": psql_base,
        "server_version": [*psql_base, "SHOW server_version"],
        "database": database,
    }


def _run_streaming(args, dest_tmp, *, stdin_path=None, timeout=600, quiet=False):
    return pg_backup.run_streaming(
        args, dest_tmp, stdin_path=stdin_path, timeout=timeout, quiet=quiet,
    )


def _read_command_line(command, timeout=30):
    return pg_backup.read_command(command, timeout)


def _git_commit():
    """备份这份数据时**运行的代码**的 commit。

    `ROOT` 可被部署布局改写（它同时决定数据目录与备份根），脚本自身所在仓库才是
    「跑的是哪版代码」的可靠锚点：`source_commit` 描述的是代码版本，不是被备份的
    数据目录。两处都不在仓库里才返回 None（由 manifest 完整性闸门拒绝发布）。
    """
    for cwd in (ROOT, Path(__file__).resolve().parents[1]):
        try:
            result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd,
                                    capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and (result.stdout or "").strip():
            return result.stdout.strip()
    return None


def _dump_name_tag(name):
    """`console.dump.<tag>` → tag；形态不合法返回 None。

    tag 白名单与 deploy/rollback 的备份标签同口径（`backup_runtime.preflight`）：
    定时备份是 8 位日期，**回滚前置快照是 `preroll-<run_id>`**——原先只认前者，
    于是 PG 引擎下 `snapshot_group` 100% 抛 ValueError，标准回滚链拿不到恢复前
    快照（G05 真实形态实测）。
    """
    if not name.startswith(PG_DUMP_PREFIX + "."):
        return None
    tag = name[len(PG_DUMP_PREFIX) + 1:]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", tag) or ".." in tag:
        return None
    return tag


def _snapshot_sql(snapshot):
    return pg_backup.snapshot_sql(snapshot)


def exported_pg_snapshot(psql_cmd, *, timeout=600):
    return pg_backup.exported_pg_snapshot(psql_cmd, timeout=timeout)


def _psql_rows(psql_cmd, sql, *, timeout=120, stage="", snapshot=None):
    return pg_backup.query_rows(psql_cmd, sql, timeout=timeout, stage=stage, snapshot=snapshot)


def pg_schema_identity(psql_cmd, *, timeout=60, snapshot=None):
    """活库的 schema 身份：最高版本 + 该版本记录的迁移指纹。

    与 `pg_migrations` 的代码基线比对（`matches_code`）——备份描述的是一个
    schema 与当前代码不同步的库时，那正是切流最危险的形态，宁可让备份失败。
    """
    rows = _psql_rows(psql_cmd, "SELECT version, checksum FROM schema_migrations ORDER BY version",
                      timeout=timeout, stage="schema_migrations", snapshot=snapshot)
    if not rows:
        return None
    versions = {}
    for row in rows:
        version_text, _, checksum = row.partition("|")
        if not version_text.strip().isdigit():
            return None
        version = int(version_text.strip())
        versions[version] = {"checksum": checksum.strip() or None}
    identity = pg_migrations.migration_identity(versions)
    return {
        "schema_version": identity["schema_version"],
        "schema_checksum": identity["checksum"],
        "matches_code": identity["compatible"],
    }


def _digest_sql():
    return pg_backup.digest_sql(PG_TABLES, PG_AMOUNT_COLUMNS)


def row_digest_of(tables):
    return pg_backup.row_digest_of(tables)


def pg_content_digest(psql_cmd, *, timeout=120, snapshot=None, tables=None,
                      amount_columns=None):
    """活库（或恢复库）逐表内容摘要；v1 冻结 25 表，v2 传实表清单（R07）。"""
    return pg_backup.content_digest(
        psql_cmd, PG_TABLES if tables is None else tuple(tables),
        amount_columns=PG_AMOUNT_COLUMNS if amount_columns is None else amount_columns,
        timeout=timeout, snapshot=snapshot,
    )


def dump_client_version(dump_cmd):
    """`pg_dump --version` 的输出；命令里找不到 pg_dump 可执行文件时返回 None。

    按 basename 找（`docker exec … pg_dump …` 与 `/usr/lib/postgresql/16/bin/pg_dump`
    两种形态都要认），版本问的是**同一个**可执行文件，不猜「系统里那个」。
    """
    args = list(dump_cmd or [])
    index = next((i for i, arg in enumerate(args)
                  if os.path.basename(str(arg)) == "pg_dump"), None)
    if index is None:
        return None
    return _read_command_line([*args[:index + 1], "--version"])


def _build_manifest(fields, attachments, *, report=None):
    """仅无组件报告且无构建身份的旧入口写v1；组件入口缺证据就拒绝。"""
    import backup_identity
    from backup_contracts import build_backup_manifest, build_backup_manifest_v2

    composition = backup_identity.composition_block()
    if report is None and composition is None:
        return build_backup_manifest(**fields)
    if composition is None:
        raise ValueError("组件备份缺少构建身份，拒绝降级v1")
    backup_identity.check_report_usable(report, composition)
    return build_backup_manifest_v2(
        **fields,
        composition=composition,
        components=backup_identity.components_block(report),
        tables=backup_identity.tables_block(report),
        attachments=backup_identity.attachments_block(attachments),
    )


def _require_complete_manifest(manifest):
    """§7 必填语义字段全到位才允许发布；缺一即整次失败（fail closed）。

    「不发布」而不是「发布一份带 null 的 complete」：备份是开放写入前的门禁
    （§9 D6），一份说不清自己内容的 manifest 比没有备份更危险。
    """
    missing = [key for key in REQUIRED_MANIFEST_FIELDS if not manifest.get(key)]
    if missing or manifest.get("status") != "complete":
        raise RuntimeError(
            f"备份 manifest 必填字段为空 {missing}"
            f"（status={manifest.get('status')!r}），拒绝发布不完整备份"
        )
    from backup_contracts import (
        BACKUP_MANIFEST_VERSION,
        BACKUP_MANIFEST_VERSION_V2,
        validate_backup_manifest_v2,
    )

    version = manifest.get("manifest_version")
    if version == BACKUP_MANIFEST_VERSION_V2:
        # v2 的四块组合事实由契约校验器全量把关；这里只负责「别让半份 v2 发布」。
        validate_backup_manifest_v2(manifest)
        members = manifest["members"]
        attachments = manifest["attachments"]
        dump_members = [member for member in members if not member.get("path", "").endswith(SUFFIX)]
        if (len(dump_members) != 1 or len(members) != 1 + len(attachments)
                or sorted((member for member in members if member not in dump_members),
                          key=lambda member: member["path"]) != attachments
                or len(attachments) > 1
                or any(member["path"] != dump_members[0]["path"] + SUFFIX
                       for member in attachments)):
            raise RuntimeError("v2附件声明与备份成员不符")
        if set(manifest["tables"]) != set(manifest["row_digest"].get("tables", {})):
            raise RuntimeError("v2实表清单与内容摘要覆盖范围不符")
    elif version != BACKUP_MANIFEST_VERSION:
        raise RuntimeError(f"未知的备份 manifest 版本: {version!r}，拒绝发布")
    return manifest


def _fsync_directory(path):
    return pg_backup.fsync_directory(path)


def _publish_pg_group(dest, pending_dump, pending_pay, pending_manifest, *,
                      with_attachment=True):
    """同组发布；无附件的组合不发布附件成员（不留空 Pay 包）。"""
    pairs = [(pending_dump, dest)]
    if with_attachment:
        pairs.append((pending_pay, Path(str(dest) + SUFFIX)))
    pairs.append((pending_manifest, Path(str(dest) + MANIFEST_SUFFIX)))
    return pg_backup.publish_group(pairs)


def _parse_psql_fields(args):
    """从已去掉 docker 前缀的 psql argv 解析 -h/-p/-U/-d。"""
    fields = {}
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-h", "-p", "-U", "-d") and index + 1 < len(args):
            key = {"-h": "host", "-p": "port", "-U": "user", "-d": "dbname"}[arg]
            fields[key] = args[index + 1]
            index += 2
            continue
        if arg.startswith("--"):
            key, _, value = arg.partition("=")
            mapped = {"--host": "host", "--port": "port", "--username": "user",
                      "--dbname": "dbname"}.get(key)
            if mapped and value:
                fields[mapped] = value
        index += 1
    return fields


def _parse_docker_exec_psql(args):
    """解析 `docker exec … psql -U … -d …`；容器名即与 dump 同机的身份。"""
    if len(args) < 3 or args[0] != "docker" or args[1] != "exec":
        return None
    index = 2
    value_flags = {"-u", "--user", "-e", "--env", "-w", "--workdir"}
    while index < len(args):
        arg = args[index]
        if arg in ("-i", "-t", "-T", "--privileged", "--detach-keys"):
            index += 1
            continue
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 2 and set(arg[1:]) <= set("itT"):
            index += 1  # -it / -iT 等合并短选项
            continue
        if arg in value_flags:
            index += 2 if index + 1 < len(args) else 1
            continue
        if arg.startswith("--user=") or arg.startswith("--env=") or arg.startswith("--workdir="):
            index += 1
            continue
        break
    if index >= len(args):
        raise RuntimeError("docker exec 命令缺少容器名，无法建立同 snapshot 连接")
    container = args[index]
    index += 1
    if index >= len(args) or args[index] != "psql":
        raise RuntimeError(
            "docker exec 命令未落到 psql（"
            f"got {args[index] if index < len(args) else None!r}），"
            "拒绝在不明工具上收集组件报告")
    fields = _parse_psql_fields(args[index + 1:])
    user = fields.get("user") or ""
    dbname = fields.get("dbname") or ""
    if not container or not user or not dbname:
        raise RuntimeError(
            "无法从 docker exec psql 命令解析出明确的 container/user/db（"
            f"container={container!r} user={user!r} db={dbname!r}），"
            "拒绝在来源不明连接上收集组件报告")
    return {"container": container, "user": user, "dbname": dbname}


def _psql_tcp_conninfo(psql_cmd):
    """TCP/`psql -h …` 形态：直接映射同一 DB，不回退 CONSOLE_PG_*。"""
    args = [str(a) for a in (psql_cmd or [])]
    if not args:
        raise RuntimeError("未配置 psql 命令，无法在 snapshot 内收集组件报告")
    if args[0] == "docker":
        raise RuntimeError("内部错误：docker exec 形态应走容器本机代理，不得当 TCP 解析")
    # 允许前缀路径中的 psql 二进制
    try:
        start = next(i for i, arg in enumerate(args) if Path(arg).name == "psql")
    except StopIteration as exc:
        raise RuntimeError("未配置 psql 命令，无法在 snapshot 内收集组件报告") from exc
    fields = _parse_psql_fields(args[start + 1:])
    host = fields.get("host") or os.environ.get("PGHOST") or "/tmp"
    port = fields.get("port") or os.environ.get("PGPORT") or "5432"
    user = fields.get("user") or os.environ.get("PGUSER") or ""
    dbname = fields.get("dbname") or os.environ.get("PGDATABASE") or ""
    if not user or not dbname or host.startswith("/"):
        raise RuntimeError(
            "无法从 psql 命令解析出明确的 host/user/db（"
            f"host={host!r} user={user!r} db={dbname!r}），"
            "拒绝在来源不明连接上收集组件报告")
    support = sys.modules.get("pg_test_support")
    if support is not None:
        support.assert_not_production(host, dbname, user)
    from psycopg.conninfo import make_conninfo

    return make_conninfo(host=host, port=port, dbname=dbname, user=user)


def _psql_conninfo(psql_cmd):
    """从 psql 命令前缀解析**同一 DB** 的 psycopg conninfo（R07 修正）。

    支持两种生产形态：
    - `docker exec <c> psql -U <user> -d <db> ...`：由 `_snapshot_readonly_conn`
      经容器本机 trust 代理接入（见该函数）；本函数对 docker 形态不再裸拒绝，
      但也不猜宿主映射端口、不另开 CONSOLE_PG_* 数据源。
    - `psql -h <host> -p <port> -U <user> [-d <db>] ...`：直接映射。
    """
    args = [str(a) for a in (psql_cmd or [])]
    if not args:
        raise RuntimeError("未配置 psql 命令，无法在 snapshot 内收集组件报告")
    if args[0] == "docker":
        # 保留可调用入口：返回值仅用于错误信息；真正连接见代理路径。
        identity = _parse_docker_exec_psql(args)
        raise RuntimeError(
            "docker exec 形态请经 _snapshot_readonly_conn 的容器本机代理连接："
            f"container={identity['container']!r} db={identity['dbname']!r}")
    return _psql_tcp_conninfo(psql_cmd)


@contextmanager
def _docker_exec_pg_proxy(container, *, timeout):
    """宿主 loopback → `docker exec -i <c> nc 127.0.0.1 5432` → 容器本机 PG。

    利用官方镜像本机 trust：不猜宿主映射端口、不把密码打进命令行/日志。
    容器名必须与 dump/psql 命令中的同一身份。
    """
    if not container or any(ch in container for ch in "\n\r\0"):
        raise RuntimeError("docker 容器名无效，拒绝建立同 snapshot 代理")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    host, port = listener.getsockname()
    stop = threading.Event()
    workers = []

    def _relay(client):
        proc = None
        try:
            proc = subprocess.Popen(
                ["docker", "exec", "-i", container, "nc", "127.0.0.1", "5432"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0,
            )

            def client_to_proc():
                try:
                    while not stop.is_set():
                        data = client.recv(65536)
                        if not data:
                            break
                        proc.stdin.write(data)
                        proc.stdin.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    try:
                        proc.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass

            def proc_to_client():
                try:
                    while not stop.is_set():
                        data = proc.stdout.read(65536)
                        if not data:
                            break
                        client.sendall(data)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    try:
                        client.shutdown(socket.SHUT_WR)
                    except (BrokenPipeError, OSError):
                        pass

            threads = [
                threading.Thread(target=client_to_proc, daemon=True),
                threading.Thread(target=proc_to_client, daemon=True),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=max(5, timeout))
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                client.close()
            except OSError:
                pass

    def _accept():
        while not stop.is_set():
            try:
                listener.settimeout(0.5)
                try:
                    client, _ = listener.accept()
                except socket.timeout:
                    continue
            except OSError:
                break
            worker = threading.Thread(target=_relay, args=(client,), daemon=True)
            workers.append(worker)
            worker.start()

    acceptor = threading.Thread(target=_accept, daemon=True)
    acceptor.start()
    try:
        yield host, port
    finally:
        stop.set()
        try:
            listener.close()
        except OSError:
            pass
        acceptor.join(timeout=2)
        for worker in workers:
            worker.join(timeout=2)


@contextmanager
def _snapshot_readonly_conn(psql_cmd, snapshot, *, timeout):
    """报告消费结束前保持导入的只读快照事务；连接身份来自同一psql命令。

    docker exec 形态：经容器本机 trust 代理接入，与 dump 共用容器名/库名，
    不猜宿主映射端口、不读取 CONSOLE_PG_* 另开数据源、不把密码写入 argv/日志。
    """
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from psycopg.rows import dict_row

    pg_backup.snapshot_sql(snapshot)  # 验证外部工具使用的同一snapshot标识。
    args = [str(a) for a in (psql_cmd or [])]
    if args and args[0] == "docker":
        identity = _parse_docker_exec_psql(args)
        support = sys.modules.get("pg_test_support")
        if support is not None:
            # 代理落在 loopback；生产库名/账号仍按测试闸门拦截。
            support.assert_not_production("127.0.0.1", identity["dbname"], identity["user"])
        with _docker_exec_pg_proxy(identity["container"], timeout=timeout) as (host, port):
            conninfo = make_conninfo(
                host=host, port=port, dbname=identity["dbname"], user=identity["user"],
                sslmode="disable",
            )
            with psycopg.connect(
                conninfo, connect_timeout=min(timeout, 30),
                options=f"-c statement_timeout={int(timeout * 1000)}",
                row_factory=dict_row,
            ) as conn:
                conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
                conn.read_only = True
                with conn.transaction():
                    conn.execute(sql.SQL("SET TRANSACTION SNAPSHOT {}").format(
                        sql.Literal(snapshot)))
                    yield conn
        return

    with psycopg.connect(
        _psql_tcp_conninfo(psql_cmd), connect_timeout=min(timeout, 30),
        options=f"-c statement_timeout={int(timeout * 1000)}",
        row_factory=dict_row,
    ) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        with conn.transaction():
            conn.execute(sql.SQL("SET TRANSACTION SNAPSHOT {}").format(sql.Literal(snapshot)))
            yield conn


def _registry_checksum(report):
    """组件登记库的确定性 schema_checksum（非遗产链指纹，不冒充）。

    覆盖登记的**实际身份**：baseline_version（在场时）+ 全部组件行的
    (name, version, origin, checksum, catalog_fingerprint) 规范 JSON 摘要。
    同一登记状态永远推出同一值；登记任何一行变化都会改变 checksum，
    restore 侧比对用它做「登记未漂移」的粗判据，逐表行摘要仍是内容级真判据。
    """
    rows = sorted(
        (entry.get("component"), int(entry.get("version", 0)),
         entry.get("origin"), entry.get("checksum"),
         entry.get("catalog_fingerprint"))
        for entry in report.get("components") or []
        if isinstance(entry, dict)
    )
    material = json.dumps(
        {"baseline_version": report.get("baseline_version"), "components": rows},
        ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _schema_identity(psql_cmd, *, timeout, snapshot, report=None):
    """遗产库沿用冻结链；组件新库使用kernel版本和实际组件登记摘要。"""
    if report is None or "schema_migrations" in report["present_tables"]:
        return pg_schema_identity(psql_cmd, timeout=timeout, snapshot=snapshot)
    versions = [row["version"] for row in report["components"] if row["component"] == "kernel"]
    return {"schema_version": max([report["baseline_version"], *versions]),
            "schema_checksum": _registry_checksum(report), "matches_code": True}


def _collect_component_report(component_report, psql_cmd, snapshot, *, timeout):
    """只接收同snapshot报告回调，并与现场报告核对；dict或过期报告拒绝。"""
    if component_report is None:
        return None
    if isinstance(component_report, dict) or not callable(component_report):
        raise TypeError(
            "component_report 必须是 registry_report 可调用（同 snapshot 收集）；"
            "dict 直传不构成同 snapshot 证据，已拒绝")
    import component_migrations

    with _snapshot_readonly_conn(psql_cmd, snapshot, timeout=timeout) as conn:
        report = component_report(conn)
        actual = component_migrations.registry_report(conn)
        if report != actual:
            raise ValueError("组件报告与同snapshot现场核验不符，拒绝使用外部或过期报告")
        return actual


def write_pg_dump(dest, *, commands=None, timeout=600, schema_version=None, schema_checksum=None,
                  root=None, pay_root=None, attachment=None, component_report=None):
    """PG dump、批次摘要和附件原件使用同一导出 snapshot，校验后同组发布。

    - 临时文件与 dest 同目录（同一文件系统内 os.replace 原子重命名）。
    - 库身份与内容摘要**从活库现查**，调用方传参只能用来「对账」（不符即拒绝），
      不能用来补空；任一 §7 必填语义字段取不到就整次失败——不发布声称 `complete`
      却缺字段的 manifest（G05）。
    - dump 命令失败、产物为空、或 `pg_restore --list` 无法解析归档时：清理临时
      文件、保留旧 dump（若存在）、抛 RuntimeError——不发布半份归档。
    - Pay tar 必含冻结批次的原件、回执和历史 reference；额外保留当前 reference
      与 legacy raw。任何查询/文件校验失败都不动原有好组。
    - `attachment=True` 走组合层适配器（`backup_attachments`）：有附件的组合产出
      真实归档，无附件的公共组合不产出任何假 Pay 包。该参数**默认 None**（按旧
      行为总是写 Pay），因为现役调用方仍是私人组合；公共组合接线在 B7-3/B7-5
      随独立构建一起切换，不在本批改动生产备份组成。
    - 有构建身份时自动收集组件报告；显式`component_report`仅接受同snapshot
      只读连接的回调，返回值须与现场报告一致。报告必须ok=true、登记非空，
      并与实表和构建插件集合一致；缺证据拒绝，不静默降级v1。
    - 完成标记 manifest 最后发布。替换期间先持久保留旧组，普通失败自动恢复；
      进程被杀时私有 `.previous-*` 目录仍保留恢复材料，不发布混合代际的完整标记。

    返回 (dest, manifest_path)。
    """
    from backup_contracts import LEGACY_ARCHIVE_MANIFEST_VERSION

    dest = Path(dest)
    if _dump_name_tag(dest.name) is None:
        raise ValueError(
            f"PG dump 文件名必须形如 {PG_DUMP_PREFIX}.<tag>"
            f"（tag 为字母数字与 ._-，如 20260911 或 preroll-<run_id>）: {dest.name}"
        )
    commands = commands or default_pg_commands()
    psql_cmd = commands.get("psql")
    started = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    tmp = tempfile.NamedTemporaryFile(delete=False, dir=str(dest.parent), prefix=f"{dest.name}.tmp-")
    tmp_path = Path(tmp.name)
    tmp.close()
    toc_path = tmp_path.with_name(tmp_path.name + ".toc")
    pending_manifest = tmp_path.with_name(tmp_path.name + ".manifest")
    pending_pay = tmp_path.with_name(tmp_path.name + SUFFIX)
    pay_path = Path(str(dest) + SUFFIX)
    manifest_path = Path(str(dest) + MANIFEST_SUFFIX)
    try:
        with exported_pg_snapshot(psql_cmd, timeout=timeout) as snapshot:
            import backup_identity

            composition = backup_identity.composition_block()
            if component_report is None and composition is not None:
                import component_migrations
                component_report = component_migrations.registry_report
            report = _collect_component_report(component_report, psql_cmd, snapshot,
                                               timeout=timeout)
            if report is not None:
                backup_identity.check_report_usable(report, composition)
            identity = _schema_identity(psql_cmd, timeout=min(timeout, 60),
                                        snapshot=snapshot, report=report)
            if identity is None:
                raise RuntimeError("无法从 schema_migrations 读取版本与迁移指纹，拒绝生成不完整备份")
            if not identity["matches_code"]:
                raise RuntimeError(
                    f"活库 schema {identity['schema_version']} / 迁移指纹与当前代码基线不符：拒绝备份")
            if schema_version is not None and schema_version != identity["schema_version"]:
                raise RuntimeError(f"传入 schema_version={schema_version} 与活库 {identity['schema_version']} 不符")
            if schema_checksum is not None and identity["schema_checksum"] is not None \
                    and schema_checksum != identity["schema_checksum"]:
                raise RuntimeError("传入 schema_checksum 与活库记录不符")
            # v2摘要覆盖同snapshot全部实表，包含组件/遗产登记表。
            digest_tables = tuple(backup_identity.tables_block(report)) if report is not None else PG_TABLES
            row_digest = pg_content_digest(
                psql_cmd, tables=digest_tables,
                amount_columns={t: cols for t, cols in PG_AMOUNT_COLUMNS.items()
                                if t in digest_tables},
                timeout=timeout, snapshot=snapshot)
            if row_digest is None:
                raise RuntimeError("无法计算活库内容摘要（row_digest），拒绝生成不完整备份 manifest")
            client_version = dump_client_version(commands.get("dump"))
            if not client_version:
                raise RuntimeError("无法读取 pg_dump 客户端版本，拒绝生成不完整备份 manifest")
            _run_streaming([*commands["dump"], "--snapshot=" + snapshot], tmp_path, timeout=timeout)
            if tmp_path.stat().st_size == 0:
                raise RuntimeError("pg_dump 产物为空，拒绝发布")
            query = lambda sql: _psql_rows(psql_cmd, sql, timeout=timeout,  # noqa: E731
                                           stage="文件批次", snapshot=snapshot)
            if attachment is False:
                attachment_member = None
            elif report is not None or attachment:
                # 已移出ledger时仍按实际保留表归档原件，不能只看当前插件目录。
                name = "pay" if report is not None and "ledger_file_batches" in digest_tables else None
                attachment_member = backup_attachments.write_attachments(
                    pending_pay, root=ROOT if root is None else root, query=query, name=name)
                if attachment_member is not None:
                    attachment_member["path"] = pay_path.name
            else:
                attachment_member = _pay_module().write_pay_snapshot(
                    pending_pay, root=ROOT if root is None else root, pay_root=pay_root,
                    query=query)
                attachment_member["path"] = pay_path.name
        _run_streaming(commands["list"], toc_path, stdin_path=tmp_path, timeout=min(timeout, 120))
        members = [{"path": dest.name, "sha256": _file_digest(tmp_path),
                    "bytes": tmp_path.stat().st_size}]
        attachments = []
        if attachment_member is not None:
            members.append(attachment_member)
            attachments.append(attachment_member)
        fields = {
            "engine": "postgres",
            "schema_version": identity["schema_version"],
            "schema_checksum": identity["schema_checksum"],
            "source_commit": _git_commit(),
            "pg_server_version": _read_command_line(commands.get("server_version")),
            "pg_client_version": client_version,
            "database_identity": commands.get("database"),
            "started_at": started,
            "finished_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "members": members,
            "row_digest": row_digest,
            "archive_manifest_version": LEGACY_ARCHIVE_MANIFEST_VERSION,
            "status": "complete",
        }
        manifest = _build_manifest(fields, attachments, report=report)
        _require_complete_manifest(manifest)
        # R04 发布侧矛盾核查：声明无附件的 v2 却带着账本域表或非空批次摘要，
        # 意味着原件没有被同组备份——拒绝发布，而不是发布一份会丢数据的备份。
        _verify_attachmentless_v2(manifest)
        _atomic_write_json(pending_manifest, manifest)
        _publish_pg_group(dest, tmp_path, pending_pay, pending_manifest,
                          with_attachment=attachment_member is not None)
        return dest, manifest_path
    finally:
        tmp_path.unlink(missing_ok=True)
        toc_path.unlink(missing_ok=True)
        pending_manifest.unlink(missing_ok=True)
        pending_pay.unlink(missing_ok=True)


def read_dump_manifest(dump_path):
    """读 dump 侧车 manifest；缺失/坏 JSON/版本或 engine 不符一律 None（不猜）。

    同时认 v1 与 v2：v2 是 v1 的超集，旧备份与新备份都必须能被恢复入口读出来。
    只认 v1 会让「刚写的 v2 备份恢复时被判成无效」，这正是加版本号最危险的
    回归方向。
    """
    from backup_contracts import BACKUP_MANIFEST_VERSION, BACKUP_MANIFEST_VERSION_V2

    path = Path(str(dump_path) + MANIFEST_SUFFIX)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if (not isinstance(data, dict)
            or data.get("manifest_version") not in
            (BACKUP_MANIFEST_VERSION, BACKUP_MANIFEST_VERSION_V2)
            or data.get("engine") != "postgres"):
        return None
    return data


def _digest_diff(claimed, actual):
    """摘要不符时给出可定位到表的原因（不含业务数据原文）。"""
    details = []
    want_tables = (claimed or {}).get("tables") or {}
    got_tables = (actual or {}).get("tables") or {}
    for table in sorted(set(want_tables) | set(got_tables)):
        want, got = want_tables.get(table), got_tables.get(table)
        if want == got:
            continue
        if want is None:
            details.append(f"{table}: 恢复库多出该表")
        elif got is None:
            details.append(f"{table}: 恢复库缺少该表")
        elif want.get("rows") != got.get("rows"):
            details.append(f"{table}: 行数 {got.get('rows')} != {want.get('rows')}")
        elif want.get("row_hash") != got.get("row_hash"):
            details.append(f"{table}: 行内容指纹不符")
        else:
            details.append(f"{table}: 金额合计 {got.get('amounts')} != {want.get('amounts')}")
    # 无差异时返回空串：调用方以「空」表示逐表一致（非空即不符，不能塞兜底文案）。
    return "；".join(details)


def _declares_ledger_tables(manifest):
    """账本当前归属或遗产保留表均需要同组原件。"""
    tables = manifest.get("tables") or {}
    ledger = {"ledger_tx", "ledger_tx_link", "ledger_categories", "ledger_knowledge",
              "ledger_imports", "ledger_file_batches"}
    return bool(ledger & set(tables)) or "plugin:ledger" in tables.values()


def _verify_attachmentless_v2(manifest):
    """v2 无附件备份的发布期/恢复期矛盾证据核查（R04）。

    「无附件」的判定依据是 manifest 的**显式记录**（attachments=[] 且 members
    无 .pay.tar 成员），不是「tar 文件不在」——成员在而文件缺失由成员摘要
    校验拒绝，这里只处理「声明就没有」的组合。声明无附件却带着账本域组件
    或 ledger 保留表的 v2 是自相矛盾：原件不可能凭空消失，拒绝而不是跳过。
    """
    from backup_contracts import BACKUP_MANIFEST_VERSION_V2
    if manifest.get("manifest_version") != BACKUP_MANIFEST_VERSION_V2:
        return
    if manifest.get("attachments"):
        return
    if _declares_ledger_tables(manifest):
        raise RuntimeError(
            "v2 manifest 声明无附件，但 tables 块含账本域归属"
            "（plugin:ledger 组件或 retained ledger 表）："
            "原件与批次记录必须同组备份，拒绝发布/恢复自相矛盾的无附件备份"
        )


def verify_restore(dump_path, *, boot_target, expected_tables=None,
                   expected_schema_version=None, expected_row_digest=None, timeout=600,
                   pay_dest=None):
    """把 PG dump 完整恢复到一次性隔离实例并独立比对（提案 §7.2/§10.1）。

    `pg_restore --list` 可读不等于可恢复；本函数是真判据：
    - 先核对 dump 与侧车 manifest 是同一份产物（成员 sha256），无有效 manifest
      的 dump 不算备份（恢复入口不认）；
    - boot_target() -> (restore_cmd, psql_cmd)：起一个隔离空 PG16 目标并返回
      在其中执行 pg_restore（dump 从 stdin）与 psql 查询的命令前缀；
    - 恢复后核对 schema_migrations 与 manifest 版本、调用者可选的版本要求及表清单；
    - 恢复后**重新计算**逐表内容摘要（行数/行指纹/金额合计）与 manifest 记录比对
      （或调用方给定的 expected_row_digest）——schema 版本与表清单是粗判据，
      空库恢复出 schema12 一样能过，逐值比对才拦得住内容被换掉（G05）。
    - 附件按 manifest 版本**分派**（R04）：v1 保留原强校验一字不动（Pay 快照
      必须存在且全量核对，批次与恢复库逐项对账）；v2 由 manifest 显式记录
      分派——有附件成员走与 v1 相同的 Pay 全量检查，无附件组合不 import
      backup_pay、不查询ledger/Pay；实表归属、组件登记和摘要仍须完整核对。

    返回 {"restored": True, "tables": [...], "schema_version": int,
    "row_digest_match": True, "pay": {...} 或 None（v2 无附件）}。
    """
    dump_path = Path(dump_path)
    manifest = read_dump_manifest(dump_path)
    if manifest is None:
        raise RuntimeError("PG dump 缺少有效的侧车 manifest，不能作为可恢复备份使用")
    _require_complete_manifest(manifest)
    members = [m for m in (manifest.get("members") or []) if isinstance(m, dict)]
    member = next((m for m in members if m.get("path") == dump_path.name), None)
    if member is None:
        raise RuntimeError("侧车 manifest 未记录该 dump 成员，拒绝恢复")
    if member.get("sha256") != _file_digest(dump_path):
        raise RuntimeError("PG dump 文件摘要与 manifest 记录不一致，拒绝恢复")
    if member.get("bytes") != dump_path.stat().st_size:
        raise RuntimeError("PG dump 文件大小与 manifest 记录不一致，拒绝恢复")
    _verify_attachmentless_v2(manifest)
    # v1 或「声明有附件」的 v2：Pay 快照成员必须唯一且文件真实存在（成员摘要
    # 校验在 verify_pay_snapshot 内），缺失/被删 tar 一律拒绝——不以「少了 tar
    # 就跳过应有原件」。
    pay_member_exists = any(
        str(m.get("path", "")).endswith(SUFFIX) for m in members)
    if manifest.get("manifest_version") == "pg-backup-manifest-1" or pay_member_exists:
        _pay_module().verify_pay_snapshot(dump_path, manifest)
        if pay_dest is not None and os.path.lexists(pay_dest):
            raise FileExistsError("Pay 恢复目标已存在，拒绝覆盖")
    claimed = expected_row_digest or manifest.get("row_digest")
    if not claimed:
        raise RuntimeError("侧车 manifest 未记录内容摘要（row_digest），无法独立比对")
    if claimed.get("algorithm") != ROW_DIGEST_ALGORITHM:
        raise RuntimeError(
            f"manifest 记录的内容摘要算法 {claimed.get('algorithm')!r} "
            f"不是 {ROW_DIGEST_ALGORITHM}，无法独立比对"
        )
    # manifest 自校验：整体 digest 必须由自身 tables 推出。只改 digest 字段
    # （表数据原样）时它与恢复库的比对会「逐表一致但整体不同」，那不是恢复出错
    # 而是 manifest 被改坏——先判自相矛盾，报错才指向真正的故障点。
    if row_digest_of(claimed.get("tables") or {}) != claimed.get("digest"):
        raise RuntimeError("侧车 manifest 内部自相矛盾：逐表数据与整体摘要不一致")
    restore_cmd, psql_cmd = boot_target()
    _run_streaming(list(restore_cmd), None, stdin_path=dump_path,
                   timeout=timeout, quiet=True)
    if manifest["manifest_version"] == "pg-backup-manifest-2":
        import backup_identity
        import component_migrations

        with exported_pg_snapshot(psql_cmd, timeout=timeout) as snapshot:
            report = _collect_component_report(component_migrations.registry_report,
                                               psql_cmd, snapshot, timeout=timeout)
            backup_identity.check_report_usable(report, manifest["composition"])
            tables = sorted(report["present_tables"])
            if (backup_identity.tables_block(report) != manifest["tables"]
                    or backup_identity.components_block(report) != manifest["components"]):
                raise RuntimeError("恢复后组件登记/表归属与manifest不符")
            identity = _schema_identity(psql_cmd, timeout=timeout, snapshot=snapshot, report=report)
            if identity is None or identity["schema_checksum"] != manifest["schema_checksum"]:
                raise RuntimeError("恢复后schema指纹与manifest不符")
            restored_version = identity["schema_version"]
            actual = pg_content_digest(
                psql_cmd, tables=tables,
                amount_columns={table: cols for table, cols in PG_AMOUNT_COLUMNS.items() if table in tables},
                snapshot=snapshot, timeout=timeout,
            )
    else:
        version_sql = "SELECT max(version) FROM schema_migrations"
        rows = _psql_rows(psql_cmd, version_sql, timeout=timeout, stage="恢复版本")
        try:
            restored_version = int(rows[0])
        except (TypeError, ValueError, IndexError):
            raise RuntimeError("恢复后schema版本不可读") from None
        tables_sql = ("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
        tables = _psql_rows(psql_cmd, tables_sql, timeout=timeout, stage="恢复表清单")
        if tables is None:
            raise RuntimeError("恢复后表清单不可读")
        actual = pg_content_digest(psql_cmd, timeout=timeout)
    if restored_version != manifest.get("schema_version"):
        raise RuntimeError("恢复后schema版本与manifest记录不符")
    if expected_schema_version is not None and restored_version != expected_schema_version:
        raise RuntimeError(f"恢复后schema版本{restored_version}与预期{expected_schema_version}不符")
    if expected_tables is not None and tables != sorted(expected_tables):
        raise RuntimeError("恢复后表清单与预期不符")
    if actual is None:
        raise RuntimeError("恢复库内容摘要不可读，无法完成独立比对")
    # 逐表比对定位差异；整体 digest 再兜一层——只改 `digest` 字段（表数据原样）
    # 时逐表全等，靠这一步拦住 manifest 内部自相矛盾。
    diff = _digest_diff(claimed, actual)
    if diff or actual["digest"] != claimed.get("digest"):
        detail = diff or (
            "逐表一致但整体摘要不同"
            f"（记录 {str(claimed.get('digest'))[:12]}… / 恢复 {actual['digest'][:12]}…）"
        )
        raise RuntimeError(f"恢复后内容摘要与 manifest 记录不符：{detail}")
    if manifest.get("manifest_version") == "pg-backup-manifest-1" or pay_member_exists:
        # v1 / 声明有附件：原件实际恢复到全新私有目录，与恢复库的非空批次逐项
        # 核对；不覆写既有目录。
        if pay_dest is None:
            pay_dest = Path(tempfile.mkdtemp(dir=dump_path.parent,
                                            prefix=f".{dump_path.name}.restore-")) / "pay"
        pay_report = _pay_module().restore_pay_snapshot(
            dump_path, manifest, dest=pay_dest,
            query=lambda sql: _psql_rows(psql_cmd, sql, timeout=timeout, stage="恢复库文件批次"))
        return {"restored": True, "tables": tables, "schema_version": restored_version,
                "row_digest_match": True, "pay": {**pay_report, "path": str(Path(pay_dest).resolve())}}
    return {"restored": True, "tables": tables, "schema_version": restored_version,
            "row_digest_match": True, "pay": None}


def audit_orphan_batches(*, db_rows, archived, now=None, grace_hours=24):
    """孤立归档核查（提案 §3.2 失败矩阵最后一行）：归档存在但 PG 无批次记录。

    - db_rows: PG `ledger_file_batches` 现有记录的 batch_id 集合（调用方查询；
      表未建成时传空集合——此时一切归档都算待核验，不伪造「无归档」）。
    - archived: 可迭代的 {"batch_id", "archived_at", "bytes"}；archived_at 是
      带时区的 ISO8601 文本。字段缺失/不可解析的条目按未知年龄保留并标注。
    - 只输出脱敏清单（batch_id/时间/状态/字节数），不含原件路径与内容；
      只报告不处理——自动删除与自动补写都被提案禁止。

    返回 {"checked": int, "orphans": [{"batch_id", "archived_at", "bytes",
    "age_hours"|None, "status": "pending_review"}]}。
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    known = {row for row in (db_rows or ())}
    orphans = []
    checked = 0
    for entry in archived or ():
        checked += 1
        batch_id = entry.get("batch_id")
        if batch_id and batch_id in known:
            continue
        if not batch_id:
            # 缺 batch_id 的归档永远无法与 PG 记录对上、无法按 ID 重试，
            # 直接待人工核验，不静默丢弃，也不等宽限期。
            orphans.append({
                "batch_id": None,
                "archived_at": entry.get("archived_at")
                if isinstance(entry.get("archived_at"), str) else None,
                "bytes": entry.get("bytes") if isinstance(entry.get("bytes"), int) else None,
                "age_hours": None,
                "status": "pending_review",
            })
            continue
        age_hours = None
        archived_at = entry.get("archived_at")
        if isinstance(archived_at, str) and archived_at.strip():
            try:
                moment = datetime.datetime.fromisoformat(archived_at.strip())
            except ValueError:
                moment = None
            if moment is not None:
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=datetime.timezone.utc)
                age_hours = round((now - moment).total_seconds() / 3600, 2)
        if age_hours is None or age_hours > grace_hours:
            orphans.append({
                "batch_id": str(batch_id)[:64],
                "archived_at": archived_at if isinstance(archived_at, str) else None,
                "bytes": entry.get("bytes") if isinstance(entry.get("bytes"), int) else None,
                "age_hours": age_hours,
                "status": "pending_review",
            })
    return {"checked": checked, "orphans": orphans}


def _file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timed_pg_backup(db_path, attempt):
    """PG 引擎的定时备份：容器内 pg_dump + 侧车 manifest + 孤立批次核查。

    返回 (dest, removed)。PG 时代 console.db 文件可能不存在（引擎切换后保留
    旧文件作恢复取证），操作锁仍取真实数据目录，与 deploy/rollback 互斥不变。
    """
    engine = active_engine()
    commands = default_pg_commands()
    stamp = datetime.datetime.now().strftime("%Y%m%d")
    dest = BACKUP_ROOT / f"{PG_DUMP_PREFIX}.{stamp}"
    stage = "lock"
    locked = False
    try:
        with operation_lock(db_path):
            locked = True
            stage = "snapshot"
            BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
            # manifest 的 schema 身份/内容摘要由 write_pg_dump 从活库现查并
            # fail-closed；这里不再手工补参数（补出来的只是「看起来完整」）。
            write_pg_dump(dest, commands=commands)
            stage = "audit"
            report = _orphan_batch_report(db_path)
            if report is not None:
                _atomic_write_json(BACKUP_ROOT / "orphan-batches.json",
                                   {"attempt": attempt, **report})
            stage = "prune"
            removed = prune_old_backups(BACKUP_ROOT, KEEP_DAYS, trash_root=trash_dir(ROOT) / "timed")
            stage = "marker"
            write_status(
                BACKUP_ROOT,
                ok=True,
                attempt=attempt,
                stage=None,
                last_success=attempt,
                snapshot=dest.name,
                engine=engine,
            )
    except Exception as exc:
        if locked:
            _record_failure(BACKUP_ROOT, stage, attempt)
        if stage == "marker":
            print(f"ERROR: dump 已写入 {dest}，但成功标记发布失败：{exc}", file=sys.stderr)
        else:
            print(f"ERROR: PG 备份失败（阶段 {stage}），已保留旧 dump（若存在）：{exc}",
                  file=sys.stderr)
        return None, 0
    size = dest.stat().st_size
    print(f"backup ok: {dest} ({size} bytes)")
    if removed:
        print(f"archived {removed} old backup(s) (keep {KEEP_DAYS} days active)")
    return dest, removed


def _orphan_batch_report(db_path=None):
    """孤立批次核查（脱敏）；无法取得任一输入时返回 None 或 unknown，不伪造空结论。

    `db_path` 是本次操作的真实数据目录标识（与操作锁同一路径）；归档目录默认
    取它旁边的 `pay/`，保证与在线库同卷，而不是依赖进程 CWD。

    §11.5.3：psql 非零退出时**不能**拿空集合继续——那是「查询失败」，不是「库内无
    批次」。以前的写法会把已有归档全部标成待人工核验的孤儿（假告警），或者反过来
    在别的组合下给出「没有孤儿」的假清白。现在保留 unknown 语义并把原因写进报告。
    """
    commands = default_pg_commands()
    db_rows = None
    unavailable_reason = None
    try:
        result = subprocess.run(
            [*commands["psql"], "SELECT batch_id FROM ledger_file_batches"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            unavailable_reason = f"psql exit {result.returncode}"
            print(f"WARN: 批次记录查询失败（{unavailable_reason}），孤立核查保留 unknown 语义",
                  file=sys.stderr)
        else:
            db_rows = {line for line in (result.stdout or "").splitlines() if line.strip()}
    except (OSError, subprocess.TimeoutExpired) as exc:
        unavailable_reason = type(exc).__name__
        print(f"WARN: 批次记录查询不可用（{unavailable_reason}），孤立核查保留 unknown 语义",
              file=sys.stderr)
    manifest_path = os.environ.get("CONSOLE_BATCH_ARCHIVE_MANIFEST")
    if manifest_path:
        try:
            with open(manifest_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            entries = data.get("batches") if isinstance(data, dict) else data
            if isinstance(entries, list):
                archived = [e for e in entries if isinstance(e, dict)]
        except (OSError, ValueError) as exc:
            print(f"WARN: 归档清单不可读（{exc}），孤立核查只有 PG 侧输入", file=sys.stderr)
            archived = []
    else:
        archived = _archived_batches(db_path)
    if db_rows is None:
        # 查询失败 ≠ 库里没有批次：保留 unknown 语义，不产出「全是孤儿」或「没有孤儿」
        # 这类无法支撑的结论（§11.5.3）。
        # `checked` 保持 0 / `audited` 显式为 false：unknown 不能被消费方按「已核查
        # 且没有孤儿」读（复核 B：演练判据曾把 unknown 与 ok 当同形，误判 PASS）。
        return {"checked": 0, "orphans": [], "status": "unknown", "audited": False,
                "db_rows_available": False, "reason": unavailable_reason,
                "observed_archives": len(archived),
                "note": "批次记录查询失败：孤立判定未执行，需人工核验"}
    return {**audit_orphan_batches(db_rows=db_rows, archived=archived),
            "status": "ok", "audited": True, "db_rows_available": True}


def _archived_batches(db_path=None):
    """直接按 PG-01 的归档布局读批次清单（PG-05 集成收口）。

    布局：`<CONSOLE_PAY_DATA_DIR>/archive/<batch_id>/manifest.json`，回执字段见
    `ledger_files`。以前只认 `CONSOLE_BATCH_ARCHIVE_MANIFEST` 指向的外部 JSON，
    而没有任何组件生产它——核查因此长期是「无输入」的假空结论。这里改为默认
    直接读归档目录，环境变量仅作显式覆盖。

    归档目录不可读/缺失 → 返回 [] 并 WARN：**有 PG 侧输入的空结论**才是真的
    「没有归档」，读不到归档时必须说清楚，不能与前者混同。
    """
    base = Path(db_path) if db_path is not None else Path(ROOT) / DB_RELATIVE
    pay_data = os.environ.get("CONSOLE_PAY_DATA_DIR") or str(base.resolve().parent / "pay")
    archive = Path(pay_data) / "archive"
    if not archive.is_dir():
        print(f"WARN: 归档目录不可读（{archive}），孤立核查按无归档处理", file=sys.stderr)
        return []
    batches = []
    for entry in sorted(archive.iterdir()):
        if not entry.is_dir():
            continue
        manifest = entry / "manifest.json"
        try:
            with open(manifest, "r", encoding="utf-8") as handle:
                receipt = json.load(handle)
        except (OSError, ValueError):
            # 目录在但回执缺失/损坏：正是最该人工核验的一类，按缺 batch_id 上报。
            batches.append({"batch_id": None, "archived_at": None, "bytes": None})
            continue
        if not isinstance(receipt, dict):
            batches.append({"batch_id": None, "archived_at": None, "bytes": None})
            continue
        artifacts = receipt.get("artifacts")
        total = sum(
            item.get("bytes") for item in artifacts
            if isinstance(item, dict) and isinstance(item.get("bytes"), int)
        ) if isinstance(artifacts, list) else None
        batches.append({
            "batch_id": receipt.get("batch_id") or None,
            "archived_at": receipt.get("stored_at") or None,
            "bytes": total,
        })
    return batches


def main():
    if __package__:
        from .backup_runtime import deployment_environment
    else:
        from backup_runtime import deployment_environment
    try:
        os.environ.update(deployment_environment(ROOT))
    except (OSError, ValueError) as exc:
        print(f"ERROR: 备份引擎配置校验失败：{exc}", file=sys.stderr)
        return 1
    engine = active_engine()
    db_path = ROOT / DB_RELATIVE
    attempt = _now_iso()
    if engine == "postgres":
        dest, _removed = _timed_pg_backup(db_path, attempt)
        return 0 if dest is not None else 1
    if not db_path.exists():
        print(f"WARN: {db_path} 不存在，跳过（symlink 未就位或仓库路径变化）", file=sys.stderr)
        return 1
    stamp = datetime.datetime.now().strftime("%Y%m%d")
    dest = BACKUP_ROOT / f"console.db.{stamp}"
    stage = "lock"
    locked = False
    try:
        with operation_lock(db_path):
            locked = True
            stage = "snapshot"
            BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
            try:
                write_snapshot(dest, db_path)
            except BackupIntegrityError:
                stage = "quick_check"
                raise
            except FileNotFoundError:
                stage = "source"
                raise
            stage = "prune"
            removed = prune_old_backups(BACKUP_ROOT, KEEP_DAYS, trash_root=trash_dir(ROOT) / "timed")
            # 快照已原子发布，再原子发布成功标记：标记失败即本次不算成功。
            stage = "marker"
            write_status(
                BACKUP_ROOT,
                ok=True,
                attempt=attempt,
                stage=None,
                last_success=attempt,
                snapshot=dest.name,
                engine=engine,
            )
    except Exception as exc:
        # 未取得操作锁就失败时不留任何副作用（A 契约：被互斥挡住的定时备份不建目录、
        # 不写标记）；已持锁才写失败留痕，供运维卡区分「本次失败」与「上次成功」。
        if locked:
            _record_failure(BACKUP_ROOT, stage, attempt)
        if stage == "marker":
            print(
                f"ERROR: 快照已写入 {dest}，但成功标记发布失败：{exc}",
                file=sys.stderr,
            )
        else:
            print(
                f"ERROR: 备份失败，已保留旧的最终文件（若存在）并丢弃临时文件：{exc}",
                file=sys.stderr,
            )
        return 1
    size = dest.stat().st_size
    print(f"backup ok: {dest} ({size} bytes)")

    if removed:
        print(f"archived {removed} old backup(s) (keep {KEEP_DAYS} days active)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
