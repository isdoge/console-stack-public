"""Pure file-source time/identity contract; no I/O, config or clock reads."""
import datetime
import math
from dataclasses import dataclass
from typing import Optional, Tuple

SOURCE_MAX_AGE_SECONDS = 900
SOURCE_FUTURE_TOLERANCE_SECONDS = 60


@dataclass(frozen=True)
class SourceStamp:
    identity: Tuple[int, int, int, int]
    sampled_at: str
    # None rejects a future timestamp without establishing any validity period.
    deadline: Optional[float]

    def expired(self, mono_now):
        return self.deadline is None or mono_now > self.deadline


def source_stamp(stat, wall_now, mono_now, previous=None):
    """Use one descriptor's stat and never extend an observed version's lifetime.

    A wall-clock forward jump can shorten a deadline on a real reread. A rollback,
    force read or cache invalidation cannot extend it. Stale sources retain a past
    deadline so a later wall rollback cannot revive them. Callers keep their prior
    stamp on a rejected future timestamp or read/parse/row-conversion failure.
    """
    sampled_epoch = stat.st_mtime
    for value in (sampled_epoch, wall_now, mono_now):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("invalid source time")
    sampled_at = datetime.datetime.fromtimestamp(
        sampled_epoch, datetime.timezone.utc
    ).isoformat().replace("+00:00", "Z")
    identity = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in identity):
        raise ValueError("invalid source identity")
    age = wall_now - sampled_epoch
    if age < -SOURCE_FUTURE_TOLERANCE_SECONDS:
        return SourceStamp(identity, sampled_at, None)
    deadline = mono_now + SOURCE_MAX_AGE_SECONDS - max(0.0, age)
    if previous is not None and previous.identity == identity and previous.deadline is not None:
        deadline = min(deadline, previous.deadline)
    return SourceStamp(identity, sampled_at, deadline)
