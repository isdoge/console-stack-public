"""B6-3：setup_monitoring_maintenance_cron.sh 的安装 / 幂等 / 失败安全契约。

全部用例只使用假 crontab 可执行文件，绝不读写宿主机真实 crontab，
不安装任何 cron，不创建真实日志目录，不执行任何 docker exec。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "console/scripts/setup_monitoring_maintenance_cron.sh"

MARKER_RETENTION = "console-stack:monitoring-maintenance:retention"
MARKER_EVALUATE = "console-stack:monitoring-maintenance:evaluate"
MARKER_DMS = "console-stack:monitoring-maintenance:dms"
MARKERS = (MARKER_RETENTION, MARKER_EVALUATE, MARKER_DMS)


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
        ["bash", str(SCRIPT), *args], check=False, cwd=ROOT, env=env, text=True,
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
            for marker in MARKERS
            if line.rstrip().endswith(" # " + marker)]


@pytest.fixture
def env(tmp_path):
    return fake_environment(tmp_path)


def test_default_mode_is_dry_run_and_writes_nothing(env):
    """默认 dry-run：不写 crontab、不建日志目录。"""
    result = run_script(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ["-"] not in crontab_calls(env)
    assert not log_dir(env).exists()
    for mode in ("retention", "evaluate", "dms"):
        assert mode in result.stdout


def test_dry_run_reports_shadow_mode_by_default(env):
    result = run_script(env, "--dry-run")
    assert "shadow" in result.stdout
    assert "--notify" not in result.stdout.split("would ensure crontab lines:")[0]


def test_install_writes_exactly_three_marked_lines(env):
    result = run_script(env, "--install")

    assert result.returncode == 0, result.stdout + result.stderr
    lines = crontab_text(env).splitlines()
    own = marked_lines(crontab_text(env))
    assert len(own) == 3
    assert lines == [line for line in lines if line.strip()], "不得留下空行"
    assert log_dir(env).is_dir(), "install 才创建日志目录"
    by_marker = {line.rsplit(" # ", 1)[1]: line for line in own}
    assert by_marker[MARKER_RETENTION].startswith("*/5 * * * * ")
    assert by_marker[MARKER_EVALUATE].startswith("* * * * * ")
    assert by_marker[MARKER_DMS].startswith("*/5 * * * * ")
    for line in own:
        assert "docker exec console-brain python monitoring_maintenance.py" in line
        assert ">> " in line  # 输出必须落日志文件，不能丢


def test_installed_lines_are_shadow_by_default(env):
    """默认安装是影子模式：evaluate/dms 线不带 --notify。"""
    result = run_script(env, "--install")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "notify=False" in result.stdout or "shadow" in result.stdout
    own = marked_lines(crontab_text(env))
    assert len(own) == 3
    for line in own:
        assert "--notify" not in line


def test_install_with_notify_adds_flag_only_to_evaluate_and_dms(env):
    """--notify 只加在 evaluate/dms；retention 无发信面永不带开关。"""
    result = run_script(env, "--install", "--notify")
    assert result.returncode == 0, result.stdout + result.stderr
    by_marker = {line.rsplit(" # ", 1)[1]: line for line in marked_lines(crontab_text(env))}
    assert "--notify" not in by_marker[MARKER_RETENTION]
    assert "--notify" in by_marker[MARKER_EVALUATE]
    assert "--notify" in by_marker[MARKER_DMS]


def test_install_preserves_unrelated_lines(env):
    unrelated = [
        "MAILTO=ops@example.invalid",
        "# 手写注释，不要动",
        "0 3 * * * /usr/local/bin/backup-other.sh",
        "*/30 * * * * docker exec console-brain python version_sync.py >> /tmp/v.log 2>&1 # console-stack:version_sync",
    ]
    seed(env, "\n".join(unrelated) + "\n")

    result = run_script(env, "--install")

    assert result.returncode == 0, result.stdout + result.stderr
    lines = crontab_text(env).splitlines()
    for line in unrelated:
        assert line in lines, line
    assert len(marked_lines(crontab_text(env))) == 3


def test_install_is_idempotent_across_repeated_runs(env):
    seed(env, "0 3 * * * /usr/local/bin/other.sh\n")
    first = run_script(env, "--install")
    assert first.returncode == 0, first.stdout + first.stderr
    after_first = crontab_text(env)

    second = run_script(env, "--install")

    assert second.returncode == 0, second.stdout + second.stderr
    assert crontab_text(env) == after_first
    assert len(marked_lines(after_first)) == 3


def test_install_replaces_stale_own_lines_without_duplication(env):
    """旧的自家行（如 shadow 时代的 evaluate）被替换成当前版本，不叠行。"""
    stale = (
        "* * * * * docker exec console-brain python monitoring_maintenance.py "
        f"evaluate >> /tmp/old.log 2>&1 # {MARKER_EVALUATE}"
    )
    seed(env, stale + "\n")

    result = run_script(env, "--install", "--notify")

    assert result.returncode == 0, result.stdout + result.stderr
    own = marked_lines(crontab_text(env))
    assert len(own) == 3
    assert stale not in crontab_text(env).splitlines()
    evaluate_lines = [line for line in own if MARKER_EVALUATE in line]
    assert len(evaluate_lines) == 1
    assert "--notify" in evaluate_lines[0]


def test_marker_lookalikes_are_not_owned(env):
    lookalikes = [
        f"* * * * * docker exec console-brain python monitoring_maintenance.py evaluate # {MARKER_EVALUATE}-extra",
        "*/5 * * * * docker exec console-brain python monitoring_maintenance.py retention >> /tmp/o.log 2>&1 # other-tool",
        f"# 注释里提到 marker：{MARKER_DMS} 但不以它结尾",
    ]
    seed(env, "\n".join(lookalikes) + "\n")

    result = run_script(env, "--install")

    assert result.returncode == 0, result.stdout + result.stderr
    lines = crontab_text(env).splitlines()
    for line in lookalikes:
        assert line in lines, line
    assert len(marked_lines(crontab_text(env))) == 3


@pytest.mark.parametrize("mode", ["--install", "--uninstall", "--dry-run"])
def test_unreadable_crontab_fails_without_writing_or_mkdir(env, mode):
    seed(env, "0 3 * * * /usr/local/bin/keep.sh\n")
    env["FAIL_CRONTAB"] = "1"

    result = run_script(env, mode)

    assert result.returncode != 0, result.stdout + result.stderr
    assert ["-"] not in crontab_calls(env), "读不到就不能写"
    assert crontab_text(env) == "0 3 * * * /usr/local/bin/keep.sh\n"
    assert not log_dir(env).exists()


def test_uninstall_removes_only_marked_jobs_and_keeps_everything_else(env):
    own = [
        f"*/5 * * * * docker exec console-brain python monitoring_maintenance.py retention >> /tmp/a.log 2>&1 # {MARKER_RETENTION}",
        f"* * * * * docker exec console-brain python monitoring_maintenance.py evaluate --notify >> /tmp/b.log 2>&1 # {MARKER_EVALUATE}",
        f"*/5 * * * * docker exec console-brain python monitoring_maintenance.py dms >> /tmp/c.log 2>&1 # {MARKER_DMS}",
    ]
    kept = [
        "0 3 * * * /usr/local/bin/other.sh",
        "*/30 * * * * docker exec console-brain python version_sync.py >> /tmp/v.log 2>&1 # console-stack:version_sync",
    ]
    seed(env, "\n".join([*kept, *own]) + "\n")

    result = run_script(env, "--uninstall")

    assert result.returncode == 0, result.stdout + result.stderr
    lines = crontab_text(env).splitlines()
    assert marked_lines(crontab_text(env)) == []
    for line in kept:
        assert line in lines, line


def test_uninstall_on_empty_crontab_is_a_noop(env):
    result = run_script(env, "--uninstall")
    assert result.returncode == 0, result.stdout + result.stderr
    assert ["-"] not in crontab_calls(env)


def test_notify_alone_does_not_install(env):
    """--notify 单独出现不触发安装：默认仍 dry-run。"""
    result = run_script(env, "--notify")
    assert result.returncode == 0, result.stdout + result.stderr
    assert ["-"] not in crontab_calls(env)
    assert not log_dir(env).exists()


def test_script_refuses_unknown_args(env):
    result = run_script(env, "--explode")
    assert result.returncode == 2
    assert ["-"] not in crontab_calls(env)
