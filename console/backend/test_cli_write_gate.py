# -*- coding: utf-8 -*-
"""MT-01 回归：CLI 写入口在维护态宿主上必须在**落盘之前**拒绝（提案 §11.12）。

首红（PG-06 S20 现场复现 + PG-05 r7 黑盒复跑，`evidence/r7/repro-mt01-cli-write-gate*.json`）：
CLI 进程不带 `CONSOLE_MAINTENANCE_MODE` 时 `runtime_mode` 缺省 `normal` →
`require_writable()` 静默放行 → 文件先落盘、PG 事务因冻结被拒而回滚，**文件不回滚**
→ 孤儿归档（`GET /api/ledger/files/batches/<id>` 还回 200，对外像「已归档且完整」）。

判据分两层：

- `write_gate.resolve()` 纯判定：env 短路、sqlite 缺省放行（**不翻转全局默认**）、
  postgres 判不出 → **fail-closed**；
- 真 PG16 + 真 §6.3 受限账号 + 真 CLI `main()`：宿主冻结的三种形态（角色撤权 /
  独占维护锁 / 两者）都必须在落盘前拒绝且 archive/staging 零新增；未冻结时放行
  （对照，证明没有把功能一起关掉）；**共享写锁不误伤**（正常业务写事务持共享锁时
  不得判成冻结）。
"""
import pathlib
import socket
import sys
import uuid

import psycopg
import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import ledger_cli  # noqa: E402
import pg_migrations  # noqa: E402
import pg_test_support  # noqa: E402
import write_gate  # noqa: E402

NAS_PW = "mt01_regression_pw"
_PG_ENV_KEYS = ("CONSOLE_PG_HOST", "CONSOLE_PG_PORT", "CONSOLE_PG_DB",
                "CONSOLE_PG_USER", "CONSOLE_PG_PASSWORD")


@pytest.fixture()
def cli_env(pg_container):
    """独立库 + schema12 + 一个 §6.3 权限面的受限账号（与部署同形态的权限面）。"""
    import pgdb

    admin = pg_test_support.connect(pg_container.dsn(), autocommit=True)
    name = f"console_mt01_{uuid.uuid4().hex[:8]}"
    role = f"mt01_nas_{uuid.uuid4().hex[:10]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    db_admin = pg_test_support.connect(pg_container.dsn(dbname=name), autocommit=True)
    try:
        pg_migrations.apply(db_admin)
        # `schema_migrations` 按 §6.3 单独撤销写权限，不属「正常可写面」。
        # 授权必须落在**目标库连接**上（GRANT 的表要在该库存在）。
        tables = [t for t in pg_migrations.SCHEMA_TABLES if t != "schema_migrations"]
        pg_test_support.create_app_role(db_admin, role, NAS_PW, tables=tables,
                                        database=name)
        pgdb.close_pool()      # 池是单例：别把别的用例的库带进来
        yield {"container": pg_container, "database": name, "role": role}
    finally:
        pgdb.close_pool()
        db_admin.close()
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        pg_test_support.drop_app_role(admin, role)
        admin.close()


def _point_at(monkeypatch, env):
    """把 CLI/gate 指向夹具库（受限账号身份）；**故意不设** 维护变量（首红条件）。"""
    import pgdb

    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authority")
    monkeypatch.setenv("CONSOLE_PG_HOST", env["container"].host)
    monkeypatch.setenv("CONSOLE_PG_PORT", str(env["container"].port))
    monkeypatch.setenv("CONSOLE_PG_DB", env["database"])
    monkeypatch.setenv("CONSOLE_PG_USER", env["role"])
    monkeypatch.setenv("CONSOLE_PG_PASSWORD", NAS_PW)
    monkeypatch.setenv("CONSOLE_PG_LOCK_TIMEOUT", "2")
    monkeypatch.delenv("CONSOLE_MAINTENANCE_MODE", raising=False)
    pgdb.close_pool()


def _freeze_role(admin, role):
    """镜像 `scripts/cutover_writers` 的撤权面：DML 与序列 USAGE/UPDATE 全撤、SELECT 保留。"""
    admin.execute(
        f'REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public '
        f'FROM "{role}"')
    admin.execute(
        f'REVOKE USAGE, UPDATE ON ALL SEQUENCES IN SCHEMA public FROM "{role}"')


