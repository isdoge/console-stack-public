# -*- coding: utf-8 -*-
"""effective_config（C10-a）：来源三态、敏感遮蔽边界、探针自监控 warn 边界。"""
import inspect
import json
import os
import pathlib
import sqlite3
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import app as console_app  # noqa: E402
import attention  # noqa: E402
import db as dbmod  # noqa: E402
import effective_config as ec  # noqa: E402
import feed_aggregator  # noqa: E402
import hindsight_proxy  # noqa: E402
import machine_config as mc  # noqa: E402
import paths  # noqa: E402
import probe_cache  # noqa: E402
import version_service  # noqa: E402

# ---- 路由级（app.py 集成）：新路由不被 Beszel 代理接管 ------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(console_app, "DB_PATH", str(tmp_path / "console.db"))
    console_app.init_db()
    return console_app.app.test_client()


def test_endpoint_returns_json_and_matches_shape(client, monkeypatch):
    monkeypatch.setattr(
        ec, "PROBE_STATE", {"boot_utc": "2026-09-06T00:00:00Z", "last_run_utc": None}
    )
    res = client.get("/api/config/effective")
    assert res.status_code == 200
    assert res.mimetype == "application/json"
    payload = res.get_json()
    assert set(payload) == {
        "generated_at",
        "schema_version",
        "storage_engine",  # PG-04：提案 §6.4 存储引擎/连接状态非敏感字段
        "database",
        "running_commit",
        "last_probe_run",
        "machines",
        "groups",
    }
    # init_db 跑完全部迁移 → DB schema_migrations MAX(version) = MIGRATIONS 末项。
    # 从 MIGRATIONS 推导而非硬编码：迁移号以后只以 db.py 为准（2026-09-06 纪律），
    # v9（dashboard_layout）起不再每次加迁移都改这里。
    assert payload["schema_version"] == dbmod.MIGRATIONS[-1][0]


def test_effective_config_is_json_and_retired_hub_proxy_is_gone(client):
    """有效配置保持 JSON，Hub 代理不再接管未知 API 或静态资源。"""
    assert console_app._is_console_api("/api/config/effective")
    assert client.get("/api/config/effective").mimetype == "application/json"
    assert client.get("/api/definitely-unregistered").status_code == 404
    assert not hasattr(console_app, "BESZEL_ORIGIN")


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """隔离 env / machines.yaml / 探针状态，防测试互相污染。"""
    monkeypatch.chdir(tmp_path)
    mc._cache.update({"path": None, "mtime": None, "machines": None})
    fake_backend = tmp_path / "backend"
    fake_backend.mkdir()
    monkeypatch.setattr(mc, "machines_path", lambda: str(tmp_path / "machines.yaml"))
    monkeypatch.setattr(ec, "PROBE_STATE", {"boot_utc": "2026-09-06T00:00:00Z", "last_run_utc": None})
    monkeypatch.setenv("CONSOLE_MACHINE_PROBE_INTERVAL", "1800")
    yield tmp_path
    mc._cache.update({"path": None, "mtime": None, "machines": None})


@pytest.fixture
def empty_db(tmp_path):
    db_path = tmp_path / "console.db"
    sqlite3.connect(db_path).close()
    return str(db_path)


def _clear_env(monkeypatch, keep=()):
    for name in list(os.environ):
        if name in keep:
            continue
        monkeypatch.delenv(name, raising=False)


# ---- 遮蔽判定（含大小写边界） -------------------------------------------


@pytest.mark.parametrize(
    "key,expected",
    [
        ("GITHUB_TOKEN", True),
        ("BESZEL_ADMIN_PASSWORD", True),
        ("CONSOLE_AUTH_PASSWORD", True),
        ("api_key", True),
        ("SECRET_KEY", True),
        ("MyToken", True),
        ("tokEn", True),
        ("CONSOLE_SSH_KEY_DIR", True),  # 含 key 子串
        ("CONSOLE_AUTH_ENABLED", False),
        ("RSS_URLS", False),
        ("CONSOLE_HOME_LAN_IP", False),
        ("CONSOLE_ALLOW_DOCKER_EXEC_PROBES", False),
        ("", False),
    ],
)
def test_is_sensitive_key_case_insensitive(key, expected):
    assert ec.is_sensitive_key(key) is expected


