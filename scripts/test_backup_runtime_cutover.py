#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cutover 状态机自测（PG-04，提案 §8.3）。

覆盖三条主路径：正常推进、缺注入命令时拒绝推进并可重入、writes_opened
先持久化后执行开放副作用。全部用例在 tmp_path 合成仓库上进行，不触碰
真实数据目录。

**PG-05 集成收口**：迁移器/验证器不再是替身——`cutover_default_actions()`
接的是 PG-03 真实 CLI（`migrate_sqlite_to_postgres.py --mode apply` +
独立 `verify_postgres_migration.py`），`test_cutover_real_cli_end_to_end`
用真 PG16 跑完整状态机。其余用例用可调用替身只为验证状态机语义（注入点仍
保留，PG-07 现场命令要走同一条路），不再代表「真实 CLI 未接入」。
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import psycopg
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


def _pg_available():
    return bool(os.environ.get("CONSOLE_PG_TEST_NATIVE_BIN")) or support.docker_available()


def _pay_recovery_stub(root, dump):
    """The state-machine stub still creates and verifies real companion bytes."""
    pay = runtime.backup_db.backup_pay
    source = root / "synthetic-pay/raw/original.csv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"synthetic,original\n")
    member = pay.write_pay_snapshot(Path(str(dump) + pay.SUFFIX), root=root,
                                    pay_root=root / "synthetic-pay", query=lambda sql: [])
    manifest = {"manifest_version": "pg-backup-manifest-1", "engine": "postgres",
                "members": [{"path": dump.name, "sha256": runtime.file_digest(dump),
                             "bytes": dump.stat().st_size}, member]}
    Path(str(dump) + runtime.backup_db.MANIFEST_SUFFIX).write_text(json.dumps(manifest))
    target = Path(tempfile.mkdtemp(dir=dump.parent, prefix="stub-restore-")) / "pay"
    return {**pay.restore_pay_snapshot(dump, manifest, dest=target, query=lambda sql: []),
            "path": str(target)}


@pytest.fixture
def repo(tmp_path):
    repo = tds.make_repo(tmp_path)
    conn = tds.make_db(repo / "console/backend/data/console.db")
    conn.close()
    commit = tds.git(repo, "rev-parse", "HEAD").strip()
    return repo, commit


def _state_file(repo, state_dir=None):
    return runtime.cutover_state_path(repo, state_dir)


def backup_receipt_stub(root, env=None):
    """`backup_verify` 状态的替身动作：写一份**与当前状态绑定**的门禁回执。

    真实验收（真 `pg_dump` → 隔离 restore → 逐值比较）由真 PG16 用例
    （`test_cutover_reentry_after_open_failure_on_nonempty_target`、
    `test_backup_gate_blocks_open_when_restore_fails`）覆盖；这里只让状态机语义用例
    能走过 `backup_verified`。替身**也走** `validate_restore_receipt`——字段不齐、
    来源/候选/目标对不上时照样被拒。
    """
    root = Path(root)
    state = runtime.read_cutover_state(root)
    dump = root / "backup/cutover/console.dump.stub"
    dump.parent.mkdir(parents=True, exist_ok=True)
    dump.write_bytes(b"stub-dump")
    gate = Path(runtime.__file__).resolve().parent / "cutover_backup_restore.py"
    pay = _pay_recovery_stub(root, dump)
    receipt = {
        "format": runtime.CUTOVER_RESTORE_RECEIPT_FORMAT,
        "status": "ok",
        "source": {"sha256": state["evidence"]["cutover_source"]["sha256"]},
        "execution": {"digest": runtime.execution_digest(
            runtime.cutover_execution_identity(root, state["target_commit"]))},
        "gate": {"script": "scripts/cutover_backup_restore.py",
                 "sha256": runtime.file_digest(gate)},
        "target": {"database": ((state["evidence"].get("target_preflight") or {})
                                .get("server") or {}).get("database") or "stub-target"},
        "dump": {"path": str(dump), "sha256": runtime.file_digest(dump), "name": dump.name},
        "comparison": {"mismatches": [], "tables": ["ledger_tx"]},
        "restored": {"restored": True, "row_digest_match": True,
                     "schema_version": pg_migrations.latest_schema_version(), "pay": pay},
        "file_mapping": {"ledger_file_batches": {"restored": 0, "expected_empty": True}, "pay": pay},
        "cleanup": {"restore_database": "cu_restore_stub", "dropped": True},
    }
    (root / runtime.CUTOVER_RESTORE_REPORT_RELATIVE).write_text(
        json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
    return "backup-verified-stub"


def _no_pg(**kwargs):
    """状态机语义用例的调用参数：显式声明**不接 PG**（`maintenance_lock=False`）。

    PG-05 review B-2 修复后 `run_cutover` 默认要求控制器持 PG 独占维护锁（§7.3），
    下列用例目的是验证状态机转移/落盘/拒绝语义，没有 PG 环境也不该假装有——
    显式关掉比隐式跳过诚实。**真的锁行为**由 `test_cutover_real_cli_end_to_end`
    （真 PG16）与 `test_cutover_holds_maintenance_lock_across_freeze` 覆盖。

    首备份恢复门禁（V4-03）同理：语义用例注入绑定当前状态的替身回执，真实门禁由
    真 PG16 用例覆盖。
    """
    actions = {"backup_restore_cmd": backup_receipt_stub, **kwargs.pop("actions", {})}
    return {**kwargs, "maintenance_lock": False, "target_preflight": False,
            "actions": actions}


def test_cutover_default_actions_wire_real_cli(repo):
    """缺省动作是 PG-03 真实 CLI，且源恒为 cutover 最后快照（§5.3）。

    默认值是**运行期求值**的可调用对象（`--source-sha256` 要等 `frozen` 写出快照），
    所以这里断言 argv 构造而不是现成列表。
    """
    repo, commit = repo
    actions = runtime.cutover_default_actions(repo, commit)
    # G01：停写/恢复也有缺省实现（`cutover_writers.py`），不再是「现场自己拼」；
    # V4-03：首备份恢复门禁（`cutover_backup_restore.py`）同样是缺省实现。
    assert set(actions) == {"migrator", "verify_cmd", "stop_cmd", "open_cmd",
                            "backup_restore_cmd"}
    # 迁移器/验证器要等 `frozen` 写出快照后才求值 → callable；停写/恢复是
    # 固定 argv（`cutover_writers.py freeze|release`），可直接审阅。
    assert callable(actions["migrator"]) and callable(actions["verify_cmd"])
    assert callable(actions["backup_restore_cmd"])
    for key in ("stop_cmd", "open_cmd"):
        assert isinstance(actions[key], list) and str(actions[key][1]).endswith(".py"), actions[key]

    # V4-03：门禁 argv 指向真实现，源恒为冻结快照，且带候选执行身份
    restore_argv = runtime._backup_restore_argv(repo, commit, repo / "backup/cutover/r.json")
    assert restore_argv[1].endswith("scripts/cutover_backup_restore.py")
    assert restore_argv[restore_argv.index("--source") + 1] == str(runtime.cutover_source_path(repo))
    assert restore_argv[restore_argv.index("--target-commit") + 1] == commit

    source = runtime.cutover_source_path(repo)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"snapshot-bytes")
    migrator = runtime._migrator_argv(repo, "apply", repo / "backup/cutover/m.json")
    verify = runtime._verifier_argv(repo, repo / "backup/cutover/v.json")
    assert migrator[1].endswith("scripts/migrate_sqlite_to_postgres.py")
    assert verify[1].endswith("scripts/verify_postgres_migration.py")
    for argv in (migrator, verify):
        assert argv[argv.index("--source") + 1] == str(source)
    assert migrator[migrator.index("--mode") + 1] == "apply"
    assert migrator[migrator.index("--target-purpose") + 1] == "production"
    # apply 必须带源指纹（§5.3：无 --source-sha256 直接拒绝）
    assert migrator[migrator.index("--source-sha256") + 1] == runtime.file_digest(source)
    # 独立验证器不读搬运器报告，只走 --target env
    assert verify[verify.index("--target") + 1] == "env"
    assert "--source-sha256" not in verify



