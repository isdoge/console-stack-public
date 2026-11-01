#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G01/G03：cutover 写者冻结/恢复（`cutover_writers.py`）+ 两条 CLI 入口自测。

G01（提案 §11.3.2）：两条外置入口（`backup_runtime.py cutover` 与
`deploy.sh --storage-cutover`）都要能用**已实现且审查过的**停写/恢复配置，
不能到 PG-07 现场手拼 shell。本文件覆盖参数拒绝、dry-run、以及两种入口的真实执行。

G03（§11.3.2/§6.4）：停写必须**持久**——控制器进程死掉、app 重启、锁连接断开之后
业务写仍被拒绝。持久兜底两条腿，都在真 PG16 上验证：

  1. `CONSOLE_MAINTENANCE_MODE=read_only` 写进部署 env 文件（**新进程**起来仍拒写）；
  2. PG 账号级权限冻结（REVOKE DML、**保留 SELECT**；管理账号不动，恢复通道可用）。

`g` 系列用例按「先红后绿」写：实现前必须能失败。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg.rows
import pytest

BACKEND = Path(__file__).resolve().parents[1] / "console" / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backup_runtime as runtime  # noqa: E402
import pg_migrations  # noqa: E402
import pg_test_support as support  # noqa: E402
import test_deploy_safety as tds  # noqa: E402
from pg03_test_fixtures import make_legacy_db, pg_env  # noqa: E402
from pg_test_support import connect  # noqa: E402
from test_backup_runtime_cutover import (  # noqa: E402
    _pg_available,
    _seed_pg03_into_repo,
)

SCRIPTS = Path(__file__).resolve().parent
CLI = SCRIPTS / "backup_runtime.py"
WRITERS = "cutover_writers.py"
VENV_BIN = Path(sys.executable).parent  # deploy.sh 里的 `python3` 也要能找到 psycopg


@pytest.fixture
def repo(tmp_path):
    root = tds.make_repo(tmp_path)
    tds.make_db(root / "console/backend/data/console.db").close()
    return root, tds.git(root, "rev-parse", "HEAD").strip()


def _cli(root, *argv, env=None, timeout=300):
    return subprocess.run(
        [sys.executable, str(CLI), *argv], cwd=str(root), capture_output=True,
        text=True, env=env, timeout=timeout, check=False,
    )


def _state(root, state_dir=None):
    path = runtime.cutover_state_path(root, state_dir)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


# --- G01：CLI 入口可达 + 参数拒绝 + dry-run -------------------------------------


def test_cutover_cli_help_lists_contract_flags(repo):
    """G01 首红：`cutover` 必须真的挂在 argparse 上（此前整段参数不可达 → rc=2）。"""
    root, _commit = repo
    proc = _cli(root, "cutover", "--help")
    assert proc.returncode == 0, proc.stderr
    for flag in ("--target-commit", "--yes", "--migrator", "--verify-cmd", "--stop-cmd",
                 "--open-cmd", "--publish-cmd", "--state-dir", "--dry-run", "--lock-fd"):
        assert flag in proc.stdout, f"--help 缺 {flag}"


def test_cutover_cli_rejects_missing_yes_without_touching_state(repo):
    root, commit = repo
    proc = _cli(root, "cutover", str(root), "--target-commit", commit)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "需要 --yes" in proc.stderr
    assert _state(root) is None  # 拒绝发生在任何状态落盘之前


def test_release_refuses_before_writes_opened(repo, monkeypatch):
    """V4-03 第二道闸（复核 A）：cutover 未开放写入时，站点动作也不得放开写者。

    没有 cutover 状态文件（非割接场景）不拦；确需开放前站点恢复时用显式豁免。
    """
    import cutover_writers as writers

    root, commit = repo
    state_path = runtime.cutover_state_path(root)
    assert writers.cutover_preopen_reason(root) is None          # 没有状态文件 → 不拦

    def write_state(state, pending=None):
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "format": "console-cutover-state-2", "state": state, "pending": pending,
            "target_commit": commit, "root": str(root), "history": [],
        }), encoding="utf-8")

    write_state("verified_read_only")
    assert "尚未开放" in writers.cutover_preopen_reason(root)
    monkeypatch.delenv(writers.ALLOW_PREOPEN_ENV, raising=False)
    with pytest.raises(writers.FreezeFailure, match="拒绝在开放前恢复业务写者"):
        writers.release(root)
    write_state("verified_read_only", {"state": "writes_opened"})
    assert writers.cutover_preopen_reason(root) is None           # 正在开放 → 放行
    write_state("writes_opened")
    assert writers.cutover_preopen_reason(root) is None
    # 显式豁免：开放前的站点恢复仍可走，但必须自己声明
    write_state("verified_read_only")
    monkeypatch.setenv(writers.ALLOW_PREOPEN_ENV, "1")
    with pytest.raises(writers.UsageError, match="无冻结记录"):
        writers.release(root)                                     # 越过了闸门，走到下一步