def test_mask_value_only_masks_sensitive():
    assert ec.mask_value("GITHUB_TOKEN", "ghp_abc123") == "***"
    assert ec.mask_value("CONSOLE_PROBE_INTERVAL", "600") == "600"


def test_home_machine_tailscale_env_is_masked(isolated, empty_db, monkeypatch):
    """MACHINE_TAILSCALE_MAC/WIN 不含 key 子串，但属内网地址 → sensitive=True 显式遮蔽。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("MACHINE_TAILSCALE_MAC", "100.64.0.2")
    result = ec.get_effective_config(db_path=empty_db)
    ssh = {e["key"]: e for e in result["groups"]["ssh"]["entries"]}
    assert ssh["MACHINE_TAILSCALE_MAC"]["value"] == "***"
    assert ssh["MACHINE_TAILSCALE_MAC"]["source"] == "env"
    assert ssh["MACHINE_TAILSCALE_WIN"]["source"] == "default"


def test_home_machine_tailscale_default_is_plain(isolated, empty_db, monkeypatch):
    """未设时走 default 来源，值为空串而非 ***（空串本就无敏感信息）。"""
    _clear_env(monkeypatch)
    result = ec.get_effective_config(db_path=empty_db)
    ssh = {e["key"]: e for e in result["groups"]["ssh"]["entries"]}
    assert ssh["MACHINE_TAILSCALE_MAC"]["source"] == "default"
    assert ssh["MACHINE_TAILSCALE_MAC"]["value"] == ""


# ---- 来源三态（env > yaml > default） -----------------------------------


def test_env_entries_fall_back_to_default(isolated, empty_db, monkeypatch):
    _clear_env(monkeypatch)
    result = ec.get_effective_config(db_path=empty_db)
    entries = {e["key"]: e for e in result["groups"]["runtime"]["entries"]}
    assert entries["FLASK_ENV"] == {"key": "FLASK_ENV", "value": "production", "source": "default"}
    assert entries["PORT"]["source"] == "default"
    assert entries["PORT"]["value"] == "8080"


def test_env_entries_mark_env_source_and_value(isolated, empty_db, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("PORT", "9090")
    result = ec.get_effective_config(db_path=empty_db)
    entries = {e["key"]: e for e in result["groups"]["runtime"]["entries"]}
    assert entries["FLASK_ENV"]["source"] == "env"
    assert entries["FLASK_ENV"]["value"] == "development"
    assert entries["PORT"]["value"] == "9090"


def test_sensitive_env_values_are_masked(isolated, empty_db, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecret")
    monkeypatch.setenv("BESZEL_ADMIN_PASSWORD", "hunter2")
    monkeypatch.setenv("BESZEL_ADMIN_EMAIL", "ops@console.local")
    result = ec.get_effective_config(db_path=empty_db)
    ws = {e["key"]: e for e in result["groups"]["workstation"]["entries"]}
    assert ws["GITHUB_TOKEN"]["value"] == "***"
    assert ws["GITHUB_TOKEN"]["source"] == "env"
    assert "beszel" not in result["groups"]  # 退役 Hub 凭据不再展示




# ---- P2 收敛①：默认值单一来源（评审 P2：字面量双写） ---------------------


def _default_entries(empty_db, monkeypatch):
    """清空 env 后 get_effective_config 的 default 来源条目，key → entry。"""
    _clear_env(monkeypatch)
    result = ec.get_effective_config(db_path=empty_db)
    entries = {}
    for group in result["groups"].values():
        for entry in group["entries"]:
            if entry["source"] == "default":
                entries[entry["key"]] = entry
    return entries


def test_defaults_sourced_from_reading_modules(isolated, empty_db, monkeypatch):
    """有模块常量 / 读取函数的默认值与读取点一致（本模块不留副本）。"""
    entries = _default_entries(empty_db, monkeypatch)
    assert entries["CONSOLE_HOME_LAN_IP"]["value"] == mc.HOME_LAN_IP
    assert entries["CONSOLE_SSH_KEY_DIR"]["value"] == mc.SSH_KEY_DIR
    assert entries["GITHUB_TOKEN"]["value"] == version_service.GITHUB_TOKEN
    assert entries["CONSOLE_PROBE_INTERVAL"]["value"] == str(probe_cache.probe_interval())
    assert entries["CONSOLE_MACHINE_PROBE_INTERVAL"]["value"] == str(
        probe_cache.machine_probe_interval()
    )
    assert entries["CONSOLE_PUBLIC_PROBE_INTERVAL"]["value"] == str(
        probe_cache.public_probe_interval()
    )
    assert entries["HINDSIGHT_UPSTREAM_PORT"]["value"] == str(hindsight_proxy.UPSTREAM_PORT)
    assert entries["HINDSIGHT_PROXY_PORT"]["value"] == str(hindsight_proxy.LISTEN_PORT)


def test_default_literal_copies_removed(isolated, empty_db, monkeypatch):
    """双写消除断言：可单一来源的字面量不允许再出现在本模块源码里。"""
    source = inspect.getsource(ec)
    for literal in (
        "192.168.50.100",  # CONSOLE_HOME_LAN_IP → machine_config.HOME_LAN_IP
        "/root/.ssh",  # CONSOLE_SSH_KEY_DIR → machine_config.SSH_KEY_DIR
        "admin@console.local",  # BESZEL_ADMIN_EMAIL 读取点回退是空串
        "http://100.64.0.10",  # HINDSIGHT_ORIGIN 默认即 tailnet IP（收敛③绝不回显）
    ):
        assert literal not in source, literal


@pytest.mark.parametrize(
    "module,expr",
    [
        ("app", 'environ.get("PORT", "8080")'),
        ("app", 'environ.get("FLASK_ENV", "production")'),
        ("app", 'environ.get("CONSOLE_PUBLIC_URL", "")'),
        ("auth", 'environ.get("CONSOLE_AUTH_ENABLED", "false")'),
        ("auth", 'environ.get("CONSOLE_AUTH_USER", "")'),
        ("auth", 'environ.get("CONSOLE_AUTH_PASSWORD", "")'),
        ("auth", 'environ.get("CONSOLE_CORS_ORIGINS", "")'),
        ("feed_aggregator", 'environ.get("RSS_URLS", "")'),
        ("feed_aggregator", 'environ.get("RAINDROP_TOKEN", "")'),
        ("version_service", 'environ.get("CONSOLE_ALLOW_DOCKER_EXEC_PROBES", "")'),
    ],
)
def test_inline_defaults_reading_point_keeps_literal(module, expr):
    """读取点为函数内联字面量的字段：读取点表达式仍在（改默认须同步有效配置页）。"""
    module_obj = __import__(module)
    assert expr in inspect.getsource(module_obj), (module, expr)


@pytest.mark.parametrize(
    "name,expected_default",
    [
        ("PORT", "8080"),
        ("FLASK_ENV", "production"),
        ("CONSOLE_PUBLIC_URL", ""),
        ("CONSOLE_AUTH_ENABLED", "false"),
        ("CONSOLE_AUTH_USER", ""),
        ("CONSOLE_AUTH_PASSWORD", ""),
        ("CONSOLE_CORS_ORIGINS", ""),
        ("RAINDROP_TOKEN", ""),
        ("RSS_URLS", ""),
        ("CONSOLE_ALLOW_DOCKER_EXEC_PROBES", ""),
    ],
)
def test_inline_defaults_displayed_match_reading_point(
    isolated, empty_db, monkeypatch, name, expected_default
):
    """内联读取点字段的展示默认与读取点回退值逐项比对，任一侧漂移即红。"""
    entries = _default_entries(empty_db, monkeypatch)
    assert entries[name]["value"] == expected_default


def test_feed_aggregator_github_token_default_is_empty(isolated, monkeypatch):
    """GITHUB_TOKEN 展示默认取 version_service 常量；feed_aggregator 内联回退同为空串。"""
    assert 'environ.get("GITHUB_TOKEN", "")' in inspect.getsource(feed_aggregator)
    assert 'environ.get("GITHUB_TOKEN", "")' in inspect.getsource(version_service)


def test_ops_backup_dir_default_matches_fallback(isolated, empty_db, monkeypatch):
    """OPS_BACKUP_DIR 副本与 ops_status 候选链语义等价（相对 BACKEND_DIR 同一目录）。"""
    import ops_status as ops

    entries = _default_entries(empty_db, monkeypatch)
    displayed = entries["OPS_BACKUP_DIR"]["value"]
    assert displayed == "../../backup"
    resolved = os.path.normpath(os.path.join(paths.BACKEND_DIR, displayed))
    fallback = os.path.normpath(os.path.join(paths.BACKEND_DIR, "..", "..", "backup"))
    assert resolved == fallback
    assert 'os.path.join(paths.BACKEND_DIR, "..", "..", "backup")' in inspect.getsource(ops)


# ---- machines 段（yaml 来源 + env 覆盖行） -------------------------------


@pytest.fixture
def two_machines_yaml(tmp_path):
    path = tmp_path / "machines.yaml"
    path.write_text(
        """
