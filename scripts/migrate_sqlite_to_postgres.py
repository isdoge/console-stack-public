#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`scripts/migrate_sqlite_to_postgres.py --mode preflight|apply|verify`（提案 §5.3；PG-03）。

- preflight：只读校验离线源快照（hash/WAL/quick_check/版本 1–11/映射/金额/悬挂/安全整数/JSON）。
- apply：源校验 + 目标空库/schema12 保护 → 单事务导入全部表 + 追加旧迁移历史 +
  sequence 高水位恢复 + **同事务**写完成凭据 → 原子发布报告。
  幂等：同来源+同目标身份再入只回显凭据摘要；非空无凭据 → 拒绝（4），绝不 TRUNCATE/DROP。
- verify：搬运器自检口径的只读复核（独立验证器另有 `verify_postgres_migration.py`；
  本模式不写任何状态，也不读上次运行的缓存）。

密码只来自环境（`CONSOLE_PG_*`，见 `pgdb.PgConfig.from_env`）；命令行与报告不携带 DSN。
退出码：0 成功 · 2 用法/配置 · 3 源 preflight 失败 · 4 目标保护拒绝 · 5 验证不一致 ·
6 数据副作用已完成但报告落盘失败（重跑幂等再生）。
"""
import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = ROOT / "console" / "backend"
for _path in (str(BACKEND), str(ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import pg_contracts  # noqa: E402
import sqlite_legacy  # noqa: E402
from pg_migration_lib import (  # noqa: E402
    CREDENTIAL_FORMAT,
    SOURCE_OPTIONAL_TABLES,
    TABLE_MAP,
    TOOL_VERSION,
    PreflightFailure,
    TargetGuardFailure,
    acquire_migration_lock,
    append_legacy_migrations,
    business_table_counts,
    compare_source_target,
    connect_target,
    copy_table_data,
    file_sha256,
    new_report,
    preflight_source,
    publish_report,
    read_credential,
    release_migration_lock,
    require_schema12,
    restore_sequences,
    target_config,
    target_identity,
    utc_now_text,
    wal_issues,
    write_credential,
)

EXIT_OK, EXIT_USAGE, EXIT_PREFLIGHT, EXIT_TARGET, EXIT_MISMATCH, EXIT_REPORT = 0, 2, 3, 4, 5, 6


def _emit(manifest, report_path, exit_code, started_status_note=None):
    """stdout 恒为脱敏摘要 JSON。报告落盘失败不掩盖业务结果；
    apply 提交成功后的落盘失败 → 6（重跑幂等再生）。"""
    summary = {
        "status": manifest["status"],
        "exit_code": exit_code,
        "mismatches": manifest["mismatches"],
    }
    if started_status_note:
        summary.update(started_status_note)
    print(json.dumps(summary, ensure_ascii=False))
    if report_path:
        try:
            publish_report(report_path, manifest)
        except OSError as exc:
            print(json.dumps({"status": "report-publish-failed",
                              "detail": type(exc).__name__,
                              "hint": "数据副作用已完成，重跑幂等再生报告"},
                             ensure_ascii=False), file=sys.stderr)
            if exit_code == EXIT_OK:
                return EXIT_REPORT
    return exit_code


def mode_preflight(args, started):
    info, issues = preflight_source(args.source, expected_sha256=args.source_sha256)
    manifest = new_report("preflight", args.target_purpose,
                          "ok" if not issues else "failed", started, info=info,
                          result={"mismatches": issues})
    return _emit(manifest, args.report, EXIT_OK if not issues else EXIT_PREFLIGHT)


def mode_apply(args, started):
    try:
        config = target_config()
    except Exception as exc:
        print(json.dumps({"status": "config-error", "detail": str(exc)}, ensure_ascii=False))
        return EXIT_USAGE

    try:
        info, issues = preflight_source(args.source, expected_sha256=args.source_sha256)
    except PreflightFailure as exc:
        manifest = new_report("apply", args.target_purpose, "preflight-failed", started,
                              result={"mismatches": exc.issues})
        return _emit(manifest, args.report, EXIT_PREFLIGHT)
    if issues:
        manifest = new_report("apply", args.target_purpose, "preflight-failed", started,
                              info=info, result={"mismatches": issues})
        return _emit(manifest, args.report, EXIT_PREFLIGHT)
    source_sha = info["sha256"]

    conn = connect_target(config, statement_timeout=args.statement_timeout)
    try:
        try:
            acquire_migration_lock(conn, timeout_s=args.lock_timeout)
        except TargetGuardFailure as exc:
            return _emit(_guard_report(args, started, info, str(exc)), args.report, EXIT_TARGET)
        try:
            schema_report = require_schema12(conn)
            identity = target_identity(conn)
            credential = read_credential(conn)
            if credential:
                if credential.get("source_sha256") != source_sha:
                    raise TargetGuardFailure(
                        "目标已有不同来源的完成凭据，拒绝覆盖；先人工核查（§5.3.7）"
                    )
                if credential.get("target_identity", {}).get("database") != identity["database"]:
                    raise TargetGuardFailure("完成凭据的目标身份与当前连接不符")
                counts = business_table_counts(conn)
                result = {
                    "target_identity": identity,
                    "target_schema_version": schema_report["schema_version"],
                    "target_schema_checksum": schema_report["checksum"],
                    "tables": [{"table": table, "action": "already-completed",
                                "source_rows": info["tables"].get(table, {}).get("source_rows"),
                                "target_rows": counts.get(table)}
                               for table in TABLE_MAP if table != "schema_migrations"],
                    "counts": counts,
                    "key_ranges": None,
                    "amount_summary": None,
                    "mismatches": [],
                }
                manifest = new_report("apply", args.target_purpose, "already-completed",
                                      started, info=info, result=result)
                return _emit(manifest, args.report, EXIT_OK,
                             started_status_note={"credential_completed_at":
                                                  credential.get("completed_at")})

            nonempty = {table: count for table, count in business_table_counts(conn).items()
                        if count}
            if nonempty:
                raise TargetGuardFailure(
                    f"目标业务表非空且无完成凭据：{nonempty}；拒绝覆盖（不使用 TRUNCATE/DROP）"
                )

            source_conn = sqlite_legacy.open_legacy_source(str(pathlib.Path(args.source).expanduser()))
            try:
                credential = _apply_transaction(conn, source_conn, args, info, source_sha,
                                                identity)
            finally:
                source_conn.close()

            post_issues = post_commit_check(args.source, source_sha)
            if post_issues:
                manifest = new_report("apply", args.target_purpose, "post-commit-mismatch",
                                      started, info=info,
                                      result={"mismatches": post_issues,
                                              "target_identity": identity,
                                              "target_schema_version":
                                                  schema_report["schema_version"],
                                              "target_schema_checksum":
                                                  schema_report["checksum"]})
                return _emit(manifest, args.report, EXIT_MISMATCH)

            comparison = _readonly_comparison(conn, args.source)
            result = _apply_result(info, identity, schema_report, comparison)
            clean = not comparison["mismatches"]
            manifest = new_report("apply", args.target_purpose,
                                  "ok" if clean else "migrated-unverified",
                                  started, info=info, result=result)
            return _emit(manifest, args.report, EXIT_OK if clean else EXIT_MISMATCH)
        finally:
            release_migration_lock(conn)
    except PreflightFailure as exc:
        return _emit(_preflight_report(args, started, exc), args.report, EXIT_PREFLIGHT)
    except TargetGuardFailure as exc:
        return _emit(_guard_report(args, started, info, str(exc)), args.report, EXIT_TARGET)
    finally:
        conn.close()


def _guard_report(args, started, info, message):
    return new_report(args.mode, args.target_purpose, "target-guard", started, info=info,
                      result={"mismatches": [{"table": None, "column": None, "count": 1,
                                              "reason": message}]})


def _preflight_report(args, started, exc):
    return new_report(args.mode, args.target_purpose, "preflight-failed", started,
                      result={"mismatches": exc.issues})


def _apply_transaction(conn, source_conn, args, info, source_sha, identity):
    """单受控事务：全部业务表 + 旧迁移历史 + sequence + 完成凭据（§5.3.3/5/6）。"""
    present_source_tables = set(sqlite_legacy.table_names(source_conn))
    with conn.transaction():
        copies = {}
        for table in TABLE_MAP:
            if table == "schema_migrations":
                continue
            if table not in present_source_tables:
                if table in SOURCE_OPTIONAL_TABLES:
                    copies[table] = {"source_rows": 0, "row_hash": "absent-from-source",
                                     "key_min": None, "key_max": None}
                    continue
                raise PreflightFailure([{"kind": "missing-source-table", "table": table}])
            copies[table] = copy_table_data(source_conn, conn, table)
        append_legacy_migrations(source_conn, conn)
        restore_sequences(info, source_conn, conn)
        credential = {
            "format": CREDENTIAL_FORMAT,
            "tool_version": TOOL_VERSION,
            "source_sha256": source_sha,
            "source_schema_versions": info["schema_versions"],
            "target_identity": identity,
            "target_purpose": args.target_purpose,
            "counts": {table: data["source_rows"] for table, data in copies.items()},
            "row_hashes": {table: data["row_hash"] for table, data in copies.items()},
            "amount_summary": {
                table: {"row_hash": info["tables"][table]["row_hash"]}
                for table in ("ledger_tx", "renewals") if table in info["tables"]
            },
            "completed_at": utc_now_text(),
        }
        write_credential(conn, credential)
        return credential


def post_commit_check(source_path, expected_sha):
    expanded = str(pathlib.Path(source_path).expanduser())
    if file_sha256(expanded) != expected_sha:
        return [{"table": None, "column": None, "count": 1,
                 "reason": "source-file-changed-during-apply"}]
    return wal_issues(expanded)


def _readonly_comparison(conn, source_path):
    conn.rollback()  # 结束 apply 事务会话，另起只读事务重新读
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        return compare_source_target(source_path, conn)


def _apply_result(info, identity, schema_report, comparison):
    return {
        "target_identity": identity,
        "target_schema_version": schema_report["schema_version"],
        "target_schema_checksum": schema_report["checksum"],
        "tables": comparison["tables"],
        "counts": comparison["counts"],
        "key_ranges": comparison["key_ranges"],
        "amount_summary": comparison["amount_summary"],
        "mismatches": comparison["mismatches"],
    }


def mode_verify(args, started):
    try:
        config = target_config()
    except Exception as exc:
        print(json.dumps({"status": "config-error", "detail": str(exc)}, ensure_ascii=False))
        return EXIT_USAGE
    try:
        info, issues = preflight_source(args.source, expected_sha256=args.source_sha256)
    except PreflightFailure as exc:
        return _emit(_preflight_report(args, started, exc), args.report, EXIT_PREFLIGHT)
    if issues:
        manifest = new_report("verify", args.target_purpose, "preflight-failed", started,
                              info=info, result={"mismatches": issues})
        return _emit(manifest, args.report, EXIT_PREFLIGHT)

    conn = connect_target(config, statement_timeout=args.statement_timeout)
    try:
        schema_report = require_schema12(conn)
        comparison = _readonly_comparison(conn, args.source)
        identity = target_identity(conn)
        mismatches = list(comparison["mismatches"])
        credential = read_credential(conn)
        if not (credential and credential.get("source_sha256") == info["sha256"]):
            mismatches.append({"table": "schema_migrations", "column": "description",
                               "count": 1,
                               "reason": "completion-credential-missing-or-mismatched"})
        result = {
            "target_identity": identity,
            "target_schema_version": schema_report["schema_version"],
            "target_schema_checksum": schema_report["checksum"],
            "tables": comparison["tables"],
            "counts": comparison["counts"],
            "key_ranges": comparison["key_ranges"],
            "amount_summary": comparison["amount_summary"],
            "mismatches": mismatches,
        }
        clean = not mismatches
        manifest = new_report("verify", args.target_purpose,
                              "ok" if clean else "mismatch", started, info=info, result=result)
        return _emit(manifest, args.report, EXIT_OK if clean else EXIT_MISMATCH)
    except TargetGuardFailure as exc:
        return _emit(_guard_report(args, started, info, str(exc)), args.report, EXIT_TARGET)
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", required=True, choices=pg_contracts.MIGRATION_MODES)
    parser.add_argument("--source", required=True, help="SQLite 离线一致性快照路径")
    parser.add_argument("--source-sha256", help="预期待迁源指纹；apply 必填")
    parser.add_argument("--target-purpose", required=True,
                        choices=pg_contracts.TARGET_PURPOSES)
    parser.add_argument("--report", help="报告 manifest JSON 落盘路径（原子发布）")
    parser.add_argument("--lock-timeout", type=int, default=10)
    parser.add_argument("--statement-timeout", type=int, default=600)
    args = parser.parse_args(argv)
    started = utc_now_text()
    if args.mode == "apply" and not args.source_sha256:
        print(json.dumps({"status": "usage-error",
                          "detail": "apply 必须提供 --source-sha256（§5.3）"}, ensure_ascii=False))
        return EXIT_USAGE
    try:
        if args.mode == "preflight":
            return mode_preflight(args, started)
        if args.mode == "apply":
            return mode_apply(args, started)
        return mode_verify(args, started)
    except Exception as exc:
        text = str(exc)
        if "password" in text.lower():
            text = type(exc).__name__
        print(json.dumps({"status": "error", "detail": f"{type(exc).__name__}: {text}"},
                         ensure_ascii=False))
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
