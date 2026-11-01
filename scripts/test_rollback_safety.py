"""Actual rollback shell tests: temporary Git/SQLite/YAML and fake Docker only."""
import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import backup_db
import backup_runtime as runtime
from test_deploy_safety import (
    CURRENT_SCHEMA_VERSIONS,
    copy_backend_module,
    FIXTURE_PANELS,
    SOURCE,
    TAG,
    commands,
    db_values,
    fake_environment,
    git,
    make_db,
    make_repo,
    run_script,
)


def backup_group(repo, tag=TAG, schema=None, sha=None):
    backup = repo / "backup"
    backup.mkdir(exist_ok=True)
    source = repo / "console/backend/data" / f"fixture-target-{tag}.db"
    conn = make_db(source, "target", schema=schema)
    try:
        # A restore input is a standalone API snapshot, never a live DB with sidecars.
        backup_db.write_snapshot(backup / f"console.db.{tag}", source)
    finally:
        conn.close()
    for name in ("panels.yaml", "assets.yaml", "machines.yaml", "settings.yaml"):
        shutil.copy2(repo / "console" / name, backup / f"{name}.{tag}")
    (backup / f"gitsha.{tag}").write_text((sha or git(repo, "rev-parse", "HEAD")) + "\n")


@pytest.fixture
def rollback_env(tmp_path):
    repo = make_repo(tmp_path)
    conn = make_db(repo / "console/backend/data/console.db")
    conn.close()
    backup_group(repo)
    return repo, fake_environment(tmp_path)


def test_corrupt_backup_refused_before_git_or_docker_mutation(rollback_env):
    repo, env = rollback_env
    (repo / "backup" / f"console.db.{TAG}").write_bytes(b"not a sqlite database")

    result = run_script(repo, env, "rollback.sh", TAG)

    assert result.returncode != 0, result.stdout + result.stderr
    assert db_values(repo / "console/backend/data/console.db") == ["current"]
    assert not [c for c in commands(env) if c["kind"] == "docker" or c["args"][:1] in (["checkout"], ["stash"])]


def test_git_checkout_failure_never_falls_back_to_data(rollback_env):
    repo, env = rollback_env
    env["FAIL_GIT"] = "checkout"

    result = run_script(repo, env, "rollback.sh", TAG)

    assert result.returncode != 0, result.stdout + result.stderr
    assert db_values(repo / "console/backend/data/console.db") == ["current"]
    assert not [c for c in commands(env) if c["kind"] == "docker" and c["args"][:2] == ["compose", "up"]]


def test_stop_precedes_recovery_snapshot_and_rebuild_acceptance(rollback_env):
    repo, env = rollback_env
    env["STOP_DB_VALUE"] = "last-write-at-stop"

    result = run_script(repo, env, "rollback.sh", TAG)

    assert result.returncode == 0, result.stdout + result.stderr
    recovery = list((repo / "backup").glob("console.db.preroll-*"))
    assert len(recovery) == 1, "restore needs a stopped-writer recovery snapshot"
    assert db_values(recovery[0]) == ["current", "last-write-at-stop"]
    assert db_values(repo / "console/backend/data/console.db") == ["target"]
    calls = commands(env)
    stop = next(i for i, c in enumerate(calls) if c["args"][:2] == ["compose", "stop"])
    checkout = next(i for i, c in enumerate(calls) if c["kind"] == "git" and c["args"][:1] == ["checkout"])
    up = next(i for i, c in enumerate(calls) if c["args"][:2] == ["compose", "up"])
    assert stop < checkout < up
    assert calls[stop]["args"][-1] == calls[up]["args"][-1] == "console"
    assert "--no-deps" in calls[up]["args"]
    assert calls[up]["commit"] == git(repo, "rev-parse", "--short", "HEAD")
    result_record = json.loads((repo / "backup/last_rollback.json").read_text())
    assert result_record["status"] == "success"
    assert result_record["phase"] == "complete"


@pytest.mark.parametrize("extra", [[], ["--data-only"]])
@pytest.mark.parametrize("member", ["console.db", "panels.yaml", "assets.yaml", "gitsha"])
def test_missing_required_backup_member_refused(rollback_env, member, extra):
    repo, env = rollback_env
    (repo / "backup" / f"{member}.{TAG}").unlink()
    result = run_script(repo, env, "rollback.sh", TAG, *extra)
    assert result.returncode != 0
    assert db_values(repo / "console/backend/data/console.db") == ["current"]
    assert not [c for c in commands(env) if c["kind"] == "docker"]


