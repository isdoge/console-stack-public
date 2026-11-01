"""F08: disposable three-source fixtures; clocks are mocked, never changed on host."""
import json
import os
import threading
from types import SimpleNamespace

import pytest

import caddy_view as cv
import tailscale_view as tv


@pytest.fixture
def sources(tmp_path, monkeypatch):
    clock = {"wall": 1800000000.0, "mono": 1000.0}
    paths = {
        "tailnet": tmp_path / "tailnet.json",
        "upstreams": tmp_path / "upstreams.json",
        "metrics": tmp_path / "metrics.prom",
    }
    contents = {
        "tailnet": json.dumps({"Self": {}, "Peer": {}}),
        "upstreams": json.dumps([{"address": "127.0.0.1:9000", "num_requests": 7, "fails": 0}]),
        "metrics": 'caddy_http_requests_total{host="example.test"} 7\n',
    }
    for name, path in paths.items():
        path.write_text(contents[name], encoding="utf-8")
        os.utime(path, (clock["wall"], clock["wall"]))
    monkeypatch.setenv("CONSOLE_TAILNET_STATUS_FILE", str(paths["tailnet"]))
    monkeypatch.setenv("CONSOLE_CADDY_STATUS_FILE", str(paths["upstreams"]))
    monkeypatch.setenv("CONSOLE_CADDY_METRICS_FILE", str(paths["metrics"]))
    monkeypatch.setattr(tv.mc, "load_machines", lambda: {})
    monkeypatch.setattr(cv, "_service_index", lambda: {})
    monkeypatch.setattr(tv.time, "time", lambda: clock["wall"])
    monkeypatch.setattr(tv.time, "monotonic", lambda: clock["mono"])
    tv.invalidate()
    cv.invalidate()
    yield paths, clock
    tv.invalidate()
    cv.invalidate()


@pytest.fixture(params=["tailnet", "upstreams", "metrics"])
def source(request, sources):
    kind = request.param
    paths, clock = sources
    module = tv if kind == "tailnet" else cv
    getter = tv.get_tailnet_snapshot if kind == "tailnet" else cv.get_caddy_snapshot
    peek = tv.peek_tailnet_snapshot if kind == "tailnet" else cv.peek_caddy_snapshot
    reader = {"tailnet": "_read_status_from_file", "upstreams": "_read_upstreams_from_file",
              "metrics": "_read_metrics_from_file"}[kind]
    return {
        "kind": kind, "clock": clock, "path": paths[kind], "get": getter, "peek": peek,
        "module": module, "reader": reader,
        "flag": "metrics_available" if kind == "metrics" else "available",
        "error": "metrics_error" if kind == "metrics" else "error",
        "sample": {"tailnet": "source_sampled_at", "upstreams": "upstreams_sampled_at",
                   "metrics": "metrics_sampled_at"}[kind],
    }


def set_age(source, age):
    stamp = source["clock"]["wall"] - age
    os.utime(source["path"], (stamp, stamp))


@pytest.mark.parametrize("future,available", [(59, True), (60, True), (61, False), (86400, False)])
def test_source_future_tolerance(source, future, available):
    set_age(source, -future)
    snap = source["get"]()
    assert snap[source["flag"]] is available
    assert snap[source["sample"]] is not None
    if not available:
        assert "采集时间异常" in snap[source["error"]]
        assert str(source["path"]) not in json.dumps(snap)


def test_future_within_tolerance_is_clamped_to_zero_age(source):
    set_age(source, -60)
    assert source["get"]()[source["flag"]] is True
    source["clock"]["mono"] += 899
    assert source["peek"]()[source["flag"]] is True
    source["clock"]["mono"] += 1
    assert source["peek"]()[source["flag"]] is True
    source["clock"]["mono"] += 1
    assert source["peek"]()[source["flag"]] is False


def test_ttl_uses_monotonic_after_wall_rollback(source, monkeypatch):
    original = getattr(source["module"], source["reader"])
    reads = []

    def counted(path):
        reads.append(1)
        return original(path)

    monkeypatch.setattr(source["module"], source["reader"], counted)
    assert source["get"]()[source["flag"]] is True
    source["clock"]["mono"] += 600
    source["clock"]["wall"] -= 30
    assert source["get"]()[source["flag"]] is True
    assert len(reads) == 2


