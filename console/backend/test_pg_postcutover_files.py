"""R05/R06/R07: real PG, separate local authority files, and repeatable input bytes."""

import contextlib
import io
import json
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from flask import Flask
from openpyxl import load_workbook
from werkzeug.serving import make_server

import db
import ledger_ai_classify as ai
import ledger_auto_classify as ac
import ledger_cli
import ledger_file_routes
import ledger_files
import ledger_import
import paths
import pg_contracts
import pg_locks
import pg_test_support
import pgdb
from test_ledger_toolkit import workbook_upload
from test_pg01_batch_protocol import _reference_bytes

pytest_plugins = ("pg01_test_support",)


@pytest.fixture()
def file_world(ledger_env, tmp_path, monkeypatch):
    mac, nas = tmp_path / "mac", tmp_path / "nas"
    mac.mkdir()
    (nas / "reference").mkdir(parents=True)
    monkeypatch.setattr(paths, "PAY_DATA_DIR", str(mac))
    app = Flask(__name__)
    ledger_file_routes.register_routes(app, data_dir=lambda: nas, guard=lambda: True)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "client")
    monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("CONSOLE_CODE_VERSION", "r05-r07-test")
    try:
        yield mac, nas
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_classify_cli_uses_pg_knowledge_and_explicit_offline_sqlite(
    file_world, monkeypatch, capsys
):
    mac, _ = file_world
    retired = mac / "retired.db"
    with sqlite3.connect(retired) as conn:
        conn.executescript("""
            CREATE TABLE ledger_categories(level1 TEXT, level2 TEXT);
            CREATE TABLE ledger_knowledge(who TEXT, good TEXT, type1 TEXT, type2 TEXT);
            INSERT INTO ledger_categories VALUES('离线旧类','旧知识');
            INSERT INTO ledger_knowledge VALUES('合成商户','合成商品','离线旧类','旧知识');
        """)
    monkeypatch.setattr(paths, "DB_PATH", str(retired))
    monkeypatch.setattr(db, "DB_PATH", str(retired))
    with pgdb.pg_transaction() as conn:
        conn.execute("INSERT INTO ledger_knowledge(who,good,type1,type2) "
                     "VALUES('合成商户','合成商品','测试支出','日用')")
    source = mac / "bill.xlsx"
    source.write_bytes(workbook_upload().read())

    ledger_cli.main(["classify", "--data-dir", str(mac), str(source)])
    result = json.loads(capsys.readouterr().out)
    with contextlib.closing(load_workbook(mac / "classified" / result["files"][0]["file"])) as book:
        header, row = list(book.active.values)
        assert row[header.index("一级分类")] == "测试支出"
        assert row[header.index("二级分类")] == "日用"

    result = ac.classify_files([source], db_path=retired, data_dir=mac)
    with contextlib.closing(load_workbook(mac / "classified" / result["files"][0]["file"])) as book:
        header, row = list(book.active.values)
        assert row[header.index("一级分类")] == "离线旧类"
        assert row[header.index("二级分类")] == "旧知识"


def _mapping(mac):
    with pgdb.pg_transaction() as conn:
        ident = conn.execute("INSERT INTO ledger_tx(status,note) VALUES('normal','合成备注') "
                             "RETURNING id").fetchone()["id"]
    source = mac / "mapping.json"
    source.write_bytes(json.dumps({str(ident): ["测试支出", "日用"]}, ensure_ascii=False,
                                 indent=2).encode("utf-8"))
    return source, ident


