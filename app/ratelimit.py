"""按调用方维度的令牌桶限流器。

设计要点：

* 每个调用方（``X-Client-Id``，缺省回退对端地址）一个桶，桶之间互不影响；
* 取令牌在一把全局 asyncio 锁内完成"惰性补充 + 扣减"，保证并发请求下
  配额既不会被算超，也不会漏算；
* 采用惰性补充而不是后台任务：只在请求到来时按距上次更新的时间差补令牌，
  因此流量恢复后无需等待任何定时器即可立刻放行，服务空闲也不耗资源；
* 返回标准 ``RateLimit-Limit/Remaining/Reset`` 与（拒限时）``Retry-After``
  数值，供中间件生成客户端可识别的响应头。
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass


@dataclass
class TokenBucket:
    capacity: int
    refill_per_second: float
    tokens: float
    updated_at: float


class RateLimiter:
    def __init__(self, capacity: int, refill_per_second: float) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = asyncio.Lock()

    def _refill(self, bucket: TokenBucket, now: float) -> None:
        elapsed = max(0.0, now - bucket.updated_at)
        if elapsed > 0:
            bucket.tokens = min(
                float(bucket.capacity), bucket.tokens + elapsed * bucket.refill_per_second
            )
            bucket.updated_at = now

    async def take(self, key: str, now: float | None = None) -> tuple[bool, dict[str, float]]:
        """尝试为 ``key`` 取 1 个令牌。

        返回 ``(allowed, headers)``，headers 含 limit/remaining/reset，
        被拒绝时额外含 retry_after（秒，向上取整，至少 1 秒）。
        """
        if now is None:
            now = time.monotonic()
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(
                    capacity=self.capacity,
                    refill_per_second=self.refill_per_second,
                    tokens=float(self.capacity),
                    updated_at=now,
                )
                self._buckets[key] = bucket
            self._refill(bucket, now)

            headers = self._headers(bucket, now)
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                headers = self._headers(bucket, now)
                return True, headers

            deficit = 1.0 - bucket.tokens
            if bucket.refill_per_second <= 0:
                # 永不补充的桶：没有有意义的等待时间
                headers["retry_after"] = 0.0
            else:
                retry_after = max(1, math.ceil(deficit / bucket.refill_per_second))
                headers["retry_after"] = float(retry_after)
            return False, headers

    @staticmethod
    def _headers(bucket: TokenBucket, now: float) -> dict[str, float]:
        remaining = int(math.floor(bucket.tokens))
        if bucket.tokens >= bucket.capacity or bucket.refill_per_second <= 0:
            reset = 0.0
        else:
            reset = math.ceil(
                (bucket.capacity - bucket.tokens) / bucket.refill_per_second
            )
        return {
            "limit": float(bucket.capacity),
            "remaining": float(remaining),
            "reset": float(reset),
        }

    def reset(self) -> None:
        """清空所有桶（测试用）。"""
        self._buckets.clear()