@pytest.mark.parametrize("name,content", [
    ("panels.yaml", "[broken"), ("assets.yaml", "services: []"),
    ("machines.yaml", "[not-a-mapping]"), ("settings.yaml", "[]"),
])
def test_invalid_yaml_refused_without_leaking_values(rollback_env, name, content):
    repo, env = rollback_env
    marker = "NEVER_LOG_THIS_PRIVATE_VALUE"
    (repo / "backup" / f"{name}.{TAG}").write_text(content + " # " + marker)
    result = run_script(repo, env, "rollback.sh", TAG)
    assert result.returncode != 0
    assert marker not in result.stdout + result.stderr
    assert db_values(repo / "console/backend/data/console.db") == ["current"]
    assert not [c for c in commands(env) if c["kind"] == "docker"]


@pytest.mark.parametrize("sha", ["main", "--help", "a" * 40, "abc\n123", "../../other"])
def test_invalid_or_missing_git_object_refused(rollback_env, sha):
    repo, env = rollback_env
    (repo / "backup" / f"gitsha.{TAG}").write_text(sha)
    result = run_script(repo, env, "rollback.sh", TAG)
    assert result.returncode != 0
    assert not [c for c in commands(env) if c["kind"] == "docker"]
    assert db_values(repo / "console/backend/data/console.db") == ["current"]


def test_optional_legacy_yamls_can_be_absent(rollback_env):
    repo, env = rollback_env
    for name in ("machines.yaml", "settings.yaml"):
        (repo / "backup" / f"{name}.{TAG}").unlink()
    result = run_script(repo, env, "rollback.sh", TAG)
    assert result.returncode == 0, result.stdout + result.stderr
    assert db_values(repo / "console/backend/data/console.db") == ["target"]
    assert (repo / "console/machines.yaml").read_text() == "[]\n"
    assert (repo / "console/settings.yaml").read_text() == "{}\n"


def test_data_only_is_explicit_and_preserves_code_and_other_dirty_files(rollback_env):
    repo, env = rollback_env
    original_sha = git(repo, "rev-parse", "HEAD")
    original_branch = git(repo, "branch", "--show-current")
    dirty = repo / "console/backend/db.py"
    dirty.write_text(dirty.read_text() + "\n# keep this local code edit\n")
    untracked = repo / "operator-note.txt"
    untracked.write_text("preserve me")
    result = run_script(repo, env, "rollback.sh", TAG, "--data-only")
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(repo, "rev-parse", "HEAD") == original_sha
    assert git(repo, "branch", "--show-current") == original_branch
    assert dirty.read_text().endswith("# keep this local code edit\n")
    assert untracked.read_text() == "preserve me"
    assert db_values(repo / "console/backend/data/console.db") == ["target"]
    assert not [c for c in commands(env) if c["kind"] == "git" and c["args"][:1] in (["stash"], ["checkout"])]


@pytest.mark.parametrize("which", ["target", "current", "code", "gap"])
def test_data_only_requires_known_matching_schema(rollback_env, which):
    repo, env = rollback_env
    if which == "code":
        # MIGRATIONS 字面量在 kernel/backend/db_sqlite（或过渡期 console 真身/旧 db.py）。
        for candidate in (
            "kernel/backend/db_sqlite.py",
            "console/backend/db_sqlite.py",
            "console/backend/db.py",
        ):
            path = repo / candidate
            if path.is_file() and "Thin relocating shim" not in path.read_text()[:240]:
                break
        else:
            raise AssertionError("fixture missing MIGRATIONS body")
        path.write_text(path.read_text() + "\nMIGRATIONS = (*MIGRATIONS, (max(v for v, _, _ in MIGRATIONS) + 1, 'future', ()))\n")
    else:
        path = repo / (f"backup/console.db.{TAG}" if which in ("target", "gap") else "console/backend/data/console.db")
        conn = sqlite3.connect(path)
        missing_version = CURRENT_SCHEMA_VERSIONS[4] if which == "gap" else max(CURRENT_SCHEMA_VERSIONS)
        conn.execute("DELETE FROM schema_migrations WHERE version = ?", (missing_version,))
        conn.commit()
        conn.close()
    result = run_script(repo, env, "rollback.sh", TAG, "--data-only")
    assert result.returncode != 0, result.stdout + result.stderr
    assert not [c for c in commands(env) if c["kind"] == "docker"]
    assert db_values(repo / "console/backend/data/console.db") == ["current"]


def test_full_rollback_preserves_tracked_and_untracked_dirty_changes(rollback_env):
    repo, env = rollback_env
    dirty = "- name: local-draft\n"
    (repo / "console/panels.yaml").write_text(dirty)
    (repo / "operator-note.txt").write_text("saved untracked")
    result = run_script(repo, env, "rollback.sh", TAG)
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads((repo / "backup/last_rollback.json").read_text())
    stash = record["stash"]
    assert git(repo, "show", stash + ":console/panels.yaml") == dirty.strip()
    assert git(repo, "show", stash + "^3:operator-note.txt") == "saved untracked"
    assert (repo / "backup" / f"panels.yaml.{record['recovery_tag']}").read_text() == dirty
    assert (repo / "console/panels.yaml").read_text() == FIXTURE_PANELS