def copy_backend_module(source_root, name, dest_dir):
    """Copy implementation bytes into a fixture repo ``console/backend``.

    Prefer ``kernel/backend`` when the console path is only a relocating shim.
    """
    import shutil
    console = Path(source_root) / "console" / "backend" / name
    kernel = Path(source_root) / "kernel" / "backend" / name
    src = console
    if console.is_file():
        head = console.read_text(encoding="utf-8", errors="ignore")[:240]
        if "Thin relocating shim" in head and kernel.is_file():
            src = kernel
    elif kernel.is_file():
        src = kernel
    else:
        raise FileNotFoundError(console)
    shutil.copy2(src, Path(dest_dir) / name)

def _seed_pg03_into_repo(repo):
    """把 PG-03 CLI 及其依赖复制进最小夹具仓库（真实 CLI 要能被子进程找到）。"""
    import shutil

    source = Path(__file__).resolve().parents[1]
    scripts = ("migrate_sqlite_to_postgres.py", "verify_postgres_migration.py",
               "pg_migration_lib.py", "pg_schema.py")
    modules = ("money.py", "pgdb.py", "pg_locks.py", "pg_migrations.py", "sqlite_legacy.py", "db_sqlite.py",
               "backup_contracts.py", "ledger_batch_contracts.py", "pg_migration_contracts.py", "contract_errors.py")
    for name in scripts:
        shutil.copy2(source / "scripts" / name, repo / "scripts" / name)
    for name in modules:
        copy_backend_module(source, name, repo / "console" / "backend")


def test_cutover_real_cli_end_to_end(repo, pg_container, pg_dsn, monkeypatch):
    """真 PG16：状态机 + PG-03 真实迁移器/独立验证器跑完整流程。

    证明的是集成后的**真实**路径：`frozen` 写出固定源快照 → `migrated` 用
    `migrate_sqlite_to_postgres.py --mode apply` 真搬 → `verified_read_only`
    用 `verify_postgres_migration.py` 独立复核。报告落 `backup/cutover/`。
    """
    if not _pg_available():
        raise RuntimeError("docker 不可用：cutover 真实 CLI 端到端无法执行（不伪 skip）")
    repo, commit = repo
    _seed_pg03_into_repo(repo)
    # 源必须是 PG-03 迁移器认识的离线快照（1–11 + 业务表），不是 tds 的最小库。
    # `repo` 夹具已用 tds.make_db 造过同名库（含 schema_migrations 记录），
    # `run_migrations` 会因此空转、业务表全缺 —— 先删掉再合成。
    legacy = repo / "console/backend/data/console.db"
    for stale in (legacy, *legacy.parent.glob("console.db-*")):
        stale.unlink(missing_ok=True)
    make_legacy_db(legacy)
    # 数据目录被 .gitignore 排除（生产同形），源不必进 git：`frozen` 阶段从磁盘
    # 快照，迁移器读的是那份快照。夹具提交仍是有效的 target_commit。

    admin = connect(pg_dsn, autocommit=True)
    name = f"pg05cutover_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    # 真实 CLI 在子进程里跑：目标配置只能经环境传递（`pg_env` 的返回值得写进进程）
    for key, value in pg_env(pg_container, name).items():
        if key.startswith("CONSOLE_"):
            monkeypatch.setenv(key, value)
    try:
        # 目标先备好 schema12（§9 D3：只启动 PG、运行 schema12、检查空库）
        with psycopg.connect(pg_container.dsn(dbname=name), autocommit=True) as conn:
            pg_migrations.ensure_migrations_table(conn)
            pg_migrations.apply(conn)

        calls = []
        state = runtime.run_cutover(repo, target_commit=commit, actions={
            "stop_cmd": lambda root: calls.append("stop"),
            # 本用例证明的是**迁移器/验证器**的真实集成；首备份恢复门禁用替身
            # （真实门禁见 test_cutover_reentry_after_open_failure_on_nonempty_target）
            "backup_restore_cmd": backup_receipt_stub,
            "open_cmd": lambda root: calls.append("open"),
        })
        assert state["state"] == "completed"
        assert calls == ["stop", "open"]

        # 固定源快照 + 指纹都进了 evidence
        source = runtime.cutover_source_path(repo)
        evidence = state["evidence"]
        assert source.is_file()
        assert evidence["cutover_source"]["sha256"] == runtime.file_digest(source)
        assert evidence["last_sqlite_snapshot"].startswith("precut-")

        # 报告是真实 manifest（脱敏），migrated/verified 都干净
        migrate_report = json.loads((repo / "backup/cutover/migrate-apply.json").read_text())
        verify_report = json.loads((repo / "backup/cutover/verify-independent.json").read_text())
        assert migrate_report["status"] == "ok" and migrate_report["mismatches"] == []
        assert verify_report["status"] == "ok" and verify_report["mismatches"] == []
        assert migrate_report["source_sha256"] == evidence["cutover_source"]["sha256"]
        assert "password" not in json.dumps(migrate_report).lower()

        # 真搬过去了：PG 侧行数与源一致，凭据已写
        with psycopg.connect(pg_container.dsn(dbname=name), autocommit=True) as conn:
            conn.row_factory = psycopg.rows.dict_row
            for table in ("ledger_tx", "renewals"):
                src_count = sqlite3.connect(legacy).execute(
                    f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                assert conn.execute(
                    f'SELECT COUNT(*) AS c FROM "{table}"').fetchone()["c"] == src_count
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def test_cutover_full_flow_with_stubs(repo, tmp_path):
    """状态机语义：注入替身走完六态（真实 CLI 路径见上面的端到端用例）。"""
    repo, commit = repo
    calls = []

    def stop(root):
        calls.append(("stop", root))

    def migrator(root):
        calls.append(("migrator", root))
        return {"mode": "apply", "status": "complete"}

    def verify(root):
        calls.append(("verify", root))
        return "verify-ok"

    def opener(root):
        calls.append(("open", root))

    state = runtime.run_cutover(repo, target_commit=commit, **_no_pg(actions={
        "stop_cmd": stop, "migrator": migrator, "verify_cmd": verify, "open_cmd": opener,
    }))
    assert state["state"] == "completed"
    assert [c[0] for c in calls] == ["stop", "migrator", "verify", "open"]
    persisted = json.loads(_state_file(repo).read_text())
    assert persisted["state"] == "completed"
    assert persisted["evidence"]["migrator_report"] == {"mode": "apply", "status": "complete"}
    assert persisted["evidence"]["last_sqlite_snapshot"].startswith("precut-")
    assert (repo / "backup" / f"console.db.{persisted['evidence']['last_sqlite_snapshot']}").is_file()
    # 开放动作在状态持久化之后执行：writes_opened_at 已记录
    assert persisted["evidence"]["writes_opened_at"]


def test_cutover_missing_required_action_rejects_and_resume_works(repo):
    """缺必需动作即拒绝推进（§8.3：不能靠默认值猜），重入补齐后继续。

    PG-05 集成后 migrator/verify_cmd 有真实 CLI 缺省值，所以这里显式传 `None`
    模拟「现场未提供」；停写者/恢复写者仍必须由调用方给出。
    """
    repo, commit = repo

    def stop(root):
        return "stopped"

    with pytest.raises(RuntimeError, match="migrator"):
        runtime.run_cutover(repo, target_commit=commit, **_no_pg(
            actions={"stop_cmd": stop, "migrator": None}))
    persisted = json.loads(_state_file(repo).read_text())
    assert persisted["state"] == "frozen"  # frozen 已完成并落盘

    # 缺 verify_cmd 同样拒绝
    with pytest.raises(RuntimeError, match="verify_cmd"):
        runtime.run_cutover(repo, target_commit=commit, **_no_pg(actions={
            "stop_cmd": stop, "migrator": lambda root: "ok", "verify_cmd": None,
        }))
    # 重入补齐后继续推进
    state = runtime.run_cutover(repo, target_commit=commit, **_no_pg(actions={
        "stop_cmd": stop, "migrator": lambda root: "ok",
        "verify_cmd": lambda root: "ok", "open_cmd": lambda root: "ok",
    }))
    assert state["state"] == "completed"


def _write_prepared_state(path, commit, repo):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "format": "console-cutover-state-1", "state": "prepared",
        "target_commit": commit, "root": str(repo), "history": [],
    }), encoding="utf-8")


