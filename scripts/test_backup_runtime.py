"""Recoverable retention tests, exclusively on disposable local files."""
import errno
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backup_runtime as runtime  # noqa: E402
from test_deploy_safety import MEMBERS, TAG, seed_group  # noqa: E402


def test_pg_rollback_reads_latest_declared_migration_without_execution(tmp_path):
    import pg_migrations
    root = Path(__file__).resolve().parents[1]
    path = None
    for candidate in (
        root / "kernel/backend/pg_migrations.py",
        root / "console/backend/pg_migrations.py",
    ):
        if not candidate.is_file():
            continue
        head = candidate.read_text(encoding="utf-8", errors="ignore")[:240]
        if "Thin relocating shim" in head:
            continue
        path = candidate
        break
    assert path is not None, "pg_migrations implementation missing"
    source = path.read_text()
    current = pg_migrations.latest_schema_version()
    assert runtime.pg_schema_version_from_source(source) == current
    assert runtime.pg_schema_version_from_source(
        source + f'\nPG_MIGRATIONS += (({current + 1}, "next", ("SELECT 1",)),)\n'
    ) == current + 1
    sentinel = tmp_path / "must-not-exist"
    assert runtime.pg_schema_version_from_source(
        source + f'\nopen({str(sentinel)!r}, "w").write("unsafe")\n'
    ) == current
    assert not sentinel.exists()
    with pytest.raises(ValueError):
        runtime.pg_schema_version_from_source(source + '\nPG_MIGRATIONS = build()\n')


def test_pinned_runtime_includes_pay_snapshot_implementation():
    sources = runtime.controller_sources()
    assert "scripts/backup_pay.py" in sources
    assert "scripts/backup_attachments.py" in sources
    assert "console/backend/pg_contracts.py" in sources
    assert "console/backend/backup_contracts.py" in sources
    assert "console/backend/ledger_batch_contracts.py" in sources
    assert "console/backend/pg_migration_contracts.py" in sources
    assert "console/backend/contract_errors.py" in sources
    assert "console/backend/composition.py" in sources
    assert "console/backend/pg_backup.py" in sources


def test_preserved_controller_executes_without_source_repository(tmp_path):
    """真实控制器副本独立加载身份/组件/附件与割接恢复依赖。"""
    run_dir = tmp_path / "run"
    runtime.preserve_controller(run_dir)
    controller = run_dir / "controller"
    plugins = tmp_path / "public-plugins"
    plugins.mkdir()
    (plugins / "contract.json").write_text("{}\n")
    (plugins / "composition.json").write_text('{"plugins": []}\n')
    config = tmp_path / "config"
    config.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    document = {
        "schema_version": 1, "profile": "public",
        "roots": {"plugins": str(plugins), "config": str(config), "data": str(data),
                  "static": str(tmp_path / "static"), "backup": str(tmp_path / "backup")},
        "host": {}, "alert_machines": {},
    }
    composition_file = tmp_path / "composition.json"
    composition_file.write_text(json.dumps(document))
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("CONSOLE_", "OPS_BACKUP_", "PYTHON"))}
    env.update(CONSOLE_COMPOSITION_FILE=str(composition_file), CONSOLE_INSTANCE_PROFILE="public",
               CONSOLE_INSTANCE_ROLE="test", CONSOLE_STORAGE_ENGINE="sqlite")
    code = '''
import importlib
import json
from pathlib import Path
import sys
root = Path(sys.argv[1])
sys.path[:0] = [str(root / "scripts"), str(root / "console/backend")]
modules = [importlib.import_module(name) for name in (
    "backup_runtime", "backup_db", "backup_identity", "composition_identity",
    "component_migrations", "kernel_baseline", "backup_attachments", "plugin_imports",
    "cutover_prepare", "cutover_backup_restore", "cutover_writers", "pg_migration_lib",
)]
assert all(Path(module.__file__).resolve().is_relative_to(root) for module in modules)
import backup_attachments
import backup_identity
assert backup_attachments.composition_attachment_name() is None
assert not any(name == "plugins.ledger" or name.startswith("plugins.ledger.") for name in sys.modules)
identity = {"kernel": {"version": "synthetic", "digest": "a" * 64}, "plugins": [],
            "composition_digest": "b" * 64}
assert backup_identity.composition_block(identity)["plugins"] == []
print(json.dumps({"modules": len(modules), "public_attachments": "none"}))
'''
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(controller)], cwd=tmp_path,
        env=env, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"modules": 12, "public_attachments": "none"}
    for entry in ("backup_runtime.py", "cutover_prepare.py", "cutover_backup_restore.py", "cutover_writers.py"):
        result = subprocess.run(
            [sys.executable, str(controller / "scripts" / entry), "--help"],
            cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_publish_runtime_reuses_verified_deploy_lock(tmp_path):
    data = tmp_path / "console/backend/data"
    data.mkdir(parents=True)
    with runtime.backup_db.operation_lock(data / "console.db") as fd:
        info = runtime.publish_runtime(tmp_path, lock_fd=fd)
        assert runtime.validate_runtime(tmp_path)["version"] == info["version"]
        # 发布不能释放上层锁，否则 timer 可与未完成部署并行。
        with pytest.raises(RuntimeError, match="互斥锁"):
            with runtime.backup_db.operation_lock(data / "console.db"):
                pass


def test_publish_runtime_rejects_other_data_lock_before_writing(tmp_path):
    data = tmp_path / "console/backend/data"
    data.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    with runtime.backup_db.operation_lock(data / "console.db"):
        with runtime.backup_db.operation_lock(other / "console.db") as fd:
            with pytest.raises(RuntimeError, match="不匹配"):
                runtime.publish_runtime(tmp_path, lock_fd=fd)
    assert not (tmp_path / "backup/.runtime").exists()


def test_pg_retention_moves_pay_companion_with_dump_and_manifest(tmp_path):
    root = tmp_path / "backup"
    root.mkdir()
    names = [f"console.dump.{TAG}", f"console.dump.{TAG}.manifest.json",
             f"console.dump.{TAG}.pay.tar"]
    for name in names:
        (root / name).write_text(name)
    assert runtime.retire_groups(tmp_path, keep=0) == 1
    target = tmp_path / ".trash" / f"deploy-retention-{TAG}"
    assert all((target / name).read_text() == name for name in names)
    assert not any((root / name).exists() for name in names)


def test_guard_backup_cron_accepts_explicit_postgres_env(tmp_path, monkeypatch):
    entry = tmp_path / "backup/.runtime/entry.py"
    command = ["env", "CONSOLE_STORAGE_ENGINE=postgres", "python3", str(entry),
               "timed", str(tmp_path)]
    monkeypatch.setattr(runtime, "read_crontab", lambda root: "0 2 * * * " + shlex.join(command))
    checked = []
    monkeypatch.setattr(runtime, "validate_runtime", lambda root: checked.append(root))
    runtime.guard_backup_cron(tmp_path)
    assert checked == [tmp_path]


@pytest.mark.parametrize("prefix", ["env CONSOLE_STORAGE_ENGINE=postgres sh -c", "env PATH=/tmp",
                                    "env -i", "env CONSOLE_STORAGE_ENGINE=wrong"])
def test_guard_backup_cron_rejects_unknown_env_prefix(tmp_path, monkeypatch, prefix):
    entry = tmp_path / "backup/.runtime/entry.py"
    monkeypatch.setattr(runtime, "read_crontab", lambda root:
                        f"0 2 * * * {prefix} python3 {entry} timed {root}")
    with pytest.raises(RuntimeError, match="未识别"):
        runtime.guard_backup_cron(tmp_path)


@pytest.mark.parametrize("tag", [
    "20260907-231123", "20240229-000000", "20261231-235959",
])
def test_normal_tag_accepts_real_dates(tag):
    assert runtime.normal_tag(tag)


@pytest.mark.parametrize("tag", [
    "preroll-20260907-231123", "20260229-000000", "20261301-000000",
    "20260907-240000", "20260907-236000", "20260907-235960",
    "2026097-231123", "20260907", "20260907-231123.tmp-x", "../../other",
])
def test_special_or_invalid_tags_never_enter_quota(tag):
    assert not runtime.normal_tag(tag)


def test_retention_finds_groups_without_gitsha_and_records_missing_members(tmp_path):
    seed_group(tmp_path, TAG, missing=("console.db", "gitsha", "settings.yaml"))
    assert runtime.retire_groups(tmp_path, keep=0) == 1
    target = tmp_path / ".trash" / f"deploy-retention-{TAG}"
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["status"] == "complete"
    assert manifest["members"]["console.db"]["status"] == "missing"
    assert manifest["members"]["gitsha"]["status"] == "missing"
    assert (target / f"panels.yaml.{TAG}").is_file()
    assert runtime.retire_groups(tmp_path, keep=0) == 0


def test_existing_archive_without_our_manifest_is_not_overwritten(tmp_path):
    seed_group(tmp_path, TAG)
    target = tmp_path / ".trash" / f"deploy-retention-{TAG}"
    target.mkdir(parents=True)
    conflict = target / f"console.db.{TAG}"
    conflict.write_bytes(b"keep-this")
    with pytest.raises(FileExistsError):
        runtime.retire_groups(tmp_path, keep=0)
    assert conflict.read_bytes() == b"keep-this"
    assert len(list((tmp_path / "backup").iterdir())) == 6


def test_partial_move_failure_resumes_and_leaves_old_trash(tmp_path, monkeypatch):
    seed_group(tmp_path, TAG)
    old_trash = tmp_path / ".trash/older/manual.txt"
    old_trash.parent.mkdir(parents=True)
    old_trash.write_text("keep")
    real_move = runtime.move_preserving
    calls = []

    def fail_second(source, target, digest):
        calls.append(source)
        if len(calls) == 2:
            raise OSError("injected move failure")
        return real_move(source, target, digest)

    monkeypatch.setattr(runtime, "move_preserving", fail_second)
    with pytest.raises(OSError):
        runtime.retire_groups(tmp_path, keep=0)
    target = tmp_path / ".trash" / f"deploy-retention-{TAG}"
    manifest_path = target / "manifest.json"
    assert json.loads(manifest_path.read_text())["status"] == "failed"
    assert (target / f"console.db.{TAG}").exists()
    assert (tmp_path / "backup" / f"panels.yaml.{TAG}").exists()

    monkeypatch.setattr(runtime, "move_preserving", real_move)
    assert runtime.retire_groups(tmp_path, keep=10) == 1  # unfinished group is resumed even below quota
    assert json.loads(manifest_path.read_text())["status"] == "complete"
    assert len(list((tmp_path / "backup").iterdir())) == 0
    assert old_trash.read_text() == "keep"
    for name in MEMBERS:
        assert (target / f"{name}.{TAG}").read_text() == f"{name} for {TAG}\n"


def test_interruption_after_move_before_manifest_is_reentrant(tmp_path, monkeypatch):
    seed_group(tmp_path, TAG)
    real_json = runtime.atomic_json
    calls = []

    def interrupted(path, value):
        calls.append(path)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real_json(path, value)

    monkeypatch.setattr(runtime, "atomic_json", interrupted)
    with pytest.raises(KeyboardInterrupt):
        runtime.retire_groups(tmp_path, keep=0)
    target = tmp_path / ".trash" / f"deploy-retention-{TAG}"
    assert (target / f"console.db.{TAG}").exists()
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["status"] == "moving"
    assert manifest["members"]["console.db"]["status"] == "pending"
    monkeypatch.setattr(runtime, "atomic_json", real_json)
    assert runtime.retire_groups(tmp_path, keep=10) == 1
    assert runtime.retire_groups(tmp_path, keep=10) == 0


def test_resume_refuses_a_changed_destination(tmp_path, monkeypatch):
    seed_group(tmp_path, TAG)
    real_move = runtime.move_preserving

    def interrupted(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime, "move_preserving", interrupted)
    with pytest.raises(KeyboardInterrupt):
        runtime.retire_groups(tmp_path, keep=0)
    target = tmp_path / ".trash" / f"deploy-retention-{TAG}"
    conflict = target / f"console.db.{TAG}"
    conflict.write_text("unrelated existing backup")
    monkeypatch.setattr(runtime, "move_preserving", real_move)
    with pytest.raises(FileExistsError):
        runtime.retire_groups(tmp_path, keep=0)
    assert conflict.read_text() == "unrelated existing backup"
    assert (tmp_path / "backup" / f"console.db.{TAG}").exists()


def test_cross_volume_move_publishes_verified_copy_before_source_retirement(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"recoverable backup")
    target = tmp_path / "target"
    real_link = runtime.os.link

    def cross_device(src, dst):
        if Path(src) == source:
            raise OSError(errno.EXDEV, "injected cross volume")
        return real_link(src, dst)

    monkeypatch.setattr(runtime.os, "link", cross_device)
    runtime.move_preserving(source, target, runtime.file_digest(source))
    assert target.read_bytes() == b"recoverable backup"
    assert not source.exists()
    assert not list(tmp_path.glob("*.tmp-*"))


def test_published_duplicate_after_unlink_failure_can_resume(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"recoverable backup")
    target = tmp_path / "target"
    digest = runtime.file_digest(source)
    real_unlink = Path.unlink

    def fail_unlink(path, *args, **kwargs):
        if path == source:
            raise OSError("injected source retirement failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(OSError):
        runtime.move_preserving(source, target, digest)
    assert source.read_bytes() == target.read_bytes()
    monkeypatch.setattr(Path, "unlink", real_unlink)
    runtime.move_preserving(source, target, digest)
    assert not source.exists()
    assert target.read_bytes() == b"recoverable backup"


@pytest.mark.parametrize("kind", ["missing", "directory", "relative", "empty"])
def test_private_panels_invalid_path_rejected(tmp_path, kind):
    path = tmp_path / "private-panels.yaml"
    if kind == "directory":
        path.mkdir()
    value = {"relative": "private-panels.yaml", "empty": ""}.get(kind, str(path))
    with pytest.raises(ValueError, match="CONSOLE_PANELS_HOST_PATH"):
        runtime.deployment_environment(tmp_path, {
            "CONSOLE_STORAGE_ENGINE": "postgres", "CONSOLE_PANELS_HOST_PATH": value})


def test_private_panels_env_resolves_existing_symlink(tmp_path):
    source = tmp_path / "private panels.yaml"
    source.write_text("[]\n")
    link = tmp_path / "panels-link.yaml"
    link.symlink_to(source)
    (tmp_path / ".env").write_text(
        "CONSOLE_STORAGE_ENGINE=postgres\nCONSOLE_PANELS_HOST_PATH=" + shlex.quote(str(link)) + "\n")
    configured = runtime.deployment_environment(tmp_path, {})
    assert configured["CONSOLE_PANELS_HOST_PATH"] == str(source.resolve())
    assert runtime.deployment_environment(tmp_path, configured) == configured


def test_private_panels_different_files_still_conflict(tmp_path):
    first, second = tmp_path / "first.yaml", tmp_path / "second.yaml"
    first.write_text("[]\n")
    second.write_text("[]\n")
    (tmp_path / ".env").write_text(f"CONSOLE_PANELS_HOST_PATH={first}\n")
    with pytest.raises(ValueError, match="CONSOLE_PANELS_HOST_PATH.*冲突"):
        runtime.deployment_environment(tmp_path, {
            "CONSOLE_STORAGE_ENGINE": "postgres", "CONSOLE_PANELS_HOST_PATH": str(second)})


# 2026-09-19 线上面板清空事故的前置门禁：Git 侧私有化（console/panels.yaml 清成 []）
# 而宿主侧 CONSOLE_PANELS_HOST_PATH 未接线时，compose 静默回退到空文件，线上零面板
# 但容器全 healthy。这里锁死「空 tracked panels 必须配一个非空的私有路径」。
@pytest.mark.parametrize("body", ["[]\n", "[]", "", "  \n"])
def test_empty_tracked_panels_without_private_path_refused(tmp_path, body):
    (tmp_path / "console").mkdir()
    (tmp_path / "console" / "panels.yaml").write_text(body)
    with pytest.raises(ValueError, match="未配置 CONSOLE_PANELS_HOST_PATH"):
        runtime.deployment_environment(tmp_path, {"CONSOLE_STORAGE_ENGINE": "postgres"})


def test_empty_tracked_panels_with_private_path_allowed(tmp_path):
    (tmp_path / "console").mkdir()
    (tmp_path / "console" / "panels.yaml").write_text("[]\n")
    private = tmp_path / "private-panels.yaml"
    private.write_text("- id: 1\n  name: mihomo\n")
    configured = runtime.deployment_environment(tmp_path, {
        "CONSOLE_STORAGE_ENGINE": "postgres", "CONSOLE_PANELS_HOST_PATH": str(private)})
    assert configured["CONSOLE_PANELS_HOST_PATH"] == str(private.resolve())


@pytest.mark.parametrize("body", ["[]\n", "[]", "", "  \n"])
def test_empty_private_panels_refused_when_tracked_also_empty(tmp_path, body):
    # tracked 已按私有化清空时，私有文件是容器唯一的面板来源；它为空等于线上零面板。
    (tmp_path / "console").mkdir()
    (tmp_path / "console" / "panels.yaml").write_text("[]\n")
    private = tmp_path / "private-panels.yaml"
    private.write_text(body)
    with pytest.raises(ValueError, match="指向的文件是空列表"):
        runtime.deployment_environment(tmp_path, {
            "CONSOLE_STORAGE_ENGINE": "postgres", "CONSOLE_PANELS_HOST_PATH": str(private)})


def test_empty_private_panels_tolerated_when_tracked_has_content(tmp_path):
    # 兼容模式下容器读的是 tracked 文件，私有文件为空不影响线上面板，不该拦。
    (tmp_path / "console").mkdir()
    (tmp_path / "console" / "panels.yaml").write_text("- id: 1\n  name: mihomo\n")
    private = tmp_path / "private-panels.yaml"
    private.write_text("[]\n")
    configured = runtime.deployment_environment(tmp_path, {
        "CONSOLE_STORAGE_ENGINE": "postgres", "CONSOLE_PANELS_HOST_PATH": str(private)})
    assert configured["CONSOLE_PANELS_HOST_PATH"] == str(private.resolve())


def test_nonempty_tracked_panels_needs_no_private_path(tmp_path):
    (tmp_path / "console").mkdir()
    (tmp_path / "console" / "panels.yaml").write_text("- id: 1\n  name: mihomo\n")
    configured = runtime.deployment_environment(tmp_path, {"CONSOLE_STORAGE_ENGINE": "postgres"})
    assert "CONSOLE_PANELS_HOST_PATH" not in configured


def test_absent_tracked_panels_does_not_trigger_private_path_rule(tmp_path):
    # 缺文件由 snapshot_group 的 FileNotFoundError 负责，不在本规则里重复报错。
    configured = runtime.deployment_environment(tmp_path, {"CONSOLE_STORAGE_ENGINE": "postgres"})
    assert "CONSOLE_PANELS_HOST_PATH" not in configured
