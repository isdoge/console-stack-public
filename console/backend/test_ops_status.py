# -*- coding: utf-8 -*-
"""ops_status：字段可得 / 字段缺失（null）两种路径。"""
import datetime
import json
import pathlib
import sqlite3
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import effective_config as ec  # noqa: E402
import ops_status  # noqa: E402
import plugin_contributions  # noqa: E402
import plugin_imports  # noqa: E402
import plugin_runtime  # noqa: E402


@pytest.fixture(autouse=True)
def _feed_contribution(monkeypatch):
    feed = plugin_imports.load_module('plugins.feed.backend.contributions')
    monkeypatch.setattr(plugin_contributions, '_registry', {('ops_status', 'feed_last_fetched'): ('feed', feed.last_fetched)})
    monkeypatch.setattr(plugin_runtime, 'is_available', lambda pid: True)


@pytest.fixture(autouse=True)
def _isolate_probe_state(monkeypatch):
    """隔离 `effective_config.PROBE_STATE`（同 test_effective_config 的 isolated）。

    没有它这个文件在**整点/半点前后跑就会随机红**：import app 时启动的
    `_bg_machine_probes` 守护线程一到调度节点就 `mark_probe_run()`，把这个
    模块级内存时间戳从 None 改成实际时刻，于是「字段缺失时全为 null」的断言挂掉。
    2026-09-07 实测复现（02:30:00 那一轮）。
    """
    monkeypatch.setattr(
        ec, "PROBE_STATE", {"boot_utc": "2026-09-06T00:00:00Z", "last_run_utc": None}
    )


@pytest.fixture
def empty_db(tmp_path):
    db_path = tmp_path / "console.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE versions (
            svc_name TEXT PRIMARY KEY,
            last_checked TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE feed_cache (
            id TEXT PRIMARY KEY,
            fetched_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()
    return str(db_path)


@pytest.fixture
def seeded_db(empty_db):
    conn = sqlite3.connect(empty_db)
    conn.executemany(
        "INSERT INTO versions (svc_name, last_checked) VALUES (?, ?)",
        [
            ("a", "2026-08-01 10:00:00"),
            ("b", "2026-08-03 12:30:00"),
            ("c", ""),
        ],
    )
    conn.executemany(
        "INSERT INTO feed_cache (id, fetched_at) VALUES (?, ?)",
        [
            ("1", "2026-08-02 08:00:00"),
            ("2", "2026-08-03 09:15:00"),
        ],
    )
    conn.commit()
    conn.close()
    return empty_db


def test_fields_available_when_data_exists(seeded_db, tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_MONITORING_BUSINESS_SOURCE", "console_pg")
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("RUNNING_COMMIT", raising=False)
    monkeypatch.setenv("GIT_COMMIT", "abc123def")

    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "gitsha.20260801-120000").write_text("old\n", encoding="utf-8")
    (backup / "gitsha.20260803-090233").write_text("new\n", encoding="utf-8")

    # repo without .git — env should still win
    result = ops_status.get_ops_status(
        db_path=seeded_db,
        backup_dir=str(backup),
        repo_dir=str(tmp_path / "norepo"),
    )

    assert result["running_commit"] == "abc123def"
    assert result["versions_last_checked"] == "2026-08-03 12:30:00"
    assert result["feed_last_fetched"] == "2026-08-03 09:15:00"
    assert result["last_backup_tag"] == "20260803-090233"
    assert set(result.keys()) == {
        "running_commit",
        "versions_last_checked",
        "feed_last_fetched",
        "last_backup_tag",
        "last_deploy",
        # C10-a：守护线程时间戳，从未跑过为 None
        "last_probe_run",
        # F17：定时备份状态（该 fixture 的 backup 目录没有 timed/ → never）
        "timed_backup",
        # B6-3：监控业务源现值（flag 缺省 beszel）
        "monitoring_business_source",
    }
    assert result["timed_backup"]["state"] == "never"
    assert result["monitoring_business_source"] == "console_pg"


def test_identity_uses_configured_static_root(empty_db, tmp_path, monkeypatch):
    root = tmp_path / 'custom-static'
    checked = []
    monkeypatch.setenv('CONSOLE_STATIC_ROOT', str(root))
    monkeypatch.setattr(ops_status.composition_identity, 'verify_static', checked.append)
    ops_status.get_ops_status(db_path=empty_db, backup_dir=str(tmp_path), repo_dir=str(tmp_path))
    assert checked == [str(root)]


def test_last_backup_tag_ignores_preroll_rehearsal(tmp_path):
    """preroll- 演练备份（2026-07-30 遗留）字典序压过数字 tag，必须被过滤。"""
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "gitsha.preroll-20260730-030959").write_text("old\n", encoding="utf-8")
    (backup / "gitsha.20260906-061453").write_text("new\n", encoding="utf-8")
    assert ops_status._last_backup_tag(str(backup)) == "20260906-061453"