def test_cutover_rejects_target_change(repo):
    repo, commit = repo
    _write_prepared_state(_state_file(repo), commit, repo)
    # 制造一个真实存在的不同提交
    (repo / "extra.txt").write_text("later", encoding="utf-8")
    tds.git(repo, "add", "extra.txt")
    tds.git(repo, "-c", "user.email=t@test", "-c", "user.name=t", "commit", "-m", "second")
    other = tds.git(repo, "rev-parse", "HEAD").strip()
    assert other != commit
    with pytest.raises(RuntimeError, match="目标变更被拒绝"):
        runtime.run_cutover(repo, target_commit=other, **_no_pg(actions={}))
    # G02（判据 A/B）：把 target 改回旧提交、工作区却停在 other → 直接拒绝，
    # 不允许「记录 commit、执行 other 的字节」。拒绝发生在任何状态落盘之前。
    with pytest.raises(RuntimeError, match="工作区 HEAD 与 --target-commit 不一致"):
        runtime.run_cutover(repo, target_commit=commit, **_no_pg(actions={}))


def test_cutover_rejects_short_sha(repo):
    repo, commit = repo
    with pytest.raises(ValueError, match="完整 SHA"):
        runtime.run_cutover(repo, target_commit="abc123", actions={})
    assert not _state_file(repo).exists()


def test_writes_opened_persisted_before_open_side_effect(repo):
    """open 失败：落盘「正在开放」的保守边界（§8.3），且重入**重跑 open**（G04）。

    旧实现把 `state` 在动作前推进到 writes_opened，open 失败后重入从
    `index(state)+1` 起跳 → 跳过 open 直接 completed（写一直没开放却记成功）。
    现在 `state` 停在最后**有回执**的 verified_read_only，`pending=writes_opened`
    落盘先于副作用，重入按 pending 重跑开放动作。
    """
    repo, commit = repo
    calls = []

    def record(name, result=None):
        def run(root):
            calls.append(name)
            return result
        return run

    def boom(root):
        calls.append("open-failed")
        raise RuntimeError("开放动作失败")

    actions = {"stop_cmd": record("stop"), "migrator": record("migrator", "ok"),
               "verify_cmd": record("verify", "ok"), "open_cmd": boom}
    with pytest.raises(RuntimeError, match="开放动作"):
        runtime.run_cutover(repo, target_commit=commit, **_no_pg(actions=actions))

    persisted = json.loads(_state_file(repo).read_text())
    assert persisted["state"] == "backup_verified"         # 只记有回执的状态
    assert persisted["pending"]["state"] == "writes_opened"
    assert persisted["history"][-1]["state"] == "writes_opened"
    assert persisted["history"][-1]["status"] == "running"  # 没有完成回执
    assert persisted["evidence"]["open_report"]["error"] == "RuntimeError"
    assert persisted["last_error"]["state"] == "writes_opened"
    # 保守边界不变：文件一落盘就按「可能已开放」处理（§7.3/§8.3）
    assert runtime.cutover_writes_opened(repo) is True

    # 重入：open 必须真的被重跑一次；已拿回执的 stop/migrator/verify 不重跑
    calls.clear()
    actions["open_cmd"] = record("open-ok")
    state = runtime.run_cutover(repo, target_commit=commit, **_no_pg(actions=actions))
    assert state["state"] == "completed"
    assert calls == ["open-ok"], calls
    assert state["pending"] is None
    assert state["history"][-1]["status"] == "complete"
    assert _state_file(repo).read_text().find('"pending": null') != -1


@pytest.mark.parametrize("crash_at,state_name,pre_state,pre_pending,expected_first,expected_total", [
    ("stop", "frozen", "prepared", "frozen", ["stop"],
     ["stop", "stop", "migrator", "verify", "backup", "open"]),
    ("migrator", "migrated", "frozen", "migrated", ["stop", "migrator"],
     ["stop", "migrator", "migrator", "verify", "backup", "open"]),
    ("open", "writes_opened", "backup_verified", "writes_opened",
     ["stop", "migrator", "verify", "backup", "open"],
     ["stop", "migrator", "verify", "backup", "open", "open"]),
])
def test_cutover_crash_reentry_replays_side_effects(
        repo, tmp_path, crash_at, state_name, pre_state, pre_pending,
        expected_first, expected_total):
    """G04 崩溃注入：动作执行到一半进程被 kill（`os._exit`，不走 except/finally）。

    断言的是**实际副作用**（marker 文件里的真实调用序列），不是状态字符串：
    旧缺陷下 `open` 崩溃后重入会跳过开放动作却把状态标成 completed——marker
    里就不会有第二行 `open`，运行账号也永远不会被恢复。
    """
    root, commit = repo
    scripts_dir = Path(__file__).resolve().parent
    marker = tmp_path / "calls.txt"
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import os, sys\n"
        f"sys.path.insert(0, {str(BACKEND)!r})\n"
        f"sys.path.insert(0, {str(scripts_dir)!r})\n"
        "import backup_runtime as runtime\n"
        "import test_backup_runtime_cutover as t\n"
        "mode, crash_at = sys.argv[1], sys.argv[2]\n"
        "def record(name):\n"
        "    def run(root):\n"
        f"        with open({str(marker)!r}, 'a', encoding='utf-8') as handle:\n"
        "            handle.write(name + '\\n')\n"
        "        if mode == 'crash' and name == crash_at:\n"
        "            os._exit(9)\n"
        "        return 'ok'\n"
        "    return run\n"
        "def backup(root):\n"
        "    record('backup')(root)\n"
        "    return t.backup_receipt_stub(root)\n"
        f"runtime.run_cutover({str(root)!r}, target_commit={commit!r}, "
        "maintenance_lock=False, target_preflight=False, "
        "actions={'stop_cmd': record('stop'), "
        "'migrator': record('migrator'), 'verify_cmd': record('verify'), "
        "'backup_restore_cmd': backup, 'open_cmd': record('open')})\n",
        encoding="utf-8")

    first = subprocess.run([sys.executable, str(driver), "crash", crash_at],
                           cwd=str(root), capture_output=True, text=True, check=False)
    assert first.returncode == 9, first.stdout + first.stderr
    assert marker.read_text(encoding="utf-8").split() == expected_first
    crashed = json.loads(_state_file(root).read_text())
    # 已执行的动作在磁盘上是「已开始、无回执」；`state` 只记有回执的状态
    assert crashed["state"] == pre_state, crashed
    assert crashed["pending"]["state"] == pre_pending, crashed
    assert crashed["history"][-1]["status"] == "running"
    if crash_at == "open":
        # 崩溃在开放动作内部：没有回执，但 §7.3 保守规则必须视为「可能已开放」
        assert "open_report" not in crashed.get("evidence", {})
        assert runtime.cutover_writes_opened(root) is True

    second = subprocess.run([sys.executable, str(driver), "resume", crash_at],
                            cwd=str(root), capture_output=True, text=True, check=False)
    assert second.returncode == 0, second.stdout + second.stderr
    assert marker.read_text(encoding="utf-8").split() == expected_total
    state = json.loads(_state_file(root).read_text())
    assert state["state"] == "completed" and state["pending"] is None
    assert state["history"][-1]["status"] == "complete"
    # 重跑过的状态留下 attempts 记录（可审计「这一步被执行了几次」）
    attempts = [entry for entry in state["history"] if entry["state"] == state_name]
    assert attempts[-1]["attempts"] == 2, attempts


