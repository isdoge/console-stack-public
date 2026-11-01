# -*- coding: utf-8 -*-
"""tailscale_view.py 回归：socket 全 mock，不碰真实 tailscaled。

覆盖：KeyExpiry 边界（不过期/已过期/恰好 7 天）、IP→机器映射与白名单
（响应绝不含 tailscale IP）、TTL 与失败降级缓存、/api/tailnet 路由、
attention 的 ≤7 天窗口。
"""
import datetime
import json
import os
import pathlib
import socket
import sys

import pytest
import yaml

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ["CONSOLE_AUTH_ENABLED"] = "false"

import app as console_app  # noqa: E402
import attention  # noqa: E402
import feed_aggregator  # noqa: E402
import machine_config as mc  # noqa: E402
import tailscale_view as tv  # noqa: E402

NOW = datetime.datetime(2026, 9, 6, 12, 0, 0, tzinfo=datetime.timezone.utc)

# 测试台账：aly 故意不配 peer，验证「机器从 tailnet 消失 → online=None」
MACHINES_YAML = [
    {"id": "nas", "tailscale": "100.64.0.1"},
    {"id": "txy", "tailscale": "100.64.0.2"},
    {"id": "aly", "tailscale": "100.64.0.3"},
]


@pytest.fixture(autouse=True)
def _registry(tmp_path, monkeypatch):
    # env > yaml 是固定方向，测试前清掉可能存在的 tailscale 覆盖
    for key in list(os.environ):
        if key.startswith("MACHINE_TAILSCALE_"):
            monkeypatch.delenv(key, raising=False)
    path = tmp_path / "machines.yaml"
    path.write_text(yaml.safe_dump(MACHINES_YAML), encoding="utf-8")
    monkeypatch.setenv("CONSOLE_MACHINES_PATH", str(path))
    mc.invalidate()
    tv.invalidate()
    feed_aggregator._last_fetch_errors = []
    yield
    mc.invalidate()
    tv.invalidate()


def _status_json():
    return {
        "Self": {
            "HostName": "nas",
            "TailscaleIPs": ["100.64.0.1", "fd7a:115c:a1e0::1"],
            "Online": True,
            "KeyExpiry": "2026-09-16T12:00:00Z",  # 相对 NOW 恰好 10 天
            "LastSeen": "2026-09-06T11:59:00Z",
            "ClientVersion": "1.78.0",
            "OS": "linux",
            "UpdateAvailable": True,
            "Authorized": True,
        },
        "Peer": {
            "nodekey:aaa": {
                "HostName": "txy",
                "TailscaleIPs": ["100.64.0.2"],
                "Online": False,
                "KeyExpiry": "0001-01-01T00:00:00Z",  # Go 零值 = 不过期
                "LastSeen": "2026-09-01T08:00:00Z",
                "ClientVersion": "1.76.0",
                "OS": "linux",
                "UpdateAvailable": False,
                # Authorized 故意缺失 → 缺省 True（见口径）
            },
            # 台账外设备（手机/mac/win）不进响应
            "nodekey:bbb": {
                "HostName": "iphone",
                "TailscaleIPs": ["100.64.0.99"],
                "Online": True,
            },
        },
    }


def _snapshot(days_by_machine):
    """构造 attention 用的缓存快照。"""
    return {
        "available": True,
        "sampled_at": "2026-09-06T12:00:00Z",
        "error": None,
        "machines": [
            {
                "machine_id": mid,
                "hostname": mid,
                "online": True,
                "key_expiry_days": days,
                "last_seen": None,
                "client_version": None,
            }
            for mid, days in days_by_machine.items()
        ],
    }


# ---------------------------------------------------------------------------
# key_expiry_days：边界全列（含不过期/已过期）
# ---------------------------------------------------------------------------


def test_key_expiry_days_future():
    assert tv.key_expiry_days("2026-10-06T12:00:00Z", NOW) == 30


def test_key_expiry_days_exactly_7():
    assert tv.key_expiry_days("2026-09-13T12:00:00Z", NOW) == 7


def test_key_expiry_days_just_under_7():
    assert tv.key_expiry_days("2026-09-13T11:59:59Z", NOW) == 6


def test_key_expiry_days_expires_now_is_zero():
    assert tv.key_expiry_days("2026-09-06T12:00:00Z", NOW) == 0


def test_key_expiry_days_already_expired_negative():
    assert tv.key_expiry_days("2026-09-03T12:00:00Z", NOW) == -3


