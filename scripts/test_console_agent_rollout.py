#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""console_agent_rollout 回归：命令生成、fail-closed 闸门、secret 纪律。

全部用 fake runner / monkeypatch 台账解析，**绝不触真实 SSH/SCP**。重点锁死：

- 默认 dry-run：不带 --execute 时 main() 一次 runner 都不调；
- 生成的命令里**永远不出现** token 值（只有 ``$CONSOLE_AGENT_ENROLL_TOKEN``
  形式的 env 名字面量）；
- fail closed：非 Linux / arch 不符 / 无 systemctl / install 遇既有安装 /
  未 enrol 且 token env 为空——全部拒绝执行后续阶段；
- 备份计划不含 identity.json；install/switch 分叉正确；
- verify 覆盖 systemctl 之外的实际上报核对（PG last_seen 只读 SQL）。
"""
import contextlib
import io
import json
import os
import pathlib
import shlex
import subprocess
import sys

import pytest

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
BACKEND_DIR = SCRIPTS_DIR.parent / "console" / "backend"
for _path in (str(BACKEND_DIR), str(SCRIPTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import console_agent_rollout as rollout  # noqa: E402
import machine_config as mc  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_registry():
    mc.invalidate()
    yield
    mc.invalidate()


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    """5 台目标机器的最小台账（ssh/ssh_key 与 machines.yaml 同形状）。"""
    path = tmp_path / "machines.yaml"
    path.write_text(
        "- id: txy\n  label: TXY\n  role: server\n  ssh: root@10.0.0.1\n"
        "- id: aly\n  label: ALY\n  role: server\n  ssh: root@10.0.0.2\n"
        "- id: us\n  label: US\n  role: server\n  ssh: ubuntu@10.0.0.3\n"
        "  ssh_key: id_ed25519\n"
        "- id: kr\n  label: KR\n  role: node\n  ssh: admin@10.0.0.4\n"
        "  ssh_key: aws-kr.pem\n"
        "- id: sg\n  label: SG\n  role: node\n  ssh: admin@10.0.0.5\n"
        "  ssh_key: aws-sg.pem\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONSOLE_MACHINES_PATH", str(path))
    monkeypatch.setenv("CONSOLE_SSH_KEY_DIR", str(tmp_path / "keys"))
    (tmp_path / "keys").mkdir()
    for name in ("id_ed25519", "aws-kr.pem", "aws-sg.pem"):
        (tmp_path / "keys" / name).write_text("key", encoding="utf-8")
    mc.invalidate()
    yield path
    mc.invalidate()


class FakeRunner:
    """替 subprocess.run：记录 argv/input，按脚本返回码与 stdout。"""

    def __init__(self, outputs=None):
        self.calls = []
        self.outputs = outputs or {}

    def __call__(self, argv, input=None, **kwargs):
        self.calls.append({"argv": list(argv), "input": input})
        argv_str = " ".join(str(a) for a in argv)
        for needle, result in self.outputs.items():
            if needle in argv_str:
                returncode, stdout = result
                return _Completed(returncode, stdout)
        return _Completed(0, "OS=Linux\nARCH=x86_64\nSYSTEMCTL=ok\nBIN=absent\nCFG=absent\n"
                             "UNIT=absent\nIDENT=absent\nACTIVE=unknown")

    def last(self):
        return self.calls[-1]


class _Completed:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class _UsageExit(SystemExit):
    """argparse parser.error 的退出（stderr 合并进 output 供断言）。"""


def _main_capture(argv):
    buffer = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(err):
        try:
            code = rollout.main(argv)
        except SystemExit as exc:  # parser.error / --help 的用法退出
            if exc.code not in (None, 0, 2):
                raise
            code = rollout.EXIT_USAGE if exc.code == 2 else (exc.code or 0)
            err.seek(0)
            buffer.write(err.read())
    return code, buffer.getvalue()


def _plan(argv):
    """收集 main() 的 stdout（dry-run 审计视图）。"""
    return _main_capture(argv)


# ---------------------------------------------------------------------------
# 命令生成
# ---------------------------------------------------------------------------


def test_ssh_argv_uses_ledger_and_key(ledger):
    argv = rollout.ssh_argv("kr", "uname -s")
    assert argv[0] == "ssh"
    assert "-i" in argv
    assert argv[argv.index("-i") + 1].endswith("aws-kr.pem")
    assert argv[-2] == "admin@10.0.0.4"
    # 非 root 机器（SUDO_BY_MACHINE）整条远端命令提进 root shell
    assert argv[-1] == "sudo -n sh -c 'uname -s'"
    for flag in ("-o", "BatchMode=yes", "StrictHostKeyChecking=yes"):
        assert flag in argv


def test_ssh_argv_root_machine_has_no_sudo(ledger):
    assert rollout.ssh_argv("txy", "uname -s")[-1] == "uname -s"
    assert rollout.ssh_argv("aly", "uname -s")[-1] == "uname -s"


def test_ssh_argv_without_target_fails_closed(ledger, monkeypatch):
    monkeypatch.setattr(mc, "ssh_target", lambda mid: None if mid == "txy" else "root@10.0.0.1")
    with pytest.raises(rollout.PreflightError):
        rollout.ssh_argv("txy")


def test_detect_script_covers_all_tags(ledger):
    script = rollout.remote_detect_script()
    for tag in rollout.DETECT_TAGS.values():
        assert tag in script


def test_parse_detection_defaults_conservative():
    detection = rollout.parse_detection("")
    assert detection.os_name == ""
    assert not detection.installed()
    assert not detection.systemctl
    detection = rollout.parse_detection(
        "OS=Linux\nARCH=aarch64\nSYSTEMCTL=ok\nBIN=present\nCFG=absent\nUNIT=present\nIDENT=absent\nactive\n"
    )
    assert detection.os_name == "Linux"
    assert detection.goarch() == "arm64"
    assert detection.installed()
    assert detection.existing_paths() == [rollout.REMOTE_BIN, rollout.REMOTE_UNIT]


def test_agent_config_json_has_no_secrets(ledger):
    payload = json.loads(rollout.agent_config_json("txy", "http://10.9.9.9:8081", "/data"))
    assert payload["machine_id"] == "txy"
    assert payload["server_url"] == "http://10.9.9.9:8081"
    assert payload["disk_path"] == "/data"
    blob = json.dumps(payload)
    assert "token" not in blob.lower()
    assert "secret" not in blob.lower()


# ---------------------------------------------------------------------------
# plan_install：备份/上传/enrol/安装命令
# ---------------------------------------------------------------------------


def _fresh():
    return rollout.Detection(os_name="Linux", uname_arch="x86_64", systemctl=True)


def _switched():
    return rollout.Detection(os_name="Linux", uname_arch="x86_64", systemctl=True,
                             binary=True, config=True, unit=True)


def test_install_fresh_has_no_backup_or_stop(ledger):
    steps = rollout.plan_install("txy", _fresh(), "bin", "http://s", "/", "S", "ENV", False)
    labels = [step.label for step in steps]
    assert "backup" not in labels and "stop-service" not in labels
    assert labels[0] == "upload-binary"


def test_install_switch_backs_up_existing_without_identity(ledger):
    steps = rollout.plan_install("txy", _switched(), "bin", "http://s", "/", "S", "ENV", False)
    backup = next(step for step in steps if step.label == "backup")
    joined = " ".join(backup.argv)
    assert "cp -a --parents" in joined
    assert "/opt/.trash/console-stack/S" in joined
    assert rollout.REMOTE_IDENTITY not in joined.replace(
        "/var/lib/console-agent/config.yaml", "")  # 身份文件绝不进备份
    stop = next(step for step in steps if step.label == "stop-service")
    assert "systemctl stop console-agent" in " ".join(stop.argv)


def test_backup_never_contains_identity_path(ledger):
    for detection in (_fresh(), _switched()):
        for step in rollout.plan_install("txy", detection, "bin", "http://s", "/", "S", "ENV", False):
            if step.label == "backup":
                assert rollout.REMOTE_IDENTITY not in " ".join(step.argv)


def test_enrol_step_pipes_token_via_stdin_only(ledger):
    steps = rollout.plan_install("txy", _fresh(), "bin", "http://s", "/", "S",
                                 "MY_TOKEN_ENV", False)
    enrol = next(step for step in steps if step.label == "enrol")
    joined = " ".join(enrol.argv)
    assert 'CONSOLE_AGENT_ENROLL_TOKEN="$tok"' in joined
    assert "read -r tok" in joined
    assert enrol.stdin_env == "MY_TOKEN_ENV"
    # 命令串里只有变量名字面量：token 值没有传播通道
    assert "MY_TOKEN_ENV" not in joined


def test_skip_enrol_replaces_enrol_step(ledger):
    steps = rollout.plan_install("txy", _fresh(), "bin", "http://s", "/", "S",
                                 "MY_TOKEN_ENV", True)
    labels = [step.label for step in steps]
    assert "enrol" not in labels and "skip-enrol" in labels


def test_rm_usage_is_scoped_to_uploaded_file(ledger):
    steps = rollout.plan_install("txy", _fresh(), "bin", "http://s", "/", "S", "ENV", False)
    for step in steps:
        joined = " ".join(step.argv)
        if "rm " in joined:
            assert joined.count("rm -f") == 1
            assert "/tmp/console-agent.upload." in joined
            assert "rm -rf" not in joined


# ---------------------------------------------------------------------------
# fail-closed 闸门
# ---------------------------------------------------------------------------


def test_check_detection_rejects_non_linux():
    with pytest.raises(rollout.PreflightError):
        rollout.check_detection(rollout.Detection(os_name="Darwin", uname_arch="x86_64", systemctl=True))


def test_check_detection_rejects_unknown_arch():
    with pytest.raises(rollout.PreflightError):
        rollout.check_detection(rollout.Detection(os_name="Linux", uname_arch="riscv64", systemctl=True))


def test_check_detection_rejects_arch_mismatch():
    with pytest.raises(rollout.PreflightError):
        rollout.check_detection(
            rollout.Detection(os_name="Linux", uname_arch="x86_64", systemctl=True),
            expect_arch="arm64",
        )


def test_check_detection_rejects_missing_systemd():
    with pytest.raises(rollout.PreflightError):
        rollout.check_detection(rollout.Detection(os_name="Linux", uname_arch="x86_64", systemctl=False))


def test_install_rejects_existing_installation(ledger, monkeypatch, tmp_path):
    binary = tmp_path / "bin"
    binary.write_text("x")
    runner = FakeRunner()
    runner.outputs = {"uname": (0, "OS=Linux\nARCH=x86_64\nSYSTEMCTL=ok\nBIN=present\nCFG=absent\n"
                                    "UNIT=absent\nIDENT=absent\nACTIVE=unknown")}
    monkeypatch.setattr(rollout.subprocess, "run", runner)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = rollout.main(["install", "--machine", "txy", "--binary", str(binary), "--execute"])
    assert code == rollout.EXIT_FAILED
    assert "拒绝覆盖" in buffer.getvalue()
    assert len(runner.calls) == 1  # 只跑了探测，没跑任何安装命令


def test_switch_rejects_fresh_machine(ledger, monkeypatch, tmp_path):
    binary = tmp_path / "bin"
    binary.write_text("x")
    runner = FakeRunner()
    monkeypatch.setattr(rollout.subprocess, "run", runner)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = rollout.main(["switch", "--machine", "txy", "--binary", str(binary), "--execute"])
    assert code == rollout.EXIT_FAILED
    assert "无从切换" in buffer.getvalue()


def test_install_requires_token_env_when_not_enrolled(ledger, monkeypatch, tmp_path):
    binary = tmp_path / "bin"
    binary.write_text("x")
    monkeypatch.delenv("CONSOLE_AGENT_ENROLL_TOKEN", raising=False)
    runner = FakeRunner()
    monkeypatch.setattr(rollout.subprocess, "run", runner)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = rollout.main(["install", "--machine", "txy", "--binary", str(binary), "--execute"])
    assert code == rollout.EXIT_FAILED
    assert "CONSOLE_AGENT_ENROLL_TOKEN" in buffer.getvalue()
    assert len(runner.calls) == 1


def test_execute_runs_detection_then_install_in_order(ledger, monkeypatch, tmp_path):
    binary = tmp_path / "bin"
    binary.write_text("x")
    monkeypatch.setenv("CONSOLE_AGENT_ENROLL_TOKEN", "super-secret-token-value")
    runner = FakeRunner()
    monkeypatch.setattr(rollout.subprocess, "run", runner)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = rollout.main(["install", "--machine", "txy", "--binary", str(binary), "--execute"])
    assert code == rollout.EXIT_OK
    kinds = [call["argv"][0] for call in runner.calls]
    assert kinds[0] == "ssh"  # 探测
    assert "scp" in kinds
    assert kinds.count("ssh") >= 4  # place-binary/write-config/enrol/agent-install
    enrol_calls = [call for call in runner.calls if any(
        "console-agent enrol" in str(a) for a in call["argv"])]
    assert len(enrol_calls) == 1
    # 远端 read 行协议：stdin 必须带行尾换行
    assert enrol_calls[0]["input"] == b"super-secret-token-value\n"


def test_execute_does_not_leak_token_into_output(ledger, monkeypatch, tmp_path):
    binary = tmp_path / "bin"
    binary.write_text("x")
    monkeypatch.setenv("CONSOLE_AGENT_ENROLL_TOKEN", "super-secret-token-value")
    runner = FakeRunner()
    monkeypatch.setattr(rollout.subprocess, "run", runner)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        rollout.main(["install", "--machine", "txy", "--binary", str(binary), "--execute"])
    assert "super-secret-token-value" not in buffer.getvalue()


def test_dry_run_never_invokes_runner(ledger, monkeypatch, tmp_path):
    binary = tmp_path / "bin"
    binary.write_text("x")

    def boom(*args, **kwargs):
        raise AssertionError("dry-run 不许调用 runner")

    monkeypatch.setattr(rollout.subprocess, "run", boom)
    code, output = _plan(["install", "--machine", "txy", "--binary", str(binary)])
    assert code == rollout.EXIT_OK
    assert "upload-binary" in output
    assert "enrol" in output


def test_plan_never_invokes_runner_even_with_execute(ledger, monkeypatch, tmp_path):
    binary = tmp_path / "bin"
    binary.write_text("x")

    def boom(*args, **kwargs):
        raise AssertionError("plan 阶段不许调用 runner")

    monkeypatch.setattr(rollout.subprocess, "run", boom)
    code, output = _plan(["plan", "--machine", "txy", "--binary", str(binary), "--execute"])
    assert code == rollout.EXIT_OK
    assert "rollback" in output


# ---------------------------------------------------------------------------
# verify / rollback
# ---------------------------------------------------------------------------


def test_verify_covers_beyond_systemctl(ledger):
    steps = rollout.plan_verify("txy")
    labels = [step.label for step in steps]
    assert "is-active" in labels and "pg-last-seen" in labels
    pg = next(step for step in steps if step.label == "pg-last-seen")
    joined = " ".join(pg.argv)
    assert "last_seen_at" in joined and "txy" in joined


def test_verify_is_read_only(ledger):
    for step in rollout.plan_verify("txy"):
        joined = " ".join(step.argv)
        assert "systemctl restart" not in joined
        assert "systemctl stop" not in joined
        assert "rm " not in joined


def test_rollback_restores_from_backup_and_reloads(ledger):
    steps = rollout.plan_rollback("txy", "S")
    labels = [step.label for step in steps]
    # 先只读校验，再停服务，最后按 MANIFEST 恢复——顺序固定
    assert labels == ["verify-backup", "stop-service", "restore"]
    verify = next(step for step in steps if step.label == "verify-backup")
    verify_joined = " ".join(verify.argv)
    assert "/opt/.trash/console-stack/S/MANIFEST" in verify_joined
    restore = next(step for step in steps if step.label == "restore")
    joined = " ".join(restore.argv)
    assert "/opt/.trash/console-stack/S" in joined
    assert "cp -a" in joined and "MANIFEST" in joined  # 恢复范围=清单
    assert rollout.REMOTE_IDENTITY not in joined
    assert "daemon-reload" in joined
    assert "|| true" not in joined  # 恢复/重启失败必须非零
    assert "rm -rf" not in joined


def test_rollback_requires_stamp(ledger):
    code, output = _plan(["rollback", "--machine", "txy"])
    assert code == rollout.EXIT_USAGE
    assert "--stamp" in output


def test_rollback_execute_restores(ledger, monkeypatch):
    runner = FakeRunner()
    monkeypatch.setattr(rollout.subprocess, "run", runner)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = rollout.main(["rollback", "--machine", "txy", "--stamp", "STAMP1", "--execute"])
    assert code == rollout.EXIT_OK
    assert len(runner.calls) == 3  # verify-backup → stop-service → restore
    assert "/opt/.trash/console-stack/STAMP1" in " ".join(runner.calls[0]["argv"])
    assert "systemctl stop console-agent" in " ".join(runner.calls[1]["argv"])
    assert "cp -a" in " ".join(runner.calls[2]["argv"])


# ---------------------------------------------------------------------------
# 用法与安全边界
# ---------------------------------------------------------------------------


def test_target_machine_choices_exclude_nas(ledger):
    parser = rollout.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--machine", "nas"])


def test_install_requires_binary(ledger):
    code, output = _plan(["install", "--machine", "txy"])
    assert code == rollout.EXIT_USAGE
    assert "--binary" in output


def test_missing_binary_file_fails_before_any_ssh(ledger, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("binary 不存在时不该触 ssh")

    monkeypatch.setattr(rollout.subprocess, "run", boom)
    code, output = _plan(["install", "--machine", "txy", "--binary", "/no/such/bin"])
    assert code == rollout.EXIT_FAILED
    assert "ABORT" in output


# ---------------------------------------------------------------------------
# R3 回归（shell 级，无秘密）：真实 /bin/sh 执行生成的远端脚本片段
# ---------------------------------------------------------------------------

NON_SECRET_TEST_TOKEN = "NON_SECRET_TEST_TOKEN"


def _extract_remote_enrol_script(machine, ledger):
    """取 plan_install 为该机器生成的 enrol 步骤远端脚本（ssh argv 的最后一段）。

    mock runner 只断言 argv 字符串，无法发现「stdin 无换行 → 远端 read 返回 1」
    这类真实 shell 行协议缺陷；这里把脚本交给本机 /bin/sh 真执行，用假 token
    验证行终止语义与退出码。
    """
    steps = rollout.plan_install(machine, _fresh(), "bin", "http://s", "/", "S",
                                 "MY_TOKEN_ENV", False)
    enrol = next(step for step in steps if step.label == "enrol")
    return enrol.argv[-1]


def _run_remote_script(script, stdin_bytes, fake_bin):
    """在临时目录真跑远端脚本片段；PATH 注入假 console-agent（无副作用）。

    返回 CompletedProcess。脚本里的 ``{bin} enrol --config {cfg}`` 解析到假实现——
    打印标记后退出，证明 enrol 在 read 成功后确实执行到。
    """
    env = dict(os.environ)
    env["PATH"] = "{}:{}".format(fake_bin, env.get("PATH", ""))
    result = subprocess.run(["/bin/sh", "-c", script], input=stdin_bytes,
                            capture_output=True, env=env)
    result.stdout_text = result.stdout.decode("utf-8", errors="replace")
    return result


@pytest.fixture()
def fake_agent_bin(tmp_path):
    """PATH 注入的假 console-agent：enrol 只打印标记，绝不触网络/机器。"""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    (bin_dir / "console-agent").write_text(
        "#!/bin/sh\nif [ \"$1\" = \"enrol\" ]; then echo ENROL_EXECUTED; exit 0; fi\n"
        "if [ \"$1\" = \"version\" ]; then echo 0.1.0; exit 0; fi\nexit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "console-agent").chmod(0o755)
    return bin_dir


def test_enrol_script_reads_token_without_trailing_newline_fails(ledger, fake_agent_bin):
    """stdin 无行尾换行时 read 返回 1，enrol 不执行——原缺陷的 shell 级复现。"""
    script = _extract_remote_enrol_script("txy", ledger).replace(
        "/usr/local/bin/console-agent", "console-agent")
    result = _run_remote_script(script, NON_SECRET_TEST_TOKEN.encode(), fake_agent_bin)
    assert result.returncode != 0
    assert "ENROL_EXECUTED" not in result.stdout_text


def test_enrol_script_reads_token_with_trailing_newline_succeeds(ledger, fake_agent_bin):
    script = _extract_remote_enrol_script("txy", ledger).replace(
        "/usr/local/bin/console-agent", "console-agent")
    result = _run_remote_script(script, "{}\n".format(NON_SECRET_TEST_TOKEN).encode(),
                                fake_agent_bin)
    assert result.returncode == 0
    assert "ENROL_EXECUTED" in result.stdout_text


def test_enrol_script_sudo_machine_also_requires_newline(ledger, fake_agent_bin):
    """sudo 机器（kr）的 enrol 分支同缺陷：无换行 read 失败，enrol 不执行。

    sudo -n 在本机不可用，把脚本里的 ``sudo -n`` 换成透传执行的假命令，只验证
    「read 成功才轮到 enrol」的行协议，不验证提权本身。
    """
    (fake_agent_bin / "sudo").write_text(
        "#!/bin/sh\nshift\nexec \"$@\"\n", encoding="utf-8")
    (fake_agent_bin / "sudo").chmod(0o755)
    script = _extract_remote_enrol_script("kr", ledger).replace(
        "/usr/local/bin/console-agent", "console-agent")
    no_newline = _run_remote_script(script, NON_SECRET_TEST_TOKEN.encode(), fake_agent_bin)
    assert no_newline.returncode != 0
    assert "ENROL_EXECUTED" not in no_newline.stdout_text
    with_newline = _run_remote_script(
        script, "{}\n".format(NON_SECRET_TEST_TOKEN).encode(), fake_agent_bin)
    assert with_newline.returncode == 0
    assert "ENROL_EXECUTED" in with_newline.stdout_text


def test_run_steps_sends_stdin_with_trailing_newline(monkeypatch):
    """run_steps 送出的 stdin 必须带行尾换行（read 行终止语义）。"""
    captured = {}

    def fake_run(argv, input=None, **kwargs):
        captured["input"] = input
        return _Completed(0, "")

    monkeypatch.setattr(rollout.subprocess, "run", fake_run)
    steps = [rollout.Step("enrol", ["ssh", "t", "read -r tok"], stdin_env="MY_TOKEN_ENV")]
    monkeypatch.setenv("MY_TOKEN_ENV", NON_SECRET_TEST_TOKEN)
    assert rollout.run_steps(steps, execute=True) == rollout.EXIT_OK
    assert captured["input"] == "{}\n".format(NON_SECRET_TEST_TOKEN).encode()


def _local_ssh_shim(fake_bin):
    """替 subprocess.run：ssh 调用改为本地 /bin/sh 真执行远端脚本段。

    保持 input=stdin 原样传递——真实 run_steps() 生成的 stdin 数据直接决定
    远端 ``read`` 的成败，这正是在 mock runner 下漏掉的原缺陷链。远端绝对路径
    ``/usr/local/bin/console-agent`` 重定向为 PATH 解析的假实现。
    """
    real_run = subprocess.run

    def run(argv, input=None, **kwargs):
        if argv and argv[0] == "ssh":
            script = argv[-1].replace(rollout.REMOTE_BIN, "console-agent")
            env = dict(os.environ)
            env["PATH"] = "{}:{}".format(fake_bin, env.get("PATH", ""))
            # 用 real_run：monkeypatch 换掉的是全局 subprocess.run，
            # 再经模块属性调用会掉回本 shim 的 fallback 分支并丢掉 env。
            return real_run(["/bin/sh", "-c", script], input=input,
                            capture_output=True, env=env)
        return real_run(argv, input=input, capture_output=True)

    return run


def test_run_steps_end_to_end_enrol_executes_with_real_shell(ledger, fake_agent_bin, monkeypatch):
    """真实 run_steps() + 本地 /bin/sh 执行远端 enrol 脚本（root 机器 txy）。

    原缺陷形态：stdin 无换行 → read 返回 1 → enrol 不执行 → run_steps 判失败。
    修复后整链必须走通（enrol 真执行、退出码 0）。
    """
    monkeypatch.setattr(rollout.subprocess, "run", _local_ssh_shim(fake_agent_bin))
    monkeypatch.setenv("MY_TOKEN_ENV", NON_SECRET_TEST_TOKEN)
    steps = [step for step in rollout.plan_install(
        "txy", _fresh(), "bin", "http://s", "/", "S", "MY_TOKEN_ENV", False)
        if step.label == "enrol"]
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = rollout.run_steps(steps, execute=True)
    assert code == rollout.EXIT_OK
    assert "NON_SECRET_TEST_TOKEN" not in buffer.getvalue()
    assert "FAILED" not in buffer.getvalue()


def test_run_steps_end_to_end_enrol_sudo_machine(ledger, fake_agent_bin, monkeypatch):
    """sudo 机器（kr）同链路：普通用户 read 后经 sudo env 提权执行 enrol。

    sudo -n 用 PATH 假命令透传，只验证行协议与 run_steps 整链，不验证提权。
    """
    (fake_agent_bin / "sudo").write_text(
        "#!/bin/sh\nshift\nexec \"$@\"\n", encoding="utf-8")
    (fake_agent_bin / "sudo").chmod(0o755)
    monkeypatch.setattr(rollout.subprocess, "run", _local_ssh_shim(fake_agent_bin))
    monkeypatch.setenv("MY_TOKEN_ENV", NON_SECRET_TEST_TOKEN)
    steps = [step for step in rollout.plan_install(
        "kr", _fresh(), "bin", "http://s", "/", "S", "MY_TOKEN_ENV", False)
        if step.label == "enrol"]
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = rollout.run_steps(steps, execute=True)
    assert code == rollout.EXIT_OK
    assert "FAILED" not in buffer.getvalue()


# ---------------------------------------------------------------------------
# R3 回归（回退）：隔离临时目录真实执行生成的 restore 脚本
# ---------------------------------------------------------------------------

MANAGED_REL_PATHS = (
    "usr/local/bin/console-agent",
    "etc/console-agent/config.yaml",
    "etc/systemd/system/console-agent.service",
)


def _extract_restore_script(stamp="STAMP1"):
    """只取 restore 步骤的远端脚本段（ssh argv 最后一段）。

    绝不把整条 ssh argv 交给本地 shell——那会真的发起 SSH（BatchMode 打不通
    也是对未知主机的网络尝试），违反「测试不触真实 SSH/SCP」边界。
    """
    steps = rollout.plan_rollback("txy", stamp)
    restore = next(step for step in steps if step.label == "restore")
    return restore.argv[-1]


def _rollback_sandbox(tmp_path):
    """本地复刻受管路径树与备份根；systemctl 换成记录调用的假命令。

    返回 (fake_bin, logs_dir, backup_root, managed_root)。managed 树预置三个
    受管文件（内容 MANAGED:<rel>），备份树由各用例按需创建。
    """
    root = tmp_path / "rollback-sandbox"
    managed_root = root / "managed"
    backup_root = root / "opt" / ".trash" / "console-stack"
    fake_bin = root / "fake-bin"
    logs = root / "logs"
    fake_bin.mkdir(parents=True)
    for rel in MANAGED_REL_PATHS:
        target = managed_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("MANAGED:" + rel, encoding="utf-8")
    (fake_bin / "systemctl").write_text(
        "#!/bin/sh\nmkdir -p {logdir}\necho \"$*\" >> {logdir}/systemctl.log\nexit 0\n".format(
            logdir=shlex.quote(str(logs))),
        encoding="utf-8",
    )
    (fake_bin / "systemctl").chmod(0o755)
    return fake_bin, logs, backup_root, managed_root


def _sandbox_run_rollback(monkeypatch, fake_bin, logs, backup_root, managed_root, stamp):
    """真实 run_steps() 执行完整 rollback 计划，远端脚本映射进沙箱执行。

    stamp 目录与 cp 目标 ``/`` 重定向到临时树；systemctl 由 PATH 假命令记录。
    返回 (退出码, stdout)。restore 脚本里的绝对路径 ``/usr/local/bin/...`` 经
    sh 按沙箱相对路径解析（cwd=managed_root），因此 cp 行为与远端一致。
    """
    real_run = subprocess.run

    def run(argv, input=None, **kwargs):
        if argv and argv[0] == "ssh":
            script = argv[-1]
            if "/opt/.trash/console-stack/" in script:  # rollback 步骤做沙箱映射
                script = script.replace(
                    shlex.quote("/opt/.trash/console-stack/" + stamp),
                    shlex.quote(str(backup_root / stamp)))
                if "cp -a" in script:  # restore 步骤：绝对目的地 /$rel 重定向进沙箱
                    managed = str(managed_root)
                    assert script.count('mkdir -p "/$(dirname "$rel")"') == 1
                    assert script.count(' "/$rel"') == 1  # cp 目的地；替换不生效即测试失真
                    script = script.replace(
                        'mkdir -p "/$(dirname "$rel")"',
                        'mkdir -p "{}/$(dirname "$rel")"'.format(managed))
                    script = script.replace(' "/$rel"', ' "{}/$rel"'.format(managed))
            env = dict(os.environ)
            env["PATH"] = "{}:{}".format(fake_bin, env.get("PATH", ""))
            return real_run(["/bin/sh", "-c", script], capture_output=True,
                            text=True, env=env, cwd=str(managed_root))
        return real_run(argv, input=input, capture_output=True)

    monkeypatch.setattr(rollout.subprocess, "run", run)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = rollout.run_steps(rollout.plan_rollback("txy", stamp), execute=True)
    return code, buffer.getvalue()


def _make_backup(stamp_dir, rels):
    stamp_dir.mkdir(parents=True)
    for rel in rels:
        src = stamp_dir / rel
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("BACKUP:" + rel, encoding="utf-8")
    (stamp_dir / "MANIFEST").write_text("".join(rel + "\n" for rel in rels), encoding="utf-8")


def test_rollback_restore_missing_backup_fails_nonzero_and_keeps_service(
        ledger, monkeypatch, tmp_path):
    """备份目录不存在：rollback 必须非零退出，且校验失败发生在停服务之前。"""
    fake_bin, logs, backup_root, managed_root = _rollback_sandbox(tmp_path)
    code, output = _sandbox_run_rollback(
        monkeypatch, fake_bin, logs, backup_root, managed_root, "MISSING")
    assert code == rollout.EXIT_FAILED
    log_file = logs / "systemctl.log"
    if log_file.exists():  # 校验先于停服务：失败路径上不该有任何 systemctl 调用
        logged = log_file.read_text(encoding="utf-8")
        assert "stop" not in logged and "restart" not in logged
    for rel in MANAGED_REL_PATHS:  # 受管文件原样保留，没有被半恢复
        assert (managed_root / rel).read_text(encoding="utf-8") == "MANAGED:" + rel


def test_rollback_restore_incomplete_backup_fails_nonzero(ledger, monkeypatch, tmp_path):
    """MANIFEST 缺失/清单内文件不完整：同样非零退出、不动服务。"""
    fake_bin, logs, backup_root, managed_root = _rollback_sandbox(tmp_path)
    stamp_dir = backup_root / "HALF"
    stamp_dir.mkdir(parents=True)  # 有目录、无 MANIFEST、无文件
    code, output = _sandbox_run_rollback(
        monkeypatch, fake_bin, logs, backup_root, managed_root, "HALF")
    assert code == rollout.EXIT_FAILED
    log_file = logs / "systemctl.log"
    if log_file.exists():
        logged = log_file.read_text(encoding="utf-8")
        assert "stop" not in logged and "restart" not in logged
    for rel in MANAGED_REL_PATHS:
        assert (managed_root / rel).read_text(encoding="utf-8") == "MANAGED:" + rel


def test_rollback_restore_complete_backup_restores_managed_files(
        ledger, monkeypatch, tmp_path):
    """备份完整（MANIFEST 齐全）：恢复全部受管文件、daemon-reload 并重启。"""
    fake_bin, logs, backup_root, managed_root = _rollback_sandbox(tmp_path)
    _make_backup(backup_root / "FULL", MANAGED_REL_PATHS)
    code, output = _sandbox_run_rollback(
        monkeypatch, fake_bin, logs, backup_root, managed_root, "FULL")
    assert code == rollout.EXIT_OK, output
    for rel in MANAGED_REL_PATHS:
        assert (managed_root / rel).read_text(encoding="utf-8") == "BACKUP:" + rel
    logged = (logs / "systemctl.log").read_text(encoding="utf-8")
    assert "daemon-reload" in logged and "restart console-agent" in logged


def test_rollback_restore_partial_backup_restores_only_manifest_scope(
        ledger, monkeypatch, tmp_path):
    """部分安装：MANIFEST 只列 config/unit——恢复范围按清单，不在清单的路径不动。"""
    fake_bin, logs, backup_root, managed_root = _rollback_sandbox(tmp_path)
    partial = ("etc/console-agent/config.yaml", "etc/systemd/system/console-agent.service")
    _make_backup(backup_root / "PART", partial)
    code, output = _sandbox_run_rollback(
        monkeypatch, fake_bin, logs, backup_root, managed_root, "PART")
    assert code == rollout.EXIT_OK, output
    for rel in partial:
        assert (managed_root / rel).read_text(encoding="utf-8") == "BACKUP:" + rel
    # binary 不在备份清单里：保持现状，不去备份外找「应有文件」，也不报缺失
    assert (managed_root / "usr/local/bin/console-agent").read_text(
        encoding="utf-8") == "MANAGED:usr/local/bin/console-agent"
    logged = (logs / "systemctl.log").read_text(encoding="utf-8")
    assert "restart console-agent" in logged


def test_rollback_restore_does_not_swallow_restart_failure(ledger):
    """restore 脚本不得以 ``|| true`` 兜底吞掉恢复/重启失败（原伪成功缺陷）。"""
    script = _extract_restore_script()
    assert "restart console-agent || true" not in script
    assert "|| true" not in script
    assert "cp -a" in script


def test_ssh_opts_match_service_manager(ledger):
    import service_manager as sm

    assert rollout.SSH_OPTS == sm.SSH_OPTS