def test_cutover_writes_opened_blocks_sqlite_rollback(repo, monkeypatch):
    """§7.3 开放后边界：writes_opened 后 SQLite 组回滚被拒绝（走到预检前的闸门）。"""
    repo, commit = repo
    path = _state_file(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "format": "console-cutover-state-1", "state": "writes_opened",
        "target_commit": commit, "root": str(repo), "history": [],
    }), encoding="utf-8")
    with pytest.raises(RuntimeError, match="拒绝回退到 SQLite"):
        runtime.rollback(repo, "20260910-010101", data_only=False)
    # 状态文件缺失时不拦（开放前退路保持）
    path.unlink()
    assert runtime.cutover_writes_opened(repo) is False


def test_cutover_corrupt_state_is_conservative(repo):
    repo, commit = repo
    _write_prepared_state(_state_file(repo), commit, repo)
    path = _state_file(repo)
    path.write_text("{broken json", encoding="utf-8")
    assert runtime.cutover_writes_opened(repo) is True
    with pytest.raises(RuntimeError, match="状态记录损坏"):
        runtime.read_cutover_state(repo)


def test_cutover_dry_run_lists_remaining(repo):
    repo, commit = repo
    report = runtime.run_cutover(repo, target_commit=commit, actions={}, dry_run=True)
    assert report["dry_run"] is True
    assert report["remaining"] == ["frozen", "migrated", "verified_read_only",
                                   "backup_verified", "writes_opened", "completed"]


def test_retire_covers_pg_dump_and_sidecar(tmp_path):
    """PG 时代 deploy 备份组的 retention 归档：dump + manifest 侧车一起退役。"""
    import shutil

    backup = tmp_path / "backup"
    trash = tmp_path / ".trash"
    backup.mkdir()
    tag = "20260910-010101"
    for name in ("panels.yaml", "assets.yaml", "machines.yaml", "settings.yaml"):
        shutil.copy2(tds.SOURCE / "console" / name, backup / f"{name}.{tag}")
    (backup / f"console.dump.{tag}").write_bytes(b"pg-dump-bytes")
    (backup / f"console.dump.{tag}.manifest.json").write_text("{}", encoding="utf-8")
    (backup / f"gitsha.{tag}").write_text("a" * 40 + "\n", encoding="utf-8")
    runtime.retire_group(backup, trash, tag)
    retired = trash / f"deploy-retention-{tag}"
    assert (retired / f"console.dump.{tag}").read_bytes() == b"pg-dump-bytes"
    assert (retired / f"console.dump.{tag}.manifest.json").is_file()
    assert not (backup / f"console.dump.{tag}").exists()
    manifest = json.loads((retired / "manifest.json").read_text())
    assert manifest["status"] == "complete"


# --- B-2 回归：控制器自己持 PG 独占维护锁 ---------------------------------------


def test_cutover_holds_maintenance_lock_across_freeze(repo, pg_container, pg_dsn,
                                                      monkeypatch):
    """控制器在 `frozen`→`writes_opened` 期间**自己**持锁冻结业务写事务（§7.3）。

    PG-05 review B-2（blocker）：修复前控制器只调外部 `stop_cmd`，自己不持任何 PG 锁，
    停没停干净无从自证。这里用真 PG16 验证：`stop_cmd` 执行时（=控制器已持锁），
    来自**另一连接**的业务写事务被拒。
    """
    if not _pg_available():
        raise RuntimeError("docker 不可用：维护锁验收无法执行（不伪 skip）")

    repo, commit = repo
    _seed_pg03_into_repo(repo)
    legacy = repo / "console/backend/data/console.db"
    for stale in (legacy, *legacy.parent.glob("console.db-*")):
        stale.unlink(missing_ok=True)
    make_legacy_db(legacy)

    admin = connect(pg_dsn, autocommit=True)
    name = f"pg05lock_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    for key, value in pg_env(pg_container, name).items():
        if key.startswith("CONSOLE_"):
            monkeypatch.setenv(key, value)
    try:
        with psycopg.connect(pg_container.dsn(dbname=name), autocommit=True) as conn:
            pg_migrations.apply(conn)

        observed = {}

        def stop(root):
            """stop_cmd 执行的瞬间：控制器应已持锁，业务写事务必须被拒。"""
            import pg_locks
            import pgdb
            probe = connect(pg_container.dsn(dbname=name), autocommit=True)
            try:
                observed["lock_holder_pid"] = probe.execute(
                    "SELECT pid FROM pg_locks WHERE locktype='advisory' AND objid=%s "
                    "AND granted AND mode='ExclusiveLock'",
                    (pg_locks.KEY_MAINTENANCE,),
                ).fetchone()
                observed["lock_mode"] = probe.execute(
                    "SELECT mode FROM pg_locks WHERE locktype='advisory' AND objid=%s "
                    "AND granted",
                    (pg_locks.KEY_MAINTENANCE,),
                ).fetchone()
            finally:
                probe.close()
            # 业务写事务走另一条连接与进程内池：必须被维护锁挡住
            pgdb.close_pool()
            monkeypatch.setenv("CONSOLE_PG_LOCK_TIMEOUT", "1")
            try:
                with pgdb.pg_transaction() as conn:
                    conn.execute("SELECT 1")
                observed["writer"] = "acquired"
            except pgdb.StorageUnavailable:
                observed["writer"] = "blocked"
            finally:
                pgdb.close_pool()
            return "stopped"

        state = runtime.run_cutover(repo, target_commit=commit, actions={
            "stop_cmd": stop,
            "backup_restore_cmd": backup_receipt_stub,
            "open_cmd": lambda root: None,
        })
        assert state["state"] == "completed"
        # 控制器在 frozen 阶段确实持着独占维护锁
        assert observed["lock_mode"] == {"mode": "ExclusiveLock"}, observed
        assert observed["lock_holder_pid"] is not None, observed
        # 且该锁真的挡住了业务写事务（这就是 B-2 要证明的「冻结」）
        assert observed["writer"] == "blocked", observed
        # 锁事实进了 evidence
        assert state["evidence"]["maintenance_lock"]["held"] is True
        assert state["evidence"]["maintenance_lock_after_stop"]["held"] is True
        assert "maintenance_lock_released_at" in state["evidence"]
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def test_cutover_refuses_to_advance_without_maintenance_lock(repo):
    """取不到维护锁时拒绝推进——不允许「不带锁迁移」（§7.3）。"""
    repo, commit = repo
    # 环境无 PG 配置：默认 maintenance_lock=True 必然取不到锁
    with pytest.raises(RuntimeError, match="未能取得 PG 独占维护锁"):
        runtime.run_cutover(repo, target_commit=commit, actions={
            "stop_cmd": lambda root: "stopped",
            "open_cmd": lambda root: None,
        })
    # 状态没有推进：仍是 prepared（失败发生在任何状态转移之前）
    persisted = json.loads(_state_file(repo).read_text())
    assert persisted["state"] == "prepared"


