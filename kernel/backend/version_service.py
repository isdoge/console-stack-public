# -*- coding: utf-8 -*-
"""单服务版本同步：Docker 镜像版本 + GitHub 上游 + 更新区间。

存储（PG-02）：`versions` 表的读写走 `storage_dispatch`（PG 下 `%s` + 显式 UPSERT）。
`last_checked` 统一写 **UTC** 文本——旧实现用 `datetime.now()`（本机时区），
在 Mac 上会与 NAS 写出两种时间口径（提案 §4.3：同一写路径不得因本机时区产生
另一套服务器时间）。格式不变，仍是 `YYYY-MM-DD HH:MM:SS`。
"""
import io
import json
import os
import re
import shlex
import socket
import struct
import subprocess
import tarfile
import urllib.error
import urllib.request
from datetime import datetime, timezone

import yaml

import collect_guard
import composition
import machine_config as mc
import paths
import service_manager as sm
import storage_dispatch as sd
from validation import validate_identifier, validate_port

DB_PATH = paths.DB_PATH
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

GENERIC_TAGS = frozenset({"latest", "main", "master", "stable", "develop", "dev", "edge"})
VERSION_LABEL_KEYS = (
    "org.opencontainers.image.version",
    "org.label-schema.version",
    "version",
    "build_version",
)
# 常见镜像的内置探测（compose 常写 :latest，真实版本在这些位置）
_BUILTIN_PROBES = {
    "mihomo": {"command": ["/mihomo", "-v"], "pattern": r"Meta v([\d.]+)"},
    "metacubexd": {"file": "/app/.output/server/package.json", "json_key": "version"},
    "magpie": {"command": ["magpie", "--version"], "pattern": r"v?(\d+\.\d+\.\d+)"},
}
_SAFE_CONTAINER_FILE_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
_SAFE_HTTP_PATH_RE = re.compile(r"^/[A-Za-z0-9._~%/-]*$")
_SAFE_JSON_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_UNKNOWN_VERSIONS = frozenset({
    "",
    "unknown",
    "未知",
    "n/a",
    "latest",
    "running (docker)",
})


def _parse_image_tag(image_ref):
    if not image_ref:
        return ""
    ref = image_ref.split("/")[-1]
    if "@" in ref:
        return ref.split("@", 1)[0].split(":")[-1] if ":" in ref.split("@")[0] else "latest"
    if ":" in ref:
        return ref.split(":", 1)[1]
    return "latest"


def _version_from_labels(labels):
    if not labels:
        return None
    if isinstance(labels, str):
        try:
            labels = json.loads(labels)
        except json.JSONDecodeError:
            return None
    for key in VERSION_LABEL_KEYS:
        val = labels.get(key)
        if val and str(val).strip():
            return str(val).strip()
    return None


def _format_semver(version):
    v = str(version).strip()
    if re.match(r"^\d+\.\d+", v) and not v.startswith("v"):
        return f"v{v}"
    return v


