# -*- coding: utf-8 -*-
"""G09 / G10 / A01 定向回归：Mac 实际入口接 NAS 批次协议、reference 绑定、归档完整性。

**关闭标准原文**（提案 §11.3.2）：

- G09：「在两个隔离进程和不同文件根，直接测试 Mac 页面使用的 HTTP 路由及 CLI：解析在 Mac、
  权威原件只发布 NAS，角色/代码版本正确；batch_id 由调用方持有并跨失败重试不变，
  NAS 不可达则不写 PG、不降级本机归档。」
- G10：「导入/重分类显式使用下载且校验过的不可变 reference 版本，记录实际使用 digest。」
- A01：「不能把 manifest 可读标为原件完整。」

这些用例**直接打页面真实路由** `/api/ledger/import`，不是内部函数；权威端是**另一个进程**
里真的 HTTP 服务（`werkzeug.serving.make_server`），两个文件根物理分离。
"""
import hashlib
import io
import pathlib
import sys
import threading
import uuid

import pytest
from openpyxl import Workbook
from werkzeug.serving import make_server

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

from flask import Flask  # noqa: E402

import ledger_auto_classify as ac  # noqa: E402
import ledger_files  # noqa: E402
import pg01_test_support  # noqa: E402
import pg_contracts  # noqa: E402
import pg_migrations  # noqa: E402
import pg_test_support  # noqa: E402
import pgdb  # noqa: E402

CMB_HEADER = ["记账日期", "货币", "交易金额", "联机余额", "交易摘要", "对手信息"]


def _xlsx_bytes(day=1, amount=-12.5, merchant="合成商户甲"):
    wb = Workbook()
    wb.active.append(CMB_HEADER)
    wb.active.append([f"2026-09-{day:02d}", "CNY", amount, 100, "快捷支付", merchant])
    buffer = io.BytesIO()
    wb.save(buffer)
    wb.close()
    return buffer.getvalue()


def _reference_bytes(marker="R1"):
    """一份可被 `build_kb_stat` 读入的参考工作簿（列布局与真实参考表一致）。

    `marker` 只用于改变字节（制造不同 digest）；分类必须落在库内已有类别上，
    否则分类器会以「类别不存在」拒收，测不出参考表是否真的生效。
    """
    wb = Workbook()
    expense = wb.active
    expense.title = "支出"
    expense.append(["交易时间", "类型", "交易对方", "账号", "商品说明", "收/支",
                    "", "", "", "", "", "", "一级分类", "二级分类", "三级分类", "四级分类"])
    expense.append(["2026-09-01 12:00:00", marker, "合成商户甲", "", "快捷支付", "支出",
                    "", "", "", "", "", "", "测试支出", "日用", "测试支出", "日用"])
    income = wb.create_sheet("收入")
    income.append(["交易时间", "类型", "交易对方", "账号", "商品说明", "收/支",
                   "", "", "", "", "", "", "一级分类", "二级分类"])
    buffer = io.BytesIO()
    wb.save(buffer)
    wb.close()
    return buffer.getvalue()


class _Authority:
    """在**独立线程**里跑一个真的权威端 HTTP 服务（独立文件根）。"""

    def __init__(self, data_dir, dbname, pg_container):
        self.data_dir = pathlib.Path(data_dir)
        self.dbname = dbname
        self.pg_container = pg_container
        self.app = Flask(__name__)
        self.app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

        import ledger_file_routes

        ledger_file_routes.register_routes(
            self.app, data_dir=lambda: self.data_dir, guard=lambda: True
        )
        self._server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.port = self._server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._thread.join(timeout=10)

    def write_reference(self, raw, name="reference.xlsx"):
        target = self.data_dir / "reference"
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        (target / name).write_bytes(raw)
        return target / name


