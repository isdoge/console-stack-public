#!/usr/bin/env python3
"""Shared deployment/restore file operations; manifests contain metadata, never YAML values."""
import argparse
import ast
import datetime
import errno
import fcntl
import hashlib
import inspect
import json
import math
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import nullcontext
from pathlib import Path

if __package__:
    from . import backup_db
    from .host_paths import backup_dir, backup_path, timed_backup_dir, trash_dir
else:
    import backup_db
    from host_paths import backup_dir, backup_path, timed_backup_dir, trash_dir

MEMBERS = ("console.db", "panels.yaml", "assets.yaml", "machines.yaml", "settings.yaml", "gitsha")
YAMLS = MEMBERS[1:5]
# PG-04：PG 时代 deploy 备份组的数据库成员是 console.dump.<tag>，retention 归档
# 必须覆盖（否则退役漏掉 dump）；其 manifest 侧车随成员一起归档（见 retire_group）。
RETIRE_MEMBERS = MEMBERS + ("console.dump",)
# PG-04：备份组按引擎区分数据库成员；YAML 与 gitsha 成员不变。
DB_MEMBER_BY_ENGINE = {"sqlite": "console.db", "postgres": backup_db.PG_DUMP_PREFIX}
CUTOVER_STATE_RELATIVE = "backup/cutover/state.json"
CUTOVER_WRITES_OPENED = "writes_opened"
RUNTIME_FORMAT = "console-backup-lock-v1"
# Resolve current once, then exec the immutable version: a refresh cannot mix modules.
RUNTIME_LAUNCHER = '''from pathlib import Path
import os
import sys
entry = (Path(__file__).resolve().parent / "current/controller/scripts/backup_runtime.py").resolve(strict=True)
os.execv(sys.executable, [sys.executable, str(entry), *sys.argv[1:]])
'''


def atomic_json(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=path.name + ".tmp-", delete=False) as handle:
        temp = Path(handle.name)
        try:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def file_digest(path):
    with Path(path).open("rb") as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normal_tag(tag):
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}", tag):
        return False
    try:
        datetime.datetime.strptime(tag, "%Y%m%d-%H%M%S")
    except ValueError:
        return False
    return True


def move_preserving(source, target, digest):
    """Publish without clobbering, including across volumes; then retire the source.

    An interruption between publication and unlink leaves two identical copies, which
    the next invocation can reconcile. Interrupted cross-volume copies have .tmp names.
    """
    source, target = Path(source), Path(target)
    if source.exists() and (not source.is_file() or source.is_symlink() or file_digest(source) != digest):
        raise RuntimeError(f"归档源已变化: {source.name}")
    if target.exists() or target.is_symlink():
        if not target.is_file() or target.is_symlink() or file_digest(target) != digest:
            raise FileExistsError(f"归档目标冲突: {target}")
    elif source.exists():
        try:
            os.link(source, target)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=target.name + ".tmp-", delete=False) as handle:
                temp = Path(handle.name)
            try:
                shutil.copy2(source, temp)
                if file_digest(temp) != digest:
                    raise RuntimeError(f"跨卷复制校验失败: {source.name}")
                with temp.open("rb") as handle:
                    os.fsync(handle.fileno())
                os.link(temp, target)
            finally:
                temp.unlink(missing_ok=True)
    else:
        raise FileNotFoundError(f"归档源和目标均缺失: {source.name}")
    # Only a verified recoverable copy permits removing the original directory entry.
    if source.exists():
        source.unlink()


def retire_group(backup_root, trash_root, tag):
    backup_root, trash_root = Path(backup_root).resolve(), Path(trash_root).resolve()
    paths = {name: backup_root / f"{name}.{tag}" for name in RETIRE_MEMBERS}
    paths.update({f"console.dump{suffix}": backup_root / f"console.dump.{tag}{suffix}"
                  for suffix in (backup_db.MANIFEST_SUFFIX, backup_db.backup_pay.SUFFIX)})
    target = trash_root / f"deploy-retention-{tag}"
    manifest_path = target / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("tag") != tag or manifest.get("source") != str(backup_root):
            raise FileExistsError(f"归档清单不匹配: {target}")
    else:
        target.mkdir(parents=True, exist_ok=True)
        if any(target.iterdir()):
            raise FileExistsError(f"归档目录已有文件且无清单: {target}")
        manifest = {"tag": tag, "source": str(backup_root), "target": str(target), "status": "moving", "members": {}}
        for name, source in paths.items():
            if source.is_symlink() or (source.exists() and not source.is_file()):
                raise ValueError(f"备份成员不是普通文件: {source.name}")
            manifest["members"][name] = {
                "source": str(source), "target": str(target / source.name),
                "status": "pending" if source.exists() else "missing",
                "sha256": file_digest(source) if source.exists() else None,
            }
        atomic_json(manifest_path, manifest)
    # Resume an older retention journal using the same independently hashed
    # companion entries. Never copy over an existing destination unverified.
    upgraded = False
    for name, source in paths.items():
        if name not in manifest["members"]:
            upgraded = True
            dest = target / source.name
            existing = source if source.exists() else dest
            if existing.is_symlink() or (existing.exists() and not existing.is_file()):
                raise ValueError(f"备份成员不是普通文件: {existing.name}")
            manifest["members"][name] = {
                "source": str(source), "target": str(dest),
                "status": "pending" if existing.exists() else "missing",
                "sha256": file_digest(existing) if existing.exists() else None,
            }
    if upgraded:
        atomic_json(manifest_path, manifest)
    try:
        for name, source in paths.items():
            member = manifest["members"][name]
            dest = target / source.name
            if member["status"] == "missing":
                if source.exists() or dest.exists():
                    raise RuntimeError(f"清单外出现备份成员: {source.name}")
                continue
            move_preserving(source, dest, member["sha256"])
            member["status"] = "moved"
            atomic_json(manifest_path, manifest)
        manifest["status"] = "complete"
        manifest.pop("error", None)
        atomic_json(manifest_path, manifest)
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = type(exc).__name__
        atomic_json(manifest_path, manifest)
        raise
    return manifest


def retire_groups(root, keep=10):
    root = Path(root).resolve()
    backup, trash = backup_dir(root), trash_dir(root)
    tags = set()
    for name in RETIRE_MEMBERS:
        for path in backup.glob(name + ".*"):
            tag = path.name[len(name) + 1:]
            if normal_tag(tag):
                tags.add(tag)
    selected = set(sorted(tags, reverse=True)[keep:])
    # A previous move may already have removed its gitsha or every source member.
    for path in trash.glob("deploy-retention-*/manifest.json"):
        manifest = json.loads(path.read_text())
        tag = manifest.get("tag", "")
        if normal_tag(tag) and manifest.get("status") != "complete":
            selected.add(tag)
    for tag in sorted(selected):
        retire_group(backup, trash, tag)
    return len(selected)


def run_command(root, *args, env=None, check=True, timeout=60, lock_fd=None):
    # Keep the same open-file lock in an in-flight child if its controller is killed.
    result = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True, timeout=timeout,
                            pass_fds=() if lock_fd is None else (lock_fd,))
    if check and result.returncode:
        # Git/build output may contain configuration values; record command/category only.
        raise RuntimeError(f"{args[0]} {args[1]} 失败 (exit {result.returncode})")
    return result


def members_for(engine):
    """某引擎的备份组成员清单：数据库成员按引擎替换，其余成员共享。"""
    db_member = DB_MEMBER_BY_ENGINE.get(engine)
    if db_member is None:
        raise ValueError(f"未知存储引擎: {engine}")
    return tuple(db_member if name == "console.db" else name for name in MEMBERS)


def group_engine(backup_dir, tag):
    """按「恰好存在一个数据库成员」判定备份组引擎；两个都没有/都有都拒绝。"""
    backup_dir = Path(backup_dir)
    present = [engine for engine, member in DB_MEMBER_BY_ENGINE.items()
               if (backup_dir / f"{member}.{tag}").is_file()]
    if len(present) != 1:
        raise ValueError(
            f"备份组 {tag} 的数据库成员不唯一（{', '.join(present) or '缺失'}），拒绝猜测引擎"
        )
    return present[0]


def cutover_writes_opened(root):
    """只读查询 cutover 状态机是否已到 writes_opened（§7.3 开放后边界）。

    状态文件缺失 → False（未进入 PG 时代，SQLite 退路保持）；状态记录损坏
    → 按提案「状态记录丢失不能推定还没写入」的保守规则，视为已开放。
    """
    path = backup_path(root, CUTOVER_STATE_RELATIVE)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    current = state.get("state")
    if current not in CUTOVER_STATES:
        return True
    if CUTOVER_STATES.index(current) >= CUTOVER_STATES.index(CUTOVER_WRITES_OPENED):
        return True
    # G04：open 动作没有回执时 `state` 还停在 verified_read_only，但状态已经
    # 落盘为「正在开放」——按 §7.3 保守规则视为已开放（不能推定还没写入）。
    pending = state.get("pending")
    return bool(isinstance(pending, dict)
                and pending.get("state") == CUTOVER_WRITES_OPENED)


def live_pg_schema():
    """读活 PG 库的 schema 版本（docker exec psql）；不可用返回 None，不猜。"""
    commands = backup_db.default_pg_commands()
    try:
        result = subprocess.run(
            [*commands["psql"], "SELECT max(version) FROM schema_migrations"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0 and (result.stdout or "").strip().isdigit():
        return int(result.stdout.strip())
    return None


def pg_schema_version_from_source(source, name="pg_migrations.py"):
    """静态提取目标 PG 最新迁移版本；SQL 只解析 AST，不执行目标代码。"""
    try:
        tree = ast.parse(source)
        node = next(stmt.value for stmt in tree.body if isinstance(stmt, ast.Assign)
                    and any(isinstance(n, ast.Name) and n.id == "PG_SCHEMA_VERSION"
                            for n in stmt.targets))
        value = ast.literal_eval(node)
        if type(value) is not int or value < 1:
            raise ValueError
        versions = []
        assigned = False
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "PG_MIGRATIONS"
                for target in stmt.targets
            ):
                if assigned:
                    raise ValueError
                assigned = True
                records = stmt.value
            elif (isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name)
                  and stmt.target.id == "PG_MIGRATIONS"):
                if not assigned or not isinstance(stmt.op, ast.Add):
                    raise ValueError
                records = stmt.value
            else:
                continue
            if not isinstance(records, (ast.Tuple, ast.List)):
                raise ValueError
            for record in records.elts:
                if not isinstance(record, (ast.Tuple, ast.List)) or len(record.elts) != 3:
                    raise ValueError
                version_node = record.elts[0]
                version = (value if isinstance(version_node, ast.Name)
                           and version_node.id == "PG_SCHEMA_VERSION"
                           else ast.literal_eval(version_node))
                if type(version) is not int:
                    raise ValueError
                versions.append(version)
        # 早期脚本夹具/基线源码仅声明版本常量；真实迁移表存在时必须完整、递增。
        if not assigned:
            return value
        if not versions or versions[0] != value or versions != sorted(set(versions)):
            raise ValueError
        return versions[-1]
    except (SyntaxError, ValueError, TypeError, StopIteration, IndexError):
        raise ValueError(f"无法静态确认代码 {name} 的 PG_SCHEMA_VERSION") from None



def migrations_source_text(root, target_sha=None):
    """Locate the file that literally assigns ``MIGRATIONS``.

    After the B7 P1 split the body lives in ``db_sqlite.py`` while ``db.py`` only
    re-exports. Older commits/checkouts still keep the assignment in ``db.py``.
    Prefer ``db_sqlite.py`` when present and parseable; otherwise fall back.
    """
    relatives = (
        "kernel/backend/db_sqlite.py",
        "console/backend/db_sqlite.py",
        "console/backend/db.py",
    )
    last_error = None
    for relative in relatives:
        try:
            if target_sha is None:
                path = Path(root) / relative
                if not path.is_file():
                    continue
                source = path.read_text()
            else:
                result = run_command(root, "git", "show", f"{target_sha}:{relative}", check=False)
                if result.returncode:
                    continue
                source = result.stdout
            # Skip relocating shims — they have no literal MIGRATIONS.
            if "Thin relocating shim" in source[:240]:
                continue
            code_schema(source)  # validate before accepting
            return source
        except ValueError as exc:
            last_error = exc
            continue
    raise ValueError("无法静态确认代码 MIGRATIONS schema") from last_error


def code_schema(source):
    """Read the literal migration contract without importing/executing target code."""
    try:
        tree = ast.parse(source)
        stores = [node for node in ast.walk(tree) if isinstance(node, ast.Name)
                  and node.id == "MIGRATIONS" and isinstance(node.ctx, ast.Store)]
        if len(stores) != 1:
            raise ValueError
        node = next(stmt.value for stmt in tree.body if isinstance(stmt, ast.Assign)
                    and any(isinstance(name, ast.Name) and name.id == "MIGRATIONS" for name in stmt.targets))
        migrations = ast.literal_eval(node)
        if not isinstance(migrations, (tuple, list)):
            raise ValueError
        versions = tuple(item[0] for item in migrations)
        if not versions or any(type(v) is not int or v < 1 for v in versions):
            raise ValueError
        if versions != tuple(sorted(set(versions))):
            raise ValueError
        return versions
    except (SyntaxError, ValueError, TypeError, StopIteration, IndexError):
        raise ValueError("无法静态确认代码 MIGRATIONS schema") from None


