#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""割接写者冻结/恢复：`freeze|release|status ROOT`（提案 §8.3 / §11.3.2 G01+G03、§6.4）。

**为什么要有这个脚本**：割接期间的「停写」不能只在文档里写「由现场注入命令」——
那样 PG-07 只能临时拼 shell，没有人能审。本脚本给出仓库内、可测试、可审阅的
停写/恢复配置，`cutover_default_actions()` 用它做缺省值，`backup_runtime.py cutover`
与 `deploy.sh --storage-cutover` 两条入口走的是同一条路。

**停写必须持久**（G03）：控制器进程被 kill、app 重启、维护锁连接断开之后，业务写
仍要拒绝。因此这里是「库 + 配置」两条腿，不依赖任何进程活着：

1. `--role`：PG 账号级冻结。撤销业务表 DML 与序列 `USAGE/UPDATE`、**保留 SELECT**，
   并尽力终止既有会话。权限写在库里，控制器死了照样拒写；`console_admin` 不在冻结
   范围内，管理/恢复通道全程可用。
2. `--deploy-env`：把 `CONSOLE_MAINTENANCE_MODE=read_only` 原子写进部署 env 文件，
   app 重启后读到的仍是只读态（不是进程内标志）。
3. `--stop-writers`：站点自己的停写命令（停旧 app / cron / 其它写者），按命令串给，
   不做任何猜测。

**纪律**：什么都不声明 → 退出 2（「没声明」不等于「已经停干净了」）；超管角色与表
属主角色拒绝冻结（REVOKE 对它们无效，冻了也是假成功）；失败也留档，`release` 按
基线逐条恢复并**复验实际权限**，不靠「state 字符串说好了」。

退出码：0 成功 · 1 运行期失败（含停写命令失败，此时记录为 partial） · 2 用法/配置错误。
"""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent
for _path in (str(ROOT_DIR / "console" / "backend"), str(ROOT_DIR / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from host_paths import backup_path  # noqa: E402
from pg_migration_lib import connect_target, target_config  # noqa: E402

SCHEMA = "public"
RECORD_RELATIVE = "backup/cutover/writers-freeze.json"
ENV_KEY = "CONSOLE_MAINTENANCE_MODE"
RECORD_FORMAT = "console-cutover-writers-1"

# 冻结撤哪些：DML 全撤，SELECT 保留（§6.4「临时 SELECT 权限」）。
TABLE_PRIVS = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
REVOKE_TABLE_PRIVS = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")
SEQUENCE_PRIVS = ("USAGE", "SELECT", "UPDATE")
REVOKE_SEQUENCE_PRIVS = ("USAGE", "UPDATE")


class UsageError(RuntimeError):
    """用法/配置错误 → 退出 2。"""


class FreezeFailure(RuntimeError):
    """运行期失败 → 退出 1（已完成的步骤仍留在记录里）。"""


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
            mode="w", dir=str(path.parent), prefix=path.name + ".tmp-", delete=False,
            encoding="utf-8") as handle:
        temp = pathlib.Path(handle.name)
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(str(temp), str(path))
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


# --- 部署 env 文件 -------------------------------------------------------------


def _env_line_pattern(key):
    return re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")


def _set_env_value(path, key, value):
    """原地改一行（保留其它行与 `export ` 前缀）；value=None 表示删掉该行。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    out, replaced = [], False
    for line in lines:
        if _env_line_pattern(key).match(line):
            if value is None:
                replaced = True
                continue
            prefix = "export " if line.lstrip().startswith("export ") else ""
            out.append(f"{prefix}{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced and value is not None:
        out.append(f"{key}={value}")
    _atomic_write(path, "\n".join(out) + "\n")


def _read_env_value(path, key):
    for line in path.read_text(encoding="utf-8").splitlines():
        if _env_line_pattern(key).match(line):
            _raw = line.split("=", 1)[1] if "=" in line else ""
            return _raw.strip().strip('"').strip("'")
    return None


def _capture_deploy_env(path):
    if not path.is_file():
        raise UsageError(f"--deploy-env 指向的文件不存在：{path}（不做猜测，拒绝空转）")
    return {"path": str(path), "sha256_before": _sha256(path),
            "previous": _read_env_value(path, ENV_KEY)}


def _freeze_deploy_env(path, baseline):
    _set_env_value(path, ENV_KEY, "read_only")
    if _read_env_value(path, ENV_KEY) != "read_only":
        raise FreezeFailure(f"写入 {ENV_KEY}=read_only 后复读不一致：{path}")
    return {**baseline, "sha256_after": _sha256(path)}


def _release_deploy_env(entry, path=None):
    target = pathlib.Path(entry["path"]) if path is None else pathlib.Path(path)
    _set_env_value(target, ENV_KEY, entry["previous"])
    readback = _read_env_value(target, ENV_KEY)
    if readback != entry["previous"]:
        raise FreezeFailure(f"恢复 {ENV_KEY} 失败：期望 {entry['previous']!r} 实得 {readback!r}")
    return {"path": str(target), "restored": entry["previous"], "sha256_after": _sha256(target)}


# --- PG 权限面 -----------------------------------------------------------------


def _tables(conn):
    return [row["table_name"] for row in conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY table_name",
        (SCHEMA,))]


