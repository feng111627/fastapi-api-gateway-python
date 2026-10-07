"""治理中间件（纯 ASGI 实现）。

固定执行顺序，任何业务请求都必须依次通过，层与层之间不存在绕过：

    1. 版本判定  —— 未知版本直接 400 version_not_supported；
                    命中弃用版本则记录 warning 日志并下发 Deprecation/Sunset 头；
    2. 调用方限流 —— 按 X-Client-Id（缺省对端地址，再缺省 anonymous）取令牌，
                    超限 429 rate_limited，携带 RateLimit-* 与 Retry-After；
    3. 幂等控制  —— 写请求带 Idempotency-Key 时做指纹去重/回放/冲突拒绝。

纯 ASGI 而非 BaseHTTPMiddleware，避免请求体被上游提前消费导致下游拿不到数据。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from email.utils import formatdate
from typing import Any

from app.config import GovernanceConfig
from app.errors import (
    GovernanceError,
    IdempotencyConflictError,
    RateLimitedError,
    VersionNotSupportedError,
    error_body,
)
from app.idempotency import IdempotencyEntry, IdempotencyStore
from app.ratelimit import RateLimiter
from app.versioning import resolve_version, strip_version_prefix

logger = logging.getLogger("governance")

_IDEMPOTENT_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_EXEMPT_PATHS = frozenset({"/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico"})
# 重放/转发时不直接沿用的逐跳与长度头
_DROP_HEADER_NAMES = frozenset(
    {
        "connection",
        "keep-alive",
        "transfer-encoding",
        "content-length",
        "ratelimit-limit",
        "ratelimit-remaining",
        "ratelimit-reset",
        "retry-after",
        "deprecation",
        "sunset",
        "link",
        "x-api-version",
        "x-api-deprecated",
        "idempotency-replayed",
    }
)


class GovernanceMiddleware:
    def __init__(
        self,
        app: Any,
        limiter: RateLimiter,
        idempotency: IdempotencyStore,
        cfg: GovernanceConfig,
        monotonic: Any = time.monotonic,
        wall_time: Any = time.time,
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.idempotency = idempotency
        self.cfg = cfg
        self._monotonic = monotonic
        self._wall_time = wall_time

    # ---------- ASGI 入口 ----------

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw_headers = self._parse_headers(scope.get("headers", []))
        path: str = scope["path"]

        # 1) 版本判定（最先执行，未知版本不会进入后续环节）
        try:
            version_info = resolve_version(
                path, raw_headers.get("x-api-version"), self.cfg
            )
        except VersionNotSupportedError as exc:
            logger.info("rejected: %s %s -> %s", scope["method"], path, exc.code)
            await self._send_error(send, exc)
            return

        effective_path = strip_version_prefix(path)
        if effective_path != path:
            scope = dict(scope)
            scope["path"] = effective_path
            scope["raw_path"] = effective_path.encode("latin-1")
        scope["api_version"] = version_info.version

        # 文档/健康检查等辅助端点只做版本门控，不参与限流/幂等，也不打弃用日志
        if effective_path in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        if version_info.deprecated:
            logger.warning(
                "deprecated API version %s used (source=%s): %s %s",
                version_info.version,
                version_info.source,
                scope["method"],
                path,
            )

        # 2) 调用方限流
        client_id = (
            raw_headers.get("x-client-id")
            or (scope.get("client") or ("", 0))[0]
            or "anonymous"
        )
        allowed, rl = await self.limiter.take(f"rl:{client_id}", self._monotonic())
        governance_headers = self._ratelimit_headers(rl)
        governance_headers.update(self._version_headers(version_info))
        if not allowed:
            logger.info("rate limited caller=%s path=%s", client_id, path)
            exc = RateLimitedError(
                details={
                    "client_id": client_id,
                    "limit": int(rl["limit"]),
                    "retry_after_seconds": int(rl.get("retry_after", 0)),
                },
                headers={"Retry-After": str(int(rl.get("retry_after", 0)))},
            )
            await self._send_error(send, exc, governance_headers)
            return

        # 3) 幂等控制（仅写请求 + 携带 Idempotency-Key）
        method = scope["method"]
        idem_key = raw_headers.get("idempotency-key")
        if idem_key and method in _IDEMPOTENT_METHODS:
            await self._handle_idempotent(
                scope, receive, send, method, effective_path, client_id, idem_key,
                governance_headers,
            )
            return

        await self._invoke(scope, receive, send, b"", governance_headers)

    # ---------- 幂等 ----------

    async def _handle_idempotent(
        self, scope, receive, send, method, effective_path, client_id,
        idem_key, governance_headers,
    ) -> None:
        body = await self._read_body(receive)
        fingerprint = self._fingerprint(
            method, scope["api_version"], effective_path,
            scope.get("query_string", b"").decode("latin-1"), body,
        )
        scope_key = f"idem:{client_id}:{idem_key}"
        now = self._wall_time()

        # 同键并发时循环处理 "首请求失败 -> 放行重试" 的窗口
        while True:
            state = await self.idempotency.begin(scope_key, fingerprint, now)
            if state == "new":
                await self._invoke(
                    scope, _single_message_receive(body), send, body,
                    governance_headers, idem=(scope_key, fingerprint, now),
                )
                return
            if state == "conflict":
                logger.info(
                    "idempotency conflict key=%s caller=%s path=%s",
                    idem_key, client_id, effective_path,
                )
                exc = IdempotencyConflictError(
                    details={"idempotency_key": idem_key, "client_id": client_id}
                )
                await self._send_error(send, exc, governance_headers)
                return
            if state == "replay":
                entry = await self.idempotency.get(scope_key)
                if entry is not None:
                    await self._send_replay(send, entry, governance_headers)
                    return
                # 理论上取不到则继续重试
            outcome = await self.idempotency.wait_for_completion(scope_key)
            if outcome == "replay":
                entry = await self.idempotency.get(scope_key)
                if entry is not None:
                    await self._send_replay(send, entry, governance_headers)
                    return

    async def _invoke(
        self, scope, receive, send, body: bytes, governance_headers, idem=None
    ) -> None:
        """调用下游应用并捕获响应；幂等执行请求负责写缓存。"""
        captured: dict[str, Any] = {"status": 200, "headers": [], "chunks": []}

        async def send_capture(message) -> None:
            mtype = message["type"]
            if mtype == "http.response.start":
                captured["status"] = message["status"]
                captured["headers"] = list(message.get("headers", []))
            elif mtype == "http.response.body":
                captured["chunks"].append(message.get("body", b""))

        try:
            await self.app(scope, receive, send_capture)
        except Exception:
            if idem is not None:
                await self.idempotency.fail(idem[0])
            raise

        response_body = b"".join(captured["chunks"])
        status = captured["status"]

        if idem is not None:
            scope_key, fingerprint, now = idem
            if status < 500:
                safe_headers = {
                    k.decode("latin-1"): v.decode("latin-1")
                    for k, v in captured["headers"]
                    if k.decode("latin-1") not in _DROP_HEADER_NAMES
                }
                entry = IdempotencyEntry(
                    fingerprint=fingerprint,
                    response_status=status,
                    response_headers=safe_headers,
                    response_body=response_body,
                    created_at=now,
                )
                await self.idempotency.complete(scope_key, entry)
            else:
                await self.idempotency.fail(scope_key)

        await self._flush(
            send, status, captured["headers"], response_body, governance_headers,
            replayed=False,
        )

    async def _send_replay(self, send, entry: IdempotencyEntry, governance_headers) -> None:
        headers = [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in entry.response_headers.items()
        ]
        await self._flush(
            send, entry.response_status, headers, entry.response_body,
            governance_headers, replayed=True,
        )

    # ---------- 输出 ----------

    async def _flush(
        self, send, status, downstream_headers, body: bytes,
        governance_headers: dict[str, str], replayed: bool,
    ) -> None:
        kept = [
            (k, v)
            for k, v in downstream_headers
            if k.decode("latin-1") not in _DROP_HEADER_NAMES
        ]
        extra = dict(governance_headers)
        if replayed:
            extra["Idempotency-Replayed"] = "true"
        for k, v in extra.items():
            kept.append((k.lower().encode("latin-1"), str(v).encode("latin-1")))
        kept.append((b"content-length", str(len(body)).encode("latin-1")))

        await send({
            "type": "http.response.start",
            "status": status,
            "headers": kept,
        })
        await send({"type": "http.response.body", "body": body})

    async def _send_error(
        self, send, exc: GovernanceError, governance_headers: dict[str, str] | None = None
    ) -> None:
        headers = dict(governance_headers or {})
        headers.update(exc.headers)
        body = json.dumps(error_body(exc.code, exc.message, exc.details)).encode("utf-8")
        start_headers = [
            (k.lower().encode("latin-1"), str(v).encode("latin-1"))
            for k, v in headers.items()
        ]
        start_headers.append((b"content-type", b"application/json"))
        start_headers.append((b"content-length", str(len(body)).encode("latin-1")))
        await send({"type": "http.response.start", "status": exc.status_code,
                    "headers": start_headers})
        await send({"type": "http.response.body", "body": body})

    # ---------- 小工具 ----------

    @staticmethod
    def _parse_headers(scope_headers) -> dict[str, str]:
        return {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope_headers
        }

    @staticmethod
    def _ratelimit_headers(rl: dict[str, float]) -> dict[str, str]:
        return {
            "RateLimit-Limit": str(int(rl["limit"])),
            "RateLimit-Remaining": str(int(rl["remaining"])),
            "RateLimit-Reset": str(int(rl["reset"])),
        }

    def _version_headers(self, version_info) -> dict[str, str]:
        headers = {"X-API-Version": version_info.version}
        if version_info.deprecated:
            headers["X-API-Deprecated"] = "true"
            headers["Deprecation"] = "true"
            # 公告 6 个月后移除（仅为可观察信号）
            sunset = time.strptime("2027-04-08", "%Y-%m-%d")
            headers["Sunset"] = formatdate(time.mktime(sunset), usegmt=True)
            headers["Link"] = f'</v{self.cfg.latest_version}>; rel="successor-version"'
        return headers

    @staticmethod
    def _fingerprint(method, version, path, query_string, body) -> str:
        digest = hashlib.sha256()
        digest.update(method.encode())
        digest.update(b"\n")
        digest.update(version.encode())
        digest.update(b"\n")
        digest.update(path.encode())
        digest.update(b"\n")
        digest.update(query_string.encode("latin-1"))
        digest.update(b"\n")
        digest.update(body)
        return digest.hexdigest()

    @staticmethod
    async def _read_body(receive) -> bytes:
        chunks = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                chunks.append(b"")
                break
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        return b"".join(chunks)


def _single_message_receive(body: bytes):
    """把已缓冲的请求体重新作为 ASGI receive 提供给下游。"""

    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive
