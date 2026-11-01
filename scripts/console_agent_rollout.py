#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""console-agent 逐机 rollout CLI（B6-2/3 准备；目标机器 txy/aly/kr/sg/us）。

**定位**：审计过的分阶段安装/切换/回退/验证工具，不是发布系统。生成并（显式
``--execute`` 后）执行少量 SSH/SCP 命令；**默认 dry-run**——只打印命令，不碰
任何机器。``plan`` 阶段永远只打印（含 preflight + install + verify + rollback
模板，供整轮审阅）。

阶段划分（``install``/``switch`` 执行时自动先跑 preflight）：

- ``preflight``：远端现状探测（单次 SSH、只读）：OS/arch/systemd/既有
  binary/config/unit/身份文件/service 状态。**fail closed**：非 Linux、arch
  不在构建矩阵或与 ``--arch`` 声明不符、无 systemctl、fresh install 却发现
  既有安装——一律中止，绝不「边清边装」。
- ``install``（fresh）或 ``switch``（覆盖既有安装）：先备份既有文件到
  ``/opt/.trash/console-stack/<stamp>/``（``cd / && cp -a --parents``，**绝不
  含 identity.json**——备份目录不存秘密；**全程无 rm -rf**），scp 上传 binary
  到 /tmp 再 ``install -m 0755`` 就位，写 /etc/console-agent/config.yaml
  （无秘密，0644），enrol，``console-agent install``（agent 自写加固过的
  systemd unit 并 enable --now）。
- ``verify``：只读——``systemctl is-active``、``console-agent version``、
  identity.json 权限必须是 0600、journal 尾部；并**打印** PG ``last_seen_at``
  只读核对 SQL（systemctl active ≠ 在上报；本工具不直连生产 PG，核对由
  操作者在 NAS 侧执行）。
- ``rollback``：先**只读校验**备份（stamp 目录、MANIFEST 清单、清单内文件
  齐全——失败即非零中止，绝不停了服务才发现备份不可用），再停服务、按
  MANIFEST 逐文件恢复（恢复范围=实际备份清单，部分安装=部分恢复）、
  daemon-reload 并重启；恢复/重启失败非零退出，不吞错。identity.json 不在
  备份里也不动（身份不回滚）。

**secret 纪律**（security-boundaries 契约）：enrol token 只允许经**本地环境
变量**引用（配置存变量名，默认 ``CONSOLE_AGENT_ENROLL_TOKEN``，与 agent 既有
env 名一致），执行时经 SSH **stdin** 送远端（远端 ``read`` 后放进进程 env）。
token 的值**永远不出现**在生成的命令串、日志、配置输出、备份或本工具任何
输出里——命令串里只有 ``$CONSOLE_AGENT_ENROLL_TOKEN`` 形式的字面引用。工具
拒绝任何取值 flag；execute 时该 env 缺失/为空 → 拒绝执行对应步骤。

**与正式切换的边界**：本 CLI 不切 business source、不移交 Beszel 告警、不改
monitoring_machines 台账；那些是主控在 app/NAS 侧的人工步骤。

**enrol 现实**（读 monitoring_api/monitoring_ingest 确认，2026-10-24）：HTTP
``/api/monitoring/v1/enrol`` 只做台账白名单校验、不校验 token；PG store 的
``enrol_machine`` 尚未实现（maintenance 线在补）。enrol 步骤失败时工具如实
报错中止，不伪装成功；已按 B6 约定手工 enrol 过的机器用 ``--skip-enrol``。

binary 来源：本工具不编译（不假设 Go 工具链）；``--binary`` 指向按
kernel/agent/README 交叉编译的产物（``GOOS=linux GOARCH=<arch> go build``），
preflight 把 ``--arch`` 声明与远端 ``uname -m`` 交叉核对。私钥解析沿用
machine_config（``CONSOLE_SSH_KEY_DIR`` 本机直跑时须指到仓库 ``.ssh-console/``）。