def database_schema(path, immutable=False):
    def _open(flag):
        uri = Path(path).resolve().as_uri() + "?mode=ro" + ("&immutable=1" if flag else "")
        return sqlite3.connect(uri, uri=True, timeout=5)

    try:
        conn = _open(immutable)
        try:
            if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("数据库 quick_check 未通过")
            versions = tuple(
                row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
            )
        finally:
            conn.close()
    except sqlite3.OperationalError:
        if immutable:
            raise ValueError("数据库完整性或 schema 校验失败") from None
        # 与 backup_db._quick_check 同一类问题：WAL 库在 checkpoint 后只剩主文件
        #（头部仍标记 WAL、无 -wal/-shm 伴生），新 SQLite（3.54+）拒绝纯 mode=ro
        # 打开（connect 惰性，错误在首条语句冒出）。无伴生文件时它不可能处于
        # 撕裂的 WAL 状态，immutable=1 是安全的；有伴生文件则按真实错误失败。
        if any(Path(str(path) + s).exists() for s in ("-wal", "-shm")):
            raise ValueError("数据库完整性或 schema 校验失败") from None
        return database_schema(path, immutable=True)
    if not versions or any(type(v) is not int for v in versions):
        raise ValueError("数据库 schema 未知")
    return versions


def validate_yaml(path, name):
    # This existing backend dependency is loaded during preflight, before checkout.
    import yaml

    try:
        value = yaml.safe_load(Path(path).read_text())
    except (yaml.YAMLError, UnicodeError):
        raise ValueError(f"YAML 无法解析: {name}") from None
    expected = dict if name == "settings.yaml" else list
    if value is not None and not isinstance(value, expected):
        raise ValueError(f"YAML 顶层类型不匹配: {name}")
    if isinstance(value, list) and any(not isinstance(item, dict) for item in value):
        raise ValueError(f"YAML 列表成员必须是 mapping: {name}")


