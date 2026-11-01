#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""§8.3 准备阶段自测（V4-02）。

覆盖：release 逐文件校验、YAML 收编、root 推进到**最终 SHA**（快进 / 收编后
rebase / 分叉拒绝）、运行 env 阶段配置、镜像预构建与 PG 服务创建失败时的 fail
closed。全部用例在 tmp_path 合成仓库上进行；镜像构建与 PG 服务用替换命令
（真 `docker compose build` 由 CI 的 docker/build job 承担），其余步骤都是真的。
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import cutover_prepare as prep  # noqa: E402
import test_deploy_safety as tds  # noqa: E402


def _no_docker_env(monkeypatch):
    """把外部命令换成可观测的本地替换命令（不是「跳过」，是可控的等价物）。"""
    monkeypatch.setenv("CONSOLE_CUTOVER_BUILD_CMD", '/bin/sh -c "echo built > build.txt"')
    monkeypatch.setenv("CONSOLE_CUTOVER_IMAGE_ID_CMD", '/bin/sh -c "echo sha256:fixture"')
    monkeypatch.setenv("CONSOLE_CUTOVER_PG_UP_CMD", '/bin/sh -c "echo up"')
    monkeypatch.setenv("CONSOLE_CUTOVER_PG_READY_CMD", '/bin/sh -c "exit 0"')


def _fixture(tmp_path, *, yaml_dirty=True):
    root = tds.make_repo(tmp_path)
    for relative in prep.RELEASE_FILES:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {relative}\n", encoding="utf-8")
    tds.git(root, "add", "-A")
    tds.git(root, "-c", "user.email=t@test", "-c", "user.name=t", "commit", "-qm", "old")
    old = tds.git(root, "rev-parse", "HEAD").strip()
    (root / "console/backend/app.py").write_text("# candidate\n", encoding="utf-8")
    tds.git(root, "add", "-A")
    tds.git(root, "-c", "user.email=t@test", "-c", "user.name=t", "commit", "-qm", "candidate")
    target = tds.git(root, "rev-parse", "HEAD").strip()
    tds.git(root, "checkout", "-q", old)
    if yaml_dirty:
        (root / "console/panels.yaml").write_text("- id: ui-edited\n", encoding="utf-8")
    (root / ".env").write_text("CONSOLE_MAINTENANCE_MODE=normal\n", encoding="utf-8")
    return root, old, target


def _release(scratch, root, target):
    release = scratch / "release"
    release.mkdir(parents=True)
    archive = subprocess.run(["git", "archive", target], cwd=str(root),
                             capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(release)], input=archive.stdout, check=True)
    return release


def test_prepare_moves_root_and_writes_phase_env(tmp_path, monkeypatch):
    """收编 + rebase → 最终 SHA 与候选不同但代码一致；env 写入三个阶段键之一。"""
    _no_docker_env(monkeypatch)
    root, old, target = _fixture(tmp_path)
    release = _release(tmp_path, root, target)
    receipt = prep.prepare(root, requested_commit=target, release_dir=release,
                           env_file=root / ".env", instance_role="authority")
    final = tds.git(root, "rev-parse", "HEAD").strip()
    assert receipt["requested_commit"] == target
    assert receipt["final_commit"] == final and final != target   # 收编提交在候选之上
    assert receipt["root_move"]["mode"] == "rebase-adoption"
    assert receipt["release"]["files_checked"] == len(prep.RELEASE_FILES)
    assert receipt["yaml_adoption"]["adopted"] is True
    assert receipt["root_verification"]["files_checked"] == len(prep.RELEASE_FILES)
    env_lines = (root / ".env").read_text(encoding="utf-8")
    assert "CONSOLE_STORAGE_ENGINE=postgres" in env_lines
    assert "CONSOLE_INSTANCE_ROLE=authority" in env_lines
    # 维护模式不在这里写（由冻结步骤负责），值保持原样
    assert "CONSOLE_MAINTENANCE_MODE=normal" in env_lines
    assert receipt["image"]["image_id"] == "sha256:fixture"
    assert receipt["pg_service"]["ready_via"] == "command"
    # 收编提交真的进了历史（可审计：最终 SHA 就是它）
    assert tds.git(root, "log", "-1", "--format=%s").strip().startswith("chore(nas)")