def test_ai_apply_cli_archives_original_and_binds_force(file_world, capsys):
    mac, nas = file_world
    source, ident = _mapping(mac)
    batch_id = str(uuid.uuid4())
    args = ["ai", "--apply", str(source), "--data-dir", str(mac), "--batch-id", batch_id]
    ledger_cli.main(args)
    first = json.loads(capsys.readouterr().out)
    assert first["applied"] == 1 and first["batch_id"] == batch_id
    receipt = ledger_files.load_receipt(batch_id, nas)
    assert receipt["operation"] == "ai_apply" and receipt["client"] == "mac"
    assert receipt["artifacts"][0]["sha256"] == ledger_files.sha256_bytes(source.read_bytes())
    assert ledger_files.verify_archive(batch_id, nas)["ok"]
    assert not (mac / "archive").exists()
    with pgdb.pg_transaction(readonly=True) as conn:
        assert conn.execute("SELECT type1 FROM ledger_tx WHERE id=%s", (ident,)).fetchone()["type1"] == "测试支出"
        stored = ledger_files.find_batch_record(conn, batch_id)
        assert stored["result_json"] == first
    ledger_cli.main(args)
    assert json.loads(capsys.readouterr().out) == first
    with pytest.raises(pg_contracts.ContractError, match="不一致"):
        ledger_cli.main([*args, "--force"])


def test_ai_apply_rolls_back_business_and_batch_but_keeps_archive(file_world, monkeypatch):
    mac, nas = file_world
    source, ident = _mapping(mac)
    batch_id = str(uuid.uuid4())
    real_commit = ledger_files.commit_batch_record

    def fail_after_record(conn, receipt, result):
        real_commit(conn, receipt, result)
        raise RuntimeError("synthetic before COMMIT")

    monkeypatch.setattr(ledger_files, "commit_batch_record", fail_after_record)
    with pytest.raises(RuntimeError, match="before COMMIT"):
        ai.apply_file(source, batch_id=batch_id, data_dir=mac)
    assert ledger_files.verify_archive(batch_id, nas)["ok"]
    with pgdb.pg_transaction(readonly=True) as conn:
        assert ledger_files.find_batch_record(conn, batch_id) is None
        assert conn.execute("SELECT type1 FROM ledger_tx WHERE id=%s", (ident,)).fetchone()["type1"] is None
    monkeypatch.setattr(ledger_files, "commit_batch_record", real_commit)
    assert ai.apply_file(source, batch_id=batch_id, data_dir=mac)["applied"] == 1


def test_reference_content_version_ignores_generated_at():
    first = {"version": pg_contracts.REFERENCE_MANIFEST_VERSION,
             "generated_at": "2026-09-01 00:00:00", "files": []}
    later = {**first, "generated_at": "2026-09-12 00:00:00"}
    assert pg_contracts.reference_manifest_digest(first) == pg_contracts.reference_manifest_digest(later)


def test_reference_history_restores_after_current_replacement_and_cache_loss(tmp_path):
    root = tmp_path / "authority"
    (root / "reference").mkdir(parents=True)
    raw = _reference_bytes("old")
    (root / "reference" / "reference.xlsx").write_bytes(raw)
    batch_id = str(uuid.uuid4())
    original = ledger_files.open_session("reclassify", role="authority", data_dir=root,
                                         batch_id=batch_id)
    receipt = original.archive([])
    shutil.rmtree(root / "reference-cache")
    (root / "reference" / "reference.xlsx").write_bytes(_reference_bytes("new"))
    ledger_files.reference_manifest(root)
    assert ledger_files.read_reference(ledger_files.sha256_bytes(raw), root)["content"] == raw
    retry = ledger_files.open_session("reclassify", role="authority", data_dir=root,
                                      batch_id=batch_id)
    assert retry.reference.path.read_bytes() == raw
    assert retry.archive([]) == receipt


