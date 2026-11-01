# -*- coding: utf-8 -*-
"""settings_store（C10-b）：白名单写回、永不可写硬拒、env 锁定、数据管理。

安全侧的四条底线各有独立用例，改动本模块时它们必须一起绿：
① secrets / 安全键永远 403（含手滑加进白名单的情况）；② 白名单外 400；
③ `.env` 已设的键 UI 只读（409）；④ 手工往 settings.yaml 塞危险键不会生效。
"""
import datetime
import pathlib
import sys

import pytest
import yaml

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import app as console_app  # noqa: E402
import probe_cache  # noqa: E402
import settings_store as ss  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    """隔离 settings.yaml 与 env：每例一份干净文件 + 干净原生 env 快照。"""
    path = tmp_path / "settings.yaml"
    monkeypatch.setenv("CONSOLE_SETTINGS_FILE", str(path))
    for key in ss.WRITABLE_SPEC:
        monkeypatch.delenv(key, raising=False)
    ss._injected_keys.clear()
    ss.refresh_native_env_snapshot()
    yield path
    ss._injected_keys.clear()


@pytest.fixture
def client(tmp_path, monkeypatch, store):
    # 显式设 test 角色：防止被前面直接赋值 os.environ 的测试污染成 client，
    # 导致 PUT 请求被 forward_observation_to_authority 转发而非本地执行（503）。
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "test")
    monkeypatch.setattr(console_app, "DB_PATH", str(tmp_path / "console.db"))
    console_app.init_db()
    return console_app.app.test_client()


def _stored(path):
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ---- 白名单与永不可写 ----------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "CONSOLE_AUTH_ENABLED",
        "CONSOLE_AUTH_USER",
        "CONSOLE_AUTH_PASSWORD",
        "CONSOLE_CORS_ORIGINS",
        "CONSOLE_ALLOW_DOCKER_EXEC_PROBES",
        "MACHINE_SSH_NAS",
        "MACHINE_TAILSCALE_MAC",
        "SECRET_KEY",
        "CONSOLE_OPLOG_TOKEN",
        "GITHUB_TOKEN",
        "RAINDROP_TOKEN",
        "BESZEL_ADMIN_PASSWORD",
    ],
)
def test_never_writable_keys_are_rejected_with_403(store, key):
    with pytest.raises(ss.SettingsError) as excinfo:
        ss.update_settings({key: "x"})
    assert excinfo.value.status == 403
    assert not store.exists() or _stored(store) == {}


def test_sensitive_key_stays_403_even_if_someone_whitelists_it(store, monkeypatch):
    """永不可写判定在白名单之前：手滑把 secret 加进白名单也拒。"""
    monkeypatch.setitem(
        ss.WRITABLE_SPEC,
        "SOME_API_TOKEN",
        {
            "group": "general",
            "title": "手滑加进来的 token",
            "description": "",
            "type": "int",
            "min": 0,
            "max": 1,
            "unit": "",
            "default": "0",
            "requires_restart": False,
        },
    )
    with pytest.raises(ss.SettingsError) as excinfo:
        ss.update_settings({"SOME_API_TOKEN": 1})
    assert excinfo.value.status == 403


def test_unknown_key_rejected_with_400(store):
    with pytest.raises(ss.SettingsError) as excinfo:
        ss.update_settings({"CONSOLE_SOMETHING_NEW": "1"})
    assert excinfo.value.status == 400
    assert "白名单" in str(excinfo.value)


def test_env_locked_key_rejected_with_409(store, monkeypatch):
    monkeypatch.setenv("CONSOLE_PROBE_INTERVAL", "600")
    ss.refresh_native_env_snapshot()
    with pytest.raises(ss.SettingsError) as excinfo:
        ss.update_settings({"CONSOLE_PROBE_INTERVAL": 900})
    assert excinfo.value.status == 409
    assert _stored(store) == {}


# ---- 写回语义 ------------------------------------------------------------