def _exec_probes_allowed(machine):
    """组合本机经 socket-proxy 禁止 docker exec 探针；远端 SSH 机器仍可用。

    NAS 仍保留原禁止规则，不因组合本机改名扩大权限。
    可用 CONSOLE_ALLOW_DOCKER_EXEC_PROBES=1 强制打开（仅调试），默认按机器判定。
    """
    override = os.environ.get("CONSOLE_ALLOW_DOCKER_EXEC_PROBES", "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    if override in ("0", "false", "no", "off"):
        return False
    return bool(machine) and machine != "nas" and not sm.is_local_machine(machine)


def _probe_exec_version(machine, container, command, pattern):
    if not _exec_probes_allowed(machine):
        return None
    cmd = sm._docker_cmd(machine, ["exec", container, *command])
    code, out, _ = sm._run(cmd, timeout=15)
    if code != 0 or not out.strip():
        return None
    if pattern:
        m = re.search(pattern, out)
        if m:
            return _format_semver(m.group(1))
    line = out.strip().splitlines()[0].strip()
    return line[:80] if line else None


def _run_bytes(cmd, timeout=20):
    """运行固定参数命令并保留二进制 stdout，供 docker cp tar 流使用。"""
    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        return 124, b"", b"timeout"
    except Exception as exc:
        return 1, b"", str(exc).encode("utf-8", errors="replace")


def _copy_container_file(machine, container, path):
    """通过只读 docker cp 获取容器文件，不依赖 Docker EXEC 权限。"""
    try:
        container = validate_identifier(container, "container")
    except ValueError:
        return None
    path = str(path or "")
    if (
        not _SAFE_CONTAINER_FILE_RE.fullmatch(path)
        or ".." in path.split("/")
    ):
        return None

    cmd = sm._docker_cmd(machine, ["cp", f"{container}:{path}", "-"])
    code, out, _ = _run_bytes(cmd, timeout=20)
    if code != 0 or not out:
        return None

    try:
        with tarfile.open(fileobj=io.BytesIO(out), mode="r:*") as archive:
            expected = os.path.basename(path)
            members = [
                member for member in archive.getmembers()
                if member.isfile() and os.path.basename(member.name) == expected
            ]
            if not members:
                return None
            member = members[0]
            if member.size > 2 * 1024 * 1024:
                return None
            file_obj = archive.extractfile(member)
            return file_obj.read() if file_obj else None
    except (tarfile.TarError, OSError):
        return None


def _probe_file_version(machine, container, path, json_key="version"):
    payload = _copy_container_file(machine, container, path)
    if not payload:
        return None
    try:
        data = json.loads(payload)
        val = data.get(json_key)
        return _format_semver(val) if val else None
    except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
        return None


def _probe_builtin(svc, machine, container, image_ref):
    """按配置或内置规则探测版本；文件探针只读，命令探针仍受 EXEC 门禁。"""
    v_conf = svc.get("version") or {}
    probe = v_conf.get("probe") or {}
    if probe.get("command"):
        return _probe_exec_version(machine, container, probe["command"], probe.get("pattern"))
    if probe.get("file"):
        return _probe_file_version(machine, container, probe["file"], probe.get("json_key", "version"))

    name_key = (container or svc.get("name") or "").lower()
    image_key = (image_ref or "").lower()
    for key, spec in _BUILTIN_PROBES.items():
        if key in name_key or key in image_key:
            if spec.get("command"):
                ver = _probe_exec_version(machine, container, spec["command"], spec.get("pattern"))
            else:
                ver = _probe_file_version(machine, container, spec["file"], spec.get("json_key", "version"))
            if ver:
                return ver
    return None


def _default_gateway_address():
    """读取 Linux 容器默认网关，用于访问宿主机仅本机监听的版本接口。"""
    try:
        with open("/proc/net/route", encoding="ascii") as route_file:
            next(route_file, None)
            for line in route_file:
                fields = line.split()
                if len(fields) < 4 or fields[1] != "00000000":
                    continue
                flags = int(fields[3], 16)
                if not flags & 0x2:
                    continue
                return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except (OSError, ValueError, IndexError):
        return None
    return None


def _json_version(payload, json_key):
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None
    value = data
    for key in json_key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return _format_semver(value) if value not in (None, "") else None


def _probe_machine_http_version(machine, port, path, json_key="version"):
    """读取机器本机 HTTP 版本接口；地址固定为 loopback/容器宿主网关。"""
    try:
        machine = sm._validate_machine(machine)
        port = validate_port(port)
    except ValueError:
        return None

    path = str(path or "")
    json_key = str(json_key or "")
    if (
        not port
        or not _SAFE_HTTP_PATH_RE.fullmatch(path)
        or ".." in path.split("/")
        or not _SAFE_JSON_KEY_RE.fullmatch(json_key)
    ):
        return None

    ssh_target = sm.MACHINE_SSH[machine]
    if ssh_target:
        url = f"http://127.0.0.1:{port}{path}"
        remote = (
            "curl --fail --silent --show-error --max-time 8 "
            + shlex.quote(url)
        )
        code, out, _ = sm._run(
            ["ssh", *sm.SSH_OPTS, ssh_target, remote],
            timeout=12,
        )
        return _json_version(out, json_key) if code == 0 and out.strip() else None

    hosts = ["127.0.0.1"]
    gateway = _default_gateway_address()
    if gateway and gateway not in hosts:
        hosts.append(gateway)
    for host in hosts:
        url = f"http://{host}:{port}{path}"
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "OpsConsole/1.0"},
            )
            with urllib.request.urlopen(req, timeout=8) as response:
                payload = response.read(64 * 1024)
            version = _json_version(payload, json_key)
            if version:
                return version
        except (OSError, urllib.error.URLError, ValueError):
            continue
    return None


def _assets_path():
    return str(composition.config_path("assets.yaml", "CONSOLE_ASSETS_PATH"))


def _parse_github_repo(repo_url):
    if not repo_url:
        return None, None
    m = re.search(r"github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", repo_url.strip())
    if not m:
        return None, None
    return m.group(1), m.group(2)


def _gh_api(path):
    url = f"https://api.github.com{path}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "OpsConsole/1.0"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read())


def _inspect_container(machine, container):
    """docker inspect 完整 JSON，避免 SSH 远程 shell 破坏 -f 模板引号。"""
    cmd = sm._docker_cmd(machine, ["inspect", container])
    code, out, _ = sm._run(cmd, timeout=20)
    if code != 0 or not out.strip():
        return None
    try:
        data = json.loads(out)
        return data[0] if data else None
    except json.JSONDecodeError:
        return None


