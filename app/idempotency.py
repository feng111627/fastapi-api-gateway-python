"""幂等键存储：去重、请求指纹校验、重放一致结果。

语义：

* 客户端在写请求（POST/PUT/PATCH/DELETE）上携带 ``Idempotency-Key`` 头；
* 同一 (调用方, 幂等键) 第一次请求：真正执行业务，并缓存最终响应
  （状态码 + 可安全重放的响应头 + 响应体）；
* 后续携带相同键的请求：
    - 请求指纹（方法 + 版本 + 路径 + 查询串 + 请求体哈希）一致 → 直接回放
      首次结果，状态码/响应体完全一致，并加 ``Idempotency-Replayed: true``；
    - 指纹不一致 → 409 ``idempotency_conflict``，明确拒绝；
* 同一键并发到达时，只有一个请求实际执行，其余请求等待其完成后回放，
  因此业务效果严格只有一次；
* 首次请求执行失败（抛出异常 / 返回 5xx）时不写缓存，允许客户端用同键重试；
* 条目有 TTL，过期后同键视为新请求。

注意：内存实现适合单实例；多实例部署应替换为带 CAS 的外部存储，接口不变。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class IdempotencyEntry:
    fingerprint: str
    response_status: int
    response_headers: dict[str, str]
    response_body: Any
    created_at: float


@dataclass
class _Slot:
    fingerprint: str
    done: asyncio.Event = field(default_factory=asyncio.Event)
    entry: IdempotencyEntry | None = None
    failed: bool = False


class IdempotencyStore:
    def __init__(self, ttl_seconds: float = 3600.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._slots: dict[str, _Slot] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

    async def _get_lock(self, scope_key: str) -> asyncio.Lock:
        async with self._registry_lock:
            lock = self._locks.get(scope_key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[scope_key] = lock
            return lock

    def _purge_if_expired(self, scope_key: str, now: float) -> None:
        slot = self._slots.get(scope_key)
        if slot is not None and slot.entry is not None:
            if now - slot.entry.created_at > self.ttl_seconds:
                del self._slots[scope_key]

    async def begin(self, scope_key: str, fingerprint: str, now: float) -> str:
        """登记一次幂等请求。

        返回：
        * ``"new"``        —— 本请求获得执行权，完成后必须调用 complete/fail；
        * ``"replay"``     —— 已有成功结果，调用 get 回放；
        * ``"conflict"``   —— 同键不同指纹，应拒绝。
        """
        lock = await self._get_lock(scope_key)
        async with lock:
            self._purge_if_expired(scope_key, now)
            slot = self._slots.get(scope_key)
            if slot is None:
                self._slots[scope_key] = _Slot(fingerprint=fingerprint)
                return "new"
            if slot.fingerprint != fingerprint:
                return "conflict"
            if slot.entry is not None:
                return "replay"
            # 同键同指纹的并发请求：首请求还在执行中
            return "processing"

    async def wait_for_completion(self, scope_key: str) -> str:
        """等待同键的首个并发请求执行结束，返回 ``replay`` 或 ``retry``。"""
        slot = self._slots.get(scope_key)
        if slot is None:
            return "retry"
        await slot.done.wait()
        if slot.entry is not None:
            return "replay"
        return "retry"  # 首请求失败，未缓存：放行重试

    async def get(self, scope_key: str) -> IdempotencyEntry | None:
        slot = self._slots.get(scope_key)
        if slot is None or slot.entry is None:
            return None
        return slot.entry

    async def complete(self, scope_key: str, entry: IdempotencyEntry) -> None:
        slot = self._slots.get(scope_key)
        if slot is not None:
            slot.entry = entry
            slot.done.set()

    async def fail(self, scope_key: str) -> None:
        """首请求执行失败：移除占位，不缓存结果，允许同键重试。"""
        slot = self._slots.pop(scope_key, None)
        if slot is not None:
            slot.failed = True
            slot.done.set()

    def reset(self) -> None:
        """清空全部条目（测试用）。"""
        self._slots.clear()
        self._locks.clear()