def preflight(root, tag, data_only=False):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", tag) or ".." in tag:
        raise ValueError("备份标签格式不合法")
    root = Path(root).resolve()
    guard_backup_cron(root)
    guard_running_legacy_backup(root)
    engine = group_engine(backup_dir(root), tag)
    members = members_for(engine)
    db_member = DB_MEMBER_BY_ENGINE[engine]
    files = {}
    required = {db_member, "panels.yaml", "assets.yaml", "gitsha"}
    for name in members:
        path = backup_dir(root) / f"{name}.{tag}"
        if not path.is_file():
            if name in required:
                raise ValueError(f"缺少备份成员: {path.name}")
            if path.exists() or path.is_symlink():
                raise ValueError(f"备份成员不是普通文件: {path.name}")
            continue
        if path.is_symlink():
            raise ValueError(f"备份成员不能是 symlink: {path.name}")
        if name == "console.db" and any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm")):
            raise ValueError("备份数据库含 WAL/SHM 伴生文件，不能视为独立一致快照")
        if name in YAMLS:
            validate_yaml(path, name)
        files[name] = {"path": str(path.resolve()), "sha256": file_digest(path)}
    if engine == "postgres":
        manifest = backup_db.read_dump_manifest(files[db_member]["path"])
        if manifest is None or manifest.get("status") != "complete":
            raise ValueError("PG dump 侧车 manifest 缺失或未完成，拒绝恢复")
        if manifest["members"][0]["sha256"] != files[db_member]["sha256"]:
            raise ValueError("PG dump 内容与 manifest 摘要不一致")
        backup_db.backup_pay.verify_pay_snapshot(files[db_member]["path"], manifest)
    current_sha = run_command(root, "git", "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
    target_sha = current_sha
    sha = Path(files["gitsha"]["path"]).read_text().strip()
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
        raise ValueError("备份 gitsha 格式不合法")
    source_sha = run_command(root, "git", "rev-parse", "--verify", sha + "^{commit}").stdout.strip()
    if not data_only:
        target_sha = source_sha
    if data_only:
        source = migrations_source_text(root)
    else:
        source = migrations_source_text(root, target_sha)
        run_command(root, "git", "check-ref-format", "--branch", "rollback/" + tag)
    schema = code_schema(source)
    if engine == "sqlite":
        if database_schema(files[db_member]["path"], immutable=True) != schema:
            raise ValueError("目标数据库与代码 schema 不一致，拒绝恢复")
    else:
        # PG 组：目标代码必须携带 pg_migrations 且版本与 dump manifest 一致（§7.3 行3）。
        if data_only:
            pg_source = (root / "console/backend/pg_migrations.py").read_text()
        else:
            pg_source = run_command(root, "git", "show", target_sha + ":console/backend/pg_migrations.py").stdout
        code_pg_version = pg_schema_version_from_source(pg_source)
        recorded = backup_db.read_dump_manifest(files[db_member]["path"]).get("schema_version")
        if recorded is not None and recorded != code_pg_version:
            raise ValueError(
                f"目标代码 PG schema {code_pg_version} 与备份记录 {recorded} 不一致，拒绝恢复"
            )
    db_path = (root / backup_db.DB_RELATIVE).resolve()
    if engine == "sqlite":
        if not db_path.is_file():
            raise ValueError("当前数据库缺失，无法保留恢复前快照")
        if data_only and database_schema(db_path) != schema:
            raise ValueError("--data-only 要求目标 DB、当前 DB、当前代码 schema 一致")
    else:
        if not db_path.parent.is_dir():
            raise ValueError("数据目录缺失，无法执行恢复流程")
        if data_only:
            live_version = live_pg_schema()
            if live_version is None:
                raise ValueError("无法读取活 PG schema 版本，拒绝 --data-only 回滚")
            code_pg_version = pg_schema_version_from_source(
                (root / "console/backend/pg_migrations.py").read_text()
            )
            if live_version != code_pg_version:
                raise ValueError(
                    f"活库 PG schema {live_version} 与当前代码 {code_pg_version} 不一致，"
                    "拒绝 --data-only 回滚（先完成 schema 迁移或改用向前修复）"
                )
    return {
        "files": files, "current_commit": current_sha, "target_commit": target_sha, "source_commit": source_sha,
        "build_commit": run_command(root, "git", "rev-parse", "--short", target_sha).stdout.strip(),
        "schema": schema, "db_path": str(db_path), "engine": engine, "db_member": db_member,
    }


def copy_atomic(source, target):
    target = Path(target)
    target_mode = target.stat().st_mode & 0o777 if target.exists() else None
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=target.name + ".restore-tmp-", delete=False) as handle:
        temp = Path(handle.name)
    try:
        shutil.copy2(source, temp)
        if target_mode is not None:
            # Archives may be intentionally read-only; retain runtime write access.
            temp.chmod(target_mode)
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def _panels_file_is_empty(path):
    """True only for a panels.yaml whose whole body is blank or a bare ``[]``.

    刻意不解析 YAML（本模块零第三方依赖），也刻意不猜注释/缩进形态：判定越窄，
    误判成「空」而卡死部署的概率越低。仓库里的 `console/panels.yaml` 私有化后正是
    字面 `[]`，这一种形态就是需要拦的那一种。
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return False
    return text.strip() in ("", "[]")


def deployment_environment(root, environ=None):
    """Resolve host deployment settings without sourcing shell or silently choosing SQLite.

    .env is the Compose deployment configuration. Only the operational allowlist is
    read; passwords and unrelated application configuration stay in that file.
    """
    environ = dict(os.environ if environ is None else environ)
    allowed = {"CONSOLE_STORAGE_ENGINE", "CONSOLE_PG_DB", "CONSOLE_PG_BACKUP_CONTAINER",
               "CONSOLE_PG_BACKUP_USER", "CONSOLE_PG_BACKUP_DB", "CONSOLE_PG_ADMIN_PASSWORD_FILE",
               "CONSOLE_PAY_DATA_DIR", "CONSOLE_PANELS_HOST_PATH"}
    path = Path(root) / ".env"
    configured = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, raw = line.strip().removeprefix("export ").partition("=")
            key = key.strip()
            if not separator or key not in allowed:
                continue
            values = shlex.split(raw, comments=True)
            if len(values) != 1 or key in configured:
                raise ValueError(f"部署 .env 的 {key} 为空、重复或格式不受支持")
            configured[key] = values[0]
    for key, value in configured.items():
        if environ.get(key) and environ[key] != value:
            same_panels_file = (key == "CONSOLE_PANELS_HOST_PATH"
                                and Path(environ[key]).is_absolute() and Path(value).is_absolute()
                                and Path(environ[key]).resolve() == Path(value).resolve())
            if not same_panels_file:
                raise ValueError(f"宿主环境与部署 .env 的 {key} 冲突，拒绝继续")
    result = {**environ, **configured}
    if "CONSOLE_PANELS_HOST_PATH" in result:
        panels_path = Path(result["CONSOLE_PANELS_HOST_PATH"])
        if not panels_path.is_absolute() or not panels_path.is_file():
            raise ValueError("CONSOLE_PANELS_HOST_PATH 必须是存在的宿主绝对文件路径")
        result["CONSOLE_PANELS_HOST_PATH"] = str(panels_path.resolve())
    engine = result.get("CONSOLE_STORAGE_ENGINE")
    if engine not in DB_MEMBER_BY_ENGINE:
        raise ValueError("部署必须显式配置 CONSOLE_STORAGE_ENGINE=postgres 或 sqlite")
    if engine == "sqlite" and cutover_writes_opened(root):
        raise ValueError("PG 开放写入后拒绝以 SQLite 引擎部署")
    if engine == "postgres":
        database = result.get("CONSOLE_PG_DB", backup_db.DEFAULT_PG_DATABASE)
        if result.get("CONSOLE_PG_BACKUP_DB", database) != database:
            raise ValueError("PG 备份库与部署业务库不一致，拒绝继续")
        result["CONSOLE_PG_BACKUP_DB"] = database
    tracked_panels = Path(root) / "console" / "panels.yaml"
    if tracked_panels.is_file() and _panels_file_is_empty(tracked_panels):
        # 走到这里说明 Git 侧私有化已完成（tracked 刻意留空），容器只能从
        # CONSOLE_PANELS_HOST_PATH 取面板。此时 result 里的该值已被上面校验成
        # 「存在的宿主绝对路径」，可以安全读取内容。
        private_panels = result.get("CONSOLE_PANELS_HOST_PATH")
        if not private_panels:
            raise ValueError(
                "console/panels.yaml 是空列表，但部署 .env 未配置 CONSOLE_PANELS_HOST_PATH："
                "compose 会静默回退挂载这个空文件，线上面板将全部消失且各容器仍报 healthy。"
                "面板凭据私有化后必须把该变量指向宿主私有文件（0600）。")
        if _panels_file_is_empty(private_panels):
            raise ValueError(
                f"CONSOLE_PANELS_HOST_PATH 指向的文件是空列表: {private_panels}。"
                "tracked console/panels.yaml 已按私有化清空，容器只能从这个私有文件取面板，"
                "空文件等于线上面板全部消失；部署验收 [4] 同样要求 /api/panels 非空。")
    return result


def snapshot_group(root, tag, *, lock_fd=None, engine=None):
    """The gitsha is the last publication; an interrupted group has no completion marker.

    数据库成员按引擎分派：sqlite 走既有 write_snapshot；postgres 走容器内
    pg_dump + 侧车 manifest（backup_db.write_pg_dump）。
    """
    backup = backup_dir(root)
    # Explicit SQLite is used only for the frozen pre-cutover source. Default callers
    # (deploy/manual snapshots) must resolve the configured active engine.
    if engine == "sqlite":
        if cutover_writes_opened(root):
            raise ValueError("PG 开放写入后拒绝生成 SQLite 活库备份")
        configured = {}
    else:
        configured = deployment_environment(root)
        if engine is not None and engine != configured["CONSOLE_STORAGE_ENGINE"]:
            raise ValueError("快照引擎与部署配置不一致，拒绝备份")
        engine = configured["CONSOLE_STORAGE_ENGINE"]
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in members_for(engine):
        if os.path.lexists(backup / f"{name}.{tag}"):
            raise FileExistsError(f"恢复前标签已存在: {tag}")
    if engine == "sqlite":
        backup_db.write_snapshot(backup / f"console.db.{tag}", root / backup_db.DB_RELATIVE)
    else:
        for suffix in (backup_db.MANIFEST_SUFFIX, backup_db.backup_pay.SUFFIX):
            if os.path.lexists(backup / f"console.dump.{tag}{suffix}"):
                raise FileExistsError(f"恢复前标签已存在: {tag}")
        commands = backup_db.default_pg_commands(
            container=configured.get("CONSOLE_PG_BACKUP_CONTAINER"),
            user=configured.get("CONSOLE_PG_BACKUP_USER"),
            database=configured["CONSOLE_PG_BACKUP_DB"])
        backup_db.write_pg_dump(backup / f"console.dump.{tag}", commands=commands,
                               root=root, pay_root=configured.get("CONSOLE_PAY_DATA_DIR"))
    for name in YAMLS:
        source = (Path(configured["CONSOLE_PANELS_HOST_PATH"])
                  if name == "panels.yaml" and "CONSOLE_PANELS_HOST_PATH" in configured
                  else root / "console" / name)
        if source.is_file():
            copy_atomic(source, backup / f"{name}.{tag}")
        elif name in ("panels.yaml", "assets.yaml"):
            raise FileNotFoundError(f"当前 YAML 缺失: {name}")
    sha = run_command(root, "git", "rev-parse", "HEAD", lock_fd=lock_fd).stdout.strip()
    if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", sha):
        raise RuntimeError("无法取得有效的当前 Git commit，拒绝发布备份组")
    with tempfile.NamedTemporaryFile(mode="w", dir=backup, prefix="gitsha.tmp-", delete=False) as handle:
        temp = Path(handle.name)
        handle.write(sha + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temp, backup / f"gitsha.{tag}")
    finally:
        temp.unlink(missing_ok=True)



def _backend_implementation_path(backend_dir, name):
    """Prefer ``kernel/backend/<name>`` when console copy is only a relocating shim.

    ``backend_dir`` may already be ``kernel/backend`` after a relocating ``db``
    shim sets ``db.__file__`` to the kernel path; still look at sibling
    ``console/backend`` for modules that remain only there.
    """
    backend_dir = Path(backend_dir).resolve()
    if backend_dir.name == "backend" and backend_dir.parent.name in ("console", "kernel"):
        root = backend_dir.parent.parent
        console_path = root / "console" / "backend" / name
        kernel_path = root / "kernel" / "backend" / name
    else:
        console_path = backend_dir / name
        kernel_path = backend_dir.parent.parent / "kernel" / "backend" / name
    if console_path.is_file():
        head = console_path.read_text(encoding="utf-8", errors="ignore")[:240]
        if "Thin relocating shim" in head and kernel_path.is_file():
            return kernel_path
        if "Thin relocating shim" not in head:
            return console_path
        # Shim present but kernel missing — fall through.
    if kernel_path.is_file():
        return kernel_path
    return console_path


def controller_sources():
    # PG-04：PG 时代 runtime 捆绑的模块增加 PG 基础层（提案 §7.1：新实现使用
    # 更多模块时连依赖一起固化）。psycopg 驱动本身仍由解释器环境提供。
    backend_dir = Path(backup_db.dbmod.__file__).parent
    # 备份、组合身份及恢复分支的依赖一同固化；缺文件在发布控制器前拒绝，
    # 避免原仓可见性掩盖惰性 import 缺失。
    required = [
        "composition.py", "composition_identity.py", "plugin_imports.py",
        "backup_identity.py", "component_migrations.py", "kernel_baseline.py",
        "pg_backup.py", "pg_migrations.py", "pg_contracts.py", "backup_contracts.py", "ledger_batch_contracts.py", "pg_migration_contracts.py", "contract_errors.py",
        "db_sqlite.py", "pgdb.py", "pg_locks.py", "runtime_mode.py", "money.py", "sqlite_legacy.py",
    ]
    sources = {
        "scripts/backup_runtime.py": Path(__file__),
        "scripts/host_paths.py": Path(__file__).resolve().parent / "host_paths.py",
        "scripts/backup_db.py": Path(backup_db.__file__),
        "scripts/backup_pay.py": Path(__file__).resolve().parent / "backup_pay.py",
        "scripts/backup_attachments.py": Path(__file__).resolve().parent / "backup_attachments.py",
        # V4-02/V4-03：准备阶段与首备份恢复门禁也是控制器的一部分——恢复句柄必须
        # 带着它们，否则「用保存的旧控制器恢复」会在缺模块时炸掉。
        "scripts/cutover_prepare.py": Path(__file__).resolve().parent / "cutover_prepare.py",
        "scripts/cutover_backup_restore.py": Path(__file__).resolve().parent / "cutover_backup_restore.py",
        "scripts/cutover_writers.py": Path(__file__).resolve().parent / "cutover_writers.py",
        "scripts/pg_migration_lib.py": Path(__file__).resolve().parent / "pg_migration_lib.py",
        "console/backend/db.py": Path(backup_db.dbmod.__file__),
        "console/backend/paths.py": Path(backup_db.dbmod.paths.__file__),
    }
    for name in required:
        path = _backend_implementation_path(backend_dir, name)
        if not path.is_file():
            raise FileNotFoundError(f"控制器捆绑模块缺失: {backend_dir / name}")
        # 控制器仍用 console/backend/ 相对键，便于旧恢复句柄；内容取自 kernel 真身（若已搬迁）。
        sources[f"console/backend/{name}"] = path
    for path in sources.values():
        if not path.is_file():
            raise FileNotFoundError(f"控制器捆绑模块缺失: {path}")
    return sources


def preserve_controller(run_dir):
    """Keep an executable recovery handle even when the target predates these helpers."""
    controller = run_dir / "controller"
    for relative, source in controller_sources().items():
        target = controller / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if file_digest(target) != file_digest(source):
                raise FileExistsError(f"控制器目录冲突: {target}")
        else:
            copy_atomic(source, target)
    return str(controller / "scripts/backup_runtime.py")


def runtime_description(root):
    root = Path(root).resolve()
    files = {name: file_digest(path) for name, path in controller_sources().items()}
    version = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    directory = (backup_path(root, "backup/.runtime")).resolve()
    return {"format": RUNTIME_FORMAT, "version": version, "files": files,
            "root": str(root), "directory": str(directory),
            "db_path": str((root / backup_db.DB_RELATIVE).resolve()),
            "entry": str(directory / "entry.py")}


def publish_runtime(root, *, lock_fd=None):
    """发布完整定时控制器；部署中验证并沿用已有锁，不递归抢锁。"""
    if lock_fd is not None:
        check_deploy_lock(root, lock_fd)
    info = runtime_description(root)
    directory = Path(info["directory"])
    lock = (backup_db.operation_lock(info["db_path"])
            if lock_fd is None else nullcontext(lock_fd))
    with lock:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        version_dir = directory / info["version"]
        version_dir.mkdir(exist_ok=True, mode=0o700)
        preserve_controller(version_dir)
        atomic_json(version_dir / "manifest.json", info)
        entry = Path(info["entry"])
        if entry.exists() and entry.read_text() != RUNTIME_LAUNCHER:
            raise FileExistsError("稳定 runtime 入口内容冲突")
        if not entry.exists():
            with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as handle:
                temp = Path(handle.name)
                handle.write(RUNTIME_LAUNCHER)
            try:
                copy_atomic(temp, entry)
            finally:
                temp.unlink(missing_ok=True)
        current = directory / "current"
        if current.exists() and not current.is_symlink():
            raise FileExistsError("稳定 runtime current 必须是版本 symlink")
        link = directory / (".current-" + uuid.uuid4().hex)
        try:
            link.symlink_to(info["version"], target_is_directory=True)
            os.replace(link, current)
        finally:
            link.unlink(missing_ok=True)
    return info


def validate_runtime(root):
    root = Path(root).resolve()
    directory = (backup_path(root, "backup/.runtime")).resolve()
    if (directory / "entry.py").read_text() != RUNTIME_LAUNCHER:
        raise ValueError("稳定 runtime 入口校验失败")
    version_dir = (directory / "current").resolve(strict=True)
    if version_dir.parent != directory:
        raise ValueError("稳定 runtime current 不在版本目录内")
    manifest = json.loads((version_dir / "manifest.json").read_text())
    if (manifest.get("format") != RUNTIME_FORMAT or manifest.get("root") != str(root)
            or manifest.get("db_path") != str((root / backup_db.DB_RELATIVE).resolve())):
        raise ValueError("稳定 runtime 与当前数据锁不匹配")
    expected = set(controller_sources())
    if set(manifest.get("files", {})) != expected:
        raise ValueError("稳定 runtime 成员清单不完整")
    for name, digest in manifest["files"].items():
        if file_digest(version_dir / "controller" / name) != digest:
            raise ValueError("稳定 runtime 文件摘要校验失败")
    version = hashlib.sha256(json.dumps(manifest["files"], sort_keys=True).encode()).hexdigest()
    if manifest.get("version") != version or version_dir.name != version:
        raise ValueError("稳定 runtime 版本摘要不一致")
    return manifest


def read_crontab(root):
    result = run_command(root, "crontab", "-l", check=False)
    if result.returncode == 0:
        return result.stdout
    if result.returncode == 1 and not result.stdout.strip() and "no crontab" in result.stderr.lower():
        return ""
    raise RuntimeError("无法只读核验宿主 cron，拒绝开始回滚")


def backup_cron_jobs(text):
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or re.match(r"\s*[A-Za-z_]\w*\s*=", line):
            continue
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            raise ValueError("宿主 cron 行无法解析，拒绝猜测") from None
        if any("backup_db.py" in token or "backup_runtime.py" in token or "/.runtime/entry.py" in token for token in tokens):
            yield line, tokens


def guard_backup_cron(root):
    """A new process reading an old checkout must not bypass the held flock."""
    entry = (backup_path(root, "backup/.runtime/entry.py")).resolve()
    for _line, tokens in backup_cron_jobs(read_crontab(root)):
        # Recognize only the direct command emitted by cron-migration. Finding a
        # stable entry somewhere inside a shell fallback/chain is not sufficient.
        local_entry = any("backup_db.py" in token or "backup_runtime.py" in token for token in tokens)
        command = tokens[1 if tokens[0].startswith("@") else 5:]
        if (len(command) >= 2 and Path(command[0]).name == "env"
                and command[1] in ("CONSOLE_STORAGE_ENGINE=postgres", "CONSOLE_STORAGE_ENGINE=sqlite")):
            command = command[2:]
        safe = False
        if not local_entry and len(command) >= 4:
            tail = command[4:]
            safe = (re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(command[0]).name) is not None
                    and Path(command[1]).resolve() == entry and command[2] == "timed"
                    and Path(command[3]).resolve() == root
                    and (not tail or tail == [">>", str(backup_path(root, "backup/timed/backup.log")), "2>&1"]))
        if not safe:
            raise RuntimeError("宿主备份 cron 含旧入口或未识别的命令形式；先 publish-runtime 并审阅 cron-migration --dry-run，迁移后才能回滚")
        validate_runtime(root)


def guard_running_legacy_backup(root):
    # Migrating a schedule does not stop an older invocation already in flight.
    processes = run_command(root, "ps", "-axo", "pid=,args=").stdout
    for line in processes.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        try:
            argv = shlex.split(fields[1])
        except ValueError:
            continue
        if (argv and Path(argv[0]).name.startswith("python") and "-c" not in argv
                and any(Path(arg).name == "backup_db.py" for arg in argv[1:])):
            raise RuntimeError(f"旧 cron 备份入口仍有进程 PID {fields[0]}，等待退出后再回滚")


def cron_migration_plan(root):
    """Return only the affected schedules and proposed safe jobs, never unrelated cron values."""
    root = Path(root).resolve()
    info = runtime_description(root)
    command = [sys.executable, info["entry"], "timed", str(root)]
    replacements = []
    for line, _tokens in backup_cron_jobs(read_crontab(root)):
        fields = line.split(None, 1 if line.lstrip().startswith("@") else 5)
        if len(fields) != (2 if line.lstrip().startswith("@") else 6):
            raise ValueError("备份 cron 时间字段无法确认")
        schedule = " ".join(fields[:-1])
        log = backup_path(root, "backup/timed/backup.log")
        replacements.append(schedule + " " + shlex.join(command) + " >> " + shlex.quote(str(log))
                            + " 2>&1 # console-stack:timed-backup")
    return {"dry_run": True, "installs_cron": False, "runtime": info, "command": command,
            "affected_jobs": len(replacements), "proposed_jobs": replacements}


def console_ids(root, *, lock_fd=None):
    return run_command(root, "docker", "compose", "ps", "--all", "--quiet", "console", lock_fd=lock_fd).stdout.split()


def console_state(root, container, *, lock_fd=None):
    state = run_command(root, "docker", "inspect", "--format", "{{json .State}}", container, lock_fd=lock_fd).stdout
    return json.loads(state)


def stop_console(root, *, lock_fd=None):
    run_command(root, "docker", "compose", "stop", "--timeout", "30", "console", lock_fd=lock_fd)
    for container in console_ids(root, lock_fd=lock_fd):
        state = console_state(root, container, lock_fd=lock_fd)
        if state.get("Running") is not False or state.get("Restarting"):
            raise RuntimeError("console 未确认停止，拒绝操作数据库")


def wait_console(root, info, timeout, interval, *, lock_fd=None):
    deadline = time.monotonic() + timeout
    while True:
        ids = console_ids(root, lock_fd=lock_fd)
        healthy = bool(ids)
        for container in ids:
            state = console_state(root, container, lock_fd=lock_fd)
            healthy &= state.get("Running") is True and state.get("Health", {}).get("Status") == "healthy"
            if healthy:
                raw = run_command(root, "docker", "inspect", "--format", "{{json .Config.Env}}", container, lock_fd=lock_fd).stdout
                actual_env = json.loads(raw)
                if "GIT_COMMIT=" + info["build_commit"] not in actual_env:
                    raise RuntimeError("容器 GIT_COMMIT 与回滚目标不一致")
                if info.get("verify_engine") and "CONSOLE_STORAGE_ENGINE=" + info["engine"] not in actual_env:
                    raise RuntimeError("容器存储引擎与部署配置不一致")
        if healthy:
            if info.get("engine") == "postgres":
                identity = backup_db.pg_schema_identity(backup_db.default_pg_commands()["psql"])
                if identity and identity["matches_code"] and (identity["schema_version"],) == info["schema"]:
                    return
            elif database_schema(info["db_path"]) == info["schema"]:
                return
        if time.monotonic() >= deadline:
            raise RuntimeError("console 健康/迁移验收超时")
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


def deploy_schema(root, tag, *, lock_fd=None):
    """Use the just-built image's Python driver and the established owner migration path."""
    env = deployment_environment(root)
    if env["CONSOLE_STORAGE_ENGINE"] != "postgres":
        return None
    commands = backup_db.default_pg_commands()
    before = live_pg_schema()
    target = backup_db.pg_migrations.latest_schema_version()
    receipt_path = backup_dir(root) / f"schema-deploy-{tag}.json"
    receipt = {"engine": "postgres", "schema_before": before, "schema_target": target,
               "status": "running", "started_at": backup_db._now_iso()}
    atomic_json(receipt_path, receipt)
    # Default path is the secret source declared by docker-compose.yml; only the path is passed.
    secret = Path(env.get("CONSOLE_PG_ADMIN_PASSWORD_FILE",
                          "/vol1/console-stack-data/secrets/pg-admin.password"))
    try:
        if before is None or not secret.is_file():
            raise RuntimeError("PG schema 不可读或管理员 secret 文件缺失，拒绝迁移/启动")
        container = env.get("CONSOLE_PG_BACKUP_CONTAINER", backup_db.DEFAULT_PG_CONTAINER)
        user = env.get("CONSOLE_PG_BACKUP_USER", backup_db.DEFAULT_PG_USER)
        mount_target = "/run/secrets/console_pg_migration"
        result = run_command(
            root, "docker", "compose", "run", "--rm", "--no-deps", "-T",
            "--volume", f"{root}:/release:ro", "--volume", f"{secret.resolve()}:{mount_target}:ro",
            "--workdir", "/release", "--entrypoint", "python", "console",
            "/release/scripts/deploy_pg_schema.py", "--host", container,
            "--database", commands["database"], "--user", user,
            "--password-file", mount_target, env=env, timeout=600, lock_fd=lock_fd,
        )
        outcome = json.loads(result.stdout)
        if outcome.get("ok") is not True:
            raise RuntimeError("PG 迁移未返回有效成功回执")
        identity = backup_db.pg_schema_identity(commands["psql"])
        if not identity or not identity["matches_code"]:
            raise RuntimeError("PG 迁移后 schema/checksum 验收失败，拒绝启动")
        receipt.update(status="complete", schema_after=identity["schema_version"],
                       applied_versions=outcome.get("applied_versions", []),
                       created_as_owner=outcome.get("created_as_owner"))
    except BaseException as exc:
        receipt.update(status="failed", error=type(exc).__name__)
        raise
    finally:
        receipt["finished_at"] = backup_db._now_iso()
        atomic_json(receipt_path, receipt)
    return receipt