def test_stash_failure_keeps_current_data_and_recovery_handle(rollback_env):
    repo, env = rollback_env
    (repo / "console/panels.yaml").write_text("- name: local-draft\n")
    env["FAIL_GIT"] = "stash"
    result = run_script(repo, env, "rollback.sh", TAG)
    assert result.returncode != 0
    assert db_values(repo / "console/backend/data/console.db") == ["current"]
    record = json.loads((repo / "backup/last_rollback.json").read_text())
    assert (record["status"], record["phase"], record["recovery_snapshot"]) == ("failed", "stash", "complete")
    assert not [c for c in commands(env) if c["kind"] == "git" and c["args"][:1] == ["checkout"]]


@pytest.mark.parametrize("failure,phase", [("stop", "stop"), ("up", "build"), ("health", "acceptance")])
def test_docker_failure_records_real_stage_and_never_success(rollback_env, failure, phase):
    repo, env = rollback_env
    (repo / "backup/last_deploy.json").write_text('{"commit":"previous-success"}')
    env["FAIL_DOCKER"] = failure
    result = run_script(repo, env, "rollback.sh", TAG, "--health-timeout", "0.01", "--poll-interval", "0.01")
    assert result.returncode != 0
    record = json.loads((repo / "backup/last_rollback.json").read_text())
    assert (record["status"], record["phase"]) == ("failed", phase)
    assert json.loads((repo / "backup/last_deploy.json").read_text())["commit"] == "previous-success"
    assert "回滚完成" not in result.stdout
    if failure != "stop":
        assert record["recovery_snapshot"] == "complete"
        assert record["failure_stop"] == "confirmed"
        assert Path(record["controller"]).is_file()


def test_running_container_after_stop_blocks_snapshot_and_restore(rollback_env):
    repo, env = rollback_env
    env["KEEP_RUNNING"] = "1"
    result = run_script(repo, env, "rollback.sh", TAG)
    assert result.returncode != 0
    assert db_values(repo / "console/backend/data/console.db") == ["current"]
    assert not list((repo / "backup").glob("console.db.preroll-*"))
    record = json.loads((repo / "backup/last_rollback.json").read_text())
    assert record["phase"] == "stop"


def test_recovery_snapshot_api_failure_blocks_git_and_restore(rollback_env):
    repo, env = rollback_env
    path = repo / "console/backend/db.py"
    path.write_text(path.read_text() + "\ndef backup_database(dest_path, db_path=None):\n    raise OSError('injected snapshot error')\n")
    git(repo, "add", "console/backend/db.py")
    git(repo, "commit", "-m", "inject snapshot error")
    result = run_script(repo, env, "rollback.sh", TAG)
    assert result.returncode != 0
    assert db_values(repo / "console/backend/data/console.db") == ["current"]
    assert not list((repo / "backup").glob("gitsha.preroll-*"))
    record = json.loads((repo / "backup/last_rollback.json").read_text())
    assert (record["phase"], record["recovery_snapshot"]) == ("snapshot", "incomplete")
    assert not [c for c in commands(env) if c["kind"] == "git" and c["args"][:1] in (["checkout"], ["stash"])]


def test_controller_survives_checkout_that_removes_both_helper_modules(tmp_path):
    repo = make_repo(tmp_path)
    for name in ("backup_runtime.py", "backup_db.py"):
        (repo / "scripts" / name).unlink()
    (repo / "console/backend/composition.py").unlink()
    (repo / "console/backend/pg_backup.py").unlink()
    (repo / "scripts/rollback.sh").write_text("#!/bin/bash\necho legacy-controller\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "legacy code without helpers")
    target_sha = git(repo, "rev-parse", "HEAD")
    source_scripts = Path(__file__).resolve().parent
    for name in ("backup_runtime.py", "backup_db.py", "rollback.sh"):
        shutil.copy2(source_scripts / name, repo / "scripts" / name)
    copy_backend_module(SOURCE, "composition.py", repo / "console/backend")
    copy_backend_module(SOURCE, "pg_backup.py", repo / "console/backend")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "current helpers")
    make_db(repo / "console/backend/data/console.db").close()
    backup_group(repo, sha=target_sha)
    env = fake_environment(tmp_path)

    result = run_script(repo, env, "rollback.sh", TAG)

    assert result.returncode == 0, result.stdout + result.stderr
    assert git(repo, "rev-parse", "HEAD") == target_sha
    assert not (repo / "scripts/backup_runtime.py").exists()
    assert not (repo / "scripts/backup_db.py").exists()
    assert not (repo / "console/backend/composition.py").exists()
    assert db_values(repo / "console/backend/data/console.db") == ["target"]
    record = json.loads((repo / "backup/last_rollback.json").read_text())
    assert Path(record["controller"]).is_file()
    assert (Path(record["controller"]).parent.parent / "console/backend/composition.py").is_file()
    # The printed fallback actually works from the legacy tree, not just as a path.
    recovery = subprocess.run(record["recovery_command"], cwd=repo, env=env, text=True, capture_output=True, timeout=25)
    assert recovery.returncode == 0, recovery.stdout + recovery.stderr
    assert db_values(repo / "console/backend/data/console.db") == ["current"]