@pytest.mark.parametrize("reread", ["force", "ttl", "invalidate", "read_failure"])
def test_same_source_version_never_gets_a_later_deadline(source, monkeypatch, reread):
    set_age(source, 800)
    first = source["get"]()
    assert first[source["flag"]] is True
    source["clock"]["mono"] += 60
    source["clock"]["wall"] -= 30
    if reread == "invalidate":
        source["module"].invalidate()
    if reread == "ttl":
        monkeypatch.setenv("CONSOLE_TAILNET_INTERVAL", "60")
        monkeypatch.setenv("CONSOLE_CADDY_INTERVAL", "60")
    if reread == "read_failure":
        original = getattr(source["module"], source["reader"])

        def fail(path):
            raise PermissionError("test failure must not reset source freshness")

        monkeypatch.setattr(source["module"], source["reader"], fail)
        assert source["get"](force=True)[source["flag"]] is False
        monkeypatch.setattr(source["module"], source["reader"], original)
    assert source["get"](force=reread in {"force", "read_failure"})[source["flag"]] is True
    source["clock"]["mono"] = 1100
    assert source["peek"]()[source["flag"]] is True
    source["clock"]["mono"] += 1
    assert source["peek"]()[source["flag"]] is False
    assert source["get"](force=True)[source["flag"]] is False
    assert first[source["flag"]] is True


def test_already_stale_source_does_not_revive_after_rollback(source):
    set_age(source, 901)
    assert source["get"]()[source["flag"]] is False
    source["module"].invalidate()
    source["clock"]["wall"] -= 120
    source["clock"]["mono"] += 1
    assert source["get"](force=True)[source["flag"]] is False


def test_wall_forward_reread_shortens_same_source_deadline(source):
    set_age(source, 800)
    assert source["get"]()[source["flag"]] is True
    source["clock"]["mono"] += 1
    source["clock"]["wall"] += 100
    assert source["get"](force=True)[source["flag"]] is True
    source["clock"]["mono"] += 1
    source["clock"]["wall"] -= 100
    assert source["get"](force=True)[source["flag"]] is False


def test_new_source_identity_must_pass_wall_validation(source):
    set_age(source, 800)
    assert source["get"]()[source["flag"]] is True
    replacement = source["path"].with_name("replacement")
    replacement.write_text(source["path"].read_text(encoding="utf-8"), encoding="utf-8")
    stamp = source["clock"]["wall"] + 61
    os.utime(replacement, (stamp, stamp))
    replacement.replace(source["path"])
    assert source["get"](force=True)[source["flag"]] is False
    source["clock"]["wall"] += 2
    source["clock"]["mono"] += 2
    assert source["get"](force=True)[source["flag"]] is True


def test_rejected_future_reread_keeps_the_previous_deadline(source):
    set_age(source, 800)
    assert source["get"]()[source["flag"]] is True
    source["clock"]["wall"] -= 900
    assert source["get"](force=True)[source["flag"]] is False
    source["clock"]["wall"] += 900
    source["clock"]["mono"] += 101
    assert source["get"](force=True)[source["flag"]] is False


@pytest.mark.parametrize("rejected_age", [0, 901])
def test_rejected_new_content_keeps_previous_identity_deadline(source, monkeypatch, rejected_age):
    """Keep A's inode, reject B, then restore A after its monotonic expiry.

    Metrics is genuinely garbage; upstream JSON parses but its overflowing
    numeric value fails row conversion. Tailnet's otherwise tolerant mapper
    gets a synthetic registry failure after reading a real replacement file.
    """
    set_age(source, 800)
    first = source["get"]()
    assert first[source["flag"]] is True
    path = source["path"]
    first_stat = path.stat()
    saved = path.with_name("saved-" + path.name)
    path.rename(saved)
    rejected = {
        "metrics": "this is not supported metrics\n",
        "upstreams": '[{"address":"127.0.0.1:9000","num_requests":1e309,"fails":0}]',
        "tailnet": '{"Self": {}, "Peer": {}}',
    }[source["kind"]]
    path.write_text(rejected, encoding="utf-8")
    set_age(source, rejected_age)
    assert path.stat().st_ino != first_stat.st_ino

    def fail_registry():
        raise ValueError("synthetic registry parse failure")

    with monkeypatch.context() as patch:
        if source["kind"] == "tailnet":
            patch.setattr(tv.mc, "load_machines", fail_registry)
        assert source["get"](force=True)[source["flag"]] is False

    saved.replace(path)
    restored_stat = path.stat()
    assert (restored_stat.st_dev, restored_stat.st_ino, restored_stat.st_mtime_ns, restored_stat.st_size) == (
        first_stat.st_dev, first_stat.st_ino, first_stat.st_mtime_ns, first_stat.st_size)
    source["clock"]["wall"] -= 30
    source["clock"]["mono"] = 1101
    restored = source["get"](force=True)
    assert restored[source["flag"]] is False
    assert "过期" in restored[source["error"]]
    assert first[source["flag"]] is True


