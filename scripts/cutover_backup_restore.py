#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""§9 D6 首份 PG 一致性备份 + 隔离恢复 + 逐值/金额/原件映射验证（V4-03 门禁）。

主控 §11.5.2 的 V4-03：修复前 `verified_read_only` 只跑独立迁移比较器，下一步就
准备开放——「首 PG dump → 隔离 restore → 完整比较」从来没有接进正式 cutover 入口。
本模块就是那个动作，由 `backup_runtime.py cutover` 在 `backup_verified` 状态调用；
它失败（或回执对不上）时控制器**不得**调用 open、不得发放 DML。

做了什么：
1. 对活 PG 目标做 `pg_dump -Fc` + 侧车 manifest（`backup_db.write_pg_dump`，字段
   从活库现查、fail closed）。
2. 在**隔离**实例的新建空库上恢复该 dump（`docker exec` 或本机 `pg_restore`）。
3. `backup_db.verify_restore`：dump 摘要 == manifest、schema 版本、表清单、逐表
   行数/行指纹/金额合计（**恢复副本 vs 活库**）。
4. `pg_migration_lib.compare_source_target`：**恢复副本 vs 冻结的 SQLite 源快照**
   逐字段比较（这是 §9 D6 的「逐值/金额/原件映射」判据；`ledger_file_batches`
   等目标独有表在首次割接期间必须为空）。
5. 写回执（`console-cutover-restore-1`）：绑定本次**源指纹 / 目标身份 / 候选执行
   摘要 / dump 摘要 / 恢复库身份**；重入时控制器按这些字段复核，不复用不相关旧报告。

隔离要求：`CONSOLE_CUTOVER_RESTORE_DSN` 必填，且必须是**另一个 PG 集群**——
判据是服务端自述身份（`system_identifier`，读不到时退回 `addr/port/database`），
不是配置字符串（`localhost`/`hostaddr=`/URI 等写法绕不过去）；恢复容器（若配置）
还必须与 DSN 指向同一实例。不设或指向生产一律拒绝执行。

退出码：0 成功；1 验证失败；2 用法/配置错误。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import backup_db as bkp  # noqa: E402
import backup_runtime as runtime  # noqa: E402
import pg_migration_lib  # noqa: E402
import pg_migrations  # noqa: E402
from host_paths import backup_path  # noqa: E402

RECEIPT_FORMAT = "console-cutover-restore-1"
DEFAULT_REPORT_RELATIVE = "backup/cutover/restore-verify.json"
TARGET_ONLY_TABLES = pg_migration_lib.TARGET_ONLY_TABLES


class GateError(RuntimeError):
    pass


class UsageError(GateError):
    pass


def _utc_now():
    return runtime._utc_now()


def _dsn_fields(dsn: str) -> dict:
    """keyword 与 URI 两种 libpq 写法都要解析（复核 A：只按 `=` 分片会让
    `postgresql://user@prod:5432/console` 解析成空 dict → 隔离判定被绕过）。"""
    from psycopg.conninfo import conninfo_to_dict
    try:
        fields = conninfo_to_dict(dsn)
    except Exception as exc:  # noqa: BLE001 - 解析不了就必须拒绝，不能当「没有 host」
        raise UsageError(
            f"CONSOLE_CUTOVER_RESTORE_DSN 无法解析（{type(exc).__name__}）："
            "支持 libpq 的 keyword 与 URI 两种写法") from exc
    return {key: value for key, value in fields.items() if value}


def _identity_from_connection(conn) -> dict:
    """从一条已建立的连接读服务端自述身份。

    `system_identifier` 需要权限（受限账号读不到 → None），
    `inet_server_addr()/inet_server_port()/current_database()` 任何账号可读。
    """
    row = conn.execute(
        "SELECT current_database() AS database, inet_server_addr()::text AS addr,"
        " inet_server_port() AS port").fetchone()
    identity = {"database": row["database"], "addr": row["addr"], "port": row["port"],
                "system_identifier": None}
    try:
        identity["system_identifier"] = str(conn.execute(
            "SELECT system_identifier FROM pg_control_system()").fetchone()[
                "system_identifier"])
    except Exception:  # noqa: BLE001 - 权限不足时降级，用 addr/port/database 判
        pass
    return identity


def server_identity(dsn: str) -> dict:
    """连上隔离实例读它**自己**看到的身份（不是配置字符串）。"""
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(dsn, connect_timeout=10, autocommit=True,
                         row_factory=dict_row) as conn:
        return _identity_from_connection(conn)


def production_identity() -> dict:
    """控制器视角的生产目标身份（用迁移凭据只读连接）。"""
    conn = pg_migration_lib.connect_target(readonly=True, statement_timeout=30)
    try:
        return _identity_from_connection(conn)
    finally:
        conn.close()