def _db_counts(env):
    with psycopg.connect(env["container"].dsn(dbname=env["database"]),
                         autocommit=True) as conn:
        return {table: conn.execute(f'SELECT count(*) FROM public."{table}"').fetchone()[0]
                for table in ("ledger_tx", "ledger_file_batches")}


def _fs_state(data_dir):
    out = {}
    for name in ("archive", "staging"):
        base = data_dir / name
        out[name] = sorted(p.name for p in base.iterdir()) if base.is_dir() else []
    return out


# --- 纯判定：env 短路 / 缺省不翻转 / fail-closed ---------------------------------


def test_env_read_only_refuses_without_touching_pg(monkeypatch, tmp_path, capsys):
    """env 显式 read_only：即使声明 postgres 且无 PG 配置也必须立即只读（不尝试连接）。"""
    monkeypatch.setenv("CONSOLE_MAINTENANCE_MODE", "read_only")
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    for key in _PG_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    state = write_gate.resolve()
    assert state["read_only"] is True
    assert state["source"] == write_gate.SOURCE_ENV_READ_ONLY

    with pytest.raises(SystemExit) as excinfo:
        ledger_cli.main(["--data-dir", str(tmp_path), "reclassify"])
    assert excinfo.value.code == 1
    assert "拒绝执行" in capsys.readouterr().err


def test_sqlite_default_stays_writable(monkeypatch):
    """不翻转全局默认：不带维护变量、未声明 postgres（sqlite 缺省）→ 放行。"""
    monkeypatch.delenv("CONSOLE_MAINTENANCE_MODE", raising=False)
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "sqlite")
    for key in _PG_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    state = write_gate.resolve()
    assert state["read_only"] is False
    assert state["source"] == write_gate.SOURCE_SQLITE_DEFAULT


def test_postgres_undeterminable_fails_closed(monkeypatch):
    """已部署形态（显式 postgres）判不出维护态 → 拒绝写入，不放行。"""
    monkeypatch.delenv("CONSOLE_MAINTENANCE_MODE", raising=False)
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    for key in _PG_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    state = write_gate.resolve()
    assert state["read_only"] is True
    assert state["source"] == write_gate.SOURCE_UNDETERMINED
    assert state["detail"]["error"]

    with socket.socket() as sock:          # 取一个刚释放的本地端口：连接会被拒
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
    monkeypatch.setenv("CONSOLE_PG_HOST", "127.0.0.1")
    monkeypatch.setenv("CONSOLE_PG_PORT", str(dead_port))
    monkeypatch.setenv("CONSOLE_PG_USER", "someone")
    monkeypatch.setenv("CONSOLE_PG_PASSWORD", "x")
    state = write_gate.resolve()
    assert state["read_only"] is True
    assert state["source"] == write_gate.SOURCE_UNDETERMINED


# --- 真库：冻结三形态 + 对照 + 共享锁不误伤 ---------------------------------------


@pytest.mark.parametrize("freeze_kind", ["role", "lock", "both"])
def test_frozen_host_refuses_cli_write_before_any_file(cli_env, tmp_path, monkeypatch,
                                                       capsys, freeze_kind):
    """宿主冻结（撤权 / 独占锁 / 两者）→ CLI 在落盘之前拒绝，archive/staging 零新增。"""
    admin = pg_test_support.connect(cli_env["container"].dsn(dbname=cli_env["database"]),
                                    autocommit=True)
    holder = None
    try:
        if freeze_kind in ("role", "both"):
            _freeze_role(admin, cli_env["role"])
        if freeze_kind in ("lock", "both"):
            holder = pg_test_support.connect(
                cli_env["container"].dsn(dbname=cli_env["database"]), autocommit=False)
            holder.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                           (1129270867, 1))     # pg_locks: namespace=0x434F4E53, key=1
        _point_at(monkeypatch, cli_env)

        state = write_gate.resolve()
        assert state["read_only"] is True
        expected = {"role": write_gate.SOURCE_PG_ROLE_FROZEN,
                    "lock": write_gate.SOURCE_PG_LOCK,
                    "both": write_gate.SOURCE_PG_LOCK}[freeze_kind]
        assert state["source"] == expected

        data_dir = tmp_path / "pay"
        data_dir.mkdir()
        before = _fs_state(data_dir), _db_counts(cli_env)
        with pytest.raises(SystemExit) as excinfo:
            ledger_cli.main(["--data-dir", str(data_dir), "reclassify"])
        assert excinfo.value.code == 1
        assert "拒绝执行" in capsys.readouterr().err

        after_fs, after_db = _fs_state(data_dir), _db_counts(cli_env)
        assert after_fs == before[0], "拒绝必须发生在任何文件系统动作之前"
        assert after_db == before[1], "拒绝必须发生在任何写库之前"
    finally:
        if holder is not None:
            holder.rollback()
            holder.close()
        admin.close()


