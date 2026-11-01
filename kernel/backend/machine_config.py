# -*- coding: utf-8 -*-
"""机器台账 registry：machines.yaml 是 console 内「机器」的单一事实源。

历史上机器映射硬编码在四处（machine_config / service_manager / probe_cache /
beszel_client），加机器要改四个文件且容易漂移。此模块收编为唯一 owner：

- 地址与访问规则（suggest_access / tailscale_ip / ssh_target / ssh_key_path）
- 角色与能力（role=server|node，capabilities ⊆ docker/beszel/ssh）
- 对外投影（machines_for_api / machines_for_config）

env 覆盖方向固定 **env > yaml**（MACHINE_TAILSCALE_<ID> / MACHINE_SSH_<ID>）；
只有“无组合文件的 private”在 yaml 缺失时回退 _DEFAULT_MACHINES（等于收编前的
硬编码值），保证新 checkout 和未挂载 machines.yaml 的容器行为不变。组合文件
生效或 public 档案下，缺台账/损坏台账都抛有界 `CompositionError`，不返回空表
冒充成功；显式空列表 `[]` 是合法的空台账（见 composition.py 与 B7-1）。

家庭入口（HOME_MACHINES / HOME_LAN_IP / 排除 id）与台账正交（D5：mac/win 不进
machines.yaml），现由组合 `host` 策略给出；无组合文件时等于原私人常量。
"""
import ipaddress
import os
import re

import yaml

import composition
from validation import validate_identifier, validate_port

# 家庭入口来自组合 host 策略；无组合文件时仍是原私人常量（effective_config
# 直接读这两个模块常量做展示默认，故保留名称）。
_HOME_POLICY = composition.host_policy()
HOME_LAN_IP = _HOME_POLICY["home_lan_ip"]
HOME_MACHINES = set(_HOME_POLICY["home_machines"])

VALID_ROLES = ("server", "node")
VALID_CAPABILITIES = ("docker", "beszel", "ssh")

# upgrade.proxy_env 允许注入的 env 键白名单：只放行 http(s) 代理三件套 + docker 自己的
# 两个拉镜像变量。值会拼进远端 shell 的 `env K=V docker compose ...`，所以值本身也限字符。
_VALID_PROXY_ENV_KEYS = frozenset({
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
    "DOCKER_CLI_HINTS",  # 允许关掉 cli hint 输出，跟代理无关但常与升级脚本同场
})
_PROXY_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9.,:;/_+=@%\-]{1,200}$")

# 主机名/域名：public_host 会进探活目标与 URL，ssh target 的 host 段同样走它。
# 首字符不许是 `-`，否则 `ssh <target>` 会把它当选项解析（-oProxyCommand= 可执行任意命令）。
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,253}$")
# ssh target：`user@host` 或裸 host。user 段同样禁首字符 `-`。
_SSH_TARGET_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._\-]{0,31}@)?[A-Za-z0-9][A-Za-z0-9.\-]{0,253}$")
# renewal_ref 既可能是 renewals 的完整 UUID，也可能是人可读固定 id（node-kr 等，§7.5）
_RENEWAL_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
# systemd unit 展示白名单（Track J §4.1）：A-Za-z0-9_.@:-，unit 名大小写敏感，
# 不进入 shell，只决定「显示什么」；读宽容（非法项丢弃）/写严格（中文 400）。
_SYSTEMD_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@:\-]{1,128}$")
SYSTEMD_UNITS_MAX = 32

# machines.yaml 顶部契约头：PyYAML 不保留注释，UI 写回时由 save_machines 显式重写。
# 逐条的行内注释（决策出处、UUID 陷阱等）会在第一次 UI 编辑后丢失，理由留在 .agents/notes/ 里。
MACHINES_HEADER = """\
# 机器台账：console 内「机器」的单一事实源（D5：不含 mac/win）。
# role: server | node；capabilities ⊆ [docker, beszel, ssh]
# 写回契约见 .agents/contracts/backend/config-writeback.md；字段见 docs/configuration.md。
# env 覆盖方向固定 env > yaml：MACHINE_TAILSCALE_<ID> / MACHINE_SSH_<ID>。
# 卡片字段（docs/configuration.md 的 machines.yaml 节）：
#   os/region（header）、server 的 probe（Q3 走 tailscale）、traffic_quota_gb 初值（Q1/Q2）。
# systemd_units 展示白名单（.agents/contracts/backend/security-boundaries.md）：
#   只决定机器页显示哪些 systemd 单元，不授予操作能力；US/KR/SG 初值 sing-box。
# public_probe 家宽公网探测（.agents/notes/*/architecture/2026-09-28-machine-public-probe-path.md）：
#   独立于 probe，从 NAS 直连 public_host 的该端口；依赖 NAS mihomo 豁免，不改变 probe 语义。
#
# ⚠️ 本文件可由控制台 UI 写回（B3：POST/PUT/DELETE /api/machines）。PyYAML 不保留注释，
#    UI 一写就只剩这段头，逐条的行内注释会消失——决策出处请写进 .agents/notes，别只写在这里。
# ⚠️ renewal_ref 指向权威 PostgreSQL 的 renewals.id，数据库不在 git 里：
#    换机/重建库后必须重新对齐，否则价格 chip 静默消失。"""

# yaml 缺失时的兜底：与收编前硬编码完全一致（nas/txy/aly + win/mac 的 TS 位）
_DEFAULT_MACHINES = [
    {
        "id": "nas", "label": "NAS", "role": "server",
        "tailscale": "100.64.0.10", "ssh": "",
        "beszel_name": "NAS", "capabilities": ["docker", "beszel", "ssh"],
    },
    {
        "id": "txy", "label": "TXY", "role": "server",
        "tailscale": "100.64.0.30", "ssh": "root@100.64.0.30",
        "beszel_name": "TXY", "capabilities": ["docker", "beszel", "ssh"],
    },
    {
        "id": "aly", "label": "ALY", "role": "server",
        "tailscale": "100.64.0.40", "ssh": "root@100.64.0.40",
        "beszel_name": "ALY", "capabilities": ["docker", "beszel", "ssh"],
    },
]

# 家庭机器的 Tailscale 位：不进 machines.yaml，仍由 env 提供（D5）。
# 键来自组合 host.excluded_machine_ids；无组合文件时仍是 win/mac。
_EXTRA_TAILSCALE = {
    mid: os.environ.get("MACHINE_TAILSCALE_{}".format(mid.upper()), "")
    for mid in _HOME_POLICY["excluded_machine_ids"]
}

# 不许进台账的 id：只有宅内入口机器（D5）。注意 **不能**拿 HOME_MACHINES 当黑名单，
# 它含 nas，而 nas 是台账里的 server——那样会把 nas 的编辑一并拒掉。
LEDGER_EXCLUDED_IDS = frozenset(_EXTRA_TAILSCALE)

# 容器内 .ssh-console/ 挂到 /root/.ssh；本机开发可用 env 指到仓库内目录
SSH_KEY_DIR = os.environ.get("CONSOLE_SSH_KEY_DIR", "/root/.ssh")

_cache = {"path": None, "mtime": None, "machines": None}


def _host_policy():
    return composition.host_policy()


def home_machines():
    """组合 host.home_machines；无组合文件时等于原私人集合。"""
    return set(_host_policy()["home_machines"])


def home_lan_ip():
    """组合 host.home_lan_ip；公共实例为空则不出家庭入口。"""
    return _host_policy()["home_lan_ip"]


def excluded_machine_ids():
    """组合 host.excluded_machine_ids（不许进台账的宅内机器）。"""
    return frozenset(_host_policy()["excluded_machine_ids"])


def local_machine():
    """组合 host.local_machine；无文件时为 nas（现役 NAS 本机 docker 形态）。"""
    return _host_policy()["local_machine"]


def host_control_key():
    """组合 host.host_control_key；显式组合省略时保持中性空值。"""
    return _host_policy()["host_control_key"]


def host_control_target():
    """宿主控制 SSH 目标；空值按本机身份的 Tailscale IP 派生。"""
    policy = _host_policy()
    target = policy["host_control_target"]
    if target:
        return target
    local = policy["local_machine"]
    if not local or not policy["host_control_key"]:
        return ""
    address = tailscale_ip(local)
    return "root@{}".format(address) if address else ""


def _extra_tailscale():
    """宅内机器的 Tailscale 位：组合模式按 host.home_machines 现读 env。"""
    if composition.composition_file() is None:
        return _EXTRA_TAILSCALE
    return {
        mid: os.environ.get("MACHINE_TAILSCALE_{}".format(mid.upper()), "")
        for mid in _host_policy()["home_machines"]
    }


def machines_path():
    """machines.yaml 位置：专用 env > 组合配置根 > 现役 backend/console 探测。"""
    return str(composition.config_path("machines.yaml", "CONSOLE_MACHINES_PATH"))


def _normalize_systemd_units(value):
    """systemd_units 宽容读：trim、去重保序、非法项丢弃、封顶 32 项（§4.1）。"""
    if not isinstance(value, list):
        return []
    units = []
    for item in value:
        if not isinstance(item, str):
            continue
        unit = item.strip()
        if not unit or len(unit) > 128 or not _SYSTEMD_UNIT_RE.fullmatch(unit):
            continue
        if unit not in units and len(units) < SYSTEMD_UNITS_MAX:
            units.append(unit)
    return units


def _normalize_public_probe(value):
    """public_probe 宽容读：只认 {port}（type 缺省 tcp），其它形状一律当没配。

    与 probe 分开存：probe 答「机器活着吗」（走 Tailscale），public_probe 答
    「家宽直连公网端口通不通」——代理节点公网被封时前者照样 up
    （决策见 .agents/notes/*/architecture/2026-09-28-machine-public-probe-path.md）。
    """
    if not isinstance(value, dict):
        return None
    probe_type = str(value.get("type") or "tcp").strip().lower()
    if probe_type != "tcp":
        return None
    try:
        port = int(value.get("port"))
    except (TypeError, ValueError):
        return None
    if not 1 <= port <= 65535:
        return None
    return {"type": "tcp", "port": port}


def _normalize_upgrade(value):
    """upgrade 宽容读：缺省/非 dict → None（= 本机服务 UI 不出 upgrade 按钮）。

    形状 {enabled: bool, proxy_env: {K: V}}；proxy_env 键限白名单、值限字符集，
    非法键值读路径静默丢弃（与 systemd_units 一致），写路径在 validate 里严格报错。
    """
    if not isinstance(value, dict):
        return None
    enabled = bool(value.get("enabled"))
    proxy_raw = value.get("proxy_env")
    proxy_env = {}
    if isinstance(proxy_raw, dict):
        for key, raw in proxy_raw.items():
            key_text = str(key or "").strip()
            if key_text not in _VALID_PROXY_ENV_KEYS:
                continue
            text = str(raw or "").strip()
            if not text or not _PROXY_ENV_VALUE_RE.fullmatch(text):
                continue
            proxy_env[key_text] = text
    if not enabled and not proxy_env:
        return None
    result = {"enabled": enabled}
    if proxy_env:
        result["proxy_env"] = proxy_env
    return result


def _normalize(entry):
    """单条台账归一化：非法 role/capability 丢弃而不抛，避免一条坏数据打死整个 registry。"""
    machine_id = str(entry.get("id") or "").strip().lower()
    if not machine_id:
        return None
    role = str(entry.get("role") or "server").strip().lower()
    if role not in VALID_ROLES:
        role = "server"
    caps_raw = entry.get("capabilities") or []
    if not isinstance(caps_raw, list):
        caps_raw = []
    capabilities = [
        str(c).strip().lower() for c in caps_raw
        if str(c).strip().lower() in VALID_CAPABILITIES
    ]

    probe_raw = entry.get("probe") or {}
    probe = None
    if isinstance(probe_raw, dict) and probe_raw.get("port"):
        try:
            port = int(probe_raw["port"])
        except (TypeError, ValueError):
            port = None
        if port and 1 <= port <= 65535:
            probe = {
                "type": str(probe_raw.get("type") or "tcp").strip().lower(),
                "port": port,
            }

    quota = entry.get("traffic_quota_gb")
    try:
        quota = int(quota) if quota not in (None, "") else None
    except (TypeError, ValueError):
        quota = None

    public_host = str(entry.get("public_host") or "").strip()

    return {
        "id": machine_id,
        "label": str(entry.get("label") or machine_id.upper()),
        "role": role,
        "provider": str(entry.get("provider") or ""),
        # 卡片 header 的 OS / 地区（emoji 旗），缺失时前端整块不渲染
        "os": str(entry.get("os") or "").strip(),
        "region": str(entry.get("region") or "").strip(),
        "public_host": public_host,
        "tailscale": str(entry.get("tailscale") or "").strip(),
        "ssh": str(entry.get("ssh") or "").strip(),
        "ssh_key": str(entry.get("ssh_key") or "").strip(),
        "beszel_name": str(entry.get("beszel_name") or machine_id.upper()),
        "capabilities": capabilities,
        "probe": probe,
        # 家宽公网探测：没有 public_host 就无处可探，读路径直接丢弃（不抛）
        "public_probe": _normalize_public_probe(entry.get("public_probe")) if public_host else None,
        "traffic_quota_gb": quota,
        # E1：计入配额的网卡名；留空由 node_traffic 按排除表自动挑（tailscale0/lo 等不算）
        "traffic_nic": str(entry.get("traffic_nic") or "").strip(),
        "renewal_ref": str(entry.get("renewal_ref") or "").strip(),
        # J-B：systemd 展示白名单（读宽容，非法项丢弃不抛）
        "systemd_units": _normalize_systemd_units(entry.get("systemd_units")),
        # 服务升级闸（2026-10-22）：None = 本机服务 UI 不出 upgrade 按钮
        "upgrade": _normalize_upgrade(entry.get("upgrade")),
    }


def _strict_entry_error(index, detail, path):
    return composition.CompositionError(
        f"机器台账不可解析（第 {index} 项{detail}）: {path}"
    )