def test_cutover_cli_rejects_bad_target_commit(repo):
    """短 SHA 与「仓库里不存在的完整 SHA」都必须拒绝（§8.3 身份绑定）。"""
    root, commit = repo
    short = _cli(root, "cutover", str(root), "--target-commit", "abc123", "--yes",
                 "--skip-prepare")
    assert short.returncode == 1 and "完整 SHA" in short.stderr, short.stderr

    missing = "0" * 40
    assert missing != commit
    unknown = _cli(root, "cutover", str(root), "--target-commit", missing, "--yes",
                   "--skip-prepare")
    assert unknown.returncode == 1, unknown.stdout + unknown.stderr
    assert "完整提交" in unknown.stderr or "完整 SHA" in unknown.stderr, unknown.stderr
    assert _state(root) is None


def test_cutover_cli_dry_run_reports_remaining_without_advancing(repo):
    root, commit = repo
    proc = _cli(root, "cutover", str(root), "--target-commit", commit, "--yes", "--dry-run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["dry_run"] is True
    assert payload["remaining"] == ["frozen", "migrated", "verified_read_only",
                                    "backup_verified", "writes_opened", "completed"]
    persisted = _state(root)
    assert persisted["state"] == "prepared"  # dry-run 只初始化，不推进
    assert [entry["state"] for entry in persisted["history"]] == ["prepared"]


def test_cutover_default_actions_include_reviewed_writer_actions(repo, monkeypatch):
    """G01：缺省停写/恢复动作就是已实现、可审阅的 `cutover_writers.py`。

    以前 stop_cmd/open_cmd 只在文档里说「由现场注入」——PG-07 就得手拼 shell。
    现在缺省值是仓库内脚本，`CONSOLE_CUTOVER_*` 环境变量可覆盖（同一条路）。
    """
    root, _commit = repo
    monkeypatch.delenv("CONSOLE_CUTOVER_STOP_CMD", raising=False)
    monkeypatch.delenv("CONSOLE_CUTOVER_OPEN_CMD", raising=False)
    actions = runtime.cutover_default_actions(root)
    assert set(actions) >= {"migrator", "verify_cmd", "stop_cmd", "open_cmd"}
    stop = runtime._writers_argv(root, "freeze")
    open_ = runtime._writers_argv(root, "release")
    for argv, verb in ((stop, "freeze"), (open_, "release")):
        assert argv[1].endswith(f"scripts/{WRITERS}")
        assert argv[2] == verb and argv[3] == str(root.resolve())
    assert str(root.resolve()) in stop
    # 环境变量覆盖走同一条路（deploy.sh 与直连入口都继承进程环境）
    monkeypatch.setenv("CONSOLE_CUTOVER_STOP_CMD", "true stop-declared")
    assert runtime.cutover_default_actions(root)["stop_cmd"] == ["true", "stop-declared"]


# --- 写者冻结/恢复：配置面拒绝（无需 PG） ---------------------------------------


def test_freeze_refuses_without_any_declaration(repo):
    """未声明任何写者时拒绝——「什么都没有」不能等同「已经停干净」（§8.3）。"""
    root, _commit = repo
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / WRITERS), "freeze", str(root)],
        cwd=str(root), capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "未声明" in proc.stdout + proc.stderr


def test_release_without_record_refuses(repo):
    root, _commit = repo
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / WRITERS), "release", str(root)],
        cwd=str(root), capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "无冻结记录" in proc.stdout + proc.stderr


def test_first_freeze_env_interruption_keeps_original_baseline(repo, monkeypatch):
    """The first durable side effect can be interrupted before its receipt is written."""
    import cutover_writers as writers

    root, _commit = repo
    deploy_env = root / "writer.env"
    deploy_env.write_text("CONSOLE_MAINTENANCE_MODE=normal\n")
    original = writers._set_env_value

    def interrupt_after_write(*args):
        original(*args)
        raise KeyboardInterrupt("injected first env-write interruption")

    with monkeypatch.context() as patch:
        patch.setattr(writers, "_set_env_value", interrupt_after_write)
        with pytest.raises(KeyboardInterrupt):
            writers.freeze(root, deploy_env=deploy_env)
    record = writers.read_record(root)
    assert record["baseline"]["deploy_env"]["previous"] == "normal"
    assert "read_only" in deploy_env.read_text()
    writers.freeze(root, deploy_env=deploy_env)
    writers.release(root)
    assert "CONSOLE_MAINTENANCE_MODE=normal" in deploy_env.read_text()