def accept_deploy(root, tag, *, lock_fd=None, timeout=120, interval=1):
    env = deployment_environment(root)
    engine = env["CONSOLE_STORAGE_ENGINE"]
    commit = run_command(root, "git", "rev-parse", "HEAD", lock_fd=lock_fd).stdout.strip()
    build_commit = run_command(root, "git", "rev-parse", "--short", "HEAD", lock_fd=lock_fd).stdout.strip()
    schema = ((backup_db.pg_migrations.latest_schema_version(),) if engine == "postgres"
              else code_schema(migrations_source_text(root)))
    wait_console(root, {"build_commit": build_commit, "engine": engine, "verify_engine": True,
                        "db_path": str(root / backup_db.DB_RELATIVE), "schema": schema},
                 timeout, interval, lock_fd=lock_fd)
    before = (backup_db.read_dump_manifest(backup_dir(root) / f"console.dump.{tag}") or {}).get("schema_version")
    if engine == "sqlite":
        before = max(database_schema(backup_dir(root) / f"console.db.{tag}"))
    result = {"commit": commit, "backup_tag": tag, "deployed_at": backup_db._now_iso(),
              "engine": engine, "migrations_applied": max(schema),
              "schema_before": before, "schema_target": max(schema)}
    atomic_json(backup_path(root, "backup/last_deploy.json"), result)
    return result


def rollback(root, tag, data_only=False, health_timeout=120, poll_interval=1):
    root = Path(root).resolve()
    # §7.3 开放后边界先于预检：PG 开放写入后，SQLite 组回滚等于丢弃 PG 增量，
    # 一律拒绝（快速失败，不先做无关的组校验）。
    if cutover_writes_opened(root):
        raise RuntimeError(
            "PG 开放写入后拒绝回退到 SQLite 备份组（§7.3：旧文件不含新写入）；"
            "请改用兼容当前 PG schema 的应用版本或向前修复"
        )
    # The first check is read-only. Repeat under the common lock before pausing writers.
    try:
        info = preflight(root, tag, data_only)
    except Exception:
        print("回滚预检失败（阶段 preflight）；当前数据未覆盖", file=sys.stderr)
        raise
    # PG 组非 --data-only（连带数据整体回滚）是未经验证的独立恢复工作，拒绝。
    if info["engine"] == "postgres" and not data_only:
        raise RuntimeError(
            "PG 备份组不支持连带数据的整体回滚（§7.3：PG 数据恢复是单独设计的"
            "未验证程序）；应用回退请使用 --data-only"
        )
    with backup_db.operation_lock(info["db_path"]) as lock_fd:
        if preflight(root, tag, data_only) != info:
            raise RuntimeError("预检输入已变化，请重新运行")
        run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        run_dir = (backup_dir(root) / ("rollback-" + run_id)).resolve()
        run_dir.mkdir(mode=0o700)
        recovery_tag = "preroll-" + run_id
        record = {
            "operation": "rollback", "tag": tag, "data_only": data_only,
            "status": "running", "phase": "prepare", "current_commit": info["current_commit"],
            "target_commit": info["target_commit"], "source_commit": info["source_commit"], "schema": max(info["schema"]),
            "started_at": datetime.datetime.now().astimezone().isoformat(),
            "manifest": str(run_dir / "manifest.json"), "recovery_tag": recovery_tag,
            "recovery_snapshot": "incomplete", "restored_members": [],
        }

        def phase(name):
            record["phase"] = name
            atomic_json(run_dir / "manifest.json", record)
            atomic_json(backup_path(root, "backup/last_rollback.json"), record)
            print(f"回滚阶段: {name}", flush=True)

        stopped = False
        rebuilding = False
        try:
            phase("prepare")
            record["controller"] = preserve_controller(run_dir)
            record["recovery_command"] = [sys.executable, record["controller"], "rollback", str(root), recovery_tag]
            inputs = run_dir / "target"
            inputs.mkdir(mode=0o700)
            for name, member in info["files"].items():
                shutil.copy2(member["path"], inputs / name)
                if file_digest(inputs / name) != member["sha256"]:
                    raise RuntimeError(f"备份成员在预检后变化: {name}")
            record["input_sha256"] = {name: member["sha256"] for name, member in info["files"].items()}
            phase("stop")
            stop_console(root, lock_fd=lock_fd)
            stopped = True
            phase("snapshot")
            snapshot_group(root, recovery_tag, lock_fd=lock_fd)
            record["recovery_snapshot"] = "complete"
            if data_only and info["engine"] == "sqlite" and database_schema(backup_dir(root) / f"console.db.{recovery_tag}", immutable=True) != info["schema"]:
                raise RuntimeError("停写后 schema 已变化，拒绝 --data-only 恢复")
            if not data_only:
                phase("stash")
                dirty = run_command(root, "git", "status", "--porcelain", lock_fd=lock_fd).stdout.strip()
                if dirty:
                    run_command(root, "git", "stash", "push", "-u", "-m", "rollback-auto-stash-" + run_id, lock_fd=lock_fd)
                    record["stash"] = run_command(root, "git", "rev-parse", "--verify", "refs/stash", lock_fd=lock_fd).stdout.strip()
                    if run_command(root, "git", "status", "--porcelain", lock_fd=lock_fd).stdout.strip():
                        raise RuntimeError("stash 后仍有未保护改动，拒绝 checkout")
                phase("checkout")
                branch = "rollback/" + tag
                exists = run_command(root, "git", "show-ref", "--verify", "--quiet", "refs/heads/" + branch, check=False, lock_fd=lock_fd)
                if exists.returncode == 0:
                    branch += "-" + run_id
                elif exists.returncode != 1:
                    raise RuntimeError("无法确认回滚分支状态")
                record["branch"] = branch
                run_command(root, "git", "checkout", "--quiet", "-b", branch, info["target_commit"], lock_fd=lock_fd)
                if run_command(root, "git", "rev-parse", "HEAD", lock_fd=lock_fd).stdout.strip() != info["target_commit"]:
                    raise RuntimeError("checkout 后 HEAD 与目标不一致")
            if (root / backup_db.DB_RELATIVE).resolve() != Path(info["db_path"]):
                raise RuntimeError("checkout 后数据真实路径变化，拒绝恢复")
            phase("sidecars")
            sidecars = run_dir / "sidecars"
            sidecars.mkdir(mode=0o700)
            for suffix in ("-wal", "-shm"):
                source = Path(info["db_path"] + suffix)
                if source.exists():
                    move_preserving(source, sidecars / source.name, file_digest(source))
            for name in (info["db_member"], *YAMLS):
                if name not in info["files"]:
                    continue  # Legacy machines/settings omission retains the checkout's version.
                phase("restore:" + name)
                if info["engine"] == "postgres" and name == info["db_member"]:
                    # PG --data-only 只回代码：dump 成员仅随组归档备查，不覆盖活库
                    # （活库数据按 §7.3 保持；数据回滚是单独设计的未验证程序）。
                    continue
                dest = Path(info["db_path"]) if name == "console.db" else root / "console" / name
                copy_atomic(inputs / name, dest)
                record["restored_members"].append(name)
            phase("build")
            rebuilding = True
            run_command(root, "docker", "compose", "up", "-d", "--no-deps", "--build", "--force-recreate", "console",
                        env={**os.environ, "GIT_COMMIT": info["build_commit"]}, timeout=900, lock_fd=lock_fd)
            phase("acceptance")
            wait_console(root, info, health_timeout, poll_interval, lock_fd=lock_fd)
            phase("record")
            atomic_json(backup_path(root, "backup/last_deploy.json"), {
                "commit": info["target_commit"], "backup_tag": tag,
                "deployed_at": datetime.datetime.now().astimezone().isoformat(),
                "migrations_applied": max(info["schema"]), "operation": "rollback",
            })
            record["status"] = "success"
            record["finished_at"] = datetime.datetime.now().astimezone().isoformat()
            phase("complete")
        except BaseException as exc:
            record["status"] = "failed"
            record["error"] = type(exc).__name__
            record["finished_at"] = datetime.datetime.now().astimezone().isoformat()
            if rebuilding:
                try:
                    stop_console(root, lock_fd=lock_fd)
                    record["failure_stop"] = "confirmed"
                except Exception:
                    record["failure_stop"] = "unconfirmed"
            elif stopped:
                record["failure_stop"] = "confirmed"
            try:
                atomic_json(run_dir / "manifest.json", record)
                atomic_json(backup_path(root, "backup/last_rollback.json"), record)
            except OSError:
                pass
            print(f"回滚失败，阶段 {record['phase']}；恢复前标签 {recovery_tag} ({record['recovery_snapshot']})；清单 {run_dir / 'manifest.json'}", file=sys.stderr)
            raise
        print(f"回滚完成；恢复前标签 {recovery_tag}；清单 {run_dir / 'manifest.json'}")
    return 0


# --- 首次割接控制器（PG-04；提案 §8.3/§9 C–D） ---------------------------------
# 外置控制器：不 import app、不用自身 release 目录当生产 root；状态机落盘，
# writes_opened 先持久化再恢复写入。真实迁移器/验证器由 PG-03 提供，通过
# --migrator/--verify-cmd 注入；缺配置时明确拒绝，不伪造阶段完成。

CUTOVER_STATES = ("prepared", "frozen", "migrated", "verified_read_only",
                  "backup_verified", "writes_opened", "completed")
# 格式 2（G04）：`state` = 最近**有完成回执**的状态，`pending` = 已开始未回执的动作。
# 格式 1（`console-cutover-state-1`）里 `state` 是「已开始」，重入会跳过动作，
# `read_cutover_state` 按保守规则就地升级（不写盘）。
# `backup_verified`（V4-03/§9 D6）：首份 PG 一致性备份 + 隔离恢复 + 逐值比较的回执，
# **在 writes_opened 之前**——缺报告/恢复失败/比较失败都不许调用 open。
CUTOVER_STATE_FORMAT = "console-cutover-state-2"
# 每个状态必需的注入命令；缺失时控制器拒绝推进（§8.3：不能靠默认值猜）。
CUTOVER_REQUIRED_ACTIONS = {
    "frozen": ("stop_cmd", "last SQLite 快照前停止写者的命令"),
    "migrated": ("migrator", "PG-03 迁移器 CLI"),
    "verified_read_only": ("verify_cmd", "只读验证命令"),
    "backup_verified": ("backup_restore_cmd",
                        "首份 PG 备份 + 隔离恢复 + 逐值/金额/原件映射比较（§9 D6）"),
    "writes_opened": ("open_cmd", "恢复写者的命令"),
}
# 准备阶段（§8.3）：从「旧 root + 未启动的新 PG 服务」出发完成 release 校验/YAML 收编/
# root 推进到最终 SHA/镜像预构建/PG 服务创建与就绪，再进入状态机。可注入以便演练，
# 缺省是仓库内 `cutover_prepare.py`。
CUTOVER_PREPARE_RECEIPT_FORMAT = "console-cutover-prepare-1"
# 首备份恢复回执（V4-03）：控制器在开放前校验它，重入不复用不相关旧报告。
CUTOVER_RESTORE_RECEIPT_FORMAT = "console-cutover-restore-1"
CUTOVER_RESTORE_REPORT_RELATIVE = "backup/cutover/restore-verify.json"
CUTOVER_PREPARE_REPORT_RELATIVE = "backup/cutover/prepare.json"
# PG-05 集成收口：迁移器/验证器不再是「必须外部注入」的空白。
# `migrated` 用 PG-03 的 `migrate_sqlite_to_postgres.py --mode apply`（§5.3），
# `verified_read_only` 用**独立**验证器 `verify_postgres_migration.py`（§8.2 要求
# 不复用搬运器结果缓存）。二者都是 argv 列表，走 `run_action` 子进程执行。
# 停写/恢复写者有仓库内缺省实现（`cutover_writers.py freeze|release`，G01/G03），
# 站点差异经 `CONSOLE_CUTOVER_*` 环境变量声明；发布 runtime 仍由 §9 E 按现场注入。
CUTOVER_SOURCE_RELATIVE = "backup/cutover/console.db"
MIGRATOR_MODES = ("preflight", "apply", "verify")

