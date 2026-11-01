# -*- coding: utf-8 -*-
"""B7 runbook 阶段 4 P3：宿主备份接组合身份（.env 显式开关）与 sync-identity。"""
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backup_runtime as runtime  # noqa: E402

IDENTITY = {
    "schema_version": 1,
    "kernel": {"version": "abc1234", "digest": "a" * 64},
    "plugins": [{"id": "ledger", "version": "1", "digest": "b" * 64}],
    "composition_digest": "c" * 64,
}


def _plugins(root):
    (root / "plugins").mkdir(exist_ok=True)
    (root / "plugins" / "contract.json").write_text("{}", encoding="utf-8")


def _write_env(root, **values):
    _plugins(root)
    lines = ["CONSOLE_STORAGE_ENGINE=postgres"]
    lines += [f"{key}={shlex.quote(str(value))}" for key, value in values.items()]
    (root / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _identity_file(root, document=IDENTITY):
    path = root / "identity" / "build-identity.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_unconfigured_keeps_v1_behaviour(tmp_path):
    _write_env(tmp_path)
    env = runtime.deployment_environment(tmp_path, {})
    assert "CONSOLE_BUILD_IDENTITY_FILE" not in env


def test_configured_identity_is_validated_and_wired_for_backup(tmp_path):
    path = _identity_file(tmp_path)
    _write_env(tmp_path, CONSOLE_BACKUP_IDENTITY_FILE=path)
    env = runtime.deployment_environment(tmp_path, {})
    assert env["CONSOLE_BUILD_IDENTITY_FILE"] == str(path)
    # 定时控制器在 .runtime 里没有插件树：v2 的组件报告按部署检出的插件根核对。
    assert env["CONSOLE_PLUGIN_ROOT"] == str((tmp_path / "plugins").resolve())
    # 幂等：把结果再喂回去不冲突（deploy → 子进程 → backup_db.main 会二次解析）。
    assert runtime.deployment_environment(tmp_path, env) == env


@pytest.mark.parametrize("kind", ["missing", "relative", "corrupt", "extra-field"])
def test_configured_identity_fails_closed(tmp_path, kind):
    path = tmp_path / "identity" / "build-identity.json"
    value = path
    if kind == "relative":
        value = "identity/build-identity.json"
        _identity_file(tmp_path)
    elif kind == "corrupt":
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
    elif kind == "extra-field":
        _identity_file(tmp_path, dict(IDENTITY, extra=1))
    _write_env(tmp_path, CONSOLE_BACKUP_IDENTITY_FILE=value)
    with pytest.raises(ValueError, match="CONSOLE_BACKUP_IDENTITY_FILE"):
        runtime.deployment_environment(tmp_path, {})


def test_identity_requires_deploy_checkout_plugin_root(tmp_path):
    path = _identity_file(tmp_path)
    _write_env(tmp_path, CONSOLE_BACKUP_IDENTITY_FILE=path)
    (tmp_path / "plugins" / "contract.json").unlink()
    with pytest.raises(ValueError, match="插件根"):
        runtime.deployment_environment(tmp_path, {})


def test_plugin_root_conflict_is_rejected(tmp_path):
    path = _identity_file(tmp_path)
    _write_env(tmp_path, CONSOLE_BACKUP_IDENTITY_FILE=path)
    with pytest.raises(ValueError, match="CONSOLE_PLUGIN_ROOT"):
        runtime.deployment_environment(tmp_path, {"CONSOLE_PLUGIN_ROOT": str(tmp_path)})


def test_host_identity_conflict_is_rejected(tmp_path):
    path = _identity_file(tmp_path)
    other = tmp_path / "other.json"
    other.write_text(json.dumps(IDENTITY), encoding="utf-8")
    _write_env(tmp_path, CONSOLE_BACKUP_IDENTITY_FILE=path)
    with pytest.raises(ValueError, match="冲突"):
        runtime.deployment_environment(tmp_path, {"CONSOLE_BUILD_IDENTITY_FILE": str(other)})


def test_cron_line_is_unchanged_and_still_guarded():
    """不改 cron 行：身份经 .env 白名单键接入，guard_backup_cron 的识别形态不变。"""
    assert "CONSOLE_BACKUP_IDENTITY_FILE" in runtime.DEPLOY_ENV_KEYS
    assert "CONSOLE_BUILD_IDENTITY_FILE" not in runtime.DEPLOY_ENV_KEYS


class _FakeDocker:
    def __init__(self, runtime_text, static_text, rc=0):
        self.outputs = {runtime.IDENTITY_IN_IMAGE: runtime_text,
                        runtime.STATIC_IDENTITY_IN_IMAGE: static_text}
        self.rc = rc
        self.calls = []

    def __call__(self, root, *args, **kwargs):
        self.calls.append(args)
        assert args[:2] == ("docker", "exec")
        if self.rc:
            raise RuntimeError("docker exec 失败 (exit 1)")
        return subprocess.CompletedProcess(args, 0, stdout=self.outputs[args[-1]], stderr="")


def test_sync_identity_skips_when_unconfigured(tmp_path, monkeypatch):
    _write_env(tmp_path)
    monkeypatch.delenv("CONSOLE_BACKUP_IDENTITY_FILE", raising=False)
    fake = _FakeDocker("", "")
    monkeypatch.setattr(runtime, "run_command", fake)
    assert runtime.sync_identity(tmp_path)["status"] == "skipped"
    assert fake.calls == []


def test_sync_identity_writes_atomically_from_running_container(tmp_path, monkeypatch):
    target = tmp_path / "identity" / "build-identity.json"
    _write_env(tmp_path, CONSOLE_BACKUP_IDENTITY_FILE=target, CONSOLE_IDENTITY_CONTAINER="iso-app")
    text = json.dumps(IDENTITY, indent=2) + "\n"
    fake = _FakeDocker(text, text)
    monkeypatch.setattr(runtime, "run_command", fake)
    result = runtime.sync_identity(tmp_path)
    assert result["status"] == "synced" and result["container"] == "iso-app"
    assert result["composition_digest"] == "c" * 64
    assert target.read_text(encoding="utf-8") == text
    assert oct(target.stat().st_mode & 0o777) == "0o644"
    assert not list(target.parent.glob(".identity-*"))
    # 同步后的文件即可作为备份身份。
    assert runtime.deployment_environment(tmp_path, {})["CONSOLE_BUILD_IDENTITY_FILE"] == str(target)


@pytest.mark.parametrize("runtime_doc, static_doc, rc, match", [
    (IDENTITY, dict(IDENTITY, composition_digest="d" * 64), 0, "不一致"),
    ("{broken", IDENTITY, 0, "格式错误"),
    (IDENTITY, IDENTITY, 1, "docker exec"),
])
def test_sync_identity_never_replaces_good_file_on_failure(tmp_path, monkeypatch,
                                                         runtime_doc, static_doc, rc, match):
    target = _identity_file(tmp_path)
    before = target.read_text(encoding="utf-8")
    _write_env(tmp_path, CONSOLE_BACKUP_IDENTITY_FILE=target)
    as_text = [doc if isinstance(doc, str) else json.dumps(doc) for doc in (runtime_doc, static_doc)]
    monkeypatch.setattr(runtime, "run_command", _FakeDocker(*as_text, rc=rc))
    with pytest.raises((ValueError, RuntimeError), match=match):
        runtime.sync_identity(tmp_path)
    assert target.read_text(encoding="utf-8") == before


def test_deploy_script_syncs_identity_after_accept():
    script = (Path(__file__).resolve().parent / "deploy.sh").read_text(encoding="utf-8")
    accept = script.index("backup_runtime.py\" deploy-accept")
    sync = script.index("backup_runtime.py\" sync-identity")
    assert accept < sync