@pytest.mark.parametrize("interrupted_sql", ["ON ALL TABLES", "ON ALL SEQUENCES"])
def test_first_freeze_disconnect_recovers_all_original_grants(app_db, repo, monkeypatch, interrupted_sql):
    import cutover_writers as writers

    root, _commit = repo
    _env, container, name = app_db
    dsn = container.dsn(dbname=name)
    deploy_env = root / "writer.env"
    deploy_env.write_text("CONSOLE_MAINTENANCE_MODE=normal\n")

    class InterruptedConnection:
        def __init__(self):
            self.conn = support.connect(dsn, autocommit=True)

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, sql, *args):
            result = self.conn.execute(sql, *args)
            if sql.startswith("REVOKE") and interrupted_sql in sql:
                self.conn.close()
                raise KeyboardInterrupt("injected disconnect after first revoke")
            return result

    with monkeypatch.context() as patch:
        patch.setattr(writers, "_connect", InterruptedConnection)
        with pytest.raises(KeyboardInterrupt):
            writers.freeze(root, roles=("console_nas", "console_mac"), deploy_env=deploy_env)
    record = writers.read_record(root)
    for role in ("console_nas", "console_mac"):
        assert record["baseline"]["roles"][role]["tables"]["ledger_tx"]["INSERT"] is True
    # The table revoke must roll back together with the sequence revoke on disconnect.
    writable, error = _as_role(dsn, "console_nas", "INSERT INTO ledger_tx (amount) VALUES (1.00)")
    assert writable, error
    monkeypatch.setattr(writers, "_connect", lambda: support.connect(dsn, autocommit=True))
    writers.freeze(root, roles=("console_nas", "console_mac"), deploy_env=deploy_env)
    denied, error = _as_role(dsn, "console_nas", "INSERT INTO ledger_tx (amount) VALUES (2.00)")
    assert not denied and error == "InsufficientPrivilege"
    released = writers.release(root)
    assert all(data["verified"] for data in released["releases"][-1]["roles"].values())
    writable, error = _as_role(dsn, "console_nas", "INSERT INTO ledger_tx (amount) VALUES (3.00)")
    assert writable, error
    assert "CONSOLE_MAINTENANCE_MODE=normal" in deploy_env.read_text()


def test_freeze_requires_pg_config_when_roles_declared(repo):
    """声明了角色却没有 PG 配置 → 配置错误（2），不许静默跳过冻结。"""
    root, _commit = repo
    env = {key: value for key, value in os.environ.items() if not key.startswith("CONSOLE_PG_")}
    env["CONSOLE_STORAGE_ENGINE"] = "postgres"
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / WRITERS), "freeze", str(root), "--role", "console_nas"],
        cwd=str(root), capture_output=True, text=True, timeout=60, env=env, check=False,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "配置" in proc.stdout + proc.stderr


# --- 真 PG16：角色权限冻结/恢复 --------------------------------------------------


@pytest.fixture
def app_db(pg_container, pg_dsn):
    """隔离库 + §6.3 全套角色 + schema12（真 PG16）。

    yield `(env, container, dbname)`：`env` 是跑 CLI 子进程用的环境，`dbname` 用来
    拼**本库**连接（角色权限是每库一套，不能拿集群默认库核对）。
    """
    from pg03_test_fixtures import drop_database, fresh_database

    admin = connect(pg_dsn, autocommit=True)
    name = fresh_database(admin)
    env = pg_env(pg_container, name)
    with support.connect(pg_container.dsn(dbname=name), autocommit=True) as conn:
        conn.row_factory = psycopg.rows.dict_row
        pg_migrations.bootstrap_roles(conn, database=name)
        out = pg_migrations.apply_as_owner(conn)
        assert out["role_switched"] is True
    yield env, pg_container, name
    # 只删库：表级 grant 与 default ACL 随库消失；集群级角色留着（bootstrap 幂等，
    # 多删反而会撞上其它库的依赖）。
    drop_database(admin, name)
    admin.close()


def _as_role(dsn, role, statement, params=None):
    """以 `SET ROLE` 落到目标角色执行语句；返回 (ok, error_type)。

    用 `SET ROLE` 而不是另开账号连接：判定的就是**该角色自己的 ACL**，
    且不受容器认证方式影响。当前 DSN 账号（自建容器模式下是 `console_test_admin`，
    external 模式下是外部库的超管）本身就是超管，测试侧先把它加进目标角色
    （与被测权限面无关，只为能切身份）。
    """
    admin = connect(dsn, autocommit=True)
    try:
        admin.execute(f'GRANT "{role}" TO current_user')
        admin.execute(f'SET ROLE "{role}"')
        try:
            admin.execute(statement, params)
        finally:
            admin.execute("RESET ROLE")
        return True, None
    except Exception as exc:  # noqa: BLE001 - 测试要的就是「被拒绝」与拒绝类型
        return False, type(exc).__name__
    finally:
        admin.close()


