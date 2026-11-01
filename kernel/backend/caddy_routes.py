# -*- coding: utf-8 -*-
"""只读采集 Caddy 反向代理路由，供设备页展示。"""
import json
import os
import shlex
import time
from concurrent.futures import ThreadPoolExecutor

import service_manager as sm

_cache = {}
_CADDY_ADAPT_ARGS = [
    "caddy",
    "adapt",
    "--config",
    "/etc/caddy/Caddyfile",
    "--adapter",
    "caddyfile",
]
_CADDY_SOURCES = {"container", "host"}
_CADDY_COMMAND_TIMEOUT = 12


def _route_ttl():
    try:
        return max(60, int(os.environ.get("CONSOLE_PROBE_INTERVAL", "600")))
    except ValueError:
        return 600


def _as_strings(value):
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if item is not None and str(item).strip()]


def _extend_unique(items, values):
    for value in values:
        if value not in items:
            items.append(value)


def _copy_context(context):
    return {
        "hosts": list(context.get("hosts") or []),
        "paths": list(context.get("paths") or []),
    }


def _match_contexts(parent, matchers):
    if not isinstance(matchers, list) or not matchers:
        return [_copy_context(parent)]

    contexts = []
    for matcher in matchers:
        if not isinstance(matcher, dict):
            continue
        current = _copy_context(parent)
        _extend_unique(current["hosts"], _as_strings(matcher.get("host")))
        _extend_unique(current["paths"], _as_strings(matcher.get("path")))
        contexts.append(current)
    return contexts or [_copy_context(parent)]


def _walk_routes(routes, listen, inherited, result):
    if not isinstance(routes, list):
        return
    for route in routes:
        if not isinstance(route, dict):
            continue
        for context in _match_contexts(inherited, route.get("match")):
            for handler in route.get("handle") or []:
                if not isinstance(handler, dict):
                    continue
                if handler.get("handler") == "reverse_proxy":
                    upstreams = []
                    for upstream in handler.get("upstreams") or []:
                        if not isinstance(upstream, dict):
                            continue
                        dial = upstream.get("dial")
                        if dial:
                            upstreams.append(str(dial))
                    if upstreams:
                        result.append(
                            {
                                "listen": list(listen),
                                "hosts": list(context["hosts"]),
                                "paths": list(context["paths"]),
                                "upstreams": upstreams,
                            }
                        )
                _walk_routes(handler.get("routes"), listen, context, result)


def parse_caddy_config(config):
    """把 Caddy JSON 配置归一为前置监听/匹配条件/上游数组。"""
    if not isinstance(config, dict):
        return []
    servers = (
        config
        .get("apps", {})
        .get("http", {})
        .get("servers", {})
    )
    if not isinstance(servers, dict):
        return []

    result = []
    for server in servers.values():
        if not isinstance(server, dict):
            continue
        listen = _as_strings(server.get("listen"))
        _walk_routes(server.get("routes"), listen, {"hosts": [], "paths": []}, result)

    unique = []
    seen = set()
    for route in result:
        key = (
            tuple(route["listen"]),
            tuple(route["hosts"]),
            tuple(route["paths"]),
            tuple(route["upstreams"]),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(route)
    return unique


def _error_message(stderr, stdout):
    text = (stderr or stdout or "unknown error").strip()
    if not text:
        return "unknown error"
    return text.splitlines()[-1][:160]


def is_caddy_service(svc):
    proxy = svc.get("proxy") or {}
    return str(proxy.get("provider") or "").strip().lower() == "caddy"


def _proxy_sources(svc):
    proxy = svc.get("proxy") or {}
    raw_sources = proxy.get("sources")
    if raw_sources is None:
        return ["container"]
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ValueError("Caddy sources 必须是非空数组")

    sources = []
    for raw_source in raw_sources:
        source = str(raw_source or "").strip().lower()
        if source not in _CADDY_SOURCES:
            raise ValueError(f"不支持的 Caddy 来源: {source or 'empty'}")
        if source not in sources:
            sources.append(source)
    return sources


def _proxy_target(svc):
    proxy = svc.get("proxy") or {}
    target = proxy.get("target")
    if not isinstance(target, dict):
        raise ValueError("Caddy target 必须显式配置")
    if not target.get("machine") or not target.get("container"):
        raise ValueError("Caddy target 缺少 machine/container")
    machine = sm._validate_machine(target["machine"])
    container = sm._container_name({"container": target["container"]})
    return machine, container


def _host_caddy_cmd(machine):
    """构造固定的宿主机 caddy adapt 命令，不接受配置路径或任意参数。"""
    machine = sm._validate_machine(machine)
    ssh_target = sm.MACHINE_SSH[machine]
    if ssh_target is None:
        return list(_CADDY_ADAPT_ARGS)

    remote = " ".join(shlex.quote(arg) for arg in _CADDY_ADAPT_ARGS)
    return ["ssh", *sm.SSH_OPTS, ssh_target, remote]


def _source_command(machine, container, source):
    if source == "host":
        return _host_caddy_cmd(machine)
    return sm._docker_cmd(machine, ["exec", container, *_CADDY_ADAPT_ARGS])


def _collect_source(machine, container, source):
    cmd = _source_command(machine, container, source)
    code, stdout, stderr = sm._run(cmd, timeout=_CADDY_COMMAND_TIMEOUT)
    if code != 0:
        return [], _error_message(stderr, stdout)
    try:
        routes = parse_caddy_config(json.loads(stdout))
    except (json.JSONDecodeError, TypeError, AttributeError) as exc:
        return [], f"invalid caddy config: {exc}"
    return [{**route, "source": source} for route in routes], None


def get_service_routes(svc, force=False):
    """读取显式标记的 Caddy 来源；失败降级，不影响服务主链路。"""
    if not is_caddy_service(svc):
        return [], None

    try:
        machine, container = _proxy_target(svc)
        sources = _proxy_sources(svc)
    except ValueError as exc:
        return [], str(exc)

    key = f"{machine}:{container}:{','.join(sources)}"
    now = time.time()
    cached = _cache.get(key)
    if not force and cached and now - cached["ts"] < _route_ttl():
        return cached["routes"], cached["error"]

    routes = []
    errors = []
    stale_routes = cached["routes"] if cached else []
    with ThreadPoolExecutor(max_workers=len(sources)) as executor:
        futures = {
            source: executor.submit(_collect_source, machine, container, source)
            for source in sources
        }
        for source in sources:
            source_routes, source_error = futures[source].result()
            if source_error:
                errors.append(f"{source}: {source_error}")
                routes.extend(
                    route for route in stale_routes
                    if route.get("source") == source
                )
                continue
            routes.extend(source_routes)

    error = "; ".join(errors) or None
    _cache[key] = {"ts": time.time(), "routes": routes, "error": error}
    return routes, error


def invalidate():
    _cache.clear()