# G02：参与割接的代码就是这些磁盘字节——`--target-commit` 只是「想割到哪个提交」，
# 实际执行的是工作区当前内容。两者不一致时事后审计会把 v2 的执行记成 v1（首红判据 A/B）。
CUTOVER_EXECUTION_FILES = (
    "scripts/host_paths.py",
    "scripts/backup_runtime.py",
    "scripts/backup_db.py",
    "scripts/backup_pay.py",
    "scripts/cutover_writers.py",
    "scripts/cutover_prepare.py",
    "scripts/cutover_backup_restore.py",
    "scripts/migrate_sqlite_to_postgres.py",
    "scripts/verify_postgres_migration.py",
    "scripts/pg_migration_lib.py",
    "console/backend/pgdb.py",
    "console/backend/pg_migrations.py",
    "console/backend/pg_contracts.py",
    "console/backend/backup_contracts.py",
    "console/backend/ledger_batch_contracts.py",
    "console/backend/pg_migration_contracts.py",
    "console/backend/contract_errors.py",
    "console/backend/sqlite_legacy.py",
)
CONSOLE_PG_CONTAINER_ENV = "CONSOLE_PG_CONTAINER"


def cutover_execution_identity(root, target_commit):
    """记录本次割接**实际执行**的代码身份：HEAD、是否 dirty、每个脚本的 sha256。"""
    head = run_command(root, "git", "rev-parse", "HEAD").stdout.strip()
    status = [line for line in run_command(root, "git", "status", "--porcelain").stdout.splitlines()
              if line.strip()]
    files = {}
    for relative in CUTOVER_EXECUTION_FILES:
        path = Path(root) / relative
        files[relative] = ({"sha256": file_digest(path), "bytes": path.stat().st_size}
                           if path.is_file() else None)
    return {
        "target_commit": target_commit,
        "head": head,
        "head_matches_target": head == target_commit,
        "dirty": bool(status),
        "dirty_entries": status[:20],
        "files": files,
        "recorded_at": _utc_now(),
    }


def _execution_drift(root, state):
    """当前工作区代码 vs 首次记录的执行身份（G02）。返回 (final, 漂移文件列表)。

    「漂移」= CUTOVER_EXECUTION_FILES 里任一文件 sha256 变化，或 HEAD 不再等于
    `target_commit`。调用点：开放写入前与标记完成前——两处都会**拒绝**而不是告警。
    """
    final = cutover_execution_identity(root, state["target_commit"])
    initial = (state.get("evidence") or {}).get("execution", {}).get("files") or {}
    drift = sorted(name for name, entry in final["files"].items()
                   if initial and (initial.get(name) or {}).get("sha256")
                   != (entry or {}).get("sha256"))
    return final, drift


def cutover_target_image_identity():
    """PG 目标镜像身份（§9 D3）。容器运行时给 digest；外部 PG 明说不可得，不猜。"""
    name = (os.environ.get(CONSOLE_PG_CONTAINER_ENV) or "").strip()
    if not name:
        return {"available": False,
                "reason": f"未声明 {CONSOLE_PG_CONTAINER_ENV}（外部 PG 或非 docker 场景）"}
    try:
        proc = subprocess.run(["docker", "inspect", "--format", "{{.Image}} {{.Id}}", name],
                              capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "container": name,
                "reason": f"docker inspect 不可用（{type(exc).__name__}）"}
    if proc.returncode != 0:
        return {"available": False, "container": name,
                "reason": f"docker inspect 失败（exit {proc.returncode}）",
                "tail": (proc.stderr or "").strip()[-300:]}
    image, _, container_id = proc.stdout.strip().partition(" ")
    return {"available": True, "container": name, "image": image, "container_id": container_id}


def cutover_preflight_expectation(root, state):
    """重入时对目标 PG 的**期望**（§11.5 V4-01 / §9 D3）。

    空库限制只对「首次导入前」成立；一旦状态机进入 `migrated` 起（含崩溃未回执），
    目标非空就是**预期结果**，此时改用来源指纹 + 完成凭据 + 实际行数核对。
    返回给 `cutover_target_preflight(expectation=…)`。
    """
    point = _resume_point(state)
    if point is None:
        return {"mode": "completed"}
    index = CUTOVER_STATES.index(point)
    if index < CUTOVER_STATES.index("migrated"):
        return {"mode": "empty"}
    recorded = ((state.get("evidence") or {}).get("cutover_source") or {}).get("sha256")
    return {
        "mode": "migrated",
        "source_path": str(cutover_source_path(root)),
        "source_sha256": recorded,
        # 开放动作可能已经执行过（pending=writes_opened 的崩溃重入）：此时业务写
        # 可能已放行，行数只允许变多，不允许拿「必须相等」把恢复卡死。
        "allow_new_writes": index >= CUTOVER_STATES.index("writes_opened"),
    }


def _verify_migrated_reentry(conn, evidence, expectation, counts):
    """已进入迁移阶段的重入核对：来源指纹 + 完成凭据 + 实际副作用。

    只证明「这份目标里确实躺着本次割接迁入的数据」，不重复全量比对（那是
    `verified_read_only` 的独立验证器职责）。任何一项对不上都拒绝推进——
    目标身份不符 / 无凭据的非空目标属于**未知数据**，不能按「已迁入」放行。
    """
    from pg_migration_lib import TARGET_ONLY_TABLES, read_credential

    source = Path(str(expectation.get("source_path") or ""))
    recorded = expectation.get("source_sha256")
    if not recorded:
        raise RuntimeError(
            "重入核对失败：状态记录里没有首份快照指纹（不再推定「还没写入」，§8.3）")
    if not source.is_file():
        raise RuntimeError(f"重入核对失败：cutover 源快照不存在 {source}")
    actual = file_digest(source)
    if actual != recorded:
        raise RuntimeError(
            f"重入核对失败：源快照摘要 {actual[:12]}… 与首次记录 {recorded[:12]}… 不一致"
            "（不能证明迁入目标的就是这份源）")
    credential = read_credential(conn)
    if not credential:
        raise RuntimeError(
            "重入核对失败：目标业务表非空但**没有迁移完成凭据**——这是未知数据，"
            "拒绝按「已迁入」继续（§5.3.7）")
    if credential.get("source_sha256") != recorded:
        raise RuntimeError(
            f"重入核对失败：目标完成凭据的来源 {str(credential.get('source_sha256'))[:12]}… "
            f"与本状态机记录的源 {recorded[:12]}… 不一致")
    database = evidence["server"]["database"]
    credential_db = (credential.get("target_identity") or {}).get("database")
    if credential_db != database:
        raise RuntimeError(
            f"重入核对失败：完成凭据属于数据库 {credential_db!r}，当前连接是 {database!r}")
    allow_new = bool(expectation.get("allow_new_writes"))
    expected_counts = credential.get("counts") or {}
    checked = {}
    for table, want in sorted(expected_counts.items()):
        got = counts.get(table)
        if got is None:
            continue
        checked[table] = {"credential": want, "actual": got}
        ok = got >= want if allow_new else got == want
        if not ok:
            raise RuntimeError(
                f"重入核对失败：{table} 实际 {got} 行与凭据记录 {want} 行不符"
                f"（allow_new_writes={allow_new}）")
    # 目标独有表（`ledger_file_batches`）在首次割接期间必须为空：迁移凭据不覆盖它们，
    # 这里单独核对，避免「凭据对了但目标混进了别的批次记录」被漏掉。
    for table in TARGET_ONLY_TABLES:
        got = counts.get(table)
        if got:
            raise RuntimeError(
                f"重入核对失败：{table} 在首次割接期间必须为空，实际 {got} 行（未知数据）")
    return {"source_sha256": actual, "credential_completed_at": credential.get("completed_at"),
            "target_database": database, "allow_new_writes": allow_new,
            "row_counts": checked}


def cutover_target_preflight(*, prepare=True, empty_tables=None, expectation=None):
    """§9 D3 目标就绪检查（G02）：角色矩阵 / schema12 / 空库 / 镜像身份。

    目标可能是**未初始化**的空库（关闭标准要求「未初始化的隔离 PG 目标经真实外置
    入口跑通」）：`prepare=True` 就地 bootstrap 角色并跑 schema12；权限不足时明确
    拒绝并指向 §9 D3 准备步骤，不静默跳过。已初始化的目标一律核 schema 版本
    （必须等于 `PG_SCHEMA_VERSION`）与空库（业务表必须为空——首次割接不覆盖旧数据）。

    `expectation`（V4-01）区分「首次导入前」与「迁移开始后的重入」：前者保持空库
    限制，后者用 `_verify_migrated_reentry` 按来源指纹/凭据/实际行数核对，**不是**
    全局关掉预检。
    """
    import pg_migrations
    from pg_migration_lib import connect_target

    expectation = dict(expectation or {"mode": "empty"})
    tables = tuple(empty_tables or [name for name in pg_migrations.SCHEMA_TABLES
                                    if name != "schema_migrations"])
    evidence = {"checked_at": _utc_now(), "prepare": prepare, "expectation": expectation,
                "image": cutover_target_image_identity()}
    conn = connect_target(readonly=False, statement_timeout=300)
    try:
        evidence["server"] = dict(conn.execute(
            "SELECT current_database() AS database, current_user AS role,"
            " current_setting('server_version') AS server_version").fetchone())
        wanted = (pg_migrations.OWNER_ROLE, pg_migrations.ADMIN_ROLE, *pg_migrations.APP_ROLES)
        present = [row["rolname"] for row in conn.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
            (list(wanted),)).fetchall()]
        evidence["roles_present"] = present
        evidence["roles_missing"] = [role for role in wanted if role not in present]
        initialized = "schema_migrations" in pg_migrations.table_names(conn)
        evidence["initialized"] = initialized
        if not initialized:
            if expectation["mode"] == "migrated":
                # 迁移已开始却看不到 schema：不能按「还没写入」推定，也不能就地重建
                # （可能是连错了库）。fail closed。
                raise RuntimeError(
                    "重入核对失败：状态机已进入迁移阶段，但目标 PG 没有 schema_migrations"
                    "——目标身份不符或库被重建，拒绝继续（§8.3 不推定「还没写入」）")
            if not prepare:
                raise RuntimeError(
                    "目标 PG 未初始化（无 schema_migrations）：按 §9 D3 先启动 PG、"
                    "运行角色 bootstrap 与 schema12，再割接")
            try:
                evidence["bootstrap_roles"] = pg_migrations.bootstrap_roles(
                    conn, database=evidence["server"]["database"])
                evidence["schema_apply"] = pg_migrations.apply_as_owner(conn)
                evidence["bootstrapped"] = True
            except Exception as exc:  # noqa: BLE001 - 就地初始化失败必须给出人可读的下一步
                raise RuntimeError(
                    f"目标 PG 未初始化且就地 bootstrap/schema12 失败（{type(exc).__name__}）："
                    "按 §9 D3 用管理身份先跑 pg_schema.py bootstrap-roles + migrate"
                ) from exc
        evidence["schema_version"] = pg_migrations.current_version(conn)
        if evidence["schema_version"] != pg_migrations.latest_schema_version():
            raise RuntimeError(
                f"目标 PG schema 版本为 {evidence['schema_version']}，要求 "
                f"{pg_migrations.latest_schema_version()}（先跑目标代码迁移再割接）")
        counts = {table: conn.execute(
            f'SELECT count(*) AS c FROM public."{table}"').fetchone()["c"] for table in tables}
        evidence["row_counts"] = counts
        nonempty = {table: count for table, count in counts.items() if count}
        if nonempty:
            if expectation["mode"] == "migrated":
                evidence["reentry"] = _verify_migrated_reentry(conn, evidence, expectation, counts)
            else:
                raise RuntimeError(
                    f"目标 PG 业务表非空：{nonempty}（首次割接必须落在空库上，§9 D3）")
        return evidence
    finally:
        conn.close()


class MaintenanceLock:
    """控制器自己持有的 PG 独占维护锁（提案 §7.3）。

    PG-05 review B-2（blocker）：控制器原先只调外部 `stop_cmd`，**自己不冻结 PG 写入者**
    ——停没停干净、有没有自动重启重连，控制器无从自证。§7.3 要求「独占锁由维护控制器
    自己的**独立 PG 连接**持有，不能用随后要停止的 app 连接」。

    这里持有 `pg_locks.MAINTENANCE`（key=1，与业务写事务的共享锁同 key），从 `frozen`
    一直持到 `writes_opened` 状态落盘。**进程意外退出锁会随连接释放**——所以 §7.3 同时
    要求持久维护模式与受限账号兜底，单靠本锁不足以宣称冻结（见 `freeze-evidence`）。
    """

    def __init__(self, conn=None):
        self._conn = conn
        self._acquired = False

    def acquire(self, timeout_s=10):
        """取独占锁；失败即拒绝推进 cutover（宁可停在这里，不带锁迁移）。"""
        if self._conn is None:
            import pg_locks
            from pg_migration_lib import acquire_migration_lock, connect_target
            self._conn = connect_target(readonly=False, statement_timeout=300)
            self._pg_locks = pg_locks
        acquire_migration_lock(self._conn, timeout_s=timeout_s)
        self._acquired = True
        return self.held()

    def held(self):
        if not self._acquired or self._conn is None:
            return {"held": False}
        row = self._conn.execute(
            "SELECT objid, mode FROM pg_locks "
            "WHERE locktype = 'advisory' AND pid = pg_backend_pid() AND granted "
            "ORDER BY objid"
        ).fetchall()
        return {
            "held": True,
            "locks": [{"key": r["objid"], "mode": r["mode"]} for r in row],
        }

    def release(self):
        if self._acquired and self._conn is not None:
            try:
                from pg_migration_lib import release_migration_lock
                release_migration_lock(self._conn)
            except Exception:
                pass  # 会话级锁随连接关闭释放
            self._acquired = False

    def close(self):
        self.release()
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


def _migrator_argv(root, mode, report=None):
    """PG-03 迁移器 argv：源恒为 cutover 最后快照，目标走 `CONSOLE_PG_*` 环境。"""
    argv = [
        sys.executable, str(Path(root) / "scripts/migrate_sqlite_to_postgres.py"),
        "--mode", mode,
        "--source", str(cutover_source_path(root)),
        "--target-purpose", "production",
    ]
    if mode == "apply":
        argv += ["--source-sha256", file_digest(cutover_source_path(root))]
    if report is not None:
        argv += ["--report", str(report)]
    return argv


