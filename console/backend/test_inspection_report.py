# -*- coding: utf-8 -*-
"""C1 巡检报告：双出格式、sampled_at 诚实性、单源故障隔离。"""
import os
import pathlib
import sys
import textwrap
import time

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

os.environ["CONSOLE_AUTH_ENABLED"] = "false"

import app as console_app  # noqa: E402
import inspection_report as ir  # noqa: E402
from plugin_imports import load_module  # noqa: E402

renewal_contributions = load_module("plugins.renewals.backend.contributions")
feed_contributions = load_module("plugins.feed.backend.contributions")



def test_history_remains_exported_without_overdue_count(monkeypatch):
    monkeypatch.setattr(renewal_contributions.rn, "list_renewals", lambda **kwargs: [
        {"id": "old", "name": "ended plan", "kind": "subscription", "status": "ended",
         "expires_at": "2020-01-01", "cost": 12, "note": "keep"},
    ])
    section = renewal_contributions.inspection(None, ir.datetime.datetime.now(ir.datetime.timezone.utc))
    assert section["items"][0]["days_left"] is None
    assert section["items"][0]["note"] == "keep"
    summary = ir._summary({
        "renewals": section, "attention": {}, "machines": {}, "services": {},
    })
    assert summary["renewals_total"] == 1
    assert summary["renewals_expired"] == []
    out = []
    ir._render_renewals({"sections": {"renewals": section}}, out)
    assert "已结束" in "\n".join(out)

# 固定骨架：六个二级标题，顺序不可变
SKELETON = ["## 总评", "## 关注项", "## 机器", "## 服务", "## 续费", "## 运维状态"]

# 缓存写入时刻取一个明显早于「现在」的值，用来证明 sampled_at 不是生成时间
STALE_EPOCH = 1757000000.0  # 2025-09-04T15:33:20Z
STALE_ISO = "2025-09-04T15:33:20Z"


@pytest.fixture
def workspace(tmp_path, monkeypatch, pg_online):
    """一份最小但完整的数据面：assets + 在线库 + 只读缓存。

    PG-05 集成收口：续费段已按决策 D3 只走 PG（`db_path=` 忽略），夹具必须真连 PG，
    否则 `sections.renewals` 每次都降级成 `PgConfigError`。

    `"db"` 是**在线库**标识，PG 模式下就是 `None`（诊断入口据此走 PG 只读事务）。
    以前这里塞 `tmp_path/console.db` 并调 `init_db()` 建库，但 PG 下 `init_db()` 是
    空操作（schema 由部署阶段迁移应用），文件根本不存在 → `readonly_connection`
    走 SQLite 分支报 `unable to open database file`，attention / ops_status 两段
    连带降级。PG 模式下**不存在**在线 SQLite，别再造一个假的。
    """
    assets_path = tmp_path / "assets.yaml"
    assets_path.write_text(
        textwrap.dedent(
            """
            - name: alpha
              type: docker
              machine: nas
              port: 8080
            - name: beta
              type: external
              machine: nas
            """
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(console_app, "ASSETS_PATH", str(assets_path))

    # 只读缓存：alpha 有陈旧样本，beta 从未采过
    monkeypatch.setattr(
    ir.pc,
        "peek_health_detail",
        lambda name: (
            {"ts": STALE_EPOCH, "status": "Unhealthy", "msg": "connection refused"}
            if name == "alpha"
            else None
        ),
    )
    monkeypatch.setattr(ir.pc, "peek_health", lambda name: None)
    monkeypatch.setattr(ir.attention, "_monitoring_items", lambda: [])
    monkeypatch.setattr(ir.mc, "machines_for_api", lambda: [])
    monkeypatch.setattr(ir.mc, "get_machine", lambda machine_id: None)
    # attention 还会读 Beszel 主缓存与 feed 错误态：这两个是进程级全局，
    # 会被同一次 pytest 里跑过的别的用例（及 app.py 的后台线程）污染，显式清零。
    monkeypatch.setattr(feed_contributions.feed_aggregator, "get_last_fetch_errors", lambda: [])
    # 在线库标识：PG 模式下没有在线 SQLite，None 即「走 PG」
    return {"assets": str(assets_path), "db": None}


@pytest.fixture
def client(workspace):
    return console_app.app.test_client()


# ---- 时间归一 ----------------------------------------------------------


class TestTimestampNormalisation:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (STALE_EPOCH, STALE_ISO),
            ("2026-09-06T12:00:00Z", "2026-09-06T12:00:00Z"),
            ("2026-09-06T20:00:00+08:00", "2026-09-06T12:00:00Z"),
            ("2026-09-06 12:00:00", "2026-09-06T12:00:00Z"),  # 裸串按 UTC 解
        ],
    )
    def test_known_shapes_normalise_to_utc(self, value, expected):
        assert ir._fmt(ir._to_utc(value)) == expected

    @pytest.mark.parametrize("value", [None, "", "  ", "not-a-time", True, float("nan")])
    def test_unparseable_is_none(self, value):
        assert ir._to_utc(value) is None

    def test_oldest_wins(self):
        oldest = ir._oldest(["2026-09-06T12:00:00Z", "2026-01-01T00:00:00Z", "junk"])
        assert ir._fmt(oldest) == "2026-01-01T00:00:00Z"

    def test_oldest_all_unknown_is_none(self):
        assert ir._oldest(["junk", None]) is None