def assert_isolated_restore_instance(dsn: str) -> dict:
    """隔离判定：**服务端自述身份**不同集群才算隔离（复核 A/B）。

    只比配置字符串会被 `localhost` / `hostaddr=` / URI / 同一台机器的另一个地址
    绕过。这里问的是「恢复目标与服务端自述的生产目标是不是同一个集群」：
      - 两边都能读到 `system_identifier` → 必须不同；
      - 读不到（受限账号）→ 退回 `(addr, port, database)` 三元组，必须不同。
    """
    if not dsn.strip():
        raise UsageError(
            "未配置 CONSOLE_CUTOVER_RESTORE_DSN：§9 D6 要求恢复到**独立** PG 再比较，"
            "不设就拒绝执行（拒绝拿生产库当恢复目标）")
    restored = _dsn_fields(dsn)
    production = production_identity()
    try:
        restored_identity = server_identity(dsn)
    except Exception as exc:  # noqa: BLE001 - 连不上就拒绝，不猜它是不是隔离的
        raise GateError(
            f"恢复目标不可连接（{type(exc).__name__}）：无法证明它是独立实例，拒绝执行"
        ) from exc
    same_cluster = (
        production["system_identifier"] is not None
        and restored_identity["system_identifier"] is not None
        and production["system_identifier"] == restored_identity["system_identifier"]
    )
    same_endpoint = (
        production["database"] == restored_identity["database"]
        and production["addr"] == restored_identity["addr"]
        and production["port"] == restored_identity["port"]
    )
    if same_cluster or same_endpoint:
        raise UsageError(
            f"CONSOLE_CUTOVER_RESTORE_DSN 指向生产实例/库"
            f"（{restored_identity['addr']}:{restored_identity['port']}/"
            f"{restored_identity['database']}）：§9 D6 要求恢复到**独立** PG 再比较")
    return {"configured": {key: restored.get(key) for key in ("host", "port", "user", "dbname")},
            "production": production, "restore": restored_identity}


def _pg_restore_commands(dsn: str, dbname: str) -> tuple[list[str], list[str]]:
    """(restore_cmd, psql_cmd)：容器形态走 docker exec，否则用本机客户端。

    非容器形态用 `make_conninfo` 生成连接串（自动转义特殊字符），**密码不进 argv**：
    走临时 `.pgpass` + `PGPASSFILE`（见 `_secret_env`），与 migrator 的脱敏纪律一致。
    """
    from psycopg.conninfo import make_conninfo
    container = (os.environ.get("CONSOLE_CUTOVER_RESTORE_CONTAINER") or "").strip()
    fields = _dsn_fields(dsn)
    user = fields.get("user", "")
    if container:
        return (
            ["docker", "exec", "-i", container, "pg_restore", "-U", user,
             "-d", dbname],
            ["docker", "exec", container, "psql", "-U", user, "-d", dbname, "-Atc"],
        )
    client = shlex.split(os.environ.get("CONSOLE_CUTOVER_PG_RESTORE") or "pg_restore")
    psql = shlex.split(os.environ.get("CONSOLE_CUTOVER_PSQL") or "psql")
    target = make_conninfo(
        host=fields.get("host"), port=fields.get("port"), user=user or None,
        dbname=dbname, connect_timeout="10")
    return ([*client, "-w", "-d", target], [*psql, "-w", target, "-Atc"])


class _secret_env:
    """把恢复实例的口令经临时 `.pgpass`（0600）交给子进程，不进 argv/日志。"""

    def __init__(self, dsn: str):
        fields = _dsn_fields(dsn)
        self.password = fields.get("password") or ""
        self.fields = fields
        self.path = None
        self.previous = None

    def __enter__(self):
        if not self.password:
            return self
        self.path = Path(tempfile.mkstemp(prefix=".pgpass-", suffix=".tmp")[1])
        self.path.chmod(0o600)
        self.path.write_text(
            "*:*:*:*:" + self.password.replace("\\", "\\\\").replace(":", "\\:")
            + "\n", encoding="utf-8")
        self.previous = os.environ.get("PGPASSFILE")
        os.environ["PGPASSFILE"] = str(self.path)
        return self

    def __exit__(self, *exc):
        if self.previous is None:
            os.environ.pop("PGPASSFILE", None)
        else:
            os.environ["PGPASSFILE"] = self.previous
        if self.path is not None:
            self.path.unlink(missing_ok=True)
        return False


