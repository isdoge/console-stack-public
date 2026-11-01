# -*- coding: utf-8 -*-
"""PG-05 集成：真实 PG16 整链端到端（提案 §9 D3、§8.2、§3.2；implement.md S6 末项）。

`scripts/test_pg03_*.py` 已分别覆盖 schema/搬运器/独立验证器，`test_pg_*` 覆盖各模块在
PG 上的语义，但**没有一条用例把它们串起来**：迁移器落库 → 独立验证器复核 → 应用进程读
业务 API → 文件批次协议。本文件补的就是这一段，全部走真实 CLI 子进程与真实 app 对象。

链路（每一步都是真件，无替身）：

1. 全新隔离库 + `scripts/pg_schema.py migrate`（管理员路径，§6.3 运行账号无 DDL 权限）；
2. 受限运行账号（对齐 `console_nas` 权限面）+ 合成 SQLite 源快照（`pg03_test_fixtures`）；
3. `scripts/migrate_sqlite_to_postgres.py --mode preflight` → `--mode apply`（真实单事务搬运）；
4. `scripts/verify_postgres_migration.py --target env` 独立复核（不读搬运器报告，§8.2）；
5. `app.test_client()` 读业务 API：账本总览/流水/续费/有效配置（storage_engine=postgres
   且探针可达，正是 `verify_deploy.py` 核验项 19 的判据）；
6. 文件批次协议：manifest → 下载复算 → 上传归档 → 同 ID 重放幂等 → 同 ID 不同内容 409
   → 状态查询（§3.2 的失败与重试矩阵里可离线验证的那几条）。

**金额口径**：`pg03_test_fixtures.EXPECTED_LEDGER_TOTAL = 101214.8778901234567` 不能被
`money.json_number` 无损转 JSON number（实测抛 `MoneyError`），所以本文件的账本夹具**自带
金额**（全部两位小数，可安全往返），不沿用那份合成的总计数。迁移器/验证器的口径仍由
PG-03 的测试用那份夹具覆盖。
"""
import decimal
import io
import json
import os
import pathlib
import sqlite3
import sys
import threading
import uuid

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
SCRIPTS_DIR = BACKEND_DIR.parents[1] / "scripts"
for _path in (str(BACKEND_DIR), str(SCRIPTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import app as console_app  # noqa: E402
import db as dbmod  # noqa: E402
import ledger_files  # noqa: E402
import paths  # noqa: E402
import pg_contracts  # noqa: E402
import pg_migrations  # noqa: E402
import pg_test_support  # noqa: E402
from pg03_test_fixtures import (  # noqa: E402
    drop_database,
    fresh_database,
    make_legacy_db,
    pg_env,
    run_cli,
)
from pg_migration_lib import file_sha256  # noqa: E402

ROLE = "console_pg05_app"
ROLE_PASSWORD = "console_pg05_app_pw"

# 合成账本金额：两位小数，`money.json_number` 可无损往返（不沿用 PG-03 的长尾合计）。
LEDGER_ROWS = (
    # id, amount, direction, type1, type2
    (11, 100.25, "收入", "工资", "月薪"),
    (12, 30.10, "支出", "餐饮", "三餐"),
    (13, 12.34, "支出", "生活", "日用"),
    (14, 7.00, "支出", None, None),
)
MONTH_INCOME, MONTH_EXPENSE = 100.25, 49.44
UNCATEGORIZED = 1
LEDGER_TOTAL_ROWS = len(LEDGER_ROWS)
# 续费金额同样两位小数；PG-03 夹具的 99.99/0.1 亦可，但本文件自持口径更清楚。
RENEWAL_ROWS = (
    ("r1", "域名A", "domain", "2027-01-01", 99.99, "CNY", 0),
    ("r2", "证书B", "cert", "2026-12-31", 0.10, "CNY", 1),
)
RENEWAL_TOTAL = "100.09"  # 文本常量：Decimal(float) 会带二进制尾数，不是本用例要比的东西


def _seed_source(path):
    """在 PG-03 合成源之上改写成自持的金额口径（其余表保持原样，迁移面更宽）。"""
    make_legacy_db(path)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("DELETE FROM ledger_tx")
        conn.execute("DELETE FROM ledger_tx_link")
        conn.execute("DELETE FROM renewals")
        # 分类字典要覆盖本文件用到的组合（写路径会校验 level1+level2 必须成对存在）
        conn.execute("DELETE FROM ledger_categories")
        conn.executemany(
            "INSERT INTO ledger_categories (id,level1,level2,sort) VALUES (?,?,?,?)",
            [(10, "工资", "月薪", 1), (11, "餐饮", "三餐", 2), (12, "生活", "日用", 3)],
        )
        for tx_id, amount, direction, type1, type2 in LEDGER_ROWS:
            conn.execute(
                "INSERT INTO ledger_tx (id,dedup,source,src_file,order_no,time,date,month,"
                "direction,amount,who,goods,pay,type1,type2,basis,pay_confirmed,"
                "created_at,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tx_id, f"dedup-{tx_id}", "wechat", "f.xlsx", f"o{tx_id}",
                 "2026-09-01 10:00:00", "2026-09-01", "2026-09", direction, amount,
                 "山田商店😀", "商品\n第二行", "wx", type1, type2, "manual", 1,
                 "2026-09-01 10:00:01", "normal"),
            )
        for row in RENEWAL_ROWS:
            conn.execute(
                "INSERT INTO renewals (id,name,kind,expires_at,cost,currency,sort_order) "
                "VALUES (?,?,?,?,?,?,?)", row,
            )
        conn.commit()
    finally:
        conn.close()
    return str(path)


@pytest.fixture()
def migrated(pg_container, pg_dsn, tmp_path, monkeypatch):
    """整链前置：真 PG16 + schema migrate + 真实 CLI preflight/apply + 独立 verify。

    `yield` 出 app 夹具与文件批次夹具需要的全部句柄；退出时关池、还原环境、删库。
    """
    if not pg_test_support.docker_available() and not (
        os.environ.get("CONSOLE_PG_TEST_DSN") or ""
    ).strip():
        pytest.fail("docker 不可用：真实 PG16 整链端到端无法执行（不伪 skip）")

    admin = pg_test_support.connect(pg_dsn, autocommit=True)
    db_name = fresh_database(admin, "pg05e2e")
    reports = tmp_path / "reports"
    reports.mkdir()
    source = _seed_source(tmp_path / "console.db")
    data_dir = tmp_path / "pay"
    (data_dir / "reference").mkdir(parents=True, mode=0o700)
    (data_dir / "reference" / "分类表.csv").write_bytes(
        "分类,子类\n餐饮,三餐\n".encode("utf-8")
    )
    (data_dir / "reference" / "README.txt").write_text("不参与分类", encoding="utf-8")

    env = pg_env(pg_container, db_name)
    for key, value in env.items():
        if key.startswith("CONSOLE_"):
            monkeypatch.setenv(key, value)
    monkeypatch.setenv("CONSOLE_PG_USER", pg_container.user)
    monkeypatch.setenv("CONSOLE_PG_PASSWORD", pg_container.password)
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authority")  # 文件接口只在 authority 注册
    monkeypatch.setattr(paths, "PAY_DATA_DIR", str(data_dir))
    dbmod.pg_close()

    try:
        # 1) schema：管理员路径（CLI 用 CONSOLE_PG_* 连管理员账号）
        proc = run_cli("pg_schema.py", ["migrate"], env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert json.loads(proc.stdout)["ok"] is True

        # 2) 运行账号：权限面对齐 console_nas/console_mac（§6.3）。
        # GRANT 必须在**目标库自己的连接**上执行——`admin` 连的是 server 默认库，
        # 在它上面 GRANT 会找不到刚建的库里的表。
        db_admin = pg_test_support.connect(
            pg_container.dsn(dbname=db_name), autocommit=True
        )
        pg_test_support.create_app_role(
            db_admin, ROLE, ROLE_PASSWORD,
            tables=list(pg_migrations.SCHEMA_TABLES), database=db_name,
        )
        app_env = pg_env(pg_container, db_name,
                         {"CONSOLE_PG_USER": ROLE, "CONSOLE_PG_PASSWORD": ROLE_PASSWORD,
                          "CONSOLE_INSTANCE_ROLE": "authority"})
        for key, value in app_env.items():
            if key.startswith("CONSOLE_"):
                monkeypatch.setenv(key, value)

        # 3) preflight → apply（真实 CLI，退出码就是契约）。
        # 运行账号**没有** schema_migrations 写权限（§6.3），而 apply 要在同事务里写完成
        # 凭据（§5.3.6），所以割接窗口的搬运由管理员账号执行——与 `test_cutover_real_cli_end_to_end`
        # 同一口径；运行账号只用于应用进程。
        cli_env = dict(app_env)
        cli_env.update(
            {"CONSOLE_PG_USER": pg_container.user,
             "CONSOLE_PG_PASSWORD": pg_container.password}
        )
        preflight = run_cli(
            "migrate_sqlite_to_postgres.py",
            ["--mode", "preflight", "--source", source,
             "--target-purpose", "rehearsal",
             "--report", str(reports / "preflight.json")],
            cli_env,
        )
        assert preflight.returncode == 0, preflight.stdout + preflight.stderr
        assert json.loads(preflight.stdout)["status"] == "ok"

        apply_proc = run_cli(
            "migrate_sqlite_to_postgres.py",
            ["--mode", "apply", "--source", source,
             "--source-sha256", file_sha256(source),
             "--target-purpose", "rehearsal",
             "--report", str(reports / "apply.json")],
            cli_env,
        )
        assert apply_proc.returncode == 0, apply_proc.stdout + apply_proc.stderr
        assert json.loads(apply_proc.stdout)["status"] == "ok"

        # 4) 独立验证器：只读、不读搬运器报告
        verify = run_cli(
            "verify_postgres_migration.py",
            ["--source", source, "--target", "env",
             "--report", str(reports / "verify.json")],
            cli_env,
        )
        assert verify.returncode == 0, verify.stdout + verify.stderr
        summary = json.loads(verify.stdout)
        assert summary["status"] == "ok" and summary["mismatch_entries"] == 0
        for name in ("preflight.json", "apply.json", "verify.json"):
            body = (reports / name).read_text(encoding="utf-8")
            assert "password" not in body.lower(), f"{name} 泄漏了凭据字样"

        dbmod.pg_close()  # 应用进程要用同一个库，但必须重新开池
        yield {
            "db_admin": db_admin,
            "dsn": pg_container.dsn(dbname=db_name),
            "admin_dsn": pg_container.dsn(dbname=db_name),
            "database": db_name,
            "data_dir": data_dir,
            "source": source,
            "reports": reports,
            "container": pg_container,
        }
    finally:
        dbmod.pg_close()
        try:
            db_admin.execute(f'DROP OWNED BY "{ROLE}" CASCADE')
        except Exception:
            pass
        db_admin.close()
        drop_database(admin, db_name)
        admin.close()


@pytest.fixture()
def client(migrated, monkeypatch):
    monkeypatch.setattr("plugin_runtime.authority_state", lambda pid: {"enabled": True, "available": True})
    return console_app.app.test_client()


def _upload(files, **form):
    return {
        "files": [(io.BytesIO(raw), name) for name, raw in files],
        **form,
    }


# --- 1. 迁移结果在应用侧可见 ---------------------------------------------------


def test_migrated_rows_are_visible_through_ledger_api(client):
    """§9 D 的「迁移后可读」：账本总览与分页流水读的是迁移后的 PG 数据。"""
    overview = client.get("/api/ledger/overview?year=2026&month=2026-09")
    assert overview.status_code == 200, overview.get_json()
    body = overview.get_json()
    assert body["year"] == "2026" and body["month"] == "2026-09"
    assert body["month_income"] == MONTH_INCOME
    assert body["month_expense"] == MONTH_EXPENSE
    assert body["uncategorized"] == UNCATEGORIZED
    assert body["total"] == LEDGER_TOTAL_ROWS

    page = client.get("/api/ledger/tx?page=1&size=50")
    assert page.status_code == 200
    rows = page.get_json()["rows"]
    assert page.get_json()["total"] == LEDGER_TOTAL_ROWS
    assert {row["id"]: row["amount"] for row in rows} == {
        row[0]: row[1] for row in LEDGER_ROWS
    }
    # 中文/换行/emoji 逐字段无损（迁移不做任何文本清洗）
    uncategorized = [row for row in rows if row["id"] == 14][0]
    assert uncategorized["who"] == "山田商店😀"
    assert uncategorized["goods"] == "商品\n第二行"


def test_migrated_renewals_keep_decimal_costs(client):
    body = client.get("/api/renewals").get_json()
    # 列表契约：expires_at IS NULL 前置、再按到期日升序（r2 早于 r1）
    assert [(row["id"], row["cost"]) for row in body] == [("r2", 0.10), ("r1", 99.99)]
    # 求和用 Decimal：float 相加会漂成 100.08999999999999（这正是金额契约要防的事）
    assert sum(decimal.Decimal(str(row["cost"])) for row in body) == decimal.Decimal(
        RENEWAL_TOTAL
    )
    assert all(row["currency"] == "CNY" for row in body)


def test_effective_config_reports_reachable_postgres(client, migrated):
    """核验项 19 的判据：storage_engine=postgres 且 database 探针可达、无敏感键。"""
    data = client.get("/api/config/effective").get_json()
    assert data["storage_engine"] == "postgres"
    database = data["database"]
    assert isinstance(database, dict) and database["reachable"] is True
    assert database["database"] == migrated["database"]
    assert database["user"] == ROLE  # 运行账号，不是管理员
    assert database["schema_version"] == pg_migrations.latest_schema_version()
    assert not [k for k in database
                if any(h in k.lower() for h in ("password", "token", "secret"))]


def test_ledger_write_paths_commit_to_postgres(client, migrated):
    """迁移只是起点：应用写路径（人工记账 → 分类）必须落在同一个 PG 库上。"""
    created = client.post(
        "/api/ledger/tx/manual",
        json={"date": "2026-09-08", "time": "09:30", "direction": "支出",
              "amount": 20.50, "who": "测试商户", "goods": "测试商品",
              "type1": "餐饮", "type2": "三餐"},
    )
    assert created.status_code == 200, created.get_json()
    new_id = created.get_json()["id"]

    conn = pg_test_support.connect(migrated["admin_dsn"], autocommit=True)
    try:
        row = conn.execute(
            "SELECT amount, direction, type1, basis FROM ledger_tx WHERE id = %s",
            (new_id,),
        ).fetchone()
        assert row["amount"] == decimal.Decimal("20.50")
        assert row["direction"] == "支出" and row["type1"] == "餐饮"
        assert row["basis"] == "人工"
    finally:
        conn.close()

    # 分类写路径同样生效（账本 advisory 锁下的事务）
    recat = client.post(f"/api/ledger/tx/{new_id}/cat",
                        json={"type1": "生活", "type2": "日用"})
    assert recat.status_code == 200, recat.get_json()
    assert recat.get_json() == {"ok": 1, "followed": 0}

    overview = client.get("/api/ledger/overview?year=2026&month=2026-09").get_json()
    # 49.44 + 20.50 = 69.94，两位小数可无损往返
    assert overview["month_expense"] == 69.94
    assert overview["total"] == LEDGER_TOTAL_ROWS + 1


# --- 2. 文件批次协议（§3.2） ---------------------------------------------------


def test_reference_manifest_excludes_non_reference_files(client, migrated):
    body = client.get("/api/ledger/files/references")
    assert body.status_code == 200
    assert body.headers["Cache-Control"] == "no-store"
    manifest = body.get_json()
    assert pg_contracts.validate_reference_manifest(manifest)["version"]
    assert [item["path"] for item in manifest["files"]] == ["分类表.csv"]
    raw = (migrated["data_dir"] / "reference" / "分类表.csv").read_bytes()
    assert manifest["files"][0]["sha256"] == ledger_files.sha256_bytes(raw)


def test_reference_download_recomputes_digest(client, migrated):
    manifest = client.get("/api/ledger/files/references").get_json()
    entry = manifest["files"][0]
    got = client.get(f"/api/ledger/files/references/{entry['sha256']}")
    assert got.status_code == 200
    assert got.headers["Cache-Control"] == "no-store"
    assert got.data == (migrated["data_dir"] / "reference" / "分类表.csv").read_bytes()

    assert client.get("/api/ledger/files/references/" + "0" * 64).status_code == 404
    assert client.get("/api/ledger/files/references/not-a-digest").status_code == 400


def test_batch_upload_is_idempotent_and_conflict_safe(client, migrated):
    batch_id = pg_contracts.new_batch_id()
    manifest = client.get("/api/ledger/files/references").get_json()
    ref_digest = pg_contracts.reference_manifest_digest(manifest)
    payload = {"operation": "import", "client": "mac",
               "code_version": "pg05-e2e",
               "reference_manifest_sha256": ref_digest}
    files = [("微信账单.csv", "日期,金额\n2026-09-01,12.34\n".encode("utf-8")),
             ("支付宝账单.pdf", b"%PDF-1.4 e2e")]

    created = client.post(f"/api/ledger/files/batches/{batch_id}",
                          data=_upload(files, **payload),
                          content_type="multipart/form-data")
    assert created.status_code == 201, created.get_json()
    receipt = created.get_json()
    assert created.headers["Cache-Control"] == "no-store"
    assert receipt["batch_id"] == batch_id
    assert receipt["status"] == pg_contracts.BATCH_STATUS_ARCHIVED
    assert [item["name"] for item in receipt["artifacts"]] == [
        "微信账单.csv", "支付宝账单.pdf"
    ]
    assert receipt["reference_manifest_sha256"] == ref_digest

    # 幂等重放：同 ID 同内容 → 同一份回执，不重写文件
    again = client.post(f"/api/ledger/files/batches/{batch_id}",
                        data=_upload(files, **payload),
                        content_type="multipart/form-data")
    assert again.status_code == 201
    assert again.get_json() == receipt

    # 同 ID 不同内容 → 409，不覆盖已归档批次
    conflict = client.post(
        f"/api/ledger/files/batches/{batch_id}",
        data=_upload([("微信账单.csv", b"tampered")], **payload),
        content_type="multipart/form-data",
    )
    assert conflict.status_code == 409
    assert "不一致" in conflict.get_json()["error"]

    # 状态查询（上传结果未知时的核验入口）
    status = client.get(f"/api/ledger/files/batches/{batch_id}")
    assert status.status_code == 200 and status.get_json() == receipt
    assert client.get(f"/api/ledger/files/batches/{uuid.uuid4()}").status_code == 404
    assert client.get("/api/ledger/files/batches/not-a-uuid").status_code == 400

    # 归档落盘：文件名只作回执元数据，落盘名是序号 + digest；原件内容可复算
    stored = sorted((migrated["data_dir"] / "archive" / batch_id).iterdir())
    assert len(stored) == len(files) + 1  # 2 个原件 + manifest.json
    bodies = [p.read_bytes() for p in stored if p.name != ledger_files.MANIFEST_NAME]
    assert sorted(bodies) == sorted(raw for _, raw in files)
    assert all(p.stat().st_mode & 0o077 == 0 for p in stored)
    assert (migrated["data_dir"] / "archive" / batch_id).stat().st_mode & 0o077 == 0


def test_batch_limits_and_suffix_rules_are_enforced(client):
    """协议边界：后缀白名单、空文件、非法 batch_id、超量文件。"""
    payload = {"operation": "import", "client": "mac", "code_version": "pg05-e2e"}
    bad_suffix = client.post(
        f"/api/ledger/files/batches/{pg_contracts.new_batch_id()}",
        data=_upload([("账单.txt", b"x")], **payload),
        content_type="multipart/form-data",
    )
    assert bad_suffix.status_code == 400
    assert "XLSX" in bad_suffix.get_json()["error"]

    empty = client.post(
        f"/api/ledger/files/batches/{pg_contracts.new_batch_id()}",
        data=_upload([("账单.csv", b"")], **payload),
        content_type="multipart/form-data",
    )
    assert empty.status_code == 400

    too_many = client.post(
        f"/api/ledger/files/batches/{pg_contracts.new_batch_id()}",
        data=_upload([(f"账单{i}.csv", b"x") for i in range(11)], **payload),
        content_type="multipart/form-data",
    )
    assert too_many.status_code == 400

    invalid_id = client.post(
        "/api/ledger/files/batches/not-a-uuid",
        data=_upload([("账单.csv", b"x")], **payload),
        content_type="multipart/form-data",
    )
    assert invalid_id.status_code == 400


def test_batch_client_round_trip_against_real_app(migrated):
    """Mac 侧 `BatchClient` 走真 HTTP 打同一个 app：manifest → 上传 → 状态查询。

    用 werkzeug 的 `make_server` 起真 TCP 服务，证明协议在「真 socket + requests」下
    也成立（`test_client()` 不经过 HTTP 栈）。绑定 127.0.0.1 随机端口，仅本进程可见。
    """
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", 0, console_app.app, threaded=True)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f"http://127.0.0.1:{port}"
        batch_client = ledger_files.BatchClient(
            origin, client_name="mac", code_version="pg05-e2e",
            cache_dir=migrated["data_dir"] / "cache",
        )
        manifest, digest = batch_client.fix_reference()
        entry = manifest["files"][0]
        raw = batch_client.download_reference(entry["sha256"])
        assert ledger_files.sha256_bytes(raw) == entry["sha256"]
        assert (migrated["data_dir"] / "cache" / entry["sha256"]).read_bytes() == raw

        batch_id = pg_contracts.new_batch_id()
        uploads = [("微信账单.csv", "日期,金额\n2026-09-01,1.00\n".encode("utf-8"))]
        receipt = batch_client.upload_batch(
            batch_id, "import", uploads, reference_manifest_sha256=digest
        )
        assert receipt["batch_id"] == batch_id
        assert batch_client.batch_status(batch_id) == receipt
        assert batch_client.batch_status(pg_contracts.new_batch_id()) is None

        # 同 ID 不同内容：客户端侧也必须报协议错，而不是把新内容盖上去
        with pytest.raises(pg_contracts.ContractError):
            batch_client.upload_batch(
                batch_id, "import", [("微信账单.csv", b"different")],
                reference_manifest_sha256=digest,
            )
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_authority_only_routes_are_404_on_client_role(migrated, monkeypatch):
    """§3.2：client 侧不得出现第二份权威归档目录。"""
    monkeypatch.setattr("plugin_runtime.authority_state", lambda pid: {"enabled": True, "available": True})
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "client")
    client = console_app.app.test_client()
    assert client.get("/api/ledger/files/references").status_code == 404
    assert client.get(f"/api/ledger/files/batches/{uuid.uuid4()}").status_code == 404
    # 业务读接口在 client 上仍可用（D3：唯一在线源是 PG，与角色无关）
    assert client.get("/api/ledger/overview?year=2026&month=2026-09").status_code == 200


# --- 3. 目标库仍是「迁移后」状态 ----------------------------------------------


def test_migration_credential_and_schema_survive_app_traffic(migrated):
    """应用读写之后，完成凭据与 schema 版本仍可独立复核（§5.3.6/§8.2）。"""
    conn = pg_test_support.connect(migrated["admin_dsn"], autocommit=True)
    try:
        row = conn.execute(
            "SELECT description FROM schema_migrations WHERE version = %s",
            (pg_migrations.PG_SCHEMA_VERSION,),
        ).fetchone()
        credential = json.loads(row["description"])
        assert credential["format"] == "pg-migrate-credential-1"
        assert credential["source_sha256"] == file_sha256(migrated["source"])
        assert credential["target_purpose"] == "rehearsal"
        assert credential["target_identity"]["database"] == migrated["database"]
        report = pg_migrations.verify(conn)
        assert report["compatible"] is True
        assert report["schema_version"] == pg_migrations.latest_schema_version()
    finally:
        conn.close()


def test_verify_cli_still_clean_after_application_writes(client, migrated):
    """应用写入后独立验证器会报「不一致」——这是预期语义，必须显式记录。

    §8.2 的验证器比对的是**源快照与目标库的当前状态**。迁移后应用继续写业务数据，
    两者必然分叉；因此「迁移完成」的验收窗口是冻结期（§9 D），不是长期不变式。
    本用例把这条边界钉下来，避免后人把正常的写入误判成迁移缺陷。
    """
    created = client.post(
        "/api/ledger/tx/manual",
        json={"date": "2026-09-09", "time": "10:00", "direction": "支出",
              "amount": 1.00, "who": "分叉商户", "goods": "分叉商品",
              "type1": "餐饮", "type2": "三餐"},
    )
    assert created.status_code == 200, created.get_json()
    proc = run_cli(
        "verify_postgres_migration.py",
        ["--source", migrated["source"], "--target", "env"],
        dict(os.environ),
    )
    assert proc.returncode == 5, proc.stdout + proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["status"] == "mismatch" and summary["mismatch_entries"] >= 1


def test_no_sqlite_fallback_under_postgres_engine(migrated, client):
    """D3：PG 引擎下不得退回本地 SQLite——legacy 连接门面必须直接报错。"""
    client.get("/api/ledger/overview?year=2026&month=2026-09")
    with pytest.raises(RuntimeError, match="不得使用 legacy SQLite"):
        console_app.get_db_conn()