# ---- sampled_at 诚实性 -------------------------------------------------


class TestSampledAt:
    def test_every_section_carries_sampled_at(self, workspace):
        report = ir.build_report(workspace["assets"], workspace["db"])
        assert set(report["sections"]) == {"attention", "machines", "services", "renewals", "observability", "ops_status"}
        for name, section in report["sections"].items():
            assert "sampled_at" in section, name

    def test_cached_sections_report_cache_time_not_now(self, workspace):
        report = ir.build_report(workspace["assets"], workspace["db"])
        assert report["sections"]["services"]["sampled_at"] == STALE_ISO
        assert report["sections"]["attention"]["sampled_at"] == STALE_ISO
        assert report["generated_at"] != STALE_ISO

    def test_live_sections_report_generation_time(self, workspace):
        report = ir.build_report(workspace["assets"], workspace["db"])
        assert report["sections"]["renewals"]["sampled_at"] == report["generated_at"]
        assert report["sections"]["ops_status"]["sampled_at"] == report["generated_at"]

    def test_never_sampled_source_is_null_not_now(self, workspace, monkeypatch):
        monkeypatch.setattr(ir.mb, "observability_snapshot", lambda: None)
        section = ir.build_report(workspace["assets"], workspace["db"])["sections"]["observability"]
        assert section["sampled_at"] is None
        assert section["error"] is None
        assert section["available"] is False

    def test_generation_does_not_probe(self, workspace, monkeypatch):
        def boom(*args, **kwargs):
            raise AssertionError("巡检报告不得发起探测")
        monkeypatch.setattr(ir.pc, "check_service_health", boom)
        monkeypatch.setattr(ir.pc, "check_machine_probe", boom)
        monkeypatch.setattr(ir.mb, "observability_snapshot", boom)
        ir.build_report(workspace["assets"], workspace["db"])


# ---- 单源故障隔离 ------------------------------------------------------