def _validate_strict_entries(raw, path):
    """显式组合/public 台账：合法 list 里的每条记录都必须能成为一台机器。

    legacy（无组合文件的 private）仍保留宽容读和三机回退；组合文件生效或
    `public` 档案下，静默丢掉坏条目会让页面显示一个看似成功的残缺台账。
    非 dict、无字符串 id、非法 id/role/capabilities、重复 id 一律按 broken
    失败；显式 `[]` 仍是合法空台账，不经过这里。
    """
    seen = set()
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise _strict_entry_error(index, "不是对象", path)
        raw_id = entry.get("id")
        if not isinstance(raw_id, str):
            raise _strict_entry_error(index, "缺少字符串 id", path)
        try:
            machine_id = validate_identifier(raw_id, "机器 id").lower()
        except ValueError as exc:
            raise _strict_entry_error(index, f"的 id 非法: {raw_id!r}", path) from exc
        if machine_id in seen:
            raise _strict_entry_error(index, f"的 id 重复: {machine_id}", path)
        seen.add(machine_id)

        role = entry.get("role", "server")
        if not isinstance(role, str) or role.strip().lower() not in VALID_ROLES:
            raise _strict_entry_error(index, f"的 role 非法: {role!r}", path)

        capabilities = entry.get("capabilities")
        if capabilities is not None:
            if not isinstance(capabilities, list):
                raise _strict_entry_error(index, "的 capabilities 不是数组", path)
            for capability in capabilities:
                if (
                    not isinstance(capability, str)
                    or capability.strip().lower() not in VALID_CAPABILITIES
                ):
                    raise _strict_entry_error(
                        index, f"含非法 capability: {capability!r}", path
                    )


def _read_raw(path, mtime):
    """读台账原始 list。返回 (raw, status)。

    status ∈ {'ok', 'empty', 'missing', 'broken'}：
    - `ok`     ：读到非空 list
    - `empty`  ：文件存在且顶层是显式空列表 `[]`（合法空台账）
    - `missing`：文件不存在、没有 mtime 或文件为空（未声明台账）
    - `broken` ：读取失败或顶层不是列表
    """
    if mtime is None:
        return None, "missing"
    try:
        with open(path, "r", encoding="utf-8") as file:
            loaded = yaml.safe_load(file)
    except (OSError, yaml.YAMLError):
        return None, "broken"
    if loaded is None or loaded == "":
        return None, "missing"
    if not isinstance(loaded, list):
        return None, "broken"
    return loaded, ("ok" if loaded else "empty")


def load_machines(force=False):
    """读取台账，按 mtime 缓存。返回 {id: machine dict}，保持 yaml 内顺序。"""
    path = machines_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None

    if (
        not force
        and _cache["machines"] is not None
        and _cache["path"] == path
        and _cache["mtime"] == mtime
    ):
        return _cache["machines"]

    raw, status = _read_raw(path, mtime)
    fallback_allowed = composition.private_machine_defaults()
    if status in ("missing", "broken") and not fallback_allowed:
        # 组合文件/公共档案下不能把“缺台账或坏台账”伪装成空台账：空台账是页面
        # 可接入的正常状态，读不到配置是配置错误，两者必须能区分。
        detail = "不存在" if status == "missing" else "不可解析或顶层不是列表"
        raise composition.CompositionError(f"机器台账{detail}: {path}")
    if status in ("missing", "broken"):
        raw = _DEFAULT_MACHINES
    elif not fallback_allowed:
        _validate_strict_entries(raw, path)

    machines = _build(raw or [])
    if (
        not machines
        and status == "ok"
        and raw is not _DEFAULT_MACHINES
        and fallback_allowed
    ):
        # yaml 存在但一台可用机器都没解析出来，等价于缺失：回落硬编码，
        # 否则容器动作会因「未知机器」全线拒绝。
        machines = _build(_DEFAULT_MACHINES)

    _cache.update({"path": path, "mtime": mtime, "machines": machines})
    return machines


def _build(raw):
    machines = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        normalized = _normalize(entry)
        if normalized:
            machines[normalized["id"]] = normalized
    return machines


def invalidate():
    """清空 registry 缓存（测试与 yaml 热改用）。"""
    _cache.update({"path": None, "mtime": None, "machines": None})


# ---------------------------------------------------------------------------
# 写路径（B3 机器增删，§5.3）：assets.yaml 那条链照抄——
# 校验 → 读原始 list → 改 → yaml_io.atomic_write_yaml → invalidate()
# ---------------------------------------------------------------------------

def _text(value, field, limit, allow_empty=True):
    """自由文本字段：禁控制字符 + 限长。label/provider/os 这类不能套 identifier 正则。"""
    text = "" if value is None else str(value).strip()
    if not text:
        if allow_empty:
            return ""
        raise ValueError(f"{field} 不能为空")
    if any(ch in text for ch in ("\r", "\n", "\x00")):
        raise ValueError(f"{field} 不能包含控制字符")
    if len(text) > limit:
        raise ValueError(f"{field} 长度不能超过 {limit}")
    return text


def _host(value, field, allow_empty=True):
    text = "" if value is None else str(value).strip()
    if not text:
        if allow_empty:
            return ""
        raise ValueError(f"{field} 不能为空")
    if not _HOSTNAME_RE.fullmatch(text):
        raise ValueError(f"{field} 必须是合法主机名或 IP（字母数字起头，仅含字母数字点和连字符）")
    return text