- id: nas
  label: NAS
  role: server
  tailscale: "100.64.0.10"
  ssh: ""
  traffic_quota_gb: 300
- id: us
  label: Oracle
  role: node
  public_host: example.com
  tailscale: "100.99.0.1"
  ssh: ubuntu@example.com
""",
        encoding="utf-8",
    )
    return str(path)


def test_machines_section_yaml_source(isolated, empty_db, monkeypatch, two_machines_yaml):
    monkeypatch.chdir(isolated)
    (isolated / "machines.yaml").write_text(
        pathlib.Path(two_machines_yaml).read_text(encoding="utf-8"), encoding="utf-8"
    )
    _clear_env(monkeypatch)
    mc._cache.update({"path": None, "mtime": None, "machines": None})
    result = ec.get_effective_config(db_path=empty_db)
    by_key = {row["key"]: row for row in result["machines"]}
    # 台账 tailscale 行只暴露「来源是 yaml」，值恒遮蔽（内网地址不经 API 外泄；
    # 2026-09-06 集成收口，此前这里断言的是明文 IP）
    assert by_key["machines.yaml → nas[].tailscale"]["value"] == "***"
    assert by_key["machines.yaml → nas[].tailscale"]["source"] == "yaml"
    assert "100.64.0.10" not in json.dumps(result)
    assert by_key["machines.yaml → nas[].traffic_quota_gb"]["value"] == 300
    # ssh 不在 machines_for_api 投影里：无 env 覆盖时不该出现
    assert "machines.yaml → us[].ssh" not in by_key


def test_machine_tailscale_env_override_masked(isolated, empty_db, monkeypatch, two_machines_yaml):
    monkeypatch.chdir(isolated)
    (isolated / "machines.yaml").write_text(
        pathlib.Path(two_machines_yaml).read_text(encoding="utf-8"), encoding="utf-8"
    )
    _clear_env(monkeypatch)
    monkeypatch.setenv("MACHINE_TAILSCALE_NAS", "100.64.0.99")
    mc._cache.update({"path": None, "mtime": None, "machines": None})
    result = ec.get_effective_config(db_path=empty_db)
    by_key = {row["key"]: row for row in result["machines"]}
    # env 覆盖行出现在 machines 段，值为遮蔽后的 ***（不泄露覆盖 IP）
    assert "MACHINE_TAILSCALE_NAS" in by_key
    assert by_key["MACHINE_TAILSCALE_NAS"]["value"] == "***"
    assert by_key["MACHINE_TAILSCALE_NAS"]["source"] == "env"


def test_machine_ssh_env_override_masked(isolated, empty_db, monkeypatch, two_machines_yaml):
    monkeypatch.chdir(isolated)
    (isolated / "machines.yaml").write_text(
        pathlib.Path(two_machines_yaml).read_text(encoding="utf-8"), encoding="utf-8"
    )
    _clear_env(monkeypatch)
    monkeypatch.setenv("MACHINE_SSH_US", "root@evil.example.com")
    mc._cache.update({"path": None, "mtime": None, "machines": None})
    result = ec.get_effective_config(db_path=empty_db)
    by_key = {row["key"]: row for row in result["machines"]}
    assert by_key["MACHINE_SSH_US"]["value"] == "***"
    assert by_key["MACHINE_SSH_US"]["source"] == "env"


# ---- 附加键 --------------------------------------------------------------


def test_attachment_keys_present_and_sane(isolated, empty_db, monkeypatch):
    _clear_env(monkeypatch)
    # running_commit 复用 ops_status：env 没注入时回退读仓库 .git——主检出的 .git 是目录
    # 会读到 sha，worktree 的 .git 是文件则为 None。用例不该随检出形态变色，显式隔离；
    # 真实取值逻辑由 test_ops_status 覆盖。
    monkeypatch.setattr(ec.ops_status, "_running_commit", lambda repo_dir=None: None)
    result = ec.get_effective_config(db_path=empty_db)
    assert isinstance(result["generated_at"], str)
    assert result["generated_at"].endswith("Z")
    # empty_db 是无表的裸库：DB 已应用迁移版本拿不到 → None（不猜、不回退代码版本）
    assert result["schema_version"] is None
    assert result["running_commit"] is None
    assert result["last_probe_run"] is None
    assert isinstance(result["groups"], dict)
    assert isinstance(result["machines"], list)


# ---- P2 收敛②：schema_version = DB 已应用迁移版本 ------------------------


def _seed_migrations(db_path, versions):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT '', description TEXT DEFAULT '')"
    )
    for version in versions:
        conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
    conn.commit()
    conn.close()


def test_schema_version_reads_applied_max_from_db(isolated, tmp_path, monkeypatch):
    """口径：显示 DB schema_migrations 的 MAX(version)，不是代码 MIGRATIONS 末项。"""
    import db as dbmod

    db_path = str(tmp_path / "console.db")
    _seed_migrations(db_path, [1, 3, 5])
    _clear_env(monkeypatch)
    result = ec.get_effective_config(db_path=db_path)
    assert result["schema_version"] == 5
    code_max = max(version for version, _d, _s in dbmod.MIGRATIONS)
    assert code_max != 5  # 前提：代码领先 seeded DB——显示值必须是 5（DB 侧）
    assert result["schema_version"] != code_max


def test_schema_version_missing_table_is_none(isolated, empty_db, monkeypatch):
    """无 schema_migrations 表（未跑过迁移的库）→ None，不建表不猜值。"""
    _clear_env(monkeypatch)
    assert ec.get_effective_config(db_path=empty_db)["schema_version"] is None


def test_schema_version_missing_db_file_is_none(isolated, tmp_path, monkeypatch):
    """库文件不存在 → None，且只读打开自身不创建文件（零写路径）。

    单元级直测 _applied_schema_version：get_effective_config 里排在前面的
    ops_status.get_ops_status 用 db.get_connection（可写打开）会先建出空文件，
    那是既有行为，不是本函数的副作用。
    """
    _clear_env(monkeypatch)
    missing = str(tmp_path / "nope.db")
    assert ec._applied_schema_version(missing) is None
    assert not os.path.exists(missing)


# ---- P2 收敛③：HINDSIGHT_ORIGIN 内网地址不外泄（e329e27 纪律） ------------


def test_hindsight_origin_env_masked_keep_port(isolated, empty_db, monkeypatch):
    """env 已配置：主机段遮蔽、保尾部端口，真实 IP 不进响应任何角落。

    全响应断言：machines 段的台账 tailscale 行已随 2026-09-06 集成一并遮蔽，
    `100.` 前缀 IP 在 /api/config/effective 里应零命中。
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv("HINDSIGHT_ORIGIN", "http://100.64.0.10:9999")
    result = ec.get_effective_config(db_path=empty_db)
    ops = {e["key"]: e for e in result["groups"]["ops"]["entries"]}
    assert ops["HINDSIGHT_ORIGIN"] == {
        "key": "HINDSIGHT_ORIGIN",
        "value": "http://***:9999",
        "source": "env",
    }
    assert "100.64.0.10" not in json.dumps(result)


