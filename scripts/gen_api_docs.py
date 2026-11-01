#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 `docs/api.md`：后端 Flask 路由全量参考（纯静态解析，零副作用）。

为什么用 `ast` 静态解析而不是 import 后读 `app.url_map`：`kernel/backend/app.py` 在
import 期就 `init_db()` 建表跑迁移、`seed_workstation_data()` 写库、拉起四个守护线程
（版本同步 / feed+流量记账 / 机器探针 / hindsight 反代），还要 flask、flask_cors、
yaml、waitress 等运行依赖。文档生成器不该碰数据库、不该起线程、也不该依赖后端 venv，
所以只读源码 AST，纯 stdlib（`ast` + `inspect.cleandoc`，后者由 `ast.get_docstring`
内部复用）。

事实源全部来自代码：路径 / 方法 / 用途（函数 docstring 首行）/ 定义处 file:line /
`CONSOLE_API_PATHS` 常量 / `_is_console_api()` 的前缀规则。本文件内的中文标题、分区
说明与敏感端点纪律是**唯一的人工文案**，集中在下面几个常量里。

用法::

    python3 scripts/gen_api_docs.py             # 生成 / 更新 docs/api.md
    python3 scripts/gen_api_docs.py --check     # 校验 docs/api.md 与代码是否一致（部署前门禁）
    python3 scripts/gen_api_docs.py --stdout    # 只打印，不落盘
    python3 scripts/gen_api_docs.py --self-test # 脚本自测

退出码：0 正常；1 `--check` 检出漂移（生成物与现有 `docs/api.md` diff 非空）；
2 一致性校验不通过（`CONSOLE_API_PATHS` 死登记 / 未覆盖的 `/api` 路由 /
声明的敏感端点已不存在）。
"""
from __future__ import annotations

import argparse
import ast
import dataclasses
import datetime
import difflib
import json
import os
import re
import sys
from pathlib import Path

# ---- 人工文案（唯一的非代码事实源）--------------------------------------

# app.py 里 /api 路由的功能域分区：(标题, 分区说明, 路径前缀元组)。
# 匹配按本表顺序取首个命中的前缀；都不命中落到 _FALLBACK_DOMAIN。
API_DOMAINS = (
    (
        "总览与配置",
        "总控台首屏与设置页读取的聚合数据，全部只读。",
        ("/api/config", "/api/health", "/api/ops-status", "/api/attention"),
    ),
    (
        "机器台账",
        "`machines.yaml` 台账的读写、探活时序与 node 白名单动作（E3：只有 key 与文案出后端，命令不外泄）。",
        ("/api/machines", "/api/machine/"),
    ),
    (
        "服务台账",
        "`assets.yaml` 台账的读写，混入健康检查 / 版本状态 / 容器运行态，以及 start/stop/restart/logs 动作。",
        ("/api/services", "/api/service"),
    ),
    (
        "监控与网络",
        "Beszel 快照、tailnet 组网视图与公网入口流量，均为只读且带显式降级（数据源不可用返回 `available=false`，不抛 500）。",
        ("/api/monitor", "/api/observability", "/api/tailnet", "/api/caddy"),
    ),
    (
        "工作台数据",
        "书签 / 资讯流 / 个人记账 / 续费台账 / 外部面板页签。",
        ("/api/bookmarks", "/api/feed", "/api/ledger", "/api/renewals", "/api/panels"),
    ),
    (
        "备份证据",
        "备份目录只读扫描、恢复回执导入与保留策略预览（backup_evidence 插件，PG 迁移 15）。",
        ("/api/backup-evidence",),
    ),
    (
        "审计与已退役",
        "操作日志查询；OliveTin 时期的回调与触发接口保留 410 占位，避免旧请求误落到其它 API。",
        ("/api/oplog", "/api/trigger"),
    ),
)
_FALLBACK_DOMAIN = ("其它 `/api` 路由", "未归入上列功能域的路由；新增功能域请改 `scripts/gen_api_docs.py` 的 `API_DOMAINS`。")

# app.py 之外、由 register_routes 挂到主 app 上的模块（渲染顺序即本表顺序）。
MODULE_TITLES = (
    ("inspection_report.py", "巡检报告（C1 / W1）"),
    ("effective_config.py", "有效配置（C10-a）"),
    ("dashboard_layout.py", "总控台布局 override（C8 / D1'）"),
    ("capacity.py", "磁盘增长趋势（Track M2）"),
    ("monitor_events.py", "事件时间线（Track K）"),
)

NON_API_SECTION = (
    "非 `/api` 路由：前端、反向代理与已退役页面",
    "静态前端托管、Beszel 与外部面板的同源反代。不经过 `CONSOLE_API_PATHS` 机制。",
)

STANDALONE_APPS = {
    "hindsight_app": (
        "独立监听：hindsight 同源反代（`hindsight_proxy.py`，44683）",
        "**不挂在主 app 上**：`app.py` 启动期在守护线程里另起一个 waitress 监听 44683，"
        "compose 发布为宿主 `127.0.0.1:44683`，`tailscale serve 9443 → 44683`。仅 tailnet 可达。",
    ),
}

# 敏感端点：有明确「值遮蔽 / IP 不外泄」纪律的路由。key 必须是真实存在的路由路径，
# 对不上时一致性校验报错（退出码 2），避免纪律描述随代码漂移。
SENSITIVE_ENDPOINTS = (
    (
        "/api/config/effective",
        "敏感值遮蔽",
        "key 名含 `token` / `password` / `secret` / `key`（不区分大小写）的字段值只回显 `***`，长度不外泄；"
        "SSH target（`user@host` 形态）、machines 段台账 `tailscale` 行与 `MACHINE_TAILSCALE_*` / `MACHINE_SSH_*` "
        "覆盖行恒 `***`；`HINDSIGHT_ORIGIN` 遮蔽主机段保尾部端口，未配置显「未配置」，绝不回显默认字面量里的 tailnet IP。"
        "事实源 `effective_config.py` 模块 docstring。",
    ),
    (
        "/api/caddy/entry",
        "IP 不外泄",
        "Caddy upstream 的 `address` 全是 tailnet IP，只在服务端反查服务名（匹配 `assets.yaml` 的 `access.tailscale`），"
        "upstream 行仅 `{id, name, num_requests, fails}`，匹配不到降级 `upstream-N`。"
        "顶层 `upstreams_sampled_at` / `metrics_sampled_at` 分别表示两份文件的采集时间，"
        "各自超过 900 秒即降级并移除对应旧值；`sampled_at` 仅为接口读取时间。"
        "事实源 `caddy_view.py` 模块 docstring。",
    ),
    (
        "/api/tailnet",
        "IP 不外泄",
        "peer 的 `TailscaleIPs` 只在服务端用于「tailscale IP → `machines.yaml` 机器 id」映射，绝不下发；"
        "响应为白名单字段 `{machine_id, hostname, online, key_expiry_days, last_seen, client_version, os, "
        "update_available, authorized}`。顶层 `source_sampled_at` 为文件采集时间，"
        "超过 900 秒后不可用且清空旧机器行；`sampled_at` 仅为接口读取时间。"
        "事实源 `tailscale_view.py` 模块 docstring。",
    ),
)

SENSITIVE_MARK = "🔒"

# ---- 常量与工具 ---------------------------------------------------------

DEFAULT_OUTPUT = os.path.join("docs", "api.md")
KERNEL_BACKEND_SUBDIR = os.path.join("kernel", "backend")
CONSOLE_BACKEND_SUBDIR = os.path.join("console", "backend")
# 兼容旧名：优先 kernel 真身。
BACKEND_SUBDIR = KERNEL_BACKEND_SUBDIR

_METHOD_ORDER = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")

# 归一化对比用的占位符：这两行每次生成都会变，不算内容漂移
_VOLATILE_PATTERNS = (
    (re.compile(r"^updateTime: .*$", re.MULTILINE), "updateTime: @@VOLATILE@@"),
    (re.compile(r"^> \*\*生成时间\*\*：.*$", re.MULTILINE), "> **生成时间**：@@VOLATILE@@"),
)

_RULE_PLACEHOLDER_RE = re.compile(r"<[^>]*>")

# 读表说明里的 `app.py:NN` 指针；生成前由 _locate_console_api_paths_lineno() 定位，
# 解析不到时退回这里的当前行号，不让文档因为一个指针生成失败。
_CONSOLE_API_PATHS_LINENO = [85]


def repo_root() -> str:
    """仓库根（本脚本位于 `<root>/scripts/`）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclasses.dataclass(frozen=True)
