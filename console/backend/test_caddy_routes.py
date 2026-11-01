# -*- coding: utf-8 -*-
"""Caddy 路由采集回归：纯解析、固定命令与缓存降级。"""
import json
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import caddy_routes  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_cache():
    caddy_routes.invalidate()
    yield
    caddy_routes.invalidate()


def _sample_config():
    return {
        "apps": {
            "http": {
                "servers": {
                    "srv0": {
                        "listen": [":3000"],
                        "routes": [
                            {
                                "match": [{"path": ["/static/*", "/assets/*"]}],
                                "handle": [
                                    {
                                        "handler": "subroute",
                                        "routes": [
                                            {
                                                "handle": [
                                                    {
                                                        "handler": "reverse_proxy",
                                                        "upstreams": [{"dial": "new-api:3000"}],
                                                    }
                                                ]
                                            }
                                        ],
                                    }
                                ],
                            },
                            {
                                "handle": [
                                    {
                                        "handler": "reverse_proxy",
                                        "upstreams": [{"dial": "new-api:3000"}],
                                    }
                                ]
                            },
                        ],
                    }
                }
            }
        }
    }


def _service(sources=None, **overrides):
    proxy = {
        "provider": "caddy",
        "target": {"machine": "aly", "container": "new-api-proxy"},
    }
    if sources is not None:
        proxy["sources"] = sources
    return {
        "name": "new-api-proxy",
        "machine": "aly",
        "container": "new-api-proxy",
        "proxy": proxy,
        **overrides,
    }


def test_parse_nested_routes_preserves_frontends_and_upstreams():
    assert caddy_routes.parse_caddy_config(_sample_config()) == [
        {
            "listen": [":3000"],
            "hosts": [],
            "paths": ["/static/*", "/assets/*"],
            "upstreams": ["new-api:3000"],
        },
        {
            "listen": [":3000"],
            "hosts": [],
            "paths": [],
            "upstreams": ["new-api:3000"],
        },
    ]


def test_parse_non_object_config_returns_empty():
    assert caddy_routes.parse_caddy_config([]) == []


def test_parse_host_matcher_and_deduplicates_routes():
    config = _sample_config()
    route = config["apps"]["http"]["servers"]["srv0"]["routes"][1]
    route["match"] = [{"host": ["api.example.test"]}]
    config["apps"]["http"]["servers"]["srv0"]["routes"].append(route.copy())

    rows = caddy_routes.parse_caddy_config(config)
    assert rows[-1]["hosts"] == ["api.example.test"]
    assert len(rows) == 2


def test_parse_matcher_sets_preserves_or_semantics():
    config = _sample_config()
    route = config["apps"]["http"]["servers"]["srv0"]["routes"][1]
    route["match"] = [
        {"host": ["a.example.test"], "path": ["/a"]},
        {"host": ["b.example.test"], "path": ["/b"]},
    ]

    rows = caddy_routes.parse_caddy_config(config)
    assert rows[-2:] == [
        {
            "listen": [":3000"],
            "hosts": ["a.example.test"],
            "paths": ["/a"],
            "upstreams": ["new-api:3000"],
        },
        {
            "listen": [":3000"],
            "hosts": ["b.example.test"],
            "paths": ["/b"],
            "upstreams": ["new-api:3000"],
        },
    ]


def test_unconfigured_service_does_not_execute(monkeypatch):
    monkeypatch.setattr(
        caddy_routes.sm,
        "_run",
        lambda *_a, **_k: pytest.fail("unconfigured service must not execute"),
    )
    assert caddy_routes.get_service_routes({"name": "svc", "machine": "aly"}) == ([], None)
    assert caddy_routes.is_caddy_service(
        {"proxy": {"provider": " Caddy "}}
    ) is True


def test_rejects_unknown_source_without_executing(monkeypatch):
    monkeypatch.setattr(
        caddy_routes.sm,
        "_run",
        lambda *_a, **_k: pytest.fail("invalid source must not execute"),
    )
    routes, error = caddy_routes.get_service_routes(
        _service(["host", "shell"])
    )
    assert routes == []
    assert error == "不支持的 Caddy 来源: shell"


def test_requires_explicit_target_without_executing(monkeypatch):
    monkeypatch.setattr(
        caddy_routes.sm,
        "_run",
        lambda *_a, **_k: pytest.fail("missing target must not execute"),
    )
    routes, error = caddy_routes.get_service_routes(
        {"name": "proxy", "machine": "aly", "proxy": {"provider": "caddy"}}
    )
    assert routes == []
    assert error == "Caddy target 必须显式配置"


def test_host_command_has_fixed_target_and_arguments(monkeypatch):
    monkeypatch.setitem(caddy_routes.sm.MACHINE_SSH, "aly", "root@100.64.0.40")
    assert caddy_routes._host_caddy_cmd("aly") == [
        "ssh",
        *caddy_routes.sm.SSH_OPTS,
        "root@100.64.0.40",
        "caddy adapt --config /etc/caddy/Caddyfile --adapter caddyfile",
    ]