def test_last_backup_tag_all_preroll_returns_none(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "gitsha.preroll-20260730-030959").write_text("old\n", encoding="utf-8")
    assert ops_status._last_backup_tag(str(backup)) is None


def test_fields_null_when_missing(empty_db, tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("RUNNING_COMMIT", raising=False)
    monkeypatch.delenv("OPS_BACKUP_DIR", raising=False)

    missing_backup = tmp_path / "no-backup-here"
    norepo = tmp_path / "norepo"
    norepo.mkdir()

    result = ops_status.get_ops_status(
        db_path=empty_db,
        backup_dir=str(missing_backup),
        repo_dir=str(norepo),
    )

    assert result["running_commit"] is None
    assert result["versions_last_checked"] is None
    assert result["feed_last_fetched"] is None
    assert result["last_backup_tag"] is None
    assert result["last_deploy"] is None
    assert result["last_probe_run"] is None
    assert result["timed_backup"]["state"] == "never"


def test_running_commit_from_git(tmp_path, empty_db, monkeypatch):
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("RUNNING_COMMIT", raising=False)

    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess

    try:
        subprocess.check_call(
            ["git", "init"],
            cwd=str(repo),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.check_call(
            ["git", "config", "user.email", "t@example.com"],
            cwd=str(repo),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.check_call(
            ["git", "config", "user.name", "t"],
            cwd=str(repo),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (repo / "f.txt").write_text("x", encoding="utf-8")
        subprocess.check_call(
            ["git", "add", "f.txt"],
            cwd=str(repo),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.check_call(
            ["git", "commit", "-m", "init"],
            cwd=str(repo),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        sha = (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo))
            .decode()
            .strip()
        )
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git unavailable")

    result = ops_status.get_ops_status(
        db_path=empty_db,
        backup_dir=str(tmp_path / "missing"),
        repo_dir=str(repo),
    )
    assert result["running_commit"] == sha


def test_missing_tables_yield_null(tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("RUNNING_COMMIT", raising=False)
    db_path = tmp_path / "bare.db"
    sqlite3.connect(db_path).close()
    result = ops_status.get_ops_status(
        db_path=str(db_path),
        backup_dir=str(tmp_path / "missing"),
        repo_dir=str(tmp_path / "norepo"),
    )
    assert result["versions_last_checked"] is None
    assert result["feed_last_fetched"] is None
    assert result["running_commit"] is None
    assert result["last_backup_tag"] is None
    assert result["last_deploy"] is None


# ---- D0：GIT_COMMIT 注入 / backup 挂载点 / last_deploy.json ----


def _write_last_deploy(backup, payload):
    (backup / "last_deploy.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_last_deploy_whitelists_known_keys(empty_db, tmp_path, monkeypatch):
    """D0-3：只透出四个约定键，部署脚本将来多写的字段不原样外泄。"""
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("RUNNING_COMMIT", raising=False)
    backup = tmp_path / "backup"
    backup.mkdir()
    _write_last_deploy(
        backup,
        {
            "commit": "0123456789abcdef",
            "backup_tag": "20260906-214411",
            "deployed_at": "2026-09-06T21:44:11+08:00",
            "migrations_applied": 8,
            "operator": "should-be-dropped",
        },
    )

    result = ops_status.get_ops_status(
        db_path=empty_db, backup_dir=str(backup), repo_dir=str(tmp_path / "norepo")
    )

    assert result["last_deploy"] == {
        "commit": "0123456789abcdef",
        "backup_tag": "20260906-214411",
        "deployed_at": "2026-09-06T21:44:11+08:00",
        "migrations_applied": 8,
    }


def test_last_deploy_missing_keys_become_null(empty_db, tmp_path, monkeypatch):
    """部署时 DB 还没落迁移 → migrations_applied 缺失，报 null 而不是抹掉整段。"""
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("RUNNING_COMMIT", raising=False)
    backup = tmp_path / "backup"
    backup.mkdir()
    _write_last_deploy(backup, {"commit": "abc", "backup_tag": "t"})

    result = ops_status.get_ops_status(
        db_path=empty_db, backup_dir=str(backup), repo_dir=str(tmp_path / "norepo")
    )

    assert result["last_deploy"] == {
        "commit": "abc",
        "backup_tag": "t",
        "deployed_at": None,
        "migrations_applied": None,
    }


@pytest.mark.parametrize("body", ["{ not json", "[]", '"text"'])
def test_last_deploy_bad_payload_is_null(empty_db, tmp_path, monkeypatch, body):
    """坏 JSON / 非对象一律 None——半个运维状态卡好过一个假的。"""
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("RUNNING_COMMIT", raising=False)
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "last_deploy.json").write_text(body, encoding="utf-8")

    result = ops_status.get_ops_status(
        db_path=empty_db, backup_dir=str(backup), repo_dir=str(tmp_path / "norepo")
    )

    assert result["last_deploy"] is None


def test_backup_dir_prefers_container_mount(tmp_path, monkeypatch):
    """D0-2：容器内 /app/backup（BACKEND_DIR/backup）优先于仓库根 backup/。"""
    monkeypatch.delenv("OPS_BACKUP_DIR", raising=False)
    fake_backend = tmp_path / "app"
    (fake_backend / "backup").mkdir(parents=True)
    monkeypatch.setattr(ops_status.paths, "BACKEND_DIR", str(fake_backend))

    assert ops_status._resolve_backup_dir() == str(fake_backend / "backup")


def test_backup_dir_falls_back_to_repo_root(tmp_path, monkeypatch):
    """开发机直跑：没有挂载点时退到仓库根 backup/。"""
    monkeypatch.delenv("OPS_BACKUP_DIR", raising=False)
    fake_backend = tmp_path / "repo" / "console" / "backend"
    fake_backend.mkdir(parents=True)
    monkeypatch.setattr(ops_status.paths, "BACKEND_DIR", str(fake_backend))

    assert ops_status._resolve_backup_dir() == str(tmp_path / "repo" / "backup")


def test_backup_dir_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_BACKUP_DIR", str(tmp_path / "elsewhere"))
    assert ops_status._resolve_backup_dir() == str(tmp_path / "elsewhere")


def test_running_commit_prefers_injected_env(empty_db, tmp_path, monkeypatch):
    """D0-1：镜像里没有 .git，构建期注入的 GIT_COMMIT 是唯一可信来源。"""
    monkeypatch.delenv("RUNNING_COMMIT", raising=False)
    monkeypatch.setenv("GIT_COMMIT", "cd5494c")

    result = ops_status.get_ops_status(
        db_path=empty_db,
        backup_dir=str(tmp_path / "missing"),
        repo_dir=str(tmp_path / "norepo"),
    )

    assert result["running_commit"] == "cd5494c"


# ---- F17（G 批次）：定时备份状态 ----


def _iso(hours_ago):
    """宿主本地时区的 ISO8601 时间戳；hours_ago 可为小数。"""
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_ago)
    return moment.astimezone().isoformat(timespec="seconds")


def _timed_dir(backup):
    path = backup / "timed"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _today_snapshot():
    return "console.db." + datetime.date.today().strftime("%Y%m%d")


def _write_timed_status(backup, **overrides):
    payload = {
        "version": 1,
        "last_attempt": _iso(1),
        "last_success": _iso(1),
        "snapshot": _today_snapshot(),
        "ok": True,
        "stage": None,
    }
    payload.update(overrides)
    path = _timed_dir(backup) / "status.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    if payload.get("snapshot"):
        (_timed_dir(backup) / payload["snapshot"]).write_bytes(b"snapshot")
    return payload


def test_timed_backup_never_when_timed_dir_absent(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    result = ops_status._timed_backup(str(backup))
    assert result["state"] == "never"
    assert result["last_success"] is None
    assert result["last_attempt"] is None


def test_timed_backup_never_when_timed_dir_empty(tmp_path):
    backup = tmp_path / "backup"
    _timed_dir(backup)
    assert ops_status._timed_backup(str(backup))["state"] == "never"


def test_timed_backup_unknown_when_snapshot_exists_without_marker(tmp_path):
    """旧快照没有成功标记：存在性不能冒充完整性（F17 红线）。"""
    backup = tmp_path / "backup"
    (_timed_dir(backup) / _today_snapshot()).write_bytes(b"snapshot")
    assert ops_status._timed_backup(str(backup))["state"] == "unknown"


def test_timed_backup_ok_when_marker_and_file_are_fresh(tmp_path):
    backup = tmp_path / "backup"
    payload = _write_timed_status(backup)
    result = ops_status._timed_backup(str(backup))
    assert result["state"] == "ok"
    assert result["last_success"] == payload["last_success"]
    assert result["last_attempt"] == payload["last_attempt"]
    assert result["snapshot"] == payload["snapshot"]
    assert result["stage"] is None


def test_timed_backup_stale_after_26_hours_but_keeps_last_success(tmp_path):
    backup = tmp_path / "backup"
    payload = _write_timed_status(backup, last_attempt=_iso(27), last_success=_iso(27))
    result = ops_status._timed_backup(str(backup))
    assert result["state"] == "stale"
    assert result["last_success"] == payload["last_success"]


@pytest.mark.parametrize("hours,expected", [(25.9, "ok"), (26.1, "stale")])
def test_timed_backup_threshold_is_26_hours(tmp_path, hours, expected):
    backup = tmp_path / "backup"
    _write_timed_status(backup, last_attempt=_iso(hours), last_success=_iso(hours))
    assert ops_status._timed_backup(str(backup))["state"] == expected


def test_timed_backup_failed_keeps_stage_and_prior_success(tmp_path):
    backup = tmp_path / "backup"
    prior = _iso(2)
    _write_timed_status(
        backup, ok=False, stage="quick_check", last_attempt=_iso(0.1), last_success=prior
    )
    result = ops_status._timed_backup(str(backup))
    assert result["state"] == "failed"
    assert result["stage"] == "quick_check"
    assert result["last_success"] == prior


def test_timed_backup_unknown_when_marker_corrupt(tmp_path):
    backup = tmp_path / "backup"
    (_timed_dir(backup) / "status.json").write_text("{ not json", encoding="utf-8")
    assert ops_status._timed_backup(str(backup))["state"] == "unknown"


@pytest.mark.parametrize("body", ["[]", '"text"', "null", "42"])
def test_timed_backup_unknown_when_marker_not_object(tmp_path, body):
    backup = tmp_path / "backup"
    (_timed_dir(backup) / "status.json").write_text(body, encoding="utf-8")
    assert ops_status._timed_backup(str(backup))["state"] == "unknown"


def test_timed_backup_unknown_when_ok_marker_references_missing_file(tmp_path):
    backup = tmp_path / "backup"
    _write_timed_status(backup)
    (_timed_dir(backup) / _today_snapshot()).unlink()
    assert ops_status._timed_backup(str(backup))["state"] == "unknown"


@pytest.mark.parametrize(
    "bad", ["../console.db.20260908", "nested/console.db.20260908", "/etc/passwd"]
)
def test_timed_backup_unknown_when_snapshot_name_escapes_timed_dir(tmp_path, bad):
    """快照名只接受纯文件名：带分隔符或绝对路径一律 unknown，不做越界探测。"""
    backup = tmp_path / "backup"
    (backup / "timed").mkdir(parents=True)
    (backup / "timed" / "status.json").write_text(
        json.dumps(
            {
                "version": 1,
                "last_attempt": _iso(1),
                "last_success": _iso(1),
                "snapshot": bad,
                "ok": True,
                "stage": None,
            }
        ),
        encoding="utf-8",
    )
    assert ops_status._timed_backup(str(backup))["state"] == "unknown"


@pytest.mark.parametrize("bad", ["not-a-time", "", "2026-09-08T04:10:12"])
def test_timed_backup_unknown_when_success_timestamp_unusable(tmp_path, bad):
    """无时区的裸时间戳也按不可用处理：不猜时区，也不拿读盘时刻顶替。"""
    backup = tmp_path / "backup"
    _write_timed_status(backup, last_success=bad)
    assert ops_status._timed_backup(str(backup))["state"] == "unknown"


def test_get_ops_status_includes_timed_backup_seventh_key(empty_db, tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("RUNNING_COMMIT", raising=False)
    backup = tmp_path / "backup"
    _write_timed_status(backup)

    result = ops_status.get_ops_status(
        db_path=empty_db, backup_dir=str(backup), repo_dir=str(tmp_path / "norepo")
    )

    assert set(result.keys()) == {
        "running_commit",
        "versions_last_checked",
        "feed_last_fetched",
        "last_backup_tag",
        "last_deploy",
        "last_probe_run",
        "timed_backup",
        "monitoring_business_source",
    }
    assert result["timed_backup"]["state"] == "ok"