def test_key_expiry_days_expired_hours_floor_to_minus_one():
    assert tv.key_expiry_days("2026-09-06T00:00:00Z", NOW) == -1


def test_key_expiry_days_go_zero_time_is_none():
    assert tv.key_expiry_days("0001-01-01T00:00:00Z", NOW) is None


def test_key_expiry_days_missing_or_bad_is_none():
    assert tv.key_expiry_days(None, NOW) is None
    assert tv.key_expiry_days("", NOW) is None
    assert tv.key_expiry_days("not-a-date", NOW) is None


def test_key_expiry_days_naive_datetime_treated_as_utc():
    assert tv.key_expiry_days("2026-10-06T12:00:00", NOW) == 30


# ---------------------------------------------------------------------------
# 映射与白名单
# ---------------------------------------------------------------------------


def test_machine_rows_maps_self_and_peer():
    rows = tv._machine_rows(_status_json(), NOW)
    by_id = {row["machine_id"]: row for row in rows}

    nas = by_id["nas"]
    assert nas["hostname"] == "nas"
    assert nas["online"] is True
    assert nas["key_expiry_days"] == 10
    assert nas["last_seen"] == "2026-09-06T11:59:00Z"
    assert nas["client_version"] == "1.78.0"

    txy = by_id["txy"]
    assert txy["online"] is False
    assert txy["key_expiry_days"] is None  # Go 零值 = 不过期


def test_machine_rows_unknown_machine_is_explicit_none():
    rows = tv._machine_rows(_status_json(), NOW)
    aly = next(row for row in rows if row["machine_id"] == "aly")
    assert aly["online"] is None
    assert aly["hostname"] is None


def test_machine_rows_keep_registry_order():
    rows = tv._machine_rows(_status_json(), NOW)
    assert [row["machine_id"] for row in rows] == ["nas", "txy", "aly"]


def test_machine_rows_whitelist_no_ip_leak():
    """e329e27 纪律：tailscale IP（含台账外设备的）绝不出现在输出里。"""
    rows = tv._machine_rows(_status_json(), NOW)
    blob = json.dumps(rows)
    assert "100.64" not in blob
    assert "fd7a" not in blob
    assert "iphone" not in blob


def test_machine_rows_missing_online_defaults_false():
    status = _status_json()
    del status["Self"]["Online"]
    rows = tv._machine_rows(status, NOW)
    assert rows[0]["online"] is False


# ---------------------------------------------------------------------------
# D4 三字段：os / update_available / authorized（存在 / 缺失 / 类型异常）
# ---------------------------------------------------------------------------


def test_d4_fields_present():
    rows = tv._machine_rows(_status_json(), NOW)
    by_id = {row["machine_id"]: row for row in rows}
    nas = by_id["nas"]
    assert nas["os"] == "linux"
    assert nas["update_available"] is True
    assert nas["authorized"] is True
    txy = by_id["txy"]
    assert txy["os"] == "linux"
    assert txy["update_available"] is False
    # Authorized 缺失 → 缺省 True（tailscale 设备缺省已授权，不把缺字段误报成待授权）
    assert txy["authorized"] is True


def test_d4_fields_missing_peer_is_explicit_none():
    """peer 缺失（机器从 tailnet 消失）→ 三字段显式 None，与 online=None 同口径。"""
    rows = tv._machine_rows(_status_json(), NOW)
    aly = next(row for row in rows if row["machine_id"] == "aly")
    assert aly["os"] is None
    assert aly["update_available"] is None
    assert aly["authorized"] is None


_D4_FIELD_KEYS = {"update_available": "UpdateAvailable", "authorized": "Authorized"}


@pytest.mark.parametrize(
    "field, raw_values, expected_default",
    [
        # update_available：只有明确 True 才报有更新；字符串/数字/缺失一律 False
        ("update_available", ["yes", 1, None, ""], False),
        # authorized：只有明确 False 才报未授权；字符串/数字/缺失一律 True
        ("authorized", [0, None, ""], True),
    ],
)
def test_d4_fields_type_anomalies_take_defaults(field, raw_values, expected_default):
    for raw in raw_values:
        status = _status_json()
        status["Self"][_D4_FIELD_KEYS[field]] = raw
        rows = tv._machine_rows(status, NOW)
        assert rows[0][field] is expected_default, f"{field}={raw!r} 应取缺省 {expected_default}"