def validate_machine_payload(payload):
    """把 UI 提交的机器台账规范化成可写回 yaml 的 dict；任一字段不合法直接抛 ValueError。

    与 `_normalize` 的分工：`_normalize` 是**读**路径的宽容归一化（一条坏数据不打死
    整个 registry，非法值静默回落）；这里是**写**路径，非法值必须报错，否则用户改完
    看不出哪儿被悄悄改掉了。
    """
    payload = payload or {}
    machine_id = validate_identifier(payload.get("id"), "机器 id").lower()
    if machine_id in excluded_machine_ids():
        raise ValueError(
            f"{machine_id} 是家庭入口机器（D5），不进 machines.yaml；"
            "它的 Tailscale 位由环境变量提供"
        )

    role = str(payload.get("role") or "").strip().lower()
    if role not in VALID_ROLES:
        raise ValueError("role 必须是 server 或 node")

    caps_raw = payload.get("capabilities")
    if caps_raw in (None, ""):
        caps_raw = []
    if not isinstance(caps_raw, list):
        raise ValueError("capabilities 必须是列表")
    capabilities = []
    for cap in caps_raw:
        text = str(cap).strip().lower()
        if text not in VALID_CAPABILITIES:
            raise ValueError(f"未知能力 {text}；仅允许 {'/'.join(VALID_CAPABILITIES)}")
        if text not in capabilities:
            capabilities.append(text)

    probe = None
    probe_raw = payload.get("probe")
    if isinstance(probe_raw, dict) and str(probe_raw.get("port") or "").strip() != "":
        port = validate_port(probe_raw.get("port"))
        probe_type = str(probe_raw.get("type") or "tcp").strip().lower()
        if probe_type != "tcp":
            raise ValueError("probe.type 目前只支持 tcp")
        probe = {"type": probe_type, "port": port}

    # 家宽公网探测：与 probe 同形状；缺省/空端口 = 不探（清空也走这条）
    public_probe = None
    public_raw = payload.get("public_probe")
    if isinstance(public_raw, dict):
        if str(public_raw.get("port") or "").strip() != "":
            public_type = str(public_raw.get("type") or "tcp").strip().lower()
            if public_type != "tcp":
                raise ValueError("public_probe.type 目前只支持 tcp")
            public_probe = {"type": public_type, "port": validate_port(public_raw.get("port"))}
    elif public_raw not in (None, ""):
        raise ValueError("public_probe 必须是 {port} 对象，或留空表示不探")

    quota = payload.get("traffic_quota_gb")
    if quota in (None, ""):
        quota = None
    else:
        try:
            quota = int(quota)
        except (TypeError, ValueError) as exc:
            raise ValueError("traffic_quota_gb 必须是整数 GB") from exc
        if quota < 0:
            raise ValueError("traffic_quota_gb 不能为负数")

    ssh = "" if payload.get("ssh") is None else str(payload.get("ssh")).strip()
    if ssh and not _SSH_TARGET_RE.fullmatch(ssh):
        raise ValueError("ssh 必须是 user@host 或 host 形式（首字符不能是连字符）")

    tailscale = "" if payload.get("tailscale") is None else str(payload.get("tailscale")).strip()
    if tailscale:
        try:
            ipaddress.ip_address(tailscale)
        except ValueError as exc:
            raise ValueError("tailscale 必须是 IP 地址（如 100.64.0.30）") from exc

    # ssh_key 只收目录内纯文件名：私钥文件不经 HTTP 上传，由人放进 .ssh-console/（§5.3 红线）
    ssh_key = validate_identifier(payload.get("ssh_key"), "ssh_key 文件名", allow_empty=True)

    renewal_ref = "" if payload.get("renewal_ref") is None else str(payload.get("renewal_ref")).strip()
    if renewal_ref and not _RENEWAL_REF_RE.fullmatch(renewal_ref):
        raise ValueError("renewal_ref 只能是 renewals 的 id（字母数字、下划线、连字符）")

    # systemd_units 写路径严格校验（§4.1）：非法值中文报错，与读路径的宽容相反
    if "systemd_units" not in payload:
        units_raw = []
    else:
        units_raw = payload["systemd_units"]
        if units_raw is None or units_raw == "":
            raise ValueError("systemd_units 不能为 null 或空字符串；清空请传 []")
        if not isinstance(units_raw, list):
            raise ValueError("systemd_units 必须是列表")
    systemd_units = []
    for item in units_raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("systemd unit 不能为空")
        unit = item.strip()
        if len(unit) > 128:
            raise ValueError("systemd unit 长度不能超过 128")
        if not _SYSTEMD_UNIT_RE.fullmatch(unit):
            raise ValueError(
                "systemd unit 只能含字母数字和 _ . @ : -（如 sing-box / foo@bar.service）"
            )
        if unit not in systemd_units:
            systemd_units.append(unit)
    if len(systemd_units) > SYSTEMD_UNITS_MAX:
        raise ValueError(f"systemd_units 最多 {SYSTEMD_UNITS_MAX} 项")

    # upgrade 写路径严格校验：形状 {enabled: bool, proxy_env: {K: V}}
    # enabled 缺省 False；proxy_env 键限白名单、值限字符集（要进远端 shell env）
    upgrade = None
    upgrade_raw = payload.get("upgrade")
    if upgrade_raw not in (None, ""):
        if not isinstance(upgrade_raw, dict):
            raise ValueError("upgrade 必须是 {enabled, proxy_env} 对象")
        enabled_raw = upgrade_raw.get("enabled", False)
        if not isinstance(enabled_raw, bool):
            raise ValueError("upgrade.enabled 必须是布尔值")
        proxy_env = {}
        proxy_env_raw = upgrade_raw.get("proxy_env")
        if proxy_env_raw not in (None, ""):
            if not isinstance(proxy_env_raw, dict):
                raise ValueError("upgrade.proxy_env 必须是 {KEY: value} 对象")
            for key, raw in proxy_env_raw.items():
                key_text = str(key or "").strip()
                if key_text not in _VALID_PROXY_ENV_KEYS:
                    raise ValueError(
                        f"upgrade.proxy_env 只允许 {'/'.join(sorted(_VALID_PROXY_ENV_KEYS))}"
                    )
                text = str(raw or "").strip()
                if not text:
                    continue  # 空串视为清除该键
                if not _PROXY_ENV_VALUE_RE.fullmatch(text):
                    raise ValueError(
                        f"upgrade.proxy_env.{key_text} 含非法字符"
                        "（仅允许字母数字 . , : ; / _ + = @ % -，最长 200）"
                    )
                proxy_env[key_text] = text
        if enabled_raw or proxy_env:
            upgrade = {"enabled": enabled_raw}
            if proxy_env:
                upgrade["proxy_env"] = proxy_env

    entry = {
        "id": machine_id,
        "label": _text(payload.get("label"), "label", 64) or machine_id.upper(),
        "role": role,
        "provider": _text(payload.get("provider"), "provider", 64),
        "os": _text(payload.get("os"), "os", 64),
        # region 是 emoji 旗（🇨🇳 是两个 code point），限长按字符数给宽一点
        "region": _text(payload.get("region"), "region", 16),
        "public_host": _host(payload.get("public_host"), "public_host"),
        "tailscale": tailscale,
        "ssh": ssh,
        "ssh_key": ssh_key,
        "beszel_name": _text(payload.get("beszel_name"), "beszel_name", 64) or machine_id.upper(),
        "capabilities": capabilities,
        "probe": probe,
        "public_probe": public_probe,
        "traffic_quota_gb": quota,
        "traffic_nic": validate_identifier(
            payload.get("traffic_nic"), "traffic_nic", allow_empty=True
        ),
        "renewal_ref": renewal_ref,
        "systemd_units": systemd_units,
        "upgrade": upgrade,
    }
    if role == "node" and not entry["public_host"] and not entry["tailscale"]:
        raise ValueError("node 至少要有 public_host 或 tailscale，否则无法探活")
    if public_probe and not entry["public_host"]:
        raise ValueError("配了家宽公网探测端口就必须填公网域名（public_host），否则无处可探")
    return entry