def test_frozen_host_refuses_cli_import_before_any_file(cli_env, tmp_path,
                                                        monkeypatch, capsys):
    """PG-06 S20 的同款入口：`ledger_cli.py import`（PG-06 复验只需重跑这一腿）。"""
    admin = pg_test_support.connect(cli_env["container"].dsn(dbname=cli_env["database"]),
                                    autocommit=True)
    try:
        _freeze_role(admin, cli_env["role"])
        _point_at(monkeypatch, cli_env)

        data_dir = tmp_path / "pay"
        data_dir.mkdir()
        bill = tmp_path / "bill.csv"
        bill.write_bytes(b"a,b\n1,2\n")     # 门禁在前：这个文件根本不会被读
        before = _fs_state(data_dir), _db_counts(cli_env)
        with pytest.raises(SystemExit) as excinfo:
            ledger_cli.main(["--data-dir", str(data_dir), "import", str(bill)])
        assert excinfo.value.code == 1
        assert "拒绝执行" in capsys.readouterr().err
        after_fs, after_db = _fs_state(data_dir), _db_counts(cli_env)
        assert after_fs == before[0] and after_db == before[1]
    finally:
        admin.close()


def test_unfrozen_host_passes_gate_and_reclassify_runs(cli_env, tmp_path, monkeypatch):
    """对照：未冻结、同样不带维护变量 → 放行且真的跑起来（没把功能一起关掉）。"""
    _point_at(monkeypatch, cli_env)
    state = write_gate.resolve()
    assert state["read_only"] is False
    assert state["source"] == write_gate.SOURCE_PG_WRITABLE
    assert state["detail"]["tables_missing_insert"] == []

    data_dir = tmp_path / "pay"
    data_dir.mkdir()
    ledger_cli.main(["--data-dir", str(data_dir), "reclassify"])
    # 回执（文件侧）与批次记录（库侧）都落地：与冻结场景的「零新增」形成对照
    assert (data_dir / "archive").is_dir() and list((data_dir / "archive").iterdir())
    counts = _db_counts(cli_env)
    assert counts["ledger_file_batches"] == 1


def test_shared_writer_lock_alone_is_not_freeze(cli_env, monkeypatch):
    """正常业务写事务持**共享**维护锁时不得判成冻结（独占行才算，防误伤）。"""
    holder = pg_test_support.connect(
        cli_env["container"].dsn(dbname=cli_env["database"]), autocommit=False)
    try:
        holder.execute("SELECT pg_advisory_xact_lock_shared(%s, %s)",
                       (1129270867, 1))
        _point_at(monkeypatch, cli_env)
        state = write_gate.resolve()
        assert state["read_only"] is False
        assert state["source"] == write_gate.SOURCE_PG_WRITABLE
        assert state["detail"]["maintenance_lock_held"] is False
    finally:
        holder.rollback()
        holder.close()


def test_export_and_file_only_tools_are_not_gated():
    """只读/只产料的命令不进写入口门禁（数据断言，防止误加卡住只读用法）。"""
    assert "export" not in ledger_cli._WRITE_TARGETS
    for name in ("merge", "classify", "mail-parse", "mail-sync", "build-table"):
        assert name not in ledger_cli._WRITE_TARGETS
    for name in ("import", "reclassify", "transfer", "relink", "link-repay", "normalize", "ai"):
        assert name in ledger_cli._WRITE_TARGETS
