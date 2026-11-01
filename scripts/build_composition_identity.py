#!/usr/bin/env python3
"""为确定的源码输入生成组合身份；不读取生产配置内容。"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "console" / "backend"))
import composition  # noqa: E402
from composition_identity import read_json, validate  # noqa: E402

EXCLUDED = frozenset({
    ".git", ".claude", ".agents", "node_modules", "__pycache__", ".pytest_cache",
    ".ruff_cache", "dist", "static", "data", "runtime", ".build", ".env",
})


def _source_root(root, label):
    # 先逐段检查原始输入，再解析 `..`，避免它掩盖符号链接祖先。
    root = composition.resolve_root_path(Path.cwd(), label, root)
    if not root.is_dir():
        raise ValueError(f"{label}必须是普通目录")
    return root


def source_digest(root):
    root = _source_root(root, "源码根")
    digest = hashlib.sha256()
    files = []
    for file in sorted(root.rglob("*")):
        relative = file.relative_to(root)
        if any(part in EXCLUDED for part in relative.parts):
            continue
        if file.is_symlink():
            raise ValueError("源码输入不允许符号链接")
        if not file.is_file() or file.suffix in {".pyc", ".pyo"}:
            continue
        files.append(file)
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(file.read_bytes()).digest())
    if not files:
        raise ValueError("源码输入为空")
    return digest.hexdigest()


def build_identity(kernel_root, version, plugins_root, plugin_ids=None):
    root = _source_root(plugins_root, "插件根")
    read_json(root / composition.SOURCES_FILE, label="插件组合清单")
    declared = composition.plugin_sources(root)
    if plugin_ids is not None:
        if isinstance(plugin_ids, (str, bytes)):
            raise ValueError("插件身份必须是数组")
        try:
            requested = list(plugin_ids)
        except TypeError as exc:
            raise ValueError("插件身份必须是数组") from exc
        if (any(not isinstance(pid, str) for pid in requested)
                or len(requested) != len(set(requested))
                or set(requested) != set(declared)):
            raise ValueError("插件身份必须与组合来源清单一致且不重复")
    contract = read_json(root / "contract.json", label="插件协议")
    if not isinstance(contract, dict) or type(contract.get("api_version")) is not int:
        raise ValueError("插件协议格式无效")
    # discover 负责协议、路由、设置和 owns 冲突；先严格读取，防止其普通 JSON
    # 解析静默覆盖重复字段或接受非有限数值。
    for pid in declared:
        read_json(root / pid / "manifest.json", label="插件清单")
    from plugin_manifest import discover

    try:
        manifests = discover(root, contract)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("插件协议或清单结构无效") from exc
    composition.require_plugin_sources(root, manifests)
    plugins = [
        {"id": manifest["id"], "version": manifest["version"],
         "digest": source_digest(root / manifest["id"])}
        for manifest in sorted(manifests, key=lambda item: item["id"])
    ]
    body = {
        "schema_version": 1,
        "kernel": {"version": version, "digest": source_digest(kernel_root)},
        "plugins": plugins,
    }
    body["composition_digest"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return validate(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel-root", required=True, type=Path)
    parser.add_argument("--plugins-root", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        identity = build_identity(args.kernel_root, args.version, args.plugins_root)
        # 不覆盖已有产物；不同组合生成新的构建目录。
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(identity, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.write("\n")
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(1, "组合身份生成失败：检查源码根、插件清单与输出位置\n")
    print(identity["composition_digest"])


if __name__ == "__main__":
    main()
