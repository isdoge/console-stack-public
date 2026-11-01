"""Last-good snapshots for slow page reads, with one background refresh per key."""
import datetime
import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger("console.backend")


@dataclass(frozen=True)
class Snapshot:
    data: object
    updated_at: str
    refreshing: bool
    error: bool


class SnapshotCache:
    """Return stale data immediately; force and first read wait for collection.

    A key change invalidates the old payload, so a replaced assets file cannot
    display entries from its predecessor. A failed refresh keeps the last good
    payload and retries ordinary reads after a short backoff.
    """

    def __init__(self, ttl, usable=None, retry_seconds=30):
        self.ttl = ttl
        self.usable = usable or (lambda data: True)
        self.retry_seconds = retry_seconds
        self._cv = threading.Condition()
        self._key = None
        self._generation = 0
        self._data = None
        self._updated_at = None
        self._collected_at = 0.0
        self._retry_at = 0.0
        self._refreshing = False
        self._error = False

    def _snapshot(self):
        return Snapshot(self._data, self._updated_at, self._refreshing, self._error)

    def invalidate(self):
        with self._cv:
            self._generation += 1
            self._key = None
            self._data = None
            self._updated_at = None
            self._collected_at = 0.0
            self._retry_at = 0.0
            self._refreshing = False
            self._error = False
            self._cv.notify_all()

    def _run(self, builder, generation):
        try:
            data = builder()
            usable = self.usable(data)
        except Exception as exc:
            logger.warning("page snapshot refresh failed: %s", type(exc).__name__)
            data = None
            usable = False
        now = time.monotonic()
        with self._cv:
            if generation != self._generation:
                return
            if usable or (self._data is None and data is not None):
                self._data = data
                self._updated_at = datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat().replace("+00:00", "Z")
                self._collected_at = now
            self._error = not usable
            self._retry_at = now + self.retry_seconds if not usable else 0.0
            self._refreshing = False
            self._cv.notify_all()

    def get(self, builder, key, force=False):
        with self._cv:
            if self._key != key:
                self._generation += 1
                self._key = key
                self._data = None
                self._updated_at = None
                self._collected_at = 0.0
                self._retry_at = 0.0
                self._refreshing = False
                self._error = False
                self._cv.notify_all()
            generation = self._generation
            now = time.monotonic()
            if not force and self._data is not None:
                if (
                    (
                        self._error
                        or now - self._collected_at >= (
                            self.ttl() if callable(self.ttl) else self.ttl
                        )
                    )
                    and not self._refreshing
                    and now >= self._retry_at
                ):
                    self._refreshing = True
                    threading.Thread(
                        target=self._run, args=(builder, generation), daemon=True
                    ).start()
                return self._snapshot()
            if self._refreshing:
                self._cv.wait_for(
                    lambda: generation != self._generation or not self._refreshing
                )
                if generation != self._generation:
                    raise RuntimeError("snapshot changed during refresh")
                if self._data is None:
                    raise RuntimeError("snapshot unavailable")
                return self._snapshot()
            self._refreshing = True

        self._run(builder, generation)
        with self._cv:
            if generation != self._generation or self._data is None:
                raise RuntimeError("snapshot unavailable")
            return self._snapshot()

    def wait_for_idle(self, timeout):
        """Wait for an in-flight refresh; used by bounded diagnostics and tests."""
        with self._cv:
            if not self._cv.wait_for(lambda: not self._refreshing, timeout):
                raise TimeoutError("snapshot refresh timed out")
            return self._snapshot()