def _sequences(conn):
    return [row["sequence_name"] for row in conn.execute(
        "SELECT sequence_name FROM information_schema.sequences "
        "WHERE sequence_schema = %s ORDER BY sequence_name", (SCHEMA,))]


def _qualified(name):
    """限定名在 **Python 侧**拼：psycopg 的客户端参数替换会把 SQL 里的 `%I`
    当占位符拒绝（实测 `only '%s','%b','%t' are allowed`），`format()` 用不了。"""
    return f'"{SCHEMA}"."{name.replace(chr(34), chr(34) * 2)}"'


def _privilege_baseline(conn, role, names, privileges, checker):
    baseline = {}
    for name in names:
        checks = ", ".join(f"{checker}(%s, %s, '{priv}') AS \"{priv}\"" for priv in privileges)
        params = []
        for _ in privileges:
            params += [role, _qualified(name)]
        row = conn.execute(f"SELECT {checks}", params).fetchone()
        baseline[name] = {priv: bool(row[priv]) for priv in privileges}
    return baseline


def _table_baseline(conn, role, tables):
    return _privilege_baseline(conn, role, tables, TABLE_PRIVS, "has_table_privilege")


def _sequence_baseline(conn, role, sequences):
    return _privilege_baseline(conn, role, sequences, SEQUENCE_PRIVS,
                               "has_sequence_privilege")