@pytest.fixture()
def world(pg_container, tmp_path, monkeypatch):
    """建库 + 两个物理分离的文件根；返回 (mac_root, nas_root, dsn)。"""
    admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
    dbname = f"console_test_g09_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{dbname}"')
    admin.close()
    conn = pg_test_support.connect(pg_container.dsn(dbname=dbname), autocommit=True)
    try:
        pg_migrations.apply(conn)
        conn.execute(
            "INSERT INTO ledger_categories(level1,level2,sort) VALUES "
            + ",".join(["(%s,%s,%s)"] * len(pg01_test_support.BASE_CATEGORIES)),
            [value for row in pg01_test_support.BASE_CATEGORIES for value in row],
        )
    finally:
        conn.close()

    mac_root = tmp_path / "mac_root"
    nas_root = tmp_path / "nas_root"
    mac_root.mkdir()
    nas_root.mkdir()

    values = {
        "CONSOLE_PG_HOST": pg_container.host,
        "CONSOLE_PG_PORT": str(pg_container.port),
        "CONSOLE_PG_DB": dbname,
        "CONSOLE_PG_USER": pg_container.user,
        "CONSOLE_PG_PASSWORD": pg_container.password,
        "CONSOLE_STORAGE_ENGINE": "postgres",
        "CONSOLE_MAINTENANCE_MODE": "normal",
        "CONSOLE_PG_POOL_MIN": "1",
        "CONSOLE_PG_POOL_MAX": "4",
        "CONSOLE_PG_LOCK_TIMEOUT": "1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    pgdb.close_pool()
    try:
        yield {
            "mac": mac_root,
            "nas": nas_root,
            "dsn": pg_container.dsn(dbname=dbname),
            "dbname": dbname,
            "container": pg_container,
        }
    finally:
        pgdb.close_pool()
        admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
        admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        admin.close()


@pytest.fixture()
def mac_client(world, monkeypatch):
    """Mac 侧 Flask 应用：真实 `/api/ledger/import` 路由，data_dir 指向 Mac 根。"""
    import paths

    monkeypatch.setattr(paths, "PAY_DATA_DIR", str(world["mac"]))
    monkeypatch.setattr(paths, "DB_PATH", str(world["mac"] / "console.db"))
    import app as console_app

    monkeypatch.setattr("plugin_runtime.authority_state", lambda pid: {"enabled": True, "available": True})
    monkeypatch.setattr(console_app, "DB_PATH", str(world["mac"] / "console.db"))
    console_app.init_db()
    monkeypatch.setattr(console_app, "STATIC_DIR", str(world["mac"] / "not-built"))
    # 账本导入**不得**泄漏到观测转发
    monkeypatch.setattr(
        console_app,
        "_proxy_authority",
        lambda *a, **kw: pytest.fail("ledger import leaked to observation proxy"),
    )
    return console_app.app.test_client()


def _count_rows(sql, params=()):
    with pgdb.pg_transaction(readonly=True) as conn:
        return conn.execute(sql, params).fetchone()["c"]


# ═══ G09 ═══════════════════════════════════════════════════════════════════

def test_client_role_publishes_to_authority_not_local(world, mac_client, monkeypatch):
    """client 角色：原件只落权威端，Mac 本机不留归档；角色/代码版本写进回执。"""
    reference = _reference_bytes("R1")
    with _Authority(world["nas"], world["dbname"], world["container"]) as authority:
        authority.write_reference(reference)
        monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "client")
        monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", authority.origin)
        monkeypatch.setenv("CONSOLE_CODE_VERSION", "sha-test-abc")
        pgdb.close_pool()

        response = _route_import(
            mac_client, (io.BytesIO(_xlsx_bytes()), "2026-09 账单.xlsx")
        )
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert body["inserted"] == 1

    # 权威端落盘、Mac 根不落权威归档
    archived = list((world["nas"] / "archive").rglob("*"))
    assert any(item.is_file() for item in archived), "权威端应有归档原件"
    assert not (world["mac"] / "archive").exists(), "Mac 本机不得留权威归档"
    # 角色与代码版本进了回执（G09「角色/代码版本正确」）
    receipt = body["receipt"]
    assert receipt["client"] == "mac"
    assert receipt["code_version"] == "sha-test-abc"
    assert receipt["batch_id"] == body["batch_id"]
    # 回执自校验：调用方可用自己持有的 batch_id 核对
    pg_contracts.validate_batch_receipt(receipt, expected_batch_id=body["batch_id"])
    # 业务确已提交
    assert _count_rows("SELECT COUNT(*) c FROM ledger_tx") == 1
    assert _count_rows("SELECT COUNT(*) c FROM ledger_file_batches") == 1