def test_freeze_revokes_app_role_writes_and_release_restores(app_db, repo, pg_dsn):
    """G03 核心：冻结后 DML 被拒（SELECT 保留），恢复后 DML 回来——真 PG16。"""
    root, _commit = repo
    env, container, name = app_db
    dsn = container.dsn(dbname=name)

    def write(statement, params=None):
        return _as_role(dsn, "console_nas", statement, params)

    ok, err = write("INSERT INTO ledger_tx (month, direction, amount) VALUES ('2026-09', 'out', 1.00)")
    assert ok, err  # 前提出清：未冻结时运行账号确实能写

    freeze = subprocess.run(
        [sys.executable, str(SCRIPTS / WRITERS), "freeze", str(root),
         "--role", "console_nas"],
        cwd=str(root), capture_output=True, text=True, timeout=120, env=env, check=False,
    )
    assert freeze.returncode == 0, freeze.stdout + freeze.stderr
    payload = json.loads(freeze.stdout)
    assert payload["status"] == "frozen"
    assert payload["verified"]["console_nas"]["insert_denied"] is True
    assert payload["verified"]["console_nas"]["select_kept"] is True

    denied, err_type = write(
        "INSERT INTO ledger_tx (month, direction, amount) VALUES ('2026-09', 'out', 2.00)")
    assert denied is False and err_type == "InsufficientPrivilege", (denied, err_type)
    # §6.4「临时 SELECT 权限」：读通道保留，管理/诊断不被冻结挡住
    readable, err = write("SELECT count(*) FROM ledger_tx")
    assert readable is True, err

    # 新连接（等价于 app 重启/重连）同样被拒：冻结不依赖控制器进程存活
    again, err_type = _as_role(dsn, "console_nas", "DELETE FROM ledger_tx WHERE id = -999")
    assert again is False and err_type == "InsufficientPrivilege"

    # 管理恢复通道（console_admin）全程可用，且能继续迁移
    out = pg_migrations.apply_as_owner(connect(dsn, autocommit=True))
    assert out["applied"] == [] and out["role_switched"] is True

    release = subprocess.run(
        [sys.executable, str(SCRIPTS / WRITERS), "release", str(root)],
        cwd=str(root), capture_output=True, text=True, timeout=120, env=env, check=False,
    )
    assert release.returncode == 0, release.stdout + release.stderr
    assert json.loads(release.stdout)["status"] == "released"
    restored, err = write(
        "INSERT INTO ledger_tx (month, direction, amount) VALUES ('2026-09', 'out', 3.00)")
    assert restored is True, err
    record = json.loads((root / "backup/cutover/writers-freeze.json").read_text("utf-8"))
    assert record["status"] == "released"
    assert record["baseline"]["roles"]["console_nas"]["tables"]["ledger_tx"]["INSERT"] is True
    assert len(record["releases"]) == 1


def test_refreeze_keeps_original_baseline_and_restores_all_writers(app_db, repo):
    """G03/G04：冻结**可重入**——二次冻结不许把「已撤权」当基线，也不许丢写者。

    状态机重入会按 pending 重跑 `frozen`（stop_cmd 再执行一次）。首红实测：第二次
    冻结读到的权限面已经是撤权后的，release 照它「恢复」→ 库里永久拒写。同时第二次
    调用只声明了角色（没声明部署 env），但 env 仍处于 read_only 冻结态，不能因此被
    排除在恢复之外。
    """
    root, _commit = repo
    env, _container, _name = app_db
    deploy_env = root / "console" / ".env"
    deploy_env.write_text("CONSOLE_MAINTENANCE_MODE=normal\n", encoding="utf-8")

    first = subprocess.run(
        [sys.executable, str(SCRIPTS / WRITERS), "freeze", str(root),
         "--role", "console_nas", "--deploy-env", str(deploy_env)],
        cwd=str(root), capture_output=True, text=True, timeout=120, env=env, check=False)
    assert first.returncode == 0, first.stdout + first.stderr

    second = subprocess.run(
        [sys.executable, str(SCRIPTS / WRITERS), "freeze", str(root), "--role", "console_nas"],
        cwd=str(root), capture_output=True, text=True, timeout=120, env=env, check=False)
    assert second.returncode == 0, second.stdout + second.stderr
    record = json.loads((root / "backup/cutover/writers-freeze.json").read_text("utf-8"))
    assert record["refrozen"]["roles"] == ["console_nas"]
    assert record["baseline"]["roles"]["console_nas"]["tables"]["ledger_tx"]["INSERT"] is True
    assert record["effective"]["deploy_env"] == str(deploy_env)
    assert record["verified"]["console_nas"]["insert_denied"] is True  # 当场复核，不看上一轮结论

    release = subprocess.run(
        [sys.executable, str(SCRIPTS / WRITERS), "release", str(root)],
        cwd=str(root), capture_output=True, text=True, timeout=120, env=env, check=False)
    assert release.returncode == 0, release.stdout + release.stderr
    ok, err = _as_role(_container.dsn(dbname=_name), "console_nas",
                       "INSERT INTO ledger_tx (month, direction, amount)"
                       " VALUES ('2026-09','out',4.00)")
    assert ok, err
    assert "CONSOLE_MAINTENANCE_MODE=normal" in deploy_env.read_text(encoding="utf-8")


