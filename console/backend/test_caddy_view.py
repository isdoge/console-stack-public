# -*- coding: utf-8 -*-
"""C11：caddy_view 的文件源/映射/降级/纪律测试（全部 mock，不碰真实数据源）。"""
import datetime
import json
import os
import threading

import pytest

import caddy_view as cv
import entry_view


@pytest.fixture(autouse=True)
def _reset_cache():
    cv.invalidate()
    yield
    cv.invalidate()


@pytest.mark.parametrize("age,available", [(0, True), (900, True), (901, False)])
def test_metrics_freshness_boundary(tmp_path, monkeypatch, age, available):
    _with_both_sources(tmp_path, monkeypatch)
    now = 1800000000
    os.utime(tmp_path / "status.json", (now, now))
    os.utime(tmp_path / "metrics.prom", (now - age, now - age))
    monkeypatch.setattr(cv.time, "time", lambda: now)
    monkeypatch.setattr(cv.time, "monotonic", lambda: 1000.0)
    snap = cv.get_caddy_snapshot()
    assert snap["metrics_available"] is available
    assert snap["metrics_sampled_at"] == datetime.datetime.fromtimestamp(
        now - age, datetime.timezone.utc
    ).isoformat().replace("+00:00", "Z")
    assert snap["available"] is True
    if not available:
        assert snap["metrics_error"] == "Caddy 指标数据已过期（超过 15 分钟未更新）"
        assert snap["hosts"] == []
        assert snap["counting_since"] is None


def test_metrics_cache_cannot_extend_freshness_and_force_recovers(tmp_path, monkeypatch):
    _with_both_sources(tmp_path, monkeypatch)
    now = [1800000000]
    os.utime(tmp_path / "metrics.prom", (now[0] - 899, now[0] - 899))
    monkeypatch.setattr(cv.time, "time", lambda: now[0])
    monkeypatch.setattr(cv.time, "monotonic", lambda: 1000.0 + now[0] - 1800000000)
    assert cv.get_caddy_snapshot()["metrics_available"] is True
    now[0] += 1
    assert cv.get_caddy_snapshot()["metrics_available"] is True
    now[0] += 1
    assert cv.get_caddy_snapshot()["metrics_available"] is False
    replacement = tmp_path / "replacement.prom"
    replacement.write_text(_METRICS_SAMPLE, encoding="utf-8")
    os.utime(replacement, (now[0], now[0]))
    replacement.replace(tmp_path / "metrics.prom")
    assert cv.get_caddy_snapshot()["metrics_available"] is False
    assert cv.get_caddy_snapshot(force=True)["metrics_available"] is True


def test_metrics_read_uses_open_descriptor_during_atomic_replace(tmp_path, monkeypatch):
    path = tmp_path / "metrics.prom"
    path.write_text("old contents", encoding="utf-8")
    os.utime(path, (1800000000, 1800000000))
    original_inode = path.stat().st_ino
    real_fstat = os.fstat

    def replace_then_fstat(fd):
        replacement = tmp_path / "replacement.prom"
        replacement.write_text("new contents", encoding="utf-8")
        os.utime(replacement, (1800000901, 1800000901))
        replacement.replace(path)
        return real_fstat(fd)

    monkeypatch.setattr(cv.os, "fstat", replace_then_fstat)
    contents, stat = cv._read_metrics_from_file(path)
    assert contents == "old contents"
    assert stat.st_mtime == 1800000000
    assert stat.st_ino == original_inode
    assert path.stat().st_ino != original_inode
    assert path.read_text(encoding="utf-8") == "new contents"


def test_metrics_error_cache_recovers_after_ttl(tmp_path, monkeypatch):
    _with_both_sources(tmp_path, monkeypatch)
    now = [1800000000]
    os.utime(tmp_path / "metrics.prom", (now[0] - 901, now[0] - 901))
    monkeypatch.setattr(cv.time, "time", lambda: now[0])
    monkeypatch.setattr(cv.time, "monotonic", lambda: 1000.0 + now[0] - 1800000000)
    assert cv.get_caddy_snapshot()["metrics_available"] is False
    now[0] += 600
    os.utime(tmp_path / "metrics.prom", (now[0], now[0]))
    assert cv.get_caddy_snapshot()["metrics_available"] is True


