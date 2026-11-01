"""Execute smoke environment construction and its real client configuration gate.

These checks need neither Docker nor PG; image/schema/DML coverage remains in CI.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ci-image-smoke.sh"


def smoke_env(name):
    source = SCRIPT.read_text()
    # Run the actual Bash arrays, without provisioning containers or loading .env.
    arrays = source[source.index("admin_kv=("):source.index("yaml_mounts=(")]
    dump = 'import json,sys; print(json.dumps(dict(x.split("=",1) for x in sys.argv[1:] if x != "-e")))'
    result = subprocess.run(
        ["bash", "-c", 'set -eu\nPG_NAME=isolated.invalid\nDB=smoke\n'
         'ADMIN_USER=admin\nADMIN_PW=synthetic\nAPP_ROLE=console_mac\nAPP_PW=synthetic\n'
         + arrays + '\n"$1" -c "$2" "${' + name + '[@]}"', "smoke", sys.executable, dump],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def client_gate(env):
    clean = {key: value for key, value in os.environ.items()
             if not key.startswith("CONSOLE_") and key != "PORT"}
    clean.update(env, PYTHONPATH=str(ROOT / "console/backend"))
    return subprocess.run(
        [sys.executable, "-c", "import runtime_mode; runtime_mode.require_client_authority()"],
        env=clean, capture_output=True, text=True, timeout=10,
    )


@pytest.mark.parametrize("name", ["app_env", "wrong_pw_env", "no_engine_env"])
def test_smoke_env_reaches_its_intended_gate(name):
    env = smoke_env(name)
    assert env["CONSOLE_INSTANCE_ROLE"] == "client"
    result = client_gate(env)
    assert result.returncode == 0, result.stderr
    assert env["CONSOLE_AUTHORITY_ORIGIN"] == "http://console-ci-authority.invalid:8080"
    if name == "no_engine_env":
        assert "CONSOLE_STORAGE_ENGINE" not in env
    else:
        assert env["CONSOLE_STORAGE_ENGINE"] == "postgres"
    if name == "wrong_pw_env":
        assert env["CONSOLE_PG_PASSWORD"] != smoke_env("app_env")["CONSOLE_PG_PASSWORD"]


def test_smoke_missing_authority_is_a_separate_failure():
    env = smoke_env("no_authority_env")
    assert env == {k: v for k, v in smoke_env("app_env").items()
                   if k != "CONSOLE_AUTHORITY_ORIGIN"}
    result = client_gate(env)
    assert result.returncode != 0
    assert "client 未配置 CONSOLE_AUTHORITY_ORIGIN" in result.stderr
    assert 'run_app_expect_exit "未声明 CONSOLE_AUTHORITY_ORIGIN" 4 "${no_authority_env[@]}"' in SCRIPT.read_text()


def test_pg_readiness_waits_for_tcp_after_temporary_socket_server(tmp_path):
    source = SCRIPT.read_text()
    readiness = source[source.index("for _ in $(seq 1 60)"):source.index("admin_kv=(")]
    # Official PG images start a socket-only bootstrap server, then stop it.
    # Old socket readiness breaks early; the next check sees its shutdown.
    harness = r"""
set -eu
PG_NAME=isolated-pg
ADMIN_USER=synthetic
DB=synthetic
ATTEMPTS="$1"
printf 0 > "$ATTEMPTS"
docker() {
  count=$(cat "$ATTEMPTS")
  count=$((count + 1))
  printf '%s' "$count" > "$ATTEMPTS"
  case " $* " in
    *" pg_isready -h 127.0.0.1 "*) [ "$count" -ge 3 ] ;;
    *" pg_isready "*) [ "$count" -eq 1 ] ;;
    *" logs "*) return 0 ;;
    *) return 99 ;;
  esac
}
sleep() { :; }
ok() { :; }
"""
    attempts = tmp_path / "attempts"
    result = subprocess.run(
        ["bash", "-c", harness + readiness, "readiness", str(attempts)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert int(attempts.read_text()) >= 3


@pytest.mark.parametrize("fault", [
    None, "sqlite", "unreachable", "schema", "probe_schema", "server",
    "invalid_json", "local_503", "authority_200", "authority_wrong_error",
])
def test_smoke_checks_local_storage_and_unavailable_authority(tmp_path, fault):
    """Run the real shell assertions against good and deliberately broken responses."""
    source = SCRIPT.read_text()
    checks = source[
        source.index('split_body_code "$(fetch http://127.0.0.1:8080/api/config/effective)"'):
        source.index("# 真写读一个来回")
    ]
    splitter = source[
        source.index("split_body_code() {"):source.index("\nready=0")
    ]
    # 期望 schema 与烟测脚本同源（pg_migrations 派生），新增迁移后两端都不腐烂。
    sys.path.insert(0, str(ROOT / "console/backend"))
    import pg_migrations
    expect_schema = pg_migrations.latest_schema_version()
    payload = {
        "storage_engine": "postgres", "schema_version": expect_schema,
        "database": {"reachable": True, "schema_version": expect_schema, "server_version": "16.9"},
    }
    if fault == "sqlite":
        payload["storage_engine"] = "sqlite"
    elif fault == "unreachable":
        payload["database"]["reachable"] = False
    elif fault == "schema":
        payload["schema_version"] = 11
    elif fault == "probe_schema":
        payload["database"]["schema_version"] = 11
    elif fault == "server":
        payload["database"]["server_version"] = "17.1"
    # Config bodies must never be echoed, even when the diagnostic check fails.
    payload["synthetic_secret"] = "DO_NOT_PRINT_CONFIG"
    local_body = "DO_NOT_PRINT_CONFIG" if fault == "invalid_json" else json.dumps(payload)
    (tmp_path / "local").write_text(local_body + ("\n503" if fault == "local_503" else "\n200"))
    error = "unrelated" if fault == "authority_wrong_error" else "NAS 权威端不可达，请稍后重试"
    (tmp_path / "authority").write_text(
        json.dumps({"error": error}) + ("\n200" if fault == "authority_200" else "\n503")
    )
    harness = r"""
set -eu
FAILED=0
FIXTURES="$1"
EXPECT_SCHEMA="__EXPECT_SCHEMA__"
export EXPECT_SCHEMA
ok() { :; }
bad() { FAILED=$((FAILED + 1)); }
fetch() {
  case "$1" in
    */api/config/effective) cat "$FIXTURES/local" ;;
    */api/ops-status) cat "$FIXTURES/authority" ;;
    *) return 99 ;;
  esac
}
"""
    result = subprocess.run(
        ["bash", "-c",
         harness.replace("__EXPECT_SCHEMA__", str(expect_schema)) + splitter + checks
         + '\n[ "$FAILED" = 0 ]',
         "diagnostics", str(tmp_path)],
        capture_output=True, text=True, timeout=10,
    )
    assert (result.returncode == 0) == (fault is None), result.stdout + result.stderr
    assert "DO_NOT_PRINT_CONFIG" not in result.stdout + result.stderr
