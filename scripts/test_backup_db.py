#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backup_db.py 的自测：定向覆盖「同目录临时文件 → quick_check → os.replace」的
原子备份链路，以及 retention 的按日期精确匹配。

跑法：`python3 -m pytest scripts/test_backup_db.py -q`（除 pytest 外零依赖）。

**硬约束**：全部用例只在 `tmp_path` 下构造假源库和假备份目录，绝不指向真实
`console/backend/data/console.db` 或任何 NAS 路径（`CONSOLE_BACKUP_ROOT` 默认值
`/vol1/console-stack-data/backup/timed` 在测试里从不被实际访问，凡是调用
`bkp.main()` 的用例都先 monkeypatch `bkp.ROOT` / `bkp.BACKUP_ROOT` 指向 tmp_path）。
"""
import datetime
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import backup_db as bkp  # noqa: E402 —— 必须先把 scripts/ 放进 sys.path


@pytest.fixture(autouse=True)
def explicit_offline_engine(monkeypatch):
    monkeypatch.setenv("CONSOLE_STORAGE_ENGINE", "sqlite")


def _make_valid_sqlite(path, rows=2):
    """构造一个可通过 quick_check 的最小合法 sqlite 文件。"""
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT);"
        + "".join(f"INSERT INTO t VALUES ({i}, 'v{i}');" for i in range(1, rows + 1))
    )
    conn.commit()
    conn.close()
    return path


def _stamp(days_ago=0):
    return (datetime.date.today() - datetime.timedelta(days=days_ago)).strftime("%Y%m%d")


# ---------------------------------------------------------------- write_snapshot 好场景


def test_write_snapshot_temp_lives_in_same_dir_and_replaces_atomically(tmp_path, monkeypatch):
    """临时文件必须与 dest 同目录（os.replace 原子性前提），成功后 dest 是唯一残留、
    且真的调用了 os.replace（而不是 copy 或 rename 到别处）。"""
    src = _make_valid_sqlite(tmp_path / "source.db")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    dest = backup_dir / f"console.db.{_stamp()}"

    seen = {}
    real_backup = bkp.dbmod.backup_database

    def spy_backup(dest_path, db_path=None):
        seen["temp_dir"] = Path(dest_path).parent
        seen["temp_name"] = Path(dest_path).name
        seen["siblings_during"] = sorted(p.name for p in Path(dest_path).parent.iterdir())
        return real_backup(dest_path, db_path)

    monkeypatch.setattr(bkp.dbmod, "backup_database", spy_backup)

    replace_calls = []
    real_replace = bkp.os.replace

    def spy_replace(src_path, dst_path):
        replace_calls.append((src_path, dst_path))
        return real_replace(src_path, dst_path)

    monkeypatch.setattr(bkp.os, "replace", spy_replace)

    result = bkp.write_snapshot(dest, src)

    assert result == dest
    assert seen["temp_dir"] == backup_dir  # 同目录
    assert seen["temp_name"] != dest.name  # 唯一临时名，非最终名
    assert seen["temp_name"] in seen["siblings_during"]  # 备份期间临时文件确实存在
    assert replace_calls == [(str(backup_dir / seen["temp_name"]), str(dest))]
    assert dest.exists()
    assert [p.name for p in backup_dir.iterdir()] == [dest.name]  # 无残留临时文件


def test_write_snapshot_first_backup_without_prior_final_file(tmp_path):
    """首次备份（当日尚无最终文件）也能成功。"""
    src = _make_valid_sqlite(tmp_path / "source.db")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    dest = backup_dir / f"console.db.{_stamp()}"
    assert not dest.exists()

    result = bkp.write_snapshot(dest, src)

    assert result == dest
    assert dest.exists()
    assert dest.stat().st_size > 0


# ---------------------------------------------------------------- 失败路径


def test_quick_check_failure_keeps_old_final_file_and_removes_temp(tmp_path, monkeypatch):
    """quick_check 不是 'ok' 时：报 BackupIntegrityError，旧的最终文件原样保留，
    临时文件被清理，不发生替换。"""
    src = _make_valid_sqlite(tmp_path / "source.db")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    dest = backup_dir / f"console.db.{_stamp()}"
    dest.write_bytes(b"OLD-VALID-BACKUP-BYTES")

    monkeypatch.setattr(bkp, "_quick_check", lambda path: "corruption near page 3")

    with pytest.raises(bkp.BackupIntegrityError):
        bkp.write_snapshot(dest, src)

    assert dest.read_bytes() == b"OLD-VALID-BACKUP-BYTES"  # 旧文件原样保留
    assert [p.name for p in backup_dir.iterdir()] == [dest.name]  # 临时文件已清理


def test_backup_database_exception_keeps_old_final_file_and_removes_temp(tmp_path, monkeypatch):
    """backup_database 自身抛异常时：异常原样上抛，旧最终文件不变，临时文件被清理。"""
    src = _make_valid_sqlite(tmp_path / "source.db")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    dest = backup_dir / f"console.db.{_stamp()}"
    dest.write_bytes(b"OLD-VALID-BACKUP-BYTES")

    def boom(dest_path, db_path=None):
        raise OSError("simulated disk error")

    monkeypatch.setattr(bkp.dbmod, "backup_database", boom)

    with pytest.raises(OSError, match="simulated disk error"):
        bkp.write_snapshot(dest, src)

    assert dest.read_bytes() == b"OLD-VALID-BACKUP-BYTES"
    assert [p.name for p in backup_dir.iterdir()] == [dest.name]


# ---------------------------------------------------------------- retention


def test_prune_old_backups_removes_only_expired_own_prefix(tmp_path):
    keep_days = 14
    old = tmp_path / f"console.db.{_stamp(keep_days + 1)}"
    recent = tmp_path / f"console.db.{_stamp(1)}"
    deploy_style = tmp_path / f"console.db.{_stamp(keep_days + 5)}-013357"  # deploy.sh 命名
    stray_temp = tmp_path / f"console.db.{_stamp(keep_days + 5)}.tmp-abcd1234"
    unrelated = tmp_path / "notes.txt"
    for f in (old, recent, deploy_style, stray_temp, unrelated):
        f.write_bytes(b"x")

    removed = bkp.prune_old_backups(tmp_path, keep_days)

    assert removed == 1
    assert (tmp_path / ".trash" / old.name / old.name).read_bytes() == b"x"
    assert not old.exists()
    assert recent.exists()
    assert deploy_style.exists()  # 不是本脚本精确命名，不误删
    assert stray_temp.exists()  # 同上
    assert unrelated.exists()


# ---------------------------------------------------------------- main() 端到端


def test_main_success_end_to_end_prunes_and_prints_ok(tmp_path, monkeypatch, capsys):
    root = tmp_path / "repo"
    db_path = root / "console" / "backend" / "data" / "console.db"
    db_path.parent.mkdir(parents=True)
    _make_valid_sqlite(db_path)
    backup_root = tmp_path / "backup" / "timed"

    monkeypatch.setattr(bkp, "ROOT", root)
    monkeypatch.setattr(bkp, "BACKUP_ROOT", backup_root)

    old_stamp = _stamp(bkp.KEEP_DAYS + 1)
    backup_root.mkdir(parents=True)
    old_file = backup_root / f"console.db.{old_stamp}"
    old_file.write_bytes(b"old")

    rc = bkp.main()

    assert rc == 0
    dest = backup_root / f"console.db.{_stamp()}"
    assert dest.exists()
    assert not old_file.exists()  # 本次运行顺带清理过期文件
    out = capsys.readouterr().out
    assert "backup ok" in out
    assert "archived 1 old backup" in out


def test_main_integrity_failure_returns_nonzero_and_keeps_old_file(tmp_path, monkeypatch, capsys):
    root = tmp_path / "repo"
    db_path = root / "console" / "backend" / "data" / "console.db"
    db_path.parent.mkdir(parents=True)
    _make_valid_sqlite(db_path)
    backup_root = tmp_path / "backup" / "timed"
    backup_root.mkdir(parents=True)

    monkeypatch.setattr(bkp, "ROOT", root)
    monkeypatch.setattr(bkp, "BACKUP_ROOT", backup_root)
    monkeypatch.setattr(bkp, "_quick_check", lambda path: "corruption")

    dest = backup_root / f"console.db.{_stamp()}"
    dest.write_bytes(b"OLD-VALID-BACKUP-BYTES")

    rc = bkp.main()

    assert rc == 1
    assert dest.read_bytes() == b"OLD-VALID-BACKUP-BYTES"
    assert not [p for p in backup_root.iterdir() if ".tmp-" in p.name]  # 无残留临时文件
    err = capsys.readouterr().err
    assert "ERROR" in err
    # F17：失败也留下可读状态（本次明确失败），且保留旧的 last_success
    status = _read_status(backup_root)
    assert status["ok"] is False
    assert status["stage"] == "quick_check"


def test_snapshot_uri_handles_special_path_characters(tmp_path):
    folder = tmp_path / "question? hash# percent%"
    folder.mkdir()
    source = _make_valid_sqlite(folder / "source.db")
    target = folder / "snapshot.db"
    bkp.write_snapshot(target, source)
    assert bkp._quick_check(target) == "ok"


def test_killed_backup_leaves_identifiable_temp_and_preserves_final(tmp_path):
    source = _make_valid_sqlite(tmp_path / "source.db")
    target = _make_valid_sqlite(tmp_path / "snapshot.db")
    old_bytes = target.read_bytes()
    program = '''
import os
import signal
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
import backup_db

def interrupted(dest_path, db_path=None):
    Path(dest_path).write_bytes(b"partial snapshot before abrupt kill")
    os.kill(os.getpid(), signal.SIGKILL)

backup_db.dbmod.backup_database = interrupted
backup_db.write_snapshot(sys.argv[2], sys.argv[1])
'''
    result = subprocess.run([sys.executable, "-c", program, str(source), str(target), str(Path(__file__).parent)], capture_output=True, timeout=10)
    assert result.returncode == -9
    assert target.read_bytes() == old_bytes
    assert bkp._quick_check(target) == "ok"
    partials = list(tmp_path.glob("snapshot.db.tmp-*"))
    assert len(partials) == 1
    assert partials[0].read_bytes() == b"partial snapshot before abrupt kill"


# ---------------------------------------------------------------- F17 状态标记


def _status_path(backup_root):
    return Path(backup_root) / "status.json"


def _read_status(backup_root):
    return json.loads(_status_path(backup_root).read_text(encoding="utf-8"))


def _setup_main(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    db_path = root / "console" / "backend" / "data" / "console.db"
    db_path.parent.mkdir(parents=True)
    _make_valid_sqlite(db_path)
    backup_root = tmp_path / "backup" / "timed"
    monkeypatch.setattr(bkp, "ROOT", root)
    monkeypatch.setattr(bkp, "BACKUP_ROOT", backup_root)
    return backup_root


def _fail_marker_publish(monkeypatch):
    """让快照发布照常进行，只有标记的原子替换失败。"""
    real_replace = bkp.os.replace

    def failing_replace(src, dst):
        if Path(dst).name == "status.json":
            raise OSError("simulated marker publish failure")
        return real_replace(src, dst)

    monkeypatch.setattr(bkp.os, "replace", failing_replace)


def test_main_writes_success_marker_matching_attempt_and_success(tmp_path, monkeypatch):
    backup_root = _setup_main(tmp_path, monkeypatch)

    assert bkp.main() == 0

    status = _read_status(backup_root)
    assert status["ok"] is True
    assert status["stage"] is None
    assert status["last_attempt"] == status["last_success"]
    assert status["snapshot"] == f"console.db.{_stamp()}"
    assert (backup_root / status["snapshot"]).exists()
    assert not list(backup_root.glob("status.json.tmp-*"))  # 标记也是原子发布


def test_main_failure_records_stage_and_preserves_prior_success(tmp_path, monkeypatch):
    backup_root = _setup_main(tmp_path, monkeypatch)
    assert bkp.main() == 0
    first = _read_status(backup_root)

    monkeypatch.setattr(bkp, "_quick_check", lambda path: "corruption")
    assert bkp.main() == 1

    status = _read_status(backup_root)
    assert status["ok"] is False
    assert status["stage"] == "quick_check"
    assert status["last_success"] == first["last_success"]  # 保留已知成功
    assert status["snapshot"] == first["snapshot"]


def test_marker_publish_failure_returns_nonzero_and_keeps_prior_marker(
    tmp_path, monkeypatch, capsys
):
    backup_root = _setup_main(tmp_path, monkeypatch)
    assert bkp.main() == 0
    first = _read_status(backup_root)
    capsys.readouterr()

    _fail_marker_publish(monkeypatch)

    assert bkp.main() == 1

    captured = capsys.readouterr()
    assert "backup ok" not in captured.out  # 不报本次成功
    assert "标记" in captured.err
    assert _read_status(backup_root) == first  # 旧标记未被覆盖
    assert not list(backup_root.glob("status.json.tmp-*"))


def test_same_day_rerun_with_marker_failure_never_proves_this_attempt(tmp_path, monkeypatch):
    """中断边界：快照发布成功、标记发布失败时，同一文件名的旧标记不能证明本次成功。

    用真实写者（backup_db.main）产生状态，再用真实消费者（ops_status._timed_backup）
    读取：消费者只信标记里的时间戳，不被新快照文件的存在或 mtime 冒充。
    """
    backend = Path(__file__).resolve().parents[1] / "console" / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    import ops_status

    backup_root = _setup_main(tmp_path, monkeypatch)
    assert bkp.main() == 0

    status = _read_status(backup_root)
    stale = (datetime.datetime.now() - datetime.timedelta(hours=27)).astimezone().isoformat(
        timespec="seconds"
    )
    status["last_attempt"] = stale
    status["last_success"] = stale
    _status_path(backup_root).write_text(json.dumps(status), encoding="utf-8")

    _fail_marker_publish(monkeypatch)
    assert bkp.main() == 1  # 快照已重新发布，但标记失败 → 非零

    snapshot = backup_root / status["snapshot"]
    assert snapshot.exists()  # 同名快照确实被本次覆盖
    result = ops_status._timed_backup(str(backup_root.parent))
    assert result["state"] == "stale"  # 仍是 27 小时前的成功，不是「本次成功」
    assert result["last_success"] == stale


@pytest.mark.parametrize("after_database_move", [False, True])
def test_expired_group_is_recoverable_after_interrupted_archive(tmp_path, monkeypatch, after_database_move):
    import backup_runtime as runtime

    backup, trash = tmp_path / 'backup', tmp_path / 'trash'
    backup.mkdir()
    dump = backup / f'console.dump.{_stamp(30)}'
    names = [dump.name + bkp.MANIFEST_SUFFIX, dump.name + bkp.backup_pay.SUFFIX, dump.name]
    for name in names:
        (backup / name).write_bytes(name.encode())
    move = runtime.move_preserving
    def interrupted(source, target, digest):
        if Path(source).name == dump.name:
            if after_database_move:
                move(source, target, digest)
            raise OSError('injected interruption at database marker')
        return move(source, target, digest)
    monkeypatch.setattr(runtime, 'move_preserving', interrupted)
    with pytest.raises(OSError, match='interruption'):
        bkp.prune_old_backups(backup, 14, trash_root=trash)
    assert dump.exists() is not after_database_move
    monkeypatch.setattr(runtime, 'move_preserving', move)
    assert bkp.prune_old_backups(backup, 14, trash_root=trash) == 1
    for name in names:
        assert not (backup / name).exists()
        assert (trash / dump.name / name).read_bytes() == name.encode()
    journal = json.loads((trash / dump.name / 'manifest.json').read_text())
    assert journal['status'] == 'complete'
    assert bkp.prune_old_backups(backup, 14, trash_root=trash) == 0


def test_expired_group_target_conflict_preserves_both_copies(tmp_path):
    backup, trash = tmp_path / 'backup', tmp_path / 'trash'
    backup.mkdir()
    dump = backup / f'console.dump.{_stamp(30)}'
    dump.write_bytes(b'original')
    target = trash / dump.name
    target.mkdir(parents=True)
    (target / dump.name).write_bytes(b'other retained data')
    with pytest.raises(FileExistsError):
        bkp.prune_old_backups(backup, 14, trash_root=trash)
    assert dump.read_bytes() == b'original'
    assert (target / dump.name).read_bytes() == b'other retained data'


def test_expired_group_rejects_trash_symlink(tmp_path):
    backup = tmp_path / 'backup'
    backup.mkdir()
    dump = backup / f'console.dump.{_stamp(30)}'
    dump.write_bytes(b'original')
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = tmp_path / 'trash'
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        bkp.prune_old_backups(backup, 14, trash_root=link)
    assert dump.read_bytes() == b'original'
    assert not list(outside.iterdir())