def test_d4_fields_explicit_false_and_true_pass_through():
    status = _status_json()
    status["Self"]["UpdateAvailable"] = False
    status["Self"]["Authorized"] = False
    rows = tv._machine_rows(status, NOW)
    assert rows[0]["update_available"] is False
    assert rows[0]["authorized"] is False


# ---------------------------------------------------------------------------
# 快照：TTL、失败降级、强制刷新
# ---------------------------------------------------------------------------


def test_snapshot_happy_path(monkeypatch):
    monkeypatch.setattr(tv, "_read_status_json", lambda path, timeout: _status_json())
    snap = tv.get_tailnet_snapshot()
    assert snap["available"] is True
    assert snap["error"] is None
    assert len(snap["machines"]) == 3
    assert "100.64" not in json.dumps(snap)


def test_snapshot_socket_missing_degrades(monkeypatch):
    def _boom(path, timeout):
        raise FileNotFoundError(2, "No such file or directory", path)

    monkeypatch.setattr(tv, "_read_status_json", _boom)
    snap = tv.get_tailnet_snapshot()
    assert snap["available"] is False
    assert snap["machines"] == []
    assert "socket" in snap["error"]


def test_snapshot_timeout_degrades(monkeypatch):
    def _boom(path, timeout):
        raise socket.timeout("timed out")

    monkeypatch.setattr(tv, "_read_status_json", _boom)
    snap = tv.get_tailnet_snapshot()
    assert snap["available"] is False
    assert "超时" in snap["error"]


def test_snapshot_http_error_degrades(monkeypatch):
    def _boom(path, timeout):
        raise RuntimeError("HTTP 500")

    monkeypatch.setattr(tv, "_read_status_json", _boom)
    snap = tv.get_tailnet_snapshot()
    assert snap["available"] is False
    assert "HTTP 500" in snap["error"]


# ---------------------------------------------------------------------------
# 文件源：cron 导出 status.json（owner 2026-09-06 拍板终态）
# ---------------------------------------------------------------------------


def test_snapshot_file_source_happy_path(tmp_path, monkeypatch):
    status_file = tmp_path / "status.json"
    status_file.write_text(json.dumps(_status_json()), encoding="utf-8")
    monkeypatch.setenv("CONSOLE_TAILNET_STATUS_FILE", str(status_file))
    snap = tv.get_tailnet_snapshot()
    assert snap["available"] is True
    assert snap["error"] is None
    assert len(snap["machines"]) == 3
    assert "100.64" not in json.dumps(snap)


def test_snapshot_file_missing_degrades_with_cron_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_TAILNET_STATUS_FILE", str(tmp_path / "absent.json"))
    snap = tv.get_tailnet_snapshot()
    assert snap["available"] is False
    assert "cron" in snap["error"]


def test_snapshot_file_broken_json_degrades(tmp_path, monkeypatch):
    status_file = tmp_path / "status.json"
    status_file.write_text('{"Self": truncated', encoding="utf-8")
    monkeypatch.setenv("CONSOLE_TAILNET_STATUS_FILE", str(status_file))
    snap = tv.get_tailnet_snapshot()
    assert snap["available"] is False
    assert "解析失败" in snap["error"]


def test_file_source_takes_priority_over_socket(tmp_path, monkeypatch):
    status_file = tmp_path / "status.json"
    status_file.write_text(json.dumps(_status_json()), encoding="utf-8")
    monkeypatch.setenv("CONSOLE_TAILNET_STATUS_FILE", str(status_file))

    def _must_not_be_called(path, timeout):
        raise AssertionError("配置了文件源时不应触碰 socket")

    monkeypatch.setattr(tv, "_read_status_json", _must_not_be_called)
    snap = tv.get_tailnet_snapshot()
    assert snap["available"] is True


def test_file_source_invalid_root_degrades(tmp_path, monkeypatch):
    status_file = tmp_path / "status.json"
    status_file.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    monkeypatch.setenv("CONSOLE_TAILNET_STATUS_FILE", str(status_file))
    snap = tv.get_tailnet_snapshot()
    assert snap["available"] is False
    assert "非 JSON 对象" in snap["error"]


def test_snapshot_ttl_caches(monkeypatch):
    calls = []

    def _fake(path, timeout):
        calls.append(1)
        return _status_json()

    monkeypatch.setattr(tv, "_read_status_json", _fake)
    first = tv.get_tailnet_snapshot()
    second = tv.get_tailnet_snapshot()
    assert len(calls) == 1
    assert first is second