def test_new_valid_but_stale_source_retains_its_past_deadline(source):
    set_age(source, 800)
    assert source["get"]()[source["flag"]] is True
    replacement = source["path"].with_name("valid-stale-" + source["path"].name)
    replacement.write_text(source["path"].read_text(encoding="utf-8"), encoding="utf-8")
    old_stamp = source["clock"]["wall"] - 901
    os.utime(replacement, (old_stamp, old_stamp))
    replacement.replace(source["path"])
    assert source["get"](force=True)[source["flag"]] is False
    source["clock"]["wall"] -= 120
    source["clock"]["mono"] += 1
    assert source["get"](force=True)[source["flag"]] is False


@pytest.mark.parametrize("mtime", [float("nan"), float("inf"), float("-inf"), None, True, "invalid"])
def test_invalid_mtime_fails_safe_without_resetting_source(source, monkeypatch, mtime):
    set_age(source, 800)
    assert source["get"]()[source["flag"]] is True
    original = getattr(source["module"], source["reader"])

    def invalid(path):
        data, stat = original(path)
        return data, SimpleNamespace(st_mtime=mtime)

    monkeypatch.setattr(source["module"], source["reader"], invalid)
    failed = source["get"](force=True)
    assert failed[source["flag"]] is False
    assert failed[source["sample"]] is None
    assert "invalid" not in json.dumps(failed)
    monkeypatch.setattr(source["module"], source["reader"], original)
    source["clock"]["wall"] -= 30
    source["clock"]["mono"] += 101
    assert source["get"](force=True)[source["flag"]] is False


def test_three_sources_keep_independent_deadlines(sources):
    paths, clock = sources
    for kind, age in (("tailnet", 800), ("upstreams", 0), ("metrics", 300)):
        stamp = clock["wall"] - age
        os.utime(paths[kind], (stamp, stamp))
    assert tv.get_tailnet_snapshot()["available"] is True
    assert cv.get_caddy_snapshot()["metrics_available"] is True
    clock["wall"] -= 30
    clock["mono"] = 1101
    assert tv.peek_tailnet_snapshot()["available"] is False
    assert cv.peek_caddy_snapshot()["available"] is True
    assert cv.peek_caddy_snapshot()["metrics_available"] is True
    clock["mono"] = 1601
    assert cv.peek_caddy_snapshot()["metrics_available"] is False
    assert cv.peek_caddy_snapshot()["available"] is True
    clock["mono"] = 1901
    assert cv.peek_caddy_snapshot()["available"] is False


def test_refresh_publishes_a_whole_entry_and_peek_does_not_wait(source, monkeypatch):
    set_age(source, 800)
    first = source["get"]()
    old_entry = source["module"]._cache
    original = getattr(source["module"], source["reader"])
    entered = threading.Event()
    release = threading.Event()
    errors = []
    source["clock"]["wall"] -= 30
    source["clock"]["mono"] += 60

    def paused(path):
        entered.set()
        assert release.wait(timeout=5)
        return original(path)

    def refresh():
        try:
            source["get"](force=True)
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(source["module"], source["reader"], paused)
    worker = threading.Thread(target=refresh)
    worker.start()
    try:
        assert entered.wait(timeout=5)
        assert source["module"]._cache is old_entry
        assert source["peek"]() is first
    finally:
        release.set()
    worker.join(timeout=6)
    assert not worker.is_alive()
    assert errors == []
    assert source["module"]._cache is not old_entry
    assert old_entry["data"] is first
    source["clock"]["mono"] = 1101
    assert source["peek"]()[source["flag"]] is False