def test_prepare_fast_forwards_when_no_local_adoption(tmp_path, monkeypatch):
    _no_docker_env(monkeypatch)
    root, _old, target = _fixture(tmp_path, yaml_dirty=False)
    release = _release(tmp_path, root, target)
    receipt = prep.prepare(root, requested_commit=target, release_dir=release,
                           env_file=root / ".env", instance_role="client")
    assert receipt["yaml_adoption"]["adopted"] is False
    assert receipt["root_move"]["mode"] == "fast-forward"
    assert receipt["final_commit"] == target
    assert "CONSOLE_INSTANCE_ROLE=client" in (root / ".env").read_text(encoding="utf-8")


def test_prepare_refuses_tampered_release_before_touching_root(tmp_path, monkeypatch):
    _no_docker_env(monkeypatch)
    root, _old, target = _fixture(tmp_path)
    release = _release(tmp_path, root, target)
    (release / "console/backend/app.py").write_text("# tampered\n", encoding="utf-8")
    with pytest.raises(prep.PrepareError, match="release 校验失败"):
        prep.prepare(root, requested_commit=target, release_dir=release,
                     env_file=root / ".env", instance_role="authority")
    assert tds.git(root, "rev-parse", "HEAD").strip() != target        # 没动 root
    assert "CONSOLE_STORAGE_ENGINE" not in (root / ".env").read_text(encoding="utf-8")


def test_prepare_refuses_divergent_root(tmp_path, monkeypatch):
    """分叉且无法自动收敛 → 拒绝（不自动 reset/覆盖，§8.3）。"""
    _no_docker_env(monkeypatch)
    root, _old, target = _fixture(tmp_path, yaml_dirty=False)
    release = _release(tmp_path, root, target)
    (root / "diverge.txt").write_text("x\n", encoding="utf-8")
    tds.git(root, "add", "-A")
    tds.git(root, "-c", "user.email=t@test", "-c", "user.name=t", "commit", "-qm", "diverge")
    with pytest.raises(prep.PrepareError, match="分叉"):
        prep.prepare(root, requested_commit=target, release_dir=release,
                     env_file=root / ".env", instance_role="authority")


def test_prepare_refuses_when_pg_service_fails(tmp_path, monkeypatch):
    """PG 服务起不来 → 拒绝，**不进入停写阶段**，且**不留下改过 env 的生产现场**。

    复核 A：env 原先在 build/up 之前写；准备失败时 `.env` 已经变成 postgres/新角色，
    下一次容器重建就会按目标态启动，而磁盘上没有任何「谁改的、原值是什么」记录。
    """
    _no_docker_env(monkeypatch)
    monkeypatch.setenv("CONSOLE_CUTOVER_PG_UP_CMD", '/bin/sh -c "exit 3"')
    root, _old, target = _fixture(tmp_path)
    release = _release(tmp_path, root, target)
    with pytest.raises(prep.PrepareError, match="PG 服务创建/启动失败"):
        prep.prepare(root, requested_commit=target, release_dir=release,
                     env_file=root / ".env", instance_role="authority")
    assert not (root / "backup/cutover/state.json").exists()
    # env 未被改动，且失败回执在盘上（含失败步骤与失败前的 env 值）
    env_text = (root / ".env").read_text(encoding="utf-8")
    assert "CONSOLE_STORAGE_ENGINE" not in env_text
    assert "CONSOLE_INSTANCE_ROLE" not in env_text
    failed = json.loads((root / prep.DEFAULT_REPORT_RELATIVE).read_text(encoding="utf-8"))
    assert failed["status"] == "failed" and failed["failed_step"] == "pg_service"
    assert failed["env_before"] == {"CONSOLE_STORAGE_ENGINE": None,
                                    "CONSOLE_INSTANCE_ROLE": None}


def test_prepare_rejects_unknown_instance_role(tmp_path, monkeypatch):
    """角色拼错必须在 prepare 就地拒绝，而不是等应用启动期才炸（复核 A）。"""
    _no_docker_env(monkeypatch)
    root, _old, target = _fixture(tmp_path)
    release = _release(tmp_path, root, target)
    with pytest.raises(prep.UsageError, match="取值非法"):
        prep.prepare(root, requested_commit=target, release_dir=release,
                     env_file=root / ".env", instance_role="prod")
    assert "CONSOLE_INSTANCE_ROLE" not in (root / ".env").read_text(encoding="utf-8")