def test_freeze_refuses_superuser_and_owner_roles(app_db, repo):
    """REVOKE 对超管/属主角色无效 → 必须**先拒绝**而不是假装冻住了（fail closed）。

    退出码 2（配置错误）：重试无用、要人改配置——与「停写命令失败」的运行期错误区分开。
    """
    root, _commit = repo
    env, _container, _name = app_db
    # 超管样本取**当前 DSN 的账号**，不要写字面量 `console_test_admin`：那个角色只在
    # 「docker 自建底座」模式下存在；CI 用的 external 模式（postgres service +
    # CONSOLE_PG_TEST_DSN）下不存在，本用例会以「角色不存在」通过 rc=2 但打不出
    # 「超级用户」而误红（2026-09-11 实测，与 test_pg_app_wiring 的凭据修复同源）。
    for role, marker in ((_container.user, "超级用户"), ("console_owner", "属主")):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / WRITERS), "freeze", str(root), "--role", role],
            cwd=str(root), capture_output=True, text=True, timeout=120, env=env, check=False,
        )
        assert proc.returncode == 2, f"{role}: {proc.stdout}{proc.stderr}"
        assert marker in proc.stdout + proc.stderr, (role, proc.stdout + proc.stderr)
    # 失败不留「已冻结」记录，避免后续 release 对着不存在的冻结做恢复
    record = root / "backup/cutover/writers-freeze.json"
    if record.exists():
        assert json.loads(record.read_text("utf-8"))["status"] != "frozen"


def test_freeze_writes_persistent_read_only_env_and_fresh_process_refuses(app_db, repo):
    """G03 持久腿：env 文件写 `read_only`，**新起的进程**仍拒写；恢复后放行。"""
    root, _commit = repo
    env, _container, _name = app_db
    deploy_env = root / "console" / ".env"
    deploy_env.write_text("# 部署环境\nCONSOLE_INSTANCE_ROLE=authority\n"
                          "CONSOLE_MAINTENANCE_MODE=normal\n", encoding="utf-8")

    freeze = subprocess.run(
        [sys.executable, str(SCRIPTS / WRITERS), "freeze", str(root),
         "--deploy-env", str(deploy_env)],
        cwd=str(root), capture_output=True, text=True, timeout=120, env=env, check=False,
    )
    assert freeze.returncode == 0, freeze.stdout + freeze.stderr
    text = deploy_env.read_text(encoding="utf-8")
    assert "CONSOLE_MAINTENANCE_MODE=read_only" in text
    assert "CONSOLE_INSTANCE_ROLE=authority" in text  # 其它行原样保留
    assert "# 部署环境" in text

    # 新进程读该文件：写入被拒（这是「重启后仍拒写」的可执行证据）
    probe = ("import sys; sys.path.insert(0, %r); import runtime_mode;"
             "assert runtime_mode.writes_allowed() is False, 'read_only 未生效';"
             "assert runtime_mode.is_read_only() is True;"
             "print(runtime_mode.snapshot()['maintenance_mode'])"
             % str(BACKEND))
    fresh = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                           env={**env, "CONSOLE_MAINTENANCE_MODE": "read_only"}, check=False)
    assert fresh.returncode == 0, fresh.stdout + fresh.stderr
    assert fresh.stdout.strip() == "read_only"

    release = subprocess.run(
        [sys.executable, str(SCRIPTS / WRITERS), "release", str(root), "--deploy-env", str(deploy_env)],
        cwd=str(root), capture_output=True, text=True, timeout=120, env=env, check=False,
    )
    assert release.returncode == 0, release.stdout + release.stderr
    restored_text = deploy_env.read_text(encoding="utf-8")
    assert "CONSOLE_MAINTENANCE_MODE=normal" in restored_text
    assert "read_only" not in restored_text


def test_freeze_partial_failure_keeps_record_and_release_undoes(repo):
    """停写命令失败：进程非 0 退出，但已完成的步骤留档，release 能原样撤销。"""
    root, _commit = repo
    deploy_env = root / "console" / ".env"
    deploy_env.write_text("CONSOLE_MAINTENANCE_MODE=normal\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / WRITERS), "freeze", str(root),
         "--deploy-env", str(deploy_env), "--stop-writers",
         f"{sys.executable} -c raise-systemexit"],
        cwd=str(root), capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    record = json.loads((root / "backup/cutover/writers-freeze.json").read_text("utf-8"))
    assert record["status"] == "partial"
    assert record["steps"][-1]["name"] == "stop_writers"
    assert "CONSOLE_MAINTENANCE_MODE=read_only" in deploy_env.read_text("utf-8")

    release = subprocess.run(
        [sys.executable, str(SCRIPTS / WRITERS), "release", str(root), "--deploy-env", str(deploy_env)],
        cwd=str(root), capture_output=True, text=True, timeout=120, check=False,
    )
    assert release.returncode == 0, release.stdout + release.stderr
    assert "CONSOLE_MAINTENANCE_MODE=normal" in deploy_env.read_text("utf-8")