class Route:
    """一条路由装饰器 = 一行表格。"""

    module: str  # 相对 backend 根的文件名，如 app.py
    lineno: int  # 装饰器所在行
    func: str
    app_name: str  # 被装饰的 Flask app 变量名（app / hindsight_app / …）
    rule: str
    methods: tuple  # 显式声明的方法；未声明按 Flask 默认 ("GET",)
    summary: str  # 函数 docstring 首行；无 docstring 为空串

    @property
    def is_api(self) -> bool:
        return self.rule.startswith("/api/") or self.rule == "/api"

    @property
    def concrete_path(self) -> str:
        """把 `<converter:var>` 占位替换成一个具体段，用于前缀覆盖判定。"""
        return _RULE_PLACEHOLDER_RE.sub("x", self.rule)

    @property
    def source_ref(self) -> str:
        return "{}:{}".format(self.module, self.lineno)


def _sorted_methods(methods):
    known = [m for m in _METHOD_ORDER if m in methods]
    rest = sorted(m for m in methods if m not in _METHOD_ORDER)
    return tuple(known + rest)


_SENTENCE_END = "。！？!?."
_CJK_RE = re.compile(r"[一-鿿]")


def _needs_space(left: str, right: str) -> bool:
    """中英交界补一个空格，中中 / 英英交界不补（折行处会被拼成一行）。"""

    def is_word(char):
        return char.isascii() and (char.isalnum() or char in "_-/")

    left_cjk = bool(_CJK_RE.match(left))
    right_cjk = bool(_CJK_RE.match(right))
    return (is_word(left) and right_cjk) or (left_cjk and is_word(right))