def test_instance_role_enum_matches_runtime_mode():
    """prepare 的角色枚举必须与 `runtime_mode.INSTANCE_ROLES` 一致（防漂移）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "console" / "backend"))
    import runtime_mode
    assert tuple(prep.INSTANCE_ROLES) == tuple(runtime_mode.INSTANCE_ROLES)


def test_prepare_reuse_requires_root_to_still_match_release(tmp_path, monkeypatch):
    """复核 A：prepare 成功之后受控文件被改，重入**不得**再复用「已准备」回执。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "console" / "backend"))
    import backup_runtime as runtime

    _no_docker_env(monkeypatch)
    root, _old, target = _fixture(tmp_path)
    release = _release(tmp_path, root, target)
    final, receipt = runtime._prepare_cutover_for_cli(
        root, target, {}, release_dir=release, env_file=root / ".env",
        instance_role="authority")
    assert receipt["status"] == "ok"
    # 改一个**不在** CUTOVER_EXECUTION_FILES 里的受控文件（app.py）
    (root / "console/backend/app.py").write_text("# 被改过\n", encoding="utf-8")
    try:
        runtime._prepare_cutover_for_cli(root, target, {}, release_dir=release,
                                         env_file=root / ".env", instance_role="authority")
    except RuntimeError as exc:
        assert "最终 SHA 复核失败" in str(exc)
    else:  # 复用路径没复核就会走到这里 —— 视为失败
        raise AssertionError("受控文件被改后仍然复用了准备回执")


def test_prepare_dry_run_touches_nothing(tmp_path, monkeypatch):
    _no_docker_env(monkeypatch)
    root, old, target = _fixture(tmp_path)
    release = _release(tmp_path, root, target)
    receipt = prep.prepare(root, requested_commit=target, release_dir=release,
                           env_file=root / ".env", instance_role="authority", dry_run=True)
    assert receipt["dry_run"] is True
    assert receipt["release"]["files_checked"] == len(prep.RELEASE_FILES)
    assert tds.git(root, "rev-parse", "HEAD").strip() == old


def test_prepare_cli_requires_instance_role_and_env_file(tmp_path, monkeypatch):
    _no_docker_env(monkeypatch)
    monkeypatch.delenv("CONSOLE_CUTOVER_INSTANCE_ROLE", raising=False)
    monkeypatch.delenv("CONSOLE_CUTOVER_ENV_FILE", raising=False)
    root, _old, target = _fixture(tmp_path)
    release = _release(tmp_path, root, target)
    assert prep.main(["--root", str(root), "--target-commit", target,
                      "--release-dir", str(release)]) == 2          # 缺角色
    assert prep.main(["--root", str(root), "--target-commit", target,
                      "--release-dir", str(release), "--instance-role", "authority"]) == 2


def test_prepare_receipt_round_trips_through_controller(tmp_path, monkeypatch):
    """控制器侧的复核：回执缺项 / 最终 SHA 对不上 → 拒绝（不是「有回执就放行」）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "console" / "backend"))
    import backup_runtime as runtime

    _no_docker_env(monkeypatch)
    root, _old, target = _fixture(tmp_path)
    release = _release(tmp_path, root, target)
    final, receipt = runtime._prepare_cutover_for_cli(
        root, target, {}, release_dir=release, env_file=root / ".env",
        instance_role="authority")
    assert final == receipt["final_commit"] == tds.git(root, "rev-parse", "HEAD").strip()
    # 幂等复用：第二次不再重复 build/up（回执里 reused=True），也不再改动 git
    _final2, again = runtime._prepare_cutover_for_cli(
        root, target, {}, release_dir=release, env_file=root / ".env",
        instance_role="authority")
    assert again["reused"] is True and _final2 == final      # 幂等：不重复 build/up
    # 回执被换掉（最终 SHA 与当前 HEAD 不符）→ 不采信，重跑准备并修好回执
    bad = root / runtime.CUTOVER_PREPARE_REPORT_RELATIVE
    tampered = json.loads(bad.read_text(encoding="utf-8"))
    tampered["final_commit"] = "0" * 40
    bad.write_text(json.dumps(tampered), encoding="utf-8")
    final3, again3 = runtime._prepare_cutover_for_cli(
        root, target, {}, release_dir=release, env_file=root / ".env",
        instance_role="authority")
    assert final3 == final and not again3.get("reused")

    # 准备动作退出 0 却没写回执 → 拒绝（退出码不能当准备完成的凭证）
    root2, _old2, target2 = _fixture(tmp_path / "second")
    release2 = _release(tmp_path / "second", root2, target2)
    with pytest.raises(RuntimeError, match="回执不可读"):
        runtime._prepare_cutover_for_cli(
            root2, target2, {"prepare_cmd": ["/bin/sh", "-c", "exit 0"]},
            release_dir=release2, env_file=root2 / ".env", instance_role="authority")