# --- G01：两条入口真跑（deploy.sh 与直连 CLI），走真实冻结 ------------------------


def _gate_stub_env(root):
    """首备份恢复门禁（V4-03）的替身命令。

    本文件测的是停写/恢复语义与两条入口的接线，不是门禁本身；真实门禁
    （真 pg_dump → 隔离 restore → 逐值/金额比较）由
    `test_backup_runtime_cutover.py::test_cutover_reentry_after_open_failure_on_nonempty_target`
    与 `test_backup_gate_blocks_open_when_restore_target_dead` 覆盖。替身同样要写出
    **绑定当前状态**的回执，控制器照常 `validate_restore_receipt` 复核。
    """
    stub = root / "backup/cutover/receipt-stub.py"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        f"sys.path.insert(0, {str(BACKEND)!r})\n"
        "from test_backup_runtime_cutover import backup_receipt_stub\n"
        "backup_receipt_stub(sys.argv[1] if len(sys.argv) > 1 else '.')\n",
        encoding="utf-8")
    return {"CONSOLE_CUTOVER_BACKUP_RESTORE_CMD": f"{sys.executable} {stub} {root}"}


@pytest.fixture
def cutover_repo(repo):
    """真迁移需要的合成源 + PG-03/写者脚本（子进程要能找到）。"""
    root, commit = repo
    _seed_pg03_into_repo(root)
    shutil.copy2(SCRIPTS / WRITERS, root / "scripts" / WRITERS)
    legacy = root / "console/backend/data/console.db"
    for stale in (legacy, *legacy.parent.glob("console.db-*")):
        stale.unlink(missing_ok=True)
    make_legacy_db(legacy)
    return root, commit