用法（全部先看后跑）::

    python3 scripts/console_agent_rollout.py plan --machine txy --binary ./console-agent-linux-amd64
    python3 scripts/console_agent_rollout.py install --machine txy --binary ./console-agent-linux-amd64 --execute
    python3 scripts/console_agent_rollout.py verify --machine txy --execute
    python3 scripts/console_agent_rollout.py rollback --machine txy --stamp 20261025T030000Z --execute

退出码：0 成功 · 1 运行失败（含远端命令非零、fail-closed 中止） · 2 用法/配置错误。
"""
import argparse
import datetime
import json
import os
import pathlib
import shlex
import subprocess
import sys

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "console" / "backend"
for _path in (str(BACKEND_DIR), str(ROOT_DIR / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import machine_config as mc  # noqa: E402

# B6-2 目标机器（nas 已由 B6-1 主控完成，不进本工具）。
TARGET_MACHINES = ("txy", "aly", "kr", "sg", "us")

# 非 root 的 sudoers 机器（免密 sudo 已实测，2026-10-24）：写路径/systemctl 步骤
# 需要提权。不能只对首命令加 ``sudo``——复合命令（&&、重定向、heredoc）的其余部分
# 仍以原用户跑。用 ``sudo -n sh -c '<整段>'`` 把整条 remote_script 提进 root shell。
SUDO_BY_MACHINE = {"kr": True, "sg": True, "us": True}

# 与 service_manager.SSH_OPTS 同值（不 import service_manager：那会拖进 flask 链）。
SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "UpdateHostKeys=no",
]

REMOTE_BIN = "/usr/local/bin/console-agent"
REMOTE_CONFIG_DIR = "/etc/console-agent"
REMOTE_CONFIG = REMOTE_CONFIG_DIR + "/config.yaml"
REMOTE_UNIT = "/etc/systemd/system/console-agent.service"
REMOTE_STATE_DIR = "/var/lib/console-agent"
REMOTE_IDENTITY = REMOTE_STATE_DIR + "/identity.json"
REMOTE_BACKUP_ROOT = "/opt/.trash/console-stack"
REMOTE_UPLOAD_FMT = "/tmp/console-agent.upload.{stamp}"

DEFAULT_SERVER_URL = "http://100.64.0.10:8081"
DEFAULT_TOKEN_ENV = "CONSOLE_AGENT_ENROLL_TOKEN"

# 远端 uname -m → agent 构建矩阵的 GOARCH。
GOARCH_BY_UNAME = {"x86_64": "amd64", "aarch64": "arm64"}

# 备份/恢复覆盖的受管路径（identity.json 永不在此列）。
MANAGED_PATHS = (REMOTE_BIN, REMOTE_CONFIG, REMOTE_UNIT)

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


class PreflightError(RuntimeError):
    """fail-closed 中止：远端现状或本地前提不满足该阶段的先决条件。"""


class Step:
    """一条可打印/可执行的命令。``stdin_env`` 非空时执行期从该本地 env 取 stdin。

    命令串里只允许 env 变量**名字面量**（``$VAR``），不允许出现任何秘密值。
    """

    def __init__(self, label, argv, note="", stdin_env="", check=True, no_sudo_wrap=False):
        self.label = label
        self.argv = list(argv)
        self.note = note
        self.stdin_env = stdin_env
        self.check = check
        self.no_sudo_wrap = no_sudo_wrap

    def display(self):
        lines = ["[${}] {}".format(self.label, " ".join(shlex.quote(a) for a in self.argv))]
        if self.stdin_env:
            lines.append("    stdin: <{} 的值经 ssh stdin 送入，不回显>".format(self.stdin_env))
        if self.note:
            lines.append("    # " + self.note)
        return "\n".join(lines)


class Detection:
    """preflight 探测结果（远端现状的只读快照）。"""

    def __init__(self, os_name="", uname_arch="", systemctl=False, binary=False,
                 config=False, unit=False, identity=False, service_active=False):
        self.os_name = os_name
        self.uname_arch = uname_arch
        self.systemctl = systemctl
        self.binary = binary
        self.config = config
        self.unit = unit
        self.identity = identity
        self.service_active = service_active

    def goarch(self):
        return GOARCH_BY_UNAME.get(self.uname_arch)

    def existing_paths(self):
        """已存在的受管文件（备份对象；identity 永不进备份）。"""
        return [path for present, path in (
            (self.binary, REMOTE_BIN),
            (self.config, REMOTE_CONFIG),
            (self.unit, REMOTE_UNIT),
        ) if present]

    def installed(self):
        return bool(self.unit or self.identity or self.binary or self.config)


DETECT_TAGS = {
    "os": "OS=", "arch": "ARCH=", "systemctl": "SYSTEMCTL=",
    "binary": "BIN=", "config": "CFG=", "unit": "UNIT=",
    "identity": "IDENT=", "active": "ACTIVE=",
}


def remote_detect_script():
    """单次 SSH 跑完的只读探测脚本；逐行打 TAG=结果，``parse_detection`` 消费。

    ``is-active`` 的输出行原样透传（active/inactive/failed/…），打上 ACTIVE= 前缀
    让解析器识别；服务不存在时输出非 active 字样，解析按「非 active 即未跑」处理。
    """
    return (
        "echo {o}$(uname -s); echo {a}$(uname -m); "
        "command -v systemctl >/dev/null 2>&1 && echo {s}ok || echo {s}missing; "
        "test -e {bin} && echo {b}present || echo {b}absent; "
        "test -e {cfg} && echo {c}present || echo {c}absent; "
        "test -e {unit} && echo {u}present || echo {u}absent; "
        "test -e {ident} && echo {i}present || echo {i}absent; "
        "echo {v}$(systemctl is-active console-agent 2>/dev/null)"
    ).format(
        o=DETECT_TAGS["os"], a=DETECT_TAGS["arch"],
        s=DETECT_TAGS["systemctl"], b=DETECT_TAGS["binary"], c=DETECT_TAGS["config"],
        u=DETECT_TAGS["unit"], i=DETECT_TAGS["identity"], v=DETECT_TAGS["active"],
        bin=shlex.quote(REMOTE_BIN), cfg=shlex.quote(REMOTE_CONFIG),
        unit=shlex.quote(REMOTE_UNIT), ident=shlex.quote(REMOTE_IDENTITY),
    )


def parse_detection(stdout):
    """探测脚本 stdout → ``Detection``；TAG 缺失按 absent/unknown（宁保守不冒进）。"""
    keys_by_tag = {tag: key for key, tag in DETECT_TAGS.items()}
    values = {}
    for line in str(stdout or "").splitlines():
        tag, sep, value = line.strip().partition("=")
        key = keys_by_tag.get(tag + "=")
        if sep and key:
            values[key] = value.strip()

    def ok(key):
        return values.get(key) in ("present", "ok")

    return Detection(
        os_name=values.get("os", ""),
        uname_arch=values.get("arch", ""),
        systemctl=ok("systemctl"),
        binary=values.get("binary") == "present",
        config=values.get("config") == "present",
        unit=values.get("unit") == "present",
        identity=values.get("identity") == "present",
        service_active=values.get("active") == "active",
    )


def ssh_argv(machine, remote_script=None, no_sudo_wrap=False):
    """ssh 命令 argv；私钥路径经 machine_config.ssh_key_path。

    非 root 机器（``SUDO_BY_MACHINE``）把整条 remote_script 提进 root shell：
    ``sudo -n sh -c '<script>'``。``no_sudo_wrap=True``（enrol 步）跳过包裹——
    该步须先以普通用户从 stdin read token，再自行 ``sudo -n env VAR=...`` 提权
    （sudo 默认 stdin:none，会把经 stdin 送入的 token 吞掉）。
    """
    argv = ["ssh", *SSH_OPTS]
    key_path = mc.ssh_key_path(machine)
    if key_path:
        argv += ["-i", key_path]
    target = mc.ssh_target(machine)
    if not target:
        raise PreflightError(
            "机器 {} 在台账里没有 ssh 目标，本工具不适用（nas 归主控）".format(machine)
        )
    argv.append(target)
    if remote_script is not None:
        if SUDO_BY_MACHINE.get(machine) and not no_sudo_wrap:
            remote_script = "sudo -n sh -c " + shlex.quote(remote_script)
        argv.append(remote_script)
    return argv


def scp_argv(machine, local_path, remote_path):
    argv = ["scp", *SSH_OPTS]
    key_path = mc.ssh_key_path(machine)
    if key_path:
        argv += ["-i", key_path]
    argv += [str(local_path), "{}:{}".format(mc.ssh_target(machine), remote_path)]
    return argv


def agent_config_json(machine, server_url, disk_path):
    """远端 config.yaml 内容（JSON）。不含任何 token/secret——token 只走 enrol env。"""
    payload = {
        "server_url": server_url,
        "machine_id": machine,
        "state_dir": REMOTE_STATE_DIR,
        "docker_socket": "/var/run/docker.sock",
        "sample_interval_sec": 300,
        "buffer_max_bytes": 104857600,
        "disk_path": disk_path,
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _stamp(moment=None):
    moment = moment or datetime.datetime.now(datetime.timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _remote_write_file(path, content, mode):
    # sudo 安全的写文件。引号地狱不可行：内容经 heredoc + sudo sh -c 会撞三层嵌套
    # 引号。改走 base64——内容编码后是单 token（无空格/引号/换行），不经 shell 展开，
    # 整条命令可安全包进 sudo sh -c '<...>'。install 建出带正确 mode 的空文件，
    # base64 -d 解码灌入（两步都在同一 sudo shell 内，权限一致）。
    import base64
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    # install 不建父目录（No such file or directory）——先 mkdir -p。
    return (
        "mkdir -p {dir} && install -m {mode} /dev/null {path} && "
        "echo {b64} | base64 -d > {path}"
    ).format(
        dir=shlex.quote(os.path.dirname(path)),
        mode=mode, path=shlex.quote(path), b64=encoded,
    )


def plan_preflight(machine):
    return [
        Step("detect", ssh_argv(machine, remote_detect_script()),
             note="只读探测远端现状（OS/arch/systemd/既有安装）"),
    ]


def check_detection(detection, expect_arch=None):
    """fail-closed 共同闸门：OS/arch/systemd。"""
    if detection.os_name != "Linux":
        raise PreflightError(
            "远端不是 Linux（uname -s={}），agent 只支持 Linux".format(detection.os_name or "?")
        )
    if not detection.goarch():
        raise PreflightError(
            "远端 arch {!r} 不在构建矩阵（{}），拒绝继续".format(
                detection.uname_arch or "?", sorted(GOARCH_BY_UNAME)
            )
        )
    if expect_arch and detection.goarch() != expect_arch:
        raise PreflightError(
            "arch 不符：--arch 声明 {}，远端 {}（uname {}）".format(
                expect_arch, detection.goarch(), detection.uname_arch
            )
        )
    if not detection.systemctl:
        raise PreflightError("远端没有 systemctl，agent 的 install/回退都依赖 systemd")


def plan_install(machine, detection, binary, server_url, disk_path, stamp, token_env, skip_enrol):
    """install（fresh）与 switch（覆盖既有）共用的阶段命令；取舍闸门在调用方。"""
    steps = []
    existing = detection.existing_paths()
    if existing:
        stamp_dir = "{}/{}".format(REMOTE_BACKUP_ROOT, stamp)
        # cp --parents 的相对形状必须从 / 解析：cd / 后按去根路径备份。
        # MANIFEST 只记实际备份的受管路径（部分安装=部分清单），rollback 按它
        # 定义可恢复范围并逐项校验，不再盲目 cp 全部受管路径。
        relatives = " ".join(shlex.quote(p.lstrip("/")) for p in existing)
        manifest = "".join(p.lstrip("/") + "\n" for p in existing)
        steps.append(Step(
            "backup",
            ssh_argv(machine, "cd / && mkdir -p {d} && cp -a --parents {p} {d}/".format(
                d=shlex.quote(stamp_dir), p=relatives)),
            note="备份既有文件到 {}（保留原路径；{} 永不进备份）".format(stamp_dir, REMOTE_IDENTITY),
        ))
        steps.append(Step(
            "backup-manifest",
            ssh_argv(machine, _remote_write_file(
                "{}/MANIFEST".format(stamp_dir), manifest, "0644")),
            note="写备份清单（仅实际备份的受管路径；rollback 按此校验与恢复）",
        ))
        steps.append(Step(
            "stop-service",
            ssh_argv(machine, "systemctl stop console-agent || true"),
            note="覆盖前先停服务（fresh install 无服务时无害）", check=False,
        ))
    upload = REMOTE_UPLOAD_FMT.format(stamp=stamp)
    steps.append(Step("upload-binary", scp_argv(machine, binary, upload),
                      note="上传 binary 到 /tmp（就位后删除该临时文件）"))
    steps.append(Step(
        "place-binary",
        ssh_argv(machine, "install -m 0755 {u} {b} && rm -f {u}".format(
            u=shlex.quote(upload), b=shlex.quote(REMOTE_BIN))),
        note="install -m 0755 就位；rm -f 只删刚上传的那个临时文件",
    ))
    steps.append(Step(
        "write-config",
        ssh_argv(machine, _remote_write_file(
            REMOTE_CONFIG, agent_config_json(machine, server_url, disk_path), "0644")),
        note="写 {}（无秘密，0644）".format(REMOTE_CONFIG),
    ))
    if not skip_enrol:
        # token 只经本地 env → ssh stdin → 远端进程 env；命令串里只有变量名字面量。
        # sudo 机器不能把 read 放进 sudo sh -c：sudo 默认 stdin:none 会吞掉 stdin，
        # 先在外层（普通用户）read 拿到 token，再以 sudo -n env VAR= 传进 root 进程环境
        # （env 是 sudoers 默认可用的命令白名单路径；token 值仍只走 stdin，不落命令串）。
        if SUDO_BY_MACHINE.get(machine):
            remote_enrol = (
                "read -r tok && sudo -n env CONSOLE_AGENT_ENROLL_TOKEN=\"$tok\" "
                "{bin} enrol --config {cfg}".format(
                    bin=shlex.quote(REMOTE_BIN), cfg=shlex.quote(REMOTE_CONFIG))
            )
        else:
            remote_enrol = (
                "read -r tok && CONSOLE_AGENT_ENROLL_TOKEN=\"$tok\" {bin} enrol --config {cfg}".format(
                    bin=shlex.quote(REMOTE_BIN), cfg=shlex.quote(REMOTE_CONFIG))
            )
        steps.append(Step(
            "enrol", ssh_argv(machine, remote_enrol, no_sudo_wrap=True), stdin_env=token_env, no_sudo_wrap=True,
            note="enrol token 从本地 env {} 经 stdin 送入；服务端校验 "
                 "CONSOLE_MONITORING_ENROLL_TOKEN 环境持有的管理员令牌并签发独立 secret"
                 "（值必须与本 env 一致，否则 401，如实报错）；sudo 机器在普通用户侧 read "
                 "再 env 传入（sudo 会吞 stdin）".format(token_env),
        ))
    else:
        steps.append(Step("skip-enrol", ["true"],
                          note="--skip-enrol：机器已 enrol，沿用既有身份"))
    # state_dir 必须存在：unit 用 ProtectSystem=strict + ReadWritePaths=<state_dir>，
    # 目录缺席时 mount namespace 建立失败（exit 226/NAMESPACE），服务反复重启。
    # enrol 正常会创建它，--skip-enrol 或 enrol 未跑到写盘时须在这里兜底。
    steps.append(Step(
        "ensure-state-dir",
        ssh_argv(machine, "mkdir -p {} && chmod 0700 {}".format(
            shlex.quote(REMOTE_STATE_DIR), shlex.quote(REMOTE_STATE_DIR))),
        note="确保 state_dir 存在（unit ReadWritePaths 依赖；identity 由 enrol 落 0600）",
    ))
    steps.append(Step(
        "agent-install",
        ssh_argv(machine, "{bin} install --config {cfg}".format(
            bin=shlex.quote(REMOTE_BIN), cfg=shlex.quote(REMOTE_CONFIG))),
        note="agent 自写加固 systemd unit 并 enable --now",
    ))
    return steps


def plan_verify(machine):
    """只读验证：systemctl active ≠ 在上报——last_seen 核对 SQL 打印给操作者。"""
    pg_sql = (
        "SELECT machine_id, agent_version, last_seen_at FROM monitoring_machines "
        "WHERE machine_id = '{}';".format(machine)
    )
    return [
        Step("is-active", ssh_argv(machine, "systemctl is-active console-agent"),
             note="systemd 视角；这只说明 unit 活着"),
        Step("version", ssh_argv(machine, "{} version".format(shlex.quote(REMOTE_BIN))),
             note="binary 自报版本（协议/版本核对）"),
        Step("identity-mode",
             ssh_argv(machine, "stat -c '%a' {}".format(shlex.quote(REMOTE_IDENTITY))),
             note="identity.json 必须是 600"),
        Step("journal-tail",
             ssh_argv(machine, "journalctl -u console-agent -n 20 --no-pager"),
             note="最近日志（上报错误会在这里出现）"),
        Step("pg-last-seen", ["/bin/sh", "-c", "cat <<'SQL'\n在 NAS 侧执行（本工具不连生产 PG）：\n{}\nSQL".format(pg_sql)],
             note="只读 SQL：last_seen_at 应在 5 分钟内、agent_version 是新版本"),
    ]


def plan_rollback(machine, stamp):
    """回退：只读校验备份 → 停服务 → 按 MANIFEST 恢复 → 重启。

    - 校验先于停服务（只读、不动任何东西）：stamp 目录、MANIFEST、清单内每个
      文件必须齐全，缺一即非零中止——不存在「服务停了才发现备份不可用」。
    - MANIFEST 只列实际备份过的受管路径（部分安装=部分清单）：恢复范围即清单，
      不在清单里的受管路径保持现状；identity.json 永不在备份/恢复范围。
    - 恢复与重启失败必须非零（无 ``|| true`` 兜底），CLI 不得伪成功。
    """
    stamp_dir = "{}/{}".format(REMOTE_BACKUP_ROOT, stamp)
    manifest = "{}/MANIFEST".format(stamp_dir)
    # 只读校验：目录可进、MANIFEST 存在且非空、清单内每个文件都在备份里。
    check = (
        "cd {d} && test -s {m} || {{ echo \"rollback: 备份不完整（stamp 或 MANIFEST 缺失）: {d}\" >&2; exit 1; }}; "
        "missing=0; while IFS= read -r rel; do "
        "test -n \"$rel\" || continue; "
        "test -f \"{d}/$rel\" || {{ echo \"rollback: 清单文件缺失: $rel\" >&2; missing=1; }}; "
        "done < {m}; test \"$missing\" -eq 0"
    ).format(d=shlex.quote(stamp_dir), m=shlex.quote(manifest))
    return [
        Step("verify-backup", ssh_argv(machine, check),
             note="只读校验备份（stamp/MANIFEST/清单文件齐全）——先于任何停服务动作"),
        Step("stop-service",
             ssh_argv(machine, "systemctl stop console-agent || true"), check=False,
             note="校验通过后才停服务（fresh install 无服务时无害）"),
        Step("restore",
             ssh_argv(machine,
                      "test -s {m} && {{ while IFS= read -r rel; do "
                      "test -n \"$rel\" || continue; "
                      "mkdir -p \"/$(dirname \"$rel\")\" && cp -a \"{d}/$rel\" \"/$rel\"; "
                      "done < {m}; }} && systemctl daemon-reload "
                      "&& systemctl restart console-agent".format(
                          m=shlex.quote(manifest), d=shlex.quote(stamp_dir))),
             note="按 MANIFEST 从 {} 恢复受管文件并重启；恢复/重启失败即非零退出，"
                  "不吞错；identity.json 不在备份里也不动".format(stamp_dir)),
    ]


def run_steps(steps, execute=False):
    """打印（dry-run）或逐条执行；execute 时失败即停。返回退出码。"""
    for step in steps:
        print(step.display())
        if not execute:
            continue
        stdin_data = None
        if step.stdin_env:
            token = os.environ.get(step.stdin_env, "")
            if not token.strip():
                print("ABORT: env {} 为空——拒绝把空 token 送进 {}".format(step.stdin_env, step.label))
                return EXIT_FAILED
            # 远端是 read -r tok && …：read 需要行终止符，无换行时返回 1，
            # enrol 根本不执行——必须补行尾换行。
            stdin_data = (token + "\n").encode("utf-8")
        result = subprocess.run(step.argv, input=stdin_data)
        if step.check and getattr(result, "returncode", 1) != 0:
            print("FAILED: {} 步骤退出码 {}".format(step.label, result.returncode))
            return EXIT_FAILED
    return EXIT_OK


def _run_detection(machine):
    """执行 preflight 探测并解析；ssh 失败按 fail-closed 中止。"""
    steps = plan_preflight(machine)
    step = steps[0]
    print(step.display())
    result = subprocess.run(step.argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise PreflightError(
            "preflight ssh 失败（退出码 {}）：{}".format(
                result.returncode, (result.stderr or "").strip()[:200]
            )
        )
    return parse_detection(result.stdout)


def _load_binary_arg(value):
    path = pathlib.Path(value).expanduser()
    if not path.is_file():
        raise PreflightError("--binary 不存在：{}（先按 kernel/agent/README 交叉编译）".format(path))
    return path.resolve()


def _print_plan(args, binary):
    """dry-run / plan：preflight + 阶段命令全部只打印。"""
    for step in plan_preflight(args.machine):
        print(step.display())
    stamp = args.stamp or _stamp()
    fresh = Detection()  # 展示假设：远端无既有安装；真实取舍以 preflight 结果为准
    if args.stage in ("install", "switch", "plan"):
        print("(以下 install/switch 命令以「远端无既有安装」为假设展示；"
              "实际执行时以 preflight 结果 fail-closed 取舍，有既有安装则先备份再替换)")
        for step in plan_install(args.machine, fresh, binary or "<binary>",
                                 args.server_url, args.disk_path, stamp,
                                 args.token_env, args.skip_enrol):
            print(step.display())
    if args.stage in ("verify", "plan"):
        print("(verify —— 只读)")
        for step in plan_verify(args.machine):
            print(step.display())
    if args.stage in ("rollback", "plan"):
        if args.stage == "rollback" and not args.stamp:
            print("rollback 需要 --stamp（install 的备份步骤输出里有）")
            return EXIT_USAGE
        print("(rollback —— 从备份恢复，identity 不动)")
        for step in plan_rollback(args.machine, stamp):
            print(step.display())
    return EXIT_OK


def _execute_stage(args, binary):
    """--execute 路径：探测 → 闸门 → 执行；任何失败都停在下一条命令之前。"""
    if args.stage == "verify":
        return run_steps(plan_verify(args.machine), execute=True)

    if args.stage == "rollback":
        if not args.stamp:
            print("rollback 需要 --stamp（install 的备份步骤输出里有）")
            return EXIT_USAGE
        return run_steps(plan_rollback(args.machine, args.stamp), execute=True)

    detection = _run_detection(args.machine)
    if args.stage == "preflight":
        check_detection(detection, expect_arch=args.arch)
        print("preflight ok: os={} arch={} (GOARCH {}) unit={} identity={} active={}".format(
            detection.os_name, detection.uname_arch, detection.goarch(),
            "present" if detection.unit else "absent",
            "present" if detection.identity else "absent",
            "yes" if detection.service_active else "no",
        ))
        return EXIT_OK

    # install / switch
    check_detection(detection, expect_arch=args.arch)
    if args.stage == "install" and detection.installed():
        raise PreflightError(
            "远端已有安装痕迹（binary/config/unit/identity 任一存在）——fresh install 拒绝覆盖；"
            "确认要覆盖请用 switch（会先备份再替换）"
        )
    if args.stage == "switch" and not detection.installed():
        raise PreflightError("远端没有任何既有安装——switch 无从切换，请用 install")
    if not args.skip_enrol and not detection.identity and not os.environ.get(args.token_env, "").strip():
        raise PreflightError(
            "机器未 enrol 且本地 env {} 为空——拒绝执行需要 token 的阶段".format(args.token_env)
        )
    stamp = args.stamp or _stamp()
    code = run_steps(
        plan_install(args.machine, detection, binary, args.server_url, args.disk_path,
                     stamp, args.token_env, args.skip_enrol),
        execute=True,
    )
    if code != EXIT_OK:
        print("备份位置（如需回退）：{}/{}".format(REMOTE_BACKUP_ROOT, stamp))
        return code
    print("== verify（只读）==")
    code = run_steps(plan_verify(args.machine), execute=True)
    print("备份位置：{}/{}（回退：rollback --machine {} --stamp {}）".format(
        REMOTE_BACKUP_ROOT, stamp, args.machine, stamp))
    return code


def build_parser():
    parser = argparse.ArgumentParser(
        prog="console_agent_rollout",
        description="console-agent 逐机 rollout（默认 dry-run；--execute 才真的动远端）",
    )
    parser.add_argument("--machine", required=True, choices=TARGET_MACHINES)
    parser.add_argument("stage_pos", nargs="?", default=None,
                        choices=("plan", "preflight", "install", "switch", "verify", "rollback"),
                        help="阶段（位置式，如 `plan --machine txy …`）；与 --stage 等价")
    parser.add_argument("--stage", dest="stage_opt", default=None,
                        choices=("plan", "preflight", "install", "switch", "verify", "rollback"))
    parser.add_argument("--binary", help="本地 agent binary（kernel/agent/README 交叉编译产物）")
    parser.add_argument("--arch", choices=sorted(set(GOARCH_BY_UNAME.values())),
                        help="声明的目标 GOARCH；与远端 uname -m 交叉核对（fail closed）")
    parser.add_argument(
        "--server-url", default=None,
        help="agent 上报地址（默认 env CONSOLE_AGENT_SERVER_URL，再默认 " + DEFAULT_SERVER_URL + "）",
    )
    parser.add_argument("--disk-path", default="/",
                        help="agent 磁盘采集挂载点（默认 /；多盘机器显式指定，如 NAS 的 /vol1）")
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV,
                        help="enrol token 的本地环境变量名（只存名字，绝不存值）")
    parser.add_argument("--skip-enrol", action="store_true",
                        help="机器已 enrol（手工或上一轮），沿用既有身份")
    parser.add_argument("--stamp", help="rollback 的备份目录时间戳（install 的备份步骤输出里有）")
    parser.add_argument("--execute", action="store_true",
                        help="真实执行（默认 dry-run 只打印；plan 阶段永远只打印）")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.stage_pos and args.stage_opt and args.stage_pos != args.stage_opt:
        parser.error("stage 位置参数与 --stage 冲突：{} vs {}".format(args.stage_pos, args.stage_opt))
    args.stage = args.stage_pos or args.stage_opt or "plan"
    try:
        if args.stage == "plan" and args.execute:
            print("plan 阶段永远只打印（审计视图）；要执行请用 install/switch/verify/rollback --execute")
            return EXIT_OK
        if args.stage in ("install", "switch") and not args.binary:
            parser.error("install/switch 需要 --binary（本工具不编译）")
        binary = _load_binary_arg(args.binary) if args.binary else None
        if args.server_url is None:
            args.server_url = os.environ.get("CONSOLE_AGENT_SERVER_URL", "").strip() or DEFAULT_SERVER_URL
        if args.execute:
            print("== EXECUTE 模式：命令将真实执行（machine={} stage={}）==".format(
                args.machine, args.stage))
            return _execute_stage(args, binary)
        return _print_plan(args, binary)
    except PreflightError as error:
        print("ABORT: {}".format(error))
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