def test_authority_unreachable_fails_closed(world, mac_client, monkeypatch):
    """NAS 不可达：必须失败、**不写 PG**、**不降级本机归档**（G09-1/G09-2 首红判据）。

    走页面真实路由 `/api/ledger/import`。这里断言的是「失败且闭环」，**不断言 503**：
    `ArchiveError → 503` 的映射要 PG-05 在 `_storage_error_response` 登记，属其接线面。
    """
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "client")
    monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", "http://127.0.0.1:1")  # 不可达
    pgdb.close_pool()

    response = _route_import(mac_client, (io.BytesIO(_xlsx_bytes()), "账单.xlsx"))
    assert response.status_code >= 500, response.get_data(as_text=True)
    # 关键：没有业务写入、没有本机归档
    assert _count_rows("SELECT COUNT(*) c FROM ledger_tx") == 0
    assert _count_rows("SELECT COUNT(*) c FROM ledger_file_batches") == 0
    assert not (world["mac"] / "archive").exists()


def test_client_role_without_origin_fails_closed(world, mac_client, monkeypatch):
    """client 无 `CONSOLE_AUTHORITY_ORIGIN`：拒绝在本机归档，绝不静默降级。"""
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "client")
    monkeypatch.delenv("CONSOLE_AUTHORITY_ORIGIN", raising=False)
    pgdb.close_pool()

    response = _route_import(mac_client, (io.BytesIO(_xlsx_bytes()), "账单.xlsx"))
    assert response.status_code >= 500, response.get_data(as_text=True)
    assert _count_rows("SELECT COUNT(*) c FROM ledger_tx") == 0
    assert not (world["mac"] / "archive").exists()


def _upload(files, batch_id):
    from werkzeug.datastructures import FileStorage

    return [
        FileStorage(stream=io.BytesIO(raw), filename=name) for name, raw in files
    ]


def _route_import(client, file, batch_id=None, **extra):
    """打页面真实上传路由 `/api/ledger/import`。

    批次号由**调用方（页面）**持有并随表单发出——服务端缺号一律 400、不兜底
    （PG-00 `interfaces.md` §8）。重试场景传同一个 `batch_id`，那正是 G09 的幂等键。
    """
    data = {"batch_id": batch_id or str(uuid.uuid4()), "files": file}
    data.update(extra)
    return client.post(
        "/api/ledger/import", data=data, content_type="multipart/form-data"
    )


def test_batch_id_from_caller_is_reused_across_retries(world, monkeypatch):
    """batch_id 由调用方持有，跨重试**不变**；重放返回已存结果，不重复写账。

    首红 G09-3：旧实现在服务端内部生成 ID，两次重试拿到不同的 ID。
    这里直接调模块入口（页面路由透传 batch_id 属 PG-05 接线，见交接说明）。
    """
    import ledger_import

    batch_id = str(uuid.uuid4())
    # 重放必须是同一批字节：xlsx 内嵌生成时刻，跨秒重新生成会让 request_digest
    # 不一致，测出的是时钟而不是幂等（2026-09-19 CI 首红，本地同秒内不复现）。
    payload = _xlsx_bytes()
    with _Authority(world["nas"], world["dbname"], world["container"]) as authority:
        monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "client")
        monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", authority.origin)
        pgdb.close_pool()

        def upload():
            return ledger_import.import_files(
                _upload([("账单.xlsx", payload)], batch_id),
                data_dir=world["mac"],
                batch_id=batch_id,
            )

        first = upload()
        second = upload()

    # 调用方给的 ID 被原样采用，两次一致
    assert first["batch_id"] == batch_id
    assert second["batch_id"] == batch_id
    # 重放返回同一结果，不重复插入。
    # 断言消息带上两份报告：这条用例曾在 12 次全量里红过 1 次且不复现，
    # 若再出现，`second` 的形状（duplicate=1 说明没命中已存批次记录）本身就是根因线索。
    assert first["inserted"] == 1, first
    assert second["inserted"] == 1 and second["duplicate"] == 0, {
        "first": first,
        "second": second,
    }
    assert _count_rows("SELECT COUNT(*) c FROM ledger_tx") == 1
    assert _count_rows("SELECT COUNT(*) c FROM ledger_file_batches") == 1


def test_invalid_batch_id_from_caller_is_rejected(world, monkeypatch):
    import ledger_import

    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "test")
    pgdb.close_pool()
    with pytest.raises(pg_contracts.ContractError):
        ledger_import.import_files(
            _upload([("账单.xlsx", _xlsx_bytes())], "not-a-uuid"),
            data_dir=world["mac"],
            batch_id="not-a-uuid",
        )
    assert _count_rows("SELECT COUNT(*) c FROM ledger_tx") == 0