def test_hindsight_origin_unset_shows_placeholder(isolated, empty_db, monkeypatch):
    """未配置：显示「未配置」，绝不回显默认字面量里的 tailnet IP。"""
    _clear_env(monkeypatch)
    result = ec.get_effective_config(db_path=empty_db)
    ops = {e["key"]: e for e in result["groups"]["ops"]["entries"]}
    assert ops["HINDSIGHT_ORIGIN"] == {
        "key": "HINDSIGHT_ORIGIN",
        "value": "未配置",
        "source": "default",
    }
    assert hindsight_proxy.DEFAULT_UPSTREAM not in json.dumps(result)


def test_hindsight_origin_set_but_blank_falls_to_placeholder(isolated, empty_db, monkeypatch):
    """env 设了但为空白：实际生效走 DEFAULT_UPSTREAM（upstream_origin 同语义）→ 未配置。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("HINDSIGHT_ORIGIN", "   ")
    result = ec.get_effective_config(db_path=empty_db)
    ops = {e["key"]: e for e in result["groups"]["ops"]["entries"]}
    assert ops["HINDSIGHT_ORIGIN"]["source"] == "default"
    assert hindsight_proxy.DEFAULT_UPSTREAM not in json.dumps(result)


@pytest.mark.parametrize(
    "raw,masked",
    [
        ("http://100.64.0.10:9999", "http://***:9999"),
        ("https://host.tailnet-name.ts.net:8443/", "https://***:8443"),
        ("100.64.0.10:9999", "***:9999"),
        ("http://plainhost", "http://***"),
        ("  http://100.64.0.10:9999  ", "http://***:9999"),
        ("", "***"),
    ],
)
def test_mask_host_keep_port(raw, masked):
    assert ec._mask_host_keep_port(raw) == masked


# ---- 探针自监控 warn 边界（评审新建议 D） ---------------------------------


def _monitor_items():
    """只取 probe-monitor 条目，隔离其它来源。"""
    return [i for i in attention._probe_monitor_items() if i["kind"] == "probe-monitor"]


def _advance(monkeypatch, delta_seconds):
    """把共享状态的 boot / last_run 提前 delta 秒（模拟时间流逝）。

    基准取真实 now 往前推（不是 fixture 里的固定日期），否则「偏移量」
    会被固定日期与真实时钟的差吃掉。
    """
    from datetime import datetime, timedelta, timezone

    shifted = (datetime.now(timezone.utc) - timedelta(seconds=delta_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    monkeypatch.setattr(ec, "PROBE_STATE", {"boot_utc": shifted, "last_run_utc": None})
    return shifted


def test_warn_after_two_intervals_since_boot(isolated, monkeypatch):
    """从进程启动算起超过两周期仍未跑 → warn（用 boot 作基准）。"""
    _clear_env(monkeypatch)
    _advance(monkeypatch, 2 * 1800 + 1)
    items = _monitor_items()
    assert len(items) == 1
    assert items[0]["severity"] == "warn"
    assert "尚未运行" in items[0]["detail"]


def test_quiet_within_two_intervals_since_boot(isolated, monkeypatch):
    """进程刚起、线程在等首个半点节点 → 静默（覆盖既有无探针测试的默认态）。"""
    _clear_env(monkeypatch)
    _advance(monkeypatch, 2 * 1800 - 10)
    assert _monitor_items() == []


def test_warn_when_last_run_older_than_two_intervals(isolated, monkeypatch):
    """上次运行已超两周期 → warn；「上次运行」进 detail。"""
    _clear_env(monkeypatch)
    from datetime import datetime, timedelta, timezone

    stale = (datetime.now(timezone.utc) - timedelta(seconds=2 * 1800 + 5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    ec.PROBE_STATE["last_run_utc"] = stale
    items = _monitor_items()
    assert len(items) == 1
    assert "上次探针运行于" in items[0]["detail"]


def test_quiet_when_last_run_recent(isolated, monkeypatch):
    """守护线程正常运转 → 静默。"""
    _clear_env(monkeypatch)
    ec.mark_probe_run()
    assert _monitor_items() == []
    assert ec.last_probe_run_utc() is not None


def test_mark_probe_run_updates_state(isolated, monkeypatch):
    """显式时间戳原样回存，且基准跟着最近一次运行走（不用 boot）。

    时间戳必须由真实 now 派生——写死同日字面量的话，测试只在那个时刻起两周期
    内为绿，之后每次跑都红（`_advance` 的 docstring 已提醒过同一件事）。
    """
    from datetime import datetime, timezone

    _clear_env(monkeypatch)
    # boot 已过两周期：基准若仍取 boot 就会 warn，取 last_run 才静默
    _advance(monkeypatch, 2 * 1800 + 1)
    recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ec.mark_probe_run(recent)
    assert ec.last_probe_run_utc() == recent
    assert ec.probe_run_reference_utc() == recent
    # 更新后再超时，基准跟着走（用最近一次运行判断，不用 boot）
    assert _monitor_items() == []


def test_ops_status_includes_last_probe_run(isolated, empty_db, monkeypatch):
    """第 6 键与 effective_config 共享同一内存状态。"""
    _clear_env(monkeypatch)
    import ops_status

    assert ops_status.get_ops_status(db_path=empty_db)["last_probe_run"] is None
    ec.mark_probe_run("2026-09-06T13:00:00Z")
    assert (
        ops_status.get_ops_status(db_path=empty_db)["last_probe_run"]
        == "2026-09-06T13:00:00Z"
    )
