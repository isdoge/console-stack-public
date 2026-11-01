"""组合身份的严格输入、来源集合与独立构建回归；仅使用合成临时源码。"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import build_composition_identity as builder
import composition_identity as identity

ROOT = Path(__file__).resolve().parents[1]


def _manifest(pid):
    return {
        "id": pid, "name": pid, "description": "示例", "version": "1.0.0",
        "api_version": 1, "routes": [f"/{pid}"], "api_prefixes": [f"/api/{pid}"],
        "owns": {"tables": [], "settings": [], "files": [], "tasks": []},
    }


def _write(path, document):
    path.write_text(json.dumps(document), encoding="utf-8")


@pytest.fixture
def sources(tmp_path):
    # macOS 的系统临时目录可能先经 /var 链接；夹具使用其真实路径。
    root = tmp_path.resolve()
    kernel = root / "kernel"
    kernel.mkdir()
    (kernel / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    plugins = root / "plugins"
    plugins.mkdir()
    (plugins / "contract.json").write_bytes((ROOT / "plugins/contract.json").read_bytes())
    _write(plugins / "composition.json", {"plugins": ["sample"]})
    (plugins / "sample").mkdir()
    _write(plugins / "sample/manifest.json", _manifest("sample"))
    return kernel, plugins


def _identity():
    return {
        "schema_version": 1,
        "kernel": {"version": "1.0.0", "digest": "a" * 64},
        "plugins": [{"id": "sample", "version": "1.0.0", "digest": "b" * 64}],
        "composition_digest": "c" * 64,
    }


def test_build_is_deterministic_and_tracks_source_bytes(sources):
    kernel, plugins = sources
    first = builder.build_identity(kernel, "1.0.0", plugins, ["sample"])
    assert first == builder.build_identity(kernel, "1.0.0", plugins)
    assert first["plugins"][0]["id"] == "sample"
    (kernel / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    second = builder.build_identity(kernel, "1.0.0", plugins)
    assert first["kernel"]["digest"] != second["kernel"]["digest"]
    assert first["composition_digest"] != second["composition_digest"]
    assert first["plugins"] == second["plugins"]


def test_explicit_zero_plugin_build(tmp_path):
    root = tmp_path.resolve()
    kernel, plugins = root / "kernel", root / "plugins"
    kernel.mkdir()
    plugins.mkdir()
    (kernel / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (plugins / "contract.json").write_bytes((ROOT / "plugins/contract.json").read_bytes())
    _write(plugins / "composition.json", {"plugins": []})
    assert builder.build_identity(kernel, "1.0.0", plugins, [])["plugins"] == []


@pytest.mark.parametrize("contract", [None, [], {"api_version": True}, {"api_version": "1"}])
def test_zero_plugin_build_still_validates_contract(sources, contract):
    kernel, plugins = sources
    (plugins / "sample/manifest.json").rename(plugins / "sample/not-a-manifest.json")
    _write(plugins / "composition.json", {"plugins": []})
    _write(plugins / "contract.json", contract)
    with pytest.raises(ValueError, match="协议"):
        builder.build_identity(kernel, "1.0.0", plugins)


@pytest.mark.parametrize("requested", [
    ["sample", "sample"], [], ["ghost"], ["1sample"], ["../sample"],
    ["Sample"], [None], [False], "sample", 0,
])
def test_requested_plugins_cannot_bypass_declared_sources(sources, requested):
    kernel, plugins = sources
    with pytest.raises(ValueError):
        builder.build_identity(kernel, "1.0.0", plugins, requested)


@pytest.mark.parametrize("body", [
    '{"plugins": ["sample"], "plugins": ["sample"]}',
    '{"plugins": ["sample", "sample"]}',
    '{"plugins": ["sample", "ghost"]}',
    '{"plugins": []}',
    '{"plugins": ["1sample"]}',
    '{"plugins": ["sample"], "extra": NaN}',
    '{"plugins": ["sample"], "extra": 1e999}',
])
def test_generator_rejects_invalid_or_drifting_sources(sources, body):
    kernel, plugins = sources
    (plugins / "composition.json").write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        builder.build_identity(kernel, "1.0.0", plugins)


@pytest.mark.parametrize("extra", [
    '"version": "2.0.0"',
    '"\\u0076ersion": "2.0.0"',
    '"extra": NaN', '"extra": Infinity', '"extra": -Infinity', '"extra": 1e999',
])
def test_generator_rejects_non_strict_manifest_json(sources, extra):
    kernel, plugins = sources
    path = plugins / "sample/manifest.json"
    body = path.read_text(encoding="utf-8")
    path.write_text(body[:-1] + ", " + extra + "}", encoding="utf-8")
    with pytest.raises(ValueError):
        builder.build_identity(kernel, "1.0.0", plugins)


@pytest.mark.parametrize("extra", ['"api_version": 1', '"extra": NaN', '"extra": 1e999'])
def test_generator_rejects_non_strict_contract_json(sources, extra):
    kernel, plugins = sources
    path = plugins / "contract.json"
    body = path.read_text(encoding="utf-8").rstrip()
    path.write_text(body[:-1] + ", " + extra + "}", encoding="utf-8")
    with pytest.raises(ValueError):
        builder.build_identity(kernel, "1.0.0", plugins)


def test_generator_uses_manifest_protocol_and_conflict_validation(sources):
    kernel, plugins = sources
    second = _manifest("other")
    second["routes"] = ["/sample/details"]
    (plugins / "other").mkdir()
    _write(plugins / "other/manifest.json", second)
    _write(plugins / "composition.json", {"plugins": ["sample", "other"]})
    with pytest.raises(ValueError, match="冲突"):
        builder.build_identity(kernel, "1.0.0", plugins)
    second["routes"] = ["/other"]
    second["api_version"] = 999
    _write(plugins / "other/manifest.json", second)
    with pytest.raises(ValueError, match="协议"):
        builder.build_identity(kernel, "1.0.0", plugins)


@pytest.mark.parametrize("target", ["kernel", "plugins"])
@pytest.mark.parametrize("kind", ["direct", "ancestor", "cancelled_ancestor"])
def test_generator_rejects_source_symlinks_before_normalization(sources, target, kind):
    kernel, plugins = sources
    parent = kernel.parent
    link = parent / "linked"
    link.symlink_to(kernel if kind == "direct" else parent, target_is_directory=True)
    root = kernel if target == "kernel" else plugins
    if kind == "direct":
        if target == "plugins":
            link.unlink()
            link.symlink_to(plugins, target_is_directory=True)
        root = link
    elif kind == "ancestor":
        root = link / target
    else:
        root = link / ".." / parent.name / target
    with pytest.raises(ValueError):
        builder.build_identity(
            root if target == "kernel" else kernel, "1.0.0",
            root if target == "plugins" else plugins,
        )


@pytest.mark.parametrize("filename", ["composition.json", "contract.json", "sample/manifest.json"])
def test_generator_rejects_symlinked_input_files(sources, filename):
    kernel, plugins = sources
    path = plugins / filename
    original = plugins.parent / "original.json"
    path.rename(original)
    path.symlink_to(original)
    with pytest.raises(ValueError):
        builder.build_identity(kernel, "1.0.0", plugins)


@pytest.mark.parametrize("schema_version", [1, 1.0])
def test_identity_accepts_json_numeric_version_and_distinct_nested_keys(tmp_path, schema_version):
    path = tmp_path.resolve() / "identity.json"
    document = _identity()
    document["schema_version"] = schema_version
    _write(path, document)
    assert identity.read(path) == document


@pytest.mark.parametrize("mutation", [
    lambda body: body.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'),
    lambda body: body.replace('"kernel": {', '"kernel": {"version": "2.0.0", '),
    lambda body: body.replace('"kernel": {', '"kernel": {"\\u0076ersion": "2.0.0", '),
    lambda body: body.replace('"schema_version": 1', '"schema_version": true'),
    lambda body: body.replace('"schema_version": 1', '"schema_version": NaN'),
    lambda body: body.replace('"schema_version": 1', '"schema_version": Infinity'),
    lambda body: body.replace('"schema_version": 1', '"schema_version": -Infinity'),
    lambda body: body.replace('"schema_version": 1', '"schema_version": 1e999'),
])
def test_identity_rejects_ambiguous_json(tmp_path, mutation):
    path = tmp_path.resolve() / "identity.json"
    path.write_text(mutation(json.dumps(_identity())), encoding="utf-8")
    with pytest.raises(identity.IdentityError):
        identity.read(path)


@pytest.mark.parametrize("kind", ["direct", "ancestor", "cancelled_ancestor"])
def test_identity_rejects_symlinks_before_normalization(tmp_path, kind):
    root = tmp_path.resolve()
    actual = root / "actual"
    actual.mkdir()
    path = actual / "identity.json"
    _write(path, _identity())
    link = root / "linked"
    if kind == "direct":
        link.symlink_to(path)
        configured = link
    else:
        link.symlink_to(actual, target_is_directory=True)
        configured = link / "identity.json" if kind == "ancestor" else link / ".." / "actual/identity.json"
    with pytest.raises(identity.IdentityError):
        identity.read(configured)


def test_current_matches_frontend_absolute_path_and_blank_environment(tmp_path, monkeypatch):
    path = tmp_path.resolve() / "identity.json"
    _write(path, _identity())
    monkeypatch.chdir(path.parent)
    monkeypatch.setenv("CONSOLE_BUILD_IDENTITY_FILE", "identity.json")
    with pytest.raises(identity.IdentityError, match="绝对路径"):
        identity.current()
    monkeypatch.setenv("CONSOLE_BUILD_IDENTITY_FILE", f"  {path}  ")
    assert identity.current() == _identity()
    monkeypatch.setenv("CONSOLE_BUILD_IDENTITY_FILE", "  ")
    assert identity.current() is None


def test_static_identity_must_match_backend(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    path = root / "identity.json"
    static = root / "static"
    static.mkdir()
    _write(path, _identity())
    _write(static / "build-identity.json", _identity())
    monkeypatch.setenv("CONSOLE_BUILD_IDENTITY_FILE", str(path))
    assert identity.verify_static(static) == _identity()
    changed = _identity()
    changed["composition_digest"] = "d" * 64
    _write(static / "build-identity.json", changed)
    with pytest.raises(identity.IdentityError, match="不一致"):
        identity.verify_static(static)


def test_generator_cli_writes_once_and_failure_is_bounded(sources):
    kernel, plugins = sources
    output = kernel.parent / "identity.json"
    command = [
        sys.executable, str(ROOT / "scripts/build_composition_identity.py"),
        "--kernel-root", str(kernel), "--plugins-root", str(plugins),
        "--version", "1.0.0", "--output", str(output),
    ]
    first = subprocess.run(command, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    original = output.read_bytes()
    assert identity.read(output)["composition_digest"] == first.stdout.strip()
    repeated = subprocess.run(command, capture_output=True, text=True, check=False)
    assert repeated.returncode == 1
    assert "Traceback" not in repeated.stderr
    assert output.read_bytes() == original
    (plugins / "contract.json").write_text("null", encoding="utf-8")
    failed = subprocess.run(command, capture_output=True, text=True, check=False)
    assert failed.returncode == 1
    assert "Traceback" not in failed.stderr
    assert output.read_bytes() == original