def _route_summary(node) -> str:
    """docstring 首行作为「用途」；首行断在句中时续到句末。

    后端 docstring 是中文硬折行的，纯取首行会把 `get_tailnet` 这类切成
    「…返回 available=false」的半句。所以首行末尾不是句末标点时，继续拼同一段的
    下一行，直到句末或段落结束——只会补全，不会截断。
    """
    doc = ast.get_docstring(node, clean=True) or ""
    summary = ""
    for raw in doc.splitlines():
        line = raw.strip()
        if not line:
            if summary:
                break  # 段落结束
            continue  # 跳过前导空行
        if summary and _needs_space(summary[-1], line[0]):
            summary += " "
        summary += line
        if line[-1] in _SENTENCE_END:
            break
    return summary


def _route_decorators(fn):
    """产出该函数上的每个 `X.route("...")` 装饰器：(app 名, rule, methods, lineno)。"""
    for dec in fn.decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        target = dec.func
        if not (isinstance(target, ast.Attribute) and target.attr == "route"):
            continue
        if not isinstance(target.value, ast.Name):
            continue
        if not dec.args:
            continue
        first = dec.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        methods = ("GET",)
        for kw in dec.keywords:
            if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                values = [
                    elt.value
                    for elt in kw.value.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                ]
                if values:
                    methods = tuple(values)
        yield target.value.id, first.value, _sorted_methods(methods), dec.lineno


def routes_from_tree(tree, module_label: str):
    """遍历整棵树（含 register_routes 里的嵌套函数）收集路由。"""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        summary = _route_summary(node)
        for app_name, rule, methods, lineno in _route_decorators(node):
            found.append(
                Route(
                    module=module_label,
                    lineno=lineno,
                    func=node.name,
                    app_name=app_name,
                    rule=rule,
                    methods=methods,
                    summary=summary,
                )
            )
    found.sort(key=lambda r: (r.lineno, r.rule))
    return found


def routes_from_source(source: str, module_label: str):
    return routes_from_tree(ast.parse(source), module_label)


def _is_relocating_shim(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return "Thin relocating shim" in handle.read(240)
    except OSError:
        return False


def backend_files(root: str):
    """`kernel/backend/` 真身 + `console/backend/` 非 shim 残留，再加插件后端。"""
    files = []
    seen = set()
    primary = os.path.join(root, KERNEL_BACKEND_SUBDIR)
    for subdir in (KERNEL_BACKEND_SUBDIR, CONSOLE_BACKEND_SUBDIR):
        backend = os.path.join(root, subdir)
        if not os.path.isdir(backend):
            continue
        for name in sorted(os.listdir(backend)):
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            path = os.path.join(backend, name)
            if name in seen or _is_relocating_shim(path):
                continue
            seen.add(name)
            files.append((name, path))
    for path in sorted(Path(root, "plugins").glob("*/backend/**/*.py")):
        if not path.name.startswith("test_"):
            files.append((os.path.relpath(path, primary if os.path.isdir(primary) else root), str(path)))
    return files


def collect_routes(root: str):
    routes = []
    unresolved = []
    for name, path in backend_files(root):
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source, filename=path)
        routes.extend(routes_from_tree(tree, name))
        # add_url_rule 是本生成器抓不到的第二种注册方式：出现即报备，避免静默漏路由
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_url_rule"
            ):
                unresolved.append("{}:{} add_url_rule()".format(name, node.lineno))
    return routes, unresolved


def console_api_paths(root: str):
    """app.py 的 `CONSOLE_API_PATHS` 集合字面量。"""
    tree = _app_tree(root)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "CONSOLE_API_PATHS" not in names:
            continue
        if not isinstance(node.value, ast.Set):
            raise ValueError("CONSOLE_API_PATHS 不再是集合字面量，生成器需同步改")
        return {
            elt.value
            for elt in node.value.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        }
    raise ValueError("app.py 里找不到 CONSOLE_API_PATHS")


def console_api_prefixes(root: str):
    prefixes = list(prefixes_from_tree(_app_tree(root)))
    for manifest in sorted(Path(root, 'plugins').glob('*/manifest.json')):
        prefixes.extend(json.loads(manifest.read_text())['api_prefixes'])
    return tuple(dict.fromkeys(prefixes))