# --- G02：执行身份绑定 + 目标就绪检查 -------------------------------------------


def test_cutover_records_execution_identity(repo):
    """G02：prepared 记录**实际执行字节**（HEAD + 逐个脚本 sha256），completed 复核。"""
    repo, commit = repo
    state = runtime.run_cutover(repo, target_commit=commit, **_no_pg(actions={
        "stop_cmd": lambda root: "ok", "migrator": lambda root: "ok",
        "verify_cmd": lambda root: "ok", "open_cmd": lambda root: "ok",
    }))
    assert state["state"] == "completed"
    execution = state["evidence"]["execution"]
    assert execution["head"] == commit and execution["head_matches_target"] is True
    assert execution["files"]["scripts/backup_runtime.py"]["sha256"] == \
        runtime.file_digest(repo / "scripts" / "backup_runtime.py")
    assert state["evidence"]["execution_final"]["files"] == execution["files"]


def test_cutover_refuses_to_open_writes_on_execution_drift(repo):
    """割接途中脚本被替换 → **拒绝开放写入**（不是告警、不是先开了再说）。"""
    repo, commit = repo
    opened = []

    def tamper(root):
        (Path(root) / "scripts" / "backup_runtime.py").write_text("# replaced\n", encoding="utf-8")
        return "ok"

    with pytest.raises(RuntimeError, match="执行身份漂移"):
        runtime.run_cutover(repo, target_commit=commit, **_no_pg(actions={
            "stop_cmd": lambda root: "ok", "migrator": lambda root: "ok",
            "verify_cmd": tamper, "open_cmd": lambda root: opened.append("open") or "ok",
        }))
    assert opened == []                                    # 副作用没发生：写入没开放
    persisted = json.loads(_state_file(repo).read_text())
    assert persisted["state"] == "backup_verified"         # 没有到 writes_opened
    assert persisted["pending"]["state"] == "writes_opened"
    assert persisted["last_error"]["state"] == "writes_opened"
    initial = persisted["evidence"]["execution"]["files"]["scripts/backup_runtime.py"]["sha256"]
    final = persisted["evidence"]["execution_before_open"]["files"]["scripts/backup_runtime.py"]["sha256"]
    assert initial != final


def test_cutover_refuses_completion_when_code_changes_after_writes_opened(repo):
    """开放之后、回执之前换代码 → 仍拒绝 completed（第二道闸）。"""
    repo, commit = repo
    opened = []

    def opener(root):
        opened.append("open")
        (Path(root) / "scripts" / "backup_runtime.py").write_text("# replaced\n", encoding="utf-8")
        return "ok"

    with pytest.raises(RuntimeError, match="执行身份漂移"):
        runtime.run_cutover(repo, target_commit=commit, **_no_pg(actions={
            "stop_cmd": lambda root: "ok", "migrator": lambda root: "ok",
            "verify_cmd": lambda root: "ok", "open_cmd": opener,
        }))
    assert opened == ["open"]                              # 开放动作确实执行过
    persisted = json.loads(_state_file(repo).read_text())
    assert persisted["state"] == "writes_opened"           # 但不给 completed 回执
    assert persisted["pending"]["state"] == "completed"
    assert persisted["last_error"]["state"] == "completed"
    assert persisted["evidence"]["execution"]["files"] != \
        persisted["evidence"]["execution_final"]["files"]


def test_preflight_expectation_follows_state_machine(repo):
    """V4-01：预检期望按**状态机进度**推导，不是全局关掉非空检查。

    首次导入前（prepared/frozen）→ 必须空库；迁移一开始（migrated 起、含崩溃未回执）
    → 允许非空并按凭据核对；开放之后 → 行数只允许变多（业务写可能已放行）。
    """
    repo, commit = repo
    snapshot = runtime.cutover_source_path(repo)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(b"snapshot-bytes")
    evidence = {"cutover_source": {"sha256": runtime.file_digest(snapshot)}}

    def expectation(state_name, pending=None):
        return runtime.cutover_preflight_expectation(repo, {
            "state": state_name, "pending": pending, "target_commit": commit,
            "evidence": evidence})

    assert expectation("prepared")["mode"] == "empty"
    assert expectation("frozen", {"state": "frozen"})["mode"] == "empty"
    assert expectation("frozen")["mode"] == "migrated"          # 下一步就是迁移
    assert expectation("frozen", {"state": "migrated"})["mode"] == "migrated"
    migrated = expectation("verified_read_only")
    assert migrated["mode"] == "migrated" and migrated["allow_new_writes"] is False
    assert migrated["source_sha256"] == evidence["cutover_source"]["sha256"]
    assert migrated["source_path"] == str(snapshot)
    opened = expectation("verified_read_only", {"state": "writes_opened"})
    assert opened["mode"] == "migrated" and opened["allow_new_writes"] is True
    assert expectation("completed")["mode"] == "completed"