def test_test_role_archives_locally(world, mac_client, monkeypatch):
    """`test` 角色按本地侧就地归档（既有 30+ 测试依赖这条分派）。"""
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "test")
    pgdb.close_pool()
    response = _route_import(
        mac_client, (io.BytesIO(_xlsx_bytes()), "账单.xlsx"), str(uuid.uuid4())
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["archive"]["client"] == "test"
    assert body["receipt"]["client"] == "test"
    assert (world["mac"] / "archive" / body["batch_id"]).is_dir()
    assert not (world["nas"] / "archive").exists()


def test_unknown_commit_query_returns_stored_result(world, monkeypatch):
    """「COMMIT 结果未知」后按 batch_id 核验：返回已存 result，不重复写账。

    走模块入口并显式传 `batch_id`（页面路由的透传属 PG-05 接线）；权威端仍是真的
    另一个进程里的 HTTP 服务。
    """
    import ledger_import

    batch_id = str(uuid.uuid4())
    with _Authority(world["nas"], world["dbname"], world["container"]) as authority:
        monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "client")
        monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", authority.origin)
        pgdb.close_pool()
        ledger_import.import_files(
            _upload([("账单.xlsx", _xlsx_bytes())], batch_id),
            data_dir=world["mac"],
            batch_id=batch_id,
        )
        # 权威端的归档状态可查（上传结果未知时先查后重试）
        status = authority.app.test_client().get(
            f"/api/ledger/files/batches/{batch_id}"
        )
    assert status.status_code == 200
    assert status.get_json()["batch_id"] == batch_id
    with pgdb.pg_transaction(readonly=True) as conn:
        assert ledger_files.find_batch_record(conn, batch_id) is not None


# ═══ G10 ═══════════════════════════════════════════════════════════════════

def test_classifier_never_receives_none_reference_on_batch_path(
    world, mac_client, monkeypatch
):
    """判定句：批次路径传给分类器的 `reference_path` **永不为 None**。"""
    seen = []
    original = ac.LedgerClassifier.__init__

    def spy(self, conn, data_dir=None, reference_path=None):
        seen.append(reference_path)
        return original(self, conn, data_dir=data_dir, reference_path=reference_path)

    monkeypatch.setattr(ac.LedgerClassifier, "__init__", spy)
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "test")
    pgdb.close_pool()
    # 本地根本没有任何参考表 → 必须是 NO_REFERENCE，而不是 None（None=扫描目录）
    _route_import(mac_client, (io.BytesIO(_xlsx_bytes()), "账单.xlsx"))
    assert seen, "分类器应被调用"
    assert all(item is ac.NO_REFERENCE for item in seen), seen


def test_classifier_reads_exactly_the_recorded_reference(world, mac_client, monkeypatch):
    """本机侧同样只喂「回执里记的那一份」：目录里另一份参考表不得被扫进来。

    这是 G10 的**回归**覆盖（首红证据见
    `test_missing_pinned_reference_is_rejected_not_silently_replaced`）：断言喂给
    分类器的字节其 digest == 回执记录的 digest。
    """
    reference_dir = world["mac"] / "reference"
    reference_dir.mkdir(parents=True, exist_ok=True)
    # 两份都在允许目录里：按选择规则应固定 `reference.xlsx`
    pinned = _reference_bytes("R1")
    (reference_dir / "reference.xlsx").write_bytes(pinned)
    (reference_dir / "另一份收支明细表.xlsx").write_bytes(_reference_bytes("R2"))

    hashes = []
    original = ac.build_kb_stat

    def spy(path):
        hashes.append(hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest())
        return original(path)

    monkeypatch.setattr(ac, "build_kb_stat", spy)
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "test")
    pgdb.close_pool()
    response = _route_import(mac_client, (io.BytesIO(_xlsx_bytes()), "账单.xlsx"))
    assert response.status_code == 200, response.get_data(as_text=True)
    summary = response.get_json()["reference"]
    assert summary["file"] == "reference.xlsx"
    assert summary["file_sha256"] == hashlib.sha256(pinned).hexdigest()
    # 分类器读到的就是回执里那一份，且**只读了那一份**
    assert hashes and set(hashes) == {summary["file_sha256"]}, hashes