def test_uncommitted_import_recovers_historical_reference_over_http(file_world, monkeypatch):
    mac, nas = file_world
    raw = _reference_bytes("old")
    (nas / "reference" / "reference.xlsx").write_bytes(raw)
    batch_id = str(uuid.uuid4())
    real_insert = ledger_import._insert_bill
    bill = workbook_upload().read()

    def uploads():
        from werkzeug.datastructures import FileStorage
        return [FileStorage(stream=io.BytesIO(bill), filename="bill.xlsx")]

    def fail(*args):
        raise RuntimeError("synthetic database rollback")

    monkeypatch.setattr(ledger_import, "_insert_bill", fail)
    with pytest.raises(RuntimeError, match="rollback"):
        ledger_import.import_files(uploads(), data_dir=mac, batch_id=batch_id)
    receipt = ledger_files.load_receipt(batch_id, nas)
    shutil.rmtree(mac / "reference-cache")
    (nas / "reference" / "reference.xlsx").write_bytes(_reference_bytes("new"))
    ledger_files.reference_manifest(nas)
    monkeypatch.setattr(ledger_import, "_insert_bill", real_insert)
    result = ledger_import.import_files(uploads(), data_dir=mac, batch_id=batch_id)
    assert result["inserted"] == 1
    assert result["reference"]["file_sha256"] == ledger_files.sha256_bytes(raw)
    assert result["receipt"] == receipt


@pytest.mark.parametrize("operation", ["import", "reclassify", "ai_apply"])
def test_unknown_commit_recovers_before_reference_or_archive_access(
    file_world, monkeypatch, operation
):
    mac, nas = file_world
    (nas / "reference" / "reference.xlsx").write_bytes(_reference_bytes("old"))
    batch_id = str(uuid.uuid4())
    source, _ = _mapping(mac)
    raw = workbook_upload().read()

    def invoke():
        if operation == "ai_apply":
            return ai.apply_file(source, data_dir=mac, batch_id=batch_id)
        if operation == "reclassify":
            return ledger_import.reclassify(data_dir=mac, batch_id=batch_id)
        from werkzeug.datastructures import FileStorage
        return ledger_import.import_files(
            [FileStorage(stream=io.BytesIO(raw), filename="bill.xlsx")],
            data_dir=mac, batch_id=batch_id,
        )

    real_transaction = db.pg_transaction

    @contextlib.contextmanager
    def lost_commit(**kwargs):
        with real_transaction(**kwargs) as conn:
            yield conn
        if not kwargs.get("readonly"):
            raise RuntimeError("synthetic COMMIT result unknown")

    monkeypatch.setattr(db, "pg_transaction", lost_commit)
    with pytest.raises(RuntimeError, match="result unknown"):
        invoke()
    monkeypatch.setattr(db, "pg_transaction", real_transaction)
    with pgdb.pg_transaction(readonly=True) as conn:
        stored = ledger_files.find_batch_record(conn, batch_id)
        assert stored is not None
    if (mac / "reference-cache").exists():
        shutil.rmtree(mac / "reference-cache")
    (nas / "reference" / "reference.xlsx").write_bytes(_reference_bytes("new"))

    def forbidden(*args, **kwargs):
        pytest.fail("committed recovery must query PG before file/reference service")

    monkeypatch.setattr(ledger_files, "open_session", forbidden)
    monkeypatch.setenv("CONSOLE_CODE_VERSION", "new-code-after-commit")
    assert invoke() == stored["result_json"]