def test_write_persists_and_takes_effect_without_restart(store, monkeypatch):
    """白名单各项都是「每次现读 env」的键 → 保存即生效。"""
    ss.update_settings({"CONSOLE_PROBE_INTERVAL": 900})
    assert _stored(store) == {"CONSOLE_PROBE_INTERVAL": "900"}
    assert probe_cache.probe_interval() == 900


def test_write_is_idempotent(store):
    first = ss.update_settings({"CONSOLE_PROBE_INTERVAL": 900})
    snapshot = _stored(store)
    second = ss.update_settings({"CONSOLE_PROBE_INTERVAL": 900})
    assert first == second == ["CONSOLE_PROBE_INTERVAL"]
    assert _stored(store) == snapshot


def test_partial_update_keeps_other_keys(store):
    ss.update_settings({"CONSOLE_PROBE_INTERVAL": 900})
    ss.update_settings({"CONSOLE_MACHINE_PROBE_INTERVAL": 3600})
    assert _stored(store) == {
        "CONSOLE_PROBE_INTERVAL": "900",
        "CONSOLE_MACHINE_PROBE_INTERVAL": "3600",
    }


def test_empty_value_restores_default(store):
    ss.update_settings({"CONSOLE_PROBE_INTERVAL": 900})
    ss.update_settings({"CONSOLE_PROBE_INTERVAL": ""})
    assert _stored(store) == {}
    # 注入的 env 也撤掉，读取点回到代码默认
    assert probe_cache.probe_interval() == 600


@pytest.mark.parametrize("bad", ["abc", 59, 90000, 3.5, True, None])
def test_value_validation(store, bad):
    if bad is None:
        # None 是「恢复默认」而不是非法值，单独确认它不写文件
        assert ss.update_settings({"CONSOLE_PROBE_INTERVAL": None}) == [
            "CONSOLE_PROBE_INTERVAL"
        ]
        assert _stored(store) == {}
        return
    with pytest.raises(ValueError):
        ss.update_settings({"CONSOLE_PROBE_INTERVAL": bad})
    assert _stored(store) == {}


def test_batch_is_all_or_nothing(store):
    """一批里有一项非法 → 整批不写（不要写一半留个半生效状态）。"""
    with pytest.raises(ss.SettingsError):
        ss.update_settings({"CONSOLE_PROBE_INTERVAL": 900, "GITHUB_TOKEN": "x"})
    assert _stored(store) == {}


# ---- 启动注入（env > settings.yaml > default）-----------------------------


def test_apply_to_env_injects_whitelisted_only(store, monkeypatch):
    import os

    # 断言「settings.yaml 改不动这些键」而不是「环境里没有它们」：
    # 全量并行下别的用例可能正好设过 GITHUB_TOKEN 之类，比对前后值才与外界无关。
    hazards = ("CONSOLE_AUTH_ENABLED", "GITHUB_TOKEN", "CONSOLE_SOMETHING_NEW")
    before = {key: os.environ.get(key) for key in hazards}
    store.write_text(
        yaml.safe_dump(
            {
                "CONSOLE_PROBE_INTERVAL": "900",
                # 手工塞进来的危险键：读得到，但绝不许进 os.environ
                "CONSOLE_AUTH_ENABLED": "true",
                "GITHUB_TOKEN": "ghp_fake",
                "CONSOLE_SOMETHING_NEW": "1",
            }
        ),
        encoding="utf-8",
    )
    applied = ss.apply_to_env()
    assert applied == {"CONSOLE_PROBE_INTERVAL"}
    assert os.environ.get("CONSOLE_PROBE_INTERVAL") == "900"
    assert {key: os.environ.get(key) for key in hazards} == before


