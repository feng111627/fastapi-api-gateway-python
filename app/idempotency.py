"""幂等提交存储。

判定规则：
- 同一个幂等键 + 相同请求指纹：首次执行只产生一次实际效果，
  后续重放（包括并发在途的重放，会等待首个请求完成）返回与首次一致的结果。
- 同一个幂等键 + 不同请求指纹：明确拒绝（CONFLICT），
  既不当成新请求执行，也不返回旧请求的结果。
"""

import asyncio
from dataclasses import dataclass
from enum import Enum


class IdempotencyStatus(Enum):
    """幂等键的判定结果。"""

    NEW = "new"
    REPLAY = "replay"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class IdempotencyBegin:
    """begin 的结果：要么获准执行，要么可重放，要么冲突。"""

    status: IdempotencyStatus
    status_code: int | None = None
    body: dict | None = None


class _Entry:
    __slots__ = ("fingerprint", "done", "status_code", "body")

    def __init__(self, fingerprint: str) -> None:
        self.fingerprint = fingerprint
        self.done = asyncio.Event()
        self.status_code: int | None = None
        self.body: dict | None = None


class IdempotencyStore:
    """记录幂等键 -> 请求指纹 + 响应 的存储。"""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    async def begin(self, key: str, fingerprint: str) -> IdempotencyBegin:
        """登记一个幂等键；冲突/可重放时直接给出结果。"""
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._entries[key] = _Entry(fingerprint)
                return IdempotencyBegin(status=IdempotencyStatus.NEW)
            if entry.fingerprint != fingerprint:
                return IdempotencyBegin(status=IdempotencyStatus.CONFLICT)
            waiter = entry  # 指纹相同：已完成则重放，在途则等待后重放

        await waiter.done.wait()
        return IdempotencyBegin(
            status=IdempotencyStatus.REPLAY,
            status_code=waiter.status_code,
            body=waiter.body,
        )

    async def complete(self, key: str, status_code: int, body: dict) -> None:
        """业务执行完成后记录响应，供后续重放。"""
        async with self._lock:
            entry = self._entries[key]
            entry.status_code = status_code
            entry.body = body
            entry.done.set()