def prefixes_from_tree(tree):
    """`_is_console_api()` 里的前缀规则。

    函数体里的路径字面量有两种用法：`path.startswith("/api/xxx/")` 直接前缀，
    以及 `for _prefix in (...)` 里 `path == _prefix or path.startswith(_prefix + "/")`
    的前缀族。两者的覆盖语义可统一表述为「`path == p` 或 `path` 以 `p` 的目录形态开头」，
    所以这里只收集函数体内所有路径字面量（按源码位置排序），不去解析控制流。
    拼接用的 `"/"` 按长度过滤掉——留着会让任意路径都被判为已覆盖。
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_is_console_api":
            found = []
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    if sub.value.startswith("/") and len(sub.value) > 1:
                        found.append((sub.lineno, sub.col_offset, sub.value))
            found.sort()
            seen = []
            for _, _, value in found:
                if value not in seen:
                    seen.append(value)
            return tuple(seen)
    raise ValueError("app.py 里找不到 _is_console_api()")


def _app_py_path(root: str) -> str:
    kernel = os.path.join(root, KERNEL_BACKEND_SUBDIR, "app.py")
    if os.path.isfile(kernel) and not _is_relocating_shim(kernel):
        return kernel
    console = os.path.join(root, CONSOLE_BACKEND_SUBDIR, "app.py")
    if os.path.isfile(console) and not _is_relocating_shim(console):
        return console
    if os.path.isfile(kernel):
        return kernel
    return console


def _app_tree(root: str):
    path = _app_py_path(root)
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def covering_prefix(path: str, prefixes):
    """返回兜住该路径的前缀规则；没有则 None。"""
    for prefix in prefixes:
        if path == prefix:
            return prefix
        directory = prefix if prefix.endswith("/") else prefix + "/"
        if path.startswith(directory):
            return prefix
    return None


# ---- 分区 ---------------------------------------------------------------


def _domain_of(rule: str):
    for index, (title, intro, prefixes) in enumerate(API_DOMAINS):
        for prefix in prefixes:
            if rule == prefix or rule.startswith(prefix):
                return index, title, intro
    return len(API_DOMAINS), _FALLBACK_DOMAIN[0], _FALLBACK_DOMAIN[1]


_MODULE_ORDER = {name: index for index, (name, _) in enumerate(MODULE_TITLES)}
_MODULE_TITLE = dict(MODULE_TITLES)


def _section_of(route: Route):
    """返回 (排序键, 标题, 说明)。"""
    if route.app_name in STANDALONE_APPS:
        title, intro = STANDALONE_APPS[route.app_name]
        return (300, 0), title, intro
    if route.module != "app.py":
        order = _MODULE_ORDER.get(route.module, len(_MODULE_ORDER))
        title = _MODULE_TITLE.get(route.module, route.module)
        return (100, order, route.module), "{}（`{}`）".format(title, route.module), ""
    if not route.is_api:
        return (200, 0), NON_API_SECTION[0], NON_API_SECTION[1]
    index, title, intro = _domain_of(route.rule)
    return (0, index), title, intro


def build_sections(routes):
    """按分区聚合，返回 [(标题, 说明, [Route])]，顺序固定。"""
    buckets = {}
    for route in routes:
        key, title, intro = _section_of(route)
        buckets.setdefault(key, (title, intro, []))[2].append(route)
    sections = []
    for key in sorted(buckets):
        title, intro, items = buckets[key]
        items.sort(key=_row_sort_key)
        sections.append((title, intro, items))
    return sections


def _row_sort_key(route: Route):
    first = route.methods[0] if route.methods else ""
    order = _METHOD_ORDER.index(first) if first in _METHOD_ORDER else len(_METHOD_ORDER)
    return (route.rule, order, route.lineno)


# ---- 一致性校验 ---------------------------------------------------------


@dataclasses.dataclass
class Consistency:
    total_routes: int
    api_routes: int
    registered_paths: set
    declared_paths: set
    dead_entries: list  # CONSOLE_API_PATHS 里有、代码里无对应路由
    uncovered: list  # /api 路由未被登记也未被前缀兜住 → 应显式 404
    missing_docstring: list
    missing_sensitive: list
    unresolved: list

    @property
    def ok(self) -> bool:
        return not (self.dead_entries or self.uncovered or self.missing_sensitive)


def check_consistency(routes, declared, prefixes, unresolved):
    api_routes = [r for r in routes if r.is_api]
    rules = {r.rule for r in routes}
    registered = {r.rule for r in api_routes if r.rule in declared}
    dead = sorted(declared - rules)
    uncovered = sorted(
        {
            r.rule
            for r in api_routes
            if r.rule not in declared and covering_prefix(r.concrete_path, prefixes) is None
        }
    )
    # 按函数去重：一个函数挂多个路由装饰器时，docstring 只缺一次
    missing_doc = []
    seen_funcs = set()
    for route in sorted(routes, key=lambda r: (r.module, r.lineno)):
        ident = (route.module, route.func)
        if route.summary or ident in seen_funcs:
            continue
        seen_funcs.add(ident)
        missing_doc.append("{} {}()".format(route.source_ref, route.func))
    missing_sensitive = [path for path, _, _ in SENSITIVE_ENDPOINTS if path not in rules]
    return Consistency(
        total_routes=len(routes),
        api_routes=len(api_routes),
        registered_paths=registered,
        declared_paths=set(declared),
        dead_entries=dead,
        uncovered=uncovered,
        missing_docstring=missing_doc,
        missing_sensitive=missing_sensitive,
        unresolved=list(unresolved),
    )


# ---- 渲染 ---------------------------------------------------------------


def _cell(text: str) -> str:
    """表格单元格转义：竖线会截断列，换行会打断行。"""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _ownership(route: Route, declared, prefixes) -> str:
    if not route.is_api:
        return "—"
    if route.rule in declared:
        return "✅ 直接登记"
    prefix = covering_prefix(route.concrete_path, prefixes)
    if prefix:
        return "✅ 前缀 `{}`".format(prefix)
    return "❌ **未覆盖**"


def _path_cell(route: Route, sensitive_paths) -> str:
    mark = " " + SENSITIVE_MARK if route.rule in sensitive_paths else ""
    return "`{}`{}".format(route.rule, mark)


def _render_table(routes, declared, prefixes, sensitive_paths):
    lines = [
        "| 方法 | 路径 | 用途 | 来源 | 路由归属 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for route in routes:
        lines.append(
            "| {} | {} | {} | `{}` | {} |".format(
                " / ".join(route.methods),
                _path_cell(route, sensitive_paths),
                _cell(route.summary) or "—",
                route.source_ref,
                _ownership(route, declared, prefixes),
            )
        )
    return lines


def render(routes, declared, prefixes, checks, stamp: str, today: str, created: str) -> str:
    sensitive_paths = {path for path, _, _ in SENSITIVE_ENDPOINTS}
    sections = build_sections(routes)
    out = [
        "---",
        "type: reference",
        "area: api",
        "tags: [console-stack, api, reference, generated]",
        "createTime: {}".format(created),
        "updateTime: {}".format(today),
        "---",
        "",
        "<!-- 本文件由 scripts/gen_api_docs.py 生成，请勿手改：改动会在下次生成时被覆盖。 -->",
        "",
        "# API 参考（API）— 后端路由全量清单",
        "",
        "> **本文件由 `scripts/gen_api_docs.py` 自动生成，勿手改**——任何手工编辑都会在下次生成时被覆盖；"
        "要改内容请改代码（路径 / 方法 / docstring）或生成器里的人工文案常量。",
        "> **事实源以代码为准**：下表的路径、方法、用途（函数 docstring 首行）、定义处 `file:line` 全部由 "
        "`ast` 静态解析 `kernel/backend/*.py`（及 console 非 shim 残留）得到，不导入后端、不连数据库。",
        "> **生成时间**：{}".format(stamp),
        "> 配置字段逐项参考见 [configuration.md](./configuration.md)；部署链路见 [deployment.md](./deployment.md)；"
        "运维步骤见 [.agents/operations.md](../.agents/operations.md)（索引与主题文件）。",
        "",
        "## 0. 读表说明",
        "",
        "- **方法**：路由装饰器 `methods=` 的显式声明；未声明按 Flask 默认 `GET`。Flask 另会自动补 `HEAD` / `OPTIONS`，本表不重复列出。",
        "- **用途**：路由函数 docstring 的首行；首行断在句中时补到句末（后端 docstring 是中文硬折行的）。`—` 表示该函数没有 docstring。",
        "- **来源**：定义该路由的文件与装饰器行号（相对 `kernel/backend/` 或插件后端），可直接跳源码。同一个函数挂多个装饰器时逐条列出。",
        "- **鉴权**：全站统一，不逐路由标注——`auth.py` 的 `guard_console`（`before_request`）"
        "在 `CONSOLE_AUTH_ENABLED` 为真时要求 HTTP Basic，否则全部放行；线上基线是 `false`（终局裁决 P0-3 见 `.agents/contracts/backend/scope-boundaries.md`）。"
        "跨域由 `restrict_cors`（`after_request`）按 `CONSOLE_CORS_ORIGINS` 白名单收窄，默认同源。",
        "- **路由归属**：B6-4 起正式监控只走 Console PG，未知 `/api/*` 返回 404，不再转给已退役的 Beszel Hub。"
        "框架与插件 `/api` 路由仍按以下方式登记：",
        "  - `✅ 直接登记` = 路径字面量在 `CONSOLE_API_PATHS`（`app.py:{}`）；".format(_CONSOLE_API_PATHS_LINENO[0]),
        "  - `✅ 前缀 …` = 由 `_is_console_api()` 的前缀规则兜住；",
        "  - `❌ 未覆盖` = **未登记的 API，应显式 404**；",
        "  - `—` = 非 `/api` 路由，不涉及该机制。",
        "- {} 标记的端点有明确的「敏感值遮蔽 / IP 不外泄」纪律，见 §{}。".format(SENSITIVE_MARK, len(sections) + 1),
        "",
    ]

    for index, (title, intro, items) in enumerate(sections, start=1):
        out.append("## {}. {}".format(index, title))
        out.append("")
        if intro:
            out.append(intro)
            out.append("")
        out.extend(_render_table(items, declared, prefixes, sensitive_paths))
        out.append("")

    sensitive_index = len(sections) + 1
    out.append("## {}. 敏感端点".format(sensitive_index))
    out.append("")
    out.append(
        "以下端点的响应经过收窄，纪律来自 `e329e27`「tailscale IP 刻意不经 API 外泄」与 C10-a 的遮蔽约定。"
        "**改这些端点的响应形状前先回本节确认纪律。**"
    )
    out.append("")
    out.append("| 路径 | 纪律 | 说明 |")
    out.append("| --- | --- | --- |")
    for path, kind, note in SENSITIVE_ENDPOINTS:
        out.append("| `{}` {} | {} | {} |".format(path, SENSITIVE_MARK, kind, _cell(note)))
    out.append("")

    out.extend(_render_consistency(checks, sensitive_index + 1))
    out.extend(_render_usage(sensitive_index + 2))
    return "\n".join(out).rstrip() + "\n"


def _render_consistency(checks: Consistency, index: int):
    def flag(condition):
        return "✅" if condition else "❌"

    registered = len(checks.registered_paths)
    declared = len(checks.declared_paths)
    out = [
        "## {}. 一致性校验".format(index),
        "",
        "生成时同步跑的自检。`❌` 一律是缺陷，`--check` 会以退出码 2 结束。",
        "",
        "| 校验项 | 期望 | 实测 | 结果 |",
        "| --- | --- | --- | --- |",
        "| 路由装饰器总数 | — | {} | — |".format(checks.total_routes),
        "| 其中 `/api` 路由 | — | {} | — |".format(checks.api_routes),
        "| `CONSOLE_API_PATHS` 条目数 | — | {} | — |".format(declared),
        "| 直接登记的路由路径数 | = 条目数 {} | {} | {} |".format(
            declared, registered, flag(registered == declared)
        ),
        "| 死登记（常量有、代码无对应路由） | 0 | {} | {} |".format(
            len(checks.dead_entries), flag(not checks.dead_entries)
        ),
        "| 未覆盖的 `/api` 路由（应显式 404） | 0 | {} | {} |".format(
            len(checks.uncovered), flag(not checks.uncovered)
        ),
        "| 声明的敏感端点仍存在 | {}/{} | {} | {} |".format(
            len(SENSITIVE_ENDPOINTS) - len(checks.missing_sensitive),
            len(SENSITIVE_ENDPOINTS),
            len(SENSITIVE_ENDPOINTS) - len(checks.missing_sensitive),
            flag(not checks.missing_sensitive),
        ),
        "| `add_url_rule()` 注册（本生成器不解析） | 0 | {} | {} |".format(
            len(checks.unresolved), flag(not checks.unresolved)
        ),
        "| 缺 docstring 的路由函数 | — | {} | — |".format(len(checks.missing_docstring)),
        "",
    ]
    for label, items in (
        ("死登记", checks.dead_entries),
        ("未覆盖的 `/api` 路由", checks.uncovered),
        ("已不存在的敏感端点声明", checks.missing_sensitive),
        ("`add_url_rule()` 注册点", checks.unresolved),
    ):
        if items:
            out.append("**{}**：{}".format(label, "、".join("`{}`".format(i) for i in items)))
            out.append("")
    if checks.missing_docstring:
        out.append("<details>")
        out.append(
            "<summary>缺 docstring 的路由函数（{} 个，用途列显示 <code>—</code>）</summary>".format(
                len(checks.missing_docstring)
            )
        )
        out.append("")
        for item in checks.missing_docstring:
            out.append("- `{}`".format(item))
        out.append("")
        out.append("</details>")
        out.append("")
    return out


def _render_usage(index: int):
    return [
        "## {}. 生成与校验".format(index),
        "",
        "```bash",
        "python3 scripts/gen_api_docs.py             # 生成 / 更新 docs/api.md",
        "python3 scripts/gen_api_docs.py --check     # 与代码不一致则退出码 1（部署前门禁）",
        "python3 scripts/gen_api_docs.py --self-test # 生成器自测",
        "```",
        "",
        "- 纯 stdlib，无需后端 venv；**不导入 `app.py`**（它 import 期就会建库、写库并拉起四个守护线程）。",
        "- 幂等：内容未变时不重写文件，`updateTime` 与「生成时间」两行不参与 diff 比对。",
        "- 退出码：`0` 正常 / `1` `--check` 检出漂移 / `2` 一致性校验不通过。",
        "- **已挂进 CI**：`.github/workflows/ci.yml` 的 backend job 有一步 `Check API docs in sync`"
        "（Lint 之后、pytest 之前，先 `--self-test` 再 `--check`）。改了路由或路由 docstring 却没重新生成，CI 即红。",
        "- `scripts/deploy.sh` **不**调用它——部署链保持原样，这是提交期门禁不是部署期门禁。",
    ]


# ---- 落盘与比对 ---------------------------------------------------------


def normalize(text: str) -> str:
    """抹掉每次生成都会变的两行，用于幂等比对。"""
    for pattern, replacement in _VOLATILE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def existing_created(path: str, fallback: str) -> str:
    """沿用已有文件的 createTime，避免每次生成都改写创建时间。"""
    if not os.path.exists(path):
        return fallback
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("createTime:"):
                return line.split(":", 1)[1].strip() or fallback
            if line.startswith("# "):
                break
    return fallback


def build_document(root: str, now=None, created=None):
    """返回 (文档正文, 一致性校验结果)。`created` 只给自测固定 createTime 用。"""
    now = now or datetime.datetime.now().astimezone()
    routes, unresolved = collect_routes(root)
    declared = console_api_paths(root)
    prefixes = console_api_prefixes(root)
    checks = check_consistency(routes, declared, prefixes, unresolved)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M %z")
    output = os.path.join(root, DEFAULT_OUTPUT)
    created = created or existing_created(output, today)
    return render(routes, declared, prefixes, checks, stamp, today, created), checks


def _locate_console_api_paths_lineno(root: str):
    tree = _app_tree(root)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "CONSOLE_API_PATHS" for t in node.targets
        ):
            _CONSOLE_API_PATHS_LINENO[0] = node.lineno
            return node.lineno
    return _CONSOLE_API_PATHS_LINENO[0]


# ---- 自测 ---------------------------------------------------------------

_FIXTURE = '''# -*- coding: utf-8 -*-
"""合成后端模块（自测用）。"""

CONSOLE_API_PATHS = {"/api/alpha", "/api/beta"}


def _is_console_api(path):
    if path in CONSOLE_API_PATHS:
        return True
    if path.startswith("/api/gamma/"):
        return True
    for _prefix in ("/api/delta",):
        if path == _prefix or path.startswith(_prefix + "/"):
            return True
    return False


@app.route("/api/alpha", methods=["POST", "GET"])
def alpha():
    """带|竖线的首行。

    第二行不该进表。
    """
    return None


@app.route("/api/gamma/<gid>")
@app.route("/api/gamma/<gid>/")
def gamma(gid):
    return None


@app.route("/api/delta/wrapped", methods=["GET"])
def wrapped():
    """中文 docstring 硬折行，首行断在
    句中，要补到句末。

    第二段不该被拼进来。
    """
    return None


@app.route("/api/delta/mixed", methods=["GET"])
def mixed():
    """数据源不可用返回 available=false
    显式降级。"""
    return None


@app.route("/api/zeta", methods=["GET"])
def zeta():
    """没被任何规则兜住。"""
    return None


def register_routes(app):
    @app.route("/api/beta", methods=["PUT"])
    def beta():
        """嵌套注册也要抓到。"""
        return None
'''


def self_test(root: str) -> int:
    failures = []

    def check(label, condition, detail=""):
        if condition:
            print("  PASS  {}".format(label))
        else:
            failures.append(label)
            print("  FAIL  {} {}".format(label, detail))

    print("[1/3] 合成模块解析")
    tree = ast.parse(_FIXTURE)
    routes = routes_from_tree(tree, "fixture.py")
    by_rule = {r.rule: r for r in routes}
    check("抓到 7 条路由（含嵌套 register_routes 里的）", len(routes) == 7, len(routes))
    check("嵌套路由 /api/beta 在列", "/api/beta" in by_rule)
    check("一函数多装饰器逐条列出", "/api/gamma/<gid>/" in by_rule and "/api/gamma/<gid>" in by_rule)
    check(
        "methods 归一化为固定顺序",
        by_rule["/api/alpha"].methods == ("GET", "POST"),
        by_rule["/api/alpha"].methods,
    )
    check("未声明 methods 时按 Flask 默认 GET", by_rule["/api/gamma/<gid>"].methods == ("GET",))
    check("用途只取 docstring 首行", by_rule["/api/alpha"].summary == "带|竖线的首行。")
    check("表格单元格转义竖线", _cell(by_rule["/api/alpha"].summary) == "带\\|竖线的首行。")
    check("无 docstring 时用途为空", by_rule["/api/gamma/<gid>"].summary == "")
    check("占位符替换成具体段", by_rule["/api/gamma/<gid>"].concrete_path == "/api/gamma/x")
    check(
        "首行断在句中时补到句末，且不越过空行拼下一段",
        by_rule["/api/delta/wrapped"].summary == "中文 docstring 硬折行，首行断在句中，要补到句末。",
        by_rule["/api/delta/wrapped"].summary,
    )
    check(
        "中英折行交界补空格（中中交界不补）",
        by_rule["/api/delta/mixed"].summary == "数据源不可用返回 available=false 显式降级。",
        by_rule["/api/delta/mixed"].summary,
    )

    fixture_declared = {"/api/alpha", "/api/beta"}
    fixture_prefixes = prefixes_from_tree(tree)
    check(
        "前缀规则按源码顺序提取且滤掉拼接用的 '/'",
        fixture_prefixes == ("/api/gamma/", "/api/delta"),
        fixture_prefixes,
    )
    check("startswith 前缀命中", covering_prefix("/api/gamma/x", fixture_prefixes) == "/api/gamma/")
    check("前缀族等值命中", covering_prefix("/api/delta", fixture_prefixes) == "/api/delta")
    check("前缀族子路径命中", covering_prefix("/api/delta/x", fixture_prefixes) == "/api/delta")
    check("无关路径不命中", covering_prefix("/api/zeta", fixture_prefixes) is None)
    fixture_checks = check_consistency(routes, fixture_declared, fixture_prefixes, [])
    check("未覆盖路由被抓出", fixture_checks.uncovered == ["/api/zeta"], fixture_checks.uncovered)
    check(
        "缺 docstring 按函数去重（gamma 两个装饰器只记一次）",
        fixture_checks.missing_docstring == ["fixture.py:27 gamma()"],
        fixture_checks.missing_docstring,
    )
    check("一致性判定为不通过", fixture_checks.ok is False)

    print("[2/3] 真实仓库解析")
    real_routes, unresolved = collect_routes(root)
    real_declared = console_api_paths(root)
    real_prefixes = console_api_prefixes(root)
    real_checks = check_consistency(real_routes, real_declared, real_prefixes, unresolved)
    check("解析到路由", len(real_routes) > 40, len(real_routes))
    kernel_prefixes = prefixes_from_tree(_app_tree(root))
    check(
        "kernel 前缀规则与源码一致（任一侧漂移即红）",
        kernel_prefixes == (
            "/api/plugins", "/api/trigger/", "/api/service/",
            "/api/services/", "/api/machines/",
        ),
        kernel_prefixes,
    )
    plugin_prefixes = {
        prefix
        for manifest in Path(root, "plugins").glob("*/manifest.json")
        for prefix in json.loads(manifest.read_text())["api_prefixes"]
    }
    check(
        "组合前缀覆盖插件 manifest，kernel 不重复认领插件前缀",
        set(real_prefixes) == set(kernel_prefixes) | plugin_prefixes
        and not set(kernel_prefixes) & plugin_prefixes,
        real_prefixes,
    )
    check(
        "直接登记的路由路径数 == CONSOLE_API_PATHS 条目数",
        len(real_checks.registered_paths) == len(real_declared),
        "{} vs {}；缺 {}".format(
            len(real_checks.registered_paths), len(real_declared), real_checks.dead_entries
        ),
    )
    check("无死登记", not real_checks.dead_entries, real_checks.dead_entries)
    check("无未覆盖的 /api 路由", not real_checks.uncovered, real_checks.uncovered)
    check("声明的敏感端点都存在", not real_checks.missing_sensitive, real_checks.missing_sensitive)
    check("无 add_url_rule 漏网", not unresolved, unresolved)
    check(
        "hindsight 反代被识别为独立 app",
        any(r.app_name == "hindsight_app" for r in real_routes),
    )

    print("[3/3] 渲染幂等")
    first, _ = build_document(
        root, now=datetime.datetime(2026, 1, 2, 3, 4).astimezone(), created="2026-09-06"
    )
    second, _ = build_document(
        root, now=datetime.datetime(2027, 5, 6, 7, 8).astimezone(), created="2026-09-06"
    )
    check("两次生成只差易变行", normalize(first) == normalize(second))
    check("时间戳确实写进了文档", first != second)
    check("createTime 沿用已有文件的值，不随生成时间漂", "createTime: 2026-09-06" in second)
    check("文档含勿手改声明", "勿手改" in first)
    check(
        "敏感端点在表格与 §敏感端点两处都带锁标记",
        first.count(SENSITIVE_MARK) >= 2 * len(SENSITIVE_ENDPOINTS),
        first.count(SENSITIVE_MARK),
    )

    print()
    if failures:
        print("自测失败 {} 项：{}".format(len(failures), "、".join(failures)))
        return 1
    print("自测全部通过")
    return 0


# ---- CLI ----------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成 docs/api.md（后端路由全量参考）")
    parser.add_argument("--check", action="store_true", help="只校验，不写盘；有漂移退出码 1")
    parser.add_argument("--stdout", action="store_true", help="打印到标准输出，不写盘")
    parser.add_argument("--self-test", action="store_true", help="运行生成器自测")
    parser.add_argument("--root", default=repo_root(), help="仓库根，默认取脚本所在位置的上一级")
    parser.add_argument("--output", default=None, help="输出路径，默认 <root>/docs/api.md")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root)
    if args.self_test:
        _locate_console_api_paths_lineno(root)
        return self_test(root)

    _locate_console_api_paths_lineno(root)
    output = args.output or os.path.join(root, DEFAULT_OUTPUT)
    document, checks = build_document(root)

    if args.stdout:
        sys.stdout.write(document)
        return 0 if checks.ok else 2

    if args.check:
        if not os.path.exists(output):
            print("[check] 缺少 {}，跑一次 `python3 scripts/gen_api_docs.py` 生成".format(output))
            return 1
        with open(output, "r", encoding="utf-8") as handle:
            current = handle.read()
        if normalize(current) != normalize(document):
            diff = difflib.unified_diff(
                normalize(current).splitlines(True),
                normalize(document).splitlines(True),
                fromfile="{}（现有）".format(os.path.relpath(output, root)),
                tofile="生成物",
            )
            sys.stdout.writelines(diff)
            print("[check] docs/api.md 与代码不一致，跑 `python3 scripts/gen_api_docs.py` 重新生成")
            return 1
        print("[check] docs/api.md 与代码一致（{} 条路由）".format(checks.total_routes))
        return _report(checks)

    changed = True
    if os.path.exists(output):
        with open(output, "r", encoding="utf-8") as handle:
            current = handle.read()
        changed = normalize(current) != normalize(document)
    if changed:
        os.makedirs(os.path.dirname(output), exist_ok=True)
        with open(output, "w", encoding="utf-8") as handle:
            handle.write(document)
        print("[gen] 已写入 {}（{} 条路由）".format(os.path.relpath(output, root), checks.total_routes))
    else:
        print(
            "[gen] {} 内容未变化，保持原样（{} 条路由）".format(
                os.path.relpath(output, root), checks.total_routes
            )
        )
    return _report(checks)


def _report(checks: Consistency) -> int:
    if checks.ok:
        return 0
    if checks.dead_entries:
        print("[!] CONSOLE_API_PATHS 死登记：{}".format("、".join(checks.dead_entries)))
    if checks.uncovered:
        print("[!] 未覆盖的 /api 路由（应显式 404）：{}".format("、".join(checks.uncovered)))
    if checks.missing_sensitive:
        print("[!] 声明的敏感端点已不存在：{}".format("、".join(checks.missing_sensitive)))
    return 2


if __name__ == "__main__":
    sys.exit(main())
