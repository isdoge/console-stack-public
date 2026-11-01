# -*- coding: utf-8 -*-
"""服务运行态管理：Docker/systemd 状态、白名单动作及 assets.yaml 读写。"""
import json
import os
import re
import shlex
import subprocess
import time

import yaml

import machine_config as mc
import version_service as vs
from validation import (
    IDENTIFIER_RE,
    SERVICE_ACTIONS,
    validate_action,
    validate_health_probe,
    validate_identifier,
    validate_port,
)

SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "StrictHostKeyChecking=yes",
    # known_hosts 是只读挂载：不加这条会刷 hostfile_replace_entries 警告污染输出
    # （Track A A6 实测，见 proposals 2026-08-29 §13）
    "-o", "UpdateHostKeys=no",
]

# 机器 → SSH target，由 machines.yaml registry 提供（env MACHINE_SSH_<ID> 优先）。
# None = console 直接在本机跑 docker（nas 生产形态）；yaml 缺失时 registry 自带
# 收编前的硬编码兜底，行为不变。import 期定值：调用方（caddy_routes /
# version_service / beszel_client / 单测）依赖它是可 monkeypatch 的普通 dict。
MACHINE_SSH = mc.ssh_targets()


def _local_machine():
    """组合 host.local_machine：console 本机跑 docker 的那台（现役 nas）。

    公共组合可以不声明本机身份；此时返回空串，不猜任何机器。
    """
    return mc.local_machine()


def is_local_machine(machine):
    """该机器是否就是 console 所在主机；False 时动作必须走 SSH。

    未声明本机身份时恒为 False：公共实例不能因为「空 == 空」就把任意机器
    当成 console 本机、跳过 SSH 认证。
    """
    local = _local_machine()
    return bool(local) and str(machine or '').strip().lower() == local


def _host_control_cmd(args):
    """宿主控制命令：专用 forced key，不复用通用 SSH 身份。

    目标是组合 host.host_control_target；留空时按本机身份的 Tailscale IP 派生。
    """
    key_path = os.path.expanduser(mc.host_control_key())
    target = mc.host_control_target()
    if not key_path or not target:
        raise ValueError("宿主控制未配置专用密钥或目标")
    return ["ssh", *SSH_OPTS, "-i", key_path, target, shlex.join(args)]


def refresh_machine_ssh():
    """重建 MACHINE_SSH（machines.yaml 改动后手动调用；常规路径不需要）。"""
    MACHINE_SSH.clear()
    MACHINE_SSH.update(mc.ssh_targets())
    return MACHINE_SSH


_status_cache = {"ts": 0, "data": {}}


def _status_ttl():
    try:
        return max(60, int(os.environ.get("CONSOLE_PROBE_INTERVAL", "600")))
    except ValueError:
        return 600


def _validate_machine(machine, require=None):
    """存在性校验 +（可选）能力校验。

    require='docker' 时，us/kr/sg 这类无 docker 的机器被明确拒绝，而不是发出注定
    失败的远程命令。未知机器照旧拒绝。
    """
    machine = validate_identifier(machine or _local_machine(), "machine")
    if machine not in MACHINE_SSH:
        raise ValueError(f"未知机器: {machine}")
    if require and not mc.has_capability(machine, require):
        raise ValueError(f"机器 {machine} 不支持 {require} 动作")
    if require == "ssh" and not is_local_machine(machine) and not MACHINE_SSH[machine]:
        raise ValueError("机器未配置 systemd SSH 入口")
    return machine


def _container_name(svc):
    return validate_identifier(svc.get("container") or svc.get("name"), "container")


def _run(cmd, timeout=20, shell=False):
    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
            shell=shell,
        )
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as exc:
        return 1, "", str(exc)


def ssh_key_opts(machine):
    """registry 配了 ssh_key 就补 `-i <路径>`（us/kr/sg 用非 root 用户 + 专用私钥）。"""
    key_path = mc.ssh_key_path(machine)
    return ["-i", key_path] if key_path else []


