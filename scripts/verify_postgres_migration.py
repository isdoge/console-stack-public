#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`scripts/verify_postgres_migration.py --source --target --report`（提案 §8.2/§8.5；PG-03）。

**独立只读比对器**：直接重新读取 SQLite 源快照与 PG 目标，逐字段、金额（规范十进制）
与分组汇总比较；不 import 搬运器流程、不读它留下的报告或任何结果缓存——搬运器的
PASS 在这里不构成任何证据（§8.2）。比较语义（canonical 编码）与 `money` 契约共用是
刻意的，防「共享同一错误」由测试中手工预先算好的金额样例承担（§8.5）。

目标连接 `--target`：
- `env`：读 `CONSOLE_PG_*` 环境（推荐；密码不上命令行）；
- 或 psycopg conninfo（`host=... dbname=... user=...`，密码走 `PGPASSWORD` 环境变量）。

退出码：0 一致 · 2 用法/配置 · 3 源不可用/preflight 形态失败 · 5 不一致（报告列不一致
列名与数量，不打印私人行值）。
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

from pg_migration_lib import (  # noqa: E402
    PreflightFailure,
    TargetGuardFailure,
    compare_source_target,
    connect_target,
    new_report,
    preflight_source,
    publish_report,
    require_schema12,
    target_config,
    utc_now_text,
)

EXIT_OK, EXIT_USAGE, EXIT_PREFLIGHT, EXIT_MISMATCH = 0, 2, 3, 5


def _connect_target_option(target):
    if target == "env":
        return connect_target(target_config(), statement_timeout=600)
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(target, row_factory=dict_row,
                           options="-c timezone=UTC -c statement_timeout=600000",
                           connect_timeout=5)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, help="SQLite 离线快照路径")
    parser.add_argument("--target", required=True, default="env",
                        help="'env' 读 CONSOLE_PG_*；或 psycopg conninfo（密码走 PGPASSWORD）")
    parser.add_argument("--report", help="报告 manifest JSON 落盘路径（原子发布）")
    parser.add_argument("--source-sha256", help="可选：核对源指纹")
    args = parser.parse_args(argv)
    started = utc_now_text()

    try:
        info, issues = preflight_source(args.source, expected_sha256=args.source_sha256)
    except PreflightFailure as exc:
        issues = exc.issues
        info = None
    except Exception as exc:
        print(json.dumps({"status": "source-error", "detail": str(exc)}, ensure_ascii=False))
        return EXIT_PREFLIGHT
    if issues:
        manifest = new_report("verify-independent", None, "preflight-failed", started,
                              info=info, result={"mismatches": issues})
        _publish(args.report, manifest)
        print(json.dumps({"status": "preflight-failed", "exit_code": EXIT_PREFLIGHT,
                          "issues": len(issues)}, ensure_ascii=False))
        return EXIT_PREFLIGHT

    try:
        conn = _connect_target_option(args.target)
    except Exception as exc:
        text = str(exc)
        if "password" in text.lower():
            text = type(exc).__name__
        print(json.dumps({"status": "target-connect-error", "detail": text},
                         ensure_ascii=False))
        return EXIT_USAGE
    try:
        try:
            schema_report = require_schema12(conn)
        except TargetGuardFailure as exc:
            manifest = new_report("verify-independent", None, "target-guard", started,
                                  info=info,
                                  result={"mismatches": [{"table": None, "column": None,
                                                          "count": 1, "reason": str(exc)}]})
            _publish(args.report, manifest)
            print(json.dumps({"status": "target-guard", "detail": str(exc)},
                             ensure_ascii=False))
            return EXIT_MISMATCH
        conn.rollback()
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            comparison = compare_source_target(args.source, conn)
        result = {
            "target_identity": {"database": conn.info.dbname},
            "target_schema_version": schema_report["schema_version"],
            "target_schema_checksum": schema_report["checksum"],
            "tables": comparison["tables"],
            "counts": comparison["counts"],
            "key_ranges": comparison["key_ranges"],
            "amount_summary": comparison["amount_summary"],
            "mismatches": comparison["mismatches"],
        }
        clean = not result["mismatches"]
        manifest = new_report("verify-independent", None,
                              "ok" if clean else "mismatch", started, info=info, result=result)
        _publish(args.report, manifest)
        print(json.dumps({"status": manifest["status"],
                          "exit_code": EXIT_OK if clean else EXIT_MISMATCH,
                          "mismatch_entries": len(result["mismatches"]),
                          "counts": result["counts"]}, ensure_ascii=False))
        return EXIT_OK if clean else EXIT_MISMATCH
    finally:
        conn.close()


def _publish(report_path, manifest):
    if report_path:
        publish_report(report_path, manifest)


if __name__ == "__main__":
    sys.exit(main())