class TestSourceFailureIsolation:
    @pytest.mark.parametrize("target,attr,section", [
        (lambda: ir.attention, "collect_attention", "attention"),
        (lambda: ir.mc, "machines_for_api", "machines"),
        (lambda: ir.sm, "load_assets", "services"),
        (lambda: renewal_contributions.rn, "list_renewals", "renewals"),
        (lambda: ir.ops_status, "get_ops_status", "ops_status"),
    ])
    def test_single_source_failure_keeps_report(self, workspace, monkeypatch, target, attr, section):
        def boom(*args, **kwargs):
            raise RuntimeError("数据源炸了")
        monkeypatch.setattr(target(), attr, boom)
        report = ir.build_report(workspace["assets"], workspace["db"])
        assert report["sections"][section]["error"] == ("插件数据暂不可用" if section == "renewals" else "RuntimeError: 数据源炸了")
        assert report["sections"][section]["sampled_at"] is None
        assert section in report["summary"]["sections_degraded"]
        for other, payload in report["sections"].items():
            if other != section:
                assert payload["error"] is None, other
        assert all(head in ir.render_markdown(report) for head in SKELETON)

    def test_shared_monitoring_failure_degrades_attention_and_observability(self, workspace, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("数据源炸了")
        monkeypatch.setattr(ir.attention, "_monitoring_items", boom)
        monkeypatch.setattr(ir.mb, "observability_snapshot", boom)
        report = ir.build_report(workspace["assets"], workspace["db"])
        assert report["summary"]["sections_degraded"] == ["attention", "observability"]
        for independent in ("machines", "services", "renewals", "ops_status"):
            assert report["sections"][independent]["error"] is None, independent
        assert all(head in ir.render_markdown(report) for head in SKELETON)

    def test_error_text_is_single_line_and_truncated(self, workspace, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("行一\\n行二 " + "x" * 500)
        monkeypatch.setattr(renewal_contributions.rn, "list_renewals", boom)
        message = ir.build_report(workspace["assets"], workspace["db"])["sections"]["renewals"]["error"]
        assert "\\n" not in message
        assert len(message) <= 201

    def test_all_sources_down_still_renders(self, workspace, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("全挂")
        for module, attr in ((ir.attention, "collect_attention"), (ir.mc, "machines_for_api"), (ir.sm, "load_assets"), (renewal_contributions.rn, "list_renewals"), (ir.mb, "observability_snapshot"), (ir.ops_status, "get_ops_status")):
            monkeypatch.setattr(module, attr, boom)
        report = ir.build_report(workspace["assets"], workspace["db"])
        assert len(report["summary"]["sections_degraded"]) == 6
        assert all(head in ir.render_markdown(report) for head in SKELETON)


# ---- 总评 --------------------------------------------------------------


class TestSummary:
    def test_unhealthy_service_makes_verdict_actionable(self, workspace):
        summary = ir.build_report(workspace["assets"], workspace["db"])["summary"]
        assert summary["verdict"] == "需处理"
        assert summary["services_unhealthy"] == ["alpha"]

    def test_clean_state_is_normal(self, workspace, monkeypatch):
        monkeypatch.setattr(ir.pc, "peek_health_detail", lambda name: None)
        summary = ir.build_report(workspace["assets"], workspace["db"])["summary"]
        assert summary["verdict"] == "正常"

    def test_degraded_section_downgrades_to_watch(self, workspace, monkeypatch):
        monkeypatch.setattr(ir.pc, "peek_health_detail", lambda name: None)

        def boom(*args, **kwargs):
            raise RuntimeError("挂了")

        monkeypatch.setattr(renewal_contributions.rn, "list_renewals", boom)
        summary = ir.build_report(workspace["assets"], workspace["db"])["summary"]
        assert summary["verdict"] == "留意"


# ---- markdown 渲染 -----------------------------------------------------


class TestMarkdown:
    def test_fixed_skeleton_in_order(self, workspace):
        text = ir.render_markdown(ir.build_report(workspace["assets"], workspace["db"]))
        positions = [text.index(head) for head in SKELETON]
        assert positions == sorted(positions)
        assert text.startswith("# 巡检报告 ")
        assert text.endswith("\n")

    def test_observability_folds_under_attention(self, workspace):
        text = ir.render_markdown(ir.build_report(workspace["assets"], workspace["db"]))
        assert text.index("## 关注项") < text.index("### 监控事件（Beszel）")
        assert text.index("### 监控事件（Beszel）") < text.index("## 机器")

    def test_pipe_in_value_does_not_break_table(self, workspace, monkeypatch):
        monkeypatch.setattr(
            ir.pc,
            "peek_health_detail",
            lambda name: {"ts": STALE_EPOCH, "status": "Unhealthy", "msg": "a | b\nc"},
        )
        text = ir.render_markdown(ir.build_report(workspace["assets"], workspace["db"]))
        assert "a \\| b c" in text

    def test_missing_last_deploy_is_stated_not_faked(self, workspace, monkeypatch):
        monkeypatch.setattr(
            ir.ops_status,
            "get_ops_status",
            lambda db_path=None: {
                "running_commit": None,
                "versions_last_checked": None,
                "feed_last_fetched": None,
                "last_backup_tag": None,
                "last_deploy": None,
            },
        )
        text = ir.render_markdown(ir.build_report(workspace["assets"], workspace["db"]))
        assert "backup/last_deploy.json 不可读" in text


# ---- 端点 --------------------------------------------------------------


class TestEndpoint:
    def test_json_is_default(self, client):
        res = client.get("/api/inspection/report")
        assert res.status_code == 200
        assert res.mimetype == "application/json"
        assert "sections" in res.get_json()

    def test_markdown_format(self, client):
        res = client.get("/api/inspection/report?format=markdown")
        assert res.status_code == 200
        assert res.mimetype == "text/markdown"
        # charset 只能出现一次：mimetype= 传带 charset 的串会被 Flask 再补一次
        assert res.headers["Content-Type"] == "text/markdown; charset=utf-8"
        body = res.get_data(as_text=True)
        assert all(head in body for head in SKELETON)

    def test_bad_format_rejected(self, client):
        res = client.get("/api/inspection/report?format=pdf")
        assert res.status_code == 400
        assert "format" in res.get_json()["error"]

    def test_not_forwarded_to_authority(self, client, monkeypatch):
        def boom(*args, **kwargs):
            raise AssertionError("巡检报告不得走 authority 转发")

        monkeypatch.setattr(console_app, "_proxy_authority", boom)
        assert console_app._is_console_api("/api/inspection/report")
        assert client.get("/api/inspection/report").mimetype == "application/json"

    def test_endpoint_uses_current_app_paths(self, client, workspace, tmp_path, monkeypatch):
        other = tmp_path / "other.yaml"
        other.write_text("- name: gamma\n  type: external\n", encoding="utf-8")
        monkeypatch.setattr(console_app, "ASSETS_PATH", str(other))

        names = [s["name"] for s in client.get("/api/inspection/report").get_json()[
            "sections"]["services"]["items"]]
        assert names == ["gamma"]


# ---- 续费全表导出 ------------------------------------------------------


def test_renewals_exported_in_full(workspace, monkeypatch):
    """C1 的续费段即轻量导出：不做 due_within 裁剪，全表带出并补 days_left。"""
    captured = {}

    def fake_list(due_within=None, db_path=None):
        captured["due_within"] = due_within
        return [
            {"id": "1", "name": "过期域名", "expires_at": "2020-01-01", "auto_renew": 0},
            {"id": "2", "name": "不限时充值", "expires_at": None, "auto_renew": 1},
        ]

    monkeypatch.setattr(renewal_contributions.rn, "list_renewals", fake_list)
    section = ir.build_report(workspace["assets"], workspace["db"])["sections"]["renewals"]

    assert captured["due_within"] is None
    assert [row["name"] for row in section["items"]] == ["过期域名", "不限时充值"]
    assert section["items"][0]["days_left"] < 0
    assert section["items"][1]["days_left"] is None


def test_machine_probe_read_from_history_not_probe(workspace, monkeypatch):
    """机器探活取 probe_history 最后一条真实样本，不调 check_machine_probe。"""
    monkeypatch.setattr(
        ir.mc,
        "machines_for_api",
        lambda: [
            {
                "id": "nas",
                "label": "NAS",
                "role": "server",
                "provider": "self",
                "region": "cn",
                "probe": {"type": "tcp", "port": 22},
            },
            {
                "id": "mac",
                "label": "Mac",
                "role": "workstation",
                "provider": "self",
                "region": "cn",
                "probe": None,
            },
        ],
    )
    monkeypatch.setattr(ir.mc, "get_machine", lambda mid: {"probe": {"port": 22}} if mid == "nas" else {})
    monkeypatch.setattr(
        ir.node_probe,
        "history",
        lambda machine, limit=1, db_path=None: [
            {"ts": "2026-09-06T10:00:00Z", "status": "down", "latency_ms": None}
        ],
    )
    monkeypatch.setattr(ir.nt, "usage", lambda mid, db_path=None: None)

    section = ir.build_report(workspace["assets"], workspace["db"])["sections"]["machines"]
    by_id = {row["id"]: row for row in section["items"]}

    assert by_id["nas"]["probe_status"] == "down"
    assert by_id["nas"]["probe_ts"] == "2026-09-06T10:00:00Z"
    # 没配 probe 的机器不查库，也不伪造 unknown
    assert by_id["mac"]["probe_status"] is None
    assert section["sampled_at"] == "2026-09-06T10:00:00Z"


def test_recent_events_truncated(workspace, monkeypatch):
    monkeypatch.setattr(
        ir.mb,
        "observability_snapshot",
        lambda: {
            "ok": True,
            "updated_at": "2026-09-06T11:00:00Z",
            "source_errors": [],
            "active_alerts": [],
            "recent_events": [{"machine": "nas", "name": f"e{i}"} for i in range(50)],
        },
    )
    section = ir.build_report(workspace["assets"], workspace["db"])["sections"][
        "observability"
    ]
    assert len(section["recent_events"]) == ir._MAX_RECENT_EVENTS
    assert section["events_truncated"] is True
    assert section["sampled_at"] == "2026-09-06T11:00:00Z"


def test_build_report_is_fast_enough_to_be_synchronous(workspace):
    """只读聚合不该慢到需要异步化；超时说明混进了真探测。"""
    started = time.perf_counter()
    ir.build_report(workspace["assets"], workspace["db"])
    assert time.perf_counter() - started < 5