def _role_guard(conn, role):
    """拒绝「冻了也白冻」的角色——不能报告一个假的已冻结。"""
    row = conn.execute(
        "SELECT rolcanlogin, rolsuper FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    if row is None:
        raise UsageError(f"角色不存在：{role}")
    if row["rolsuper"]:
        raise UsageError(
            f"拒绝冻结超级用户 {role}：REVOKE 对超级用户无效，冻结会假成功（§6.4 受限账号）")
    owned = conn.execute(
        "SELECT count(*) AS c FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND pg_get_userbyid(c.relowner) = %s AND c.relkind IN ('r','p')",
        (SCHEMA, role)).fetchone()["c"]
    if owned:
        raise UsageError(
            f"拒绝冻结表属主角色 {role}：属主自带全部权限，REVOKE 不会生效"
            f"（运行账号应是 §6.3 的受限账号，不是属主）")


def _capture_role_baseline(conn, role):
    _role_guard(conn, role)
    return {"tables": _table_baseline(conn, role, _tables(conn)),
            "sequences": _sequence_baseline(conn, role, _sequences(conn))}


def _freeze_role(conn, role, baseline):
    """Caller persists every baseline first, then wraps all revokes in one transaction."""
    quoted_role = role.replace('"', '""')
    if baseline["tables"]:
        conn.execute(
            f'REVOKE {", ".join(REVOKE_TABLE_PRIVS)} ON ALL TABLES IN SCHEMA {SCHEMA} '
            f'FROM "{quoted_role}"')
    if baseline["sequences"]:
        conn.execute(
            f'REVOKE {", ".join(REVOKE_SEQUENCE_PRIVS)} ON ALL SEQUENCES IN SCHEMA {SCHEMA} '
            f'FROM "{quoted_role}"')


def _terminate_role_sessions(conn, role):
    # Session termination cannot roll back, so do it only after the revoke transaction commits.
    try:
        rows = conn.execute(
            "SELECT pg_terminate_backend(pid) AS ok FROM pg_stat_activity "
            "WHERE usename = %s AND pid <> pg_backend_pid()", (role,)).fetchall()
        return {"terminated_sessions": sum(1 for row in rows if row["ok"]), "terminate_error": None}
    except Exception as exc:
        return {"terminated_sessions": 0, "terminate_error": type(exc).__name__}


def _verify_frozen(conn, role, baseline):
    """复验**实际权限面**（不是 state 字符串）：写被撤、读还在。"""
    tables = list(baseline["tables"])
    sequences = list(baseline["sequences"])
    after_tables = _table_baseline(conn, role, tables) if tables else {}
    after_sequences = _sequence_baseline(conn, role, sequences) if sequences else {}
    writable = sorted(table for table, privs in after_tables.items()
                      if any(privs[priv] for priv in REVOKE_TABLE_PRIVS))
    seq_usable = sorted(seq for seq, privs in after_sequences.items()
                        if any(privs[priv] for priv in REVOKE_SEQUENCE_PRIVS))
    return {
        "insert_denied": not writable,
        "writable_tables": writable,
        "sequence_usage_denied": not seq_usable,
        "writable_sequences": seq_usable,
        "select_kept": all(after_tables[table]["SELECT"] == bool(baseline["tables"][table]["SELECT"])
                           for table in tables),
    }


def _release_role(conn, role, baseline):
    restored = {"tables": {}, "sequences": {}}
    for table, privs in baseline["tables"].items():
        wanted = [priv for priv in TABLE_PRIVS if privs.get(priv)]
        if wanted:
            conn.execute(
                f'GRANT {", ".join(wanted)} ON TABLE {SCHEMA}."{table}" TO "{role}"')
        restored["tables"][table] = wanted
    for sequence, privs in baseline["sequences"].items():
        wanted = [priv for priv in SEQUENCE_PRIVS if privs.get(priv)]
        if wanted:
            conn.execute(
                f'GRANT {", ".join(wanted)} ON SEQUENCE {SCHEMA}."{sequence}" TO "{role}"')
        restored["sequences"][sequence] = wanted
    # 复验：基线里为真的权限必须真的回来（否则「恢复」只是打印了一行字）
    after_tables = _table_baseline(conn, role, list(baseline["tables"]))
    after_sequences = _sequence_baseline(conn, role, list(baseline["sequences"]))
    mismatches = [
        {"table": table, "privilege": priv, "expected": True, "actual": False}
        for table, privs in baseline["tables"].items() for priv, value in privs.items()
        if value and not after_tables[table][priv]
    ] + [
        {"sequence": seq, "privilege": priv, "expected": True, "actual": False}
        for seq, privs in baseline["sequences"].items() for priv, value in privs.items()
        if value and not after_sequences[seq][priv]
    ]
    return {"restored": restored, "mismatches": mismatches, "verified": not mismatches}


def _connect():
    try:
        config = target_config()
    except Exception as exc:  # PgConfigError：缺配置属于用法错误（2），不是运行失败
        raise UsageError(f"PG 配置缺失：{exc}") from exc
    conn = connect_target(config, statement_timeout=120)
    return conn


# --- 记录文件 ------------------------------------------------------------------


def record_path_for(root, record=None):
    return pathlib.Path(record) if record else backup_path(root, RECORD_RELATIVE)


def read_record(root, record=None):
    path = record_path_for(root, record)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise FreezeFailure(f"冻结记录损坏（{type(exc).__name__}）：{path}") from exc


def _write_record(path, record):
    _atomic_write(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")


def _prior_record(root, path):
    """上一份冻结记录（重入时继承它的基线）。缺失返回 None；损坏直接拒绝。

    损坏不能静默当「没有上一份」：那会丢掉**冻结之前**的真实权限面，`release`
    就只能恢复一份残缺记录，库里留下半冻结账号（G03 的持久兜底反过来伤人）。
    """
    prior = read_record(root, path)          # 损坏 → FreezeFailure
    if not isinstance(prior, dict) or prior.get("root") != str(root):
        return None
    return prior


def _merge_commands(*groups):
    merged = []
    for group in groups:
        for command in group or []:
            if command not in merged:
                merged.append(command)
    return merged


# --- freeze / release ----------------------------------------------------------


def freeze(root, *, roles=(), stop_writers=(), deploy_env=None, record=None):
    root = pathlib.Path(root).resolve()
    roles = [role for role in roles if role]
    stop_writers = [command for command in stop_writers if command]
    path = record_path_for(root, record)
    declared = {"roles": roles, "stop_writers": stop_writers,
                "deploy_env": str(deploy_env) if deploy_env else None}
    not_declared = [key for key, value in declared.items() if not value]
    if len(not_declared) == len(declared):
        raise UsageError(
            "未声明任何写者（--role / --stop-writers / --deploy-env 至少给一个）："
            "不声明就不能声称已停写（§8.3）")

    # G03/G04：冻结必须**可重入**。状态机重入会按 pending 重跑 `frozen` → 同一个冻结
    # 动作被调用第二次，此时角色已被撤权、env 文件已经是 read_only。若拿「当前状态」
    # 当基线，`release` 会把库「恢复」成拒写。所以：
    #   1. 基线跨重入继承（它要么是首次采集、要么继承自更早的首次采集，始终是冻结前的真值）；
    #   2. 本次没声明的写者（换了站点配置、只重跑一步）仍在冻结态，一并继承进记录，
    #      否则 release 只恢复一部分，库里静默留下半冻结账号。
    prior = _prior_record(root, path)
    prior_baseline = (prior or {}).get("baseline") or {}
    prior_roles = prior_baseline.get("roles") or {}
    prior_env = prior_baseline.get("deploy_env")
    prior_declared = (prior or {}).get("effective") or (prior or {}).get("declared") or {}
    record_data = {
        "format": RECORD_FORMAT, "status": "running", "root": str(root),
        "frozen_at": _utc_now(), "declared": declared, "not_declared": not_declared,
        "steps": [], "baseline": {"roles": dict(prior_roles),
                                  "deploy_env": prior_env if isinstance(prior_env, dict) else None},
        "role_facts": {}, "verified": {},
        "effective": {
            "roles": sorted(prior_roles),
            "deploy_env": (prior_env or {}).get("path") if isinstance(prior_env, dict) else None,
            "stop_writers": _merge_commands(prior_declared.get("stop_writers"),
                                            declared["stop_writers"]),
            "start_writers": _merge_commands(prior_declared.get("start_writers")),
        },
    }
    reentered = {"roles": sorted(set(prior_roles) & set(roles)),
                 "deploy_env": bool(deploy_env and isinstance(prior_env, dict)
                                    and prior_env.get("path") == str(declared["deploy_env"]))}
    if any(reentered.values()):
        record_data["refrozen"] = reentered
    conn = None
    try:
        # Capture the complete original state before env changes, REVOKE or process actions.
        # A failed legacy first attempt may have already committed a revoke without a baseline.
        # Never bless its current permissions as an original state on reentry.
        if prior and prior.get("status") in ("running", "partial") and not prior.get("baseline_ready"):
            missing = set(prior_declared.get("roles") or ()) - set(prior_roles)
            if missing or (prior_declared.get("deploy_env") and not prior_env):
                raise FreezeFailure("旧冻结记录缺少完整原始基线，拒绝重采；请先人工核验权限/env")
        if deploy_env:
            env_path = pathlib.Path(deploy_env).resolve()
            if prior_env and pathlib.Path(prior_env["path"]).resolve() != env_path:
                raise UsageError("已有冻结记录属于另一部署 env，拒绝覆盖原始基线")
            if not prior_env:
                record_data["baseline"]["deploy_env"] = _capture_deploy_env(env_path)
        if roles:
            conn = _connect()
            with conn.transaction():
                for role in roles:
                    _role_guard(conn, role)
                    if role not in prior_roles:
                        record_data["baseline"]["roles"][role] = _capture_role_baseline(conn, role)
        record_data["effective"]["roles"] = sorted(record_data["baseline"]["roles"])
        record_data["effective"]["deploy_env"] = (
            record_data["baseline"]["deploy_env"] or {}).get("path")
        record_data["baseline_ready"] = True
        record_data["phase"] = "baseline_saved"
        _write_record(path, record_data)  # fsync file AND directory before the first mutation.

        if deploy_env:
            entry = record_data["baseline"]["deploy_env"]
            applied = _freeze_deploy_env(pathlib.Path(entry["path"]), entry)
            record_data["steps"].append({"name": "deploy_env", "ok": True,
                                         "at": _utc_now(), "sha256_after": applied["sha256_after"]})
            _write_record(path, record_data)

        if roles:
            record_data["phase"] = "revoking"
            _write_record(path, record_data)
            with conn.transaction():
                for role in roles:
                    baseline = record_data["baseline"]["roles"][role]
                    _freeze_role(conn, role, baseline)
                    verified = _verify_frozen(conn, role, baseline)
                    if not all(verified[key] for key in
                               ("insert_denied", "sequence_usage_denied", "select_kept")):
                        raise FreezeFailure(f"角色 {role} 撤权后仍有写权限或 SELECT 已变化")
                    record_data["verified"][role] = verified
            record_data["phase"] = "roles_revoked"
            _write_record(path, record_data)
            for role in roles:
                baseline = record_data["baseline"]["roles"][role]
                record_data["role_facts"][role] = {
                    **_terminate_role_sessions(conn, role),
                    "tables": len(baseline["tables"]), "sequences": len(baseline["sequences"])}
                record_data["steps"].append({"name": "roles", "role": role,
                                             "ok": True, "at": _utc_now()})
                _write_record(path, record_data)

        for command in stop_writers:
            argv = command if isinstance(command, (list, tuple)) else _split(command)
            result = subprocess.run(argv, cwd=str(root), capture_output=True, text=True, timeout=600)
            record_data["steps"].append({
                "name": "stop_writers", "command": argv[0], "ok": result.returncode == 0,
                "exit": result.returncode, "at": _utc_now()})
            _write_record(path, record_data)
            if result.returncode != 0:
                raise FreezeFailure(f"停写命令失败 (exit {result.returncode})")
        record_data["status"] = "frozen"
        record_data["phase"] = "complete"
        record_data["finished_at"] = _utc_now()
        _write_record(path, record_data)
        return {**record_data, "record": str(path), "ok": True}
    except BaseException as exc:
        # A disconnect/SIGINT can occur after any side effect; the complete baseline survives.
        record_data["status"] = "partial" if record_data.get("baseline_ready") else "aborted"
        record_data["last_error"] = f"{type(exc).__name__}: {_redact(str(exc))}"
        record_data["finished_at"] = _utc_now()
        # Do not overwrite a possibly complete older baseline if validation/capture failed.
        if record_data.get("baseline_ready") or not prior:
            _write_record(path, record_data)
        if isinstance(exc, UsageError) or not isinstance(exc, Exception):
            raise
        raise FreezeFailure(f"{_redact(str(exc))}（原始基线已保留，可核验后 release）") from exc
    finally:
        if conn is not None:
            conn.close()


ALLOW_PREOPEN_ENV = "CONSOLE_CUTOVER_ALLOW_PREOPEN_RELEASE"


def cutover_preopen_reason(root):
    """cutover 状态机说「还没开放写入」时返回原因，否则 None。

    V4-03 的第二道闸（复核 A）：门禁由控制器强制，但任何能跑本脚本的人也能绕过它。
    没有 cutover 状态文件（非割接场景）不算拦截条件；确需在开放前做站点恢复
    （§7.3 的回退决策）时显式设置 `CONSOLE_CUTOVER_ALLOW_PREOPEN_RELEASE=1`。
    """
    path = backup_path(root, "backup/cutover/state.json")
    if not path.is_file():
        return None
    try:
        import backup_runtime
        opened = backup_runtime.cutover_writes_opened(root)
    except Exception:  # noqa: BLE001 - 状态不可判定时按保守方向拦（宁可要求显式豁免）
        return "cutover 状态不可判定"
    if opened:
        return None
    return "cutover 尚未开放写入（V4-03：首份 PG 备份恢复成功才开放）"


def release(root, *, start_writers=(), deploy_env=None, roles=(), record=None):
    root = pathlib.Path(root).resolve()
    path = record_path_for(root, record)
    preopen = cutover_preopen_reason(root)
    if preopen and not os.environ.get(ALLOW_PREOPEN_ENV):
        raise FreezeFailure(
            f"{preopen}：拒绝在开放前恢复业务写者（{path}）。"
            f"确需站点恢复请显式设置 {ALLOW_PREOPEN_ENV}=1")
    record_data = read_record(root, record)
    if record_data is None:
        raise UsageError(f"无冻结记录，拒绝凭空恢复：{path}")
    if record_data.get("status") == "released":
        return {**record_data, "record": str(path), "ok": True, "already_released": True}

    releases = {"at": _utc_now(), "roles": {}, "deploy_env": None, "start_writers": []}
    env_entry = record_data.get("baseline", {}).get("deploy_env")
    if env_entry:
        releases["deploy_env"] = _release_deploy_env(env_entry, deploy_env)
    baseline_roles = record_data.get("baseline", {}).get("roles") or {}
    wanted_roles = list(baseline_roles) if not roles else [r for r in roles if r in baseline_roles]
    if wanted_roles:
        conn = _connect()
        try:
            for role in wanted_roles:
                releases["roles"][role] = _release_role(conn, role, baseline_roles[role])
        finally:
            conn.close()
    # 恢复面按 `effective`（**仍处于冻结态**的全部写者，含重入继承）算，不是按某一次
    # 调用的 `declared`——否则重入后只恢复一部分，剩下的账号静默保持拒写。
    effective = record_data.get("effective") or record_data.get("declared") or {}
    commands = list(start_writers) + list(effective.get("start_writers") or [])
    for command in commands:
        argv = command if isinstance(command, (list, tuple)) else _split(command)
        result = subprocess.run(argv, cwd=str(root), capture_output=True, text=True, timeout=600)
        releases["start_writers"].append({
            "command": argv[0], "ok": result.returncode == 0, "exit": result.returncode,
            "stdout_tail": (result.stdout or "")[-500:]})
    if effective.get("stop_writers") and not commands:
        releases["start_writers_note"] = (
            "冻结时声明的停写命令没有对应启动命令：写者由站点按 §9 E 自行恢复")

    bad = [role for role, data in releases["roles"].items() if not data["verified"]]
    bad += [entry["command"] for entry in releases["start_writers"] if not entry["ok"]]
    record_data.setdefault("releases", []).append(releases)
    record_data["status"] = "released" if not bad else "release_failed"
    record_data["released_at"] = _utc_now()
    _write_record(path, record_data)
    if bad:
        raise FreezeFailure(f"恢复未完成或未通过复验：{bad}（记录已留档，可重入）")
    return {**record_data, "record": str(path), "ok": True}


def _split(command):
    import shlex

    return shlex.split(command)


# --- CLI -----------------------------------------------------------------------


def _env_list(name):
    raw = os.environ.get(name, "")
    return [item for item in re.split(r"[,\s]+", raw) if item]


def _env_one(name):
    value = os.environ.get(name)
    return [value] if value else []


def _merge(cli_values, env_values):
    merged = list(cli_values)
    for value in env_values:
        if value not in merged:
            merged.append(value)
    return merged


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for verb in ("freeze", "release", "status"):
        child = sub.add_parser(verb)
        child.add_argument("root", type=pathlib.Path)
        child.add_argument("--role", action="append", default=[],
                           help="要冻结/恢复的 PG 运行账号（可重复；env CONSOLE_CUTOVER_APP_ROLES）")
        child.add_argument("--stop-writers", action="append", default=[],
                           help="站点停写命令（可重复；env CONSOLE_CUTOVER_STOP_WRITERS）")
        child.add_argument("--start-writers", action="append", default=[],
                           help="站点恢复写者命令（可重复；env CONSOLE_CUTOVER_START_WRITERS）")
        child.add_argument("--deploy-env", type=pathlib.Path,
                           help="部署 env 文件（写/还原 CONSOLE_MAINTENANCE_MODE）")
        child.add_argument("--record", type=pathlib.Path, help="冻结记录路径")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            path = record_path_for(args.root, args.record)
            record_data = read_record(args.root, args.record)
            print(json.dumps({"ok": True, "record": str(path),
                              "status": (record_data or {}).get("status", "none"),
                              "record_data": record_data}, ensure_ascii=False, indent=2))
            return 0
        deploy_env = args.deploy_env or os.environ.get("CONSOLE_CUTOVER_DEPLOY_ENV")
        if args.command == "freeze":
            result = freeze(
                args.root,
                roles=_merge(args.role, _env_list("CONSOLE_CUTOVER_APP_ROLES")),
                stop_writers=_merge(args.stop_writers, _env_one("CONSOLE_CUTOVER_STOP_WRITERS")),
                deploy_env=deploy_env, record=args.record)
        else:
            result = release(
                args.root,
                start_writers=_merge(args.start_writers,
                                     _env_one("CONSOLE_CUTOVER_START_WRITERS")),
                deploy_env=deploy_env, roles=args.role, record=args.record)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except UsageError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 统一脱敏后再报
        print(json.dumps({"ok": False, "error": _redact(str(exc))}, ensure_ascii=False),
              file=sys.stderr)
        return 1


def _redact(text):
    """脱敏判据是「可能带上**值**」，不是「提到了 password 这个词」。

    配置缺失的文案会点名 `CONSOLE_PG_PASSWORD` 这个变量名——那是要告诉人缺什么，
    不是秘密；真正要挡的是 `password=xxx` 这种带值的形态（驱动异常/conninfo 回显）。
    """
    lowered = text.lower()
    if "password=" in lowered or "pgpassword" in lowered or "conninfo" in lowered:
        return "连接/配置错误（细节含敏感字段，已脱敏）"
    return text


if __name__ == "__main__":
    sys.exit(main())
