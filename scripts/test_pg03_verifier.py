# -*- coding: utf-8 -*-
"""`scripts/verify_postgres_migration.py` 独立比对器测试（PG-03）。

独立性纪律：本文件断言用的金额值是 `pg03_test_fixtures.EXPECTED_*`——**用计算器预先
算好的手工样例**，不 import 被测代码的计算结果，避免搬运/验证共享同一错误时假通过。
"""
import json
import pathlib
import sqlite3
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import money  # noqa: E402  （仅用于测试侧独立分组重算的 Decimal 口径，不依赖 lib）
from pg03_test_fixtures import (  # noqa: E402
    EXPECTED_LEDGER_GROUPS,
    EXPECTED_LEDGER_TOTAL,
    EXPECTED_RENEWAL_GROUPS,
    drop_database,
    fresh_database,
    make_legacy_db,
    pg_env,
    run_cli,
)
from pg_test_support import connect  # noqa: E402

VERIFY = "verify_postgres_migration.py"
MIGRATE = "migrate_sqlite_to_postgres.py"
SCHEMA = "pg_schema.py"


@pytest.fixture()
def source(tmp_path):
    return make_legacy_db(tmp_path / "console.db")


@pytest.fixture()
def migrated(pg_container, pg_dsn, source, tmp_path):
    """建独立库 → pg_schema migrate → apply → 返回 (env, dbname, report_path)。"""
    admin = connect(pg_dsn, autocommit=True)
    name = fresh_database(admin)
    env = pg_env(pg_container, name)
    assert run_cli(SCHEMA, ["migrate"], env).returncode == 0
    report = tmp_path / "apply-report.json"
    proc = run_cli(MIGRATE, ["--mode", "apply", "--source", source,
                             "--source-sha256", _sha(source),
                             "--target-purpose", "rehearsal", "--report", str(report)],
                   env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    yield env, name, report
    drop_database(admin, name)
    admin.close()


def _sha(path):
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_verify_clean_migration_exits_zero(migrated, source):
    env, _dbname, _report = migrated
    proc = run_cli(VERIFY, ["--source", source, "--target", "env"], env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "ok"


def test_verify_reports_hand_computed_amount_totals(migrated, source, tmp_path):
    env, _dbname, _report = migrated
    report = tmp_path / "verify.json"
    proc = run_cli(VERIFY, ["--source", source, "--target", "env", "--report", str(report)],
                   env)
    assert proc.returncode == 0
    manifest = json.loads(report.read_text(encoding="utf-8"))
    summary = manifest["amount_summary"]
    # 手工核算的总额与分组数（独立于被测代码）
    assert summary["ledger_tx"]["decimal_total"] == EXPECTED_LEDGER_TOTAL
    assert summary["ledger_tx"]["groups"] == len(EXPECTED_LEDGER_GROUPS)
    assert summary["ledger_tx"]["binary_drift_present"] is True
    assert summary["renewals"]["decimal_total"] == "105.59"  # 99.99+0.1+5.5
    # 目标库侧独立重算分组（psycopg 原生 SUM(Decimal)），对照手工预期值
    db_name = env["CONSOLE_PG_DB"]
    container_dsn = (f"host=127.0.0.1 port={env['CONSOLE_PG_PORT']} dbname={db_name} "
                     f"user={env['CONSOLE_PG_USER']} password={env['CONSOLE_PG_PASSWORD']}")
    conn = connect(container_dsn)
    try:
        totals = {}
        for row in conn.execute(
            "SELECT month, direction, pay, type1, SUM(amount) AS total FROM ledger_tx "
            "WHERE amount IS NOT NULL GROUP BY 1,2,3,4"
        ).fetchall():
            totals[(row["month"], row["direction"], row["pay"], row["type1"])] = row["total"]
        assert set(totals) == set(EXPECTED_LEDGER_GROUPS)
        for key, expected in EXPECTED_LEDGER_GROUPS.items():
            assert money.canonical(totals[key]) == expected, key
        renewal_totals = {
            row["currency"]: row["total"] for row in conn.execute(
                "SELECT currency, SUM(cost) AS total FROM renewals "
                "WHERE cost IS NOT NULL GROUP BY currency").fetchall()}
        for key, expected in EXPECTED_RENEWAL_GROUPS.items():
            assert money.canonical(renewal_totals[key]) == expected
    finally:
        conn.close()


def test_verify_detects_tampered_cell(migrated, source):
    env, dbname, _report = migrated
    conn = connect(_dsn(env, dbname), autocommit=True)
    conn.execute("UPDATE ledger_tx SET note = '被动过' WHERE id = 1")
    conn.close()
    proc = run_cli(VERIFY, ["--source", source, "--target", "env"], env)
    assert proc.returncode == 5
    payload = json.loads(proc.stdout)
    assert payload["status"] == "mismatch"
    assert payload["mismatch_entries"] >= 1


def test_verify_detects_missing_and_extra_rows(migrated, source):
    env, dbname, _report = migrated
    conn = connect(_dsn(env, dbname), autocommit=True)
    conn.execute("DELETE FROM ledger_tx WHERE id = 2")
    conn.close()
    proc = run_cli(VERIFY, ["--source", source, "--target", "env"], env)
    assert proc.returncode == 5


def test_verify_accepts_numeric_scale_rescaling(migrated, source):
    """NUMERIC 存成 0.100 与源 0.1 数学值相同 → canonical 口径必须判一致（§5.4）。"""
    env, dbname, _report = migrated
    conn = connect(_dsn(env, dbname), autocommit=True)
    conn.execute("UPDATE ledger_tx SET amount = '0.100'::numeric WHERE id = -5")
    conn.close()
    proc = run_cli(VERIFY, ["--source", source, "--target", "env"], env)
    assert proc.returncode == 0, proc.stdout


def test_verify_requires_initialized_target(migrated, source):
    env, _dbname, _report = migrated
    empty_env = dict(env)
    empty_env["CONSOLE_PG_DB"] = "pg03-nothing"
    proc = run_cli(VERIFY, ["--source", source, "--target", "env"], empty_env)
    assert proc.returncode in (2, 5)  # 连不上或 schema 未初始化，都不许假通过


def _dsn(env, dbname):
    return (f"host=127.0.0.1 port={env['CONSOLE_PG_PORT']} dbname={dbname} "
            f"user={env['CONSOLE_PG_USER']} password={env['CONSOLE_PG_PASSWORD']}")


def test_verify_accepts_conninfo_target(migrated, source):
    env, dbname, _report = migrated
    proc = run_cli(VERIFY, ["--source", source, "--target", _dsn(env, dbname)], env)
    assert proc.returncode == 0
    assert env["CONSOLE_PG_PASSWORD"] not in proc.stdout  # 不回显 DSN


def test_migrator_verify_requires_credential_but_content_verifier_does_not(
        migrated, source):
    """内容一致但完成凭据被抹掉：搬运器 verify 拒绝，独立比对器仍按内容判定。"""
    env, dbname, _report = migrated
    conn = connect(_dsn(env, dbname), autocommit=True)
    conn.execute("UPDATE schema_migrations SET description = '被抹掉的凭据' WHERE version = 12")
    conn.close()
    migrator = run_cli(MIGRATE, ["--mode", "verify", "--source", source,
                                 "--source-sha256", _sha(source),
                                 "--target-purpose", "rehearsal"], env)
    assert migrator.returncode == 5
    independent = run_cli(VERIFY, ["--source", source, "--target", "env"], env)
    assert independent.returncode == 0


# --- G07：独立比对器按三列（version/applied_at/description）核 11 条历史 -------------


def test_verify_detects_applied_at_drift(migrated, source, tmp_path):
    """G07：篡改一条 applied_at，独立比对器必须报出（且指名 applied_at 列）。"""
    env, dbname, _report = migrated
    sentinel = "1999-01-01 00:00:00"
    conn = connect(_dsn(env, dbname), autocommit=True)
    conn.execute("UPDATE schema_migrations SET applied_at = %s WHERE version = 5", (sentinel,))
    conn.close()

    report = tmp_path / "verify-applied-at.json"
    proc = run_cli(VERIFY, ["--source", source, "--target", "env", "--report", str(report)],
                   env)
    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["status"] == "mismatch"
    manifest = json.loads(pathlib.Path(report).read_text(encoding="utf-8"))
    entry = [m for m in manifest["mismatches"]
             if m["table"] == "schema_migrations" and m.get("column") == "applied_at"]
    assert entry and entry[0]["reason"] == "value-mismatch", manifest["mismatches"]
    assert sentinel not in pathlib.Path(report).read_text(encoding="utf-8")  # 值不外泄


def test_verify_accepts_verbatim_applied_at_after_apply(pg_container, pg_dsn, source):
    """G07 正对照：源改成 2001 年哨兵值后搬运，独立比对器必须零 mismatch。

    与 G07 反例成对：**不能靠「源与目标恰好同一秒」蒙混**——哨兵值与迁移时刻相隔 25 年，
    只有真正恒等搬运才能让独立比对器判绿。
    """
    sqlite_conn = sqlite3.connect(source)
    try:
        for version in range(1, 12):
            sqlite_conn.execute(
                "UPDATE schema_migrations SET applied_at = ? WHERE version = ?",
                (f"2001-02-{version:02d} 03:04:05", version))
        sqlite_conn.commit()
    finally:
        sqlite_conn.close()

    admin = connect(pg_dsn, autocommit=True)
    name = fresh_database(admin)
    try:
        env = pg_env(pg_container, name)
        assert run_cli(SCHEMA, ["migrate"], env).returncode == 0
        applied = run_cli(MIGRATE, ["--mode", "apply", "--source", source,
                                    "--source-sha256", _sha(source),
                                    "--target-purpose", "rehearsal"], env)
        assert applied.returncode == 0, applied.stdout + applied.stderr
        proc = run_cli(VERIFY, ["--source", source, "--target", "env"], env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
    finally:
        drop_database(admin, name)
        admin.close()