def test_restore_receipt_validation_rejects_stale_or_mismatched(repo):
    """V4-03：回执必须绑定本次源/本候选/当前目标；不相关旧报告一律拒绝。"""
    repo, commit = repo
    snapshot = runtime.cutover_source_path(repo)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(b"snapshot-bytes")
    dump = repo / "backup/cutover/console.dump.x"
    dump.write_bytes(b"dump")
    state = {"target_commit": commit, "evidence": {
        "cutover_source": {"sha256": runtime.file_digest(snapshot)},
        "target_preflight": {"server": {"database": "console"}},
    }}
    pay = _pay_recovery_stub(repo, dump)
    good = {
        "format": runtime.CUTOVER_RESTORE_RECEIPT_FORMAT, "status": "ok",
        "source": {"sha256": runtime.file_digest(snapshot)},
        "execution": {"digest": runtime.execution_digest(
            runtime.cutover_execution_identity(repo, commit))},
        "gate": {"script": "scripts/cutover_backup_restore.py",
                 "sha256": runtime.file_digest(
                     Path(runtime.__file__).resolve().parent / "cutover_backup_restore.py")},
        "target": {"database": "console"},
        "dump": {"path": str(dump), "sha256": runtime.file_digest(dump), "name": dump.name},
        "comparison": {"mismatches": [], "tables": ["ledger_tx"]},
        "restored": {"restored": True, "row_digest_match": True,
                     "schema_version": pg_migrations.latest_schema_version(), "pay": pay},
        "file_mapping": {"ledger_file_batches": {"restored": 0, "expected_empty": True}, "pay": pay},
        "cleanup": {"restore_database": "cu_restore_x", "dropped": True},
    }
    assert runtime.validate_restore_receipt(good, root=repo, state=state)["dump"] == dump.name

    for mutate, message in (
        (lambda data: data.pop("format"), "格式不符"),
        (lambda data: data.update(status="failed"), "状态为"),
        (lambda data: data.update(source={"sha256": "0" * 64}), "与本次割接源"),
        (lambda data: data.update(execution={"digest": "0" * 64}), "不是本候选"),
        (lambda data: data.update(target={"database": "other"}), "与当前目标"),
        (lambda data: data.update(target={}), "没有记录目标库身份"),
        (lambda data: data["comparison"].update(mismatches=[{"table": "ledger_tx"}]), "比较结果非空"),
        # 复核 A/B 的拒绝路径：缺整段、表清单为空、假「恢复成功」、批次表非空、恢复库没删
        (lambda data: data.pop("comparison"), "缺少比较结果段"),
        (lambda data: data.update(comparison={"mismatches": []}), "没有记录比较过的表清单"),
        (lambda data: data.update(restored={"restored": True,
                                            "schema_version": pg_migrations.latest_schema_version()}),
         "逐表摘要一致"),
        (lambda data: data["restored"].update(schema_version=11), "schema 版本"),
        (lambda data: data.update(file_mapping={"ledger_file_batches": {"restored": 3}}),
         "原件映射核对未通过"),
        (lambda data: data["file_mapping"].pop("pay"), "Pay 文件独立恢复"),
        (lambda data: data["file_mapping"]["pay"].update(verified=False), "Pay 文件独立恢复"),
        (lambda data: data.update(cleanup={"dropped": False}), "未被清理"),
        (lambda data: data.update(gate={"sha256": "0" * 64}), "已审查的门禁脚本"),
        (lambda data: data.pop("gate"), "已审查的门禁脚本"),
    ):
        tampered = json.loads(json.dumps(good))
        mutate(tampered)
        with pytest.raises(RuntimeError, match=message):
            runtime.validate_restore_receipt(tampered, root=repo, state=state)
    restored_original = Path(pay["path"]) / "raw/original.csv"
    restored_original.write_bytes(b"damaged restored file")
    with pytest.raises(ValueError, match="摘要"):
        runtime.validate_restore_receipt(good, root=repo, state=state)
    restored_original.write_bytes(b"synthetic,original\n")
    # dump 被删/被换 → 拒绝（回执不是免检牌）
    dump.write_bytes(b"dump-v2")
    with pytest.raises(RuntimeError, match="dump 文件缺失或摘要变化"):
        runtime.validate_restore_receipt(good, root=repo, state=state)
    # 源快照被换 → 拒绝
    dump.write_bytes(b"dump")
    snapshot.write_bytes(b"snapshot-v2")
    with pytest.raises(RuntimeError, match="源快照已不存在或摘要变化"):
        runtime.validate_restore_receipt(good, root=repo, state=state)


def test_migrated_reentry_checks_credential_and_real_row_counts(pg_container, pg_dsn,
                                                                monkeypatch, tmp_path):
    """V4-01 真 PG16：非空目标只在**能证明是本次迁入**时才放行。

    负例：目标非空但没有完成凭据 → 拒绝（未知数据）；
    正例：凭据来源/目标库/逐表行数都对得上 → 放行并留下 reentry 证据。
    """
    if not _pg_available():
        raise RuntimeError("docker 不可用：重入预检验收无法执行（不伪 skip）")
    from psycopg.rows import dict_row

    import pg_migration_lib

    admin = connect(pg_dsn, autocommit=True)
    name = f"pg05reentry_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    root = tmp_path / "repo"
    snapshot = runtime.cutover_source_path(root)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(b"frozen-snapshot")
    sha = runtime.file_digest(snapshot)
    try:
        _pg_env_for(monkeypatch, pg_container, name)
        # 空库 + schema12 + 一行业务数据（模拟「迁到一半/迁完之后」的非空目标）
        runtime.cutover_target_preflight()
        with psycopg.connect(pg_container.dsn(dbname=name), autocommit=True) as conn:
            conn.execute("INSERT INTO renewals (id, name) VALUES ('r1', 'x')")
        state = {"state": "verified_read_only", "pending": None, "target_commit": "a" * 40,
                 "evidence": {"cutover_source": {"sha256": sha}}}
        expectation = runtime.cutover_preflight_expectation(root, state)
        with pytest.raises(RuntimeError, match="没有迁移完成凭据"):
            runtime.cutover_target_preflight(expectation=expectation)

        # 正例：写入与本次源绑定、行数对得上的完成凭据
        with psycopg.connect(pg_container.dsn(dbname=name), autocommit=True,
                             row_factory=dict_row) as conn:
            pg_migration_lib.write_credential(conn, {
                "format": pg_migration_lib.CREDENTIAL_FORMAT,
                "source_sha256": sha,
                "target_identity": {"database": name},
                "counts": {"renewals": 1, "ledger_tx": 0},
                "completed_at": "2026-09-11T00:00:00+00:00",
            })
        evidence = runtime.cutover_target_preflight(expectation=expectation)
        assert evidence["reentry"]["source_sha256"] == sha
        assert evidence["reentry"]["row_counts"]["renewals"] == {"credential": 1, "actual": 1}

        # 负例：凭据来源是**另一份源** → 拒绝（不能拿无关旧凭据顶替）
        with psycopg.connect(pg_container.dsn(dbname=name), autocommit=True,
                             row_factory=dict_row) as conn:
            pg_migration_lib.write_credential(conn, {
                "format": pg_migration_lib.CREDENTIAL_FORMAT,
                "source_sha256": "b" * 64,
                "target_identity": {"database": name},
                "counts": {"renewals": 1},
                "completed_at": "2026-09-11T00:00:00+00:00",
            })
        with pytest.raises(RuntimeError, match="完成凭据的来源"):
            runtime.cutover_target_preflight(expectation=expectation)
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def test_gate_isolation_uses_server_identity_not_config_strings(monkeypatch):
    """V4-03 隔离判定：同一实例换个写法（localhost / hostaddr / URI）也必须被拒。

    复核 A/B：按配置字符串比较时 `localhost`、`hostaddr=`、`postgresql://…` 都能
    绕过去，恢复会跑在生产集群上。现在按服务端自述身份判（system_identifier，
    读不到时退回 addr/port/database）。
    """
    if not _pg_available():
        raise RuntimeError("docker 不可用：隔离判定验收无法执行（不伪 skip）")
    import cutover_backup_restore as gate

    prod = support.start_pg16()
    other = support.start_pg16()
    monkeypatch.setenv("CONSOLE_PG_HOST", prod.host)
    monkeypatch.setenv("CONSOLE_PG_PORT", str(prod.port))
    monkeypatch.setenv("CONSOLE_PG_DB", prod.dbname)
    monkeypatch.setenv("CONSOLE_PG_USER", prod.user)
    monkeypatch.setenv("CONSOLE_PG_PASSWORD", prod.password)
    try:
        for alias in (f"host=localhost port={prod.port} dbname={prod.dbname} "
                      f"user={prod.user} password={prod.password}",
                      f"hostaddr=127.0.0.1 port={prod.port} dbname={prod.dbname} "
                      f"user={prod.user} password={prod.password}",
                      f"postgresql://{prod.user}:{prod.password}@127.0.0.1:"
                      f"{prod.port}/{prod.dbname}"):
            with pytest.raises(gate.UsageError, match="指向生产实例"):
                gate.assert_isolated_restore_instance(alias)
        # 真正的另一个集群（本机另一个容器）→ 放行
        evidence = gate.assert_isolated_restore_instance(other.dsn())
        assert evidence["restore"]["system_identifier"] != \
            evidence["production"]["system_identifier"]
        # 未配置 → 拒绝
        with pytest.raises(gate.UsageError, match="未配置"):
            gate.assert_isolated_restore_instance("")
    finally:
        prod.stop()
        other.stop()