@pytest.mark.parametrize("command", ["deploy", "rollback", "timed"])
def test_all_entrypoints_share_the_real_data_lock_through_symlinks(rollback_env, tmp_path, command):
    repo, env = rollback_env
    data = repo / "console/backend/data"
    real_data = tmp_path / "real-data"
    data.rename(real_data)
    data.symlink_to(real_data, target_is_directory=True)
    alias = tmp_path / "repo-alias"
    alias.symlink_to(repo, target_is_directory=True)
    env["CONSOLE_BACKUP_ROOT"] = str(tmp_path / "timed")
    with backup_db.operation_lock(real_data / "console.db"):
        if command == "timed":
            result = subprocess.run([sys.executable, str(alias / "scripts/backup_db.py")], cwd=alias, env=env, text=True, capture_output=True, timeout=10)
        else:
            result = run_script(alias, env, command + ".sh", *( ["--yes"] if command == "deploy" else [TAG]))
    assert result.returncode != 0, result.stdout + result.stderr
    assert "互斥锁" in result.stderr
    assert not [c for c in commands(env) if c["kind"] == "docker" or c["args"][:1] in (["pull"], ["checkout"], ["stash"])]
    assert db_values(real_data / "console.db") == ["current"]
    assert not (tmp_path / "timed").exists()


