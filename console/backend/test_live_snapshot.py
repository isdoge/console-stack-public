"""Cached page snapshots: stale reads never wait for a slow collector."""
import threading

from live_snapshot import SnapshotCache


def test_expired_snapshot_returns_immediately_and_refreshes_once():
    cache = SnapshotCache(ttl=0)
    assert cache.get(lambda: [1], key="assets").data == [1]
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = []

    def refresh():
        calls.append(1)
        started.set()
        assert release.wait(2)
        finished.set()
        return [2]

    stale = cache.get(refresh, key="assets")
    assert stale.data == [1]
    assert stale.refreshing is True
    assert started.wait(1)
    assert cache.get(refresh, key="assets").data == [1]
    assert len(calls) == 1
    release.set()
    assert finished.wait(1)
    assert cache.wait_for_idle(1).data == [2]


def test_explicit_force_collects_and_changed_key_never_serves_old_config():
    cache = SnapshotCache(ttl=600)
    assert cache.get(lambda: [1], key="old").data == [1]
    assert cache.get(lambda: [2], key="old", force=True).data == [2]
    assert cache.get(lambda: [3], key="new").data == [3]


def test_failed_background_refresh_keeps_last_good_snapshot():
    cache = SnapshotCache(ttl=0)
    assert cache.get(lambda: [1], key="assets").data == [1]
    failed = threading.Event()

    def fail():
        failed.set()
        raise RuntimeError("collector failed")

    assert cache.get(fail, key="assets").data == [1]
    assert failed.wait(1)
    result = cache.wait_for_idle(1)
    assert result.data == [1]
    assert result.error is True


def test_first_unusable_payload_retries_after_backoff_instead_of_full_ttl():
    cache = SnapshotCache(ttl=600, usable=lambda data: data["ok"], retry_seconds=0)
    first = cache.get(lambda: {"ok": False}, key="monitor")
    assert first.error is True
    assert first.data == {"ok": False}
    refreshed = threading.Event()
    release = threading.Event()

    def recover():
        refreshed.set()
        assert release.wait(2)
        return {"ok": True}

    stale = cache.get(recover, key="monitor")
    assert stale.data == {"ok": False}
    assert refreshed.wait(1)
    release.set()
    result = cache.wait_for_idle(1)
    assert result.data == {"ok": True}
    assert result.error is False


def test_force_waits_for_inflight_refresh_without_starting_second_collector():
    cache = SnapshotCache(ttl=0)
    assert cache.get(lambda: [1], key="assets").data == [1]
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    calls = []
    result = []

    def refresh():
        calls.append(1)
        started.set()
        assert release.wait(2)
        return [2]

    assert cache.get(refresh, key="assets").refreshing is True
    assert started.wait(1)

    def force():
        result.append(cache.get(lambda: [3], key="assets", force=True))
        completed.set()

    waiter = threading.Thread(target=force)
    waiter.start()
    assert not completed.wait(0.05)
    assert calls == [1]
    release.set()
    assert completed.wait(1)
    waiter.join(timeout=1)
    assert result[0].data == [2]