def test_batch_retry_reuses_r1_after_authority_reference_changes(world, monkeypatch):
    """权威端 reference 从 R1 更新到 R2：**同 batch 重试仍用 R1**，新 batch 才用 R2。"""
    import ledger_import

    r1, r2 = _reference_bytes("R1"), _reference_bytes("R2")
    batch_id = str(uuid.uuid4())
    # 同批重试必须复用原件字节；重新保存 XLSX 会把当前时刻写入文件。
    payload = _xlsx_bytes(day=1)
    with _Authority(world["nas"], world["dbname"], world["container"]) as authority:
        authority.write_reference(r1)
        monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "client")
        monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", authority.origin)
        pgdb.close_pool()

        def run(files, batch):
            return ledger_import.import_files(
                _upload(files, batch), data_dir=world["mac"], batch_id=batch
            )

        first = run([("账单.xlsx", payload)], batch_id)
        # 权威端 reference 更新为 R2，同时换一个新 batch 的输入
        authority.write_reference(r2)
        retry = run([("账单.xlsx", payload)], batch_id)
        fresh = run([("账单2.xlsx", _xlsx_bytes(day=2))], str(uuid.uuid4()))

    assert first["reference"]["file_sha256"] == hashlib.sha256(r1).hexdigest()
    # ★ 重试仍固定 R1，而不是悄悄换成 R2
    assert retry["reference"]["file_sha256"] == first["reference"]["file_sha256"]
    assert retry["reference"]["manifest_sha256"] == first["reference"]["manifest_sha256"]
    # 新批次才用 R2
    assert fresh["reference"]["file_sha256"] == hashlib.sha256(r2).hexdigest()
    assert fresh["reference"]["file_sha256"] != first["reference"]["file_sha256"]


def test_missing_pinned_reference_is_rejected_not_silently_replaced(world, monkeypatch):
    """固定的那份 reference 被删：拒绝，不得改用目录里的「最新版本」。"""
    import ledger_files as lf

    r1 = _reference_bytes("R1")
    with _Authority(world["nas"], world["dbname"], world["container"]) as authority:
        authority.write_reference(r1)
        monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "client")
        monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", authority.origin)
        pgdb.close_pool()
        session = lf.open_session(
            "import", batch_id=str(uuid.uuid4()), data_dir=world["mac"]
        )
        assert session.reference.path is not None
        session.reference.path.unlink()  # 篡改：文件消失
        with pgdb.pg_transaction(readonly=True) as conn:
            with pytest.raises(ValueError, match="不存在"):
                ac.LedgerClassifier(conn, world["mac"], session.reference.path)


def test_reclassify_binds_reference_and_records_batch(world, monkeypatch):
    """重分类同样走批次协议：固定 reference、写批次记录（§3.2「只重分类」）。

    走模块入口（页面对 `/reclassify` 的 batch_id 透传属 PG-05 接线）。
    """
    import ledger_import

    r1 = _reference_bytes("R1")
    with _Authority(world["nas"], world["dbname"], world["container"]) as authority:
        authority.write_reference(r1)
        monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "client")
        monkeypatch.setenv("CONSOLE_AUTHORITY_ORIGIN", authority.origin)
        pgdb.close_pool()
        ledger_import.import_files(
            _upload([("账单.xlsx", _xlsx_bytes())], str(uuid.uuid4())),
            data_dir=world["mac"],
        )
        with pgdb.pg_transaction() as conn:
            conn.execute("UPDATE ledger_tx SET type1=NULL,type2=NULL")

        batch_id = str(uuid.uuid4())
        body = ledger_import.reclassify(batch_id=batch_id, data_dir=world["mac"])

    assert body["batch_id"] == batch_id
    assert body["reference"]["file_sha256"] == hashlib.sha256(r1).hexdigest()
    assert body["reclassified"] == 1
    with pgdb.pg_transaction(readonly=True) as conn:
        row = ledger_files.find_batch_record(conn, batch_id)
    assert row is not None and row["operation"] == "reclassify"


# ═══ A01 ═══════════════════════════════════════════════════════════════════