def test_killed_lock_owner_releases_lock_for_the_host_timed_backup(rollback_env, tmp_path):
    repo, env = rollback_env
    env["CONSOLE_BACKUP_ROOT"] = str(tmp_path / "timed")
    program = "import sys,time; sys.path.insert(0,'scripts'); import backup_db; " \
              "lock=backup_db.operation_lock('console/backend/data/console.db'); lock.__enter__(); " \
              "print('locked',flush=True); time.sleep(30)"
    owner = subprocess.Popen([sys.executable, "-c", program], cwd=repo, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert owner.stdout.readline().strip() == "locked"
        blocked = subprocess.run([sys.executable, "scripts/backup_db.py"], cwd=repo, env=env, text=True, capture_output=True, timeout=10)
        assert blocked.returncode != 0
        owner.kill()
        owner.wait(timeout=5)
        result = subprocess.run([sys.executable, "scripts/backup_db.py"], cwd=repo, env=env, text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(list((tmp_path / "timed").glob("console.db.*"))) == 1
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=5)


def wait_until(predicate, description, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out waiting for {description}")
        time.sleep(0.01)


def process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def pause_fake_command(env, tmp_path, kind, prefix):
    gate = tmp_path / "paused-command"
    gate.mkdir()
    fake = Path(env["PATH"].split(os.pathsep)[0]) / kind
    pause = f'''
if args[:{len(prefix)}] == {prefix!r}:
    import time
    gate = pathlib.Path({str(gate)!r})
    (gate / "ready").write_text(str(os.getpid()))
    while not (gate / "release").exists():
        time.sleep(0.01)
'''
    fake.write_text(fake.read_text().replace("args = sys.argv[1:]\n", "args = sys.argv[1:]\n" + pause, 1))
    return gate


@pytest.mark.parametrize("phase,kind,prefix", [
    ("stop", "docker", ["compose", "stop"]),
    ("stash", "git", ["stash"]),
    ("checkout", "git", ["checkout"]),
    ("build", "docker", ["compose", "up"]),
])
def test_sigkill_controller_keeps_critical_child_lock_until_exit(rollback_env, tmp_path, phase, kind, prefix):
    repo, env = rollback_env
    if phase == "stash":
        with (repo / "console/assets.yaml").open("a") as handle:
            handle.write("# uncommitted local edit\n")
    gate = pause_fake_command(env, tmp_path, kind, prefix)
    env["CONSOLE_BACKUP_ROOT"] = str(tmp_path / "timed")
    controller = subprocess.Popen(
        ["bash", str(repo / "scripts/rollback.sh"), TAG], cwd=repo, env=env,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    child_pid = None
    try:
        wait_until(lambda: (gate / "ready").exists(), f"paused {phase} child")
        child_pid = int((gate / "ready").read_text())
        record = json.loads((repo / "backup/last_rollback.json").read_text())
        assert (record["status"], record["phase"]) == ("running", phase)
        controller.kill()
        assert controller.wait(timeout=5) == -signal.SIGKILL
        assert process_alive(child_pid), "the actual operation must still be in flight"

        for command in (
            [sys.executable, str(repo / "scripts/backup_db.py")],
            ["bash", str(repo / "scripts/rollback.sh"), TAG],
            ["bash", str(repo / "scripts/deploy.sh"), "--yes"],
        ):
            blocked = subprocess.run(command, cwd=repo, env=env, text=True, capture_output=True, timeout=10)
            assert blocked.returncode != 0, f"{phase} child {child_pid} still alive: {blocked.stdout} {blocked.stderr}"
            assert "互斥锁" in blocked.stderr
        assert not (tmp_path / "timed").exists()
        assert json.loads((repo / "backup/last_rollback.json").read_text()) == record

        (gate / "release").touch()
        wait_until(lambda: not process_alive(child_pid), f"{phase} child to exit")
        with backup_db.operation_lock(repo / "console/backend/data/console.db"):
            pass
        timed = subprocess.run(
            [sys.executable, str(repo / "scripts/backup_db.py")], cwd=repo, env=env,
            text=True, capture_output=True, timeout=10,
        )
        assert timed.returncode == 0, timed.stdout + timed.stderr
        assert len(list((tmp_path / "timed").glob("console.db.*"))) == 1
    finally:
        (gate / "release").touch()
        if controller.poll() is None:
            controller.kill()
        controller.communicate(timeout=5)
        if child_pid is not None and process_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_failed_critical_child_releases_lock_for_timed_backup_and_retry(rollback_env, tmp_path, monkeypatch, failure):
    repo, env = rollback_env
    gate = pause_fake_command(env, tmp_path, "docker", ["compose", "up"])
    env["CONSOLE_BACKUP_ROOT"] = str(tmp_path / "timed")
    if failure == "exit":
        env["FAIL_DOCKER"] = "up"
        (gate / "release").touch()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    real_run = runtime.run_command

    def bounded_command(root, *args, **kwargs):
        if failure == "timeout" and args[:3] == ("docker", "compose", "up"):
            kwargs["timeout"] = 0.25
        return real_run(root, *args, **kwargs)

    monkeypatch.setattr(runtime, "run_command", bounded_command)
    expected = RuntimeError if failure == "exit" else subprocess.TimeoutExpired
    with pytest.raises(expected):
        runtime.rollback(repo, TAG)
    child_pid = int((gate / "ready").read_text())
    assert not process_alive(child_pid), "failed/timed-out commands must be reaped before unlocking"
    record = json.loads((repo / "backup/last_rollback.json").read_text())
    assert (record["status"], record["phase"]) == ("failed", "build")
    assert record["error"] == expected.__name__
    assert record["failure_stop"] == "confirmed"
    assert record["recovery_snapshot"] == "complete"
    assert not (repo / "backup/last_deploy.json").exists()
    timed = subprocess.run(
        [sys.executable, str(repo / "scripts/backup_db.py")], cwd=repo, env=env,
        text=True, capture_output=True, timeout=10,
    )
    assert timed.returncode == 0, timed.stdout + timed.stderr
    env.pop("FAIL_DOCKER", None)
    (gate / "release").touch()
    retry = run_script(repo, env, "rollback.sh", TAG)
    assert retry.returncode == 0, retry.stdout + retry.stderr
    assert json.loads(Path(record["manifest"]).read_text())["status"] == "failed"


def test_stale_live_wal_and_shm_are_quarantined_before_database_replacement(rollback_env):
    repo, env = rollback_env
    env["CHECKOUT_SIDECARS"] = "1"
    result = run_script(repo, env, "rollback.sh", TAG)
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads((repo / "backup/last_rollback.json").read_text())
    sidecars = Path(record["manifest"]).parent / "sidecars"
    for suffix in ("-wal", "-shm"):
        assert (sidecars / ("console.db" + suffix)).read_bytes() == ("old" + suffix).encode()
    assert db_values(repo / "console/backend/data/console.db") == ["target"]


def test_backup_with_pending_wal_is_not_accepted_as_a_snapshot(rollback_env):
    repo, env = rollback_env
    path = repo / "backup" / f"console.db.{TAG}"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("INSERT INTO samples VALUES ('only-in-wal')")
    conn.commit()
    try:
        result = run_script(repo, env, "rollback.sh", TAG)
    finally:
        conn.close()
    assert result.returncode != 0
    assert not [c for c in commands(env) if c["kind"] == "docker"]
    assert db_values(repo / "console/backend/data/console.db") == ["current"]


@pytest.mark.parametrize("variable", ["WRONG_RUNNING_COMMIT", "WRONG_RUNNING_SCHEMA"])
def test_healthy_container_still_requires_correct_commit_and_schema(rollback_env, variable):
    repo, env = rollback_env
    env[variable] = "incorrect"
    result = run_script(repo, env, "rollback.sh", TAG, "--health-timeout", "0.01", "--poll-interval", "0.01")
    assert result.returncode != 0
    record = json.loads((repo / "backup/last_rollback.json").read_text())
    assert (record["status"], record["phase"]) == ("failed", "acceptance")
    assert not (repo / "backup/last_deploy.json").exists()


def test_restore_replace_failure_keeps_source_and_original_recovery_snapshot(rollback_env, monkeypatch):
    repo, env = rollback_env
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    real_replace = runtime.os.replace

    def failure(source, target):
        if Path(target) == repo / "console/backend/data/console.db":
            raise OSError("injected replacement failure")
        return real_replace(source, target)

    monkeypatch.setattr(runtime.os, "replace", failure)
    with pytest.raises(OSError, match="injected replacement"):
        runtime.rollback(repo, TAG)
    assert db_values(repo / "console/backend/data/console.db") == ["current"]
    assert db_values(repo / "backup" / f"console.db.{TAG}") == ["target"]
    record = json.loads((repo / "backup/last_rollback.json").read_text())
    assert (record["phase"], record["recovery_snapshot"]) == ("restore:console.db", "complete")
    assert db_values(repo / "backup" / f"console.db.{record['recovery_tag']}") == ["current"]


def test_result_publication_failure_is_reported_and_preserves_prior_success(rollback_env, monkeypatch):
    repo, env = rollback_env
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    previous = '{"commit":"previous-success"}'
    (repo / "backup/last_deploy.json").write_text(previous)
    real_json = runtime.atomic_json

    def failure(path, value):
        if Path(path).name == "last_deploy.json":
            raise OSError("injected marker publication failure")
        return real_json(path, value)

    monkeypatch.setattr(runtime, "atomic_json", failure)
    with pytest.raises(OSError, match="marker publication"):
        runtime.rollback(repo, TAG)
    assert (repo / "backup/last_deploy.json").read_text() == previous
    record = json.loads((repo / "backup/last_rollback.json").read_text())
    assert (record["status"], record["phase"]) == ("failed", "record")


def test_sigkill_during_restore_leaves_phase_snapshot_and_unlocked_retry(rollback_env):
    repo, env = rollback_env
    helper = repo / "scripts/backup_runtime.py"
    text = helper.read_text()
    text = text.replace("        os.replace(temp, target)\n", "        os.replace(temp, target)\n        if target.name == 'console.db':\n            os.kill(os.getpid(), signal.SIGKILL)\n", 1)
    helper.write_text(text)
    git(repo, "add", "scripts/backup_runtime.py")
    git(repo, "commit", "-m", "inject abrupt restore interruption")
    (repo / "backup" / f"panels.yaml.{TAG}").write_text("- name: restored-panel\n")

    result = run_script(repo, env, "rollback.sh", TAG)

    assert result.returncode == -9
    record = json.loads((repo / "backup/last_rollback.json").read_text())
    assert (record["status"], record["phase"]) == ("running", "restore:console.db")
    assert record["recovery_snapshot"] == "complete"
    assert db_values(repo / "backup" / f"console.db.{record['recovery_tag']}") == ["current"]
    assert db_values(repo / "console/backend/data/console.db") == ["target"]
    assert (repo / "console/panels.yaml").read_text() == FIXTURE_PANELS  # a visibly partial restore
    with backup_db.operation_lock(repo / "console/backend/data/console.db"):
        pass
    retry = run_script(repo, env, "rollback.sh", TAG)
    assert retry.returncode == 0, retry.stdout + retry.stderr
    assert (repo / "console/panels.yaml").read_text() == "- name: restored-panel\n"
    assert json.loads(Path(record["manifest"]).read_text())["phase"] == "restore:console.db"


def test_restore_keeps_runtime_write_permissions_when_archive_is_readonly(rollback_env):
    repo, env = rollback_env
    dest = repo / "console/backend/data/console.db"
    previous_mode = dest.stat().st_mode & 0o777
    (repo / "backup" / f"console.db.{TAG}").chmod(0o400)
    result = run_script(repo, env, "rollback.sh", TAG)
    assert result.returncode == 0, result.stdout + result.stderr
    assert dest.stat().st_mode & 0o777 == previous_mode


def legacy_target(repo):
    # Exercise the real pre-lock implementation, not a test stub claiming to be old.
    old_backup = git(SOURCE, "show", "50d6c3e:scripts/backup_db.py")
    current = (repo / "scripts/backup_db.py").read_text()
    (repo / "scripts/backup_db.py").write_text(old_backup + "\n")
    git(repo, "add", "scripts/backup_db.py")
    git(repo, "commit", "-m", "actual legacy timed entry")
    old_sha = git(repo, "rev-parse", "HEAD")
    (repo / "scripts/backup_db.py").write_text(current)
    git(repo, "add", "scripts/backup_db.py")
    git(repo, "commit", "-m", "restore current timed entry")
    (repo / "backup" / f"gitsha.{TAG}").write_text(old_sha + "\n")
    return old_sha


def test_legacy_cron_cannot_bypass_lock_after_checkout(rollback_env, tmp_path):
    repo, env = rollback_env
    legacy_target(repo)
    env["CRONTAB_TEXT"] = f"10 4 * * * cd {repo} && python3 scripts/backup_db.py >> /tmp/fixture-backup.log 2>&1"
    env["CONSOLE_BACKUP_ROOT"] = str(tmp_path / "timed")
    env["RUN_TIMED_AFTER_CHECKOUT"] = "1"

    result = run_script(repo, env, "rollback.sh", TAG)

    launched = [c for c in commands(env) if c["kind"] == "timed-after-checkout"]
    assert result.returncode != 0, f"legacy timed calls while new lock is held: {launched}"
    assert "cron" in result.stderr
    assert not [c for c in commands(env) if c["kind"] == "docker"]
    assert not launched
    assert not (tmp_path / "timed").exists()


def publish_for_test(repo, env):
    result = subprocess.run(
        [sys.executable, str(repo / "scripts/backup_runtime.py"), "publish-runtime", str(repo)],
        cwd=repo, env=env, text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_stable_cron_entry_stays_locked_when_checkout_restores_legacy_script(rollback_env, tmp_path):
    repo, env = rollback_env
    target_sha = legacy_target(repo)
    info = publish_for_test(repo, env)
    command = [sys.executable, info["entry"], "timed", str(repo)]
    env["CRONTAB_TEXT"] = "10 4 * * * " + shlex.join(command)
    env["CONSOLE_BACKUP_ROOT"] = str(tmp_path / "timed")
    env["TIMED_COMMAND"] = json.dumps(command)
    env["RUN_TIMED_AFTER_CHECKOUT"] = "1"

    result = run_script(repo, env, "rollback.sh", TAG)

    assert result.returncode == 0, result.stdout + result.stderr
    assert git(repo, "rev-parse", "HEAD") == target_sha
    assert "def operation_lock(" not in (repo / "scripts/backup_db.py").read_text()
    launched = [c for c in commands(env) if c["kind"] == "timed-after-checkout"]
    assert len(launched) == 1
    assert launched[0]["returncode"] == 1
    assert not (tmp_path / "timed").exists()
    assert db_values(repo / "console/backend/data/console.db") == ["target"]
    # Once rollback releases the lock, the exact same stable cron command works.
    timed = subprocess.run(command, cwd=repo, env=env, text=True, capture_output=True, timeout=10)
    assert timed.returncode == 0, timed.stdout + timed.stderr
    assert len(list((tmp_path / "timed").glob("console.db.*"))) == 1


@pytest.mark.parametrize("local_entry", ["backup_db.py", "backup_runtime.py"])
def test_mixed_stable_cron_cannot_fall_back_to_checkout_local_backup(rollback_env, tmp_path, local_entry):
    repo, env = rollback_env
    legacy_target(repo)
    info = publish_for_test(repo, env)
    stable = [sys.executable, info["entry"], "timed", str(repo)]
    fallback = [sys.executable, "scripts/" + local_entry]
    if local_entry == "backup_runtime.py":
        fallback += ["timed", str(repo)]
    command = shlex.join(stable) + " || " + shlex.join(fallback)
    env["CRONTAB_TEXT"] = "10 4 * * * " + command
    env["TIMED_COMMAND"] = json.dumps(["/bin/sh", "-c", command])
    env["RUN_TIMED_AFTER_CHECKOUT"] = "1"
    env["CONSOLE_BACKUP_ROOT"] = str(tmp_path / "timed")

    result = run_script(repo, env, "rollback.sh", TAG)

    launched = [c for c in commands(env) if c["kind"] == "timed-after-checkout"]
    assert result.returncode != 0, f"mixed cron launched during rollback: {launched}"
    assert "cron" in result.stderr
    assert not launched
    assert not [c for c in commands(env) if c["kind"] == "docker"]
    assert not (tmp_path / "timed").exists()
    assert db_values(repo / "console/backend/data/console.db") == ["current"]


def test_compound_stable_cron_with_unknown_command_is_refused(rollback_env):
    repo, env = rollback_env
    info = publish_for_test(repo, env)
    stable = shlex.join([sys.executable, info["entry"], "timed", str(repo)])
    env["CRONTAB_TEXT"] = "10 4 * * * " + stable + " || sh unknown-backup-wrapper.sh"
    result = run_script(repo, env, "rollback.sh", TAG)
    assert result.returncode != 0
    assert "cron" in result.stderr
    assert not [c for c in commands(env) if c["kind"] == "docker"]


def test_runtime_refresh_updates_future_timer_behavior_without_changing_cron(rollback_env, tmp_path):
    repo, env = rollback_env
    first = publish_for_test(repo, env)
    command = [sys.executable, first["entry"], "timed", str(repo)]
    env["CONSOLE_BACKUP_ROOT"] = str(tmp_path / "timed")
    path = repo / "scripts/backup_db.py"
    path.write_text(path.read_text() + '''
_previous_main = main
def main():
    result = _previous_main()
    if result == 0:
        (BACKUP_ROOT / "new-success-marker").write_text("published revision two")
    return result
''')
    second = publish_for_test(repo, env)
    assert first["entry"] == second["entry"]
    assert first["version"] != second["version"]
    assert (Path(first["directory"]) / first["version"] / "manifest.json").is_file()
    assert (Path(second["directory"]) / "current").resolve().name == second["version"]
    result = subprocess.run(command, cwd=repo, env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "timed/new-success-marker").read_text() == "published revision two"


def test_cron_migration_is_readonly_dry_run_and_does_not_leak_unrelated_jobs(rollback_env):
    repo, env = rollback_env
    marker = "UNRELATED_PRIVATE_CRON_VALUE"
    env["CRONTAB_TEXT"] = f"10 4 * * * cd {repo} && python3 scripts/backup_db.py\n0 * * * * echo {marker}"
    result = subprocess.run(
        [sys.executable, str(repo / "scripts/backup_runtime.py"), "cron-migration", str(repo), "--dry-run"],
        cwd=repo, env=env, text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    plan = json.loads(result.stdout)
    assert plan["dry_run"] is True and plan["installs_cron"] is False
    assert plan["affected_jobs"] == 1
    assert plan["proposed_jobs"][0].startswith("10 4 * * * ")
    assert "/.runtime/entry.py" in plan["proposed_jobs"][0]
    assert marker not in result.stdout + result.stderr
    assert not (repo / "backup/.runtime").exists()
    assert [c["args"] for c in commands(env) if c["kind"] == "crontab"] == [["-l"]]


def test_emitted_stable_cron_with_log_redirection_passes_guard(rollback_env):
    repo, env = rollback_env
    env["CRONTAB_TEXT"] = f"10 4 * * * cd {repo} && python3 scripts/backup_db.py"
    result = subprocess.run(
        [sys.executable, str(repo / "scripts/backup_runtime.py"), "cron-migration", str(repo), "--dry-run"],
        cwd=repo, env=env, text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    plan = json.loads(result.stdout)
    publish_for_test(repo, env)
    env["CRONTAB_TEXT"] = plan["proposed_jobs"][0]
    assert " >> " in env["CRONTAB_TEXT"] and " 2>&1" in env["CRONTAB_TEXT"]
    rollback = run_script(repo, env, "rollback.sh", TAG)
    assert rollback.returncode == 0, rollback.stdout + rollback.stderr


def test_cron_inspection_failure_refuses_to_stop_or_restore(rollback_env):
    repo, env = rollback_env
    env["FAIL_CRONTAB"] = "1"
    result = run_script(repo, env, "rollback.sh", TAG)
    assert result.returncode != 0
    assert "cron" in result.stderr
    assert not [c for c in commands(env) if c["kind"] == "docker"]
    assert db_values(repo / "console/backend/data/console.db") == ["current"]


def test_tampered_stable_runtime_is_rejected_before_stop(rollback_env):
    repo, env = rollback_env
    info = publish_for_test(repo, env)
    command = [sys.executable, info["entry"], "timed", str(repo)]
    env["CRONTAB_TEXT"] = "10 4 * * * " + shlex.join(command)
    helper = Path(info["directory"]) / info["version"] / "controller/scripts/backup_db.py"
    helper.write_text(helper.read_text() + "\n# unexpected mutation\n")
    result = run_script(repo, env, "rollback.sh", TAG)
    assert result.returncode != 0
    assert "摘要" in result.stderr
    assert not [c for c in commands(env) if c["kind"] == "docker"]


def test_migrated_cron_does_not_hide_an_old_backup_process_still_in_flight(rollback_env):
    repo, env = rollback_env
    info = publish_for_test(repo, env)
    env["CRONTAB_TEXT"] = "10 4 * * * " + shlex.join([sys.executable, info["entry"], "timed", str(repo)])
    env["RUNNING_LEGACY_BACKUP"] = "1"
    result = run_script(repo, env, "rollback.sh", TAG)
    assert result.returncode != 0
    assert "PID 12345" in result.stderr
    assert not [c for c in commands(env) if c["kind"] == "docker"]
    assert db_values(repo / "console/backend/data/console.db") == ["current"]