# 写回 yaml 的字段顺序：与 machines.yaml 现有手写顺序一致，diff 才好读
_YAML_FIELD_ORDER = (
    "id", "label", "role", "provider", "os", "region", "public_host",
    "tailscale", "ssh", "ssh_key", "beszel_name", "capabilities", "probe",
    "public_probe", "traffic_quota_gb", "traffic_nic", "renewal_ref", "systemd_units",
    "upgrade",
)
# 空值不写进 yaml 的字段：省得每台机器都拖一串 `provider: ''`
_OMIT_WHEN_EMPTY = (
    "provider", "os", "region", "public_host", "ssh_key", "probe", "public_probe",
    "traffic_quota_gb", "traffic_nic", "renewal_ref", "systemd_units", "upgrade",
)


def _to_yaml_entry(entry, existing=None):
    """规范化 dict → 写回 yaml 的 dict。

    `existing` 是同 id 的原始 yaml 条目：手写的未知键（本模块不认识但人放在那儿的）
    原样保留，免得一次 UI 编辑把别人加的字段吃掉。
    """
    result = {}
    for key in _YAML_FIELD_ORDER:
        value = entry.get(key)
        if key in _OMIT_WHEN_EMPTY and value in (None, "", [], {}):
            continue
        # ssh 为空串是有语义的（nas：本机跑 docker，不走 SSH），必须落盘
        result[key] = value
    for key, value in (existing or {}).items():
        if key not in _YAML_FIELD_ORDER and key not in result:
            result[key] = value
    return result


def load_machines_raw():
    """读 machines.yaml 的原始 list（不归一化）。写路径专用，缺失/坏文件直接抛。"""
    path = machines_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as file:
            loaded = yaml.safe_load(file)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"machines.yaml 读取失败: {exc}") from exc
    if loaded in (None, ""):
        return []
    if not isinstance(loaded, list):
        raise ValueError("machines.yaml 顶层必须是列表")
    return [entry for entry in loaded if isinstance(entry, dict)]


def save_machines_raw(entries):
    """写回 machines.yaml 并清缓存。契约头显式重写（PyYAML 不保留注释）。"""
    from yaml_io import atomic_write_yaml

    atomic_write_yaml(machines_path(), entries, header=MACHINES_HEADER)
    invalidate()


