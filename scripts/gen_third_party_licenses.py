#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 `kernel/THIRD_PARTY_LICENSES.txt`：公共分发物的依赖闭包许可全文（B7 t41u 缺口 4）。

三段闭包，各自从**实际安装物**读许可文件，不手抄：

- npm：`kernel/frontend/package-lock.json` 中非 dev 的全部包（生产闭包），
  许可文件读 `kernel/frontend/node_modules/<pkg>/`（先 `npm ci`）。
- Python：`--python-json` 指向在**只装公共运行依赖**的解释器里执行
  `gen_third_party_licenses.py --dump-python` 得到的 JSON（镜像同版 python:3.10）。
- Go：`go version -m` 列出的实际链接进 agent 二进制的模块（离线读模块缓存）。

- 上游原文：包内没有许可文件的条目（如部分 npm 包、flask-cors、Go 标准库）以及镜像
  里不来自上述三段闭包的分发物（Node.js 二进制、CPython、psycopg-binary 内嵌原生库、
  docker CLI/插件），按 `scripts/third_party_upstream.json` 从**上游原始来源**下载，
  逐个校验固定 sha256 后内联，并写明来源 URL。
- 镜像盘点：`--image IMAGE` 在镜像里跑本脚本 `--dump-image`，取 dpkg 包清单与
  `/usr/share/doc/*/copyright`、`/usr/share/common-licenses/*`（Debian 包随附的版权文件
  即其发行原文），以及 psycopg 原生库、node/python/docker 版本；docker 二进制拷出后用
  `go version -m` 列实际链接模块，经 GOPROXY 下载模块（sumdb 校验）取许可。
- 覆盖声明：输出文末 COVERAGE 段逐行列出覆盖到的镜像成分；镜像内
  `--verify-image /app/THIRD_PARTY_LICENSES.txt` 据此核对实际安装物（CI/发布用）。