def _create_restore_database(dsn: str, dbname: str) -> None:
    """建恢复库并**先建角色**：dump 里带 §6.3 的角色/默认权限语句，隔离实例没有
    这些角色时 pg_restore 会以「errors ignored on restore」退非零——那不是备份不可
    恢复，而是恢复现场没准备好。角色先建好，恢复就是逐字节忠实的那一份。

    **任何一步失败都自己清理**（复核 A：建库成功但角色 bootstrap 失败时，库会泄漏）。
    """
    import psycopg
    from psycopg.rows import dict_row
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
            conn.execute(f'CREATE DATABASE "{dbname}"')
        try:
            with psycopg.connect(_restored_dsn(dsn, dbname), autocommit=True,
                                 row_factory=dict_row) as conn:
                pg_migrations.bootstrap_roles(conn, database=dbname)
        except Exception as exc:  # noqa: BLE001 - 现场准备不了就明说，不降级成「恢复失败」
            raise GateError(
                f"恢复实例上无法建立 §6.3 角色矩阵（{type(exc).__name__}）：恢复目标需要"
                "建角色的权限（隔离实例的超级用户）；不允许用 --no-acl 降级冒充"
                "「恢复通过」") from exc
    except BaseException:
        try:
            _drop_restore_database(dsn, dbname)
        except Exception:  # noqa: BLE001 - 清理失败不能掩盖原始错误
            print(f"WARN: 恢复库 {dbname} 未能清理", file=sys.stderr)
        raise


def _assert_container_sees_database(container: str, user: str, dbname: str) -> None:
    """恢复容器与 DSN 必须指向**同一个实例**：容器里看不到刚建的库就拒绝。

    复核 A：`CONSOLE_CUTOVER_RESTORE_CONTAINER` 与 DSN 不交叉校验时，容器可以指向
    生产（在生产容器里执行恢复），或指向另一个实例（恢复与比较各说各话）。
    """
    result = subprocess.run(
        ["docker", "exec", container, "psql", "-U", user, "-d", dbname, "-Atc",
         "SELECT current_database()"],
        capture_output=True, text=True, timeout=60)
    if result.returncode != 0 or (result.stdout or "").strip() != dbname:
        raise GateError(
            f"恢复容器 {container} 看不到刚建的恢复库 {dbname}："
            "CONSOLE_CUTOVER_RESTORE_CONTAINER 与 CONSOLE_CUTOVER_RESTORE_DSN 不是同一个实例")


def _drop_restore_database(dsn: str, dbname: str) -> None:
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


def _restored_dsn(dsn: str, dbname: str) -> str:
    from psycopg.conninfo import make_conninfo
    fields = _dsn_fields(dsn)
    return make_conninfo(host=fields.get("host"), port=fields.get("port"),
                         dbname=dbname, user=fields.get("user"),
                         password=fields.get("password"))


