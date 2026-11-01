"""F16：setup_version_sync_cron.sh 的迁移 / 幂等 / 失败安全契约。

全部用例只使用假 crontab 可执行文件，绝不读写宿主机真实 crontab，
不安装任何 cron，不创建真实日志目录。
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "console/scripts/setup_version_sync_cron.sh"

MARKER = "console-stack:version_sync"
# 2026-09-08 只读核验到的 NAS 遗留行（整行 SHA256）。
LEGACY_LINE = "*/30 * * * * docker exec console-brain python version_sync.py >/dev/null 2>&1"
LEGACY_SHA256 = "d0c2147447d98a77c93ed1d439e09a036599da0800cfc9b17f0b0cadbd12567f"


def test_legacy_line_matches_the_verified_inventory_sha():
    assert hashlib.sha256(LEGACY_LINE.encode()).hexdigest() == LEGACY_SHA256


def fake_environment(tmp_path):
    """把 crontab 换成记录调用并保存状态到文件的假命令。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "crontab.jsonl"
    fake = f"#!{sys.executable}\n" + '''
import json
import os
import pathlib
import sys

args = sys.argv[1:]
with open(os.environ["CRONTAB_LOG"], "a") as handle:
    handle.write(json.dumps({"args": args}) + "\\n")
state = pathlib.Path(os.environ["CRONTAB_STATE"])
if args == ["-l"]:
    if os.getenv("FAIL_CRONTAB"):
        print("cannot read crontab", file=sys.stderr)
        sys.exit(2)
    if state.exists():
        sys.stdout.write(state.read_text())
        sys.exit(0)
    print("no crontab for fixture-user", file=sys.stderr)
    sys.exit(1)
if args == ["-"]:
    state.write_text(sys.stdin.read())
    sys.exit(0)
sys.exit(3)
'''
    target = bin_dir / "crontab"
    target.write_text(fake)
    target.chmod(0o755)
    return {
        **{key: os.environ[key] for key in ("TMPDIR", "LANG", "LC_ALL") if key in os.environ},
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "CRONTAB_LOG": str(log),
        "CRONTAB_STATE": str(tmp_path / "crontab.txt"),
    }


def run_script(env, *args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args], cwd=ROOT, env=env, text=True,
        errors="replace", capture_output=True, timeout=25,
    )


def seed(env, text):
    Path(env["CRONTAB_STATE"]).write_text(text)


def crontab_text(env):
    path = Path(env["CRONTAB_STATE"])
    return path.read_text() if path.exists() else ""


def crontab_calls(env):
    path = Path(env["CRONTAB_LOG"])
    if not path.exists():
        return []
    return [json.loads(line)["args"] for line in path.read_text().splitlines()]


def log_dir(env):
    return Path(env["HOME"]) / ".local/log/console-stack"


def marked_lines(text):
    return [line for line in text.splitlines()
            if line.rstrip().endswith(" # " + MARKER)]


@pytest.fixture
def env(tmp_path):
    return fake_environment(tmp_path)


def test_dry_run_plans_migration_without_writing_or_creating_log_dir(env):
    seed(env, LEGACY_LINE + "\n")
    result = run_script(env, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "would migrate" in result.stdout
    assert ["-"] not in crontab_calls(env)
    assert crontab_text(env) == LEGACY_LINE + "\n"
    assert not log_dir(env).exists()


def test_dry_run_on_empty_crontab_still_makes_no_changes(env):
    result = run_script(env, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert ["-"] not in crontab_calls(env)
    assert not log_dir(env).exists()


def test_install_migrates_the_exact_legacy_line_and_preserves_unrelated_lines(env):
    unrelated = [
        "MAILTO=ops@example.invalid",
        "# 手写注释，不要动",
        "0 3 * * * /usr/local/bin/backup-other.sh",
        "*/10 * * * * docker exec other-brain python version_sync.py >/dev/null 2>&1",
        "*/30 * * * * docker exec console-brain python /srv/other/version_sync.py >/dev/null 2>&1",
    ]
    seed(env, "\n".join([*unrelated, LEGACY_LINE]) + "\n")

    result = run_script(env, "--install")

    assert result.returncode == 0, result.stdout + result.stderr
    lines = crontab_text(env).splitlines()
    assert LEGACY_LINE not in lines, "遗留行必须被迁移掉"
    for line in unrelated:
        assert line in lines, line
    own = marked_lines(crontab_text(env))
    assert len(own) == 1
    assert own[0].startswith("*/30 * * * * ")
    assert log_dir(env).is_dir(), "install 才创建日志目录"


def test_install_on_empty_crontab_writes_exactly_one_clean_line(env):
    result = run_script(env, "--install")
    assert result.returncode == 0, result.stdout + result.stderr
    lines = crontab_text(env).splitlines()
    assert lines == [line for line in lines if line.strip()], "不得留下空行"
    assert len(lines) == 1
    assert lines[0].endswith(" # " + MARKER)


def test_install_is_idempotent_across_repeated_runs(env):
    seed(env, "\n".join(["0 3 * * * /usr/local/bin/other.sh", LEGACY_LINE]) + "\n")
    first = run_script(env, "--install")
    assert first.returncode == 0, first.stdout + first.stderr
    after_first = crontab_text(env)

    second = run_script(env, "--install")

    assert second.returncode == 0, second.stdout + second.stderr
    assert crontab_text(env) == after_first
    assert len(marked_lines(after_first)) == 1


def test_marker_substring_and_lookalike_jobs_are_not_owned(env):
    lookalikes = [
        "# " + LEGACY_LINE,
        "*/30 * * * * docker exec console-brain python version_sync.py  # " + MARKER + "-extra",
        "*/30 * * * * docker exec console-brain python version_sync.py >> /tmp/other.log 2>&1 # other-tool",
    ]
    seed(env, "\n".join(lookalikes) + "\n")

    result = run_script(env, "--install")

    assert result.returncode == 0, result.stdout + result.stderr
    lines = crontab_text(env).splitlines()
    for line in lookalikes:
        assert line in lines, line
    assert len(marked_lines(crontab_text(env))) == 1


@pytest.mark.parametrize("mode", ["--install", "--uninstall", "--dry-run"])
def test_unreadable_crontab_fails_without_writing_or_mkdir(env, mode):
    seed(env, LEGACY_LINE + "\n")
    env["FAIL_CRONTAB"] = "1"

    result = run_script(env, mode)

    assert result.returncode != 0, result.stdout + result.stderr
    assert ["-"] not in crontab_calls(env), "读不到就不能写"
    assert crontab_text(env) == LEGACY_LINE + "\n"
    assert not log_dir(env).exists()


def test_uninstall_removes_only_the_marked_job_and_keeps_everything_else(env):
    own = "*/30 * * * * docker exec console-brain python version_sync.py >> /tmp/v.log 2>&1 # " + MARKER
    kept = ["0 3 * * * /usr/local/bin/other.sh", LEGACY_LINE,
            "*/30 * * * * docker exec console-brain python version_sync.py  # " + MARKER + "-extra"]
    seed(env, "\n".join([*kept, own]) + "\n")

    result = run_script(env, "--uninstall")

    assert result.returncode == 0, result.stdout + result.stderr
    lines = crontab_text(env).splitlines()
    assert own not in lines
    assert marked_lines(crontab_text(env)) == []
    for line in kept:
        assert line in lines, line
    assert LEGACY_LINE in result.stdout, "仍有未迁移的遗留行必须提示"