def test_gate_cleans_restore_database_when_role_bootstrap_fails(monkeypatch):
    """复核 A：建库成功但 §6.3 角色 bootstrap 失败时，恢复库必须被清理掉。"""
    if not _pg_available():
        raise RuntimeError("docker 不可用：恢复库清理验收无法执行（不伪 skip）")
    import psycopg

    import cutover_backup_restore as gate

    target = support.start_pg16()
    dbname = "cu_restore_cleanup"
    try:
        def boom(conn, **kwargs):
            raise RuntimeError("模拟角色矩阵建立失败")

        monkeypatch.setattr(pg_migrations, "bootstrap_roles", boom)
        with pytest.raises(gate.GateError, match="角色矩阵"):
            gate._create_restore_database(target.dsn(), dbname)
        with psycopg.connect(target.dsn(), autocommit=True) as conn:
            left = conn.execute(
                "SELECT count(*) FROM pg_database WHERE datname = %s",
                (dbname,)).fetchone()[0]
        assert left == 0
    finally:
        target.stop()


def test_migrated_reentry_refuses_when_source_snapshot_changed(repo, pg_container, pg_dsn,
                                                               monkeypatch, tmp_path):
    """V4-01：源快照被换/丢失 → 拒绝（说不出迁入的是哪份源，不能按「已迁入」继续）。"""
    if not _pg_available():
        raise RuntimeError("docker 不可用：重入预检验收无法执行（不伪 skip）")
    admin = connect(pg_dsn, autocommit=True)
    name = f"pg05resrc_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    root = tmp_path / "repo"
    snapshot = runtime.cutover_source_path(root)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(b"frozen-snapshot")
    try:
        _pg_env_for(monkeypatch, pg_container, name)
        runtime.cutover_target_preflight()
        with psycopg.connect(pg_container.dsn(dbname=name), autocommit=True) as conn:
            conn.execute("INSERT INTO renewals (id, name) VALUES ('r1', 'x')")
        state = {"state": "migrated", "pending": None, "target_commit": "a" * 40,
                 "evidence": {"cutover_source": {"sha256": runtime.file_digest(snapshot)}}}
        snapshot.write_bytes(b"replaced")
        with pytest.raises(RuntimeError, match="与首次记录"):
            runtime.cutover_target_preflight(
                expectation=runtime.cutover_preflight_expectation(root, state))
        snapshot.unlink()
        with pytest.raises(RuntimeError, match="源快照不存在"):
            runtime.cutover_target_preflight(
                expectation=runtime.cutover_preflight_expectation(root, state))
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def test_cutover_cli_rejects_head_target_mismatch(repo):
    """G02 判据 A/B：`--target-commit` 与工作区 HEAD 不一致时必须拒绝（真 CLI 入口）。"""
    root, commit = repo
    (root / "later.txt").write_text("v2\n", encoding="utf-8")
    tds.git(root, "add", "later.txt")
    tds.git(root, "-c", "user.email=t@test", "-c", "user.name=t", "commit", "-m", "v2")
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent / "backup_runtime.py"),
         "cutover", str(root), "--target-commit", commit, "--yes",
         # 本用例只测身份校验：准备阶段显式跳过（真实准备阶段见 test_cutover_prepare.py）
         "--skip-prepare"],
        cwd=str(root), capture_output=True, text=True, check=False)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "工作区 HEAD 与 --target-commit 不一致" in proc.stderr
    assert not _state_file(root).exists()   # 拒绝发生在任何状态落盘之前


def _pg_env_for(monkeypatch, container, name):
    for key, value in pg_env(container, name).items():
        if key.startswith("CONSOLE_"):
            monkeypatch.setenv(key, value)


def test_target_preflight_bootstraps_empty_database(pg_container, pg_dsn, monkeypatch):
    """G02 关闭标准：**未初始化**的空库 → 就地 bootstrap 角色 + schema12，记录空库证据。"""
    if not _pg_available():
        raise RuntimeError("docker 不可用：目标就绪检查无法执行（不伪 skip）")
    admin = connect(pg_dsn, autocommit=True)
    name = f"pg05fresh_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    try:
        _pg_env_for(monkeypatch, pg_container, name)
        evidence = runtime.cutover_target_preflight()
        assert evidence["initialized"] is False
        assert evidence["bootstrapped"] is True
        assert evidence["schema_version"] == pg_migrations.latest_schema_version()
        # 角色是**集群级**对象：本用例所在的容器可能已被别的用例建过同名角色，
        # 因此只断言「两集合互补且都在声明清单内」，不断言 present 为空。
        declared = {pg_migrations.OWNER_ROLE, pg_migrations.ADMIN_ROLE, *pg_migrations.APP_ROLES}
        assert set(evidence["roles_present"]) | set(evidence["roles_missing"]) == declared
        assert not (set(evidence["roles_present"]) & set(evidence["roles_missing"]))
        assert set(evidence["row_counts"].values()) == {0}
        # 未声明 CONSOLE_PG_CONTAINER → 镜像身份明说不可得，不猜
        assert evidence["image"]["available"] is False
        # 幂等：再跑一次不会重复 bootstrap
        again = runtime.cutover_target_preflight()
        assert again["initialized"] is True and "bootstrapped" not in again
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def test_target_preflight_refuses_unprepared_and_nonempty(pg_container, pg_dsn, monkeypatch):
    """`prepare=False` 时未初始化目标必须拒绝；已初始化但业务表非空同样拒绝。"""
    if not _pg_available():
        raise RuntimeError("docker 不可用：目标就绪检查无法执行（不伪 skip）")
    admin = connect(pg_dsn, autocommit=True)
    name = f"pg05guard_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    try:
        _pg_env_for(monkeypatch, pg_container, name)
        with pytest.raises(RuntimeError, match="未初始化"):
            runtime.cutover_target_preflight(prepare=False)
        # 初始化（bootstrap + schema12），再插一行业务数据
        runtime.cutover_target_preflight()
        with psycopg.connect(pg_container.dsn(dbname=name), autocommit=True) as conn:
            conn.execute("INSERT INTO renewals (id, name) VALUES ('preflight-probe', 'x')")
        with pytest.raises(RuntimeError, match="非空"):
            runtime.cutover_target_preflight()
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


# --- V4-01 / V4-03：真 PG16 全链（默认生产参数 + 默认动作 + 实际副作用）------------


def _chain_fixture(tmp_path, monkeypatch, *, restore_container=True):
    """真 PG16 双容器夹具：生产目标 + 可选隔离恢复目标 + 真迁移器/验证器/门禁。"""
    prod = support.start_pg16()
    restore = support.start_pg16() if restore_container else None
    admin = connect(prod.dsn(), autocommit=True)
    name = f"pg05chain_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    repo = tds.make_repo(tmp_path)
    _seed_pg03_into_repo(repo)
    legacy = repo / "console/backend/data/console.db"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    for stale in (legacy, *legacy.parent.glob("console.db-*")):
        stale.unlink(missing_ok=True)
    make_legacy_db(legacy)
    commit = tds.git(repo, "rev-parse", "HEAD").strip()
    for key, value in pg_env(prod, name).items():
        if key.startswith("CONSOLE_"):
            monkeypatch.setenv(key, value)
    monkeypatch.setenv("CONSOLE_PG_BACKUP_CONTAINER", prod.name)
    monkeypatch.setenv("CONSOLE_PG_BACKUP_USER", prod.user)
    monkeypatch.setenv("CONSOLE_PG_BACKUP_DB", name)
    if prod.name == "native":
        _native_backup_transport(tmp_path, monkeypatch, prod)
    original = repo / "console/backend/data/pay/raw/bill.csv"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"original,pre-cutover\n")
    if restore is not None:
        monkeypatch.setenv("CONSOLE_CUTOVER_RESTORE_DSN", restore.dsn())
        monkeypatch.setenv("CONSOLE_CUTOVER_RESTORE_CONTAINER", "" if restore.name == "native" else restore.name)
    return repo, commit, prod, restore, admin, name


