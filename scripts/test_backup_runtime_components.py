"""固定控制器四组合真 PG 恢复；独立子进程仅能导入控制器内源码。"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import backup_runtime
import component_migrations as cm
import kernel_baseline
import pg_contracts
import pg_migrations
import pg_test_support as support

REPO = Path(__file__).resolve().parents[1]


def _tool(instance, tool, dbname=None, user=None):
    if instance.name == "native":
        command = [str(instance.bin_dir / tool)]
        if dbname is not None:
            command += ["-h", instance.host, "-p", str(instance.port)]
    else:
        command = ["docker", "exec", "-i", instance.name, tool]
    if dbname is not None:
        command += ["-U", user or instance.user, "-d", dbname]
    return command


def _commands(instance, dbname, user=None):
    psql = [*_tool(instance, "psql", dbname, user), "-X", "-v", "ON_ERROR_STOP=1", "-Atc"]
    return {"dump": [*_tool(instance, "pg_dump", dbname, user), "-Fc"],
            "list": [*_tool(instance, "pg_restore"), "--list"],
            "psql": psql, "server_version": [*psql, "SHOW server_version"],
            "database": dbname}


def _seed_pay(conn, pay):
    """只用合成字节建立原件/历史reference，并向真实PG提交同一批次身份。"""
    reference_raw = b"category,value\nfixture,12\n"
    reference_sha = hashlib.sha256(reference_raw).hexdigest()
    reference = {
        "version": "reference-manifest-1", "generated_at": "synthetic",
        "files": [{"path": "rules.csv", "sha256": reference_sha,
                   "bytes": len(reference_raw), "format_version": "csv"}],
    }
    reference_digest = pg_contracts.reference_manifest_digest(reference)
    for relative, raw in {
        "reference/rules.csv": reference_raw,
        f"reference-history/files/{reference_sha}": reference_raw,
        f"reference-history/manifests/{reference_digest}.json": json.dumps(reference).encode(),
        "raw/legacy.csv": b"legacy,synthetic\n",
    }.items():
        path = pay / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    batch_id = "11111111-1111-4111-8111-111111111111"
    bill = b"date,amount\n2026-10-31,12.34\n"
    bill_sha = hashlib.sha256(bill).hexdigest()
    receipt = pg_contracts.build_batch_receipt(
        batch_id, "import", "test", "synthetic",
        [{"name": "bill.csv", "sha256": bill_sha, "bytes": len(bill)}], reference_digest,
    )
    archive = pay / "archive" / batch_id
    archive.mkdir(parents=True)
    (archive / "manifest.json").write_text(json.dumps(receipt))
    original = archive / f"01-{bill_sha[:16]}.csv"
    original.write_bytes(bill)
    conn.execute(
        "INSERT INTO ledger_file_batches (batch_id, operation, client, code_version, "
        "request_sha256, reference_manifest_sha256, artifacts_json) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)",
        tuple(receipt[key] for key in (
            "batch_id", "operation", "client", "code_version", "request_sha256",
            "reference_manifest_sha256")) + (json.dumps(receipt["artifacts"]),),
    )
    return {str(path.relative_to(pay)): path.read_bytes()
            for path in pay.rglob("*") if path.is_file()}


_RUN_CONTROLLER = '''
import hashlib
import json
import os
from pathlib import Path
import sys
controller, config_file = map(Path, sys.argv[1:])
sys.path[:0] = [str(controller / "scripts"), str(controller / "console/backend")]
import backup_db
import component_migrations
import psycopg
commands = json.loads(config_file.read_text())
captured = []
def collect(connection):
    assert connection.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"] == "repeatable read"
    assert connection.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
    report = component_migrations.registry_report(connection)
    assert report["ok"] and report["components"], report["issues"]
    captured.append(report)
    # 报告读取之后、摘要与dump之前提交；序列不参与MVCC，另行核水位不倒退。
    with psycopg.connect(commands["writer_dsn"], autocommit=True) as writer:
        writer.execute("INSERT INTO oplog(host, svc, action) VALUES ('late', 'fixture', 'committed')")
        if commands["has_pay"]:
            pay = Path(os.environ["CONSOLE_PAY_DATA_DIR"])
            batch_id = "22222222-2222-4222-8222-222222222222"
            raw = b"late,synthetic\\n"
            digest = hashlib.sha256(raw).hexdigest()
            import pg_contracts
            receipt = pg_contracts.build_batch_receipt(
                batch_id, "import", "test", "synthetic",
                [{"name": "late.csv", "sha256": digest, "bytes": len(raw)}], None,
            )
            archive = pay / "archive" / batch_id
            archive.mkdir(parents=True)
            (archive / "manifest.json").write_text(json.dumps(receipt))
            (archive / ("01-" + digest[:16] + ".csv")).write_bytes(raw)
            writer.execute(
                "INSERT INTO ledger_file_batches (batch_id, operation, client, code_version, "
                "request_sha256, reference_manifest_sha256, artifacts_json) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)",
                tuple(receipt[key] for key in (
                    "batch_id", "operation", "client", "code_version", "request_sha256",
                    "reference_manifest_sha256")) + (json.dumps(receipt["artifacts"]),),
            )
    return report

dest = controller / "console.dump.20261031"
backup_db.write_pg_dump(
    dest, commands=commands["backup"], attachment=commands["has_pay"], component_report=collect,
)
manifest = backup_db.read_dump_manifest(dest)
assert manifest["manifest_version"] == "pg-backup-manifest-2"
# 拒绝坏报告/旧报告和矛盾身份时，已发布完整组必须逐字节保留。
paths = [dest, Path(str(dest) + ".manifest.json")]
if commands["has_pay"]:
    paths.append(Path(str(dest) + ".pay.tar"))
before = {path: path.read_bytes() for path in paths}
def reject(callback, *, attachment=None):
    try:
        backup_db.write_pg_dump(dest, commands=commands["backup"], component_report=callback,
                                attachment=commands["has_pay"] if attachment is None else attachment)
    except (TypeError, ValueError, RuntimeError):
        pass
    else:
        raise AssertionError("无效备份输入被接受")
    assert {path: path.read_bytes() for path in paths} == before
reject(captured[0])
reject(lambda connection: dict(component_migrations.registry_report(connection), ok=False))
reject(lambda connection: dict(component_migrations.registry_report(connection), present_tables=[]))
# 现场登记本身损坏也必须拒绝，不能只验证调用方返回了坏dict。
with psycopg.connect(commands["writer_dsn"], autocommit=True) as writer:
    original_checksum = writer.execute(
        "SELECT checksum FROM component_schema_migrations WHERE component='kernel' ORDER BY version LIMIT 1"
    ).fetchone()[0]
    try:
        writer.execute("UPDATE component_schema_migrations SET checksum=%s WHERE component='kernel'", ("0" * 64,))
        reject(component_migrations.registry_report)
    finally:
        writer.execute("UPDATE component_schema_migrations SET checksum=%s WHERE component='kernel'", (original_checksum,))
identity_path = Path(os.environ["CONSOLE_BUILD_IDENTITY_FILE"])
identity = json.loads(identity_path.read_text())
try:
    wrong = dict(identity, plugins=[{"id": "synthetic_other", "version": "1", "digest": "c" * 64}])
    identity_path.write_text(json.dumps(wrong))
    reject(component_migrations.registry_report)
finally:
    identity_path.write_text(json.dumps(identity))
if commands["has_pay"]:
    reject(component_migrations.registry_report, attachment=False)
    missing = Path(os.environ["CONSOLE_PAY_DATA_DIR"]) / "archive" / "11111111-1111-4111-8111-111111111111" / "manifest.json"
    raw_receipt = missing.read_bytes()
    try:
        missing.write_bytes(b"{}")
        reject(component_migrations.registry_report)
    finally:
        missing.write_bytes(raw_receipt)
assert set(manifest["tables"]) == set(manifest["row_digest"]["tables"]) == set(captured[0]["present_tables"])
assert manifest["row_digest"]["tables"]["oplog"]["rows"] == 1
if commands["has_pay"]:
    assert len(manifest["attachments"]) == 1
    assert manifest["row_digest"]["tables"]["ledger_file_batches"]["rows"] == 1
    if commands["case"] == "retained":
        assert manifest["tables"]["ledger_file_batches"] == "retained"
        assert any(row["name"] == "plugin:ledger" for row in manifest["components"])
        assert manifest["composition"]["plugins"] == []
else:
    assert manifest["attachments"] == []
# 无显式回调时，构建身份驱动自动同snapshot报告；不退回v1。
automatic = controller / "console.dump.automatic"
backup_db.write_pg_dump(automatic, commands=commands["backup"], attachment=commands["has_pay"])
assert backup_db.read_dump_manifest(automatic)["manifest_version"] == "pg-backup-manifest-2"
if commands["has_pay"]:
    companion = Path(str(dest) + ".pay.tar")
    saved = companion.with_name(companion.name + ".fixture-saved")
    companion.replace(saved)
    try:
        def forbidden_restore():
            raise AssertionError("缺失附件必须在启动恢复目标之前拒绝")
        try:
            backup_db.verify_restore(dest, boot_target=forbidden_restore)
        except (ValueError, FileNotFoundError):
            pass
        else:
            raise AssertionError("丢失原件tar被接受")
    finally:
        saved.replace(companion)
restored = backup_db.verify_restore(
    dest, boot_target=lambda: (commands["restore"], commands["target_psql"]),
    expected_tables=list(manifest["tables"]), pay_dest=commands["pay_dest"],
)
assert restored["restored"] and restored["row_digest_match"]
if commands["has_pay"]:
    assert restored["pay"]["verified"] and restored["pay"]["batches"] == 1
else:
    assert restored["pay"] is None
    assert "backup_pay" not in sys.modules
    assert "pg_contracts" not in sys.modules
    assert not any(name == "plugins.ledger" or name.startswith("plugins.ledger.") for name in sys.modules)
    assert not Path(commands["pay_dest"]).exists()
for module in (backup_db, component_migrations):
    assert Path(module.__file__).resolve().is_relative_to(controller)
print(json.dumps({"restored": True, "tables": len(restored["tables"]), "v2": True}))
'''


@pytest.mark.parametrize("case", ["zero", "audit", "private", "retained"])
def test_fixed_controller_v2_real_restore(pg_container, tmp_path, case):
    token = uuid.uuid4().hex[:10]
    source_name, target_name = f"b7_src_{token}", f"b7_dst_{token}"
    role, password = "b7_restore_" + token, uuid.uuid4().hex
    has_pay = case in {"private", "retained"}
    admin = support.connect(pg_container.dsn(), autocommit=True)
    source = target = None
    try:
        for name in (source_name, target_name):
            admin.execute(f'CREATE DATABASE "{name}"')
        source = support.connect(pg_container.dsn(dbname=source_name), autocommit=True)
        private_components = cm.declared_components(REPO / "plugins")
        if has_pay:
            with source.transaction():
                pg_migrations.apply(source)
                cm.adopt_legacy(source, private_components)
        else:
            cm.apply_components(source, components=cm.zero_plugin_components())
        components = private_components if case == "private" else cm.zero_plugin_components()
        report = cm.registry_report(source, components)
        assert report["ok"] is True, report["issues"]
        source.execute("INSERT INTO oplog(host, svc, action) VALUES ('synthetic', 'fixture', 'created')")
        source.execute("SELECT setval('oplog_id_seq', 41, true)")
        pay = tmp_path / "pay"
        originals = _seed_pay(source, pay) if has_pay else {}
        business_tables = set(report["present_tables"]) - {"schema_migrations", cm.REGISTRY_TABLE}
        support.create_app_role(source, role, password, tables=sorted(business_tables), database=source_name)
        source.execute(f'GRANT SELECT ON component_schema_migrations TO "{role}"')
        controller_dir = tmp_path / "run"
        backup_runtime.preserve_controller(controller_dir)
        controller = controller_dir / "controller"
        plugins = tmp_path / "plugins"
        plugins.mkdir()
        shutil.copyfile(REPO / "plugins/contract.json", plugins / "contract.json")
        selected = (json.loads((REPO / "plugins/composition.json").read_text())["plugins"]
                    if case == "private" else ["audit"] if case == "audit" else [])
        (plugins / "composition.json").write_text(json.dumps({"plugins": selected}))
        plugin_identity = []
        for plugin_id in selected:
            (plugins / plugin_id).mkdir()
            raw = (REPO / "plugins" / plugin_id / "manifest.json").read_bytes()
            (plugins / plugin_id / "manifest.json").write_bytes(raw)
            plugin_identity.append({"id": plugin_id, "version": json.loads(raw)["version"],
                                    "digest": hashlib.sha256(raw).hexdigest()})
        roots = {key: tmp_path / key for key in ("config", "data", "static", "backup")}
        for path in roots.values():
            path.mkdir()
        composition = tmp_path / "composition.json"
        composition.write_text(json.dumps({
            "schema_version": 1, "profile": "private" if case == "private" else "public",
            "host": {}, "alert_machines": {},
            "roots": {**{key: str(path) for key, path in roots.items()}, "plugins": str(plugins)},
        }))
        identity = tmp_path / "identity.json"
        identity.write_text(json.dumps({
            "schema_version": 1, "kernel": {"version": "synthetic", "digest": "a" * 64},
            "plugins": plugin_identity, "composition_digest": "b" * 64,
        }))
        pay_dest = tmp_path / "restored-pay"
        bundle_commands = tmp_path / "commands.json"
        bundle_commands.write_text(json.dumps({
            "backup": _commands(pg_container, source_name, role),
            "restore": [*_tool(pg_container, "pg_restore", target_name), "--no-owner"],
            "target_psql": _commands(pg_container, target_name)["psql"],
            "writer_dsn": pg_container.dsn(dbname=source_name),
            "has_pay": has_pay, "case": case, "pay_dest": str(pay_dest),
        }))
        env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
        env.update(
            CONSOLE_COMPOSITION_FILE=str(composition),
            CONSOLE_INSTANCE_PROFILE="private" if case == "private" else "public",
            CONSOLE_BUILD_IDENTITY_FILE=str(identity), CONSOLE_STORAGE_ENGINE="postgres",
            CONSOLE_INSTANCE_ROLE="test", PGPASSWORD=password,
            CONSOLE_PAY_DATA_DIR=str(pay), PGOPTIONS="-c timezone=UTC", TZ="UTC",
        )
        # 空提交仅给合成控制器提供source_commit，不提交用户工作树。
        for command in (["git", "init", "-q"],
                        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                         "commit", "--allow-empty", "-q", "-m", "synthetic source identity"]):
            subprocess.run(command, cwd=controller, check=True, capture_output=True)
        result = subprocess.run(
            [sys.executable, "-I", "-c", _RUN_CONTROLLER, str(controller), str(bundle_commands)],
            cwd=controller, env=env, capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout) == {
            "restored": True, "tables": len(report["present_tables"]), "v2": True,
        }
        target = support.connect(pg_container.dsn(dbname=target_name), autocommit=True)
        assert source.execute("SELECT count(*) AS n FROM oplog").fetchone()["n"] == 2
        assert target.execute("SELECT host, action FROM oplog").fetchall() == [
            {"host": "synthetic", "action": "created"},
        ]
        assert target.execute("SELECT last_value, is_called FROM oplog_id_seq").fetchone() == {
            "last_value": 42, "is_called": True,
        }
        assert cm.registry_report(target, components)["ok"] is True
        assert cm.registry_rows(target) == cm.registry_rows(source)
        privileges = target.execute(
            "SELECT has_table_privilege(%s, 'oplog', 'INSERT') AS dml, "
            "has_sequence_privilege(%s, 'oplog_id_seq', 'USAGE') AS sequence, "
            "has_table_privilege(%s, 'component_schema_migrations', 'INSERT,UPDATE,DELETE') AS registry_write",
            (role, role, role),
        ).fetchone()
        assert privileges == {"dml": True, "sequence": True, "registry_write": False}
        if has_pay:
            assert source.execute("SELECT count(*) AS n FROM ledger_file_batches").fetchone()["n"] == 2
            assert target.execute("SELECT count(*) AS n FROM ledger_file_batches").fetchone()["n"] == 1
            for relative, raw in originals.items():
                assert (pay_dest / relative).read_bytes() == raw
            assert all(path.stat().st_mode & 0o077 == 0 for path in pay_dest.rglob("*") if path.is_file())
        else:
            assert target.execute("SELECT to_regclass('ledger_tx') AS t").fetchone()["t"] is None
            assert set(report["table_owners"]) == set(kernel_baseline.KERNEL_BASELINE_TABLES)
    finally:
        for connection in (source, target):
            if connection is not None:
                connection.close()
        for name in (source_name, target_name):
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()