def upsert_machine(payload, machine_id=None, create=False):
    """新增或更新一台机器，返回归一化后的条目（不含敏感字段过滤，由 app 层决定）。

    `machine_id` 是 PUT 的路径参数；id 不可改名——assets.yaml 的 `machine`、
    probe 时序、流量账本都按 id 关联，改名等于把这些记录全孤儿化。要改名就删了重建。
    """
    entry = validate_machine_payload(payload)
    if machine_id is not None:
        target = validate_identifier(machine_id, "机器 id").lower()
        if target != entry["id"]:
            raise ValueError(
                f"不支持改机器 id（{target} → {entry['id']}）："
                "服务台账、探测时序、流量账本都按 id 关联，改名会丢历史；请删除后重建"
            )

    entries = load_machines_raw()
    index = next(
        (
            i for i, item in enumerate(entries)
            if str(item.get("id") or "").strip().lower() == entry["id"]
        ),
        None,
    )
    if create and index is not None:
        raise ValueError(f"机器已存在: {entry['id']}")
    if not create and index is None:
        raise ValueError(f"机器不存在: {entry['id']}")

    if index is None:
        entries.append(_to_yaml_entry(entry))
    else:
        entries[index] = _to_yaml_entry(entry, existing=entries[index])

    save_machines_raw(entries)
    return entry


def delete_machine(machine_id):
    """删除一台机器。调用方须先做「机器上还有服务」防呆（app 层持有 assets.yaml 路径）。"""
    target = validate_identifier(machine_id, "机器 id").lower()
    entries = load_machines_raw()
    remaining = [
        item for item in entries
        if str(item.get("id") or "").strip().lower() != target
    ]
    if len(remaining) == len(entries):
        raise ValueError(f"机器不存在: {target}")
    save_machines_raw(remaining)


def services_on_machine(services, machine_id):
    """assets.yaml 里挂在这台机器上的服务名（删除防呆用）。"""
    target = str(machine_id or "").strip().lower()
    names = []
    for svc in services or []:
        if not isinstance(svc, dict):
            continue
        if str(svc.get("machine") or "").strip().lower() == target:
            names.append(str(svc.get("name") or "").strip() or "(未命名)")
    return names


def get_machine(machine_id):
    machine = str(machine_id or "").strip().lower()
    if not machine:
        return None
    return load_machines().get(machine)


def machine_ids(force=False):
    return list(load_machines(force=force).keys())


def has_capability(machine_id, capability):
    machine = get_machine(machine_id)
    if not machine:
        return False
    return str(capability or "").strip().lower() in machine["capabilities"]


def upgrade_config(machine_id):
    """机器的服务升级配置（{enabled, proxy_env?}）。未配置或机器不存在返回 None。"""
    machine = get_machine(machine_id)
    if not machine:
        return None
    upgrade = machine.get("upgrade")
    return dict(upgrade) if upgrade else None


def _env_key(prefix, machine_id):
    return "{}_{}".format(prefix, str(machine_id or "").strip().upper())


def tailscale_ip(machine_id):
    """Tailscale IP：env > yaml > 家庭机器 env 位。无则空串。"""
    machine = str(machine_id or "").strip().lower()
    env_value = os.environ.get(_env_key("MACHINE_TAILSCALE", machine), "").strip()
    if env_value:
        return env_value
    entry = get_machine(machine)
    if entry and entry["tailscale"]:
        return entry["tailscale"]
    return _extra_tailscale().get(machine, "").strip()


def probe_host(machine_id):
    """探活/健康检查目标主机：Tailscale 优先，退到公网域名。"""
    return tailscale_ip(machine_id) or (get_machine(machine_id) or {}).get("public_host", "")


def ssh_target(machine_id):
    """SSH target（user@host）。返回 None = 本机执行，不走 SSH。

    env `MACHINE_SSH_<ID>` 优先；env 显式留空视为「未设」，回退 yaml。
    yaml 里 ssh 为空串同样表示本机（nas 生产形态）。
    """
    machine = str(machine_id or "").strip().lower()
    env_value = os.environ.get(_env_key("MACHINE_SSH", machine))
    if env_value is not None and env_value.strip():
        return env_value.strip()
    entry = get_machine(machine)
    if entry and entry["ssh"]:
        return entry["ssh"]
    return None


def ssh_key_path(machine_id):
    """私钥绝对路径（容器内 /root/.ssh/<file>）。未配置返回 None。"""
    entry = get_machine(machine_id)
    if not entry or not entry["ssh_key"]:
        return None
    name = entry["ssh_key"]
    # 只接受目录内的纯文件名：yaml 里的值会进 ssh -i 参数，不许穿越出密钥目录
    if name != os.path.basename(name) or name in (".", ".."):
        return None
    return os.path.join(SSH_KEY_DIR, name)


