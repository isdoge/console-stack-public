# -*- coding: utf-8 -*-
"""`_is_console_api()` 的归属必须由插件清单决定，不由 kernel 硬编码（B7-3）。

背景：`app.py` 曾在 `CONSOLE_API_PATHS` 与 `_is_console_api()` 里写死
`/api/ledger`、`/api/renewals`、`/api/bookmarks`、`/api/feed`、`/api/panels`
等**插件路由**。启用这些插件时那几行是冗余的（`plugin_runtime.owns_api()`
已在函数第一行判定）；零插件组合下它们变成有害的——kernel 会对根本不存在的
能力声称「这是控制台自有接口」。

本文件锁死三件事：

1. 插件声明的路由由 manifest 决定归属，kernel 不重复声明；
2. 零插件组合下这些路径**不是**控制台自有接口；
3. kernel 自有路由在所有组合下都不受影响。
"""
import json
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
REPO_DIR = BACKEND_DIR.parent.parent
for _path in (BACKEND_DIR, REPO_DIR / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import composition  # noqa: E402

PLUGIN_ROUTES = (
    "/api/ledger",
    "/api/ledger/tx",
    "/api/renewals",
    "/api/bookmarks",
    "/api/feed",
    "/api/panels",
    "/api/oplog",
    "/api/events",
    "/api/backup-evidence",
    "/api/personal-workbench",
)

# kernel 自有路由：不归任何插件，任何组合下都必须被认领。
KERNEL_ROUTES = (
    "/api/services",
    "/api/config",
    "/api/machines",
    "/api/health",
    "/api/plugins",
    "/api/ops-status",
    "/api/inspection/report",
    "/api/config/effective",
    "/api/config/settings",
    "/api/dashboard-layout",
    "/api/caddy/entry",
    "/api/capacity",
    "/api/monitor/history",
    "/api/monitoring/v1/ingest",
)


def _manifests():
    root = REPO_DIR / "plugins"
    for pid in composition.plugin_sources(root):
        yield pid, json.loads((root / pid / "manifest.json").read_text(encoding="utf-8"))


def test_every_plugin_route_is_declared_by_its_manifest():
    """上表里的每条路由都必须真有一个插件声明，否则本文件在测空气。"""
    declared = set()
    for _pid, manifest in _manifests():
        for prefix in manifest.get("api_prefixes") or ():
            declared.add(prefix)
    for path in PLUGIN_ROUTES:
        owners = [prefix for prefix in declared
                  if path == prefix or path.startswith(prefix + "/")]
        assert owners, f"{path} 没有任何插件声明，本用例的前提不成立"


def _is_relocating_shim(path: pathlib.Path) -> bool:
    """与 `scripts/gen_api_docs._is_relocating_shim` 同判据：薄委托不算实现。"""
    try:
        return "Thin relocating shim" in path.read_text(encoding="utf-8")[:240]
    except OSError:
        return False


def _app_py_path() -> pathlib.Path:
    """AST 读实现：优先 `kernel/backend/app.py`，跳过 console 薄 shim。

    与 `scripts/gen_api_docs._app_py_path()` 同策略。`console/backend/app.py` 迁入
    kernel 后只剩 relocating shim，对本文件的常量/`_is_console_api` AST 扫描无意义。
    """
    kernel = REPO_DIR / "kernel" / "backend" / "app.py"
    console = BACKEND_DIR / "app.py"
    if kernel.is_file() and not _is_relocating_shim(kernel):
        return kernel
    if console.is_file() and not _is_relocating_shim(console):
        return console
    if kernel.is_file():
        return kernel
    return console


def _console_api_paths_literal():
    """用 AST 取 `CONSOLE_API_PATHS` 的真实字符串元素（不是文本搜索）。"""
    import ast

    tree = ast.parse(_app_py_path().read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
            if any(t.id == "CONSOLE_API_PATHS" for t in targets):
                assert isinstance(node.value, (ast.Set, ast.List, ast.Tuple))
                return {elt.value for elt in node.value.elts
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)}
    raise AssertionError("app.py 里找不到 CONSOLE_API_PATHS")


def _is_console_api_literal_strings():
    """`_is_console_api()` 函数体里出现的全部字符串字面量。

    只看代码里的常量，不看注释：注释里提到路径是正常的说明，不是判定。
    """
    import ast

    tree = ast.parse(_app_py_path().read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_is_console_api":
            return {child.value for child in ast.walk(node)
                    if isinstance(child, ast.Constant) and isinstance(child.value, str)}
    raise AssertionError("app.py 里找不到 _is_console_api()")


def test_kernel_never_hardcodes_plugin_routes():
    """`CONSOLE_API_PATHS` 里不能出现插件声明的路由。

    kernel 常量是**声明**，`plugin_runtime` 是**判定**；两者混在一起就无法从
    公共候选里剥掉私人路由。

    **唯一豁免**：kernel 里存在「退役占位」路由——`/api/oplog` 在
    `retired_oplog_callback` 里返回 410，目的正是防止旧请求被同源反代吞成
    HTML+200。这类占位必须留在常量里，否则退役回调自身会被代理接管。豁免
    按「kernel 里确有该路由的 410 占位」判定，不是按路径名放行。
    """
    literal = _console_api_paths_literal()
    retired = _retired_placeholder_paths()
    for plugin_route in PLUGIN_ROUTES:
        if plugin_route in retired:
            continue
        assert plugin_route not in literal, (
            f"{plugin_route} 由插件 manifest 声明，kernel 常量不应重复登记")


def _retired_placeholder_paths():
    """kernel 里明确返回 410 的退役占位路由（函数名含 retired）。"""
    import ast

    tree = ast.parse(_app_py_path().read_text(encoding="utf-8"))
    paths = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or "retired" not in node.name:
            continue
        for decorator in node.decorator_list:
            if (isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)):
                paths.add(decorator.args[0].value)
    return paths


def test_retired_placeholders_still_return_gone():
    """豁免的前提必须成立：退役占位确实返回 410，而不是普通实现。

    `/api/oplog` 的 **GET** 归 `plugins/audit`（插件启用时正常返回时间线），
    kernel 占位只覆盖写方法——那是「OliveTin 审计回调已退役」的原意。所以这里
    用 POST 验证 410，而不是 GET；用 GET 会验到插件路由，测的不是同一件事。
    """
    import app as console_app

    client = console_app.app.test_client()
    response = client.post("/api/oplog", json={})
    assert response.status_code == 410


def test_is_console_api_has_no_private_prefix_loop():
    """`_is_console_api()` 里不能再有私人插件的写死前缀。"""
    strings = _is_console_api_literal_strings()
    for prefix in ("/api/bookmarks", "/api/feed", "/api/renewals", "/api/ledger"):
        assert prefix not in strings, f"_is_console_api() 仍写死私人前缀 {prefix}"


@pytest.mark.parametrize("path", PLUGIN_ROUTES)
def test_plugin_routes_are_not_console_api_without_plugins(path):
    """零插件组合下，插件路由不能被 kernel 认领。"""
    import app as console_app
    import plugin_runtime

    if plugin_runtime.owner(path) is not None:
        pytest.skip("当前组合确实声明了该路由，由下面的组合内用例覆盖")
    assert console_app._is_console_api(path) is False


@pytest.mark.parametrize("path", KERNEL_ROUTES)
def test_kernel_routes_are_always_console_api(path):
    import app as console_app

    assert console_app._is_console_api(path) is True


@pytest.mark.parametrize("path", KERNEL_ROUTES)
def test_kernel_routes_are_not_owned_by_any_plugin(path):
    """kernel 路由不能被某个插件的 api_prefixes 顺手圈走。"""
    import plugin_runtime

    assert plugin_runtime.owner(path) is None


def test_zero_plugin_composition_owns_no_plugin_route(tmp_path):
    """真正的公共组合：零插件时，所有插件路由都不被认领。

    上面的参数化用例在当前（私人）组合下只能 skip，等于没验证 B7-3 要保证的
    性质。这里用合成的空插件根真实走一遍 `plugin_runtime` 的归属判定。
    """
    root = tmp_path / "plugins"
    root.mkdir()
    (root / "contract.json").write_text(json.dumps({"api_version": 1}), encoding="utf-8")
    (root / "composition.json").write_text(
        json.dumps({"plugins": []}), encoding="utf-8")

    import plugin_runtime

    retired = _retired_placeholder_paths()
    previous = getattr(plugin_runtime, "MANIFESTS", None)
    plugin_runtime.MANIFESTS = []
    try:
        for path in PLUGIN_ROUTES:
            assert plugin_runtime.owner(path) is None, path
            import app as console_app
            if path in retired:
                # kernel 自己就有该路径的 410 退役占位：必须继续认领，否则占位
                # 会被同源反代吞成 HTML+200。这是唯一允许 kernel 认领插件路径
                # 的情形，且前提是占位真的返回 410（另一条用例验证）。
                assert console_app._is_console_api(path) is True, path
                continue
            # 其余插件路由两份都不认领，上层才会显式 404
            assert console_app._is_console_api(path) is False, path
    finally:
        if previous is not None:
            plugin_runtime.MANIFESTS = previous


def test_unknown_api_is_not_console_api():
    """未登记的 `/api/*` 必须落空，由上层显式 404，不靠前缀猜。"""
    import app as console_app

    assert console_app._is_console_api("/api/definitely-not-a-route") is False
    assert console_app._is_console_api("/api/ledger-of-someone-else") is False
