"""Exercise the real deployment entrypoint with disposable Git/WAL and fake Docker.

No test invokes the real Docker/SSH binaries or uses a live database.
"""
import ast
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1]
REAL_GIT = shutil.which("git")
TAG = "20260907-231123"
MEMBERS = ("console.db", "panels.yaml", "assets.yaml", "machines.yaml", "settings.yaml", "gitsha")
# 夹具里 tracked `console/panels.yaml` 的内容，刻意非空。2026-09-19 线上面板清空事故后，
# 「tracked 为空 + 未配 CONSOLE_PANELS_HOST_PATH」是 deployment_environment 明确拒绝的
# 部署形态（compose 会静默回退挂空文件）；这些测试的主题是部署/回滚安全，不是面板接线，
# 所以夹具落在仍然受支持的兼容模式一侧（docs/configuration.md：未配置该变量时仍用仓库文件）。
# 门禁本身由 test_backup_runtime.py 的专属用例覆盖。

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

FIXTURE_PANELS = "- id: fixture-panel\n"
# Fixtures copy the current db.py, so their migration records must follow it.
# Parse the literal without importing paths.py or opening any application data.
def _current_schema_versions():
    for relative in (
        "kernel/backend/db_sqlite.py",
        "console/backend/db_sqlite.py",
        "console/backend/db.py",
    ):
        path = SOURCE / relative
        if not path.is_file():
            continue
        text = path.read_text()
        if "Thin relocating shim" in text[:240]:
            continue
        for node in ast.parse(text).body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "MIGRATIONS" for target in node.targets
            ):
                return tuple(migration[0] for migration in ast.literal_eval(node.value))
    raise RuntimeError("无法从 db_sqlite/db 静态读取 MIGRATIONS")


CURRENT_SCHEMA_VERSIONS = _current_schema_versions()


def git(repo, *args):
    return subprocess.run(
        [REAL_GIT, "-C", str(repo), *args], check=True, text=True, capture_output=True
    ).stdout.strip()