def ssh_targets():
    """{id: ssh_target}，供 service_manager 的 MACHINE_SSH 兜底构建。"""
    return {mid: ssh_target(mid) for mid in load_machines()}


def beszel_name_map():
    """{beszel_name(大写): machine id}，用于 Beszel system name → 机器 id。"""
    return {
        entry["beszel_name"].upper(): entry["id"]
        for entry in load_machines().values()
        if entry["beszel_name"]
    }


def unknown_machine_ids(services):
    """assets.yaml 里出现但台账没有的机器 id（防两份 yaml 漂移）。"""
    known = set(load_machines()) | home_machines()
    unknown = []
    for svc in services or []:
        if not isinstance(svc, dict):
            continue
        machine = str(svc.get("machine") or "").strip().lower()
        if machine and machine not in known and machine not in unknown:
            unknown.append(machine)
    return sorted(unknown)


def suggest_access(machine, port, path_suffix=""):
    """根据机器与端口生成建议的家庭 / TC URL。"""
    if not port:
        return {"home": "", "tailscale": ""}
    try:
        port = int(port)
    except (TypeError, ValueError):
        return {"home": "", "tailscale": ""}

    machine = (machine or _host_policy()["local_machine"]).lower().strip()
    suffix = path_suffix or ""
    if suffix:
        if suffix.startswith("#"):
            suffix = "/" + suffix
        elif not suffix.startswith("/"):
            suffix = "/" + suffix

    home = ""
    lan_ip = home_lan_ip()
    if lan_ip and machine in home_machines():
        home = f"http://{lan_ip}:{port}{suffix}"

    tailscale = ""
    ts_ip = tailscale_ip(machine)
    if ts_ip:
        tailscale = f"http://{ts_ip}:{port}{suffix}"

    return {"home": home, "tailscale": tailscale}


def machines_for_api():
    """`GET /api/machines` 的静态部分（不含探活/续费，由 app 层混入）。

    刻意不外泄 tailscale / ssh / ssh_key：前端不需要，减少内网地址与凭据路径面。
    """
    result = []
    for entry in load_machines().values():
        result.append({
            "id": entry["id"],
            "label": entry["label"],
            "role": entry["role"],
            "provider": entry["provider"],
            "os": entry["os"],
            "region": entry["region"],
            "public_host": entry["public_host"],
            "capabilities": list(entry["capabilities"]),
            # 保持 §4.1 的嵌套形状：前端 node-card 读 probe.type / probe.port
            "probe": dict(entry["probe"]) if entry["probe"] else None,
            # 家宽公网探测配置（端口）；结果由 app 层从缓存混入，这里只给形状
            "public_probe": dict(entry["public_probe"]) if entry["public_probe"] else None,
            "traffic_quota_gb": entry["traffic_quota_gb"],
            # J-B：systemd 展示白名单（§4.1）；白名单只决定显示，不授予操作
            "systemd_units": list(entry["systemd_units"]),
            # 升级闸：前端据此决定服务卡是否出现 upgrade 按钮
            "upgrade": dict(entry["upgrade"]) if entry["upgrade"] else None,
        })
    return result


def machine_for_edit(machine_id):
    """`GET /api/machines/<id>?full=1` 的编辑视图：含 tailscale / ssh / ssh_key。

    列表接口刻意不外泄这三项（少暴露内网地址与凭据路径面），但编辑表单必须看到当前值，
    否则用户一保存就把它们清空。`ssh_key` 只是 `.ssh-console/` 内的**文件名**，
    私钥内容永不经 HTTP（§5.3 红线）。
    """
    entry = get_machine(machine_id)
    if not entry:
        return None
    return {
        "id": entry["id"],
        "label": entry["label"],
        "role": entry["role"],
        "provider": entry["provider"],
        "os": entry["os"],
        "region": entry["region"],
        "public_host": entry["public_host"],
        "tailscale": entry["tailscale"],
        "ssh": entry["ssh"],
        "ssh_key": entry["ssh_key"],
        "beszel_name": entry["beszel_name"],
        "capabilities": list(entry["capabilities"]),
        "probe": dict(entry["probe"]) if entry["probe"] else None,
        "public_probe": dict(entry["public_probe"]) if entry["public_probe"] else None,
        "traffic_quota_gb": entry["traffic_quota_gb"],
        "traffic_nic": entry["traffic_nic"],
        "renewal_ref": entry["renewal_ref"],
        "systemd_units": list(entry["systemd_units"]),
        "upgrade": dict(entry["upgrade"]) if entry["upgrade"] else None,
    }


def machines_for_config():
    """`/api/config` 的 machines 段。roles 为新增字段（台账收编后可用）。"""
    tailscale = {}
    for machine_id in list(load_machines()) + list(_extra_tailscale()):
        ip = tailscale_ip(machine_id)
        if ip:
            tailscale[machine_id] = ip
    return {
        "home_lan_ip": home_lan_ip(),
        "home_machines": sorted(home_machines()),
        "tailscale": tailscale,
        "roles": {entry["id"]: entry["role"] for entry in load_machines().values()},
    }