def _docker_cmd(machine, args):
    """构造本机参数数组或经过 shell 转义的远程 docker 命令。"""
    machine = _validate_machine(machine, require="docker")
    safe_args = []
    for arg in args:
        text = str(arg)
        if any(ch in text for ch in ("\r", "\n", "\x00")):
            raise ValueError("Docker 参数包含控制字符")
        safe_args.append(text)

    ssh_target = MACHINE_SSH[machine]
    if ssh_target is None:
        return ["docker", *safe_args]

    remote = "docker " + " ".join(shlex.quote(arg) for arg in safe_args)
    return ["ssh", *SSH_OPTS, *ssh_key_opts(machine), ssh_target, remote]


def _systemd_unit(svc):
    unit = validate_identifier(svc.get("systemd_unit"), "systemd_unit")
    if not unit.endswith(".service"):
        raise ValueError("systemd_unit 必须是完整 .service 单元名")
    if svc.get("systemd_scope", "system") not in ("system", "user"):
        raise ValueError("systemd_scope 必须是 system/user")
    return unit


def _systemd_cmd(machine, args):
    target = MACHINE_SSH.get(machine)
    if not target:
        raise ValueError("机器未配置 systemd SSH 入口")
    prefix = [] if target.startswith("root@") else ["sudo", "-n"]
    scope_env = ["env", "XDG_RUNTIME_DIR=/run/user/0"] if "--user" in args else []
    return ["ssh", *SSH_OPTS, *ssh_key_opts(machine), target, shlex.join([*prefix, *scope_env, *args])]


def _process_status(services):
    status = {}
    groups = {}
    for svc in services:
        if svc.get("type") != "process" or not svc.get("systemd_unit"):
            continue
        try:
            machine = _validate_machine(svc.get("machine") or _local_machine(), require="ssh")
            unit = _systemd_unit(svc)
        except ValueError:
            continue
        groups.setdefault((machine, svc.get("systemd_scope", "system")), []).append((svc, unit))
    if any(is_local_machine(machine) for machine, _ in groups):
        try:
            cmd = _host_control_cmd(["status"])
        except ValueError:
            cmd = None
        code, out, _ = _run(cmd, timeout=20) if cmd else (1, "", "")
        if code == 0:
            try:
                states = json.loads(out)
            except json.JSONDecodeError:
                states = {}
            for name, state in states.items():
                status[f"{_local_machine()}:systemd:{name}"] = state
    for (machine, scope), group in groups.items():
        if is_local_machine(machine):
            continue
        cmd = _systemd_cmd(machine, ["systemctl", *(["--user"] if scope == "user" else []),
                                   "show", "--property=Id,ActiveState", "--", *[unit for _, unit in group]])
        code, out, _ = _run(cmd, timeout=15)
        if code:
            continue
        states = {}
        for block in out.strip().split("\n\n"):
            fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            states[fields.get("Id")] = fields.get("ActiveState")
        for svc, unit in group:
            if states.get(unit):
                status[f"{machine}:systemd:{svc['name']}"] = states[unit]
    return status


def get_all_status(services, force=False):
    now = time.time()
    ttl = _status_ttl()
    if not force and now - _status_cache["ts"] < ttl:
        return _status_cache["data"]

    machines = {}
    for svc in services:
        try:
            # 无 docker 能力的机器（us/kr/sg）不发 docker ps，其服务运行态保持 unknown
            machine = _validate_machine(svc.get("machine") or _local_machine(), require="docker")
        except ValueError:
            continue
        machines[machine] = True

    status = {}
    for machine in machines:
        try:
            cmd = _docker_cmd(machine, ["ps", "-a", "--format", "{{.Names}}\t{{.State}}"])
        except ValueError:
            continue
        code, out, _ = _run(cmd, timeout=15)
        if code != 0:
            continue
        for line in out.strip().splitlines():
            if "\t" not in line:
                continue
            name, state = line.split("\t", 1)
            try:
                name = validate_identifier(name.strip(), "container")
            except ValueError:
                continue
            status[f"{machine}:{name}"] = state.strip().lower()

    status.update(_process_status(services))
    _status_cache["ts"] = now
    _status_cache["data"] = status
    return status