def invalidate_receipt(path: Path, *, reason: str) -> None:
    """把回执置为「无效」：写一份 status=running 的占位（不是删除）。

    复核 B：门禁入口不清旧回执时，「上次成功、这次失败」会让控制器读到一份仍然
    自洽的旧回执。这里先落一份明确不可用的占位，失败路径就再也没有 ok 回执可读。
    """
    path.write_text(json.dumps({"format": RECEIPT_FORMAT, "status": "running",
                                "invalidated_reason": reason,
                                "started_at": _utc_now()}, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")


def run_gate(root: Path, *, source: Path, target_commit: str, report_path: Path,
             dump_tag: str | None = None) -> dict:
    root = Path(root).resolve()
    source = Path(source).resolve()
    if not source.is_file():
        raise UsageError(f"冻结源快照不存在：{source}")
    dsn = (os.environ.get("CONSOLE_CUTOVER_RESTORE_DSN") or "").strip()
    # 复核 B：旧的成功回执必须先失效——否则「上次成功、这次失败」时，控制器仍能
    # 读到一份四要素齐全的旧回执并据此开放写入。
    report_path.parent.mkdir(parents=True, exist_ok=True)
    invalidate_receipt(report_path, reason="run-started")
    restore_target = assert_isolated_restore_instance(dsn)
    # 稳定 tag：门禁的 dump 是「本次割接的首备份」，重入应当替换自己那一份而不是
    # 每次都堆一份全量副本；`--dump-tag` 仍可显式改名留档。
    tag = dump_tag or "cutover-current"
    dump_path = backup_path(root, "backup/cutover") / f"{bkp.PG_DUMP_PREFIX}.{tag}"
    dump_path.parent.mkdir(parents=True, exist_ok=True)

    receipt = {"format": RECEIPT_FORMAT, "root": str(root), "started_at": _utc_now(),
               "source": {"path": str(source), "sha256": runtime.file_digest(source),
                          "bytes": source.stat().st_size},
               # 门禁实现自身的摘要：回执要能证明「是哪份门禁代码产出的」
               "gate": {"script": "scripts/cutover_backup_restore.py",
                        "sha256": runtime.file_digest(Path(__file__).resolve())},
               "restore_target": restore_target,
               "target_commit": target_commit}
    # 执行身份：候选代码的摘要随回执走，控制器按它复核「这份报告是不是本候选产生的」
    identity = runtime.cutover_execution_identity(root, target_commit)
    receipt["execution"] = {"target_commit": target_commit, "head": identity["head"],
                            "files": identity["files"],
                            "digest": execution_digest(identity)}

    published, manifest_path = bkp.write_pg_dump(
        dump_path, schema_version=pg_migrations.latest_schema_version(), root=root)
    manifest = bkp.read_dump_manifest(published) or {}
    receipt["target"] = {"database": manifest.get("database_identity"),
                         "schema_version": manifest.get("schema_version"),
                         "schema_checksum": manifest.get("schema_checksum"),
                         "pg_server_version": manifest.get("pg_server_version"),
                         # 内容身份：控制器复核时对活库重算比对，避免「旧回执配新库」
                         "row_digest": (manifest.get("row_digest") or {}).get("digest")}
    receipt["dump"] = {"path": str(published), "name": published.name,
                       "sha256": runtime.file_digest(published),
                       "bytes": published.stat().st_size,
                       "manifest": str(manifest_path),
                       "row_digest_digest": (manifest.get("row_digest") or {}).get("digest")}

    dbname = f"cu_restore_{os.urandom(4).hex()}"
    _create_restore_database(dsn, dbname)
    container = (os.environ.get("CONSOLE_CUTOVER_RESTORE_CONTAINER") or "").strip()
    try:
        if container:
            _assert_container_sees_database(container, _dsn_fields(dsn).get("user", ""), dbname)
        restore_cmd, psql_cmd = _pg_restore_commands(dsn, dbname)
        with _secret_env(dsn):
            verified = bkp.verify_restore(
                published, boot_target=lambda: (restore_cmd, psql_cmd),
                expected_tables=sorted(pg_migrations.SCHEMA_TABLES),
                expected_schema_version=pg_migrations.latest_schema_version())
        receipt["restored"] = {"database": dbname, **verified}

        import psycopg
        conn = psycopg.connect(_restored_dsn(dsn, dbname), autocommit=False,
                               row_factory=psycopg.rows.dict_row)
        try:
            comparison = pg_migration_lib.compare_source_target(str(source), conn)
        finally:
            conn.close()
        if comparison["mismatches"]:
            raise GateError(
                f"恢复副本与源快照不一致：{comparison['mismatches'][:5]}"
                f"（共 {len(comparison['mismatches'])} 处）")
        receipt["comparison"] = {
            "tables": comparison["tables"],
            "counts": comparison["counts"],
            "amount_summary": comparison["amount_summary"],
            "mismatches": [],
        }
        with psycopg.connect(_restored_dsn(dsn, dbname), autocommit=True) as probe:
            file_rows = probe.execute(
                "SELECT count(*) FROM ledger_file_batches").fetchone()[0]
        receipt["file_mapping"] = {
            "ledger_file_batches": {"restored": file_rows, "expected_empty": True},
            "pay": verified["pay"],
            "source_has_no_batch_table": True,
            "note": "批次原件与历史文件已独立恢复核验；首次割接目标独有表仍必须为空",
        }
        if file_rows or verified["pay"]["batches"] != file_rows:
            raise GateError(f"恢复副本的 ledger_file_batches 有 {file_rows} 行：首次割接必须为空")
        receipt["cleanup"] = {"restore_database": dbname, "dropped": False}
    finally:
        try:
            _drop_restore_database(dsn, dbname)
            if "cleanup" in receipt:
                receipt["cleanup"]["dropped"] = True
        except Exception as exc:  # noqa: BLE001 - 清理失败不能掩盖真正的判定
            if "cleanup" in receipt:
                receipt["cleanup"]["error"] = type(exc).__name__
    receipt["status"] = "ok"
    receipt["finished_at"] = _utc_now()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    return receipt


def execution_digest(identity: dict) -> str:
    """候选执行身份的稳定摘要（控制器与门禁共用同一算法）。"""
    payload = json.dumps(identity.get("files") or {}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target-commit", required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--dump-tag")
    args = parser.parse_args(argv)
    report = args.report or backup_path(args.root, DEFAULT_REPORT_RELATIVE)
    try:
        receipt = run_gate(args.root, source=args.source, target_commit=args.target_commit,
                           report_path=report, dump_tag=args.dump_tag)
        print(json.dumps({k: receipt[k] for k in
                          ("status", "dump", "restored", "comparison", "file_mapping")},
                         ensure_ascii=False, indent=2))
        return 0
    except UsageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI 边界统一成退出码
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
