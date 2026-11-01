# -*- coding: utf-8 -*-
"""`scripts/migrate_sqlite_to_postgres.py` 测试：preflight/apply/verify、幂等、保护、回滚（PG-03）。"""
import importlib.util
import json
import pathlib
import sqlite3
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pg_contracts  # noqa: E402
import pg_migrations  # noqa: E402
from pg03_test_fixtures import (  # noqa: E402
    drop_database,
    fresh_database,
    make_legacy_db,
    pg_env,
    run_cli,
)
from pg_migration_lib import (  # noqa: E402
    PreflightFailure,
    append_legacy_migrations,
    file_sha256,
)
from pg_test_support import connect  # noqa: E402

MIGRATE = "migrate_sqlite_to_postgres.py"


@pytest.fixture()
def source(tmp_path):
    return make_legacy_db(tmp_path / "console.db")


@pytest.fixture()
def fresh_db(pg_container, pg_dsn):
    admin = connect(pg_dsn, autocommit=True)
    name = fresh_database(admin)
    yield name
    drop_database(admin, name)
    admin.close()


@pytest.fixture()
def env_for(fresh_db, pg_container):
    return pg_env(pg_container, fresh_db)


@pytest.fixture()
def prepared_schema(env_for):
    proc = run_cli("pg_schema.py", ["migrate"], env_for)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _summary(proc):
    return json.loads(proc.stdout)


def dsn_from_env(env):
    """从夹具环境变量拼 libpq DSN（隔离容器路径，无外部 DSN）。"""
    return (
        f"host={env['CONSOLE_PG_HOST']} port={env['CONSOLE_PG_PORT']} "
        f"dbname={env['CONSOLE_PG_DB']} user={env['CONSOLE_PG_USER']} "
        f"password={env['CONSOLE_PG_PASSWORD']}"
    )


# --- preflight ---------------------------------------------------------------


def test_preflight_ok(env_for, source):
    proc = run_cli(MIGRATE, ["--mode", "preflight", "--source", source,
                             "--target-purpose", "rehearsal"], env_for)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _summary(proc)["status"] == "ok"


def test_preflight_emits_full_report_manifest(env_for, source, tmp_path):
    report = tmp_path / "report.json"
    proc = run_cli(MIGRATE, ["--mode", "preflight", "--source", source,
                             "--target-purpose", "rehearsal", "--report", str(report)],
                   env_for)
    assert proc.returncode == 0
    manifest = json.loads(report.read_text(encoding="utf-8"))
    assert set(manifest) == set(pg_contracts.MIGRATION_REPORT_KEYS)
    assert manifest["manifest_version"] == pg_contracts.MIGRATION_REPORT_MANIFEST_VERSION
    assert manifest["source_sha256"] == file_sha256(source)
    assert manifest["target_engine"] == "postgres"
    assert "password" not in report.read_text(encoding="utf-8").lower()


def test_preflight_rejects_wrong_sha(env_for, source):
    proc = run_cli(MIGRATE, ["--mode", "preflight", "--source", source,
                             "--source-sha256", "b" * 64, "--target-purpose", "rehearsal"],
                   env_for)
    assert proc.returncode == 3
    reasons = [m["kind"] for m in _summary(proc)["mismatches"]]
    assert "sha256-mismatch" in reasons


def test_preflight_rejects_nonempty_wal(env_for, source):
    pathlib.Path(source + "-wal").write_bytes(b"x" * 4096)
    try:
        proc = run_cli(MIGRATE, ["--mode", "preflight", "--source", source,
                                 "--target-purpose", "rehearsal"], env_for)
        assert proc.returncode == 3
        assert "non-empty-wal" in [m["kind"] for m in _summary(proc)["mismatches"]]
    finally:
        pathlib.Path(source + "-wal").unlink()


