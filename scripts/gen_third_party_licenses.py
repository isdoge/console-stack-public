#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 `kernel/THIRD_PARTY_LICENSES.txt`：公共分发物的依赖闭包许可全文（B7 t41u 缺口 4）。

三段闭包，各自从**实际安装物**读许可文件，不手抄：

- npm：`kernel/frontend/package-lock.json` 中非 dev 的全部包（生产闭包），
  许可文件读 `kernel/frontend/node_modules/<pkg>/`（先 `npm ci`）。
- Python：`--python-json` 指向在**只装公共运行依赖**的解释器里执行
  `gen_third_party_licenses.py --dump-python` 得到的 JSON（镜像同版 python:3.10）。
- Go：`go version -m` 列出的实际链接进 agent 二进制的模块（离线读模块缓存）。

找不到许可全文的条目不硬写「已关闭」，统一列入文末 GAPS 段，退出码仍为 0；
`--check` 时 GAPS 非空则退出 1（供发布前人工裁决）。
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "kernel" / "frontend"
AGENT = ROOT / "kernel" / "agent"
OUT = ROOT / "kernel" / "THIRD_PARTY_LICENSES.txt"
LICENSE_PREFIXES = ("license", "licence", "copying", "notice")
# 生成器无法从包内许可文件闭合、需在发布说明中显式承认的分发物成分。
STATIC_GAPS = (
    "image: psycopg-binary wheel bundles native libraries (libpq, OpenSSL, etc.) under "
    "psycopg_binary.libs; their notices are not extracted here",
    "image: Debian bookworm base packages (git, curl, openssh-client, docker-ce-cli, ...) "
    "carry their copyright files under /usr/share/doc/*/copyright inside the image; not "
    "inlined here",
    "image: /usr/local/bin/node is copied from node:24-bookworm-slim (Node.js license and "
    "bundled third-party notices are not inlined here)",
)


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


def render(sections, gaps):
    lines = [
        "THIRD-PARTY LICENSES — console-stack public kernel distribution",
        "=" * 64,
        "Generated by scripts/gen_third_party_licenses.py from installed packages",
        "(npm production closure of kernel/frontend/package-lock.json, Python runtime",
        "closure of console/backend/requirements.txt (public), Go module closure of",
        "kernel/agent). Project license: LICENSE. Curated summary: NOTICE.",
        "",
    ]
    for title, entries in sections:
        lines += ["", "#" * 64, f"# {title} ({len(entries)} packages)", "#" * 64]
        for label, license_field, files in entries:
            lines += ["", "-" * 64, f"{label}  [license: {license_field}]", "-" * 64]
            for name, text in files:
                lines += [f"--- {name} ---", text, ""]
    lines += ["", "#" * 64, f"# GAPS ({len(gaps)}) — not closed; listed honestly", "#" * 64]
    lines += [f"- {gap}" for gap in gaps] or ["- none"]
    return "\n".join(lines).rstrip() + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dump-python", action="store_true",
                        help="只输出当前解释器已装发行包的许可 JSON")
    parser.add_argument("--python-json", help="--dump-python 的输出文件")
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--check", action="store_true", help="GAPS 非空时退出 1")
    args = parser.parse_args(argv)
    if args.dump_python:
        dump_python()
        return 0
    if not args.python_json:
        parser.error("需要 --python-json（先在公共运行依赖解释器里跑 --dump-python）")
    npm, npm_gaps = npm_entries()
    py, py_gaps = python_entries(args.python_json)
    go, go_gaps = go_entries()
    gaps = npm_gaps + py_gaps + go_gaps + list(STATIC_GAPS)
    text = render([("npm (frontend bundle production closure)", npm),
                   ("Python (runtime image closure)", py),
                   ("Go (kernel/agent module closure)", go)], gaps)
    pathlib.Path(args.out).write_text(text, encoding="utf-8")
    print(json.dumps({"out": args.out, "npm": len(npm), "python": len(py), "go": len(go),
                      "gaps": len(gaps)}))
    return 1 if (args.check and gaps) else 0


if __name__ == "__main__":
    sys.exit(main())
