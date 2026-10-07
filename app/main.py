"""组装 FastAPI 应用：治理中间件 + 业务路由。"""
from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import GovernanceConfig, config as default_config
from app.errors import GovernanceError, error_body
from app.idempotency import IdempotencyStore
from app.middleware import GovernanceMiddleware
from app.ratelimit import RateLimiter
from app.routers.orders import router as orders_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("app")

# 模块级单例，供 uvicorn 入口使用；测试通过 create_app 参数自建隔离实例
limiter = RateLimiter(default_config.rate_capacity, default_config.rate_refill_per_second)
idempotency = IdempotencyStore(default_config.idempotency_ttl_seconds)


def create_app(
    cfg: GovernanceConfig | None = None,
    rate_limiter: RateLimiter | None = None,
    idem_store: IdempotencyStore | None = None,
    monotonic=time.monotonic,
    wall_time=time.time,
    extra_routes: Sequence | None = None,
) -> FastAPI:
    cfg = cfg or default_config
    rate_limiter = rate_limiter or RateLimiter(cfg.rate_capacity, cfg.rate_refill_per_second)
    idem_store = idem_store or IdempotencyStore(cfg.idempotency_ttl_seconds)

    app = FastAPI(
        title="FastAPI Gateway with request governance",
        version="0.1.0",
        summary="限流 / 幂等 / 版本治理示例",
    )
    app.include_router(orders_router)
    if extra_routes:
        for route in extra_routes:
            app.router.add_api_route(**route)
    app.add_middleware(
        GovernanceMiddleware,
        limiter=rate_limiter,
        idempotency=idem_store,
        cfg=cfg,
        monotonic=monotonic,
        wall_time=wall_time,
    )
    app.state.limiter = rate_limiter
    app.state.idempotency = idem_store

    @app.exception_handler(GovernanceError)
    async def governance_error_handler(request, exc: GovernanceError):
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.details),
            headers=exc.headers,
        )

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app(default_config, limiter, idempotency)