def get_local_docker_version(svc):
    """读取容器真实版本：OCI 标签 > 非 generic tag >（可选）容器内探测。

    不因 :latest 无法探针而冒充「最新」——generic tag 且无 label/exec 时返回 None，
    由上层记为「未知」/「本地未知」。NAS 侧不调用 docker exec（socket-proxy EXEC=0）。
    """
    machine = svc.get("machine") or mc.local_machine()
    container = svc.get("container") or svc.get("name")
    if not machine or not container:
        return None

    info = _inspect_container(machine, container)
    if not info:
        return None

    labels = (info.get("Config") or {}).get("Labels") or {}
    label_ver = _version_from_labels(labels)
    if label_ver:
        return label_ver

    image = (info.get("Config") or {}).get("Image") or ""
    tag = _parse_image_tag(image)
    if tag and tag.lower() not in GENERIC_TAGS:
        return tag

    probed = _probe_builtin(svc, machine, container, image)
    if probed:
        return probed

    # generic tag（latest 等）且无可用探针 → 未知，勿把 latest 当版本号
    return None


def get_local_service_version(svc):
    """按服务类型/版本探针读取本机版本。"""
    version = svc.get("version") or {}
    kind = version.get("kind", "image")
    if kind == "http":
        machine = svc.get("machine") or mc.local_machine()
        if not machine:
            return None
        probe = version.get("probe") or {}
        return _probe_machine_http_version(
            machine,
            svc.get("port"),
            probe.get("http_path"),
            probe.get("json_key", "version"),
        )
    if svc.get("type", "docker") == "docker":
        return get_local_docker_version(svc)
    return None


def get_upstream_release(repo_url):
    owner, repo = _parse_github_repo(repo_url)
    if not owner:
        return None, None, []
    try:
        data = _gh_api(f"/repos/{owner}/{repo}/releases/latest")
        tag = data.get("tag_name")
        body = data.get("body") or ""
        return tag, body, []
    except Exception:
        pass
    try:
        tags = _gh_api(f"/repos/{owner}/{repo}/tags?per_page=1")
        if tags:
            return tags[0].get("name"), "", []
    except Exception:
        pass
    return None, None, []


def _normalize_tag(tag):
    if not tag:
        return ""
    # 剥 semver build metadata（`+` 之后）：上游 release tag 常带构建/打包信息
    # （如 ignis 的 v0.8.11+obsidian.1.12.7），本地探针只报核心版本号，
    # 不剥会让已对齐的服务永远显示「可更新」。
    return str(tag).split("+", 1)[0].lstrip("v").strip()


def _is_known_version(version):
    return str(version or "").strip().lower() not in _UNKNOWN_VERSIONS


def _versions_differ(local, upstream):
    return (
        _is_known_version(local)
        and _is_known_version(upstream)
        and _normalize_tag(local) != _normalize_tag(upstream)
    )


def get_compare_changelog(repo_url, base_tag, head_tag):
    """获取 base..head 之间的 commits 列表。

    任一端版本未知（如 generic tag 探不出）时不拼 compare URL——那会生成
    `…/compare/未知...v1.2.3` 这种死链。统一落到 releases 页。
    """
    owner, repo = _parse_github_repo(repo_url)
    if not owner:
        return [], None
    releases_url = f"https://github.com/{owner}/{repo}/releases"
    if not _is_known_version(base_tag) or not _is_known_version(head_tag):
        return [], releases_url
    if _normalize_tag(base_tag) == _normalize_tag(head_tag):
        return [], releases_url
    try:
        data = _gh_api(f"/repos/{owner}/{repo}/compare/{base_tag}...{head_tag}")
        commits = []
        for c in (data.get("commits") or [])[:30]:
            commit = c.get("commit") or {}
            commits.append({
                "sha": (c.get("sha") or "")[:7],
                "msg": (commit.get("message") or "").split("\n")[0],
                "date": (commit.get("author") or {}).get("date", ""),
            })
        return commits, data.get("html_url") or releases_url
    except Exception:
        return [], releases_url