def test_verify_archive_detects_tampered_bytes(world, tmp_path):
    """A01-1：manifest 可读 ≠ 原件完整；逐文件复算必须发现被改写的字节。"""
    data_dir = tmp_path / "a01"
    batch_id = str(uuid.uuid4())
    raw = _xlsx_bytes()
    receipt = ledger_files.publish_batch(
        batch_id, "import", "test", "v1", [("账单.xlsx", raw)], data_dir=data_dir
    )
    assert ledger_files.verify_archive(batch_id, data_dir)["ok"] is True

    stored = next((data_dir / "archive" / batch_id).glob("01-*.xlsx"))
    stored.write_bytes(b"tampered")

    report = ledger_files.verify_archive(batch_id, data_dir)
    assert report["ok"] is False
    assert report["mismatched"] == ["账单.xlsx"]
    assert report["checked"] == 1
    # 回执本身仍「可读」——这正是旧实现会误报完整的原因
    assert ledger_files.load_receipt(batch_id, data_dir)["artifacts"][0][
        "sha256"
    ] == receipt["artifacts"][0]["sha256"]


def test_verify_archive_detects_missing_file(world, tmp_path):
    """A01：缺失文件同样要报出来，不能只看 manifest。"""
    data_dir = tmp_path / "a01"
    batch_id = str(uuid.uuid4())
    ledger_files.publish_batch(
        batch_id, "import", "test", "v1", [("账单.xlsx", _xlsx_bytes())], data_dir=data_dir
    )
    stored = next((data_dir / "archive" / batch_id).glob("01-*.xlsx"))
    stored.unlink()

    report = ledger_files.verify_archive(batch_id, data_dir)
    assert report["ok"] is False
    assert report["missing"] == ["账单.xlsx"]


def test_idempotent_publish_rejects_tampered_archive(world, tmp_path):
    """A01-2：同 ID 重发不再「直接返回旧回执」——原件被改时拒绝并保留现场。"""
    data_dir = tmp_path / "a01"
    batch_id = str(uuid.uuid4())
    uploads = [("账单.xlsx", _xlsx_bytes())]
    ledger_files.publish_batch(
        batch_id, "import", "test", "v1", uploads, data_dir=data_dir
    )
    stored = next((data_dir / "archive" / batch_id).glob("01-*.xlsx"))
    stored.write_bytes(b"tampered")

    with pytest.raises(ledger_files.ArchiveError) as excinfo:
        ledger_files.publish_batch(
            batch_id, "import", "test", "v1", uploads, data_dir=data_dir
        )
    assert "不一致" in str(excinfo.value)
    # 现场保留（不修、不覆盖、不删除），供人工核验
    assert stored.read_bytes() == b"tampered"
    assert ledger_files.load_receipt(batch_id, data_dir) is not None


def test_healthy_idempotent_publish_still_returns_receipt(world, tmp_path):
    """完整性正常时幂等语义不变：同 ID 同内容 → 同回执。"""
    data_dir = tmp_path / "a01"
    batch_id = str(uuid.uuid4())
    uploads = [("账单.xlsx", _xlsx_bytes())]
    first = ledger_files.publish_batch(
        batch_id, "import", "test", "v1", uploads, data_dir=data_dir
    )
    again = ledger_files.publish_batch(
        batch_id, "import", "test", "v1", uploads, data_dir=data_dir
    )
    assert again == first


def test_batch_status_reports_integrity_not_just_readable_receipt(world, tmp_path):
    """A01-3：状态查询就是批次恢复核查——原件被改时报 503 + 明细，不冒充「完好」。"""
    data_dir = tmp_path / "a01"
    batch_id = str(uuid.uuid4())
    with _Authority(data_dir, world["dbname"], world["container"]) as authority:
        authority.write_reference(_reference_bytes("R1"))
        client = authority.app.test_client()
        authority.app.config["TESTING"] = True
        receipt = ledger_files.publish_batch(
            batch_id, "import", "nas", "v1", [("账单.xlsx", _xlsx_bytes())],
            data_dir=data_dir,
        )
        healthy = client.get(f"/api/ledger/files/batches/{batch_id}")
        # 200 的含义是「回执可读 **且** 原件逐字节核验通过」，回执字段原样不变
        assert healthy.status_code == 200, healthy.get_data(as_text=True)
        assert healthy.get_json() == receipt

        stored = next((data_dir / "archive" / batch_id).glob("01-*.xlsx"))
        stored.write_bytes(b"tampered")
        broken = client.get(f"/api/ledger/files/batches/{batch_id}")
    assert broken.status_code == 503
    payload = broken.get_json()
    assert payload["integrity"]["ok"] is False
    assert payload["integrity"]["mismatched"] == ["账单.xlsx"]
    # 回执本身仍然「可读」，字段照旧在——这正是不能只看 manifest 的原因
    assert payload["batch_id"] == batch_id