def _failing_preflight(env_for, tmp_path, mutate, expect_kind):
    path = make_legacy_db(tmp_path / f"bad-{expect_kind}.db")
    conn = sqlite3.connect(path)
    mutate(conn)
    conn.commit()
    conn.close()
    proc = run_cli(MIGRATE, ["--mode", "preflight", "--source", str(path),
                             "--target-purpose", "rehearsal"], env_for)
    assert proc.returncode == 3
    issues = _summary(proc)["mismatches"]
    matched = [m for m in issues if m["kind"] == expect_kind]
    assert matched, issues
    return matched


def test_preflight_rejects_text_amount(env_for, tmp_path):
    hits = _failing_preflight(
        env_for, tmp_path,
        lambda c: c.execute("UPDATE ledger_tx SET amount = '不是数' WHERE id = -5"),
        "amount-anomaly")
    assert hits[0]["table"] == "ledger_tx" and hits[0]["column"] == "amount"


def test_preflight_rejects_dangling_link(env_for, tmp_path):
    _failing_preflight(
        env_for, tmp_path,
        lambda c: c.execute("UPDATE ledger_tx_link SET a = 999999 WHERE b = 1"),
        "dangling-link")


def test_preflight_rejects_unknown_table(env_for, tmp_path):
    _failing_preflight(
        env_for, tmp_path,
        lambda c: c.execute("CREATE TABLE mystery (x INTEGER)"),
        "unknown-source-tables")


def test_preflight_rejects_unparseable_json_column(env_for, tmp_path):
    _failing_preflight(
        env_for, tmp_path,
        lambda c: c.execute("UPDATE bookmarks SET tags = '{坏JSON' WHERE id = 'b1'"),
        "json-unparseable")


def test_preflight_rejects_unsafe_integer_id(env_for, tmp_path):
    _failing_preflight(
        env_for, tmp_path,
        lambda c: c.execute("UPDATE ledger_tx SET id = 9007199254740993 WHERE id = 7"),
        "unsafe-integer-id")


# --- apply 保护路径 ------------------------------------------------------------


def test_apply_requires_source_sha(env_for, source, prepared_schema):
    proc = run_cli(MIGRATE, ["--mode", "apply", "--source", source,
                             "--target-purpose", "rehearsal"], env_for)
    assert proc.returncode == 2


def test_apply_refuses_schema_not_initialized(env_for, source):
    sha = file_sha256(source)
    proc = run_cli(MIGRATE, ["--mode", "apply", "--source", source,
                             "--source-sha256", sha, "--target-purpose", "rehearsal"],
                   env_for)
    assert proc.returncode == 4
    assert "pg_schema.py migrate" in _summary(proc)["mismatches"][0]["reason"]


def test_apply_refuses_nonempty_target(env_for, source, prepared_schema, fresh_db, pg_container):
    admin = connect(pg_container.dsn(dbname=fresh_db), autocommit=True)
    admin.execute("INSERT INTO probe_history (machine, ts, status) VALUES ('x','y','up')")
    admin.close()
    proc = run_cli(MIGRATE, ["--mode", "apply", "--source", source,
                             "--source-sha256", file_sha256(source),
                             "--target-purpose", "rehearsal"], env_for)
    assert proc.returncode == 4
    assert "非空" in _summary(proc)["mismatches"][0]["reason"]


# --- apply 成功路径与幂等 --------------------------------------------------------