def service_running(svc, status_map):
    if svc.get("type") == "process":
        state = status_map.get(f"{svc.get('machine') or _local_machine()}:systemd:{svc.get('name')}")
        if state in ("active", "activating", "reloading"):
            return "running"
        return "stopped" if state in ("inactive", "failed", "deactivating") else "unknown"
    try:
        name = _container_name(svc)
        machine = _validate_machine(svc.get("machine") or _local_machine())
    except ValueError:
        return "unknown"
    state = status_map.get(f"{machine}:{name}")
    if state is None:
        return "unknown"
    return "running" if state == "running" else "stopped"


def normalize_container_name(name):
    """容器名归一化（与前端 unified-table identityKey 同口径）：小写、去分隔符。"""
    return re.sub(r"[-_.\s]", "", str(name or "")).lower()


def discover_containers(machine):
    """只读白名单扫描：`docker ps -a` 列本机容器，供「扫描未登记服务」勾选登记。

    命令 argv 写死、无任何用户输入参与（§11 红线：不实现任意 shell）；
    无 docker 能力的机器（us/kr/sg）由 _validate_machine(require='docker') 明确拒绝。
    返回 [{name, image, status}]；命令失败抛 RuntimeError，机器/能力问题抛 ValueError。
    """
    machine = _validate_machine(machine, require="docker")
    cmd = _docker_cmd(
        machine,
        [
            "ps",
            "-a",
            # L4：多取一个 Docker **官方** compose label（不是靠容器名猜分组）。
            # 同一次 docker ps 顺带带回，零新增探测。手工 `docker run` 起的容器
            # 没有这个 label，那就是空字符串 —— 空即「未分组」，不做任何推断。
            "--format",
            '{{.Names}}|{{.Image}}|{{.Status}}|{{.Label "com.docker.compose.project"}}',
        ],
    )
    code, out, err = _run(cmd, timeout=20)
    if code != 0:
        raise RuntimeError(f"docker ps 失败: {(err or out).strip()[:200]}")
    items = []
    for line in out.strip().splitlines():
        parts = line.split("|", 3)
        if not parts or not parts[0].strip():
            continue
        try:
            name = validate_identifier(parts[0].strip(), "container")
        except ValueError:
            continue
        items.append(
            {
                "name": name,
                "image": parts[1].strip() if len(parts) > 1 else "",
                "status": parts[2].strip() if len(parts) > 2 else "",
                "stack": _normalize_stack(parts[3] if len(parts) > 3 else ""),
            }
        )
    return items


def _normalize_stack(value):
    """compose project 名 → 分组键；拿不到可信值一律空串（= 未分组）。

    ``default`` 也当未分组：1Panel 给部分容器落的就是这个兜底值，它不表达
    任何真实分组（2026-09-07 实测 txy 的 `1panel-axonhub-status-embed`）。
    这是**读到的值本身没信息量**，不是我们在猜名字。
    """
    text = str(value or "").strip()
    if not text or text.lower() == "default" or not IDENTIFIER_RE.fullmatch(text):
        return ""
    return text


