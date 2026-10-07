"""应用装配：版本路由 + 弃用标记 + 不存在版本的兜底错误 + 治理组件。"""

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import GovernanceConfig
from app.idempotency import IdempotencyStore
from app.rate_limit import TokenBucketRateLimiter
from app.routes import build_router
from app.service import OrderService
from app.versioning import (
    CURRENT_VERSION,
    SUPPORTED_VERSIONS,
    V1_SUNSET_DATE,
    is_deprecated,
    is_supported,
)


def create_app(config: GovernanceConfig | None = None) -> FastAPI:
    config = config or GovernanceConfig()
    app = FastAPI(title="Governed Orders API")
    app.state.config = config
    app.state.rate_limiter = TokenBucketRateLimiter(
        capacity=config.rate_limit_capacity,
        refill_per_second=config.rate_limit_refill_per_second,
    )
    app.state.idempotency_store = IdempotencyStore()
    app.state.order_service = OrderService()

    @app.middleware("http")
    async def deprecation_middleware(request: Request, call_next):
        """给已过时版本的响应打上可观察的弃用标记。"""
        response = await call_next(request)
        parts = request.url.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "api" and is_deprecated(parts[1]):
            response.headers["Deprecation"] = "true"
            response.headers["Sunset"] = V1_SUNSET_DATE
            response.headers["X-API-Warn"] = (
                f"version {parts[1]} is deprecated, migrate to {CURRENT_VERSION}"
            )
        return response

    app.include_router(
        build_router(
            config,
            app.state.rate_limiter,
            app.state.idempotency_store,
            app.state.order_service,
        )
    )

    @app.get("/")
    def read_root():
        return {"Hello": "World"}

    @app.api_route(
        "/api/{version}/{rest:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def unsupported_version(version: str, rest: str):
        """版本兜底：请求了不存在的版本时给出可区分的错误。"""
        if not is_supported(version):
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "unsupported_version",
                        "message": f"API version '{version}' is not supported",
                        "supported_versions": list(SUPPORTED_VERSIONS),
                    }
                },
            )
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": "resource not found"}},
        )

    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