def test_apply_end_to_end_and_sequence_high_water(env_for, source, prepared_schema,
                                                  fresh_db, pg_container, tmp_path):
    report = tmp_path / "apply.json"
    proc = run_cli(MIGRATE, ["--mode", "apply", "--source", source,
                             "--source-sha256", file_sha256(source),
                             "--target-purpose", "rehearsal", "--report", str(report)],
                   env_for)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    manifest = json.loads(report.read_text(encoding="utf-8"))
    assert manifest["status"] == "ok"
    assert manifest["counts"]["ledger_tx"] == {"source": 8, "target": 8}
    assert manifest["counts"]["probe_history"] == {"source": 2, "target": 2}
    # 源不存在的历史表如实标注，不伪报行数
    panels = [t for t in manifest["tables"] if t["table"] == "panels"]
    assert panels[0]["action"] == "absent-from-source"

    target = connect(pg_container.dsn(dbname=fresh_db))
    try:
        # 高水位：probe_history 的 sqlite_sequence=3 但 max(id)=2 → 下一次必须是 4
        row = target.execute(
            "INSERT INTO probe_history (machine, ts, status) "
            "VALUES ('next','2026-09-10 00:00:00','up') RETURNING id").fetchone()
        assert row["id"] == 4
        # ledger_tx 无 sqlite_sequence：max(id)=7 → 下一次 8
        row = target.execute(
            "INSERT INTO ledger_tx (month, direction) VALUES ('2026-10','x') "
            "RETURNING id").fetchone()
        assert row["id"] == 8
        versions = {r["version"] for r in target.execute(
            "SELECT version FROM schema_migrations").fetchall()}
        assert versions == set(range(1, pg_migrations.latest_schema_version() + 1))
        empty = target.execute(
            "SELECT COUNT(*) AS c FROM ledger_file_batches").fetchone()
        assert empty["c"] == 0
        target.rollback()
    finally:
        target.close()


def test_apply_twice_is_idempotent_and_preserves_edits(env_for, source, prepared_schema,
                                                       fresh_db, pg_container):
    sha = file_sha256(source)
    args = ["--mode", "apply", "--source", source, "--source-sha256", sha,
            "--target-purpose", "rehearsal"]
    assert run_cli(MIGRATE, args, env_for).returncode == 0
    admin = connect(pg_container.dsn(dbname=fresh_db), autocommit=True)
    admin.execute("UPDATE ledger_tx SET note = '迁移后的新编辑' WHERE id = 1")
    admin.close()
    second = run_cli(MIGRATE, args, env_for)
    assert second.returncode == 0
    assert _summary(second)["status"] == "already-completed"
    check = connect(pg_container.dsn(dbname=fresh_db))
    try:
        note = check.execute("SELECT note FROM ledger_tx WHERE id = 1").fetchone()["note"]
        assert note == "迁移后的新编辑"  # 不重灌、不覆盖
    finally:
        check.close()


def test_apply_with_different_source_sha_is_refused(env_for, source, prepared_schema):
    sha = file_sha256(source)
    assert run_cli(MIGRATE, ["--mode", "apply", "--source", source,
                             "--source-sha256", sha,
                             "--target-purpose", "rehearsal"], env_for).returncode == 0
    # 换源（不同内容→不同 SHA）打到同一目标 → 凭据冲突拒绝。
    # 注意必须真实改内容：同秒内两份相同数据的库字节完全一致（datetime('now') 默认值）。
    other = make_legacy_db(pathlib.Path(source).parent / "other.db")
    conn = sqlite3.connect(other)
    conn.execute("UPDATE renewals SET note = '另一来源' WHERE id = 'r1'")
    conn.commit()
    conn.close()
    assert file_sha256(other) != sha
    proc = run_cli(MIGRATE, ["--mode", "apply", "--source", other,
                             "--source-sha256", file_sha256(other),
                             "--target-purpose", "rehearsal"], env_for)
    assert proc.returncode == 4