def make_db(path, value="current", schema=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE samples (value TEXT)")
    conn.execute("INSERT INTO samples VALUES (?)", (value,))
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    versions = CURRENT_SCHEMA_VERSIONS if schema is None else range(1, schema + 1)
    conn.executemany("INSERT INTO schema_migrations VALUES (?)", ((v,) for v in versions))
    conn.commit()
    return conn


def db_values(path):
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        return [row[0] for row in conn.execute("SELECT value FROM samples")]
    finally:
        conn.close()


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    backend = repo / "console/backend"
    backend.mkdir(parents=True)
    # V4-02/V4-03：控制器现在把准备阶段与首备份恢复门禁也当作自己的一部分
    # （`controller_sources()` 必带），最小夹具仓库必须同样具备，否则
    # `preserve_controller` 会在「备份/恢复句柄不全」上直接失败。
    for name in ("deploy.sh", "rollback.sh", "backup_db.py", "backup_runtime.py", "backup_pay.py",
                 "backup_attachments.py", "host_paths.py",
                 "cutover_prepare.py", "cutover_backup_restore.py", "cutover_writers.py",
                 "pg_migration_lib.py", "deploy_pg_schema.py"):
        source = SOURCE / "scripts" / name
        if source.exists():
            shutil.copy2(source, repo / "scripts" / name)
    for name in ("db.py", "db_sqlite.py", "paths.py", "composition.py", "composition_identity.py",
                 "backup_identity.py", "component_migrations.py", "kernel_baseline.py",
                 "pg_backup.py", "pg_migrations.py", "pgdb.py", "pg_locks.py",
                 "runtime_mode.py", "pg_contracts.py", "backup_contracts.py", "ledger_batch_contracts.py", "pg_migration_contracts.py", "contract_errors.py",
                 "plugin_imports.py", "money.py",
                 "sqlite_legacy.py"):
        # B7-2：backup_db 顶层 import backup_attachments，后者无附件组合分支会
        # `import plugin_imports`（组合解析插件根），最小仓库必须一并携带。
        # B7-1：paths 顶层依赖 composition，固定控制器与最小仓库必须一并携带。
        # PG-04：backup_db 的 PG 路径惰性 import runtime_mode/pg_contracts，
        # 最小仓库必须带上。G05：pg_migrations 是 backup_db 的顶层 import（表清单与
        # schema 基线），少了连 import 都过不去——见 controller_sources 的同名必带项；
        # 其余 PG 模块仅在真 PG 路径触发，按存在性复制。
        copy_backend_module(SOURCE, name, backend)
    for name, content in {
        "panels.yaml": FIXTURE_PANELS, "assets.yaml": "[]\n",
        "machines.yaml": "[]\n", "settings.yaml": "{}\n",
    }.items():
        (repo / "console" / name).write_text(content)
    (repo / ".gitignore").write_text("backup\n.trash/\nconsole/backend/data\n__pycache__/\n.env\n")
    (repo / ".env").write_text("CONSOLE_STORAGE_ENGINE=sqlite\n")
    (repo / "docker-compose.yml").write_text("services:\n  console:\n    image: test-only\n")
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fixture")
    return repo


def fake_environment(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.jsonl"
    fake = f"#!{sys.executable}\n" + '''
import json
import os
import pathlib
import sys

kind = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["COMMAND_LOG"], "a") as handle:
    handle.write(json.dumps({"kind": kind, "args": args, "commit": os.getenv("GIT_COMMIT"), "engine": os.getenv("CONSOLE_STORAGE_ENGINE")}) + "\\n")
if kind == "git":
    if args and args[0] == os.getenv("FAIL_GIT"):
        sys.exit(23)
    if args and args[0] in ("pull", "push"):
        sys.exit(0)
    if args and args[0] == "checkout" and (os.getenv("CHECKOUT_SIDECARS") or os.getenv("RUN_TIMED_AFTER_CHECKOUT")):
        import subprocess
        result = subprocess.run([os.environ["REAL_GIT"], *args])
        if result.returncode == 0 and os.getenv("CHECKOUT_SIDECARS"):
            for suffix in ("-wal", "-shm"):
                pathlib.Path("console/backend/data/console.db" + suffix).write_bytes(("old" + suffix).encode())
        if result.returncode == 0 and os.getenv("RUN_TIMED_AFTER_CHECKOUT"):
            command = json.loads(os.environ.get("TIMED_COMMAND", "null")) or [sys.executable, "scripts/backup_db.py"]
            timed = subprocess.run(command, text=True, capture_output=True)
            with open(os.environ["COMMAND_LOG"], "a") as handle:
                handle.write(json.dumps({"kind": "timed-after-checkout", "args": command, "returncode": timed.returncode}) + "\\n")
        sys.exit(result.returncode)
    os.execv(os.environ["REAL_GIT"], [os.environ["REAL_GIT"], *args])
if kind == "docker":
    state_path = pathlib.Path(os.environ["COMMAND_LOG"] + ".state")
    state = json.loads(state_path.read_text()) if state_path.exists() else {"running": True, "commit": "before"}
    if args[:2] == ["compose", "stop"]:
        if os.getenv("FAIL_DOCKER") == "stop":
            sys.exit(24)
        if os.getenv("STOP_DB_VALUE"):
            import sqlite3
            conn = sqlite3.connect("console/backend/data/console.db")
            conn.execute("INSERT INTO samples VALUES (?)", (os.environ["STOP_DB_VALUE"],))
            conn.commit()
            conn.close()
        state["running"] = bool(os.getenv("KEEP_RUNNING"))
        state_path.write_text(json.dumps(state))
    elif args[:2] == ["compose", "ps"]:
        print("fixture-console")
    elif args[:2] == ["compose", "up"]:
        if os.getenv("FAIL_DOCKER") == "up":
            sys.exit(25)
        state.update(running=True, commit=os.getenv("WRONG_RUNNING_COMMIT", os.getenv("GIT_COMMIT")))
        state_path.write_text(json.dumps(state))
        # A healthy real app has opened its restored WAL DB. Keep its initialized
        # sidecars, as an app-owned connection would, instead of a bare file fixture.
        import sqlite3
        conn = sqlite3.connect("console/backend/data/console.db")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()
        if os.getenv("WRONG_RUNNING_SCHEMA"):
            conn.execute("INSERT INTO schema_migrations VALUES (999)")
            conn.commit()
        os._exit(0)
    elif args[:1] == ["inspect"]:
        if os.getenv("FAIL_DOCKER") == "inspect":
            sys.exit(26)
        if any(".Config.Env" in arg for arg in args):
            print(json.dumps(["GIT_COMMIT=" + (state["commit"] or ""), "CONSOLE_STORAGE_ENGINE=" + os.getenv("CONSOLE_STORAGE_ENGINE", "sqlite")]))
        else:
            print(json.dumps({"Running": state["running"], "Restarting": False, "Health": {"Status": "unhealthy" if os.getenv("FAIL_DOCKER") == "health" else "healthy"}}))
    sys.exit(0)
if kind == "crontab":
    if os.getenv("FAIL_CRONTAB"):
        print("cannot read crontab", file=sys.stderr)
        sys.exit(2)
    if os.getenv("CRONTAB_TEXT"):
        print(os.environ["CRONTAB_TEXT"])
        sys.exit(0)
    print("no crontab for fixture-user", file=sys.stderr)
    sys.exit(1)
if kind == "ps":
    if os.getenv("RUNNING_LEGACY_BACKUP"):
        print("12345 python3 scripts/backup_db.py")
    sys.exit(0)
# An accidental SSH call is blocked, never delegated to the host.
sys.exit(99)
'''
    for name in ("git", "docker", "ssh", "crontab", "ps"):
        target = bin_dir / name
        target.write_text(fake)
        target.chmod(0o755)
    return {
        **{key: os.environ[key] for key in ("TMPDIR", "LANG", "LC_ALL") if key in os.environ},
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "COMMAND_LOG": str(log), "REAL_GIT": REAL_GIT,
    }


def commands(env):
    path = Path(env["COMMAND_LOG"])
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def run_script(repo, env, name, *args):
    return subprocess.run(
        ["bash", str(repo / "scripts" / name), *args], cwd=repo, env=env,
        text=True, errors="replace", capture_output=True, timeout=25,
    )


@pytest.fixture
def deployment(tmp_path):
    repo = make_repo(tmp_path)
    conn = make_db(repo / "console/backend/data/console.db")
    try:
        yield repo, conn, fake_environment(tmp_path)
    finally:
        conn.close()


def test_backup_api_failure_aborts_before_pull_or_compose(deployment):
    repo, conn, env = deployment
    conn.execute("INSERT INTO samples VALUES ('committed-in-wal')")
    conn.commit()
    assert (repo / "console/backend/data/console.db-wal").stat().st_size > 0
    with (repo / "console/backend/db.py").open("a") as handle:
        handle.write("\ndef backup_database(dest_path, db_path=None):\n    raise OSError('injected snapshot API failure')\n")
    git(repo, "add", "console/backend/db.py")
    git(repo, "commit", "-m", "inject backup API error")

    result = run_script(repo, env, "deploy.sh", "--yes")

    assert result.returncode != 0, result.stdout + result.stderr
    assert not [c for c in commands(env) if c["kind"] == "docker" or c["args"][:1] == ["pull"]]
    assert not list((repo / "backup").glob("gitsha.*"))
    assert not list((repo / "backup").glob("console.db.*"))
    assert not (repo / "backup/last_deploy.json").exists()


def test_deploy_loads_explicit_pg_engine_before_snapshot(deployment):
    repo, _conn, env = deployment
    (repo / ".env").write_text("CONSOLE_STORAGE_ENGINE=postgres\n")
    with (repo / "scripts/backup_db.py").open("a") as handle:
        handle.write("\ndef write_pg_dump(*args, **kwargs):\n"
                     "    raise RuntimeError('injected PG backup unavailable')\n")
    result = run_script(repo, env, "deploy.sh", "--yes")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "injected PG backup unavailable" in result.stderr
    assert not list((repo / "backup").glob("console.db.*"))
    assert not [c for c in commands(env) if c["kind"] == "docker" or c["args"][:1] == ["pull"]]


def test_deploy_refuses_engine_conflict_before_any_command(deployment):
    repo, _conn, env = deployment
    (repo / ".env").write_text("CONSOLE_STORAGE_ENGINE=postgres\n")
    env["CONSOLE_STORAGE_ENGINE"] = "sqlite"
    result = run_script(repo, env, "deploy.sh", "--yes")
    assert result.returncode != 0, result.stdout + result.stderr
    assert not commands(env)


def test_deployment_snapshot_contains_committed_wal_rows(deployment):
    repo, conn, env = deployment
    conn.execute("INSERT INTO samples VALUES ('committed-in-wal')")
    conn.commit()

    result = run_script(repo, env, "deploy.sh", "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    snapshots = list((repo / "backup").glob("console.db.*"))
    assert len(snapshots) == 1
    assert db_values(snapshots[0]) == ["current", "committed-in-wal"]


def fail_backup_member_copy(tmp_path, env, member):
    """Inject the same copy fault in the shell and Python snapshot implementations."""
    marker = tmp_path / "copy-failed"
    bin_dir = Path(env["PATH"].split(os.pathsep)[0])
    fake_cp = bin_dir / "cp"
    fake_cp.write_text(f"#!{sys.executable}\n" + f'''
import os
import sys
from pathlib import Path
if Path(sys.argv[-2]).name == {member!r} and Path(sys.argv[-1]).parent.name == "backup":
    Path({str(marker)!r}).write_text("shell copy failed")
    sys.exit(74)
os.execv({shutil.which("cp")!r}, [{shutil.which("cp")!r}, *sys.argv[1:]])
''')
    fake_cp.chmod(0o755)
    injected = tmp_path / "copy-fault-python"
    injected.mkdir()
    (injected / "sitecustomize.py").write_text(f'''
import shutil
from pathlib import Path
original_copy2 = shutil.copy2
def failing_copy2(source, target, *args, **kwargs):
    if Path(source).name == {member!r} and Path(target).parent.name == "backup":
        Path({str(marker)!r}).write_text("Python copy failed")
        raise OSError("injected YAML copy failure")
    return original_copy2(source, target, *args, **kwargs)
shutil.copy2 = failing_copy2
''')
    env["PYTHONPATH"] = str(injected)
    return marker


def assert_deployment_backup_aborted(repo, env, result):
    assert result.returncode != 0, result.stdout + result.stderr
    assert not list((repo / "backup").glob("gitsha.*"))
    assert not (repo / "backup/last_deploy.json").exists()
    assert not [c for c in commands(env) if c["kind"] == "docker" or c["args"][:1] in (["pull"], ["push"])]


@pytest.mark.parametrize("member", ["panels.yaml", "assets.yaml", "machines.yaml", "settings.yaml"])
def test_deployment_yaml_copy_failure_never_publishes_group_or_continues(deployment, tmp_path, member):
    repo, _conn, env = deployment
    marker = fail_backup_member_copy(tmp_path, env, member)
    result = run_script(repo, env, "deploy.sh", "--yes")
    assert marker.exists(), "the copy must actually fail, even after changing implementations"
    assert_deployment_backup_aborted(repo, env, result)
    assert (repo / "console" / member).is_file()


@pytest.mark.parametrize("member", ["panels.yaml", "assets.yaml"])
def test_deployment_missing_required_yaml_never_publishes_group(deployment, member):
    repo, _conn, env = deployment
    (repo / "console" / member).unlink()
    result = run_script(repo, env, "deploy.sh", "--yes")
    assert_deployment_backup_aborted(repo, env, result)


@pytest.mark.parametrize("failure", ["nonzero", "empty"])
def test_deployment_gitsha_failure_never_publishes_group_or_continues(deployment, tmp_path, failure):
    repo, _conn, env = deployment
    marker = tmp_path / "gitsha-failed"
    fake_git = Path(env["PATH"].split(os.pathsep)[0]) / "git"
    injection = f'''
if args and args[0] == "rev-parse" and "--short" not in args and "--abbrev-ref" not in args:
    pathlib.Path({str(marker)!r}).write_text("gitsha failure")
    sys.exit({23 if failure == "nonzero" else 0})
'''
    fake_git.write_text(fake_git.read_text().replace("args = sys.argv[1:]\n", "args = sys.argv[1:]\n" + injection, 1))
    result = run_script(repo, env, "deploy.sh", "--yes")
    assert marker.exists()
    assert_deployment_backup_aborted(repo, env, result)


def test_deployment_keeps_legacy_optional_yaml_omission_compatible(deployment):
    repo, _conn, env = deployment
    for member in ("machines.yaml", "settings.yaml"):
        (repo / "console" / member).unlink()
    result = run_script(repo, env, "deploy.sh", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    marker, = (repo / "backup").glob("gitsha.*")
    tag = marker.name.removeprefix("gitsha.")
    assert marker.read_text().strip() == git(repo, "rev-parse", "HEAD")
    for member in ("console.db", "panels.yaml", "assets.yaml"):
        assert (repo / "backup" / f"{member}.{tag}").is_file()
    for member in ("machines.yaml", "settings.yaml"):
        assert not (repo / "backup" / f"{member}.{tag}").exists()
        assert member + " 不存在，跳过" in result.stdout


def seed_group(repo, tag, missing=()):
    backup = repo / "backup"
    backup.mkdir(exist_ok=True)
    for name in MEMBERS:
        if name not in missing:
            (backup / f"{name}.{tag}").write_text(f"{name} for {tag}\n")


def test_retention_preserves_special_tags_and_archives_whole_groups(deployment):
    repo, _conn, env = deployment
    normal = [f"202608{day:02d}-120000" for day in range(1, 13)]
    special = ["preroll-20260701-000000", "20260230-120000", "20260814-250000"]
    for tag in normal + special:
        seed_group(repo, tag, missing=("machines.yaml", "settings.yaml") if tag == normal[0] else ())

    result = run_script(repo, env, "deploy.sh", "--yes")

    assert result.returncode == 0, result.stdout + result.stderr
    for tag in special:
        assert (repo / "backup" / f"gitsha.{tag}").exists(), tag
    # The freshly created deployment group is the tenth newest normal tag.
    for tag in normal[:3]:
        retired = repo / ".trash" / f"deploy-retention-{tag}"
        assert retired.is_dir(), "expired backups must be recoverable, never rm-ed"
        manifest = json.loads((retired / "manifest.json").read_text())
        assert manifest["status"] == "complete"
        assert manifest["tag"] == tag
        assert not (repo / "backup" / f"gitsha.{tag}").exists()
        for name in MEMBERS:
            if tag == normal[0] and name in ("machines.yaml", "settings.yaml"):
                assert manifest["members"][name]["status"] == "missing"
            else:
                assert (retired / f"{name}.{tag}").read_text() == f"{name} for {tag}\n"
    for tag in normal[3:]:
        assert (repo / "backup" / f"gitsha.{tag}").exists()


# --- PG-04：--storage-cutover 委托外置控制器（§8.3） --------------------------

def _stub_cutover_controller(repo):
    """把 fake repo 的 backup_runtime.py 换成 argv 记录器，验证委托而不真跑控制器。"""
    recorder = repo / "cutover-argv.txt"
    repo.joinpath("scripts/backup_runtime.py").write_text(
        "import json, os, sys\n"
        "with open(os.environ['CUTOVER_ARGV_LOG'], 'a') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    return recorder


def test_storage_cutover_requires_target_commit_and_yes(tmp_path):
    repo = make_repo(tmp_path)
    env = fake_environment(tmp_path)
    env["CUTOVER_ARGV_LOG"] = str(tmp_path / "argv.jsonl")
    _stub_cutover_controller(repo)

    missing = run_script(repo, env, "deploy.sh", "--storage-cutover", "--yes")
    assert missing.returncode == 1
    assert "--target-commit" in missing.stderr

    recorder = tmp_path / "argv.jsonl"
    no_yes = run_script(repo, env, "deploy.sh", "--storage-cutover",
                        "--target-commit", "a" * 40)
    assert no_yes.returncode == 1
    assert "--yes" in no_yes.stderr
    assert not recorder.exists()


def test_storage_cutover_delegates_to_controller_with_lock_fd(tmp_path):
    repo = make_repo(tmp_path)
    env = fake_environment(tmp_path)
    log = tmp_path / "argv.jsonl"
    env["CUTOVER_ARGV_LOG"] = str(log)
    _stub_cutover_controller(repo)
    sha = "b" * 40

    # 无既有锁 FD：控制器自行取锁（无 --lock-fd 参数）
    result = run_script(repo, env, "deploy.sh", "--storage-cutover",
                        "--target-commit", sha, "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    argv = json.loads(log.read_text().splitlines()[-1])
    assert argv[0] == "cutover"
    assert argv[1] == str(repo)
    assert "--target-commit" in argv and sha in argv
    assert "--lock-fd" not in argv

    # 已持有锁 FD（wrapper 场景）：沿用 FD，不递归再抢
    log.write_text("")
    env["CONSOLE_DEPLOY_LOCK_FD"] = "7"
    result = run_script(repo, env, "deploy.sh", "--storage-cutover",
                        "--target-commit", sha, "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    argv = json.loads(log.read_text().splitlines()[-1])
    assert "--lock-fd" in argv and "7" in argv


def test_private_panels_staged_content_never_enters_yaml_incorporation(deployment):
    repo, _conn, env = deployment
    private = repo / "console/backend/data/private-panels.yaml"
    private.write_text("[]\n")
    with (repo / ".env").open("a") as handle:
        handle.write(f"CONSOLE_PANELS_HOST_PATH={private}\n")
    (repo / "console/panels.yaml").write_text("- id: synthetic-staged-private\n")
    git(repo, "add", "console/panels.yaml")
    (repo / "console/settings.yaml").write_text('{"synthetic":true}\n')
    result = run_script(repo, env, "deploy.sh", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(repo, "show", "HEAD:console/panels.yaml") == FIXTURE_PANELS.strip()
    assert "synthetic-staged-private" in git(repo, "show", ":console/panels.yaml")
    assert "synthetic" in git(repo, "show", "HEAD:console/settings.yaml")


def test_default_panels_path_still_incorporated_and_snapshotted(deployment):
    repo, _conn, env = deployment
    content = "- id: synthetic-default-panel\n"
    (repo / "console/panels.yaml").write_text(content)
    result = run_script(repo, env, "deploy.sh", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(repo, "show", "HEAD:console/panels.yaml") == content.strip()
    record = json.loads((repo / "backup/last_deploy.json").read_text())
    assert (repo / "backup" / f"panels.yaml.{record['backup_tag']}").read_text() == content