def test_legacy_manifest_digest_is_strict_and_promoted_before_cache_loss(tmp_path):
    root = tmp_path / "authority"
    cache = root / "reference-cache"
    (cache / "manifests").mkdir(parents=True)
    raw = _reference_bytes("legacy")
    file_hash = ledger_files.sha256_bytes(raw)
    manifest = {
        "version": pg_contracts.REFERENCE_MANIFEST_VERSION,
        "generated_at": "2026-09-01 01:02:03",
        "files": [{"path": "reference.xlsx", "sha256": file_hash,
                   "bytes": len(raw), "format_version": "reference-file-1"}],
    }
    legacy_hash = ledger_files.sha256_bytes(pg_contracts.canonical_json(manifest).encode())
    (cache / "manifests" / f"{legacy_hash}.json").write_text(json.dumps(manifest))
    (cache / f"{file_hash}.xlsx").write_bytes(raw)
    batch_id = str(uuid.uuid4())
    receipt = ledger_files.publish_batch(
        batch_id, "reclassify", "nas", "old-code", [], data_dir=root,
        reference_manifest_sha256=legacy_hash,
    )
    (root / "reference").mkdir()
    (root / "reference" / "reference.xlsx").write_bytes(_reference_bytes("new"))
    with pytest.raises(pg_contracts.ContractError, match="执行身份不一致"):
        ledger_files.open_session("reclassify", role="authority", data_dir=root,
                                  batch_id=batch_id, code_version="new-code")
    first = ledger_files.open_session("reclassify", role="authority", data_dir=root,
                                      batch_id=batch_id, code_version="old-code")
    assert first.reference.manifest_sha256 == legacy_hash
    assert first.archive([]) == receipt
    shutil.rmtree(cache)
    recovered = ledger_files.open_session("reclassify", role="authority", data_dir=root,
                                          batch_id=batch_id, code_version="old-code")
    assert recovered.reference.manifest_sha256 == legacy_hash
    assert recovered.reference.path.read_bytes() == raw
    assert recovered.archive([]) == receipt
    wrong = {**manifest, "generated_at": "2026-09-02 00:00:00"}
    (cache / "manifests" / f"{legacy_hash}.json").write_text(json.dumps(wrong))
    assert ledger_files._cached_manifest(legacy_hash, cache) is None


def test_uncommitted_retry_rejects_changed_client_identity(tmp_path):
    batch_id = str(uuid.uuid4())
    ledger_files.publish_batch(batch_id, "reclassify", "nas", "test", [], data_dir=tmp_path)
    with pytest.raises(pg_contracts.ContractError, match="执行身份不一致"):
        ledger_files.open_session("reclassify", role="test", data_dir=tmp_path,
                                  batch_id=batch_id, code_version="test")


def test_ai_apply_rejects_changed_input_before_returning_committed_result(file_world):
    mac, _ = file_world
    source, _ = _mapping(mac)
    batch_id = str(uuid.uuid4())
    ai.apply_file(source, batch_id=batch_id, data_dir=mac)
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(pg_contracts.ContractError, match="不一致"):
        ai.apply_file(source, batch_id=batch_id, data_dir=mac)


@pytest.mark.parametrize("raw", [b'{"1":["x"],"1":["y"]}', b"[]", b"not-json"])
def test_invalid_ai_json_fails_before_archive_or_write(file_world, raw):
    mac, nas = file_world
    source = mac / "invalid.json"
    source.write_bytes(raw)
    batch_id = str(uuid.uuid4())
    with pytest.raises(ValueError):
        ai.apply_file(source, batch_id=batch_id, data_dir=mac)
    assert ledger_files.load_receipt(batch_id, nas) is None
    with pgdb.pg_transaction(readonly=True) as conn:
        assert ledger_files.find_batch_record(conn, batch_id) is None


def test_ai_archive_failure_never_writes_pg_or_local_archive(file_world, monkeypatch):
    mac, _ = file_world
    source, ident = _mapping(mac)
    batch_id = str(uuid.uuid4())

    def unavailable(*args, **kwargs):
        raise ledger_files.ArchiveError("synthetic authority unavailable")

    monkeypatch.setattr(ledger_files.BatchClient, "upload_batch", unavailable)
    with pytest.raises(ledger_files.ArchiveError, match="unavailable"):
        ai.apply_file(source, batch_id=batch_id, data_dir=mac)
    with pgdb.pg_transaction(readonly=True) as conn:
        assert ledger_files.find_batch_record(conn, batch_id) is None
        assert conn.execute("SELECT type1 FROM ledger_tx WHERE id=%s", (ident,)).fetchone()["type1"] is None
    assert not (mac / "archive").exists()