def test_apply_mid_failure_rolls_back_then_succeeds(env_for, source, prepared_schema,
                                                   fresh_db, pg_container, monkeypatch):
    """中途异常 → 事务回滚、目标仍空；重跑 apply 成功（§5.3 失败恢复）。"""
    spec = importlib.util.spec_from_file_location(
        "mig_under_test", pathlib.Path(__file__).resolve().parent / MIGRATE)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    original = mig.copy_table_data
    calls = {"n": 0}

    def flaky(source_conn, target_conn, table, rows=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("注入：第二张表搬运中途失败")
        return original(source_conn, target_conn, table, rows=rows)

    monkeypatch.setattr(mig, "copy_table_data", flaky)
    for key, value in env_for.items():
        if key.startswith("CONSOLE_"):
            monkeypatch.setenv(key, value)
    args = types.SimpleNamespace(source=source, source_sha256=file_sha256(source),
                                 target_purpose="rehearsal", report=None,
                                 lock_timeout=5, statement_timeout=60, mode="apply")
    with pytest.raises(RuntimeError, match="注入"):
        mig.mode_apply(args, mig.utc_now_text())

    check = connect(pg_container.dsn(dbname=fresh_db))
    try:
        count = check.execute("SELECT COUNT(*) AS c FROM ledger_tx").fetchone()["c"]
        assert count == 0  # 单事务全回滚，不留半份数据
        desc = check.execute(
            "SELECT description FROM schema_migrations WHERE version = 12").fetchone()
        assert desc["description"] == pg_migrations.PG_BASELINE_DESCRIPTION
    finally:
        check.close()
    # 重跑成功
    proc = run_cli(MIGRATE, ["--mode", "apply", "--source", source,
                             "--source-sha256", file_sha256(source),
                             "--target-purpose", "rehearsal"], env_for)
    assert proc.returncode == 0


def test_apply_committed_but_report_fails_then_republish(env_for, source, prepared_schema,
                                                         tmp_path):
    bad_report = pathlib.Path("/dev/null/cannot-write-here/report.json")
    proc = run_cli(MIGRATE, ["--mode", "apply", "--source", source,
                             "--source-sha256", file_sha256(source),
                             "--target-purpose", "rehearsal", "--report", str(bad_report)],
                   env_for)
    assert proc.returncode == 6  # 数据已提交，报告落盘失败（§5.3.6 测试点）
    good_report = tmp_path / "republish.json"
    retry = run_cli(MIGRATE, ["--mode", "apply", "--source", source,
                              "--source-sha256", file_sha256(source),
                              "--target-purpose", "rehearsal", "--report", str(good_report)],
                    env_for)
    assert retry.returncode == 0
    assert _summary(retry)["status"] == "already-completed"
    assert good_report.exists()


def test_migrator_verify_mode_after_apply(env_for, source, prepared_schema):
    sha = file_sha256(source)
    assert run_cli(MIGRATE, ["--mode", "apply", "--source", source,
                             "--source-sha256", sha, "--target-purpose", "rehearsal"],
                   env_for).returncode == 0
    proc = run_cli(MIGRATE, ["--mode", "verify", "--source", source,
                             "--source-sha256", sha, "--target-purpose", "rehearsal"], env_for)
    assert proc.returncode == 0
    assert _summary(proc)["status"] == "ok"


# --- 迁移锁与业务写事务互斥（PG-05 review A-1 回归） --------------------------


def test_migration_lock_key_is_maintenance_key():
    """迁移锁必须与业务写事务的维护共享锁**同 key**（提案 §4.2）。

    PG-05 review A-1（blocker）：此前迁移器用自定义 key=20，与写事务的 key=1 各锁各的，
    实测两者可并发——「迁移期间冻结全部业务写者」在 PG 层不成立。这条钉死同 key 事实。
    """
    import pg_locks
    from pg_migration_lib import MIGRATE_LOCK_KEY

    assert MIGRATE_LOCK_KEY == pg_locks.KEY_MAINTENANCE == pg_locks.KEY_MAINTENANCE_SHARED


def test_migration_lock_blocks_business_write_transaction(env_for, prepared_schema,
                                                          monkeypatch):
    """迁移器持锁期间，业务写事务必须在有界超时内失败（§4.2）。

    对照 A-1 的实测反例：改 key 之前，`pg_transaction()` 在迁移锁持有期间可以正常提交。
    这里用**另一个进程**取业务写事务，证明互斥是 PG 层的、不是同进程内巧合。
    """
    import pg_locks
    import pgdb
    from pg_migration_lib import acquire_migration_lock

    holder = connect(dsn_from_env(env_for), autocommit=True)
    try:
        acquire_migration_lock(holder, timeout_s=5)
        # 业务写事务（模拟 NAS/Mac app 或 CLI）在子进程里跑：取不到维护共享锁 → 有界失败
        for key in ("CONSOLE_PG_HOST", "CONSOLE_PG_PORT", "CONSOLE_PG_DB",
                    "CONSOLE_PG_USER", "CONSOLE_PG_PASSWORD"):
            monkeypatch.setenv(key, env_for[key])
        monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
        monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "test")
        monkeypatch.setenv("CONSOLE_MAINTENANCE_MODE", "normal")
        monkeypatch.setenv("CONSOLE_PG_LOCK_TIMEOUT", "1")
        pgdb.close_pool()  # 池是进程单例：丢弃可能已按旧环境建立的池

        with pytest.raises(pgdb.StorageUnavailable):
            with pgdb.pg_transaction() as conn:
                conn.execute("SELECT 1")
        # 只读事务不取维护共享锁：冻结期间验证读取仍可用
        with pgdb.pg_transaction(readonly=True) as conn:
            assert conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
        assert pg_locks.MAINTENANCE.key == pg_locks.MAINTENANCE_SHARED.key
    finally:
        holder.close()
        pgdb.close_pool()  # 不让指向本用例库的池泄漏给后续用例