找不到许可全文的条目不硬写「已关闭」，统一列入文末 GAPS 段，退出码仍为 0；
`--check` 时 GAPS 非空则退出 1（供发布前人工裁决）。
"""
import argparse
import hashlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tarfile
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "kernel" / "frontend"
AGENT = ROOT / "kernel" / "agent"
OUT = ROOT / "kernel" / "THIRD_PARTY_LICENSES.txt"
LICENSE_PREFIXES = ("license", "licence", "copying", "notice")
UPSTREAM = ROOT / "scripts" / "third_party_upstream.json"
DEFAULT_CACHE = pathlib.Path(os.environ.get("XDG_CACHE_HOME", pathlib.Path.home() / ".cache")) \
    / "console-stack-third-party"
PSYCOPG_LIBS = "psycopg_binary.libs"
DOCKER_BINS = {
    "docker": "/usr/bin/docker",
    "docker-buildx": "/usr/libexec/docker/cli-plugins/docker-buildx",
    "docker-compose": "/usr/libexec/docker/cli-plugins/docker-compose",
}


def _license_files(directory):
    directory = pathlib.Path(directory)
    if not directory.is_dir():
        return []
    found = []
    for entry in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_file() and entry.name.lower().startswith(LICENSE_PREFIXES):
            found.append(entry)
    return found


def _read(path):
    return path.read_text(encoding="utf-8", errors="replace").strip()


def npm_entries():
    lock = json.loads((FRONTEND / "package-lock.json").read_text(encoding="utf-8"))
    entries, gaps = [], []
    for key, meta in sorted(lock.get("packages", {}).items()):
        if not key or meta.get("dev") or meta.get("devOptional"):
            continue
        name = key.split("node_modules/")[-1]
        version = meta.get("version", "?")
        files = _license_files(FRONTEND / key)
        if not files:
            if meta.get("optional") and not (FRONTEND / key).exists():
                # 他平台可选原生包（如 @rollup/rollup-linux-*）：本机未安装，不进本机构建产物。
                gaps.append(f"npm {name}@{version}: optional platform package not installed here "
                            f"(license field: {meta.get('license', '?')})")
                continue
            gaps.append(f"npm {name}@{version}: no license file in package "
                        f"(license field: {meta.get('license', '?')})")
            continue
        entries.append((f"npm {name}@{version}", meta.get("license", "?"),
                        [(f.name, _read(f)) for f in files]))
    return entries, gaps


def dump_python():
    """在只装公共运行依赖的解释器里运行：输出 JSON 到 stdout。"""
    from importlib import metadata
    rows = []
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
        name = dist.metadata["Name"]
        if name.lower() in {"pip", "setuptools", "wheel"}:
            continue
        texts = []
        for f in dist.files or []:
            base = pathlib.PurePosixPath(str(f)).name.lower()
            if base.startswith(LICENSE_PREFIXES) or "/licenses/" in str(f).lower():
                try:
                    texts.append((str(f), pathlib.Path(dist.locate_file(f)).read_text(
                        encoding="utf-8", errors="replace").strip()))
                except (OSError, IsADirectoryError):
                    pass
        rows.append({"name": name, "version": dist.version,
                     "license": dist.metadata.get("License-Expression")
                     or dist.metadata.get("License") or "?",
                     "files": texts})
    json.dump(rows, sys.stdout, ensure_ascii=False, indent=1)


def python_entries(path):
    entries, gaps = [], []
    for row in json.loads(pathlib.Path(path).read_text(encoding="utf-8")):
        label = f"python {row['name']}=={row['version']}"
        license_field = (row.get("license") or "?").splitlines()[0][:120]
        if not row["files"]:
            gaps.append(f"{label}: no license file in installed dist (metadata: {license_field})")
            continue
        entries.append((label, license_field, [(name, text) for name, text in row["files"]]))
    return entries, gaps


def go_entries():
    """按**实际链接进 agent 二进制**的模块（`go version -m`）取许可，离线读模块缓存。"""
    import tempfile
    entries, gaps = [], []
    env = dict(os.environ, GOFLAGS="-mod=readonly", GOPROXY="off")
    info = ""
    with tempfile.TemporaryDirectory() as tmp:
        # 分发目标是 Linux agent（amd64/arm64）；平台相关依赖按目标构建取并集。
        for goarch in ("amd64", "arm64"):
            binary = pathlib.Path(tmp) / f"console-agent-{goarch}"
            subprocess.run(["go", "build", "-o", str(binary), "."], cwd=AGENT,
                           env=dict(env, GOOS="linux", GOARCH=goarch, CGO_ENABLED="0"),
                           check=True, capture_output=True)
            info += subprocess.run(["go", "version", "-m", str(binary)], env=env, check=True,
                                   capture_output=True, text=True).stdout
    modcache = subprocess.run(["go", "env", "GOMODCACHE"], env=env, check=True,
                              capture_output=True, text=True).stdout.strip()
    goroot = subprocess.run(["go", "env", "GOROOT"], env=env, check=True,
                            capture_output=True, text=True).stdout.strip()
    goversion = subprocess.run(["go", "env", "GOVERSION"], env=env, check=True,
                               capture_output=True, text=True).stdout.strip()
    root_files = _license_files(goroot)
    if root_files:
        entries.append((f"go standard library/runtime ({goversion})", "BSD-3-Clause",
                        [(f.name, _read(f)) for f in root_files]))
    else:
        gaps.append(f"go standard library ({goversion}): LICENSE not found under GOROOT")
    seen = set()
    for line in info.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[0] != "dep" or (parts[1], parts[2]) in seen:
            continue
        seen.add((parts[1], parts[2]))
        path, version = parts[1], parts[2]
        escaped = "".join("!" + c.lower() if c.isupper() else c for c in path)
        label = f"go {path}@{version}"
        files = _license_files(pathlib.Path(modcache) / f"{escaped}@{version}")
        if not files:
            gaps.append(f"{label}: no license file in module cache")
            continue
        entries.append((label, "see text", [(f.name, _read(f)) for f in files]))
    return entries, gaps


# ---------------------------------------------------------------------------
# 上游原文（固定 URL + sha256）
# ---------------------------------------------------------------------------
def fetch(url, sha256, cache):
    """下载上游原文（带本地缓存），校验 sha256；不匹配即抛错（fail closed）。"""
    cache = pathlib.Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    if sha256:
        cached = cache / sha256
        if cached.is_file() and hashlib.sha256(cached.read_bytes()).hexdigest() == sha256:
            return cached.read_bytes()
    request = urllib.request.Request(url, headers={"User-Agent": "console-stack-licenses/1"})
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 — 固定上游 URL
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if sha256 and digest != sha256:
        raise RuntimeError(f"sha256 mismatch for {url}: expected {sha256}, got {digest}")
    (cache / digest).write_bytes(data)
    return data


def extract(data, spec):
    """按清单 extract 规则从上游原文取许可段；规则缺省即全文。"""
    spec = spec or {}
    if "tar_member" in spec:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            member = tar.extractfile(spec["tar_member"])
            if member is None:
                raise RuntimeError(f"tar member missing: {spec['tar_member']}")
            data = member.read()
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    if spec.get("head_comment"):
        match = re.match(r"\s*(/\*.*?\*/)", text, flags=re.S)
        if not match:
            raise RuntimeError("leading comment not found")
        text = match.group(1)
    if "lines_matching" in spec:
        text = "\n".join(line for line in text.splitlines()
                         if re.search(spec["lines_matching"], line))
    if "start" in spec:
        start = re.search(spec["start"], text, flags=re.M)
        if not start:
            raise RuntimeError(f"start marker not found: {spec['start']}")
        text = text[start.start():]
        end = re.search(spec["end"], text[start.end() - start.start():], flags=re.M) \
            if spec.get("end") else None
        if end:
            text = text[:start.end() - start.start() + end.start()]
    text = text.strip()
    if not text:
        raise RuntimeError("extracted license text is empty")
    return text


def load_upstream(path=UPSTREAM):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def pin_upstream(path, cache):
    """首次固定：下载缺 sha256 的上游原文，写回清单（之后一律校验）。"""
    manifest = load_upstream(path)
    items = [t for e in manifest["entries"] for t in e["texts"]]
    items.append(manifest["docker_cli_vendor"])
    for item in items:
        data = fetch(item["url"], item.get("sha256"), cache)
        extract(data, item.get("extract"))
        item["sha256"] = hashlib.sha256(data).hexdigest()
    pathlib.Path(path).write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n",
                                  encoding="utf-8")
    print(json.dumps({"pinned": len(items)}))


def upstream_entries(manifest, cache):
    """返回 {section: [entry]}、covers 列表、错误列表。"""
    by_section, covers, errors = {}, [], []
    for item in manifest["entries"]:
        files = []
        try:
            for text in item["texts"]:
                if not text.get("sha256"):
                    raise RuntimeError(f"{text['url']}: sha256 not pinned")
                body = extract(fetch(text["url"], text["sha256"], cache), text.get("extract"))
                files.append((f"{text['name']} — source: {text['url']} "
                              f"(sha256 {text['sha256'][:16]}…)", body))
        except Exception as exc:  # noqa: BLE001 — 任一来源失败即记缺口，不半写
            errors.append(f"{item['label']}: upstream fetch failed ({exc})")
            continue
        if item.get("note"):
            files.insert(0, ("note", item["note"]))
        by_section.setdefault(item["section"], []).append(
            (item["label"], item["license"], files))
        covers.extend(item.get("covers", []))
    return by_section, covers, errors


# ---------------------------------------------------------------------------
# 镜像盘点（--dump-image 在镜像内运行；--image 在宿主编排）
# ---------------------------------------------------------------------------
def _site_packages():
    import sysconfig
    return pathlib.Path(sysconfig.get_paths()["purelib"])


def _cmd(args):
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def image_inventory():
    """镜像内实际安装物盘点（只读）。"""
    import platform
    rows = _cmd(["dpkg-query", "-W", "-f", "${Package}\t${Version}\n"]) or ""
    packages, texts = {}, {}
    for line in rows.splitlines():
        name, version = line.split("\t", 1)
        copyright_file = pathlib.Path("/usr/share/doc") / name / "copyright"
        digest = None
        if copyright_file.is_file():
            body = copyright_file.read_text(encoding="utf-8", errors="replace").strip()
            digest = hashlib.sha256(body.encode()).hexdigest()
            texts[digest] = body
        packages[name] = {"version": version, "copyright": digest}
    common = {}
    common_dir = pathlib.Path("/usr/share/common-licenses")
    if common_dir.is_dir():
        for entry in sorted(common_dir.iterdir()):
            if entry.is_file():
                common[entry.name] = entry.read_text(encoding="utf-8", errors="replace").strip()
    libs_dir = _site_packages() / PSYCOPG_LIBS
    libs = sorted(p.name for p in libs_dir.iterdir()) if libs_dir.is_dir() else []
    docker = {}
    raw = _cmd(["docker", "--version"]) or ""
    match = re.search(r"version ([0-9][^,\s]*)", raw)
    if match:
        docker["docker"] = match.group(1)
    for name in ("docker-buildx", "docker-compose"):
        raw = _cmd([DOCKER_BINS[name], "version"]) or ""
        match = re.search(r"(v[0-9][0-9.]*)", raw)
        if match:
            docker[name] = match.group(1)
    return {"packages": packages, "copyright_texts": texts, "common_licenses": common,
            "psycopg_libs": libs, "node": _cmd(["node", "--version"]),
            "python": platform.python_version(), "docker": docker}


def lib_stem(filename):
    """`libcom_err-2abe824b.so.2.1` → `libcom_err`（auditwheel 改名：<stem>-<hash>.so…）。"""
    return re.split(r"-[0-9a-f]{8}\.so|\.so", filename, maxsplit=1)[0]


def debian_entries(inventory):
    """按版权原文去重：同一份 copyright 合并列出全部包名与版本。"""
    groups = {}
    for name, meta in sorted(inventory["packages"].items()):
        if meta["copyright"]:
            groups.setdefault(meta["copyright"], []).append(f"{name}={meta['version']}")
    entries = []
    for digest, members in sorted(groups.items(), key=lambda kv: kv[1][0]):
        entries.append((f"debian {', '.join(members)}", "see copyright",
                        [("/usr/share/doc/<package>/copyright", inventory["copyright_texts"][digest])]))
    if inventory["common_licenses"]:
        entries.append(("debian /usr/share/common-licenses (texts referenced by the copyright "
                        "files above)", "various",
                        [(name, text) for name, text in sorted(inventory["common_licenses"].items())]))
    covers = [f"debian {name}" for name, meta in inventory["packages"].items() if meta["copyright"]]
    return entries, covers


def _go_mod_download(modules, gomodcache):
    """`go mod download -json`（GOPROXY/GOSUMDB 取自环境，sumdb 校验模块完整性）。"""
    env = dict(os.environ, GOMODCACHE=str(gomodcache), GOFLAGS="-modcacherw",
               GO111MODULE="on")
    import tempfile
    result = {}
    with tempfile.TemporaryDirectory() as tmp:
        modules = sorted(modules)
        for start in range(0, len(modules), 40):
            chunk = [f"{path}@{version}" for path, version in modules[start:start + 40]]
            proc = subprocess.run(["go", "mod", "download", "-json", *chunk], cwd=tmp, env=env,
                                  capture_output=True, text=True)
            decoder, text, pos = json.JSONDecoder(), proc.stdout, 0
            while pos < len(text):
                while pos < len(text) and text[pos].isspace():
                    pos += 1
                if pos >= len(text):
                    break
                obj, pos = decoder.raw_decode(text, pos)
                result[(obj["Path"], obj["Version"])] = obj
    return result


def docker_go_entries(bins_dir, manifest, cache, gomodcache):
    """docker CLI 插件二进制（go version -m）与 docker CLI（上游 vendor/modules.txt）的 Go 模块许可。"""
    users = {}
    versions = {}
    for name in ("docker-buildx", "docker-compose"):
        binary = pathlib.Path(bins_dir) / name
        info = subprocess.run(["go", "version", "-m", str(binary)], check=True,
                              capture_output=True, text=True).stdout
        for line in info.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "mod":
                versions[name] = parts[2]
            if len(parts) >= 3 and parts[0] == "dep":
                users.setdefault((parts[1], parts[2]), set()).add(name)
            if len(parts) >= 4 and parts[0] == "=>":
                users.setdefault((parts[1], parts[2]), set()).add(name)
    vendor = manifest["docker_cli_vendor"]
    modules_txt = fetch(vendor["url"], vendor["sha256"], cache).decode()
    for line in modules_txt.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "#" and parts[2].startswith("v"):
            if "=>" in parts:
                arrow = parts.index("=>")
                if len(parts) > arrow + 2:
                    users.setdefault((parts[arrow + 1], parts[arrow + 2]), set()).add("docker")
                    continue
            users.setdefault((parts[1], parts[2]), set()).add("docker")
    downloaded = _go_mod_download(list(users), gomodcache)
    entries, gaps = [], []
    for (path, version), who in sorted(users.items()):
        label = f"go {path}@{version} (linked into: {', '.join(sorted(who))})"
        info = downloaded.get((path, version))
        if not info or not info.get("Dir"):
            reason = (info or {}).get("Error", "not downloaded")
            gaps.append(f"{label}: module download failed ({reason})")
            continue
        files = _license_files(info["Dir"])
        if not files:
            gaps.append(f"{label}: no license file in module")
            continue
        entries.append((label, f"see text (module sum {info.get('Sum', '?')})",
                        [(f.name, _read(f)) for f in files]))
    return entries, gaps, versions


def run_image_dump(image):
    script = pathlib.Path(__file__).read_text(encoding="utf-8")
    proc = subprocess.run(["docker", "run", "--rm", "-i", "--entrypoint", "python", image,
                           "-", "--dump-image"], input=script, capture_output=True, text=True,
                          check=True)
    return json.loads(proc.stdout)


def copy_docker_bins(image, dest):
    dest = pathlib.Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    cid = subprocess.run(["docker", "create", image], check=True, capture_output=True,
                         text=True).stdout.strip()
    try:
        for name, path in DOCKER_BINS.items():
            subprocess.run(["docker", "cp", f"{cid}:{path}", str(dest / name)], check=True,
                           capture_output=True)
    finally:
        subprocess.run(["docker", "rm", cid], capture_output=True)
    return dest


# ---------------------------------------------------------------------------
# 输出与镜像内核对
# ---------------------------------------------------------------------------
def render(sections, gaps, covers=()):
    lines = [
        "THIRD-PARTY LICENSES — console-stack public kernel distribution",
        "=" * 64,
        "Generated by scripts/gen_third_party_licenses.py from installed packages",
        "(npm production closure of kernel/frontend/package-lock.json, Python runtime",
        "closure of console/backend/requirements.txt (public), Go module closure of",
        "kernel/agent) plus the public Docker image inventory (Debian packages with their",
        "/usr/share/doc/*/copyright files, Node.js, CPython, psycopg-binary bundled native",
        "libraries, Docker CLI and plugins with their Go modules). Texts that are not",
        "shipped inside a package are taken from the upstream original source named next",
        "to each text and verified against a pinned sha256",
        "(scripts/third_party_upstream.json). Project license: LICENSE. Summary: NOTICE.",
        "",
    ]
    for title, entries in sections:
        lines += ["", "#" * 64, f"# {title} ({len(entries)} packages)", "#" * 64]
        for label, license_field, files in entries:
            lines += ["", "-" * 64, f"{label}  [license: {license_field}]", "-" * 64]
            for name, text in files:
                lines += [f"--- {name} ---", text, ""]
    lines += ["", "#" * 64, f"# COVERAGE ({len(covers)}) — image components covered above", "#" * 64]
    lines += [f"covers: {item}" for item in sorted(set(covers))]
    lines += ["", "#" * 64, f"# GAPS ({len(gaps)}) — not closed; listed honestly", "#" * 64]
    lines += [f"- {gap}" for gap in gaps] or ["- none"]
    return "\n".join(lines).rstrip() + "\n"


def verify_image(path, static_copy=None, inventory=None):
    """在镜像内运行：核对 THIRD_PARTY 文件覆盖实际安装物。返回 (ok, report)。"""
    inventory = inventory or image_inventory()
    failures, warnings = [], []
    target = pathlib.Path(path)
    if not target.is_file():
        return False, {"failures": [f"missing {path}"], "warnings": []}
    text = target.read_text(encoding="utf-8")
    if static_copy is not None:
        other = pathlib.Path(static_copy)
        if not other.is_file() or other.read_text(encoding="utf-8") != text:
            failures.append(f"{static_copy} missing or differs from {path}")
    gap_match = re.search(r"^# GAPS \((\d+)\)", text, flags=re.M)
    if not gap_match or gap_match.group(1) != "0":
        failures.append(f"GAPS not zero ({gap_match.group(1) if gap_match else 'section missing'})")
    covers = set(re.findall(r"^covers: (.+)$", text, flags=re.M))

    def covered_versions(prefix):
        return {c.split(" ", 2)[2] if c.count(" ") >= 2 else "" for c in covers
                if c.startswith(prefix + " ")}

    for name in sorted(inventory["packages"]):
        if f"debian {name}" not in covers:
            failures.append(f"debian package not covered: {name}")
    for filename in inventory["psycopg_libs"]:
        if f"psycopg-lib {lib_stem(filename)}" not in covers:
            failures.append(f"psycopg bundled library not covered: {filename}")

    def check_version(kind, actual, major_of):
        listed = {c.split(" ", 1)[1] for c in covers if c.startswith(kind + " ")}
        if actual is None:
            failures.append(f"{kind}: not found in image")
        elif actual in listed:
            return
        elif any(major_of(v) == major_of(actual) for v in listed):
            warnings.append(f"{kind}: image has {actual}, texts generated for {sorted(listed)} "
                            "(same major; regenerate to refresh)")
        else:
            failures.append(f"{kind}: image has {actual}, covered {sorted(listed)}")

    check_version("node", inventory["node"], lambda v: v.lstrip("v").split(".")[0])
    check_version("python", inventory["python"], lambda v: ".".join(v.split(".")[:2]))
    for name in ("docker", "docker-buildx", "docker-compose"):
        actual = inventory["docker"].get(name)
        listed = {c.split(" ", 2)[2] for c in covers if c.startswith(f"docker-bin {name} ")}
        if actual is None:
            failures.append(f"{name}: version not found in image")
        elif actual not in listed:
            same_major = any(v.lstrip("v").split(".")[0] == actual.lstrip("v").split(".")[0]
                             for v in listed)
            (warnings if same_major else failures).append(
                f"{name}: image has {actual}, covered {sorted(listed)}")
    for name, meta in inventory["packages"].items():
        if not meta["copyright"] and f"debian {name}" not in covers:
            pass  # 已在上面作为 failure 计入
    report = {"file": str(path), "bytes": len(text.encode()), "covers": len(covers),
              "debian_packages": len(inventory["packages"]),
              "psycopg_libs": len(inventory["psycopg_libs"]), "node": inventory["node"],
              "python": inventory["python"], "docker": inventory["docker"],
              "failures": failures, "warnings": warnings}
    return not failures, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dump-python", action="store_true",
                        help="只输出当前解释器已装发行包的许可 JSON")
    parser.add_argument("--dump-image", action="store_true",
                        help="（镜像内）只输出实际安装物盘点 JSON")
    parser.add_argument("--verify-image", metavar="PATH",
                        help="（镜像内）核对 PATH 覆盖实际安装物；不过退出 1")
    parser.add_argument("--static-copy", metavar="PATH",
                        help="与 --verify-image 合用：前端分发目录副本须与 PATH 字节一致")
    parser.add_argument("--pin-upstream", action="store_true",
                        help="下载缺 sha256 的上游原文并写回清单")
    parser.add_argument("--python-json", help="--dump-python 的输出文件")
    parser.add_argument("--image", help="公共镜像引用：盘点 dpkg/原生库/docker 二进制")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE), help="上游原文与 Go 模块缓存目录")
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--check", action="store_true", help="GAPS 非空时退出 1")
    args = parser.parse_args(argv)
    if args.dump_python:
        dump_python()
        return 0
    if args.dump_image:
        json.dump(image_inventory(), sys.stdout, ensure_ascii=False)
        return 0
    if args.verify_image:
        ok, report = verify_image(args.verify_image, args.static_copy)
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0 if ok else 1
    if args.pin_upstream:
        pin_upstream(UPSTREAM, args.cache)
        return 0
    if not args.python_json:
        parser.error("需要 --python-json（先在公共运行依赖解释器里跑 --dump-python）")
    if not args.image:
        parser.error("需要 --image（公共镜像引用，盘点镜像实际安装物）")
    manifest = load_upstream()
    upstream, covers, up_errors = upstream_entries(manifest, args.cache)
    resolved = {label for entries in upstream.values() for label, _, _ in entries}
    npm, npm_gaps = npm_entries()
    py, py_gaps = python_entries(args.python_json)
    go, go_gaps = go_entries()

    def unresolved(gaps):
        return [g for g in gaps if g.split(":", 1)[0] not in resolved]

    inventory = run_image_dump(args.image)
    debian, debian_covers = debian_entries(inventory)
    covers += debian_covers
    covers += [f"psycopg-lib-file {name}" for name in inventory["psycopg_libs"]]
    bins = copy_docker_bins(args.image, pathlib.Path(args.cache) / "docker-bins")
    docker_go, docker_gaps, _ = docker_go_entries(bins, manifest, args.cache,
                                                  pathlib.Path(args.cache) / "gomod")
    gaps = (unresolved(npm_gaps) + unresolved(py_gaps) + unresolved(go_gaps) + up_errors
            + docker_gaps)
    text = render([
        ("npm (frontend bundle production closure)", npm + upstream.get("npm", [])),
        ("Python (runtime image closure)", py + upstream.get("python", [])),
        ("Go (kernel/agent module closure)", go + upstream.get("go", [])),
        ("Image runtime components (upstream original texts)", upstream.get("image", [])),
        ("Go modules linked into Docker CLI and plugins", docker_go),
        ("Debian packages in the public image (copyright files as shipped)", debian),
    ], gaps, covers)
    ok, report = verify_image_text(text, inventory)
    if not ok:
        gaps += [f"coverage: {f}" for f in report["failures"]]
        text = render([
            ("npm (frontend bundle production closure)", npm + upstream.get("npm", [])),
            ("Python (runtime image closure)", py + upstream.get("python", [])),
            ("Go (kernel/agent module closure)", go + upstream.get("go", [])),
            ("Image runtime components (upstream original texts)", upstream.get("image", [])),
            ("Go modules linked into Docker CLI and plugins", docker_go),
            ("Debian packages in the public image (copyright files as shipped)", debian),
        ], gaps, covers)
    pathlib.Path(args.out).write_text(text, encoding="utf-8")
    print(json.dumps({"out": args.out, "bytes": len(text.encode()), "npm": len(npm),
                      "python": len(py), "go": len(go), "upstream": len(resolved),
                      "docker_go": len(docker_go), "debian_groups": len(debian),
                      "gaps": len(gaps), "coverage_warnings": report["warnings"]}))
    return 1 if (args.check and gaps) else 0


def verify_image_text(text, inventory):
    """生成期自核：与镜像内 --verify-image 同一判据（GAPS 计数除外，此时尚未定稿）。"""
    import tempfile
    body = re.sub(r"^# GAPS \(\d+\)", "# GAPS (0)", text, flags=re.M)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(body)
        name = fh.name
    try:
        return verify_image(name, inventory=inventory)
    finally:
        os.unlink(name)


if __name__ == "__main__":
    sys.exit(main())