def sync_service_version(svc, db_path=None, conn=None):
    """同步单个服务版本到 `versions` 表，返回版本 dict。

    非 authority 实例不做上游抓取、不写库（Q1），返回 None 由调用方回落缓存。
    """
    name = svc.get("name")
    v_conf = svc.get("version") or {}
    repo_url = v_conf.get("repo_url", "")
    if not v_conf.get("enabled") and not repo_url:
        return None
    if not collect_guard.collection_allowed():
        print(collect_guard.collection_skipped(f"版本同步 {name}"))
        return None

    track_upstream = v_conf.get("track_upstream", True)

    local = get_local_service_version(svc)
    if not local:
        local = "未知"

    upstream, release_body, _ = get_upstream_release(repo_url) if repo_url else (None, None, [])
    if not upstream:
        upstream = "未知"

    outdated = _versions_differ(local, upstream) if track_upstream else False

    changes, compare_url = [], None
    if repo_url and _is_known_version(local) and _is_known_version(upstream):
        changes, compare_url = get_compare_changelog(repo_url, local, upstream)
    if not changes and repo_url:
        owner, repo = _parse_github_repo(repo_url)
        if owner:
            try:
                commits_data = _gh_api(f"/repos/{owner}/{repo}/commits?per_page=10")
                changes = [
                    {
                        "sha": (c.get("sha") or "")[:7],
                        "msg": (c.get("commit") or {}).get("message", "").split("\n")[0],
                        "date": (c.get("commit") or {}).get("author", {}).get("date", ""),
                    }
                    for c in commits_data
                ]
            except Exception:
                pass

    if not track_upstream:
        status = "自部署"
    elif not _is_known_version(local):
        status = "本地未知"
    elif not _is_known_version(upstream):
        status = "仅本地"
    else:
        status = "可更新" if outdated else "最新"

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    row = {
        "local": local,
        "upstream": upstream,
        "behind": 1 if outdated else 0,
        "status": status,
        "changes": changes,
        "release_notes": release_body or "",
        "compare_url": compare_url,
        "last_checked": now_str,
    }

    ph = sd.ph()
    # 表由 schema 迁移建好（PG-00 的 pg_migrations / SQLite v1–v11），
    # 应用侧不再 CREATE TABLE——那会让运行账号尝试 DDL，在 PG 上直接权限不足。
    with sd.write_tx(conn=conn, db_path=db_path) as own:
        own.execute(
            f"""
            INSERT INTO versions (svc_name, local_version, upstream_version, behind_count,
                                  update_status, recent_changes, last_checked)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
            ON CONFLICT(svc_name) DO UPDATE SET
                local_version=excluded.local_version,
                upstream_version=excluded.upstream_version,
                behind_count=excluded.behind_count,
                update_status=excluded.update_status,
                recent_changes=excluded.recent_changes,
                last_checked=excluded.last_checked
            """,
            (
                name, local, upstream, row["behind"], status,
                json.dumps(changes, ensure_ascii=False), now_str,
            ),
        )
    return row


def _cached_detail(name, db_path=None, conn=None):
    """读 `versions` 缓存行；没有则返回 None（调用方决定是否同步）。"""
    ph = sd.ph()
    with sd.read_tx(conn=conn, db_path=db_path) as own:
        row = own.execute(
            f"SELECT * FROM versions WHERE svc_name = {ph}", (name,)
        ).fetchone()
    if not row:
        return None
    return {
        "local": row["local_version"],
        "upstream": row["upstream_version"],
        "behind": row["behind_count"],
        "status": row["update_status"],
        "changes": json.loads(row["recent_changes"] or "[]"),
        "release_notes": "",
        "compare_url": None,
        "last_checked": row["last_checked"],
    }


def get_service_version_detail(name, force_sync=False, db_path=None, conn=None):
    path = _assets_path()
    with open(path, encoding="utf-8") as f:
        services = yaml.safe_load(f) or []
    svc = next((s for s in services if s.get("name") == name), None)
    if not svc:
        return None

    if force_sync:
        # client 上 sync_service_version 返回 None（不采集）→ 回落共享缓存，
        # 而不是把「不能同步」显示成「没有版本信息」。
        detail = sync_service_version(svc, db_path=db_path, conn=conn) or _cached_detail(
            name, db_path=db_path, conn=conn
        )
    else:
        detail = _cached_detail(name, db_path=db_path, conn=conn)
        if detail is None:
            detail = sync_service_version(svc, db_path=db_path, conn=conn)
        else:
            stale_local = detail["local"] in (
                "Running (Docker)", "N/A", "未知", None, "Unknown", "latest",
            )
            if stale_local:
                detail = sync_service_version(svc, db_path=db_path, conn=conn) or detail

    v_conf = svc.get("version") or {}
    track_upstream = v_conf.get("track_upstream", True)

    if detail and v_conf.get("repo_url"):
        _, body, _ = get_upstream_release(v_conf["repo_url"])
        if body:
            detail["release_notes"] = body
        local, upstream = detail.get("local"), detail.get("upstream")
        ch, url = get_compare_changelog(v_conf["repo_url"], local, upstream)
        if ch:
            detail["changes"] = ch
        if url:
            detail["compare_url"] = url

    detail = detail or {}
    detail["repo_url"] = v_conf.get("repo_url", "")
    detail["track_upstream"] = track_upstream
    detail["outdated"] = track_upstream and _versions_differ(
        detail.get("local"),
        detail.get("upstream"),
    )
    return detail
