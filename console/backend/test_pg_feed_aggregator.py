# -*- coding: utf-8 -*-
"""feed_aggregator 的 PG 路径回归（PG-02）。

在真 PG16 上复验缓存语义（upsert、去重、按真实时间裁剪、排序），并覆盖采集门禁：
client 只读共享缓存、不抓取、不写库。
"""
import datetime
import pathlib
import sys

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

import feed_aggregator as fa  # noqa: E402
from pg02_support import count_rows, fetch_all  # noqa: E402

# 夹具通过插件注册加载：直接 import 同名夹具会被 pyflakes 判 F811（测试参数同名）。
pytest_plugins = ("pg02_support",)



def _item(ident, source="github", published="2026-08-01T00:00:00Z", url=None):
    return {
        "id": ident,
        "source": source,
        "title": f"title-{ident}",
        "url": url if url is not None else f"https://example.test/{ident}",
        "summary": "",
        "icon": "",
        "published_at": published,
    }


# ---- 缓存写入 ---------------------------------------------------------------


def test_cache_upsert_is_idempotent(pg_authority):
    fa._cache_feed_items([_item("a")])
    fa._cache_feed_items([_item("a")])
    assert count_rows(pg_authority, "feed_cache") == 1


def test_cache_upsert_updates_changed_columns(pg_authority):
    fa._cache_feed_items([_item("a")])
    changed = _item("a")
    changed["title"] = "renamed"
    fa._cache_feed_items([changed])
    rows = fetch_all(pg_authority, "SELECT * FROM feed_cache WHERE id = 'a'")
    assert rows[0]["title"] == "renamed"


def test_same_url_different_id_keeps_single_row(pg_authority):
    """旧随机 ID 遗留的同一逻辑条目被清理。"""
    fa._cache_feed_items([_item("legacy", url="https://example.test/same")])
    fa._cache_feed_items([_item("stable", url="https://example.test/same")])
    rows = fetch_all(pg_authority, "SELECT id FROM feed_cache")
    assert [row["id"] for row in rows] == ["stable"]


def test_same_title_without_url_keeps_single_row(pg_authority):
    """无 URL 的条目按 (source, title, published_at) 去重（同标题才算重复）。"""
    legacy = _item("legacy", url="")
    stable = _item("stable", url="")
    stable["title"] = legacy["title"]
    fa._cache_feed_items([legacy])
    fa._cache_feed_items([stable])
    assert [row["id"] for row in fetch_all(pg_authority, "SELECT id FROM feed_cache")] == [
        "stable"
    ]


def test_clear_sources_drops_only_that_source(pg_authority):
    fa._cache_feed_items([_item("gh", source="github"), _item("rss", source="rss")])
    fa._cache_feed_items([_item("gh2", source="github")], clear_sources=("github",))
    rows = fetch_all(pg_authority, "SELECT id, source FROM feed_cache ORDER BY id")
    assert [(r["id"], r["source"]) for r in rows] == [("gh2", "github"), ("rss", "rss")]


def test_prune_drops_rows_beyond_max(pg_authority, monkeypatch):
    monkeypatch.setattr(fa, "FEED_CACHE_MAX_ROWS", 2)
    fa._cache_feed_items(
        [_item(f"i{i}", published=f"2026-08-0{i + 1}T00:00:00Z") for i in range(4)]
    )
    rows = fetch_all(pg_authority, "SELECT id FROM feed_cache ORDER BY id")
    assert len(rows) == 2
    # 保留的是发布时间最新的两条
    assert [row["id"] for row in rows] == ["i2", "i3"]


def test_prune_keeps_newest_across_mixed_time_formats(pg_authority, monkeypatch):
    """RFC 2822 与 ISO 混排时按真实时间保留最新，而不是字典序。"""
    monkeypatch.setattr(fa, "FEED_CACHE_MAX_ROWS", 2)
    fa._cache_feed_items(
        [
            _item("rss-old", source="rss", published="Thu, 01 Jan 2015 00:00:00 +0000"),
            _item("rss-new", source="rss", published="Fri, 31 Jul 2026 00:00:00 +0000"),
            _item("gh-new", source="github", published="2026-08-01T00:00:00Z"),
        ]
    )
    kept = {row["id"] for row in fetch_all(pg_authority, "SELECT id FROM feed_cache")}
    assert kept == {"rss-new", "gh-new"}