# --- G08：报告脱敏的**内容级**断言（apply / verify / 失败报告） -------------------
#
# §11.3.2 G08 要求「合成敏感标记覆盖 apply/verify/**失败报告**的内容级断言」。
# 合成源（`pg03_test_fixtures._seed`）里埋了可判读的原文标记，这里断言它们
# **一个都不许**出现在报告字节里；同时把 `key_ranges` 的值形状钉到契约
# （`pg_contracts.KEY_RANGES_VALUE_RULE`：只允许数字或 null）。
#
# 为什么必须有内容级断言之上的**结构**断言：只查标记会漏掉「文本主键被换了个马甲
# 写出去」，只查结构会漏掉「数字主键表的区间里夹带别的东西」。两者都要。

SENSITIVE_MARKERS = (
    "山田商店",    # ledger_tx.who / ledger_knowledge.who
    "空商户",      # ledger_knowledge.good
    "域名A",       # renewals.name
    "证书B",       # renewals.name
    "海外VPS",     # renewals.name
    "第二行",      # ledger_tx.goods（原文含换行）
    "备注换行",    # ledger_tx.note
    "草稿",        # panel_scratch.content
    "内网",        # bookmarks.title
)

# 文本/复合文本主键的表：key_ranges 必须是空值——旧实现把主键原文写在这里（G08 首红）。
TEXT_KEY_TABLES = (
    "renewals",          # 列名是 id，但存的是 'r1'/'r2' 文本（旧实现正是死在这张表）
    "ledger_knowledge",  # (who, good)：商户/商品原文
    "ledger_tx_link",    # (a, b)
    "machine_traffic",   # (machine)
    "dashboard_layout",  # (widget_key)
    "ledger_imports",    # (source_sha256)
    "versions",          # (svc_name)
    "bookmarks",         # id 文本
    "feed_cache",        # id 文本
)

# 数值主键的表：区间必须真实（用**手工核算**的值，不跑代码回填）
NUMERIC_KEY_RANGES = {
    "ledger_tx": {"min": -5, "max": 7},
    "probe_history": {"min": 1, "max": 2},
    "service_health_history": {"min": 10, "max": 11},
    "monitor_events": {"min": 1, "max": 2},
    "ledger_categories": {"min": 1, "max": 3},
    "oplog": {"min": 5, "max": 7},
}