def test_concurrent_ai_retry_commits_one_result_under_ledger_lock(file_world, monkeypatch):
    mac, _ = file_world
    source, _ = _mapping(mac)
    batch_id = str(uuid.uuid4())
    real_apply = ai._apply_mapping_in

    def failure(*args):
        raise RuntimeError("synthetic rollback")

    monkeypatch.setattr(ai, "_apply_mapping_in", failure)
    with pytest.raises(RuntimeError, match="rollback"):
        ai.apply_file(source, batch_id=batch_id, data_dir=mac)
    monkeypatch.setattr(ai, "_apply_mapping_in", real_apply)
    real_recover = ledger_files.recover_batch_result
    both_checked = threading.Barrier(2)

    def simultaneous_recovery(*args):
        result = real_recover(*args)
        assert result is None
        both_checked.wait(timeout=5)
        return result

    monkeypatch.setattr(ledger_files, "recover_batch_result", simultaneous_recovery)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ai.apply_file, source, batch_id=batch_id, data_dir=mac)
                   for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert results[0] == results[1] and results[0]["applied"] == 1
    with pgdb.pg_transaction(readonly=True) as conn:
        assert conn.execute("SELECT count(*) AS n FROM ledger_file_batches WHERE batch_id=%s",
                            (batch_id,)).fetchone()["n"] == 1


def _waiting_for_advisory(observer, pid, future):
    deadline = time.monotonic() + 0.5
    while not future.done() and time.monotonic() < deadline:
        row = observer.execute("SELECT wait_event FROM pg_stat_activity WHERE pid=%s", (pid,)).fetchone()
        if row and row["wait_event"] == "advisory":
            return True
        time.sleep(0.01)
    return False


def test_ai_mapping_reused_transaction_waits_before_reading_manual_category(ledger_env, tmp_path):
    _, ident = _mapping(tmp_path)
    started = threading.Event()
    backend_pid = []

    def apply_in_existing_transaction():
        with db.pg_transaction() as conn:
            backend_pid.append(conn.info.backend_pid)
            started.set()
            return ai.apply_mapping({str(ident): ["测试支出", "日用"]}, conn=conn)

    with pg_test_support.connect(ledger_env, autocommit=True) as observer:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with db.pg_transaction(lock=pg_locks.LEDGER) as owner:
                future = pool.submit(apply_in_existing_transaction)
                assert started.wait(2)
                waiting = _waiting_for_advisory(observer, backend_pid[0], future)
                owner.execute("UPDATE ledger_tx SET type1='生活',type2='日用',basis='人工分类' WHERE id=%s", (ident,))
            result = future.result(timeout=2)
    assert waiting, "reused transaction did not acquire the ledger domain lock"
    assert result == {"applied": 0, "skipped": 1}
    with db.pg_transaction(readonly=True) as conn:
        assert conn.execute("SELECT basis FROM ledger_tx WHERE id=%s", (ident,)).fetchone()["basis"] == "人工分类"


def test_batch_recovery_waits_for_pending_commit_before_deciding_absent(ledger_env, tmp_path):
    batch_id = str(uuid.uuid4())
    uploads = [("mapping.json", b'{"1":["test"]}')]
    receipt = ledger_files.publish_batch(batch_id, "ai_apply", "test", "test", uploads, data_dir=tmp_path)
    result = {"batch_id": batch_id, "applied": 1}

    with pg_test_support.connect(ledger_env, autocommit=True) as observer:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with db.pg_transaction(lock=pg_locks.LEDGER) as owner:
                ledger_files.commit_batch_record(owner, receipt, result)
                future = pool.submit(ledger_files.recover_batch_result, batch_id, "ai_apply", uploads)
                deadline = time.monotonic() + 0.5
                waiting = False
                while not future.done() and time.monotonic() < deadline:
                    row = observer.execute(
                        "SELECT count(*) AS n FROM pg_stat_activity WHERE datname=current_database() "
                        "AND pid != %s AND wait_event='advisory'", (owner.info.backend_pid,),
                    ).fetchone()
                    if row["n"]:
                        waiting = True
                        break
                    time.sleep(0.01)
            recovered = future.result(timeout=2)
    assert waiting, "pending original commit was treated as an absent batch"
    assert recovered == result