def _native_backup_transport(tmp_path, monkeypatch, source):
    """Only Docker exec transport is replaced; SQL, dump and restore are real PG16."""
    binary = tmp_path / "pg-transport-bin"
    binary.mkdir()
    wrapper = binary / "docker"
    wrapper.write_text(f"#!{sys.executable}\n" + "import os,sys\n"
                       "args=list(sys.argv[1:])\n"
                       "assert args.pop(0) == 'exec'\n"
                       "if args[0] == '-i': args.pop(0)\n"
                       "assert args.pop(0) == 'native'\n"
                       "tool=args.pop(0)\n"
                       "assert tool in ('psql','pg_dump','pg_restore')\n"
                       f"path={os.environ['CONSOLE_PG_TEST_NATIVE_BIN']!r}+'/'+tool\n"
                       f"endpoint=['-h',{source.host!r},'-p',{str(source.port)!r}]\n"
                       "options=[] if tool == 'pg_restore' or '--version' in args else endpoint\n"
                       "os.execv(path,[path,*options,*args])\n")
    wrapper.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ.get("PATH", ""))


def test_cutover_reentry_after_open_failure_on_nonempty_target(tmp_path, monkeypatch):
    """V4-01 关闭标准（真 PG16，**默认** maintenance_lock/target_preflight）：

    真迁移器把数据搬进目标（目标因此非空）→ 注入 open 失败 → 第二个进程重入。
    修复前：重入先跑空库预检 → 「业务表非空」拒绝，open 永远不再执行。
    修复后：按来源指纹 + 完成凭据 + 实际行数核对放行，未完成动作被重跑。
    """
    if not _pg_available():
        raise RuntimeError("docker 不可用：V4-01 关闭验收无法执行（不伪 skip）")
    repo, commit, prod, restore, admin, name = _chain_fixture(tmp_path, monkeypatch)
    calls = []
    try:
        def open_fails(root):
            calls.append("open-first")
            raise RuntimeError("注入：开放动作失败")

        with pytest.raises(RuntimeError, match="开放动作"):
            runtime.run_cutover(repo, target_commit=commit, actions={
                "stop_cmd": lambda root: calls.append("stop") or "stopped",
                "open_cmd": open_fails})

        persisted = json.loads(_state_file(repo).read_text())
        assert persisted["state"] == "backup_verified"
        assert persisted["pending"]["state"] == "writes_opened"
        # 迁移真的发生了：目标非空（这就是修复前会把重入挡死的条件）
        with psycopg.connect(prod.dsn(dbname=name), autocommit=True) as conn:
            rows = conn.execute("SELECT count(*) FROM ledger_tx").fetchone()[0]
        assert rows > 0
        # V4-03 门禁产物：首份 PG 备份 + 恢复验证回执都在（同一轮里真跑过）
        dumps = sorted(p.name for p in (repo / "backup/cutover").glob("console.dump.*"))
        assert dumps, "首份 PG 备份产物缺失"
        receipt = json.loads((repo / runtime.CUTOVER_RESTORE_REPORT_RELATIVE).read_text())
        assert receipt["status"] == "ok" and receipt["comparison"]["mismatches"] == []
        assert receipt["restored"]["restored"] is True
        assert receipt["file_mapping"]["pay"]["verified"]
        assert receipt["file_mapping"]["pay"]["files"] == 1
        assert (Path(receipt["file_mapping"]["pay"]["path"]) / "raw/bill.csv").read_bytes() == b"original,pre-cutover\n"
        assert receipt["source"]["sha256"] == persisted["evidence"]["cutover_source"]["sha256"]

        # 重入：默认参数（不关预检、不关维护锁），未完成的 open 必须被重跑
        state = runtime.run_cutover(repo, target_commit=commit, actions={
            "stop_cmd": lambda root: calls.append("stop-again") or "stopped",
            "open_cmd": lambda root: calls.append("open-resume") or "opened"})
        assert state["state"] == "completed" and state["pending"] is None
        assert calls.count("open-resume") == 1
        assert calls.count("stop-again") == 0            # 已回执的状态不重跑
        # 重入预检走的是「迁移后」期望，并留下凭据核对证据
        expectation = state["evidence"]["target_preflight"]["expectation"]
        assert expectation["mode"] == "migrated" and expectation["allow_new_writes"] is True
        assert state["evidence"]["target_preflight"]["reentry"]["target_database"] == name
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()
        prod.stop()
        if restore is not None:
            restore.stop()


def test_backup_gate_blocks_open_when_restore_target_dead(tmp_path, monkeypatch):
    """V4-03 关闭标准：恢复失败时 **open 从未执行**（真 CLI + 真 dump + 死恢复目标）。"""
    if not _pg_available():
        raise RuntimeError("docker 不可用：V4-03 关闭验收无法执行（不伪 skip）")
    repo, commit, prod, _restore, admin, name = _chain_fixture(
        tmp_path, monkeypatch, restore_container=False)
    monkeypatch.setenv("CONSOLE_CUTOVER_RESTORE_DSN",
                       "host=127.0.0.1 port=1 dbname=dead_restore user=nobody password=x")
    opened = []
    try:
        with pytest.raises(RuntimeError, match="注入动作失败"):
            runtime.run_cutover(repo, target_commit=commit, actions={
                "stop_cmd": lambda root: "stopped",
                "open_cmd": lambda root: opened.append("open") or "opened"})
        assert opened == []                              # 开放动作从未执行
        persisted = json.loads(_state_file(repo).read_text())
        assert persisted["state"] == "verified_read_only"
        assert persisted["pending"]["state"] == "backup_verified"
        # 回执只有「无效占位」（门禁入口先失效旧回执）；没有任何 ok 回执可用
        report = repo / runtime.CUTOVER_RESTORE_REPORT_RELATIVE
        assert not report.exists() or json.loads(report.read_text())["status"] != "ok"
        assert runtime.cutover_writes_opened(repo) is False   # 保守边界：写入没开放

        # 换回真隔离恢复目标 → 重入继续推进到开放
        restore = support.start_pg16()
        try:
            monkeypatch.setenv("CONSOLE_CUTOVER_RESTORE_DSN", restore.dsn())
            monkeypatch.setenv("CONSOLE_CUTOVER_RESTORE_CONTAINER", "" if restore.name == "native" else restore.name)
            state = runtime.run_cutover(repo, target_commit=commit, actions={
                "stop_cmd": lambda root: "stopped",
                "open_cmd": lambda root: opened.append("open") or "opened"})
            assert state["state"] == "completed"
            assert opened == ["open"]
            assert state["evidence"]["backup_verified"]["restored"]["restored"] is True
        finally:
            restore.stop()
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()
        prod.stop()