def do_action(svc, action):
    try:
        process = svc.get("type") == "process"
        machine = _validate_machine(svc.get("machine") or _local_machine(), require="ssh" if process else "docker")
        name = _systemd_unit(svc) if process else _container_name(svc)
        action = validate_action(action)
        configured_raw = svc.get("actions")
        if configured_raw is None:
            configured_raw = [] if process else ["start", "stop", "restart", "logs"]
        configured_actions = {
            validate_action(item) for item in configured_raw
        }
        if action not in configured_actions:
            return False, f"服务未授权动作: {action}"
    except ValueError as exc:
        return False, str(exc)

    if process:
        if is_local_machine(machine):
            try:
                cmd = _host_control_cmd([action, svc["name"]])
            except ValueError as exc:
                return False, str(exc)
        else:
            scope = ["--user"] if svc.get("systemd_scope") == "user" else []
            argv = (["journalctl", "--user-unit" if scope else "--unit", name, "--no-pager", "-n", "200"]
                    if action == "logs" else ["systemctl", *scope, action, "--", name])
            cmd = _systemd_cmd(machine, argv)
    elif action == "upgrade":
        # compose 服务升级：pull 新镜像 + 重建容器。仅限台账显式登记 compose_dir
        # 的 docker 服务（argv 全程不接外部参数，与 NODE_ACTIONS 同一红线）。
        # 机器级闸：machines.yaml `upgrade.enabled` 缺省 False，UI 机器编辑可勾；
        # `upgrade.proxy_env` 可选注入远端 docker compose 的 env（键白名单 + 值字符集
        # 由 machine_config 限定），NAS 守护进程已全局代理故一般留空。
        compose_dir = svc.get("compose_dir")
        if not compose_dir:
            return False, "服务未登记 compose_dir，不支持 upgrade"
        if not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9_./-]{0,200}", compose_dir):
            return False, "compose_dir 必须是以 / 开头的绝对路径（字母数字点斜杠连字符）"
        # compose_file 可选：缺省 compose.yaml；ignis 等老项目用 docker-compose.yml
        compose_file = str(svc.get("compose_file") or "compose.yaml").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", compose_file):
            return False, "compose_file 必须是 compose.yaml 同目录下的文件名（不含路径分隔符）"
        upgrade_cfg = mc.upgrade_config(machine)
        if not upgrade_cfg or not upgrade_cfg.get("enabled"):
            return False, f"机器 {machine} 未开放服务升级（在机器页勾选「允许升级」）"
        ssh_target = MACHINE_SSH.get(machine)
        proxy_env = upgrade_cfg.get("proxy_env") or {}
        # env 必须放在 sudo 后面（sudo 默认剥环境变量；sudo env K=V cmd 保留注入）
        env_prefix = ""
        if proxy_env:
            env_prefix = "env " + " ".join(
                f"{key}={shlex.quote(value)}" for key, value in sorted(proxy_env.items())
            ) + " "
        # 若登记了 repo_url 且本地版本已知，先取上游最新 tag 更新 compose 文件里的 image。
        # 读/写 compose 必须走 ssh 或本机命令（console 容器不挂宿主机 /vol1 路径）。
        repo_url = (svc.get("version") or {}).get("repo_url")
        if not ssh_target and not is_local_machine(machine):
            # 无法读取远端 compose 文件，跳过 tag 更新（pull 还是会跑）
            repo_url = None
        if repo_url:
            compose_path = shlex.quote(compose_dir + "/" + compose_file)
            if ssh_target:
                read_cmd = ["ssh", *SSH_OPTS, *ssh_key_opts(machine), ssh_target, f"cat {compose_path}"]
                code, text, _err = _run(read_cmd, timeout=30)
                if code != 0:
                    text = ""
            else:
                code, text, _err = _run(f"cat {compose_path}", shell=True, timeout=30)
                if code != 0:
                    text = ""
            current_image = _first_image(text)
            if current_image and ":" in current_image:
                upstream, _, _ = vs.get_upstream_release(repo_url)
                current_tag = current_image.rsplit(":", 1)[-1]
                if upstream and vs._versions_differ(current_tag, upstream):
                    # Docker 标签沿用已有镜像的 v 前缀惯例，不能套用 GitHub tag 的格式。
                    prefix = "v" if current_tag.startswith("v") else ""
                    new_tag = prefix + upstream.removeprefix("v")
                    new_image = re.sub(r":[^:/]+$", f":{new_tag}", current_image)
                    sed_expr = f"s|{re.escape(current_image)}|{new_image}|"
                    if ssh_target:
                        write_cmd = ["ssh", *SSH_OPTS, *ssh_key_opts(machine), ssh_target, f"sed -i {shlex.quote(sed_expr)} {compose_path}"]
                        _run(write_cmd, timeout=30)
                    else:
                        _run(f"sed -i {shlex.quote(sed_expr)} {compose_path}", shell=True, timeout=30)
        if not ssh_target and is_local_machine(machine):
            # nas 本机 docker 直跑（registry 无 env/MACHINE_SSH_NAS 覆盖时）；
            # 同口径支持 proxy_env 注入，为空则 env_prefix 为空串
            compose_path = shlex.quote(compose_dir + "/" + compose_file)
            local = (
                f"sudo -n {env_prefix}docker compose -f {compose_path} pull"
                f" && sudo -n {env_prefix}docker compose -f {compose_path} up -d"
            )
            code, out, err = _run(local, shell=True, timeout=300)
            _status_cache["ts"] = 0
            if code != 0:
                return False, err.strip() or out.strip() or f"upgrade 失败 (exit {code})"
            return True, out.strip()
        if not ssh_target:
            return False, f"机器 {machine} 未配置 SSH 入口"
        compose_path = shlex.quote(compose_dir + "/" + compose_file)
        remote = (
            f"sudo -n {env_prefix}docker compose -f {compose_path} pull"
            f" && sudo -n {env_prefix}docker compose -f {compose_path} up -d"
        )
        cmd = ["ssh", *SSH_OPTS, *ssh_key_opts(machine), ssh_target, remote]
        code, out, err = _run(cmd, timeout=300)
        _status_cache["ts"] = 0
        try:
            from probe_cache import invalidate as pc_invalidate
            pc_invalidate(name)
        except Exception:
            pass
        if code != 0:
            return False, err.strip() or out.strip() or f"upgrade 失败 (exit {code})"
        return True, out.strip()
    else:
        cmd = _docker_cmd(machine, ["logs", "--tail", "200", name] if action == "logs" else [action, name])
    if action == "logs":
        code, out, err = _run(cmd, timeout=25)
        text = out or err
        text = re.sub(
            r"(?i)(token|secret|password|key|auth|bearer)\s*[=:]\s*\S+",
            r"\1=***",
            text,
        )
        return code == 0, text

    code, out, err = _run(cmd, timeout=30)
    _status_cache["ts"] = 0
    try:
        from probe_cache import invalidate as pc_invalidate
        pc_invalidate(svc.get("name"))
    except Exception:
        pass
    return code == 0, (out or err).strip()