def test_snapshot_failure_also_cached(monkeypatch):
    """失败同样占 TTL：坏掉的 socket 不会被每个请求反复戳。"""
    calls = []

    def _boom(path, timeout):
        calls.append(1)
        raise FileNotFoundError(2, "missing", path)

    monkeypatch.setattr(tv, "_read_status_json", _boom)
    tv.get_tailnet_snapshot()
    snap = tv.get_tailnet_snapshot()
    assert len(calls) == 1
    assert snap["available"] is False


def test_snapshot_force_refreshes(monkeypatch):
    calls = []

    def _fake(path, timeout):
        calls.append(1)
        return _status_json()

    monkeypatch.setattr(tv, "_read_status_json", _fake)
    tv.get_tailnet_snapshot()
    tv.get_tailnet_snapshot(force=True)
    assert len(calls) == 2


def test_peek_never_fetches(monkeypatch):
    def _boom(path, timeout):
        raise AssertionError("peek 不许触网")

    monkeypatch.setattr(tv, "_read_status_json", _boom)
    assert tv.peek_tailnet_snapshot() is None


# ---------------------------------------------------------------------------
# /api/tailnet 路由
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 显式设 test 角色：防止被前面直接赋值 os.environ 的测试污染成 client，
    # 导致 GET 请求被 forward_observation_to_authority 转发而非本地执行（503）。
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "test")
    monkeypatch.setattr(console_app, "DB_PATH", str(tmp_path / "console.db"))
    monkeypatch.setattr(console_app, "ASSETS_PATH", str(tmp_path / "assets.yaml"))
    console_app.init_db()
    return console_app.app.test_client()


def test_route_returns_whitelist_payload(client, monkeypatch):
    monkeypatch.setattr(
        tv, "get_tailnet_snapshot", lambda: {
            "available": True,
            "sampled_at": "2026-09-06T12:00:00Z",
            "error": None,
            "machines": tv._machine_rows(_status_json(), NOW),
        }
    )
    resp = client.get("/api/tailnet")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "100.64" not in body  # e329e27：IP 不经 API 外泄
    data = resp.get_json()
    assert data["available"] is True
    assert set(data["machines"][0].keys()) == {
        "machine_id",
        "hostname",
        "online",
        "key_expiry_days",
        "last_seen",
        "client_version",
        # D4：三字段进白名单（口径见 tailscale_view 模块 docstring）
        "os",
        "update_available",
        "authorized",
    }


def test_route_degraded_is_200_not_500(client, monkeypatch):
    monkeypatch.setattr(
        tv, "get_tailnet_snapshot", lambda: {
            "available": False,
            "sampled_at": "2026-09-06T12:00:00Z",
            "error": "tailscaled socket 不存在（宿主演化或挂载缺失）",
            "machines": [],
        }
    )
    resp = client.get("/api/tailnet")
    assert resp.status_code == 200
    assert resp.get_json()["available"] is False


# ---------------------------------------------------------------------------
# attention：≤7 天进今日关注，只读缓存
# ---------------------------------------------------------------------------


def test_tailnet_items_quiet_without_snapshot(monkeypatch):
    monkeypatch.setattr(tv, "peek_tailnet_snapshot", lambda: None)
    assert attention._tailnet_items() == []


def test_tailnet_items_quiet_when_unavailable(monkeypatch):
    snap = _snapshot({"nas": 3})
    snap["available"] = False
    monkeypatch.setattr(tv, "peek_tailnet_snapshot", lambda: snap)
    assert attention._tailnet_items() == []


def test_tailnet_items_warns_at_7_days(monkeypatch):
    monkeypatch.setattr(
        tv, "peek_tailnet_snapshot", lambda: _snapshot({"nas": 7, "txy": 8})
    )
    items = attention._tailnet_items()
    assert len(items) == 1
    assert items[0]["severity"] == "warn"
    assert items[0]["kind"] == "tailnet"
    assert "nas" in items[0]["title"]


def test_tailnet_items_expired_is_error(monkeypatch):
    monkeypatch.setattr(tv, "peek_tailnet_snapshot", lambda: _snapshot({"nas": -2}))
    items = attention._tailnet_items()
    assert items[0]["severity"] == "error"
    assert "已过期" in items[0]["title"]


def test_tailnet_items_skips_non_expiring(monkeypatch):
    monkeypatch.setattr(
        tv, "peek_tailnet_snapshot", lambda: _snapshot({"nas": None, "txy": 30})
    )
    assert attention._tailnet_items() == []