def test_cutover_through_standard_deploy_entry_uses_reviewed_freeze(
        cutover_repo, pg_container, pg_dsn):
    """G01+G03 端到端：**标准 deploy 入口**跑完整割接，冻结/恢复走仓库内实现。

    这里刻意不传 `--stop-cmd/--open-cmd`：走缺省的 `cutover_writers.py`，
    只通过环境声明要冻结的运行账号（现场配置，不是手拼 shell）。
    """
    if not _pg_available():
        raise RuntimeError("docker 不可用：deploy 入口端到端无法执行（不伪 skip）")
    root, commit = cutover_repo
    admin = connect(pg_dsn, autocommit=True)
    name = f"pg05deploy_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    base_env = pg_env(pg_container, name)
    with support.connect(pg_container.dsn(dbname=name), autocommit=True) as conn:
        conn.row_factory = psycopg.rows.dict_row
        pg_migrations.bootstrap_roles(conn, database=name)
    env = {**base_env, **_gate_stub_env(root),
           "CONSOLE_CUTOVER_APP_ROLES": "console_nas",
           "PATH": f"{VENV_BIN}:{os.environ.get('PATH', '')}"}
    try:
        assert subprocess.run([sys.executable, str(SCRIPTS / "pg_schema.py"), "migrate"],
                              cwd=str(root), capture_output=True, text=True, env=env,
                              check=False).returncode == 0
        proc = subprocess.run(
            ["bash", str(root / "scripts/deploy.sh"), "--storage-cutover",
             "--target-commit", commit, "--yes",
             # 这些用例测的是停写/恢复语义与两条入口的接线：准备阶段显式跳过
             # （真准备阶段见 test_cutover_prepare.py 与 V4-02 演练）
             "--skip-prepare"],
            cwd=str(root), capture_output=True, text=True, env=env, timeout=600, check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["state"] == "completed"
        # 冻结真的做过，而且冻结/恢复的两次调用都进了 evidence
        assert payload["evidence"]["stop_report"]["exit"] == 0
        assert payload["evidence"]["open_report"]["exit"] == 0
        record = json.loads((root / "backup/cutover/writers-freeze.json").read_text("utf-8"))
        assert record["status"] == "released"
        assert record["baseline"]["roles"]["console_nas"]["tables"]["ledger_tx"]["INSERT"] is True
        # 恢复后运行账号重新可写（开放后的真实副作用，不只看 state 字符串）
        ok, err = _as_role(pg_container.dsn(dbname=name), "console_nas",
                           "INSERT INTO renewals (id, name, cost) VALUES ('g01-probe', 'probe', 1.00)")
        assert ok, err
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def test_cutover_through_deploy_entry_bootstraps_uninitialized_target(
        cutover_repo, pg_container, pg_dsn):
    """G02 关闭标准：**未初始化**的隔离 PG 目标经真实外置入口一次跑通。

    只 `CREATE DATABASE`——没有角色、没有 schema。角色 bootstrap 与最新 schema 由
    目标就绪检查就地完成（受维护锁保护），证据里必须能看出「目标原本是空的」、
    「实际执行的代码身份」以及首尾身份一致（判据 C：身份可事后复现）。
    """
    if not _pg_available():
        raise RuntimeError("docker 不可用：deploy 入口端到端无法执行（不伪 skip）")
    root, commit = cutover_repo
    admin = connect(pg_dsn, autocommit=True)
    name = f"pg05boot_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    env = {**pg_env(pg_container, name), **_gate_stub_env(root),
           "CONSOLE_CUTOVER_APP_ROLES": "console_nas",
           "PATH": f"{VENV_BIN}:{os.environ.get('PATH', '')}"}
    try:
        proc = subprocess.run(
            ["bash", str(root / "scripts/deploy.sh"), "--storage-cutover",
             "--target-commit", commit, "--yes",
             # 这些用例测的是停写/恢复语义与两条入口的接线：准备阶段显式跳过
             # （真准备阶段见 test_cutover_prepare.py 与 V4-02 演练）
             "--skip-prepare"],
            cwd=str(root), capture_output=True, text=True, env=env, timeout=600, check=False)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["state"] == "completed"
        pre = payload["evidence"]["target_preflight"]
        assert pre["initialized"] is False        # 证据留痕：目标原本是空的
        assert pre["bootstrapped"] is True
        assert pre["schema_version"] == pg_migrations.latest_schema_version()
        assert set(pre["row_counts"].values()) == {0}   # 就绪检查时业务表全空
        declared = {pg_migrations.OWNER_ROLE, pg_migrations.ADMIN_ROLE,
                    *pg_migrations.APP_ROLES}
        assert set(pre["roles_present"]) | set(pre["roles_missing"]) == declared
        execution = payload["evidence"]["execution"]
        assert execution["head"] == commit and execution["head_matches_target"] is True
        assert payload["evidence"]["execution_final"]["files"] == execution["files"]
        # 目标真的就绪了：最新 schema + 业务数据段位一致
        with support.connect(pg_container.dsn(dbname=name), autocommit=True) as conn:
            conn.row_factory = psycopg.rows.dict_row
            assert pg_migrations.current_version(conn) == pg_migrations.latest_schema_version()
            for table in ("ledger_tx", "renewals"):
                src = sqlite3.connect(root / "console/backend/data/console.db").execute(
                    f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                assert conn.execute(
                    f'SELECT COUNT(*) AS c FROM "{table}"').fetchone()["c"] == src
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def test_freeze_survives_controller_kill_and_blocks_writes(
        cutover_repo, pg_container, pg_dsn):
    """G03 首红第 ③ 行回归：**杀掉控制器**之后业务写仍被拒。

    首红实测是「控制器一死，业务写就成功」（advisory 锁随会话消失）。本用例真的把
    控制器进程 kill -9：停写动作先跑真实冻结，再杀父进程。此后不依赖任何存活的
    控制器进程，只靠持久兜底（PG 账号权限 + 部署 env 的 read_only）拒写；随后重入
    标准入口，状态机按 pending 重跑 frozen 并跑完整条链到 completed。
    """
    if not _pg_available():
        raise RuntimeError("docker 不可用：控制器被杀排练无法执行（不伪 skip）")
    root, commit = cutover_repo
    admin = connect(pg_dsn, autocommit=True)
    name = f"pg05kill_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_container.dsn(dbname=name)
    deploy_env = root / "console" / ".env"
    deploy_env.write_text("CONSOLE_INSTANCE_ROLE=authority\n"
                          "CONSOLE_MAINTENANCE_MODE=normal\n", encoding="utf-8")
    # 停写动作 = 真实冻结 + 杀掉控制器（模拟进程被 kill / 宿主机掉电），
    # 控制器不可能再执行任何收尾或恢复动作。
    killer = root / "scripts" / "kill_after_freeze.sh"
    killer.write_text(
        "#!/bin/bash\nset -e\n"
        f"'{sys.executable}' '{root / 'scripts' / WRITERS}' freeze '{root}'"
        f" --role console_nas --deploy-env '{deploy_env}'\n"
        "kill -9 $PPID\n", encoding="utf-8")
    first_env = {**pg_env(pg_container, name), **_gate_stub_env(root),
                 "CONSOLE_CUTOVER_STOP_CMD": f"bash {killer}",
                 "PATH": f"{VENV_BIN}:{os.environ.get('PATH', '')}"}
    resume_env = {**pg_env(pg_container, name), **_gate_stub_env(root),
                  "CONSOLE_CUTOVER_APP_ROLES": "console_nas",
                  "PATH": f"{VENV_BIN}:{os.environ.get('PATH', '')}"}
    try:
        first = subprocess.run(
            ["bash", str(root / "scripts/deploy.sh"), "--storage-cutover",
             "--target-commit", commit, "--yes",
             # 这些用例测的是停写/恢复语义与两条入口的接线：准备阶段显式跳过
             # （真准备阶段见 test_cutover_prepare.py 与 V4-02 演练）
             "--skip-prepare"],
            cwd=str(root), capture_output=True, text=True, env=first_env,
            timeout=600, check=False)
        assert first.returncode != 0, first.stdout + first.stderr   # 控制器被 SIGKILL
        persisted = _state(root)
        assert persisted["state"] == "prepared"        # 没有 frozen 的完成回执
        assert persisted["pending"]["state"] == "frozen"
        assert (root / "backup/cutover/writers-freeze.json").is_file()

        # ① 控制器已死，业务写仍被拒绝（权限冻结是库侧持久事实）
        denied, err_type = _as_role(
            dsn, "console_nas",
            "INSERT INTO ledger_tx (month, direction, amount) VALUES ('2026-09','out',9.00)")
        assert denied is False and err_type == "InsufficientPrivilege", (denied, err_type)
        # ② 读通道保留（诊断/管理不被冻结挡住）
        readable, err = _as_role(dsn, "console_nas", "SELECT count(*) FROM ledger_tx")
        assert readable is True, err
        # ③ 部署 env 也是持久的：新进程读到 read_only
        assert "CONSOLE_MAINTENANCE_MODE=read_only" in deploy_env.read_text(encoding="utf-8")

        # ④ 重入标准入口：按 pending 重跑 frozen 并走完整条链
        second = subprocess.run(
            ["bash", str(root / "scripts/deploy.sh"), "--storage-cutover",
             "--target-commit", commit, "--yes",
             # 这些用例测的是停写/恢复语义与两条入口的接线：准备阶段显式跳过
             # （真准备阶段见 test_cutover_prepare.py 与 V4-02 演练）
             "--skip-prepare"],
            cwd=str(root), capture_output=True, text=True, env=resume_env,
            timeout=600, check=False)
        assert second.returncode == 0, second.stdout + second.stderr
        payload = json.loads(second.stdout)
        assert payload["state"] == "completed"
        assert payload["evidence"]["open_report"]["exit"] == 0
        record = json.loads((root / "backup/cutover/writers-freeze.json").read_text("utf-8"))
        assert record["status"] == "released"
        # 冻结可重入：重跑 frozen 时保留的是**最早**（冻结前）的基线，而不是读到
        # 「已撤权」当基线——否则 release 恢复不回来（本用例首跑就踩到这个）。
        assert record["refrozen"]["roles"] == ["console_nas"]
        assert record["baseline"]["roles"]["console_nas"]["tables"]["ledger_tx"]["INSERT"] is True
        # 重入继承第一次声明的写者：本次没声明部署 env，但它仍处在 read_only 冻结态
        assert record["effective"]["deploy_env"] == str(deploy_env)
        assert "CONSOLE_MAINTENANCE_MODE=normal" in deploy_env.read_text(encoding="utf-8")
        # 只有「已验证开放」之后 DML 才回来
        restored, err = _as_role(dsn, "console_nas",
                                 "INSERT INTO ledger_tx (month, direction, amount)"
                                 " VALUES ('2026-09','out',10.00)")
        assert restored is True, err
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def test_cutover_direct_cli_honours_env_declared_writer_commands(
        cutover_repo, pg_container, pg_dsn):
    """直连入口（`backup_runtime.py cutover`）同样能用环境声明的停写/恢复命令。"""
    if not _pg_available():
        raise RuntimeError("docker 不可用：直连入口端到端无法执行（不伪 skip）")
    root, commit = cutover_repo
    admin = connect(pg_dsn, autocommit=True)
    name = f"pg05direct_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    stopped = root / "backup/cutover/stopped.marker"
    started = root / "backup/cutover/started.marker"
    env = {**pg_env(pg_container, name), **_gate_stub_env(root)}
    # 现场配置形态：环境变量给命令串（shlex 切分），指向站点自己的停/启脚本
    stop_script = root / "scripts" / "site_stop_writers.sh"
    start_script = root / "scripts" / "site_start_writers.sh"
    stop_script.write_text(f"#!/bin/bash\ntouch '{stopped}'\n", encoding="utf-8")
    start_script.write_text(f"#!/bin/bash\ntouch '{started}'\n", encoding="utf-8")
    env["CONSOLE_CUTOVER_STOP_CMD"] = f"bash {stop_script}"
    env["CONSOLE_CUTOVER_OPEN_CMD"] = f"bash {start_script}"
    try:
        with support.connect(pg_container.dsn(dbname=name), autocommit=True) as conn:
            conn.row_factory = psycopg.rows.dict_row
            pg_migrations.apply_as_owner(conn)
        proc = _cli(root, "cutover", str(root), "--target-commit", commit, "--yes",
                    "--skip-prepare", env=env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert json.loads(proc.stdout)["state"] == "completed"
        assert stopped.is_file() and started.is_file()
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()
