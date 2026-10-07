"""按调用方维度的令牌桶限流。

- 每个调用方（key）一个独立令牌桶，容量 capacity，按 refill_per_second 匀速回补。
- 所有判定在同一把 asyncio 锁内完成：先按经过时间回补、再扣减，
  并发请求不会被算超（不会放行超额请求）也不会漏算（不会丢扣减）。
- 令牌随时间恢复，超限拒绝后额度回补即可再次放行，不会一直卡住。
"""

import asyncio
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitDecision:
    """一次限流判定的结果。"""

    allowed: bool
    limit: int
    remaining: int
    retry_after: float
    reset_at: float


class _Bucket:
    __slots__ = ("tokens", "updated_at")

    def __init__(self, tokens: float, updated_at: float) -> None:
        self.tokens = tokens
        self.updated_at = updated_at


class TokenBucketRateLimiter:
    """按 key 维护独立令牌桶的限流器。"""

    def __init__(self, capacity: int, refill_per_second: float) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if refill_per_second <= 0:
            raise ValueError("refill_per_second must be > 0")
        self._capacity = capacity
        self._refill_rate = refill_per_second
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    async def acquire(self, key: str) -> RateLimitDecision:
        """尝试取走一个令牌，返回判定结果（不抛异常）。"""
        async with self._lock:
            now = time.monotonic()
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(self._capacity), updated_at=now)
                self._buckets[key] = bucket

            # 按经过时间回补，封顶到容量。
            elapsed = now - bucket.updated_at
            bucket.tokens = min(
                float(self._capacity), bucket.tokens + elapsed * self._refill_rate
            )
            bucket.updated_at = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                allowed = True
                retry_after = 0.0
            else:
                allowed = False
                # 还差多少秒能攒够一个令牌。
                retry_after = (1.0 - bucket.tokens) / self._refill_rate

            reset_at = now + (self._capacity - bucket.tokens) / self._refill_rate
            return RateLimitDecision(
                allowed=allowed,
                limit=self._capacity,
                remaining=int(bucket.tokens),
                retry_after=retry_after,
                reset_at=reset_at,
            )