# E3：node 机器唯一允许的运维动作。argv 全部写死，调用方只能选 key、传不进任何参数——
# console 对 node 就开这一个口子，**不实现任意命令**（§9 E3 / §11 红线）。
NODE_ACTIONS = {
    "restart-singbox": {
        "label": "重启 sing-box",
        "unit": "sing-box",
        "verb": "restart",
        "argv": ("sudo", "systemctl", "restart", "sing-box"),
    },
}


def node_action_spec(action):
    """白名单查表；不在表里返回 None。"""
    return NODE_ACTIONS.get(str(action or "").strip())


def node_actions_for(machine):
    """该机器当前可用的节点动作（给前端渲染按钮用）；不满足条件返回空列表。

    只暴露 key + 文案，命令本身不外泄；服务端在 node_action 里重新校验一遍。
    """
    entry = mc.get_machine(machine)
    if not entry or entry["role"] != "node":
        return []
    if not mc.has_capability(machine, "ssh") or not mc.ssh_target(machine):
        return []
    return [{"key": key, "label": spec["label"]} for key, spec in NODE_ACTIONS.items()]


def node_action(machine, action):
    """对 role=node 机器执行白名单动作，返回 (ok, output)。

    四道闸门：机器在台账里、role 必须是 node、必须有 ssh 能力且配了 target、
    动作必须命中白名单。任何一条不过都不发命令。
    """
    spec = node_action_spec(action)
    if not spec:
        return False, f"未授权的节点动作: {action}"
    try:
        machine = validate_identifier(machine, "machine")
    except ValueError as exc:
        return False, str(exc)

    entry = mc.get_machine(machine)
    if not entry:
        return False, f"未知机器: {machine}"
    if entry["role"] != "node":
        return False, f"机器 {machine} 不是节点，不接受节点动作"
    if not mc.has_capability(machine, "ssh"):
        return False, f"机器 {machine} 不支持 ssh 动作"

    ssh_target = mc.ssh_target(machine)
    if not ssh_target:
        return False, f"机器 {machine} 未配置 SSH target"

    remote = " ".join(shlex.quote(arg) for arg in spec["argv"])
    code, out, err = _run(
        ["ssh", *SSH_OPTS, *ssh_key_opts(machine), ssh_target, remote], timeout=30
    )
    return code == 0, (out or err).strip()