def test_prune_drops_rows_older_than_max_age(pg_authority, monkeypatch):
    monkeypatch.setattr(fa, "FEED_CACHE_MAX_AGE_DAYS", 30)
    old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=60))
    fa._cache_feed_items([_item("ancient")])
    with __import__("db").pg_transaction() as conn:
        conn.execute(
            "UPDATE feed_cache SET fetched_at = %s WHERE id = 'ancient'",
            (old.strftime("%Y-%m-%d %H:%M:%S"),),
        )
    fa._cache_feed_items([_item("fresh")])
    assert [row["id"] for row in fetch_all(pg_authority, "SELECT id FROM feed_cache")] == [
        "fresh"
    ]


# ---- 缓存读取 ---------------------------------------------------------------


def test_get_cached_feed_orders_by_real_time(pg_authority):
    fa._cache_feed_items(
        [
            _item("rss-2015", source="rss", published="Thu, 01 Jan 2015 00:00:00 +0000"),
            _item("gh-2026", source="github", published="2026-08-01T00:00:00Z"),
        ]
    )
    assert [item["id"] for item in fa.get_cached_feed()] == ["gh-2026", "rss-2015"]


def test_get_cached_feed_limit(pg_authority):
    fa._cache_feed_items([_item(f"i{i}") for i in range(5)])
    assert len(fa.get_cached_feed(limit=2)) == 2


def test_get_cached_feed_reads_via_readonly_transaction(pg_authority):
    """读取走只读事务：maintenance read_only 下仍可读。"""
    fa._cache_feed_items([_item("a")])
    import os

    os.environ["CONSOLE_MAINTENANCE_MODE"] = "read_only"
    try:
        assert [item["id"] for item in fa.get_cached_feed()] == ["a"]
    finally:
        os.environ["CONSOLE_MAINTENANCE_MODE"] = "normal"


# ---- 采集门禁 ---------------------------------------------------------------


def test_client_does_not_fetch_and_reads_shared_cache(pg_client, monkeypatch):
    """client 调 aggregate_feed：不抓 GitHub、不写库，只回共享缓存。"""
    calls = []
    monkeypatch.setattr(fa, "fetch_github", lambda token: calls.append("github") or [])
    monkeypatch.setattr(fa, "fetch_rss", lambda urls: calls.append("rss") or [])
    monkeypatch.setattr(fa, "fetch_raindrop", lambda token: calls.append("raindrop") or [])

    # 先由 authority 写入缓存（模拟 NAS 采集结果）
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "authority")
    fa._cache_feed_items([_item("nas-item")])
    monkeypatch.setenv("CONSOLE_INSTANCE_ROLE", "client")

    result = fa.aggregate_feed()
    assert calls == []
    assert [item["id"] for item in result] == ["nas-item"]
    assert count_rows(pg_client, "feed_cache") == 1


def test_client_aggregate_does_not_write(pg_client, monkeypatch):
    monkeypatch.setattr(fa, "fetch_github", lambda token: [_item("would-write")])
    fa.aggregate_feed()
    assert count_rows(pg_client, "feed_cache") == 0


def test_authority_aggregate_writes(pg_authority, monkeypatch):
    monkeypatch.setattr(fa, "fetch_github", lambda token: [_item("gh-item")])
    monkeypatch.setattr(fa, "fetch_rss", lambda urls: [])
    monkeypatch.setattr(fa, "fetch_raindrop", lambda token: [])
    monkeypatch.setenv("RSS_URLS", "")
    result = fa.aggregate_feed(sources=["github"])
    assert [item["id"] for item in result] == ["gh-item"]
    assert count_rows(pg_authority, "feed_cache") == 1


def test_read_only_blocks_cache_write(pg_authority, monkeypatch):
    """维护模式挡住缓存写入（写事务由 pgdb 统一拒绝）。"""
    import os

    import runtime_mode

    monkeypatch.setattr(fa, "fetch_github", lambda token: [_item("x")])
    os.environ["CONSOLE_MAINTENANCE_MODE"] = "read_only"
    try:
        with pytest.raises(runtime_mode.ReadOnlyMode):
            fa._cache_feed_items([_item("x")])
    finally:
        os.environ["CONSOLE_MAINTENANCE_MODE"] = "normal"
