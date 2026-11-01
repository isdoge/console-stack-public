# -*- coding: utf-8 -*-
"""诊断五模块 PG 路径定向测试（PG-04）。

用真 PG16 隔离实例（schema12 基线）验证诊断模块的在线 PG 读取/写入路径：
ops_status、effective_config、settings_store、attention、inspection_report
经 pg_diagnostics 只读入口访问 PG，不再读旧 SQLite（提案 §8.1 W5）。
使用 start_pg16 统一选择 Docker / 显式原生 PG16，底座不可用时失败，不伪 skip。
"""
import os
import sys
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import attention  # noqa: E402
import effective_config  # noqa: E402
import inspection_report  # noqa: E402
import ops_status  # noqa: E402
import pg_diagnostics  # noqa: E402
import pg_migrations  # noqa: E402
import pg_test_support as support  # noqa: E402
import pgdb  # noqa: E402
import settings_store  # noqa: E402

# `apply_env` 直接写 os.environ，module 级夹具又不能用 monkeypatch 自动回滚；
# 不显式还原就会把 CONSOLE_STORAGE_ENGINE=postgres 泄漏给后续模块，让整份测试在
# 指向已停止容器的 postgres 模式下运行（PG-05 集成发现：全量 121 项红）。
_ENV_KEYS = (
    "CONSOLE_PG_HOST", "CONSOLE_PG_PORT", "CONSOLE_PG_DB",
    "CONSOLE_PG_USER", "CONSOLE_PG_PASSWORD",
    "CONSOLE_STORAGE_ENGINE", "CONSOLE_INSTANCE_ROLE", "CONSOLE_MAINTENANCE_MODE",
)


@pytest.fixture(scope="module")
def pg_env():
    # start_pg16 owns backend selection and fails closed if neither native PG16
    # nor Docker is available; a Docker-only precheck rejects valid native runs.
    saved_env = {key: os.environ.get(key) for key in _ENV_KEYS}
    container = support.start_pg16()
    try:
        with psycopg.connect(container.dsn(), autocommit=True) as conn:
            pg_migrations.ensure_migrations_table(conn)
            pg_migrations.apply(conn)
            conn.execute(
                "INSERT INTO versions (svc_name, local_version, upstream_version, "
                "behind_count, update_status, last_checked) VALUES "
                "('console', '1.0', '1.1', 2, 'behind', '2026-09-10 03:00:00')"
            )
            conn.execute(
                "INSERT INTO feed_cache (id, source, title) VALUES ('f1', 's', 't')"
            )
            conn.execute(
                "INSERT INTO oplog (host, svc, action, started_at) VALUES "
                "('h', 's', 'test', '2020-01-01 00:00:00')"
            )
        # apply_env 接受任何带 host/port/dbname/user/password 属性的对象（PgContainer 满足）
        support.apply_env(container)
        pgdb.close_pool()
        yield container
        pgdb.close_pool()
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        pgdb.close_pool()
        container.stop()


def test_pg_diagnostics_readonly_connection(pg_env, monkeypatch):
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    with pg_diagnostics.readonly_connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM ledger_tx").fetchone()
        assert row["n"] == 0


def test_ops_status_reads_pg(pg_env, monkeypatch, tmp_path):
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    result = ops_status.get_ops_status(db_path=None, backup_dir=str(tmp_path))
    assert result["versions_last_checked"] == "2026-09-10 03:00:00"
    assert result["feed_last_fetched"] is not None
    assert result["timed_backup"]["state"] == "never"


def test_effective_config_reports_latest_schema_version(pg_env, monkeypatch):
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    assert effective_config._applied_schema_version() == pg_migrations.latest_schema_version()


def test_effective_config_database_probe_with_online_db_path(pg_env, monkeypatch, tmp_path):
    """PG-05 集成回归：应用接线传在线库位置时，`database` 探针必须仍可见。

    旧判据 `db_path is None` 在 `lambda: DB_PATH` 下恒不成立，真实 PG 实例的
    `/api/config/effective` 永远回 `database: null`，`verify_deploy.py` 核验项 19
    因此无法通过（PG-05 端到端实测发现）。
    """
    import sqlite3

    import paths

    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    for db_path in (None, paths.DB_PATH):
        data = effective_config.get_effective_config(db_path=db_path)
        assert data["storage_engine"] == "postgres"
        assert isinstance(data["database"], dict), f"db_path={db_path!r}"
        assert data["database"]["reachable"] is True
    # 显式传**别的**路径仍走离线 SQLite 语义，不假装在线 PG
    offline = tmp_path / "offline.db"
    sqlite3.connect(offline).close()
    assert effective_config.get_effective_config(db_path=str(offline))["database"] is None


def test_settings_store_row_counts_pg(pg_env, monkeypatch):
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    counts = {r["name"]: r["rows"] for r in settings_store.table_row_counts()}
    assert counts.get("versions") == 1
    assert "ledger_tx" in counts


def test_settings_store_prune_oplog_pg(pg_env, monkeypatch):
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    deleted = settings_store.prune_oplog(days=30)
    assert deleted == 1
    deleted_again = settings_store.prune_oplog(days=30)
    assert deleted_again == 0


def test_attention_version_items_pg(pg_env, monkeypatch):
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    items = attention._version_items(None)
    assert any(i["kind"] == "version" and "console" in i["title"] for i in items)


def test_inspection_report_version_rows_pg(pg_env, monkeypatch):
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    rows = inspection_report._version_rows(None)
    # _version_rows 的 SELECT 列不含 behind_count；以实际列断言
    assert rows["console"]["upstream_version"] == "1.1"
    assert rows["console"]["update_status"] == "behind"


def test_explicit_db_path_stays_sqlite_even_under_pg_engine(pg_env, monkeypatch, tmp_path):
    """显式离线 db_path = SQLite 语义，PG 引擎下也不改写（pg_diagnostics 契约）。"""
    import sqlite3

    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    with pg_diagnostics.readonly_connection(legacy) as conn:
        assert conn.execute("SELECT COUNT(*) AS v FROM t").fetchone()["v"] == 0


def test_online_db_path_reads_pg_not_legacy_sqlite(pg_env, monkeypatch, tmp_path):
    """PG-05 集成修正：传在线库位置（`paths.DB_PATH`）必须读 PG，不得读旧 SQLite。

    生产接线是 `lambda: DB_PATH`，模块层还会 `db_path or paths.DB_PATH` 归一，
    所以「显式给了 db_path」不等于「离线取证」——判据必须看路径是不是在线库位置。
    旧实现下这里会静默打开迁移前的 SQLite 快照（§8.1 W5 禁止）。
    """
    import sqlite3

    import paths

    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "postgres")
    legacy = tmp_path / "console.db"
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE versions (svc_name TEXT)")
    conn.execute("INSERT INTO versions VALUES ('stale-sqlite-row')")
    conn.commit()
    conn.close()

    # 造一个「旧快照路径恰好等于 paths.DB_PATH」的现场：读到的必须仍是 PG 的 versions
    monkeypatch.setattr(paths, "DB_PATH", str(legacy))
    monkeypatch.setattr(pg_diagnostics, "DB_PATH", str(legacy))
    with pg_diagnostics.readonly_connection(str(legacy)) as conn:
        names = [r["svc_name"] for r in conn.execute("SELECT svc_name FROM versions").fetchall()]
    assert "stale-sqlite-row" not in names
    assert "console" in names  # pg_env 夹具写入的 PG 行
    assert pg_diagnostics.is_online_postgres(str(legacy)) is True
