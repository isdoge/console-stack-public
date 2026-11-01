# -*- coding: utf-8 -*-
"""`scripts/pg_component_schema.py` CLI 测试（真 PG16 隔离库；B7-2）。

只验证**契约**，不验证实现细节：退出码 0/2/4/5、幂等、fail closed、
脱敏。CLI 的 docstring 是契约来源；跑出来与它不符就是缺陷，不改断言。
"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pg_migrations  # noqa: E402  (conftest 已把 backend 加进 sys.path)
from pg03_test_fixtures import (  # noqa: E402
    drop_database,
    fresh_database,
    pg_env,
    run_cli,
)
from pg_migration_lib import (  # noqa: E402
    acquire_migration_lock,
    release_migration_lock,
)
from pg_test_support import connect  # noqa: E402

SCRIPTS = pathlib.Path(__file__).resolve().parent
REPO = SCRIPTS.parent
PLUGINS = REPO / "plugins"


@pytest.fixture()
def fresh_db(pg_container, pg_dsn):
    admin = connect(pg_dsn, autocommit=True)
    name = fresh_database(admin, prefix="b7comp")
    yield name
    drop_database(admin, name)
    admin.close()


@pytest.fixture()
def env(pg_container, fresh_db):
    return pg_env(pg_container, fresh_db)


def _run(env, command, *argv):
    # `--plugins-root` 是子命令级参数（三个子命令各自声明），不是顶层选项。
    return run_cli("pg_component_schema.py",
                   [command, "--plugins-root", str(PLUGINS), *argv], env)


def _out(proc):
    return json.loads(proc.stdout)


def _legacy_db(pg_container, dbname):
    """合成「私人既有库」：只跑冻结遗产链，不碰组件登记。"""
    conn = connect(pg_container.dsn(dbname=dbname), autocommit=True)
    try:
        pg_migrations.apply(conn)
    finally:
        conn.close()


def test_check_on_empty_database_fails_closed(env):
    proc = _run(env, "check")
    assert proc.returncode == 5, proc.stdout + proc.stderr
    payload = _out(proc)
    assert payload["ok"] is False
    assert payload["report"]["registry_present"] is False
    kinds = {issue["kind"] for issue in payload["report"]["issues"]}
    # 空库既没有登记也没有表：必须是明确失败，不是「空表通过」
    assert "registry-missing" in kinds
    assert "missing-table" in kinds
    assert "adoption-receipt-missing" not in kinds  # 遗产链不在库，不要求回执


def _zero_plugin_root(tmp_path):
    """合成零插件组合根：只声明空 plugins，磁盘上没有任何插件目录。"""
    root = tmp_path / "plugins"
    root.mkdir()
    (root / "contract.json").write_text(json.dumps({"api_version": 1}), encoding="utf-8")
    (root / "composition.json").write_text(json.dumps({"plugins": []}), encoding="utf-8")
    return root


def _run_zero_plugin(env, plugin_root, command, *argv):
    return run_cli("pg_component_schema.py",
                   [command, "--plugins-root", str(plugin_root), *argv], env)


def test_public_zero_plugin_new_install_is_ok_and_registers_baseline(
        env, tmp_path, pg_container, fresh_db):
    """公共零插件新装：CLI 新装路径必须 ok=true，且登记非空。"""
    root = _zero_plugin_root(tmp_path)
    proc = _run_zero_plugin(env, root, "migrate")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = _out(proc)
    assert payload["ok"] is True
    report = payload["report"]
    assert report["ok"] is True, report["issues"]
    assert [row["component"] for row in report["components"]] == ["kernel"]
    assert report["components"][0]["origin"] == "applied"
    assert report["components"][0]["version"] == report["baseline_version"] == 1
    assert report["expected_versions"] == {"kernel": [1]}
    assert report["legacy_chain_versions"] == []
    assert "ledger_tx" in report["retained_tables"]
    assert "ledger_tx" not in report["present_tables"]
    assert set(report["present_tables"]) <= set(report["table_whitelist"])

    # 重跑幂等：不再 applied，但仍 ok
    again = _run_zero_plugin(env, root, "migrate")
    assert again.returncode == 0, again.stdout + again.stderr
    assert _out(again)["applied"] == []

    check = _run_zero_plugin(env, root, "check")
    assert check.returncode == 0, check.stdout + check.stderr
    owners = _out(check)["report"]["table_owners"]
    assert owners and set(owners.values()) == {"kernel"}
    assert "ledger_tx" not in owners


def test_migrate_refuses_baseline_on_legacy_database(env, pg_container, fresh_db):
    _legacy_db(pg_container, fresh_db)
    proc = _run(env, "migrate")
    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert _out(proc)["ok"] is False


def test_check_output_is_secret_free(env, pg_container):
    proc = _run(env, "check")
    assert pg_container.password not in proc.stdout
    assert pg_container.password not in proc.stderr


def test_migrate_on_legacy_database_refuses_baseline(env, pg_container, fresh_db):
    """既有库只能走 adopt：migrate 重放基线必须整事务拒绝，不留登记表。"""
    _legacy_db(pg_container, fresh_db)
    proc = _run(env, "migrate")
    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert _out(proc)["ok"] is False
    check = _run(env, "check")
    assert check.returncode == 5
    report = _out(check)["report"]
    assert report["registry_present"] is False, "拒绝后不能留下空登记表"
    assert report["legacy_chain_versions"] == [
        version for version, _description, _statements in pg_migrations.PG_MIGRATIONS
    ]


def test_adopt_then_check_passes_and_is_idempotent(env, pg_container, fresh_db):
    _legacy_db(pg_container, fresh_db)
    assert _run(env, "migrate").returncode == 5  # 既有库拒绝重放基线

    first = _run(env, "adopt")
    assert first.returncode == 0, first.stdout + first.stderr
    adopted = _out(first)["adopted"]
    assert adopted and all("catalog_fingerprint" in row for row in adopted)

    second = _run(env, "adopt")
    assert second.returncode == 0
    assert _out(second)["adopted"] == []
    assert _out(second)["skipped"]

    check = _run(env, "check")
    assert check.returncode == 0, check.stdout + check.stderr
    report = _out(check)["report"]
    assert report["ok"] is True
    assert report["issues"] == []
    assert all(row["origin"] == "adopted" for row in report["components"])


def test_adopt_refuses_tampered_legacy_checksum(env, pg_container, fresh_db):
    _legacy_db(pg_container, fresh_db)
    admin = connect(pg_container.dsn(dbname=fresh_db), autocommit=True)
    try:
        admin.execute("UPDATE schema_migrations SET checksum = 'deadbeef' WHERE version = 15")
    finally:
        admin.close()
    proc = _run(env, "adopt")
    assert proc.returncode == 5
    assert _out(proc)["ok"] is False

    # 拒绝后不留半登记
    check = _run(env, "check")
    assert _out(check)["report"]["components"] == []


def test_adopt_without_legacy_chain_is_refused(env):
    """空库里没有可接管的结构：缺表必须失败，不能凭空写回执。"""
    proc = _run(env, "adopt")
    assert proc.returncode == 5
    assert _out(proc)["ok"] is False


def test_post_adoption_structure_change_is_reported(env, pg_container, fresh_db):
    _legacy_db(pg_container, fresh_db)
    assert _run(env, "adopt").returncode == 0
    admin = connect(pg_container.dsn(dbname=fresh_db), autocommit=True)
    try:
        admin.execute("ALTER TABLE renewals ADD COLUMN surprise TEXT")
    finally:
        admin.close()
    proc = _run(env, "check")
    assert proc.returncode == 5
    kinds = {(issue["kind"], issue.get("component"))
             for issue in _out(proc)["report"]["issues"]}
    assert ("catalog-drift", "plugin:renewals") in kinds


def test_extra_table_is_not_silently_ignored(env, pg_container, fresh_db):
    _legacy_db(pg_container, fresh_db)
    assert _run(env, "adopt").returncode == 0
    admin = connect(pg_container.dsn(dbname=fresh_db), autocommit=True)
    try:
        admin.execute("CREATE TABLE stray_table (id int)")
    finally:
        admin.close()
    proc = _run(env, "check")
    assert proc.returncode == 5
    assert {"kind": "extra-table", "table": "stray_table"} in _out(proc)["report"]["issues"]


def test_concurrent_migrate_is_refused_by_maintenance_lock(env, pg_container, fresh_db):
    holder = connect(pg_container.dsn(dbname=fresh_db), autocommit=True)
    try:
        acquire_migration_lock(holder, timeout_s=10)
        proc = _run(env, "migrate", "--lock-timeout", "1")
        assert proc.returncode == 4, proc.stdout + proc.stderr
        assert _out(proc)["ok"] is False
    finally:
        release_migration_lock(holder)
        holder.close()


def test_missing_config_exits_2(pg_container):
    empty = {key: value for key, value in pg_env(pg_container, "unused").items()
             if not key.startswith("CONSOLE_PG_")}
    proc = _run(empty, "check")
    assert proc.returncode == 2
    assert "配置缺失" in _out(proc)["error"]


def test_cli_never_imports_the_application():
    """owner CLI 不得拉起 app：否则会连带插件业务 import 与后台线程。"""
    for name in ("pg_component_schema.py",):
        source = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "\nimport app\n" not in source
        assert "\nfrom app import" not in source
        assert "import app as" not in source
    component = (REPO / "console/backend/component_migrations.py").read_text(encoding="utf-8")
    assert "\nimport app\n" not in component
    assert "\nfrom app import" not in component