def load_assets(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or []
    if not isinstance(data, list):
        raise ValueError("assets.yaml 顶层必须是列表")
    return data


def save_assets(path, services):
    from yaml_io import atomic_write_yaml

    atomic_write_yaml(path, services)


def upsert_service(path, payload, original_name=None, access_probe=None):
    services = load_assets(path)
    name = validate_identifier(payload.get("name"), "服务名称")
    original_name = validate_identifier(original_name, "原服务名称", allow_empty=True)

    existing_index = None
    if original_name:
        existing_index = next(
            (index for index, svc in enumerate(services) if svc.get("name") == original_name),
            None,
        )
        if existing_index is None:
            raise ValueError(f"服务不存在: {original_name}")
        if name != original_name and any(svc.get("name") == name for svc in services):
            raise ValueError(f"服务已存在: {name}")
    elif any(svc.get("name") == name for svc in services):
        raise ValueError(f"服务已存在: {name}")

    existing = services[existing_index] if existing_index is not None else None
    entry = _build_entry({**payload, "name": name}, access_probe=access_probe)
    if existing:
        if "type" not in payload and "type" in existing:
            entry["type"] = existing["type"]
        if "actions" not in payload and "actions" in existing:
            entry["actions"] = existing["actions"]

        incoming_version = payload.get("version") or {}
        existing_version = existing.get("version") or {}
        for field in ("enabled", "kind", "branch", "probe", "track_upstream"):
            if field not in incoming_version and field in existing_version:
                entry["version"][field] = existing_version[field]

    if existing_index is None:
        services.append(entry)
    else:
        merged = {**existing, **entry}
        merged["access"] = entry.get("access", existing.get("access", {}))
        merged["version"] = entry.get("version", existing.get("version", {}))
        if "systemd_unit" in payload and not payload["systemd_unit"]:
            merged.pop("systemd_unit", None)
            merged.pop("systemd_scope", None)
        if "health_probe" not in entry:
            merged.pop("health_probe", None)
        services[existing_index] = merged

    save_assets(path, services)
    return entry


def delete_service(path, name):
    name = validate_identifier(name, "服务名称")
    services = load_assets(path)
    updated = [svc for svc in services if svc.get("name") != name]
    if len(updated) == len(services):
        raise ValueError(f"服务不存在: {name}")
    save_assets(path, updated)


def set_priority(path, names, priority):
    """批量设置服务优先级，返回更新条数。"""
    priority = str(priority or "").strip().lower()
    if priority not in ("high", "medium", "low"):
        raise ValueError("priority 必须是 high/medium/low")
    if not isinstance(names, list) or not names:
        raise ValueError("names 必须是非空列表")
    wanted = {validate_identifier(n, "服务名称") for n in names}
    services = load_assets(path)
    updated = 0
    for svc in services:
        if svc.get("name") in wanted:
            svc["priority"] = priority
            updated += 1
    if updated == 0:
        raise ValueError("未匹配到任何服务")
    save_assets(path, services)
    return updated


def _build_entry(payload, access_probe=None):
    access = {}
    for key in ("home", "tailscale", "domain"):
        val = (payload.get("access", {}) or {}).get(key) or payload.get(key)
        if val:
            access[key] = str(val).strip()
    if access_probe:
        probe = {}
        for key in ("home", "tailscale", "domain"):
            item = access_probe.get(key) or {}
            if item.get("configured"):
                probe[key] = bool(item.get("ok"))
        if probe:
            access["probe"] = probe

    version = payload.get("version", {}) or {}
    repo_url = version.get("repo_url") or payload.get("repo_url")
    enabled_raw = version.get("enabled")
    if enabled_raw is None:
        version_enabled = bool(repo_url)
    elif isinstance(enabled_raw, str):
        version_enabled = enabled_raw.strip().lower() in ("1", "true", "yes", "on")
    else:
        version_enabled = bool(enabled_raw)
    version_entry = {
        "enabled": version_enabled,
        "kind": version.get("kind", "image"),
    }
    if repo_url:
        version_entry["repo_url"] = str(repo_url).strip()
    track_raw = version.get("track_upstream")
    if track_raw is not None:
        if isinstance(track_raw, str):
            version_entry["track_upstream"] = track_raw.strip().lower() not in ("0", "false", "no", "off")
        else:
            version_entry["track_upstream"] = bool(track_raw)
    if version.get("branch"):
        version_entry["branch"] = validate_identifier(version["branch"], "branch")

    machine = _validate_machine(payload.get("machine") or _local_machine())
    name = validate_identifier(payload.get("name"), "服务名称")
    entry = {
        "name": name,
        "machine": machine,
        "type": payload.get("type", "docker"),
    }
    if payload.get("container"):
        entry["container"] = validate_identifier(payload["container"], "container")
    port = validate_port(payload.get("port"))
    if port is not None:
        entry["port"] = port
    entry["purpose"] = str(payload.get("purpose", ""))
    priority = str(payload.get("priority") or "medium").strip().lower()
    if priority not in ("high", "medium", "low"):
        raise ValueError("priority 必须是 high/medium/low")
    entry["priority"] = priority
    if payload.get("note"):
        entry["note"] = str(payload["note"])
    if payload.get("deploy"):
        entry["deploy"] = str(payload["deploy"])
    if payload.get("compose_dir"):
        compose_dir = str(payload["compose_dir"]).strip()
        if not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9_./-]{0,200}", compose_dir):
            raise ValueError("compose_dir 必须是以 / 开头的绝对路径（字母数字点斜杠连字符）")
        entry["compose_dir"] = compose_dir
    if payload.get("compose_file"):
        compose_file = str(payload["compose_file"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", compose_file):
            raise ValueError("compose_file 必须是 compose.yaml 同目录下的文件名（不含路径分隔符）")
        entry["compose_file"] = compose_file
    if payload.get("tags"):
        entry["tags"] = [str(tag)[:64] for tag in payload["tags"][:32]]
    if access:
        entry["access"] = access

    if payload.get("systemd_unit"):
        entry["systemd_unit"] = _systemd_unit(payload)
        entry["systemd_scope"] = payload.get("systemd_scope", "system")
    actions = payload.get("actions", [] if entry["type"] == "process" else ["start", "stop", "restart", "logs"])
    if not isinstance(actions, list):
        raise ValueError("actions 必须是列表")
    entry["actions"] = [validate_action(item, SERVICE_ACTIONS) for item in actions]
    entry["version"] = version_entry
    # L1：三态。False = 关闭；dict = 语义检查（校验后落盘，未知字段直接 400）；
    # 缺省 / True = 走默认推断，不写这个键。
    hp = validate_health_probe(payload.get("health_probe"))
    if hp is not None:
        entry["health_probe"] = hp
    return entry


def _first_image(compose_text):
    """从 compose 文件内容里提取第一个 image 行（含 tag），供 tag 更新逻辑使用。"""
    if not compose_text:
        return None
    pattern = re.compile(r"^\s+image:\s*([^\s#]+)", re.MULTILINE)
    matches = pattern.findall(compose_text)
    return matches[0] if matches else None