def test_collect_attention_chains_tailnet(tmp_path, monkeypatch):
    assets = tmp_path / "assets.yaml"
    assets.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(
        tv, "peek_tailnet_snapshot", lambda: _snapshot({"nas": 3})
    )
    items = attention.collect_attention(str(assets), str(tmp_path / "console.db"))
    assert [item for item in items if item["kind"] == "tailnet"]


def _timed_file_source(tmp_path, monkeypatch, age=0):
    clock = {"wall": 1800000000.0, "mono": 1000.0}
    path = tmp_path / "status.json"
    status = _status_json()
    status["Self"]["KeyExpiry"] = (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3)
    ).isoformat()
    path.write_text(json.dumps(status), encoding="utf-8")
    os.utime(path, (clock["wall"] - age, clock["wall"] - age))
    monkeypatch.setenv("CONSOLE_TAILNET_STATUS_FILE", str(path))
    monkeypatch.setattr(tv.time, "time", lambda: clock["wall"])
    monkeypatch.setattr(tv.time, "monotonic", lambda: clock["mono"])
    return path, clock


@pytest.mark.parametrize("age,available", [(899, True), (900, True), (901, False), (86400, False)])
def test_file_source_age_boundary(tmp_path, monkeypatch, age, available):
    path, clock = _timed_file_source(tmp_path, monkeypatch, age)
    snap = tv.get_tailnet_snapshot()
    assert snap["available"] is available
    assert snap["source_sampled_at"] == datetime.datetime.fromtimestamp(
        path.stat().st_mtime, datetime.timezone.utc
    ).isoformat().replace("+00:00", "Z")
    if not available:
        assert snap["machines"] == []
        assert "过期" in snap["error"]
        assert str(path) not in json.dumps(snap)
        clock["wall"] += 600
        clock["mono"] += 600
        assert tv.get_tailnet_snapshot()["available"] is False


def test_file_cache_and_no_io_peek_revoke_expired_attention(tmp_path, monkeypatch):
    _, clock = _timed_file_source(tmp_path, monkeypatch, age=899)
    first = tv.get_tailnet_snapshot()
    assert attention._tailnet_items()
    clock["wall"] += 1
    clock["mono"] += 1
    assert tv.peek_tailnet_snapshot()["available"] is True
    clock["wall"] += 1
    clock["mono"] += 1

    def no_io(*args, **kwargs):
        raise AssertionError("cached expiry and peek must not read a source or config")

    monkeypatch.setattr(tv, "_load_status", no_io)
    monkeypatch.setattr(tv, "status_file_path", no_io)
    monkeypatch.setattr(tv.mc, "load_machines", no_io)
    assert tv.get_tailnet_snapshot()["available"] is False
    monkeypatch.setattr(tv, "tailnet_interval", no_io)
    expired = tv.peek_tailnet_snapshot()
    assert expired["machines"] == []
    assert expired["source_sampled_at"] == first["source_sampled_at"]
    assert attention._tailnet_items() == []
    assert first["available"] is True  # Readers holding an older entry are not mutated.


@pytest.mark.parametrize("force", [False, True])
def test_file_atomic_replacement_recovers_by_force_or_ttl(tmp_path, monkeypatch, force):
    path, clock = _timed_file_source(tmp_path, monkeypatch, age=901)
    assert tv.get_tailnet_snapshot()["available"] is False
    replacement = tmp_path / "new.json"
    replacement.write_text(json.dumps(_status_json()), encoding="utf-8")
    os.utime(replacement, (clock["wall"], clock["wall"]))
    replacement.replace(path)
    assert tv.get_tailnet_snapshot()["available"] is False
    if not force:
        clock["wall"] += 600
        clock["mono"] += 600
    assert tv.get_tailnet_snapshot(force=force)["available"] is True


def test_file_read_keeps_content_and_mtime_on_same_descriptor(tmp_path, monkeypatch):
    path, clock = _timed_file_source(tmp_path, monkeypatch, age=901)
    real_load = json.load

    def replace_after_read(handle):
        parsed = real_load(handle)
        replacement = tmp_path / "new.json"
        replacement.write_text(json.dumps({}), encoding="utf-8")
        os.utime(replacement, (clock["wall"], clock["wall"]))
        replacement.replace(path)
        return parsed

    monkeypatch.setattr(tv.json, "load", replace_after_read)
    snap = tv.get_tailnet_snapshot()
    assert snap["available"] is False
    assert "过期" in snap["error"]
    assert path.stat().st_mtime == clock["wall"]