def test_env_wins_over_settings_file(store, monkeypatch):
    store.write_text(yaml.safe_dump({"CONSOLE_PROBE_INTERVAL": "900"}), encoding="utf-8")
    monkeypatch.setenv("CONSOLE_PROBE_INTERVAL", "1200")
    ss.refresh_native_env_snapshot()
    assert ss.apply_to_env() == set()
    assert probe_cache.probe_interval() == 1200
    assert ss.source_of("CONSOLE_PROBE_INTERVAL", ss.load_settings()) == "env"


def test_broken_yaml_degrades_to_defaults(store):
    store.write_text("这不是: [合法\n  yaml", encoding="utf-8")
    assert ss.load_settings() == {}
    assert ss.apply_to_env() == set()


# ---- 数据管理 ------------------------------------------------------------


def _insert_op(db_path, started_at):
    import db as dbmod

    conn = dbmod.get_connection(db_path)
    conn.execute(
        "INSERT INTO oplog (host, svc, action, started_at, finished_at, result)"
        " VALUES ('console','test','x',?,?,'Success')",
        (started_at, started_at),
    )
    conn.commit()
    conn.close()


def _oplog_count(db_path):
    import db as dbmod

    conn = dbmod.get_connection(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM oplog").fetchone()[0]
    finally:
        conn.close()


def test_prune_oplog_disabled_by_default(store, tmp_path, monkeypatch):
    monkeypatch.setattr(console_app, "DB_PATH", str(tmp_path / "console.db"))
    console_app.init_db()
    db_path = str(tmp_path / "console.db")
    _insert_op(db_path, "2020-01-01 00:00:00")
    assert ss.prune_oplog(db_path=db_path) == 0
    assert _oplog_count(db_path) == 1


def test_prune_oplog_deletes_only_old_rows(store, tmp_path, monkeypatch):
    monkeypatch.setattr(console_app, "DB_PATH", str(tmp_path / "console.db"))
    console_app.init_db()
    db_path = str(tmp_path / "console.db")
    now = datetime.datetime(2026, 9, 7, 12, 0, 0)
    _insert_op(db_path, "2026-08-01 00:00:00")  # 37 天前 → 删
    _insert_op(db_path, "2026-09-06 00:00:00")  # 1 天前 → 留
    _insert_op(db_path, None)  # 时间未知 → 留（不替用户做决定）
    ss.update_settings({"CONSOLE_OPLOG_RETENTION_DAYS": 30})
    assert ss.prune_oplog(db_path=db_path, now=now) == 1
    assert _oplog_count(db_path) == 2


def test_table_row_counts_lists_known_tables(store, tmp_path, monkeypatch):
    monkeypatch.setattr(console_app, "DB_PATH", str(tmp_path / "console.db"))
    console_app.init_db()
    counts = {row["name"]: row["rows"] for row in ss.table_row_counts(str(tmp_path / "console.db"))}
    assert "oplog" in counts and "renewals" in counts
    assert "sqlite_sequence" not in counts
    assert all(isinstance(v, int) for v in counts.values())


def test_table_row_counts_degrades_on_missing_db(store):
    assert ss.table_row_counts("/nonexistent/console.db") == []


# ---- 路由 ---------------------------------------------------------------


def test_get_returns_view_and_is_not_swallowed_by_beszel_proxy(client, monkeypatch):
    """漏登记 CONSOLE_API_PATHS 时新路由不会 404，而是被代理成 HTML+200。"""

    def boom(*args, **kwargs):
        raise AssertionError("设置写回被 Beszel 代理接管了")

    monkeypatch.setattr(console_app, "_proxy_authority", boom)
    assert console_app._is_console_api("/api/config/settings")
    res = client.get("/api/config/settings")
    assert res.status_code == 200
    assert res.mimetype == "application/json"
    payload = res.get_json()
    assert set(payload) >= {
        "generated_at",
        "settings_file",
        "groups",
        "tables",
        "never_writable",
    }
    assert set(payload["groups"]) == {"general", "data"}


def test_put_writes_logs_oplog_and_returns_fresh_view(client, store, tmp_path):
    res = client.put("/api/config/settings", json={"values": {"CONSOLE_PROBE_INTERVAL": 900}})
    assert res.status_code == 200
    payload = res.get_json()
    assert payload["changed"] == ["CONSOLE_PROBE_INTERVAL"]
    entry = next(
        e for e in payload["groups"]["general"]["entries"] if e["key"] == "CONSOLE_PROBE_INTERVAL"
    )
    assert entry["value"] == "900"
    assert entry["source"] == "settings"
    assert entry["writable"] is True
    # oplog 记了这次写回（host='console', svc='settings'）
    import db as dbmod

    conn = dbmod.get_connection(str(tmp_path / "console.db"))
    try:
        row = conn.execute(
            "SELECT host, svc, action, result FROM oplog ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert (row["host"], row["svc"], row["action"]) == ("console", "settings", "settings_update")
    assert row["result"] == "Success"


@pytest.mark.parametrize(
    "key,status",
    [
        ("CONSOLE_AUTH_ENABLED", 403),
        ("MACHINE_SSH_NAS", 403),
        ("BESZEL_ADMIN_PASSWORD", 403),
        ("CONSOLE_SOMETHING_NEW", 400),
    ],
)
def test_put_rejections_have_distinct_status_codes(client, key, status):
    res = client.put("/api/config/settings", json={"values": {key: "x"}})
    assert res.status_code == status
    assert res.get_json()["error"]


def test_put_env_locked_returns_409(client, monkeypatch):
    monkeypatch.setenv("CONSOLE_MACHINE_PROBE_INTERVAL", "1800")
    ss.refresh_native_env_snapshot()
    res = client.put(
        "/api/config/settings", json={"values": {"CONSOLE_MACHINE_PROBE_INTERVAL": 3600}}
    )
    assert res.status_code == 409


def test_put_bad_value_returns_400_with_range_hint(client):
    res = client.put("/api/config/settings", json={"values": {"CONSOLE_PROBE_INTERVAL": 1}})
    assert res.status_code == 400
    assert "60" in res.get_json()["error"]


def test_response_never_contains_secrets(client, monkeypatch):
    """白名单里根本没有 secrets，响应全文不应出现任何凭据值。"""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_never_appear")
    monkeypatch.setenv("BESZEL_ADMIN_PASSWORD", "pwd_should_never_appear")
    raw = client.get("/api/config/settings").get_data(as_text=True)
    assert "ghp_should_never_appear" not in raw
    assert "pwd_should_never_appear" not in raw


# ---- Track K：事件保留期接进数据管理分区 ----------------------------------


def test_event_retention_is_in_data_group(store):
    """新键落「数据管理」分区，前端 groups.data 遍历渲染 → 零前端改动即可见。"""
    view = ss.build_view()
    keys = [entry["key"] for entry in view["groups"]["data"]["entries"]]
    assert "CONSOLE_EVENT_RETENTION_DAYS" in keys
    assert "CONSOLE_OPLOG_RETENTION_DAYS" in keys


def test_event_retention_writes_and_takes_effect(store):
    import monitor_events

    ss.update_settings({"CONSOLE_EVENT_RETENTION_DAYS": 30})
    assert _stored(store)["CONSOLE_EVENT_RETENTION_DAYS"] == "30"
    assert monitor_events.retention_days() == 30


def test_event_retention_range_is_validated(store):
    with pytest.raises(ValueError):
        ss.update_settings({"CONSOLE_EVENT_RETENTION_DAYS": 99999})
    with pytest.raises(ValueError):
        ss.update_settings({"CONSOLE_EVENT_RETENTION_DAYS": -1})


def test_monitor_events_table_shows_in_row_counts(tmp_path):
    """新表自动进「各表行数」——table_row_counts 遍历 sqlite_master，不用登记。"""
    import db as dbmod

    path = str(tmp_path / "console.db")
    dbmod.run_migrations(path)
    names = {row["name"] for row in ss.table_row_counts(path)}
    assert "monitor_events" in names