def test_cache_publishes_immutable_entries_during_concurrent_refresh(tmp_path, monkeypatch):
    clock = _timed_caddy_sources(tmp_path, monkeypatch)
    first = cv.get_caddy_snapshot()
    old_entry = cv._cache
    old_source = old_entry["metrics_source"]
    clock["wall"] += 1
    clock["mono"] += 1
    replacement = tmp_path / "replacement.prom"
    replacement.write_text('caddy_http_requests_total{host="new.test"} 77\n', encoding="utf-8")
    os.utime(replacement, (clock["wall"], clock["wall"]))
    replacement.replace(tmp_path / "metrics.prom")
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    errors = []
    original = cv._read_metrics_from_file

    def read_metrics(path):
        if threading.current_thread().name == "first":
            entered.set()
            assert release.wait(timeout=5)
        return original(path)

    def refresh():
        try:
            if threading.current_thread().name == "second":
                second_started.set()
            cv.get_caddy_snapshot(force=True)
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(cv, "_read_metrics_from_file", read_metrics)
    workers = [threading.Thread(target=refresh, name=name) for name in ("first", "second")]
    workers[0].start()
    try:
        assert entered.wait(timeout=5)
        workers[1].start()
        assert second_started.wait(timeout=5)
        assert cv.peek_caddy_snapshot() is first
        assert cv._cache is old_entry  # No new upstream data paired with old metrics state.
    finally:
        release.set()
    for worker in workers:
        worker.join(timeout=6)
        assert not worker.is_alive()
    assert errors == []
    assert cv._cache["data"]["metrics_available"] is True
    assert cv._cache["data"]["hosts"][0]["requests_total"] == 77
    assert cv._cache["data"]["metrics_sampled_at"] == cv._cache["metrics_source"].sampled_at
    assert cv._cache["metrics_source"].deadline == 1901
    assert cv._cache["metrics_source"].identity != old_source.identity
    # Readers holding the previous entry must never see a partially published update.
    assert old_entry["data"] is first
    assert old_entry["metrics_source"] is old_source
    assert old_source.deadline == 1900
    assert first["hosts"][0]["requests_total"] == 40


@pytest.mark.parametrize("failure", ["unreadable", "invalid_mtime", "non_utf8"])
def test_metrics_read_failures_stay_safe_and_independent(tmp_path, monkeypatch, failure):
    _with_both_sources(tmp_path, monkeypatch)
    if failure == "non_utf8":
        (tmp_path / "metrics.prom").write_bytes(b"\xff")
        expected = "Caddy 指标文件解析失败（非 UTF-8 文本）"
    else:
        real_fstat = os.fstat
        metrics_inode = (tmp_path / "metrics.prom").stat().st_ino

        def failing_fstat(fd):
            actual = real_fstat(fd)
            if actual.st_ino != metrics_inode:
                return actual
            if failure == "unreadable":
                raise PermissionError("private path must not leak")
            return type("Stat", (), {"st_mtime": float("inf")})()
        monkeypatch.setattr(cv.os, "fstat", failing_fstat)
        expected = "Caddy 指标文件不可读" if failure == "unreadable" else "Caddy 指标数据不可用"
    snap = cv.get_caddy_snapshot()
    assert snap["available"] is True
    assert snap["metrics_available"] is False
    assert snap["metrics_error"] == expected
    assert snap["metrics_sampled_at"] is None
    assert snap["hosts"] == []


def _upstreams():
    return [
        {"address": "100.64.0.10:8765", "num_requests": 12, "fails": 0},
        {"address": "100.64.0.70:8090", "num_requests": 340, "fails": 2},
        {"address": "10.0.0.9:9999", "num_requests": None, "fails": None},
    ]


