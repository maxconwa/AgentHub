"""In-memory token-bucket rate limits.

Applied to every agent request (keyed by agent id, after authentication), to
enrollment (keyed by the caller's address, since enrollment codes are short
enough that guessing must be throttled), and to frames on agent WebSockets.
The hub is a single process, so in-memory buckets are sufficient.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from dataclasses import dataclass

from fastapi import HTTPException


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self, rate_per_second: float, burst: int, *, max_keys: int = 10_000,
                 clock=time.monotonic) -> None:
        self.rate = rate_per_second
        self.burst = burst
        self.max_keys = max_keys
        self.clock = clock
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def try_acquire(self, key: str, cost: float = 1.0) -> float:
        """Take tokens. Returns 0 on success, else the seconds to wait before retrying."""
        now = self.clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=float(self.burst), updated=now)
            self._buckets[key] = bucket
            if len(self._buckets) > self.max_keys:
                self._buckets.popitem(last=False)  # forget the least recently seen key
        else:
            self._buckets.move_to_end(key)
            bucket.tokens = min(self.burst, bucket.tokens + (now - bucket.updated) * self.rate)
            bucket.updated = now
        if bucket.tokens >= cost:
            bucket.tokens -= cost
            return 0.0
        return (cost - bucket.tokens) / self.rate if self.rate > 0 else math.inf

    def check(self, key: str, what: str = "requests", cost: float = 1.0) -> None:
        wait = self.try_acquire(key, cost)
        if wait:
            retry = max(1, math.ceil(wait)) if math.isfinite(wait) else 3600
            raise HTTPException(429, f"too many {what}; retry in {retry}s", headers={"Retry-After": str(retry)})