def _verifier_argv(root, report=None):
    """独立验证器 argv：只读比对，不读搬运器报告（§8.2 独立性）。"""
    argv = [
        sys.executable, str(Path(root) / "scripts/verify_postgres_migration.py"),
        "--source", str(cutover_source_path(root)),
        "--target", "env",
    ]
    if report is not None:
        argv += ["--report", str(report)]
    return argv


def cutover_source_path(root):
    """cutover 最后 SQLite 快照的固定位置（`frozen` 阶段写入，后续阶段读它）。

    固定位置而非「最近一个 precut-* 标签」：状态机重入与快照保留策略都不该让
    「迁移的是哪份源」靠目录扫描猜。缺失时 `_migrator_argv` 抛错——宁可失败，
    不迁移一份来历不明的库。
    """
    return backup_path(root, CUTOVER_SOURCE_RELATIVE)


def _writers_argv(root, verb):
    """停写/恢复动作的缺省 argv：仓库内 `cutover_writers.py`（G01）。

    以前 stop_cmd/open_cmd 只在文档里说「由现场注入」——那等于把停写步骤留到
    PG-07 临场拼 shell，没人能审。缺省指向已实现、有测试的写者冻结/恢复脚本；
    站点差异（要冻结哪些账号、停哪些写者）用 `CONSOLE_CUTOVER_*` 环境变量声明，
    仍然走同一条路，不需要手拼一条新的控制流。
    """
    script = Path(__file__).resolve().parent / "cutover_writers.py"
    return [sys.executable, str(script), verb, str(Path(root).resolve())]


# 动作级环境覆盖：CLI 参数 > 环境变量 > 缺省实现。
CUTOVER_ACTION_ENV = {
    "migrator": "CONSOLE_CUTOVER_MIGRATOR",
    "verify_cmd": "CONSOLE_CUTOVER_VERIFY_CMD",
    "stop_cmd": "CONSOLE_CUTOVER_STOP_CMD",
    "open_cmd": "CONSOLE_CUTOVER_OPEN_CMD",
    "publish_cmd": "CONSOLE_CUTOVER_PUBLISH_CMD",
    "backup_restore_cmd": "CONSOLE_CUTOVER_BACKUP_RESTORE_CMD",
}


def _action_env_argv(key):
    raw = (os.environ.get(CUTOVER_ACTION_ENV[key]) or "").strip()
    return shlex.split(raw) if raw else None


def cutover_default_actions(root, target_commit=None):
    """可缺省注入的动作：迁移器、验证器、写者冻结/恢复与首备份恢复门禁。

    返回**运行期求值**的可调用对象，而不是现成的 argv：`apply` 的
    `--source-sha256` 要等 `frozen` 写出快照后才能算，控制器构造时快照还不存在。
    报告落 `backup/cutover/`，便于 `evidence` 之外再留一份可复核的 manifest。

    `stop_cmd`/`open_cmd` 缺省是 `cutover_writers.py freeze|release`（G01/G03）：
    声明面（冻结哪些账号、停哪些写者、部署 env 文件）由环境变量给；什么都没声明时
    该脚本自己会拒绝执行（退出 2）——「没声明」不能当成「已停干净」。
    `backup_restore_cmd`（V4-03）缺省是 `cutover_backup_restore.py`：首份 PG 备份 +
    隔离恢复 + 逐值比较；它是 `writes_opened` 的前置状态，缺它控制器拒绝推进。
    """
    reports = backup_path(root, "backup/cutover")

    def migrator(current_root, env=None):
        return run_action(
            _action_env_argv("migrator")
            or _migrator_argv(current_root, "apply", reports / "migrate-apply.json"),
            current_root, env=env,
        )

    def verify(current_root, env=None):
        return run_action(
            _action_env_argv("verify_cmd")
            or _verifier_argv(current_root, reports / "verify-independent.json"),
            current_root, env=env,
        )

    def backup_restore(current_root, env=None):
        return run_action(
            _action_env_argv("backup_restore_cmd")
            or _backup_restore_argv(current_root, target_commit,
                                    reports / "restore-verify.json"),
            current_root, env=env,
        )

    return {
        "migrator": migrator,
        "verify_cmd": verify,
        "backup_restore_cmd": backup_restore,
        "stop_cmd": _action_env_argv("stop_cmd") or _writers_argv(root, "freeze"),
        "open_cmd": _action_env_argv("open_cmd") or _writers_argv(root, "release"),
    }


def _backup_restore_argv(root, target_commit, report=None):
    """V4-03 门禁动作的 argv：源恒为冻结快照，目标走 `CONSOLE_PG_*`/`CONSOLE_CUTOVER_RESTORE_*`。"""
    script = Path(__file__).resolve().parent / "cutover_backup_restore.py"
    argv = [sys.executable, str(script), "--root", str(Path(root).resolve()),
            "--source", str(cutover_source_path(root)),
            "--target-commit", str(target_commit or "")]
    if report is not None:
        argv += ["--report", str(report)]
    return argv


def _prepare_argv(root, target_commit, release_dir=None, env_file=None,
                  instance_role=None, report=None):
    """§8.3 准备阶段动作的 argv（V4-02）：候选控制器的准备实现 + 显式生产 root。"""
    script = Path(__file__).resolve().parent / "cutover_prepare.py"
    argv = [sys.executable, str(script), "--root", str(Path(root).resolve()),
            "--target-commit", str(target_commit)]
    if release_dir:
        argv += ["--release-dir", str(release_dir)]
    if env_file:
        argv += ["--env-file", str(env_file)]
    if instance_role:
        argv += ["--instance-role", str(instance_role)]
    argv += ["--report", str(report or (backup_path(root, CUTOVER_PREPARE_REPORT_RELATIVE)))]
    return argv