def _write_status(tmp_path, payload):
    path = tmp_path / "status.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _assets_file(tmp_path):
    path = tmp_path / "assets.yaml"
    path.write_text(
        "- name: sublinkpro\n"
        "  access:\n"
        "    tailscale: http://100.64.0.10:8765\n"
        "- name: axonhub-oracle\n"
        "  access:\n"
        "    tailscale: http://100.64.0.70:8090\n",
        encoding="utf-8",
    )
    return str(path)


def test_snapshot_happy_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_CADDY_STATUS_FILE", _write_status(tmp_path, _upstreams()))
    monkeypatch.setenv("CONSOLE_CADDY_ASSETS_PATH", _assets_file(tmp_path))
    snap = cv.get_caddy_snapshot()
    assert snap["available"] is True
    assert snap["error"] is None
    assert [u["name"] for u in snap["upstreams"]] == [
        "sublinkpro",
        "axonhub-oracle",
        "upstream-3",
    ]
    assert snap["upstreams"][1]["num_requests"] == 340
    assert snap["upstreams"][1]["fails"] == 2


def test_snapshot_response_never_contains_ip(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_CADDY_STATUS_FILE", _write_status(tmp_path, _upstreams()))
    monkeypatch.setenv("CONSOLE_CADDY_ASSETS_PATH", _assets_file(tmp_path))
    raw = json.dumps(cv.get_caddy_snapshot())
    assert "100.64.0.10" not in raw
    assert "100.64.0.70" not in raw
    assert "10.0.0.9" not in raw
    assert "address" not in raw


def test_snapshot_file_missing_degrades_with_cron_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_CADDY_STATUS_FILE", str(tmp_path / "absent.json"))
    snap = cv.get_caddy_snapshot()
    assert snap["available"] is False
    assert snap["upstreams"] == []
    assert "cron" in snap["error"]


def test_snapshot_broken_json_degrades(tmp_path, monkeypatch):
    status = tmp_path / "status.json"
    status.write_text('{"truncated', encoding='utf-8')
    monkeypatch.setenv("CONSOLE_CADDY_STATUS_FILE", str(status))
    snap = cv.get_caddy_snapshot()
    assert snap["available"] is False
    assert "解析失败" in snap["error"]


def test_snapshot_invalid_structure_degrades(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "CONSOLE_CADDY_STATUS_FILE", _write_status(tmp_path, {"not": "a list"})
    )
    snap = cv.get_caddy_snapshot()
    assert snap["available"] is False
    assert "结构非法" in snap["error"]


def test_ttl_caches_and_force_refreshes(tmp_path, monkeypatch):
    status = tmp_path / "status.json"
    status.write_text(json.dumps(_upstreams()), encoding="utf-8")
    monkeypatch.setenv("CONSOLE_CADDY_STATUS_FILE", str(status))
    first = cv.get_caddy_snapshot()
    status.write_text(json.dumps([]), encoding="utf-8")
    second = cv.get_caddy_snapshot()
    assert first == second  # TTL 内不重读
    forced = cv.get_caddy_snapshot(force=True)
    assert forced["upstreams"] == []


def test_file_source_missing_counts_as_down(monkeypatch):
    monkeypatch.setenv("CONSOLE_CADDY_STATUS_FILE", "/nonexistent/caddy.json")
    snap = cv.get_caddy_snapshot()
    assert snap["available"] is False


def test_peek_returns_none_before_first_sample():
    cv.invalidate()
    assert cv.peek_caddy_snapshot() is None


# ---- 计数口径守护（2026-09-07 复核结论，见模块 docstring「字段语义」）----------
#
# 两个字段都是**瞬时量**：num_requests 是活跃（in-flight）请求数，请求一结束就减回；
# fails 只在 Caddy 配了被动健康检查时才计数。所以「四个上游全 0」是常态而不是故障，
# 下面两条把这个口径钉死——别再有人把 0 当成「采集坏了」而去加降级/告警/补零逻辑。


def test_all_zero_counters_are_a_valid_reading_not_a_failure(tmp_path, monkeypatch):
    """全 0（空闲期采样）必须是 available=True 的正常快照，0 原样出，不许变 None。"""
    payload = [
        {"address": "100.64.0.10:8765", "num_requests": 0, "fails": 0},
        {"address": "100.64.0.70:8090", "num_requests": 0, "fails": 0},
    ]
    monkeypatch.setenv("CONSOLE_CADDY_STATUS_FILE", _write_status(tmp_path, payload))
    monkeypatch.setenv("CONSOLE_CADDY_ASSETS_PATH", _assets_file(tmp_path))
    snap = cv.get_caddy_snapshot()
    assert snap["available"] is True
    assert snap["error"] is None
    assert [u["num_requests"] for u in snap["upstreams"]] == [0, 0]
    assert [u["fails"] for u in snap["upstreams"]] == [0, 0]


def test_counters_are_passed_through_verbatim(tmp_path, monkeypatch):
    """瞬时量只透传：不累加、不与上一次快照做差、不推断速率。"""
    status = tmp_path / "status.json"
    monkeypatch.setenv("CONSOLE_CADDY_STATUS_FILE", str(status))
    monkeypatch.setenv("CONSOLE_CADDY_ASSETS_PATH", _assets_file(tmp_path))

    status.write_text(
        json.dumps([{"address": "100.64.0.10:8765", "num_requests": 8, "fails": 0}]),
        encoding="utf-8",
    )
    assert cv.get_caddy_snapshot(force=True)["upstreams"][0]["num_requests"] == 8

    # 下一次采样回到空闲：读数就是 0，不是「累计仍为 8」也不是负增量
    status.write_text(
        json.dumps([{"address": "100.64.0.10:8765", "num_requests": 0, "fails": 0}]),
        encoding="utf-8",
    )
    assert cv.get_caddy_snapshot(force=True)["upstreams"][0]["num_requests"] == 0


def test_assets_path_container_candidate(monkeypatch, tmp_path):
    """容器路径候选：/app/assets.yaml 存在时优先于 ../assets.yaml（C11 首轮
    部署映射全降级 upstream-N 的根因回归测试——代码平铺在 /app 时
    `here/../assets.yaml` 解析成不存在的 /assets.yaml）。"""
    fake_app = tmp_path / "app"
    fake_app.mkdir()
    (fake_app / "assets.yaml").write_text("- name: x\n", encoding="utf-8")
    monkeypatch.setattr(cv, "__file__", str(fake_app / "caddy_view.py"))
    monkeypatch.setattr(
        cv.os.path, "exists", lambda p: str(p) == "/app/assets.yaml"
    )
    assert cv._assets_path() == "/app/assets.yaml"


def test_assets_path_env_override(monkeypatch, tmp_path):
    target = tmp_path / "custom.yaml"
    target.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("CONSOLE_CADDY_ASSETS_PATH", str(target))
    assert cv._assets_path() == str(target)


# ---------------------------------------------------------------------------
# 累计量（Prometheus 指标，2026-09-07 owner 授权补齐）
# ---------------------------------------------------------------------------

# 逐字节照抄 aly 线上 `curl -s localhost:2019/metrics` 的真实输出形态：
# 科学计数法的 reload 时间戳、同一域名多 handler、code/method 标签顺序都按实测来。
_METRICS_SAMPLE = """caddy_config_last_reload_success_timestamp_seconds 1.788766786460628e+09
caddy_http_requests_total{handler="subroute",host="axonhub.example.test",server="srv0"} 12
caddy_http_requests_total{handler="file_server",host="axonhub.example.test",server="srv0"} 3
caddy_http_requests_total{handler="subroute",host="axonhub2.example.test",server="srv0"} 40
caddy_http_response_duration_seconds_count{code="200",handler="subroute",host="axonhub2.example.test",method="GET",server="srv0"} 38
caddy_http_response_duration_seconds_count{code="502",handler="subroute",host="axonhub2.example.test",method="GET",server="srv0"} 2
caddy_http_response_duration_seconds_count{code="404",handler="subroute",host="axonhub.example.test",method="GET",server="srv0"} 1
"""

_HOST_FIELDS = {
    "host", "requests_total", "ok_2xx", "redirect_3xx", "client_4xx", "server_5xx",
}


def _write_metrics(tmp_path, text=_METRICS_SAMPLE):
    path = tmp_path / "metrics.prom"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _with_both_sources(tmp_path, monkeypatch, metrics=_METRICS_SAMPLE):
    monkeypatch.setenv("CONSOLE_CADDY_STATUS_FILE", _write_status(tmp_path, _upstreams()))
    monkeypatch.setenv("CONSOLE_CADDY_ASSETS_PATH", _assets_file(tmp_path))
    monkeypatch.setenv("CONSOLE_CADDY_METRICS_FILE", _write_metrics(tmp_path, metrics))


def test_metrics_happy_path_aggregates_by_host(tmp_path, monkeypatch):
    _with_both_sources(tmp_path, monkeypatch)
    snap = cv.get_caddy_snapshot()
    assert snap["metrics_available"] is True
    assert snap["metrics_error"] is None
    # 降序：axonhub2 40 > axonhub 15（12 + 3，同域名多 handler 必须求和）
    assert [h["host"] for h in snap["hosts"]] == [
        "axonhub2.example.test",
        "axonhub.example.test",
    ]
    assert snap["hosts"][0]["requests_total"] == 40
    assert snap["hosts"][1]["requests_total"] == 15


def test_metrics_buckets_status_codes_by_first_digit(tmp_path, monkeypatch):
    _with_both_sources(tmp_path, monkeypatch)
    top = cv.get_caddy_snapshot()["hosts"][0]
    assert top["ok_2xx"] == 38
    assert top["server_5xx"] == 2
    assert top["client_4xx"] == 0
    assert top["redirect_3xx"] == 0


def test_metrics_counting_since_comes_from_reload_timestamp(tmp_path, monkeypatch):
    """计数器每次 reload 归零，所以「累计」必须带起点，否则会被当成开天辟地以来的总量。"""
    _with_both_sources(tmp_path, monkeypatch)
    assert cv.get_caddy_snapshot()["counting_since"] == "2026-09-07T07:39:46Z"


def test_metrics_host_rows_have_exactly_the_whitelisted_fields(tmp_path, monkeypatch):
    _with_both_sources(tmp_path, monkeypatch)
    for row in cv.get_caddy_snapshot()["hosts"]:
        assert set(row) == _HOST_FIELDS


def test_metrics_bare_ip_host_folds_into_catchall(tmp_path, monkeypatch):
    """有人直接按 IP 打 Host 头时 host 标签就是裸 IP——零内网 IP 契约的兜底层。"""
    _with_both_sources(
        tmp_path,
        monkeypatch,
        metrics='caddy_http_requests_total{host="100.64.0.10",server="srv0"} 7\n'
                'caddy_http_requests_total{host="192.168.1.9",server="srv0"} 2\n',
    )
    snap = cv.get_caddy_snapshot()
    assert [h["host"] for h in snap["hosts"]] == [cv.CATCHALL_HOST]
    assert snap["hosts"][0]["requests_total"] == 9
    raw = json.dumps(snap, ensure_ascii=False)
    assert "100.64.0.10" not in raw
    assert "192.168.1.9" not in raw


def test_metrics_missing_file_does_not_break_upstream_half(tmp_path, monkeypatch):
    """两条链各自降级：指标文件没了，瞬时量那半边必须照常 available=true。"""
    monkeypatch.setenv("CONSOLE_CADDY_STATUS_FILE", _write_status(tmp_path, _upstreams()))
    monkeypatch.setenv("CONSOLE_CADDY_ASSETS_PATH", _assets_file(tmp_path))
    monkeypatch.setenv("CONSOLE_CADDY_METRICS_FILE", str(tmp_path / "absent.prom"))
    snap = cv.get_caddy_snapshot()
    assert snap["available"] is True
    assert snap["upstreams"] != []
    assert snap["metrics_available"] is False
    assert snap["hosts"] == []
    assert "cron" in snap["metrics_error"]
    assert snap["metrics_sampled_at"] is None


def test_upstream_file_missing_does_not_break_metrics_half(tmp_path, monkeypatch):
    """反方向同理：upstreams 没了，累计量那半边照常可用。"""
    monkeypatch.setenv("CONSOLE_CADDY_STATUS_FILE", str(tmp_path / "absent.json"))
    monkeypatch.setenv("CONSOLE_CADDY_METRICS_FILE", _write_metrics(tmp_path))
    snap = cv.get_caddy_snapshot()
    assert snap["available"] is False
    assert snap["upstreams"] == []
    assert snap["metrics_available"] is True
    assert snap["hosts"] != []


def test_metrics_garbage_lines_are_skipped_not_fatal(tmp_path, monkeypatch):
    _with_both_sources(
        tmp_path,
        monkeypatch,
        metrics="# HELP caddy_http_requests_total Counter of HTTP(S) requests made.\n"
                "# TYPE caddy_http_requests_total counter\n"
                "这行完全不是指标\n"
                "caddy_http_requests_total{host=\"a.example.test\"} NaN\n"
                "caddy_http_requests_total{host=\"b.example.test\",server=\"srv0\"} 5\n"
                "\n",
    )
    snap = cv.get_caddy_snapshot()
    assert snap["metrics_available"] is True
    assert [h["host"] for h in snap["hosts"]] == ["b.example.test"]
    assert snap["hosts"][0]["requests_total"] == 5


def test_metrics_empty_file_is_available_with_no_rows(tmp_path, monkeypatch):
    """空文件 ≠ 坏文件：cron 刚建好还没数据时应显示「暂无」而不是报错。"""
    _with_both_sources(tmp_path, monkeypatch, metrics="")
    snap = cv.get_caddy_snapshot()
    assert snap["metrics_available"] is True
    assert snap["hosts"] == []
    assert snap["counting_since"] is None


def _timed_caddy_sources(tmp_path, monkeypatch, upstream_age=0, metrics_age=0):
    _with_both_sources(tmp_path, monkeypatch)
    clock = {"wall": 1800000000.0, "mono": 1000.0}
    for filename, age in (("status.json", upstream_age), ("metrics.prom", metrics_age)):
        os.utime(tmp_path / filename, (clock["wall"] - age, clock["wall"] - age))
    monkeypatch.setattr(cv.time, "time", lambda: clock["wall"])
    monkeypatch.setattr(cv.time, "monotonic", lambda: clock["mono"])
    return clock


@pytest.mark.parametrize("age,available", [(899, True), (900, True), (901, False), (86400, False)])
def test_upstream_source_age_boundary(tmp_path, monkeypatch, age, available):
    _timed_caddy_sources(tmp_path, monkeypatch, upstream_age=age)
    snap = cv.get_caddy_snapshot()
    assert snap["available"] is available
    assert snap["upstreams_sampled_at"] == datetime.datetime.fromtimestamp(
        (tmp_path / "status.json").stat().st_mtime, datetime.timezone.utc
    ).isoformat().replace("+00:00", "Z")
    assert snap["metrics_available"] is True
    if not available:
        assert snap["upstreams"] == []
        assert "过期" in snap["error"]
        assert entry_view.caddy_upstream_names(snap) is None


@pytest.mark.parametrize("source", ["upstreams", "metrics"])
def test_caddy_cache_and_no_io_peek_expire_sources_independently(tmp_path, monkeypatch, source):
    clock = _timed_caddy_sources(
        tmp_path, monkeypatch,
        upstream_age=899 if source == "upstreams" else 0,
        metrics_age=899 if source == "metrics" else 0,
    )
    first = cv.get_caddy_snapshot()
    flag = "available" if source == "upstreams" else "metrics_available"
    clock["wall"] += 1
    clock["mono"] += 1
    assert cv.peek_caddy_snapshot()[flag] is True
    clock["wall"] += 1
    clock["mono"] += 1

    def no_io(*args, **kwargs):
        raise AssertionError("cache expiry and peek must not read files or config")

    monkeypatch.setattr(cv, "status_file_path", no_io)
    monkeypatch.setattr(cv, "metrics_file_path", no_io)
    monkeypatch.setattr(cv, "_service_index", no_io)
    assert cv.get_caddy_snapshot()[flag] is False
    monkeypatch.setattr(cv, "caddy_interval", no_io)
    expired = cv.peek_caddy_snapshot()
    assert expired[flag] is False
    if source == "upstreams":
        assert expired["metrics_available"] is True
        assert expired["hosts"] == first["hosts"]
        services = [{"name": "sublinkpro"}]
        entry_view.attach_entries(services, expired)
        assert services[0]["entry"]["edge_known"] is False
        assert expired["upstreams"] == []
    else:
        assert expired["available"] is True
        assert expired["upstreams"] == first["upstreams"]
        assert expired["hosts"] == []
        assert expired["counting_since"] is None
    assert first[flag] is True


@pytest.mark.parametrize("force", [False, True])
def test_upstream_atomic_replacement_recovers_by_force_or_ttl(tmp_path, monkeypatch, force):
    clock = _timed_caddy_sources(tmp_path, monkeypatch, upstream_age=901)
    assert cv.get_caddy_snapshot()["available"] is False
    replacement = tmp_path / "new.json"
    replacement.write_text(json.dumps(_upstreams()), encoding="utf-8")
    os.utime(replacement, (clock["wall"], clock["wall"]))
    replacement.replace(tmp_path / "status.json")
    assert cv.get_caddy_snapshot()["available"] is False
    if not force:
        clock["wall"] += 600
        clock["mono"] += 600
    assert cv.get_caddy_snapshot(force=force)["available"] is True


def test_upstream_read_keeps_content_and_mtime_on_same_descriptor(tmp_path, monkeypatch):
    clock = _timed_caddy_sources(tmp_path, monkeypatch, upstream_age=901)
    real_load = json.load

    def replace_after_read(handle):
        parsed = real_load(handle)
        replacement = tmp_path / "new.json"
        replacement.write_text("[]", encoding="utf-8")
        os.utime(replacement, (clock["wall"], clock["wall"]))
        replacement.replace(tmp_path / "status.json")
        return parsed

    monkeypatch.setattr(cv.json, "load", replace_after_read)
    snap = cv.get_caddy_snapshot()
    assert snap["available"] is False
    assert "过期" in snap["error"]
    assert snap["metrics_available"] is True


@pytest.mark.parametrize(
    "text,available",
    [
        ("", True),
        ("  \n# HELP example\n# TYPE example counter\n", True),
        ("caddy_config_last_reload_success_timestamp_seconds 1800000000.125\n", True),
        ("caddy_config_last_reload_success_timestamp_seconds 0\n", True),
        ('caddy_http_requests_total{host="zero.test"} 0\n', True),
        ('caddy_http_response_duration_seconds_count{host="zero.test",code="200"} 0\n', True),
        ("unrelated_metric 9\n", False),
        ("this is garbage\n完全坏掉的文本\n", False),
        ('garbage\ncaddy_http_requests_total{host="ok.test"} 3\n', True),
    ],
)
def test_supported_metrics_validity_matrix(tmp_path, monkeypatch, text, available):
    _with_both_sources(tmp_path, monkeypatch, metrics=text)
    snap = cv.get_caddy_snapshot()
    assert snap["metrics_available"] is available
    assert snap["available"] is True
    if not available:
        assert snap["hosts"] == []
        assert snap["counting_since"] is None
        assert snap["metrics_error"] == "Caddy 指标文件无有效支持样本"
        assert str(tmp_path) not in json.dumps(snap)


@pytest.mark.parametrize(
    "invalid_line",
    [
        'caddy_http_requests_total{host="bad.test"} NaN',
        'caddy_http_requests_total{host="bad.test"} +Inf',
        'caddy_http_requests_total{host="bad.test"} -Inf',
        'caddy_http_requests_total{host="bad.test"} true',
        'caddy_http_requests_total{host="bad.test"} "3"',
        'caddy_http_requests_total{host="bad.test"} -1',
        'caddy_http_requests_total{host="bad.test"} 1.5',
        'caddy_http_requests_total{host=bad.test} 3',
        'caddy_http_response_duration_seconds_count{host="bad.test"} 2',
        'caddy_http_response_duration_seconds_count{host="bad.test",code="2000"} 2',
        'caddy_http_response_duration_seconds_count{host="bad.test",code="2xx"} 2',
        'caddy_config_last_reload_success_timestamp_seconds -1',
        'caddy_config_last_reload_success_timestamp_seconds 1e300',
    ],
)
@pytest.mark.parametrize("with_valid", [False, True])
def test_invalid_supported_sample_does_not_become_valid(tmp_path, monkeypatch, invalid_line, with_valid):
    text = invalid_line + "\n"
    if with_valid:
        text += 'caddy_http_requests_total{host="ok.test"} 7\n'
    _with_both_sources(tmp_path, monkeypatch, metrics=text)
    snap = cv.get_caddy_snapshot()
    assert snap["metrics_available"] is with_valid
    assert snap["available"] is True
    assert [row["host"] for row in snap["hosts"]] == (["ok.test"] if with_valid else [])
    if with_valid:
        assert snap["hosts"][0]["requests_total"] == 7


@pytest.mark.parametrize("invalid_line", [
    "caddy_http_requests_total{,} 7",
    "caddy_http_requests_total{ , } 7",
    "caddy_config_last_reload_success_timestamp_seconds{,} 0",
    "caddy_config_last_reload_success_timestamp_seconds{,} 1700000000",
])
@pytest.mark.parametrize("with_valid", [False, True])
def test_empty_item_label_block_is_not_a_supported_sample(tmp_path, monkeypatch, invalid_line, with_valid):
    text = invalid_line + "\n"
    if with_valid:
        text += 'caddy_http_requests_total{host="valid.test"} 3\n'
    _with_both_sources(tmp_path, monkeypatch, metrics=text)
    snap = cv.get_caddy_snapshot()
    assert snap["metrics_available"] is with_valid
    assert snap["available"] is True
    assert snap["counting_since"] is None
    assert [row["host"] for row in snap["hosts"]] == (["valid.test"] if with_valid else [])
    assert sum(row["requests_total"] for row in snap["hosts"]) == (3 if with_valid else 0)
    if not with_valid:
        assert snap["metrics_error"] == "Caddy 指标文件无有效支持样本"


@pytest.mark.parametrize("labels,host", [
    ("{}", "_other"), ("{ }", "_other"),
    ('{host="valid.test",}', "valid.test"), ('{host="valid.test", }', "valid.test"),
])
def test_empty_or_valid_trailing_comma_label_block_stays_supported(tmp_path, monkeypatch, labels, host):
    _with_both_sources(tmp_path, monkeypatch, metrics=f"caddy_http_requests_total{labels} 3\n")
    snap = cv.get_caddy_snapshot()
    assert snap["metrics_available"] is True
    assert [row["host"] for row in snap["hosts"]] == [host]
    assert snap["hosts"][0]["requests_total"] == 3


@pytest.mark.parametrize("value", [None, True, 3, [], {}, b"caddy_http_requests_total 1"])
def test_metrics_parser_rejects_non_text(value):
    with pytest.raises(ValueError, match="无有效支持样本"):
        cv._host_rows(value)
