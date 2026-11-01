#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backup_db.py PG 路径自测（PG-04）。

覆盖：
- 真 PG16 双容器端到端：schema12 初始化 → pg_dump -Fc → 侧车 manifest →
  恢复到第二个隔离容器 → 独立比对（表清单/schema 版本）。
- dump 失败路径：命令失败/产物为空时保留旧 dump、不发布 manifest。
- 孤立批次核查的脱敏纯函数用例（不依赖 PG-01 的目录布局）。
- retention 覆盖 PG dump 与 manifest；status.json 的 engine 字段。

跑法：`python3 -m pytest scripts/test_backup_db_pg.py -q`。
支持 Docker 或 CONSOLE_PG_TEST_NATIVE_BIN + 原生 PG CLI；缺底座直接失败，不伪 skip。
测试连接目标全部经 pg_test_support 的 assert_not_production 闸门。
"""
import datetime
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "console" / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backup_db as bkp  # noqa: E402
import pg_contracts  # noqa: E402
import pg_migration_lib  # noqa: E402
import pg_migrations  # noqa: E402
import pg_test_support as support  # noqa: E402


def _pg_tools_available():
    if os.environ.get("CONSOLE_PG_TEST_NATIVE_BIN"):
        return all(shutil.which(name) for name in ("psql", "pg_dump", "pg_restore"))
    return support.docker_available()


def _start_pg16():
    return support.start_pg16()


def _native_cli(container, tool, dbname=None):
    return [tool, "-h", container.host, "-p", str(container.port), "-U", container.user,
            "-d", dbname or container.dbname]


def _container_dump_cmd(container):
    if container.name == "native":
        return [*_native_cli(container, "pg_dump"), "-Fc"]
    return ["docker", "exec", container.name, "pg_dump", "-U", container.user,
            "-Fc", container.dbname]


def _container_list_cmd(container):
    if container.name == "native":
        return ["pg_restore", "--list"]
    return ["docker", "exec", "-i", container.name, "pg_restore", "--list"]


def _container_psql(container):
    if container.name == "native":
        return [*_native_cli(container, "psql"), "-X", "-v", "ON_ERROR_STOP=1", "-Atc"]
    return ["docker", "exec", container.name, "psql", "-U", container.user,
            "-d", container.dbname, "-Atc"]


def _psql(container, sql, timeout=60):
    result = support._run([*_container_psql(container), sql], timeout=timeout)
    return (result.stdout or "").strip()


def _boot_schema12(container):
    """在夹具容器上初始化真实 schema12 基线（与生产同一 DDL）。"""
    import psycopg

    with psycopg.connect(container.dsn(), autocommit=True) as conn:
        pg_migrations.ensure_migrations_table(conn)
        pg_migrations.apply(conn)


def _fake_dump_stream(text):
    """返回伪 dump 命令工厂：stdout 输出 text 后按 rc 退出。"""
    return {
        "dump": ["python3", "-c", f"import sys; sys.stdout.write({text!r}); sys.exit(0)"],
    }


def _pg_commands(container):
    """隔离容器上的完整命令组（与 `default_pg_commands` 同形）。"""
    return {
        "dump": _container_dump_cmd(container),
        "list": _container_list_cmd(container),
        "psql": _container_psql(container),
        "server_version": [*_container_psql(container), "SHOW server_version"],
        "database": container.dbname,
    }


def _start_docker_pg16_forced():
    """强制真 docker 容器（即使设了 NATIVE_BIN）；用于证明 docker exec 命令形状。"""
    if not support.docker_available():
        raise RuntimeError("docker 不可用：docker exec 组件报告接线无法执行（不伪 skip）")
    saved = os.environ.pop("CONSOLE_PG_TEST_NATIVE_BIN", None)
    try:
        return support.start_pg16()
    finally:
        if saved is not None:
            os.environ["CONSOLE_PG_TEST_NATIVE_BIN"] = saved


def _docker_exec_commands(container):
    """生产同形：dump/psql 均为 docker exec，不含宿主 TCP。"""
    assert container.name != "native"
    return {
        "dump": ["docker", "exec", container.name, "pg_dump", "-U", container.user,
                 "-Fc", container.dbname],
        "list": ["docker", "exec", "-i", container.name, "pg_restore", "--list"],
        "psql": ["docker", "exec", container.name, "psql", "-U", container.user,
                 "-d", container.dbname, "-Atc"],
        "server_version": ["docker", "exec", container.name, "psql", "-U", container.user,
                           "-d", container.dbname, "-Atc", "SHOW server_version"],
        "database": container.dbname,
    }


def _psql_cmd(container, dbname=None):
    if container.name == "native":
        return [*_native_cli(container, "psql", dbname), "-X", "-v", "ON_ERROR_STOP=1", "-Atc"]
    return ["docker", "exec", container.name, "psql", "-U", container.user,
            "-d", dbname or container.dbname, "-Atc"]


def _boot_target_fn(target, dbname=None):
    """恢复目标：容器内某个**空库**（重复验证要各自用干净库，pg_restore 不覆盖既有对象）。"""
    def boot_target():
        if target.name == "native":
            return ([*_native_cli(target, "pg_restore", dbname), "--no-owner"],
                    _psql_cmd(target, dbname))
        return (
            ["docker", "exec", "-i", target.name, "pg_restore", "-U", target.user,
             "-d", dbname or target.dbname, "--no-owner"],
            _psql_cmd(target, dbname),
        )
    return boot_target


def test_write_pg_dump_and_verify_restore_real_pg16(tmp_path):
    """端到端：schema12 → dump → manifest → 第二容器恢复 → 独立比对。"""
    if not _pg_tools_available():
        raise RuntimeError("PG16/CLI 不可用：真 PG16 恢复验证无法执行（不伪 skip）")
    source = _start_pg16()
    target = _start_pg16()
    try:
        _boot_schema12(source)
        dest = tmp_path / f"{bkp.PG_DUMP_PREFIX}.20260910"
        commands = {
            "dump": _container_dump_cmd(source),
            "list": _container_list_cmd(source),
            "psql": _container_psql(source),
            "server_version": [*_container_psql(source), "SHOW server_version"],
            "database": source.dbname,
        }
        published, manifest_path = bkp.write_pg_dump(
            dest, commands=commands, schema_version=pg_migrations.latest_schema_version(),
        )
        assert published == dest and dest.is_file()
        manifest = bkp.read_dump_manifest(dest)
        assert manifest is not None
        assert manifest["engine"] == "postgres"
        assert manifest["schema_version"] == pg_migrations.latest_schema_version()
        assert manifest["status"] == "complete"
        assert manifest["members"][0]["bytes"] == dest.stat().st_size
        assert manifest["pg_server_version"]

        boot_target = _boot_target_fn(target)

        report = bkp.verify_restore(
            dest, boot_target=boot_target,
            expected_tables=sorted(pg_migrations.SCHEMA_TABLES),
            expected_schema_version=pg_migrations.latest_schema_version(),
        )
        assert report["restored"] is True
        assert report["schema_version"] == pg_migrations.latest_schema_version()
        assert "ledger_tx" in report["tables"] and "ledger_file_batches" in report["tables"]
    finally:
        source.stop()
        target.stop()


def test_write_pg_dump_failure_preserves_old_dump(tmp_path):
    """库身份不可读（无 psql）时**在动文件之前**失败：旧 dump/manifest 原样保留。"""
    dest = tmp_path / f"{bkp.PG_DUMP_PREFIX}.20260910"
    dest.write_bytes(b"old-dump-bytes")
    manifest_path = Path(str(dest) + bkp.MANIFEST_SUFFIX)
    manifest_path.write_text(json.dumps({"kept": True}), encoding="utf-8")

    for commands in ({"dump": ["python3", "-c", "import sys; sys.exit(3)"]},
                     _fake_dump_stream("")):
        with pytest.raises(RuntimeError):
            bkp.write_pg_dump(dest, commands=commands)
        assert dest.read_bytes() == b"old-dump-bytes"
        assert json.loads(manifest_path.read_text()) == {"kept": True}
        assert not list(tmp_path.glob("*.tmp-*"))


def test_write_pg_dump_failure_after_identity_preserves_old_dump(tmp_path):
    """真库身份齐备、dump 阶段失败：临时产物清理干净，旧组不动、不发布新 manifest。"""
    if not _pg_tools_available():
        raise RuntimeError("PG16/CLI 不可用：真 PG16 备份链路无法执行（不伪 skip）")
    source = _start_pg16()
    try:
        _boot_schema12(source)
        dest = tmp_path / f"{bkp.PG_DUMP_PREFIX}.20260910"
        dest.write_bytes(b"old-dump-bytes")
        manifest_path = Path(str(dest) + bkp.MANIFEST_SUFFIX)
        manifest_path.write_text(json.dumps({"kept": True}), encoding="utf-8")

        commands = _pg_commands(source)
        commands["dump"] = ["python3", "-c", "import sys; sys.exit(3)"]
        with pytest.raises(RuntimeError):
            bkp.write_pg_dump(dest, commands=commands)
        assert dest.read_bytes() == b"old-dump-bytes"
        assert json.loads(manifest_path.read_text()) == {"kept": True}
        assert not list(tmp_path.glob("*.tmp-*"))

        # 产物为空同样拒绝：用一个「版本答得出、dump 输出为空」的假 pg_dump
        fake = tmp_path / "pg_dump"
        fake.write_text("#!/bin/sh\n"
                        'if [ "$1" = "--version" ]; then echo "pg_dump (PostgreSQL) 16.4"; fi\n'
                        "exit 0\n", encoding="utf-8")
        fake.chmod(0o755)
        commands["dump"] = [str(fake)]
        with pytest.raises(RuntimeError, match="为空"):
            bkp.write_pg_dump(dest, commands=commands)
        assert dest.read_bytes() == b"old-dump-bytes"
        assert json.loads(manifest_path.read_text()) == {"kept": True}
        assert not list(tmp_path.glob("*.tmp-*"))
    finally:
        source.stop()


def test_write_pg_dump_rejects_bad_filename(tmp_path):
    dest = tmp_path / "backup.dump"
    with pytest.raises(ValueError, match="console.dump"):
        bkp.write_pg_dump(dest, commands={"dump": ["true"]})


def test_pg_amount_columns_match_migration_lib():
    """backup_db 的金额列白名单必须与 pg_migration_lib 全等（G05 防漂移）。

    `backup_db` 不能 import `pg_migration_lib`（runtime 控制器不捆绑它，见
    `backup_runtime.controller_sources`），所以两边靠这条断言钉住，不靠人记。
    """
    assert bkp.PG_AMOUNT_COLUMNS == pg_migration_lib.AMOUNT_COLUMNS
    assert bkp.PG_TABLES == tuple(pg_migrations.SCHEMA_TABLES)


def test_write_pg_dump_default_params_fills_required_fields(tmp_path):
    """G05 关闭标准：**不手工补参数**也要产出 §7 完整 manifest。

    首红现场（`g-repro-first-red.md` §5）：标准调用写出的 manifest 里
    schema_version / schema_checksum / pg_client_version / row_digest /
    archive_manifest_version 全为 None，`status` 仍是 `complete`。
    """
    if not _pg_tools_available():
        raise RuntimeError("PG16/CLI 不可用：真 PG16 备份链路无法执行（不伪 skip）")
    source = _start_pg16()
    try:
        _boot_schema12(source)
        # 含非 ASCII、换行与金额的行：manifest 摘要必须对它们成立，不是只数行数。
        _psql(source, "INSERT INTO ledger_tx (dedup, amount, who, goods, direction) "
                      "VALUES ('d1', 12.34, '山田商店', E'多\\n行', 'expense')")
        dest = tmp_path / f"{bkp.PG_DUMP_PREFIX}.20260911"
        bkp.write_pg_dump(dest, commands=_pg_commands(source))
        manifest = bkp.read_dump_manifest(dest)
        assert manifest is not None
        assert manifest["status"] == "complete"
        assert manifest["engine"] == "postgres"
        assert manifest["schema_version"] == pg_migrations.latest_schema_version()
        assert manifest["schema_checksum"] == pg_migrations.migration_checksums()[pg_migrations.latest_schema_version()]
        assert "pg_dump" in manifest["pg_client_version"]
        assert manifest["pg_server_version"]
        assert manifest["database_identity"] == source.dbname
        assert manifest["source_commit"]
        assert manifest["archive_manifest_version"] == pg_contracts.REFERENCE_MANIFEST_VERSION
        digest = manifest["row_digest"]
        assert digest["algorithm"] == bkp.ROW_DIGEST_ALGORITHM
        assert set(digest["tables"]) == set(pg_migrations.SCHEMA_TABLES)
        assert digest["tables"]["ledger_tx"]["rows"] == 1
        assert digest["tables"]["ledger_tx"]["amounts"]["amount"] == "12.34"
        assert digest["tables"]["renewals"]["rows"] == 0
        assert digest["digest"]
    finally:
        source.stop()


def test_verify_restore_matches_recorded_row_digest(tmp_path):
    """恢复后的库要与 manifest 记录的逐表摘要独立比对；不符必须失败。

    schema 版本与表清单都只是粗判据——空库恢复出 schema12 一样能过。这里用
    篡改过的 manifest（金额/成员哈希）证明比对真的发生。
    """
    if not _pg_tools_available():
        raise RuntimeError("PG16/CLI 不可用：真 PG16 恢复验证无法执行（不伪 skip）")
    source = _start_pg16()
    target = _start_pg16()
    try:
        _boot_schema12(source)
        _psql(source, "INSERT INTO ledger_tx (dedup, amount, who) "
                      "VALUES ('d1', 12.34, '山田商店')")
        dest = tmp_path / f"{bkp.PG_DUMP_PREFIX}.20260911"
        bkp.write_pg_dump(dest, commands=_pg_commands(source))
        manifest_path = Path(str(dest) + bkp.MANIFEST_SUFFIX)
        original = manifest_path.read_text(encoding="utf-8")
        for dbname in ("restore_a", "restore_b", "restore_c"):
            _psql(target, f"CREATE DATABASE {dbname}")

        report = bkp.verify_restore(
            dest, boot_target=_boot_target_fn(target, "restore_a"),
            expected_tables=sorted(pg_migrations.SCHEMA_TABLES),
            expected_schema_version=pg_migrations.latest_schema_version(),
        )
        assert report["restored"] is True
        assert report["row_digest_match"] is True

        # ① manifest 自称内容不同（金额少记 1 分）且 digest 同步重算——自洽的
        # 伪 manifest；恢复库内容对不上记录，逐表比对必须把它拦下来
        tampered = json.loads(original)
        tampered["row_digest"]["tables"]["ledger_tx"]["amounts"]["amount"] = "12.33"
        tampered["row_digest"]["digest"] = bkp.row_digest_of(tampered["row_digest"]["tables"])
        manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
        with pytest.raises(RuntimeError, match="金额合计"):
            bkp.verify_restore(dest, boot_target=_boot_target_fn(target, "restore_b"))

        # ①′ 只改整体 digest（逐表数据原样）→ manifest 内部自相矛盾，同样拒绝
        tampered = json.loads(original)
        tampered["row_digest"]["digest"] = "0" * 64
        manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
        with pytest.raises(RuntimeError, match="整体摘要不一致"):
            bkp.verify_restore(dest, boot_target=_boot_target_fn(target, "restore_c"))

        # ② 成员哈希被改 → dump 文件与 manifest 记录不是同一份产物，拒绝
        tampered = json.loads(original)
        tampered["members"][0]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
        with pytest.raises(RuntimeError, match="摘要"):
            bkp.verify_restore(dest, boot_target=_boot_target_fn(target, "restore_c"))
        manifest_path.write_text(original, encoding="utf-8")

        # ③ 没有有效 manifest 的 dump 不算备份：恢复入口不认
        manifest_path.unlink()
        with pytest.raises(RuntimeError, match="manifest"):
            bkp.verify_restore(dest, boot_target=_boot_target_fn(target, "restore_c"))
    finally:
        source.stop()
        target.stop()


def test_write_pg_dump_refuses_incomplete_identity(tmp_path):
    """库身份不可读（未迁移的空库）→ 拒绝发布，不留半份 dump/manifest。"""
    if not _pg_tools_available():
        raise RuntimeError("PG16/CLI 不可用：真 PG16 备份链路无法执行（不伪 skip）")
    source = _start_pg16()
    try:
        dest = tmp_path / f"{bkp.PG_DUMP_PREFIX}.20260911"
        with pytest.raises(RuntimeError, match="schema_migrations"):
            bkp.write_pg_dump(dest, commands=_pg_commands(source))
        assert not dest.exists()
        assert not Path(str(dest) + bkp.MANIFEST_SUFFIX).exists()
    finally:
        source.stop()


def test_write_pg_dump_accepts_rollback_preroll_tag(tmp_path):
    """回滚前置快照 `console.dump.preroll-<run_id>` 必须能写（G05 真实形态）。

    `backup_runtime.rollback` 停写后调 `snapshot_group(root, "preroll-" + run_id)`，
    原先的文件名闸门只认 `console.dump.YYYYMMDD`——标准回滚链在 PG 引擎下必抛
    ValueError，恢复前快照永远拿不到。
    """
    if not _pg_tools_available():
        raise RuntimeError("PG16/CLI 不可用：真 PG16 备份链路无法执行（不伪 skip）")
    source = _start_pg16()
    try:
        _boot_schema12(source)
        tag = "preroll-20260911-120000-deadbeef"
        dest = tmp_path / f"{bkp.PG_DUMP_PREFIX}.{tag}"
        bkp.write_pg_dump(dest, commands=_pg_commands(source))
        assert dest.is_file()
        assert bkp.read_dump_manifest(dest)["status"] == "complete"
        # 仍拒绝越界标签：路径分隔、上跳、空标签都不行
        for bad in ("console.dump.", "console.dump...", "console.dump.a/b",
                    "console.dump.-lead"):
            with pytest.raises(ValueError, match="console.dump"):
                bkp.write_pg_dump(tmp_path / bad, commands=_pg_commands(source))
    finally:
        source.stop()


def test_audit_orphan_batches_sanitized():
    now = datetime.datetime(2026, 9, 10, 3, 0, tzinfo=datetime.timezone.utc)
    report = bkp.audit_orphan_batches(
        db_rows={"batch-a"},
        archived=[
            {"batch_id": "batch-a", "archived_at": "2026-09-09T10:00:00+00:00", "bytes": 10},
            {"batch_id": "batch-b", "archived_at": "2026-09-09T10:00:00+00:00", "bytes": 20},
            {"batch_id": "batch-c", "archived_at": "bad-timestamp", "bytes": 30},
            {"batch_id": "batch-d", "archived_at": None, "bytes": 40},
            {"archived_at": "2026-09-09T10:00:00+00:00"},
        ],
        now=now, grace_hours=24,
    )
    assert report["checked"] == 5
    ids = {o["batch_id"] for o in report["orphans"]}
    # batch-b 年龄 17h < 24h 宽限 → 不入清单；坏时间戳/缺时间戳/缺 batch_id 一律待核验。
    assert ids == {"batch-c", "batch-d", None}
    # 超过 24h 才入清单；24h 内不算孤立
    fresh = bkp.audit_orphan_batches(
        db_rows=set(),
        archived=[{"batch_id": "b", "archived_at": "2026-09-09T10:00:00+00:00", "bytes": 20},
                  {"batch_id": "old", "archived_at": "2026-09-08T00:00:00+00:00", "bytes": 6}],
        now=now, grace_hours=24,
    )
    assert {o["batch_id"] for o in fresh["orphans"]} == {"old"}
    assert fresh["orphans"][0]["age_hours"] == 51.0


def test_orphan_report_keeps_unknown_when_psql_fails(tmp_path, monkeypatch):
    """§11.5.3：psql 非零退出是「查询失败」，不是「库内无批次」。

    修复前：只打 WARN 再以空集合继续 → 已有归档全部被标成待人工核验的孤儿（假告警）。
    修复后：保留 unknown 语义（orphans 为空 + status/reason 说明），不产出无法支撑的结论。
    """
    pay = tmp_path / "pay"
    for batch in ("b1", "b2"):
        entry = pay / "archive" / batch
        entry.mkdir(parents=True)
        (entry / "manifest.json").write_text(json.dumps(
            {"batch_id": batch, "artifacts": [{"name": "a.csv", "bytes": 3}],
             "stored_at": "2026-09-01T00:00:00+00:00"}), encoding="utf-8")
    monkeypatch.setenv("CONSOLE_PAY_DATA_DIR", str(pay))
    original = bkp.subprocess.run
    mode = {"rc": 1}

    def fake_run(args, **kwargs):
        if isinstance(args, list) and any("psql" in str(part) for part in args):
            return original(
                [sys.executable, "-c", f"import sys; sys.exit({mode['rc']})"],
                capture_output=True, text=True)
        return original(args, **kwargs)

    monkeypatch.setattr(bkp.subprocess, "run", fake_run)
    report = bkp._orphan_batch_report(db_path=tmp_path / "console.db")
    assert report["status"] == "unknown" and report["db_rows_available"] is False
    assert report["audited"] is False                   # 明确「没有判定过」
    assert report["orphans"] == []                      # 不把已有归档全判成孤儿
    assert report["checked"] == 0                       # 不得读成「查过、没有孤儿」
    assert report["observed_archives"] == 2             # 但如实报告看到几个归档
    assert "psql exit 1" in report["reason"]

    # 查询成功（真的没有批次记录）时才是「都是孤儿」的合法结论
    mode["rc"] = 0
    ok = bkp._orphan_batch_report(db_path=tmp_path / "console.db")
    assert ok["status"] == "ok" and ok["audited"] is True and ok["db_rows_available"] is True
    assert {o["batch_id"] for o in ok["orphans"]} == {"b1", "b2"}


def test_audit_orphan_batches_table_missing_means_all_pending():
    """表未建成（空集合）时一切归档都算待核验，不伪造「无归档」。"""
    report = bkp.audit_orphan_batches(
        db_rows=set(),
        archived=[{"batch_id": "x", "archived_at": "2026-08-01T00:00:00+00:00", "bytes": 1}],
        now=datetime.datetime(2026, 9, 10, tzinfo=datetime.timezone.utc),
    )
    assert report["orphans"][0]["batch_id"] == "x"


def test_archived_batches_reads_ledger_files_layout(tmp_path):
    """PG-05 集成收口：默认直接读 PG-01 的 `archive/<batch_id>/manifest.json` 布局。

    以前只认 `CONSOLE_BATCH_ARCHIVE_MANIFEST` 外部 JSON，而无人生产它 → 核查
    永远 `checked=0` 的假空结论。这里用**真实归档**（`ledger_files.publish_batch`）
    造盘，证明清单来源确实接上了归档布局。
    """
    import ledger_files

    # 与生产同形：数据目录 = 在线库同级的 `pay/`（`CONSOLE_PAY_DATA_DIR` 缺省值）
    data_dir = tmp_path / "pay"
    for batch_id, payload in (("11111111-1111-1111-1111-111111111111", b"a,b\n1,2\n"),
                              ("22222222-2222-2222-2222-222222222222", b"x,y\n3,4\n")):
        ledger_files.publish_batch(batch_id, "import", "mac", "test",
                                   [("bill.csv", payload)], data_dir=str(data_dir))
    # 半份归档（目录在、回执缺）：最该人工核验的一类，必须上报而不是跳过
    (data_dir / "archive" / "33333333-3333-3333-3333-333333333333").mkdir(parents=True)

    entries = bkp._archived_batches(tmp_path / "console.db")
    by_id = {e["batch_id"]: e for e in entries}
    assert set(by_id) == {"11111111-1111-1111-1111-111111111111",
                          "22222222-2222-2222-2222-222222222222", None}
    assert by_id["11111111-1111-1111-1111-111111111111"]["bytes"] == 8
    assert by_id["11111111-1111-1111-1111-111111111111"]["archived_at"]

    # 接上 PG 侧记录后：已记录的批次不再孤立，未记录的过了 24h 宽限即待核验
    report = bkp.audit_orphan_batches(
        db_rows={"11111111-1111-1111-1111-111111111111"},
        archived=entries,
        now=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=25),
    )
    assert report["checked"] == 3
    assert {o["batch_id"] for o in report["orphans"]} == {
        "22222222-2222-2222-2222-222222222222", None}


def test_archived_batches_missing_dir_is_warned_not_silent(tmp_path, capsys):
    """归档目录不存在 → 空清单 + WARN；与「有输入且为空」区分开。"""
    entries = bkp._archived_batches(tmp_path / "console.db")
    assert entries == []
    assert "归档目录不可读" in capsys.readouterr().err


def test_prune_covers_pg_dumps_and_manifests(tmp_path):
    keep = datetime.date.today().strftime("%Y%m%d")
    old = (datetime.date.today() - datetime.timedelta(days=30)).strftime("%Y%m%d")
    keep_dump = tmp_path / f"console.dump.{keep}"
    keep_manifest = Path(str(keep_dump) + bkp.MANIFEST_SUFFIX)
    old_dump = tmp_path / f"console.dump.{old}"
    old_manifest = Path(str(old_dump) + bkp.MANIFEST_SUFFIX)
    keep_dump.write_bytes(b"x")
    keep_manifest.write_text("{}")
    old_dump.write_bytes(b"x")
    old_manifest.write_text("{}")
    keep_pay = Path(str(keep_dump) + ".pay.tar")
    old_pay = Path(str(old_dump) + ".pay.tar")
    keep_pay.write_bytes(b"retained originals")
    old_pay.write_bytes(b"expired originals")
    deploy_tag = tmp_path / f"console.db.{keep}-010101"  # deploy 命名不被误删
    deploy_tag.write_bytes(b"x")
    removed = bkp.prune_old_backups(tmp_path, keep_days=14)
    assert removed == 1
    assert keep_dump.exists() and keep_manifest.exists()
    assert not old_dump.exists() and not old_manifest.exists()
    assert keep_pay.exists() and not old_pay.exists()
    assert (tmp_path / ".trash" / old_dump.name / old_dump.name).read_bytes() == b"x"
    assert (tmp_path / ".trash" / old_dump.name / old_pay.name).read_bytes() == b"expired originals"
    assert deploy_tag.exists()


def test_status_engine_field(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    bkp.write_status(tmp_path, ok=True, attempt="a", last_success="a", snapshot="s")
    data = json.loads((tmp_path / bkp.STATUS_NAME).read_text())
    assert data["engine"] == "postgres" and data["version"] == bkp.STATUS_VERSION
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "sqlite")
    bkp.write_status(tmp_path, ok=False, attempt="b", stage="snapshot")
    data = json.loads((tmp_path / bkp.STATUS_NAME).read_text())
    assert data["engine"] == "sqlite" and data["ok"] is False


def test_active_engine_follows_env(monkeypatch):
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    assert bkp.active_engine() == "postgres"
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "sqlite")
    assert bkp.active_engine() == "sqlite"


def test_timed_pg_backup_end_to_end(tmp_path, monkeypatch):
    """PG 引擎的 main() 全链路：dump + manifest + status(engine) + 孤立核查文件。"""
    if not _pg_tools_available():
        raise RuntimeError("PG16/CLI 不可用：真 PG16 备份链路无法执行（不伪 skip）")
    source = _start_pg16()
    try:
        _boot_schema12(source)
        backup_root = tmp_path / "timed"
        monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
        monkeypatch.setattr(bkp, "ROOT", tmp_path)
        monkeypatch.setattr(bkp, "BACKUP_ROOT", backup_root)
        monkeypatch.setattr(
            bkp, "default_pg_commands",
            lambda *a, **kw: {
                "dump": _container_dump_cmd(source),
                "list": _container_list_cmd(source),
                "psql": _container_psql(source),
                "server_version": [*_container_psql(source), "SHOW server_version"],
                "database": source.dbname,
            },
        )
        # 操作锁仍取数据目录（tmp_path 下构造），与真实行为一致。
        (tmp_path / "console" / "backend" / "data").mkdir(parents=True)
        (tmp_path / "console" / "backend" / "data" / "console.db").write_bytes(b"legacy")
        rc = bkp.main()
        assert rc == 0
        dumps = [p for p in backup_root.glob("console.dump.*")
                 if bkp._dump_name_tag(p.name) is not None
                 and not p.name.endswith((bkp.MANIFEST_SUFFIX, ".pay.tar"))]
        assert len(dumps) == 1
        assert bkp.read_dump_manifest(dumps[0]) is not None
        status = json.loads((backup_root / "status.json").read_text())
        assert status["engine"] == "postgres" and status["ok"] is True
        # 孤立核查：无归档清单输入 → checked=0、orphans=[]（真实空结论，有 PG 输入）。
        report = json.loads((backup_root / "orphan-batches.json").read_text())
        assert report["checked"] == 0 and report["orphans"] == []
    finally:
        source.stop()


def test_timed_pg_backup_keeps_failure_trace(tmp_path, monkeypatch):
    """dump 失败：非零退出 + 失败留痕（engine 字段保留）。"""
    backup_root = tmp_path / "timed"
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    monkeypatch.setattr(bkp, "ROOT", tmp_path)
    monkeypatch.setattr(bkp, "BACKUP_ROOT", backup_root)
    monkeypatch.setattr(
        bkp, "default_pg_commands",
        lambda *a, **kw: {"dump": ["python3", "-c", "import sys; sys.exit(9)"],
                          "list": ["true"], "psql": ["true"],
                          "server_version": ["true"], "database": "x"},
    )
    (tmp_path / "console" / "backend" / "data").mkdir(parents=True)
    (tmp_path / "console" / "backend" / "data" / "console.db").write_bytes(b"legacy")
    rc = bkp.main()
    assert rc == 1
    status = json.loads((backup_root / "status.json").read_text())
    assert status["ok"] is False and status["engine"] == "postgres"
    assert not list(backup_root.glob("console.dump.*"))


def test_concurrent_commit_between_digest_and_dump_restores_same_snapshot(tmp_path, monkeypatch):
    """A real writer commits after the digest query, before pg_dump imports its snapshot."""
    source, target = _start_pg16(), _start_pg16()
    try:
        _boot_schema12(source)
        _psql(source, "INSERT INTO bookmarks (id, title, url) VALUES ('before', 'before', 'https://before.invalid')")
        original = bkp.pg_content_digest
        captured = []

        def concurrent_write(psql_cmd, **kwargs):
            result = original(psql_cmd, **kwargs)
            _psql(source, "INSERT INTO bookmarks (id, title, url) VALUES ('after', 'after', 'https://after.invalid')")
            captured.append(result)
            return result

        dest = tmp_path / "console.dump.concurrent"
        with monkeypatch.context() as patch:
            patch.setattr(bkp, "pg_content_digest", concurrent_write)
            bkp.write_pg_dump(dest, commands=_pg_commands(source))
        assert captured[0]["tables"]["bookmarks"]["rows"] == 1
        assert _psql(source, "SELECT count(*) FROM bookmarks") == "2"
        report = bkp.verify_restore(dest, boot_target=_boot_target_fn(target))
        assert report["restored"] and report["row_digest_match"]
        assert _psql(target, "SELECT count(*) FROM bookmarks") == "1"
    finally:
        source.stop()
        target.stop()


def test_attachment_modes_drive_group_membership(tmp_path, monkeypatch):
    """`attachment=` 三种模式各自决定备份组成员：旧行为 / 公共无附件 / 组合适配器。

    B7-2 的关键约束：公共组合**不能**产出空 Pay 包（否则「有没有附件」不可判别），
    私人组合**必须**产出真实附件包，默认（None）保持现役私人行为不变。
    """
    if not _pg_tools_available():
        raise RuntimeError("PG16/CLI 不可用：真 PG16 附件分组无法执行（不伪 skip）")
    source = _start_pg16()
    # R06：dump 输出目录与原件根必须可分离——输出进专用 out/，原件在 pay/。
    out = tmp_path / "out"
    out.mkdir()
    try:
        _boot_schema12(source)
        commands = _pg_commands(source)

        legacy = out / f"{bkp.PG_DUMP_PREFIX}.20260911"
        bkp.write_pg_dump(legacy, commands=commands, root=tmp_path)
        legacy_manifest = bkp.read_dump_manifest(legacy)
        assert [member["path"] for member in legacy_manifest["members"]] == [
            legacy.name, legacy.name + bkp.backup_pay.SUFFIX]
        assert Path(str(legacy) + bkp.backup_pay.SUFFIX).is_file()

        public = out / f"{bkp.PG_DUMP_PREFIX}.20260912"
        bkp.write_pg_dump(public, commands=commands, attachment=False)
        public_manifest = bkp.read_dump_manifest(public)
        assert [member["path"] for member in public_manifest["members"]] == [public.name]
        assert not Path(str(public) + bkp.backup_pay.SUFFIX).exists()
        assert public_manifest["status"] == "complete"

        pay_root = tmp_path / "pay"
        pay_root.mkdir()
        monkeypatch.setenv("CONSOLE_ATTACHMENT_ADAPTER", "pay")
        monkeypatch.setenv("CONSOLE_PAY_DATA_DIR", str(pay_root))
        composed = out / f"{bkp.PG_DUMP_PREFIX}.20260913"
        bkp.write_pg_dump(composed, commands=commands, attachment=True, root=tmp_path)
        composed_manifest = bkp.read_dump_manifest(composed)
        assert [member["path"] for member in composed_manifest["members"]] == [
            composed.name, composed.name + bkp.backup_pay.SUFFIX]
        assert Path(str(composed) + bkp.backup_pay.SUFFIX).is_file()
        assert bkp.backup_pay.verify_pay_snapshot(composed, composed_manifest)["verified"]
    finally:
        source.stop()


def test_manifest_v2_written_when_composition_and_registry_present(tmp_path, monkeypatch):
    """有构建身份 + 组件登记时写 v2，且恢复入口能把它读回来。

    核心回归方向：v2 备份**不能**被自己的恢复入口判成无效。这条用真实
    `read_dump_manifest` 验证，不是只看构造函数的返回值。

    R07：`component_report` 传 `registry_report` **可调用**——报告在 dump 的
    同一导出 snapshot 内经真实 psycopg 连接收集（dict 直传已被拒绝，不构成
    同 snapshot 证据）。原生 PG 模式下 psql 命令形如
    `psql -h 127.0.0.1 -p <port> -U <user> -d <db>`，可解析出同一 DB。
    """
    import component_migrations
    import composition_identity

    if not _pg_tools_available():
        raise RuntimeError("PG16/CLI 不可用：真 PG16 manifest v2 无法执行（不伪 skip）")
    source = _start_pg16()
    out = tmp_path / "out"
    out.mkdir()
    try:
        _boot_schema12(source)
        # 组件接管需要登记表存在；在生产里由 pg_component_schema.py 负责。
        plugins_root = Path(__file__).resolve().parents[1] / "plugins"
        components = component_migrations.declared_components(plugins_root)
        with support.connect(source.dsn(), autocommit=True) as admin:
            component_migrations.adopt_legacy(admin, components)
        plugin_identity = [
            {"id": pid, "version": json.loads((plugins_root / pid / "manifest.json").read_text())["version"],
             "digest": "b" * 64}
            for pid in json.loads((plugins_root / "composition.json").read_text())["plugins"]
        ]
        monkeypatch.setattr(
            composition_identity, "current",
            lambda: {
                "schema_version": 1,
                "kernel": {"version": "1.0.0", "digest": "a" * 64},
                "plugins": plugin_identity,
                "composition_digest": "c" * 64,
            },
        )
        pay_root = tmp_path / "pay"
        pay_root.mkdir()
        monkeypatch.setenv("CONSOLE_PAY_DATA_DIR", str(pay_root))
        dest = out / f"{bkp.PG_DUMP_PREFIX}.20260914"
        bkp.write_pg_dump(dest, commands=_pg_commands(source), root=tmp_path,
                          component_report=lambda conn: component_migrations.registry_report(
                              conn, components))

        manifest = bkp.read_dump_manifest(dest)
        assert manifest is not None, "v2 备份必须能被自己的恢复入口读出"
        assert manifest["manifest_version"] == "pg-backup-manifest-2"
        assert manifest["composition"]["kernel_version"] == "1.0.0"
        assert {row["name"] for row in manifest["components"]} >= {"kernel"}
        assert manifest["tables"]["oplog"] == "kernel"
        assert manifest["tables"]["panels"] == "retained"
        assert isinstance(manifest["attachments"], list)

        # v1 键语义未变：恢复路径仍按同一组字段工作
        for key in pg_contracts.BACKUP_MANIFEST_KEYS:
            if key != "manifest_version":
                assert manifest.get(key) is not None, key
    finally:
        source.stop()


def test_manifest_stays_v1_without_component_report(tmp_path, monkeypatch):
    """现役私人部署（无登记报告/无身份）必须继续写 v1，不能因新特性失败。"""
    import composition_identity

    if not _pg_tools_available():
        raise RuntimeError("PG16/CLI 不可用：真 PG16 manifest v1 降级无法执行（不伪 skip）")
    source = _start_pg16()
    try:
        _boot_schema12(source)
        monkeypatch.setattr(composition_identity, "current", lambda: None)
        dest = tmp_path / f"{bkp.PG_DUMP_PREFIX}.20260915"
        bkp.write_pg_dump(dest, commands=_pg_commands(source))
        manifest = bkp.read_dump_manifest(dest)
        assert manifest["manifest_version"] == "pg-backup-manifest-1"
        assert set(manifest) == set(pg_contracts.BACKUP_MANIFEST_KEYS)
    finally:
        source.stop()


def test_parse_docker_exec_psql_identity():
    """生产 default_pg_commands 与 -i 变体都能解析出同机 container/user/db。"""
    commands = bkp.default_pg_commands(container="console-pg-fixture", user="console_test_admin",
                                       database="console_test")
    assert commands["psql"][:2] == ["docker", "exec"]
    identity = bkp._parse_docker_exec_psql(commands["psql"])
    assert identity == {
        "container": "console-pg-fixture",
        "user": "console_test_admin",
        "dbname": "console_test",
    }
    identity = bkp._parse_docker_exec_psql(
        ["docker", "exec", "-i", "c", "psql", "-U", "u", "-d", "d", "-Atc"])
    assert identity == {"container": "c", "user": "u", "dbname": "d"}


def test_docker_exec_component_report_same_snapshot(tmp_path, monkeypatch):
    """P2：生产 docker exec 命令形状下，组件报告在 dump 同一 exported snapshot 内收集。

    不猜宿主映射端口、不读 CONSOLE_PG_* 另开数据源；dict/过期报告仍拒绝。
    旧的「裸拒绝 docker exec」不再是成功标准。
    """
    import component_migrations
    import composition_identity

    source = _start_docker_pg16_forced()
    assert source.name != "native"
    commands = _docker_exec_commands(source)
    assert commands["psql"][:2] == ["docker", "exec"]
    assert " -h " not in " ".join(commands["psql"])
    out = tmp_path / "out"
    out.mkdir()
    try:
        _boot_schema12(source)
        plugins_root = Path(__file__).resolve().parents[1] / "plugins"
        components = component_migrations.declared_components(plugins_root)
        with support.connect(source.dsn(), autocommit=True) as admin:
            component_migrations.adopt_legacy(admin, components)
        plugin_identity = [
            {"id": pid, "version": json.loads((plugins_root / pid / "manifest.json").read_text())["version"],
             "digest": "b" * 64}
            for pid in json.loads((plugins_root / "composition.json").read_text())["plugins"]
        ]
        monkeypatch.setattr(
            composition_identity, "current",
            lambda: {
                "schema_version": 1,
                "kernel": {"version": "1.0.0", "digest": "a" * 64},
                "plugins": plugin_identity,
                "composition_digest": "c" * 64,
            },
        )
        pay_root = tmp_path / "pay"
        pay_root.mkdir()
        monkeypatch.setenv("CONSOLE_PAY_DATA_DIR", str(pay_root))
        dest = out / f"{bkp.PG_DUMP_PREFIX}.20261101"
        seen = []

        def collect(conn):
            isolation = conn.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"]
            readonly = conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"]
            assert isolation == "repeatable read"
            assert readonly == "on"
            report = component_migrations.registry_report(conn, components)
            assert report["ok"] and report["components"], report["issues"]
            seen.append(report)
            return report

        bkp.write_pg_dump(dest, commands=commands, root=tmp_path, component_report=collect)
        manifest = bkp.read_dump_manifest(dest)
        assert manifest["manifest_version"] == "pg-backup-manifest-2"
        assert seen and {row["name"] for row in manifest["components"]} >= {"kernel"}

        before = {
            dest: dest.read_bytes(),
            Path(str(dest) + bkp.MANIFEST_SUFFIX): Path(str(dest) + bkp.MANIFEST_SUFFIX).read_bytes(),
        }

        def reject(callback):
            try:
                bkp.write_pg_dump(dest, commands=commands, root=tmp_path, component_report=callback)
            except (TypeError, ValueError, RuntimeError):
                pass
            else:
                raise AssertionError("无效组件报告输入被接受")
            assert dest.read_bytes() == before[dest]
            assert Path(str(dest) + bkp.MANIFEST_SUFFIX).read_bytes() == before[
                Path(str(dest) + bkp.MANIFEST_SUFFIX)]

        # dict 直传 / 过期报告 / 篡改报告均拒绝；已发布组字节不变
        reject(seen[0])
        reject({"ok": True, "components": seen[0]["components"], "present_tables": seen[0]["present_tables"]})
        reject(lambda conn: dict(component_migrations.registry_report(conn, components), ok=False))
    finally:
        source.stop()
