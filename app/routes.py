"""业务路由与治理装配。

每个受治理接口的处理顺序固定为：版本路由（FastAPI 匹配）-> 限流 -> 幂等 -> 业务。
不存在的版本根本不会进入这些路由，因此不会消耗限流额度、也不会触碰幂等存储。
"""

import hashlib
import json

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.config import GovernanceConfig
from app.idempotency import IdempotencyStatus, IdempotencyStore
from app.rate_limit import TokenBucketRateLimiter
from app.service import OrderService


def _error(status_code: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
        headers=headers,
    )


def _rate_limit_headers(decision) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str(int(decision.reset_at)),
    }


def build_router(
    config: GovernanceConfig,
    limiter: TokenBucketRateLimiter,
    store: IdempotencyStore,
    service: OrderService,
) -> APIRouter:
    router = APIRouter()

    async def create_order(request: Request, response: Response, version: str):
        # 1) 限流：按调用方维度判定，超限直接拒绝并给出限流信息。
        client_id = request.headers.get(config.client_id_header, "anonymous")
        decision = await limiter.acquire(client_id)
        rl_headers = _rate_limit_headers(decision)
        if not decision.allowed:
            rl_headers["Retry-After"] = str(max(1, int(decision.retry_after + 0.5)))
            return _error(
                429,
                "rate_limit_exceeded",
                f"rate limit exceeded for client '{client_id}', retry later",
                headers=rl_headers,
            )
        for k, v in rl_headers.items():
            response.headers[k] = v

        # 2) 幂等：缺少幂等键直接拒绝；同键不同内容明确冲突。
        idem_key = request.headers.get(config.idempotency_header)
        if not idem_key:
            return _error(
                400,
                "idempotency_key_required",
                f"header '{config.idempotency_header}' is required for order creation",
            )
        body_bytes = await request.body()
        fingerprint = hashlib.sha256(body_bytes).hexdigest()
        begin = await store.begin(idem_key, fingerprint)
        if begin.status is IdempotencyStatus.CONFLICT:
            return _error(
                409,
                "idempotency_conflict",
                "idempotency key was already used with a different request body",
            )
        if begin.status is IdempotencyStatus.REPLAY:
            return JSONResponse(
                status_code=begin.status_code or 200,
                content=begin.body,
                headers={**rl_headers, "Idempotency-Replayed": "true"},
            )

        # 3) 业务：只有 NEW 请求会走到这里，保证只产生一次实际效果。
        try:
            payload = json.loads(body_bytes) if body_bytes else {}
        except json.JSONDecodeError:
            await store.complete(idem_key, 400, {"error": {"code": "invalid_json", "message": "request body is not valid JSON"}})
            return _error(400, "invalid_json", "request body is not valid JSON")
        if not isinstance(payload, dict) or not payload.get("item"):
            body = {"error": {"code": "invalid_order", "message": "field 'item' is required"}}
            await store.complete(idem_key, 400, body)
            return JSONResponse(status_code=400, content=body, headers=rl_headers)

        order = await service.create_order(payload)
        if version == "v1":
            result = order  # 旧版响应结构保持不变
        else:
            result = {"order": order, "version": "v2"}
        await store.complete(idem_key, 201, result)
        return JSONResponse(status_code=201, content=result, headers=rl_headers)

    async def create_order_v1(request: Request, response: Response):
        return await create_order(request, response, "v1")

    async def create_order_v2(request: Request, response: Response):
        return await create_order(request, response, "v2")

    router.add_api_route(
        "/api/v1/orders",
        create_order_v1,
        methods=["POST"],
        deprecated=True,
        tags=["orders", "v1"],
    )
    router.add_api_route(
        "/api/v2/orders",
        create_order_v2,
        methods=["POST"],
        tags=["orders", "v2"],
    )
    return router
