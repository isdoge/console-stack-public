# -*- coding: utf-8 -*-
"""文件批次归档/reference 协议回归（提案 §3.2；PG-01 交付）。

真连 PG16：批次完成记录必须与业务变更**同事务**，回滚后两者都不能留下。
文件全部是合成字节，落盘目录是 `tmp_path` 私有目录，不触碰真实账本与 NAS。
"""
import json
import pathlib
import sys
import uuid

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import ledger_files  # noqa: E402
import pg_contracts  # noqa: E402
import pg_migrations  # noqa: E402
import pg_test_support  # noqa: E402
import pgdb  # noqa: E402


@pytest.fixture()
def pg_env(pg_container, tmp_path, monkeypatch):
    """独立数据库 + 独立归档目录（§8.5：各任务不共享 DSN/目录）。"""
    admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
    name = f"console_test_files_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    admin.close()
    conn = pg_test_support.connect(pg_container.dsn(dbname=name), autocommit=True)
    try:
        pg_migrations.apply(conn)
    finally:
        conn.close()

    data_dir = tmp_path / "pay"
    values = {
        "CONSOLE_PG_HOST": pg_container.host,
        "CONSOLE_PG_PORT": str(pg_container.port),
        "CONSOLE_PG_DB": name,
        "CONSOLE_PG_USER": pg_container.user,
        "CONSOLE_PG_PASSWORD": pg_container.password,
        "CONSOLE_STORAGE_ENGINE": "postgres",
        "CONSOLE_INSTANCE_ROLE": "test",
        "CONSOLE_MAINTENANCE_MODE": "normal",
        "CONSOLE_PG_POOL_MIN": "1",
        "CONSOLE_PG_POOL_MAX": "4",
        "CONSOLE_PG_LOCK_TIMEOUT": "1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    pgdb.close_pool()
    try:
        yield {"data_dir": data_dir, "dsn": pg_container.dsn(dbname=name)}
    finally:
        pgdb.close_pool()
        admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def _batch_id():
    return str(uuid.uuid4())


def _upload(name="bill.csv", raw=b"synthetic-bill\n"):
    return (name, raw)


# --- 归档 -------------------------------------------------------------------

def test_publish_returns_receipt_with_hash_and_is_idempotent(pg_env):
    batch_id = _batch_id()
    receipt = ledger_files.publish_batch(
        batch_id, "import", "mac", "test-1", [_upload()], data_dir=pg_env["data_dir"]
    )
    assert receipt["status"] == pg_contracts.BATCH_STATUS_ARCHIVED
    assert receipt["batch_id"] == batch_id
    assert receipt["artifacts"] == [
        {
            "name": "bill.csv",
            "sha256": ledger_files.sha256_bytes(b"synthetic-bill\n"),
            "bytes": len(b"synthetic-bill\n"),
        }
    ]
    again = ledger_files.publish_batch(
        batch_id, "import", "mac", "test-1", [_upload()], data_dir=pg_env["data_dir"]
    )
    assert again == receipt


def test_same_batch_id_with_different_content_is_rejected(pg_env):
    batch_id = _batch_id()
    ledger_files.publish_batch(
        batch_id, "import", "mac", "test-1", [_upload()], data_dir=pg_env["data_dir"]
    )
    with pytest.raises(pg_contracts.ContractError):
        ledger_files.publish_batch(
            batch_id,
            "import",
            "mac",
            "test-1",
            [_upload(raw=b"different")],
            data_dir=pg_env["data_dir"],
        )


def test_upload_filename_never_reaches_the_stored_path(pg_env):
    batch_id = _batch_id()
    receipt = ledger_files.publish_batch(
        batch_id,
        "import",
        "mac",
        "test-1",
        [("../../escape.csv", b"x")],
        data_dir=pg_env["data_dir"],
    )
    # 显示名被裁成 basename，只出现在回执里
    assert receipt["artifacts"][0]["name"] == "escape.csv"
    stored = list((pg_env["data_dir"] / "archive" / batch_id).iterdir())
    names = {item.name for item in stored}
    assert names == {"manifest.json", stored[0].name} or "manifest.json" in names
    for item in stored:
        assert "escape" not in item.name
        assert ".." not in item.name


def test_batch_limits_are_enforced(pg_env):
    with pytest.raises(pg_contracts.ContractError):
        ledger_files.publish_batch(
            _batch_id(),
            "import",
            "mac",
            "test-1",
            [_upload(f"f{index}.csv", b"x") for index in range(11)],
            data_dir=pg_env["data_dir"],
        )
    with pytest.raises(pg_contracts.ContractError):
        ledger_files.publish_batch(
            _batch_id(),
            "import",
            "mac",
            "test-1",
            [_upload("big.csv", b"x" * (20 * 1024 * 1024 + 1))],
            data_dir=pg_env["data_dir"],
        )
    with pytest.raises(pg_contracts.ContractError):
        ledger_files.publish_batch(
            _batch_id(),
            "import",
            "mac",
            "test-1",
            [_upload("bad.exe", b"x")],
            data_dir=pg_env["data_dir"],
        )


def test_unknown_operation_is_rejected(pg_env):
    with pytest.raises(pg_contracts.ContractError):
        ledger_files.publish_batch(
            _batch_id(),
            "not-an-operation",
            "mac",
            "test-1",
            [_upload()],
            data_dir=pg_env["data_dir"],
        )


def test_partial_batch_is_not_published(pg_env, monkeypatch):
    """第二个文件落盘失败 → 整批不可见，不留半个归档。"""
    real = ledger_files._atomic_write

    def fail_on_second(path, raw):
        if path.name.startswith("02-"):
            raise OSError("synthetic failure")
        return real(path, raw)

    monkeypatch.setattr(ledger_files, "_atomic_write", fail_on_second)
    batch_id = _batch_id()
    with pytest.raises(ledger_files.ArchiveError):
        ledger_files.publish_batch(
            batch_id,
            "import",
            "mac",
            "test-1",
            [_upload("a.csv", b"a"), _upload("b.csv", b"b")],
            data_dir=pg_env["data_dir"],
        )
    assert ledger_files.load_receipt(batch_id, pg_env["data_dir"]) is None
    assert not (pg_env["data_dir"] / "archive" / batch_id).exists()
    assert list((pg_env["data_dir"] / "staging").glob("*.tmp")) == []


# --- reference --------------------------------------------------------------

def test_reference_manifest_and_digest_are_stable(pg_env):
    root = pg_env["data_dir"] / "reference"
    root.mkdir(parents=True, mode=0o700)
    (root / "reference.csv").write_bytes(b"a,b\n")
    (root / "notes.txt").write_bytes(b"ignored")  # 非 reference 后缀，不列出

    manifest = ledger_files.reference_manifest(pg_env["data_dir"])
    assert [item["path"] for item in manifest["files"]] == ["reference.csv"]
    digest = pg_contracts.reference_manifest_digest(manifest)
    assert digest == pg_contracts.reference_manifest_digest(
        ledger_files.reference_manifest(pg_env["data_dir"])
    )


def test_reference_content_is_read_by_digest(pg_env):
    root = pg_env["data_dir"] / "reference"
    root.mkdir(parents=True, mode=0o700)
    raw = b"a,b\n1,2\n"
    (root / "reference.csv").write_bytes(raw)
    digest = ledger_files.sha256_bytes(raw)
    found = ledger_files.read_reference(digest, pg_env["data_dir"])
    assert found["content"] == raw
    assert ledger_files.read_reference("0" * 64, pg_env["data_dir"]) is None
    with pytest.raises(pg_contracts.ContractError):
        ledger_files.read_reference("not-a-digest", pg_env["data_dir"])


# --- 批次记录（同事务） -------------------------------------------------------

def test_batch_record_commits_with_business_change_in_one_transaction(pg_env):
    batch_id = _batch_id()
    receipt = ledger_files.publish_batch(
        batch_id, "import", "mac", "test-1", [_upload()], data_dir=pg_env["data_dir"]
    )
    with pgdb.pg_transaction() as conn:
        conn.execute(
            "INSERT INTO ledger_tx(dedup,source,direction,amount) "
            "VALUES('synthetic:1','手动','支出',1)"
        )
        ledger_files.commit_batch_record(conn, receipt, {"inserted": 1})

    with pgdb.pg_transaction(readonly=True) as conn:
        row = ledger_files.find_batch_record(conn, batch_id)
        assert row["result_json"] == {"inserted": 1}
        assert row["request_sha256"] == receipt["request_sha256"]
        assert conn.execute("SELECT COUNT(*) c FROM ledger_tx").fetchone()["c"] == 1


def test_rollback_removes_both_business_change_and_batch_record(pg_env):
    """提案 §3.2：归档成功、PG 事务回滚 → 保留 archived 未提交，账本里什么都没有。"""
    batch_id = _batch_id()
    receipt = ledger_files.publish_batch(
        batch_id, "import", "mac", "test-1", [_upload()], data_dir=pg_env["data_dir"]
    )

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with pgdb.pg_transaction() as conn:
            conn.execute(
                "INSERT INTO ledger_tx(dedup,source,direction,amount) "
                "VALUES('synthetic:2','手动','支出',1)"
            )
            ledger_files.commit_batch_record(conn, receipt, {"inserted": 1})
            raise Boom()

    with pgdb.pg_transaction(readonly=True) as conn:
        assert ledger_files.find_batch_record(conn, batch_id) is None
        assert conn.execute("SELECT COUNT(*) c FROM ledger_tx").fetchone()["c"] == 0
    # 原件与回执保留为「已归档、未提交」，同 ID 可重试
    assert ledger_files.load_receipt(batch_id, pg_env["data_dir"]) is not None


def test_duplicate_batch_record_is_a_conflict_not_a_silent_overwrite(pg_env):
    batch_id = _batch_id()
    receipt = ledger_files.publish_batch(
        batch_id, "import", "mac", "test-1", [_upload()], data_dir=pg_env["data_dir"]
    )
    with pgdb.pg_transaction() as conn:
        ledger_files.commit_batch_record(conn, receipt, {"inserted": 1})
    with pytest.raises(pgdb.ConcurrentConflict):
        with pgdb.pg_transaction() as conn:
            ledger_files.commit_batch_record(conn, receipt, {"inserted": 2})
    with pgdb.pg_transaction(readonly=True) as conn:
        assert ledger_files.find_batch_record(conn, batch_id)["result_json"] == {
            "inserted": 1
        }


def test_stored_result_survives_the_unknown_commit_path(pg_env):
    """COMMIT 结果未知时的恢复判据：按 batch_id 查记录返回原 result，不重复写账。"""
    batch_id = _batch_id()
    receipt = ledger_files.publish_batch(
        batch_id, "import", "mac", "test-1", [_upload()], data_dir=pg_env["data_dir"]
    )
    with pgdb.pg_transaction() as conn:
        ledger_files.commit_batch_record(conn, receipt, {"inserted": 3, "duplicate": 1})

    with pgdb.pg_transaction(readonly=True) as conn:
        row = ledger_files.find_batch_record(conn, batch_id)
    assert ledger_files.stored_result(row) == {"inserted": 3, "duplicate": 1}


def test_list_batches_reports_orphans_without_reading_content(pg_env):
    batch_id = _batch_id()
    ledger_files.publish_batch(
        batch_id, "import", "mac", "test-1", [_upload()], data_dir=pg_env["data_dir"]
    )
    listing = ledger_files.list_batches(pg_env["data_dir"])
    assert [item["batch_id"] for item in listing] == [batch_id]
    assert listing[0]["status"] == pg_contracts.BATCH_STATUS_ARCHIVED
    assert listing[0]["files"] == 1
    assert "content" not in json.dumps(listing)


def _manifest(name, sha, generated_at):
    return {"version": pg_contracts.REFERENCE_MANIFEST_VERSION,
            "generated_at": generated_at,
            "files": [{"path": name, "sha256": sha, "bytes": 1,
                       "format_version": "reference-file-1"}]}


def test_manifest_cache_rejects_content_that_does_not_match_its_key(pg_env, tmp_path):
    """§11.5.3：缓存的**内容摘要必须等于缓存键**。

    「把 R2 的合法 manifest 放在 R1 缓存名下」在修复前会被原样接受，于是记录为 R1
    的批次被固定到 R2；修复后按未命中处理（回源核验），并且回源拿回正版时顺手修好
    缓存。不写成「账目已被错改」：后续归档请求的摘要检查仍可能以 409 拦下。
    """
    cache = tmp_path / "reference-cache"
    ledger_files._private_dir(ledger_files._manifest_cache_dir(cache))
    r1 = _manifest("categories.md", "a" * 64, "2026-09-01T00:00:00+00:00")
    r2 = _manifest("categories.md", "b" * 64, "2026-09-10T00:00:00+00:00")
    digest1 = pg_contracts.reference_manifest_digest(r1)
    digest2 = pg_contracts.reference_manifest_digest(r2)
    target = ledger_files._manifest_cache_dir(cache) / f"{digest1}.json"
    target.write_text(json.dumps(r2, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    assert ledger_files._cached_manifest(digest1, cache) is None          # 错配 → 未命中
    assert ledger_files._cached_manifest(digest2, cache) is None          # 键本来就不存在

    # 回源拿到正版后写回：同键缓存被修好，而不是继续留着错的那份
    ledger_files._remember_manifest(r1, cache)
    assert pg_contracts.reference_manifest_digest(
        ledger_files._cached_manifest(digest1, cache)) == digest1
    # 幂等：内容对得上就原样返回，不重复改写
    before = target.stat().st_mtime_ns
    ledger_files._remember_manifest(r1, cache)
    assert target.stat().st_mtime_ns == before


def test_pin_for_batch_repairs_wrong_version_cache(tmp_path, monkeypatch):
    """§11.5.3 的整条重试路径：缓存错配 → 未命中 → 回源核验 → 修好缓存 / 或拒绝。

    复核 B 的盲区探测：把 `_pin_for_batch` 打成抛异常时旧用例仍然全绿——也就是
    「回源后重修缓存」这一行从来没被覆盖。这里把整条路径跑起来。
    """
    cache = tmp_path / "reference-cache"
    ledger_files._private_dir(ledger_files._manifest_cache_dir(cache))
    r1 = _manifest("categories.md", "a" * 64, "2026-09-01T00:00:00+00:00")
    r2 = _manifest("categories.md", "b" * 64, "2026-09-10T00:00:00+00:00")
    digest1 = pg_contracts.reference_manifest_digest(r1)
    target = ledger_files._manifest_cache_dir(cache) / f"{digest1}.json"
    # 把 R2 的合法 manifest 放在 R1 的缓存名下（错配注入）
    target.write_text(json.dumps(r2, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    # 回源拿到的恰好仍是记录版本 → 固定 R1 并把缓存修回正确内容
    monkeypatch.setattr(ledger_files, "reference_manifest", lambda data_dir=None: r1)
    pinned = ledger_files._pin_for_batch("batch-fix", {"reference_manifest_sha256": digest1},
                                         data_dir=tmp_path, remote=None, cache_dir=cache)
    assert pinned.manifest_sha256 == digest1
    assert pg_contracts.reference_manifest_digest(
        json.loads(target.read_text(encoding="utf-8"))) == digest1

    # R07：刚才成功恢复的 R1 已提升为 authority 持久历史；缓存损坏后仍能恢复。
    target.write_text(json.dumps(r2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    monkeypatch.setattr(ledger_files, "reference_manifest", lambda data_dir=None: r2)
    recovered = ledger_files._pin_for_batch(
        "batch-history", {"reference_manifest_sha256": digest1},
        data_dir=tmp_path, remote=None, cache_dir=cache,
    )
    assert recovered.manifest_sha256 == digest1

    # 没有历史、缓存错配且当前是别的版本 → 拒绝，不静默改用最新版。
    target.write_text(json.dumps(r2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    with pytest.raises(ledger_files.ArchiveError, match="无法找回该批次固定的 reference 版本"):
        ledger_files._pin_for_batch("batch-miss", {"reference_manifest_sha256": digest1},
                                    data_dir=tmp_path / "missing-history", remote=None,
                                    cache_dir=cache)


def test_corrupt_manifest_is_reported_not_silently_treated_as_new(pg_env):
    batch_id = _batch_id()
    ledger_files.publish_batch(
        batch_id, "import", "mac", "test-1", [_upload()], data_dir=pg_env["data_dir"]
    )
    manifest = pg_env["data_dir"] / "archive" / batch_id / "manifest.json"
    manifest.write_text("{not json", encoding="utf-8")
    with pytest.raises(ledger_files.ArchiveError):
        ledger_files.load_receipt(batch_id, pg_env["data_dir"])
    with pytest.raises(ledger_files.ArchiveError):
        ledger_files.publish_batch(
            batch_id, "import", "mac", "test-1", [_upload()], data_dir=pg_env["data_dir"]
        )