def execution_digest(identity):
    """候选执行身份的稳定摘要：门禁回执与控制器复核共用同一算法（V4-03）。"""
    payload = json.dumps((identity or {}).get("files") or {}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_restore_receipt(data, *, root, state):
    """开放前复核首备份恢复回执（V4-03）：缺项、来源/候选/目标对不上一律拒绝。

    重入时**不复用不相关旧报告**——回执必须绑定本次源指纹、本候选执行摘要与
    当前目标库，且 dump 文件仍在原位且摘要一致。
    """
    if not isinstance(data, dict) or data.get("format") != CUTOVER_RESTORE_RECEIPT_FORMAT:
        raise RuntimeError(
            f"首备份恢复回执缺失或格式不符（{CUTOVER_RESTORE_RECEIPT_FORMAT}）：拒绝开放写入")
    if data.get("status") != "ok":
        raise RuntimeError(f"首备份恢复回执状态为 {data.get('status')!r}：拒绝开放写入")
    recorded = ((state.get("evidence") or {}).get("cutover_source") or {}).get("sha256")
    actual = (data.get("source") or {}).get("sha256")
    if not recorded or actual != recorded:
        raise RuntimeError(
            f"首备份恢复回执的来源 {str(actual)[:12]}… 与本次割接源 {str(recorded)[:12]}… 不一致："
            "拒绝复用不相关旧报告")
    # 源快照本身必须还在且没被换过（回执不是「当时对过」的免检牌）
    source = cutover_source_path(root)
    if not source.is_file() or file_digest(source) != recorded:
        raise RuntimeError("首备份恢复回执对应的源快照已不存在或摘要变化：拒绝开放写入")
    current = execution_digest(cutover_execution_identity(root, state["target_commit"]))
    if (data.get("execution") or {}).get("digest") != current:
        raise RuntimeError("首备份恢复回执不是本候选执行身份产生的（代码已变）：拒绝开放写入")
    database = ((state.get("evidence") or {}).get("target_preflight") or {}).get(
        "server", {}).get("database")
    receipt_database = (data.get("target") or {}).get("database")
    if not receipt_database:
        raise RuntimeError("首备份恢复回执没有记录目标库身份：拒绝开放写入")
    if database and receipt_database != database:
        raise RuntimeError(
            f"首备份恢复回执的目标 {data.get('target', {}).get('database')!r} 与当前目标 "
            f"{database!r} 不一致：拒绝开放写入")
    dump = data.get("dump") or {}
    dump_path = Path(str(dump.get("path") or ""))
    if not dump_path.is_file() or file_digest(dump_path) != dump.get("sha256"):
        raise RuntimeError("首备份 dump 文件缺失或摘要变化：拒绝开放写入")
    # 门禁实现本身也要对得上：不接受「换个脚本产出一份长得像的回执」
    gate = data.get("gate") or {}
    bundled_gate = Path(__file__).resolve().parent / "cutover_backup_restore.py"
    if not gate.get("sha256") or gate.get("sha256") != file_digest(bundled_gate):
        raise RuntimeError(
            "首备份恢复回执不是仓库内已审查的门禁脚本产出的（gate.sha256 缺失或不符）："
            "拒绝开放写入；自定义门禁必须调用 scripts/cutover_backup_restore.py")
    # 逐值比较的证据必须**在位且成立**（复核 A：缺整段 comparison 曾被短路放行）
    comparison = data.get("comparison")
    if not isinstance(comparison, dict) or "mismatches" not in comparison:
        raise RuntimeError("首备份恢复回执缺少比较结果段：拒绝开放写入")
    if comparison.get("mismatches") != []:
        raise RuntimeError("首备份恢复副本与源快照的比较结果非空：拒绝开放写入")
    if not comparison.get("tables"):
        raise RuntimeError("首备份恢复回执没有记录比较过的表清单：拒绝开放写入")
    restored = data.get("restored") or {}
    if restored.get("restored") is not True or restored.get("row_digest_match") is not True:
        raise RuntimeError("首备份恢复回执缺少「恢复成功且逐表摘要一致」的证据：拒绝开放写入")
    if restored.get("schema_version") != _pg_schema_version():
        raise RuntimeError(
            f"首备份恢复回执的恢复库 schema 版本 {restored.get('schema_version')!r} "
            f"不是 {_pg_schema_version()}：拒绝开放写入")
    batch_rows = ((data.get("file_mapping") or {}).get("ledger_file_batches") or {}).get(
        "restored")
    if batch_rows != 0:
        raise RuntimeError(
            f"首备份恢复回执的原件映射核对未通过（ledger_file_batches.restored="
            f"{batch_rows!r}）：拒绝开放写入")
    pay = (data.get("file_mapping") or {}).get("pay")
    if (not isinstance(pay, dict) or pay.get("verified") is not True
            or pay.get("batches") != batch_rows or not pay.get("path")
            or restored.get("pay") != pay):
        raise RuntimeError("首备份恢复回执缺少 Pay 文件独立恢复核验：拒绝开放写入")
    manifest = backup_db.read_dump_manifest(dump_path)
    if manifest is None:
        raise RuntimeError("首备份 manifest 缺失：拒绝开放写入")
    actual_pay = backup_db.backup_pay.verify_restored_pay(dump_path, manifest, dest=pay["path"])
    if actual_pay != {key: value for key, value in pay.items() if key != "path"}:
        raise RuntimeError("首备份 Pay 恢复回执与文件不符：拒绝开放写入")
    cleanup = data.get("cleanup") or {}
    if cleanup.get("dropped") is not True:
        raise RuntimeError("首备份恢复的一次性恢复库未被清理（cleanup.dropped != true）：拒绝开放写入")
    return {"dump": dump.get("name"), "restored": restored,
            "comparison_tables": comparison.get("tables"),
            "file_mapping": data.get("file_mapping"), "cleanup": cleanup}


def _pg_schema_version():
    import pg_migrations
    return pg_migrations.latest_schema_version()


def cutover_state_path(root, state_dir=None):
    base = Path(state_dir) if state_dir else backup_path(root, "backup/cutover")
    return base / "state.json"


def read_cutover_state(root, state_dir=None):
    """读状态机文件；缺失返回 None，损坏按保守规则视为 writes_opened 之外还须人工。"""
    path = cutover_state_path(root, state_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"cutover 状态记录损坏（{type(exc).__name__}）：按提案 §8.3 不能推定"
            f"「还没写入」，须人工核验后处理；文件 {path}"
        ) from None
    if not isinstance(state, dict) or state.get("state") not in CUTOVER_STATES:
        raise RuntimeError(f"cutover 状态记录内容不合法: {path}")
    return _normalize_cutover_state(state)


def _persist_cutover_state(path, state):
    """fsync + 原子替换；writes_opened 的持久化必须先于任何开放副作用。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=str(path.parent), prefix=path.name + ".tmp-", delete=False,
    ) as handle:
        temp = Path(handle.name)
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(str(temp), str(path))
    finally:
        temp.unlink(missing_ok=True)


def run_cutover(root, *, target_commit, actions, dry_run=False, state_dir=None,
                maintenance_lock=True, target_preflight=True, prepare_receipt=None):
    """推进 cutover 状态机；重入先核对实际状态与记录一致（§8.3）。

    actions: {"stop_cmd", "migrator", "verify_cmd", "open_cmd", "publish_cmd"} 的
    命令（callable 或 argv 列表）。每个状态完成后原子落盘；writes_opened 的状态
    持久化先于 open_cmd 执行。失败保留已完成状态与错误记录，重入续推。

    五个动作缺省时都取 `cutover_default_actions()`：迁移器/验证器是 PG-03 真实 CLI，
    `stop_cmd`/`open_cmd` 是仓库内 `cutover_writers.py freeze|release`（G01：两条入口
    共用同一份已审查的停写/恢复配置）。站点差异由 `CONSOLE_CUTOVER_*` 环境变量声明；
    什么都不声明时冻结脚本自己拒绝执行，控制器随之拒绝推进——不会静默放行。

    `maintenance_lock=True`（默认，生产割接必须）：控制器用自己的独立 PG 连接取
    `pg_locks.MAINTENANCE` 独占锁并在 `frozen`→`writes_opened` 全程持有（§7.3）。
    `False` 只给未接 PG 的单元测试用——它意味着迁移期间**不冻结**业务写事务。
    """
    root = Path(root).resolve()
    actions = {**cutover_default_actions(root, target_commit), **(actions or {})}
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", target_commit or ""):
        raise ValueError("--target-commit 必须是完整 SHA")
    try:
        resolved = run_command(
            root, "git", "rev-parse", "--verify", target_commit + "^{commit}").stdout.strip()
    except Exception as exc:  # 未知目标：git 报 exit 128，不能只把原始 stderr 抛出去
        raise ValueError(
            f"--target-commit 不是仓库内完整提交: {target_commit}"
            f"（git rev-parse 失败：{type(exc).__name__}）") from exc
    if resolved != target_commit:
        raise ValueError(f"--target-commit 不是仓库内完整提交: {target_commit}")
    identity = cutover_execution_identity(root, target_commit)
    if not identity["head_matches_target"]:
        # G02 判据 A/B：记着 v1、执行的却是工作区 v2 的字节。拒绝而不是告警——
        # 割接是一次性、不可静默重放的迁移，事后审计必须能由记录复现执行。
        # §8.3 的正当做法是让**外置入口的准备阶段**把 root 推进到最终 SHA（V4-02），
        # 不是让操作者手工 checkout 出这个条件。
        raise RuntimeError(
            f"工作区 HEAD 与 --target-commit 不一致：HEAD={identity['head']}，"
            f"target={target_commit}（§8.3/G02：不允许「记录一个版本、执行另一个版本」；"
            "首次割接请走外置入口的准备阶段，不要手工改 root）"
        )
    path = cutover_state_path(root, state_dir)
    state = read_cutover_state(root, state_dir)
    if state is None:
        state = {
            "format": CUTOVER_STATE_FORMAT,
            "state": "prepared",
            "pending": None,
            "target_commit": target_commit,
            "root": str(root),
            "history": [],
        }
        _begin_state(state, "prepared")
        _finish_state(state, "prepared", {
            "running_commit": identity["head"],
            "controller": preserve_controller(backup_path(root, "backup/cutover")),
        })
        # 身份进 evidence（不是只进 history effects）：completed 时限据它做漂移核验。
        state.setdefault("evidence", {})["execution"] = identity
        _persist_cutover_state(path, state)
    elif target_commit not in {state["target_commit"],
                               (state.get("evidence") or {}).get("requested_commit")}:
        raise RuntimeError(
            f"cutover 目标变更被拒绝：状态机记录 {state['target_commit']}，"
            f"本次请求 {target_commit}（§8.3：中途不换目标）"
        )
    else:
        # 重入：工作区字节可能与首次记录不同（改了脚本再续跑），变化时留痕。
        evidence = state.setdefault("evidence", {})
        if evidence.get("execution") and evidence["execution"].get("files") != identity["files"]:
            evidence["execution_reentry"] = identity
    if prepare_receipt is not None:
        evidence = state.setdefault("evidence", {})
        evidence["prepare"] = prepare_receipt
        # 首次割接请求的是候选 SHA、实际执行的是收编/rebase 之后的最终 SHA（§8.3）：
        # 两个都留在状态里，重入时既认最终 SHA，也认本次请求。
        evidence["requested_commit"] = prepare_receipt.get("requested_commit")
        evidence["final_commit"] = prepare_receipt.get("final_commit")
        _persist_cutover_state(path, state)
    if dry_run:
        return {"state": state["state"], "remaining": _remaining_states(state),
                "pending": (_pending_of(state) or {}).get("state"), "dry_run": True}

    # §7.3：控制器自己的独立 PG 连接持独占维护锁，跨 frozen→writes_opened 存活。
    # `maintenance_lock=False` 供未接 PG 的单元测试与 dry-run 使用；生产割接必须为真。
    lock = MaintenanceLock() if maintenance_lock else None
    if lock is not None and not dry_run and _resume_point(state) is not None:
        try:
            evidence = lock.acquire()
            state.setdefault("evidence", {})["maintenance_lock"] = evidence
        except Exception as exc:
            if lock is not None:
                lock.close()
            raise RuntimeError(
                f"未能取得 PG 独占维护锁，拒绝推进 cutover：{type(exc).__name__}"
            ) from exc

    if target_preflight and _resume_point(state) is not None:
        # §9 D3：先取锁再动目标——preflight 可能在**未初始化**空库上建角色/建表，
        # 那一步必须已经受维护锁保护，避免与其它控制器并发 bootstrap。
        # V4-01：期望按状态机推导——「首次导入前」才要求空库，迁移开始后的重入用
        # 来源指纹/完成凭据/实际行数核对（不是全局关掉预检）。
        state.setdefault("evidence", {})["target_preflight"] = cutover_target_preflight(
            expectation=cutover_preflight_expectation(root, state))
        _persist_cutover_state(path, state)

    try:
        return _advance_cutover(root, state, actions, path, lock)
    finally:
        if lock is not None:
            lock.close()


def _maintenance_env(lock):
    """控制器持锁时给子进程的环境声明；没持锁就不声明（子进程照常自己取锁）。"""
    if lock is None or not lock.held().get("held"):
        return None
    return {"CONSOLE_CUTOVER_MAINTENANCE_HELD": "1"}


def _advance_cutover(root, state, actions, path, lock):
    # 起点按回执判定：有 pending 就重跑该状态，否则从下一个未完成状态继续（G04）。
    for next_state in _remaining_states(state):
        _require_action(state, next_state, actions)
        _begin_state(state, next_state)
        # 「尝试」也要落盘：进程在动作中途被 kill 时，磁盘必须能看出这一步已开始
        # （否则重入只能靠猜）。动作本身重入时重跑——stop/迁移/验证/开放都幂等。
        _persist_cutover_state(path, state)
        try:
            if next_state == "frozen":
                # 停写动作的回执必须落进 evidence：G01/G04 的验收要看的是
                # 「标准入口真的调用了哪条停写命令、退出码多少」，不是状态字符串。
                state.setdefault("evidence", {})["stop_report"] = run_action(
                    actions["stop_cmd"], root)
                if lock is not None:
                    # 停写者之后、取快照之前复核锁仍在：外部动作可能悄悄切断连接。
                    held = lock.held()
                    if not held.get("held"):
                        raise RuntimeError(
                            "维护锁在冻结阶段丢失，拒绝继续（§7.3：不能靠事后补锁）"
                        )
                    state["evidence"]["maintenance_lock_after_stop"] = held
                tag = "precut-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
                snapshot_group(root, tag, engine="sqlite")
                # 迁移器读的是固定位置（`cutover_source_path`），与备份组并存：
                # 组是给人回滚的完整证据，固定文件是给 CLI 的确定输入。
                source = cutover_source_path(root)
                source.parent.mkdir(parents=True, exist_ok=True)
                backup_db.write_snapshot(source, root / backup_db.DB_RELATIVE)
                state.setdefault("evidence", {})["last_sqlite_snapshot"] = tag
                state["evidence"]["cutover_source"] = {
                    "path": str(source),
                    "sha256": file_digest(source),
                    "bytes": source.stat().st_size,
                }
            elif next_state == "migrated":
                state.setdefault("evidence", {})["migrator_report"] = run_action(
                    actions["migrator"], root, env=_maintenance_env(lock))
            elif next_state == "verified_read_only":
                state.setdefault("evidence", {})["verify_report"] = run_action(
                    actions["verify_cmd"], root, env=_maintenance_env(lock))
            elif next_state == "backup_verified":
                # V4-03/§9 D6：首份一致性 PG 备份 → 隔离 restore → 逐值/金额/原件映射
                # 比较。**只有在回执通过复核之后**才可能走到 writes_opened——动作退出码
                # 为 0 不算数，缺报告/来源不符/候选不符/恢复失败都拒绝开放。
                state.setdefault("evidence", {})["backup_restore_report"] = run_action(
                    actions["backup_restore_cmd"], root, env=_maintenance_env(lock))
                report_path = backup_path(root, CUTOVER_RESTORE_REPORT_RELATIVE)
                try:
                    receipt = json.loads(report_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise RuntimeError(
                        f"首备份恢复回执不可读（{type(exc).__name__}）：动作退出码为 0 也不能"
                        "证明恢复验证通过，拒绝开放写入（§9 D6）") from None
                state["evidence"]["backup_verified"] = validate_restore_receipt(
                    receipt, root=root, state=state)
            elif next_state == "writes_opened":
                # G02：开放写入是最不可逆的一步——先复核代码身份，再放行业务写。
                # 割接途中脚本被替换（换版本、人工改）时不开放写入，也不到 completed。
                final, drift = _execution_drift(root, state)
                state.setdefault("evidence", {})["execution_before_open"] = final
                if drift or not final["head_matches_target"]:
                    raise RuntimeError(
                        f"执行身份漂移，拒绝开放写入：文件 {drift or '无'} / "
                        f"HEAD={final['head']}（§8.3/G02）")
                # 顺序硬约束：状态先落盘，再执行开放动作（§8.3：恢复时按
                # 「可能已有 PG 新写入」保守处理）。
                state.setdefault("evidence", {})["writes_opened_at"] = _utc_now()
            elif next_state == "completed":
                # 第二道闸：开放写入之后到标记完成之间仍不得换代码；「实际执行的代码」
                # 必须与最初记录一致，否则不得声称完成（G02 判据 A/C）。
                final, drift = _execution_drift(root, state)
                state.setdefault("evidence", {})["execution_final"] = final
                if drift or not final["head_matches_target"]:
                    raise RuntimeError(
                        f"执行身份漂移，拒绝标记完成：文件 {drift or '无'} / "
                        f"HEAD={final['head']}（§8.3/G02）")
                if actions.get("publish_cmd"):
                    state.setdefault("evidence", {})["publish_report"] = run_action(actions["publish_cmd"], root)
                state.setdefault("evidence", {})["completed_note"] = (
                    "开放动作已执行；runtime 发布与 cron 恢复由 PG-07 按 §9 E 核对"
                )
            # writes_opened 的回执要等开放副作用真的执行完才给（见下），
            # 其余状态在这里收尾（G04：回执先于 `state` 推进）。
            if next_state != "writes_opened":
                _finish_state(state, next_state, None)
        except BaseException as exc:
            state["last_error"] = {"state": next_state, "error": type(exc).__name__}
            _persist_cutover_state(path, state)
            raise
        # writes_opened 特殊：`pending=writes_opened` 先落盘，再执行开放动作
        # （§8.3 顺序硬约束：崩溃后按「可能已有 PG 新写入」保守处理）。
        _persist_cutover_state(path, state)
        if next_state == "writes_opened":
            # 锁必须**在状态落盘之后**才释放：中途崩溃时重入者据 writes_opened
            # 判断写入已开放，而此时锁已不在——这正是 §7.3 要求「持久维护模式
            # 与受限账号兜底，不能仅靠锁存活」的原因。
            if lock is not None:
                lock.release()
                state["evidence"]["maintenance_lock_released_at"] = _utc_now()
                _persist_cutover_state(path, state)
            # 开放动作留回执（成功与失败都记）：失败时 `state` 停在
            # verified_read_only、`pending=writes_opened`，重入会**重跑 open**
            # 而不是跳过它直接 completed（G04 缺陷的原形）。
            try:
                report = run_action(actions["open_cmd"], root)
            except BaseException as exc:
                state.setdefault("evidence", {})["open_report"] = {
                    "executed": True, "error": type(exc).__name__}
                state["last_error"] = {"state": "writes_opened", "error": type(exc).__name__}
                _persist_cutover_state(path, state)
                raise
            open_report = report if report is not None else {"executed": True}
            state.setdefault("evidence", {})["open_report"] = open_report
            _finish_state(state, "writes_opened", {"open_report": open_report})
            _persist_cutover_state(path, state)
    return state


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _pending_of(state):
    """当前「已开始但没有完成回执」的状态；没有则 None（G04）。"""
    pending = state.get("pending")
    if isinstance(pending, dict) and pending.get("state") in CUTOVER_STATES:
        return pending
    return None


def _resume_point(state):
    """重入起点：有 pending 就**重跑该状态**，否则接着下一个未完成状态。

    G04 缺陷的原形：`_begin_state` 在动作前就把 `state` 推进到下一态并落盘，
    重入从 `index(state)+1` 开始 → 失败/崩溃后重入**跳过该动作**（open_cmd 失败
    后重入不重跑 open 却标记 completed）。现在 `state` 只表示**有完成回执**的状态，
    未回执的动作归 `pending`，重入按 pending 续推。
    """
    pending = _pending_of(state)
    if pending:
        return pending["state"]
    index = CUTOVER_STATES.index(state["state"])
    return CUTOVER_STATES[index + 1] if index + 1 < len(CUTOVER_STATES) else None


def _remaining_states(state):
    point = _resume_point(state)
    return list(CUTOVER_STATES[CUTOVER_STATES.index(point):]) if point else []


def _normalize_cutover_state(state):
    """把 format-1 记录升成 format-2 语义（`state` + `pending`）。

    format-1 里 `state` 是「已开始」而不是「已完成」，靠 history 末条的 status
    区分：running → 该状态没有回执，回退 `state` 到上一个 complete 的状态。
    `writes_opened` 另有一条保守规则：旧记录不存 open 回执，无法证明开放动作
    执行过 → 一律视为 pending 重跑（release 幂等，重跑无害；漏跑则永远不开放）。
    """
    if state.get("format") == CUTOVER_STATE_FORMAT:
        return state
    history = state.get("history") or []
    last = history[-1] if history else None
    if last and last.get("status") != "complete":
        state["state"] = history[-2]["state"] if len(history) > 1 else "prepared"
        state["pending"] = {"state": last.get("state"), "started_at": last.get("started_at"),
                            "attempts": last.get("attempts") or 1}
    else:
        state["pending"] = None
    if state["state"] == "writes_opened" and not (state.get("evidence") or {}).get("open_report"):
        state["pending"] = {"state": "writes_opened",
                            "started_at": (state.get("pending") or {}).get("started_at"),
                            "attempts": (state.get("pending") or {}).get("attempts") or 0}
    state["format"] = CUTOVER_STATE_FORMAT
    return state


def _begin_state(state, name):
    """开始一个状态：只记 `pending`，**不**推进 `state`（G04：回执才推进）。"""
    previous = _pending_of(state)
    attempts = (previous.get("attempts") or 0) + 1 if previous and previous["state"] == name else 1
    state["pending"] = {"state": name, "started_at": _utc_now(), "attempts": attempts}
    state["history"].append({
        "state": name, "started_at": _utc_now(), "status": "running", "attempts": attempts,
    })


def _finish_state(state, name, effects):
    """完成回执：只有这里能推进 `state` 并清空 `pending`。"""
    entry = state["history"][-1]
    assert entry["state"] == name
    entry["status"] = "complete"
    entry["finished_at"] = _utc_now()
    if effects:
        entry["effects"] = effects
    state["state"] = name
    state["pending"] = None


def _require_action(state, next_state, actions):
    spec = CUTOVER_REQUIRED_ACTIONS.get(next_state)
    if not spec:
        return
    key, description = spec
    if not actions.get(key):
        raise RuntimeError(
            f"cutover 推进到 {next_state} 需要 {key}（{description}）；"
            "控制器拒绝在缺配置时推进（§8.3：不能伪造阶段完成）"
        )


def run_action(action, root, env=None):
    """执行注入动作：argv 列表 → subprocess；callable → 直接调用。

    `env` 用于把「控制器已持维护锁」这一事实**显式**传给子进程（见
    `pg_migration_lib.acquire_migration_lock`）：迁移器据此不再重复取同一把锁，
    但仍会在 pg_locks 里核实锁确实被持有。
    """
    if action is None:
        return None
    if callable(action):
        # 可调用动作自己调 run_action；`env` 必须透传，否则「控制器已持锁」的
        # 声明到不了迁移器子进程（实测 exit 4：控制器与自己的迁移器抢同一把锁）。
        # 用签名判定而不是 try/except TypeError——后者会把动作**内部**的 TypeError
        # 误当成「参数不匹配」再调一次。
        if "env" in inspect.signature(action).parameters:
            return action(root, env=env)
        return action(root)
    child_env = None if env is None else {**os.environ, **env}
    result = subprocess.run(list(action), cwd=str(root), capture_output=True, text=True,
                            timeout=1800, env=child_env)
    if result.returncode != 0:
        # 现场只有这条错误能看到子进程说了什么：不带 tail 等于让人重新手跑一遍
        tail = (result.stderr or result.stdout or "").strip()[-500:]
        raise RuntimeError(
            f"注入动作失败 (exit {result.returncode}): {action[0]}"
            + (f"；输出尾部：{tail}" if tail else ""))
    return {"stdout_tail": (result.stdout or "")[-2000:], "exit": result.returncode}


def _root_matches_release(root, release_dir):
    """复用准备回执前，必须复核 root 上的受控文件与 release 仍逐字节一致。

    复核 A：只比 HEAD/release 摘要会漏掉 `app.py`、`db.py`、`docker-compose.yml`
    这类不在 `CUTOVER_EXECUTION_FILES` 里的受控文件——它们在 prepare 成功之后被改，
    重入仍会复用「已准备」回执。读几十个文件摘要的代价换这个洞，值得。
    """
    if not release_dir:
        return False
    try:
        import cutover_prepare
        cutover_prepare.verify_root_matches_release(Path(root), Path(release_dir))
        return True
    except Exception:  # noqa: BLE001 - 复核不通过就不复用（走新鲜路径重新校验）
        return False


def _release_digest_or_none(release_dir):
    """release 目录的受控文件摘要；目录不在/读不到时返回 None（调用方按「不复用」处理）。"""
    if not release_dir:
        return None
    try:
        import cutover_prepare
        return cutover_prepare._release_digest(Path(release_dir))
    except Exception:  # noqa: BLE001 - 复用判定读不到就是不复用，不影响主流程
        return None


def _prepare_cutover_for_cli(root, requested_commit, actions, *, release_dir=None,
                             env_file=None, instance_role=None, prepare_cmd=None,
                             skip_prepare=False, state_dir=None, dry_run=False):
    """§8.3 准备阶段（V4-02）：把 root 推进到最终 SHA、预构建镜像、起 PG 服务。

    幂等：状态里已有绑定同一 requested 且 final == 当前 HEAD 的回执时直接复用
    （重入不会重复 build/up）。`--skip-prepare` 只用于「root 已在最终 SHA 且现场
    已就绪」的恢复场景，用了会在 evidence 里留下 skipped 记录。
    `--dry-run` 连准备也不做（dry-run 的语义是「零副作用」）；只想预检准备阶段时
    直接跑 `cutover_prepare.py --dry-run`。
    """
    root = Path(root).resolve()
    head = run_command(root, "git", "rev-parse", "HEAD").stdout.strip()
    if dry_run:
        return head, None
    if skip_prepare:
        # 跳过准备**不**把请求的 SHA 换成当前 HEAD：身份校验仍由 run_cutover 承担，
        # 否则「--skip-prepare」会顺手把 G02 的唯一闸门也绕过去。
        return requested_commit, {
            "format": CUTOVER_PREPARE_RECEIPT_FORMAT, "skipped": True,
            "requested_commit": requested_commit, "final_commit": head,
            "reason": "--skip-prepare（现场已就绪的恢复场景）"}
    existing = read_cutover_state(root, state_dir)
    recorded = ((existing or {}).get("evidence") or {}).get("prepare") or {}
    if not recorded:
        # 状态文件还没建（准备成功但进程死在写状态之前）：磁盘上的准备回执同样算数，
        # 只要它绑定的还是这次请求、最终 SHA 仍是当前 HEAD、release 也没换过。
        try:
            candidate = json.loads(
                (backup_path(root, CUTOVER_PREPARE_REPORT_RELATIVE)).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            candidate = {}
        if candidate.get("format") == CUTOVER_PREPARE_RECEIPT_FORMAT:
            recorded = candidate
    if (recorded.get("format") == CUTOVER_PREPARE_RECEIPT_FORMAT
            and not recorded.get("skipped")
            and recorded.get("status", "ok") == "ok"
            and recorded.get("requested_commit") == requested_commit
            and recorded.get("final_commit") == head
            and recorded.get("release", {}).get("digest")
            and _release_digest_or_none(recorded.get("release_dir")) ==
            recorded["release"]["digest"]
            and _root_matches_release(root, recorded.get("release_dir"))):
        return head, {**recorded, "reused": True}
    argv = actions.get("prepare_cmd") or _prepare_argv(
        root, requested_commit, release_dir=release_dir, env_file=env_file,
        instance_role=instance_role)
    run_action(argv, root)
    report = backup_path(root, CUTOVER_PREPARE_REPORT_RELATIVE)
    try:
        receipt = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"准备阶段回执不可读（{type(exc).__name__}）：{report}") from None
    if receipt.get("format") != CUTOVER_PREPARE_RECEIPT_FORMAT:
        raise RuntimeError(f"准备阶段回执格式不符：{report}")
    if receipt.get("requested_commit") != requested_commit:
        raise RuntimeError("准备阶段回执的 requested_commit 与本次请求不符")
    final = receipt.get("final_commit")
    now = run_command(root, "git", "rev-parse", "HEAD").stdout.strip()
    if not final or final != now:
        raise RuntimeError(f"准备阶段回执的最终 SHA {final} 与当前 HEAD {now} 不符")
    if not (receipt.get("release") or {}).get("files_checked"):
        raise RuntimeError("准备阶段回执缺少 release 校验结果：拒绝用未校验的 release 割接")
    if not receipt.get("pg_service"):
        raise RuntimeError("准备阶段回执缺少 PG 服务就绪证据：拒绝在未就绪的目标上割接")
    return final, receipt


def check_deploy_lock(root, fd):
    path = backup_db.operation_lock_path(Path(root) / backup_db.DB_RELATIVE)
    held, expected = os.fstat(fd), path.stat()
    if (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino):
        raise RuntimeError("部署锁描述符与数据真实路径不匹配")
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("retire", "deploy", "check-lock", "rollback", "publish-runtime",
                    "cron-migration", "timed", "cutover", "deploy-schema", "deploy-accept"):
        child = sub.add_parser(command)
        child.add_argument("root", type=Path)
        if command == "deploy":
            child.add_argument("args", nargs=argparse.REMAINDER)
        elif command == "check-lock":
            child.add_argument("fd", type=int)
        elif command in ("deploy-schema", "deploy-accept"):
            child.add_argument("tag")
            child.add_argument("--lock-fd", type=int, required=True)
        elif command == "publish-runtime":
            child.add_argument("--lock-fd", type=int)
        elif command == "rollback":
            child.add_argument("tag")
            child.add_argument("--data-only", action="store_true")
            child.add_argument("--health-timeout", type=float, default=120)
            child.add_argument("--poll-interval", type=float, default=1)
        elif command == "cron-migration":
            child.add_argument("--dry-run", action="store_true", required=True)
        elif command == "cutover":
            child.add_argument("--target-commit", required=True)
            child.add_argument("--yes", action="store_true")
            child.add_argument("--migrator")
            child.add_argument("--verify-cmd")
            child.add_argument("--stop-cmd")
            child.add_argument("--open-cmd")
            child.add_argument("--publish-cmd")
            child.add_argument("--backup-restore-cmd")
            # V4-02/§8.3 准备阶段：候选 release、生产 env、实例角色、准备动作注入。
            child.add_argument("--release-dir", type=Path)
            child.add_argument("--deploy-env", type=Path)
            child.add_argument("--instance-role")
            child.add_argument("--prepare-cmd")
            child.add_argument("--skip-prepare", action="store_true")
            child.add_argument("--state-dir")
            child.add_argument("--dry-run", action="store_true")
            child.add_argument("--lock-fd", type=int)
    args = parser.parse_args()
    try:
        if args.command == "retire":
            count = retire_groups(args.root)
            print(f"归档旧备份 {count} 组（保留最新 10 个正常时间标签，特殊标签保留）")
        elif args.command == "check-lock":
            check_deploy_lock(args.root, args.fd)
        elif args.command == "deploy":
            env = deployment_environment(args.root)
            with backup_db.operation_lock(args.root / backup_db.DB_RELATIVE) as fd:
                return subprocess.run(
                    ["bash", str(args.root / "scripts/deploy.sh"), *args.args], cwd=args.root,
                    env={**env, "CONSOLE_DEPLOY_LOCK_FD": str(fd)}, pass_fds=(fd,),
                ).returncode
        elif args.command in ("deploy-schema", "deploy-accept"):
            check_deploy_lock(args.root, args.lock_fd)
            os.environ.update(deployment_environment(args.root))
            action = deploy_schema if args.command == "deploy-schema" else accept_deploy
            result = action(args.root, args.tag, lock_fd=args.lock_fd)
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "publish-runtime":
            print(json.dumps(publish_runtime(args.root, lock_fd=args.lock_fd),
                             ensure_ascii=False, indent=2))
        elif args.command == "cron-migration":
            print(json.dumps(cron_migration_plan(args.root), ensure_ascii=False, indent=2))
        elif args.command == "cutover":
            if not args.yes:
                print("ERROR: cutover 需要 --yes 确认（§8.3 受控编排）", file=sys.stderr)
                return 1
            actions = {}
            for key in ("migrator", "verify_cmd", "stop_cmd", "open_cmd", "publish_cmd",
                        "backup_restore_cmd"):
                arg = getattr(args, key)
                if arg:
                    actions[key] = shlex.split(arg)
            try:
                def _run():
                    prepared = _prepare_cutover_for_cli(
                        args.root, args.target_commit, actions,
                        release_dir=args.release_dir, env_file=args.deploy_env,
                        instance_role=args.instance_role, prepare_cmd=args.prepare_cmd,
                        skip_prepare=args.skip_prepare, state_dir=args.state_dir,
                        dry_run=args.dry_run)
                    final_commit, receipt = prepared
                    return run_cutover(
                        args.root, target_commit=final_commit, actions=actions,
                        dry_run=args.dry_run, state_dir=args.state_dir,
                        prepare_receipt=receipt,
                    )

                if args.lock_fd is not None:
                    # deploy.sh wrapper 已持有真实路径 flock；沿用其 FD，不递归再抢。
                    check_deploy_lock(args.root, args.lock_fd)
                    state = _run()
                else:
                    with backup_db.operation_lock(args.root / backup_db.DB_RELATIVE):
                        state = _run()
                print(json.dumps(state, ensure_ascii=False, indent=2, default=str))
            except RuntimeError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
        elif args.command == "timed":
            backup_db.ROOT = args.root.resolve()
            backup_db.BACKUP_ROOT = timed_backup_dir(args.root)
            return backup_db.main()
        else:
            if not math.isfinite(args.health_timeout) or not math.isfinite(args.poll_interval) or args.health_timeout < 0 or args.poll_interval <= 0:
                raise ValueError("health timeout/interval 参数不合法")
            return rollback(args.root, args.tag, args.data_only, args.health_timeout, args.poll_interval)
    except (Exception, KeyboardInterrupt) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise InterruptedError(f"操作被信号 {signum} 中断")

    signal.signal(signal.SIGTERM, interrupted)
    sys.exit(main())