def _assert_report_redacted(report_path, expect_status=None):
    text = pathlib.Path(report_path).read_text(encoding="utf-8")
    manifest = json.loads(text)
    if expect_status is not None:
        assert manifest["status"] == expect_status, manifest["status"]
    for marker in SENSITIVE_MARKERS:
        assert marker not in text, f"报告泄漏源数据原文：{marker!r}"
    ranges = manifest["key_ranges"] or {}
    for table, entry in ranges.items():
        assert set(entry) == {"min", "max"}, (table, entry)
        for bound in ("min", "max"):
            value = entry[bound]
            assert value is None or (isinstance(value, (int, float))
                                     and not isinstance(value, bool)), (table, bound, value)
    for table in TEXT_KEY_TABLES:
        assert ranges.get(table) == {"min": None, "max": None}, (table, ranges.get(table))
    for table, expected in NUMERIC_KEY_RANGES.items():
        assert ranges.get(table) == expected, (table, ranges.get(table))
    return manifest


def test_apply_report_is_redacted(env_for, source, prepared_schema, tmp_path):
    report = tmp_path / "apply-redaction.json"
    proc = run_cli(MIGRATE, ["--mode", "apply", "--source", source,
                             "--source-sha256", file_sha256(source),
                             "--target-purpose", "rehearsal", "--report", str(report)],
                   env_for)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    _assert_report_redacted(report, expect_status="ok")


def test_verify_report_is_redacted(env_for, source, prepared_schema, tmp_path):
    sha = file_sha256(source)
    assert run_cli(MIGRATE, ["--mode", "apply", "--source", source,
                             "--source-sha256", sha,
                             "--target-purpose", "rehearsal"], env_for).returncode == 0
    report = tmp_path / "verify-redaction.json"
    proc = run_cli(MIGRATE, ["--mode", "verify", "--source", source,
                             "--source-sha256", sha, "--target-purpose", "rehearsal",
                             "--report", str(report)], env_for)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    _assert_report_redacted(report, expect_status="ok")


def test_failure_report_is_redacted(env_for, source, prepared_schema, fresh_db,
                                    pg_container, tmp_path):
    """失败报告同样不得夹带原文（G08 点名失败报告；verify 不一致 = rc5/status=mismatch）。

    这条比对成功报告更严：不一致报告**带着** key_ranges 与 mismatches 落盘，
    是唯一会把「比较结果」写出去的报告形态。
    """
    sha = file_sha256(source)
    assert run_cli(MIGRATE, ["--mode", "apply", "--source", source,
                             "--source-sha256", sha,
                             "--target-purpose", "rehearsal"], env_for).returncode == 0
    probe = "TARGET-ONLY-泄漏探针"
    admin = connect(pg_container.dsn(dbname=fresh_db), autocommit=True)
    admin.execute("UPDATE ledger_tx SET who = %s WHERE id = 1", (probe,))
    admin.close()
    report = tmp_path / "verify-mismatch-redaction.json"
    proc = run_cli(MIGRATE, ["--mode", "verify", "--source", source,
                             "--source-sha256", sha, "--target-purpose", "rehearsal",
                             "--report", str(report)], env_for)
    assert proc.returncode == 5, proc.stdout + proc.stderr
    manifest = _assert_report_redacted(report, expect_status="mismatch")
    assert probe not in pathlib.Path(report).read_text(encoding="utf-8")
    # 不一致必须被**如实报出**（脱敏不等于把问题也一起藏掉）
    assert any(m["reason"] == "value-mismatch" and m["table"] == "ledger_tx"
               for m in manifest["mismatches"]), manifest["mismatches"]


# --- G07：11 条原始 applied_at 的恒等搬运与门禁 ----------------------------------
#
# 「恒等」= 源 TEXT 与目标 TEXT 逐字节相等：不 parse、不做时区换算、不用目标默认值
# （迁移时刻）顶替。下面用 2001 年的哨兵值，任何「用 now() 写目标」的实现都会立刻红。

_HISTORIC_APPLIED_AT = {v: f"2001-02-{v:02d} 03:04:05" for v in range(1, 12)}


def _doctor_source_applied_at(source, values=None):
    values = _HISTORIC_APPLIED_AT if values is None else values
    conn = sqlite3.connect(source)
    try:
        for version, value in values.items():
            conn.execute("UPDATE schema_migrations SET applied_at = ? WHERE version = ?",
                         (value, version))
        conn.commit()
    finally:
        conn.close()
    return source