def test_fixed_caddy_command_is_cached_and_force_refreshes(monkeypatch):
    calls = []

    monkeypatch.setattr(caddy_routes.sm, "_docker_cmd", lambda machine, args: [machine, *args])

    def fake_run(cmd, timeout=20):
        calls.append((cmd, timeout))
        return 0, json.dumps(_sample_config()), ""

    monkeypatch.setattr(caddy_routes.sm, "_run", fake_run)
    svc = _service(
        machine="txy",
        container="redirected-container",
    )

    first, error = caddy_routes.get_service_routes(svc)
    second, _ = caddy_routes.get_service_routes(svc)
    refreshed, _ = caddy_routes.get_service_routes(svc, force=True)

    assert error is None
    assert first == second == refreshed
    assert len(calls) == 2
    assert calls[0][0] == [
        "aly",
        "exec",
        "new-api-proxy",
        "caddy",
        "adapt",
        "--config",
        "/etc/caddy/Caddyfile",
        "--adapter",
        "caddyfile",
    ]
    assert calls[0][1] == caddy_routes._CADDY_COMMAND_TIMEOUT


def test_host_and_container_sources_are_labeled(monkeypatch):
    calls = []
    monkeypatch.setattr(
        caddy_routes,
        "_host_caddy_cmd",
        lambda machine: [machine, "host-caddy"],
    )
    monkeypatch.setattr(
        caddy_routes.sm,
        "_docker_cmd",
        lambda machine, args: [machine, *args],
    )

    def fake_run(cmd, timeout=20):
        calls.append((cmd, timeout))
        return 0, json.dumps(_sample_config()), ""

    monkeypatch.setattr(caddy_routes.sm, "_run", fake_run)
    routes, error = caddy_routes.get_service_routes(
        _service(["host", "container", "host"])
    )

    assert error is None
    assert [route["source"] for route in routes] == [
        "host",
        "host",
        "container",
        "container",
    ]
    assert len(calls) == 2
    assert all(timeout == caddy_routes._CADDY_COMMAND_TIMEOUT for _, timeout in calls)
    assert ["aly", "host-caddy"] in [cmd for cmd, _ in calls]
    assert [
        "aly",
        "exec",
        "new-api-proxy",
        "caddy",
        "adapt",
        "--config",
        "/etc/caddy/Caddyfile",
        "--adapter",
        "caddyfile",
    ] in [cmd for cmd, _ in calls]


def test_partial_source_failure_keeps_successful_routes(monkeypatch):
    monkeypatch.setattr(
        caddy_routes,
        "_host_caddy_cmd",
        lambda machine: [machine, "host-caddy"],
    )
    monkeypatch.setattr(
        caddy_routes.sm,
        "_docker_cmd",
        lambda machine, args: [machine, *args],
    )

    def fake_run(cmd, timeout=20):
        if cmd[-1] == "host-caddy":
            return 124, "", "timeout"
        return 0, json.dumps(_sample_config()), ""

    monkeypatch.setattr(caddy_routes.sm, "_run", fake_run)
    routes, error = caddy_routes.get_service_routes(
        _service(["host", "container"])
    )

    assert len(routes) == 2
    assert {route["source"] for route in routes} == {"container"}
    assert error == "host: timeout"


def test_failed_source_reuses_its_stale_routes(monkeypatch):
    state = {"host_fails": False}
    monkeypatch.setattr(
        caddy_routes,
        "_host_caddy_cmd",
        lambda machine: [machine, "host-caddy"],
    )
    monkeypatch.setattr(
        caddy_routes.sm,
        "_docker_cmd",
        lambda machine, args: [machine, *args],
    )

    def fake_run(cmd, timeout=20):
        if cmd[-1] == "host-caddy" and state["host_fails"]:
            return 124, "", "timeout"
        return 0, json.dumps(_sample_config()), ""

    monkeypatch.setattr(caddy_routes.sm, "_run", fake_run)
    svc = _service(["host", "container"])
    initial, initial_error = caddy_routes.get_service_routes(svc)
    state["host_fails"] = True
    refreshed, refreshed_error = caddy_routes.get_service_routes(svc, force=True)

    assert initial_error is None
    assert refreshed_error == "host: timeout"
    assert refreshed == initial


def test_all_sources_failure_returns_combined_error(monkeypatch):
    monkeypatch.setattr(
        caddy_routes,
        "_host_caddy_cmd",
        lambda machine: [machine, "host-caddy"],
    )
    monkeypatch.setattr(
        caddy_routes.sm,
        "_docker_cmd",
        lambda machine, args: [machine, *args],
    )
    monkeypatch.setattr(
        caddy_routes.sm,
        "_run",
        lambda cmd, timeout=20: (124, "", "timeout"),
    )

    routes, error = caddy_routes.get_service_routes(
        _service(["host", "container"])
    )
    assert routes == []
    assert error == "host: timeout; container: timeout"


def test_invalid_json_returns_error_without_raising(monkeypatch):
    monkeypatch.setattr(caddy_routes.sm, "_docker_cmd", lambda machine, args: ["fake"])
    monkeypatch.setattr(caddy_routes.sm, "_run", lambda cmd, timeout=20: (0, "not json", ""))
    routes, error = caddy_routes.get_service_routes(
        _service()
    )
    assert routes == []
    assert error.startswith("container: invalid caddy config:")
