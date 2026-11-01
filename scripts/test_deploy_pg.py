"""Ordinary deploy.sh against real isolated PG; Docker transport/build/health are simulated."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import backup_db
import pg_migrations
import pg_test_support as support
import test_deploy_safety as tds
from test_backup_pay import _seed_real_pay
from test_backup_runtime_cutover import _seed_pg03_into_repo


@pytest.fixture
def pg_deployment(tmp_path):
    source = support.start_pg16()
    try:
        root = tds.make_repo(tmp_path)
        _seed_pg03_into_repo(root)
        tds.make_db(root / "console/backend/data/console.db").close()
        with support.connect(source.dsn(), autocommit=True) as conn:
            pg_migrations.bootstrap_roles(conn, database=source.dbname)
            pg_migrations.apply_as_owner(conn)
            conn.execute("INSERT INTO bookmarks (id, title, url) VALUES ('kept', 'kept', 'https://kept.invalid')")
        _seed_real_pay(source, root / "console/backend/data/pay")
        secret = tmp_path / "admin.secret"
        secret.write_text(source.password or "test-placeholder")
        secret.chmod(0o600)
        (root / ".env").write_text(
            "CONSOLE_STORAGE_ENGINE=postgres\n"
            f"CONSOLE_PG_DB={source.dbname}\nCONSOLE_PG_BACKUP_DB={source.dbname}\n"
            f"CONSOLE_PG_BACKUP_CONTAINER={source.name}\nCONSOLE_PG_BACKUP_USER={source.user}\n"
            f"CONSOLE_PG_ADMIN_PASSWORD_FILE={secret}\n")
        env = tds.fake_environment(tmp_path)
        env.update(TEST_PG_HOST=source.host, TEST_PG_PORT=str(source.port),
                   TEST_PG_CONTAINER=source.name, TEST_ADMIN_SECRET=str(secret),
                   TEST_PG_BIN=os.environ.get("CONSOLE_PG_TEST_NATIVE_BIN", ""),
                   TEST_PYTHON=sys.executable)
        fake_bin = Path(env["PATH"].split(os.pathsep)[0])
        fake_docker = fake_bin / "docker"
        injection = '''
if kind == "docker" and args[:1] == ["exec"]:
    rest = list(args[1:])
    if rest[0] == "-i":
        rest.pop(0)
    name, tool, *tool_args = rest
    if name != os.environ["TEST_PG_CONTAINER"] or tool not in ("psql", "pg_dump", "pg_restore"):
        sys.exit(90)
    if name == "native":
        executable = str(pathlib.Path(os.environ["TEST_PG_BIN"]) / tool)
        options = [] if tool == "pg_restore" or "--version" in tool_args else ["-h", os.environ["TEST_PG_HOST"], "-p", os.environ["TEST_PG_PORT"]]
        os.execv(executable, [executable, *options, *tool_args])
    os.execv(os.environ["REAL_DOCKER"], [os.environ["REAL_DOCKER"], *args])
if kind == "docker" and args[:2] == ["compose", "build"]:
    sys.exit(24 if os.getenv("FAIL_STAGE") == "build" else 0)
if kind == "docker" and args[:2] == ["compose", "run"]:
    if os.getenv("FAIL_STAGE") == "migrate":
        sys.exit(25)
    if os.getenv("FAIL_STAGE") == "schema":
        print('{"ok":true}')
        sys.exit(0)
    script_index = args.index("/release/scripts/deploy_pg_schema.py")
    child_args = args[script_index + 1:]
    child_args[child_args.index("--host") + 1] = os.environ["TEST_PG_HOST"]
    child_args[child_args.index("--password-file") + 1] = os.environ["TEST_ADMIN_SECRET"]
    child_args += ["--port", os.environ["TEST_PG_PORT"]]
    os.execv(os.environ["TEST_PYTHON"], [os.environ["TEST_PYTHON"], "scripts/deploy_pg_schema.py", *child_args])
if kind == "docker" and args[:2] == ["compose", "up"]:
    state_path = pathlib.Path(os.environ["COMMAND_LOG"] + ".state")
    state_path.write_text(json.dumps({"running": True, "commit": os.getenv("GIT_COMMIT")}))
    sys.exit(0)
'''
        fake_docker.write_text(fake_docker.read_text().replace('if kind == "git":', injection + '\nif kind == "git":', 1))
        env["REAL_DOCKER"] = shutil.which("docker") or "/nonexistent/docker"
        # A real pull supplies new source after the old schema's snapshot. Simulate that
        # source transition in the disposable Git repo, retaining the released v12 SQL.
        # The appended version must stay latest+1: a hardcoded number rots the moment
        # the real PG_MIGRATIONS grows (migration 17 did exactly that).
        next_version = pg_migrations.latest_schema_version() + 1
        fake_git = fake_bin / "git"
        pull_injection = f'''
if kind == "git" and args[:1] == ["pull"]:
    import subprocess
    path = pathlib.Path("console/backend/pg_migrations.py")
    path.write_text(path.read_text() + '\\nPG_MIGRATIONS += (({next_version}, "test_deploy_append", ("ALTER TABLE bookmarks ADD COLUMN deploy_added TEXT",)),)\\n')
    subprocess.run([os.environ["REAL_GIT"], "add", "console/backend/pg_migrations.py"], check=True)
    subprocess.run([os.environ["REAL_GIT"], "commit", "-qm", "test appended migration"], check=True)
    sys.exit(0)
'''
        fake_git.write_text(fake_git.read_text().replace('if kind == "git":', pull_injection + '\nif kind == "git":', 1))
        yield root, source, env
    finally:
        source.stop()


def test_ordinary_pg_deploy_backs_up_then_appends_schema_before_start(pg_deployment):
    import test_backup_db_pg as pg

    root, source, env = pg_deployment
    published = subprocess.run(
        [sys.executable, str(root / "scripts/backup_runtime.py"), "publish-runtime", str(root)],
        cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert published.returncode == 0, published.stderr
    old_runtime = json.loads(published.stdout)
    result = tds.run_script(root, env, "deploy.sh", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    log = tds.commands(env)
    def index(kind, prefix):
        return next(i for i, row in enumerate(log)
                    if row["kind"] == kind and row["args"][:len(prefix)] == prefix)
    dump = next(i for i, row in enumerate(log) if "pg_dump" in row["args"] and "--version" not in row["args"])
    assert dump < index("git", ["pull"]) < index("docker", ["compose", "build"])
    assert index("docker", ["compose", "build"]) < index("docker", ["compose", "run"]) < index("docker", ["compose", "up"])
    assert all(row["engine"] == "postgres" for row in log)
    record = json.loads((root / "backup/last_deploy.json").read_text())
    live_before = pg_migrations.latest_schema_version()
    appended = live_before + 1
    assert (record["engine"], record["schema_before"], record["migrations_applied"]) == (
        "postgres", live_before, appended)
    assert not list((root / "backup").glob("console.db.*"))
    manifest = json.loads(next((root / "backup").glob("console.dump.*.manifest.json")).read_text())
    assert manifest["schema_version"] == live_before
    assert manifest["row_digest"]["tables"]["bookmarks"]["rows"] == 2
    dump = root / "backup" / f"console.dump.{record['backup_tag']}"
    assert backup_db.backup_pay.verify_pay_snapshot(dump, manifest)["batches"] == 2
    with support.connect(source.dsn(), autocommit=True) as conn:
        assert conn.execute("SELECT max(version) AS v FROM schema_migrations").fetchone()["v"] == appended
        assert conn.execute("SELECT deploy_added FROM bookmarks WHERE id='kept'").fetchone()["deploy_added"] is None
        assert conn.execute("SELECT pg_get_userbyid(relowner) AS owner FROM pg_class WHERE relname='bookmarks'").fetchone()["owner"] == "console_owner"
    # 部署本身必须发布新控制器；不能在测试里手动 publish 掩盖遗漏。
    runtime = json.loads((root / "backup/.runtime/current/manifest.json").read_text())
    assert "console/backend/pg_migrations.py" in runtime["files"]
    assert runtime["version"] != old_runtime["version"]
    timed = subprocess.run(
        [sys.executable, runtime["entry"], "timed", str(root)],
        cwd=root, env=env, capture_output=True, text=True, timeout=90)
    assert timed.returncode == 0, timed.stdout + timed.stderr
    timed_status = json.loads((root / "backup/timed/status.json").read_text())
    next_dump = root / "backup/timed" / timed_status["snapshot"]
    assert backup_db.read_dump_manifest(next_dump)["schema_version"] == appended
    target = support.start_pg16()
    try:
        with support.connect(target.dsn(), autocommit=True) as conn:
            pg_migrations.bootstrap_roles(conn, database=target.dbname)
        restored = backup_db.verify_restore(next_dump, boot_target=pg._boot_target_fn(target))
        assert restored["schema_version"] == appended and restored["pay"]["batches"] == 2
    finally:
        target.stop()


@pytest.mark.parametrize("stage", ["build", "migrate", "schema"])
def test_ordinary_pg_deploy_failure_does_not_start_app_or_claim_success(pg_deployment, stage):
    root, _source, env = pg_deployment
    result = tds.run_script(root, {**env, "FAIL_STAGE": stage}, "deploy.sh", "--yes")
    assert result.returncode != 0, result.stdout + result.stderr
    assert not (root / "backup/last_deploy.json").exists()
    assert not any(row["args"][:2] == ["compose", "up"] for row in tds.commands(env))
    assert len(list((root / "backup").glob("console.dump.*.manifest.json"))) == 1


def test_runtime_publish_failure_prevents_start_and_success_record(pg_deployment):
    root, _source, env = pg_deployment
    runtime_dir = root / "backup/.runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "entry.py").write_text("synthetic conflicting launcher\n")
    result = tds.run_script(root, env, "deploy.sh", "--yes")
    assert result.returncode != 0
    assert "稳定 runtime 入口内容冲突" in result.stdout + result.stderr
    assert not (root / "backup/last_deploy.json").exists()
    assert not any(row["args"][:2] == ["compose", "up"] for row in tds.commands(env))


def test_pinned_timed_entry_backs_up_pg_pay_and_preserves_good_group_on_failure(pg_deployment):
    """Run the actual immutable entry, without an engine in the process environment."""
    import test_backup_db_pg as pg

    root, source, env = pg_deployment
    env = {key: value for key, value in env.items() if key != "CONSOLE_STORAGE_ENGINE"}
    published = subprocess.run(
        [sys.executable, str(root / "scripts/backup_runtime.py"), "publish-runtime", str(root)],
        cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert published.returncode == 0, published.stdout + published.stderr
    runtime = json.loads(published.stdout)
    assert "scripts/backup_pay.py" in runtime["files"]
    # Changing mutable checkout source must not affect the pinned timer controller.
    (root / "scripts/backup_pay.py").write_text("raise RuntimeError('mutable checkout must not run')\n")
    command = [sys.executable, runtime["entry"], "timed", str(root)]
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    backup = root / "backup/timed"
    status = json.loads((backup / "status.json").read_text())
    assert status["engine"] == "postgres" and status["ok"]
    dump = backup / status["snapshot"]
    companion = Path(str(dump) + ".pay.tar")
    manifest = Path(str(dump) + ".manifest.json")
    before = {path: path.read_bytes() for path in (dump, companion, manifest)}
    target = support.start_pg16()
    try:
        with support.connect(target.dsn(), autocommit=True) as conn:
            pg_migrations.bootstrap_roles(conn, database=target.dbname)
        report = backup_db.verify_restore(
            dump, boot_target=pg._boot_target_fn(target), pay_dest=root / "independent-pay-restore")
        assert report["pay"]["verified"] and report["pay"]["batches"] == 2
        assert pg._psql(target, "SELECT count(*) FROM ledger_file_batches") == "2"
    finally:
        target.stop()
    original = next((root / "console/backend/data/pay/archive").glob("*/*.csv"))
    original.write_bytes(b"damaged source original")
    failed = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=90)
    assert failed.returncode != 0
    assert {path: path.read_bytes() for path in before} == before
    latest = json.loads((backup / "status.json").read_text())
    assert latest["ok"] is False and latest["last_success"] == status["last_success"]
    print("PINNED_TIMED " + json.dumps({**report["pay"], "path": "independent-private-target",
                                       "engine": "postgres", "previous_group": "preserved"}, sort_keys=True))


def test_pg_deploy_snapshots_private_panels_without_committing_tracked_panels(pg_deployment):
    root, _source, env = pg_deployment
    private = root / "console/backend/data/private panels.yaml"
    private.write_text("- id: private-synthetic\n  note: synthetic-private-value\n")
    link = root / "console/backend/data/panels-link.yaml"
    link.symlink_to(private)
    with (root / ".env").open("a") as handle:
        handle.write(f"CONSOLE_PANELS_HOST_PATH='{link}'\n")
    tracked = root / "console/panels.yaml"
    tracked.write_text("- id: tracked-synthetic-secret\n")
    # Other UI YAML must still be incorporated normally.
    (root / "console/settings.yaml").write_text('{"synthetic": true}\n')
    result = tds.run_script(root, env, "deploy.sh", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads((root / "backup/last_deploy.json").read_text())
    assert (root / "backup" / f"panels.yaml.{record['backup_tag']}").read_bytes() == private.read_bytes()
    assert tds.git(root, "show", "HEAD:console/panels.yaml") == tds.FIXTURE_PANELS.strip()
    assert "synthetic" in tds.git(root, "show", "HEAD:console/settings.yaml")
    adds = [row["args"] for row in tds.commands(env) if row["kind"] == "git" and row["args"][:1] == ["add"]]
    assert adds and all("console/panels.yaml" not in args for args in adds)


def test_pg_deploy_missing_private_panels_fails_before_side_effects(pg_deployment):
    root, _source, env = pg_deployment
    with (root / ".env").open("a") as handle:
        handle.write(f"CONSOLE_PANELS_HOST_PATH={root / 'missing.yaml'}\n")
    (root / "console/settings.yaml").write_text('{"synthetic": true}\n')
    result = tds.run_script(root, env, "deploy.sh", "--yes")
    assert result.returncode != 0
    assert "CONSOLE_PANELS_HOST_PATH" in result.stderr
    assert not tds.commands(env)
    assert not list((root / "backup").glob("gitsha.*"))


def test_external_host_layout_deploy_and_pinned_timer(pg_deployment):
    root, _source, env = pg_deployment
    backup, trash = root.parent / '.bak/repo', root.parent / '.trash/repo'
    with (root / '.env').open('a') as handle:
        handle.write(f'CONSOLE_HOST_BACKUP_DIR={backup}\nCONSOLE_HOST_TRASH_DIR={trash}\n')
    result = tds.run_script(root, env, 'deploy.sh', '--yes')
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((backup / 'last_deploy.json').read_text())['engine'] == 'postgres'
    assert (backup / '.runtime/current/manifest.json').is_file()
    # The timer must work from its immutable controller with a changed checkout.
    (root / 'scripts/host_paths.py').write_text("raise RuntimeError('mutable checkout')\n")
    timer = subprocess.run([sys.executable, str(backup / '.runtime/entry.py'), 'timed', str(root)],
                           env=env, capture_output=True, text=True)
    assert timer.returncode == 0, timer.stdout + timer.stderr
    status = json.loads((backup / 'timed/status.json').read_text())
    assert status['ok'] is True
    assert list((backup / 'timed').glob('console.dump.*.pay.tar'))
    assert not (root / 'backup').exists()
    assert not (root / '.trash').exists()