def _read_applied_at_sqlite(source):
    conn = sqlite3.connect(source)
    conn.row_factory = sqlite3.Row
    try:
        return {int(r["version"]): r["applied_at"] for r in conn.execute(
            "SELECT version, applied_at FROM schema_migrations").fetchall()}
    finally:
        conn.close()


def test_apply_preserves_legacy_applied_at_verbatim(env_for, source, prepared_schema,
                                                    fresh_db, pg_container, tmp_path):
    """G07：源 1–11 的 applied_at 逐条恒等搬运到目标（不得被目标默认值顶替）。"""
    _doctor_source_applied_at(source)
    expected = _read_applied_at_sqlite(source)
    assert sorted(expected) == list(range(1, 12)), "夹具源必须是 1–11 条历史"

    report = tmp_path / "apply-applied-at.json"
    proc = run_cli(MIGRATE, ["--mode", "apply", "--source", source,
                             "--source-sha256", file_sha256(source),
                             "--target-purpose", "rehearsal", "--report", str(report)],
                   env_for)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    target = connect(pg_container.dsn(dbname=fresh_db))
    try:
        actual = {int(r["version"]): r["applied_at"] for r in target.execute(
            "SELECT version, applied_at FROM schema_migrations").fetchall()}
    finally:
        target.close()
    for version in range(1, 12):
        assert actual[version] == expected[version], f"version {version} 的 applied_at 被改写"
    # 独立反证：目标侧不是「一律写成同一个当前时刻」
    assert len({actual[v] for v in range(1, 12)}) == 11
    assert not any(str(actual[v]).startswith("20") and actual[v] == actual[12]
                   for v in range(1, 12))


def test_preflight_rejects_blank_applied_at(env_for, source):
    """G07：源 applied_at 为空文本必须 preflight 拒绝（rc=3），不留到 NOT NULL 处炸。"""
    _doctor_source_applied_at(source, {4: "   "})
    proc = run_cli(MIGRATE, ["--mode", "preflight", "--source", source,
                             "--target-purpose", "rehearsal"], env_for)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    payload = _summary(proc)
    assert payload["status"] == "failed"
    assert any(item["kind"] == "legacy-applied-at-invalid"
               and item["column"] == "applied_at" for item in payload["mismatches"]), payload


def test_append_legacy_migrations_refuses_blank_without_touching_target(source):
    """G07 兜底：preflight 若被绕过，搬运函数自身拒绝，且**不触碰**目标连接。"""
    _doctor_source_applied_at(source, {1: ""})

    class _NoTarget:
        def execute(self, *args, **kwargs):
            raise AssertionError("拒绝之前不得对目标做任何写入")

    conn = sqlite3.connect(source)
    try:
        with pytest.raises(PreflightFailure):
            append_legacy_migrations(conn, _NoTarget())
    finally:
        conn.close()


def test_apply_refuses_applied_at_drift_without_credential(env_for, source, prepared_schema,
                                                           fresh_db, pg_container):
    """G07：目标已有同版本行但 applied_at 与源不符、且无完成凭据 → rc=4 拒绝静默覆盖。"""
    _doctor_source_applied_at(source)
    admin = connect(pg_container.dsn(dbname=fresh_db), autocommit=True)
    admin.execute("INSERT INTO schema_migrations (version, applied_at, description) "
                  "VALUES (1, '1999-12-31 23:59:59', '手工塞入的旧行')")
    admin.close()
    proc = run_cli(MIGRATE, ["--mode", "apply", "--source", source,
                             "--source-sha256", file_sha256(source),
                             "--target-purpose", "rehearsal"], env_for)
    assert proc.returncode == 4, proc.stdout + proc.stderr
    payload = _summary(proc)
    assert payload["status"] == "target-guard"
    assert any("applied_at" in (item.get("reason") or "")
               for item in payload["mismatches"]), payload
