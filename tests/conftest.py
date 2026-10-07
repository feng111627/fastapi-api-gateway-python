"""测试公共装置：小配额、快回补的配置，便于观察限流行为。"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import GovernanceConfig
from main import create_app


@pytest.fixture
def config() -> GovernanceConfig:
    return GovernanceConfig(rate_limit_capacity=3, rate_limit_refill_per_second=20.0)


@pytest.fixture
def app(config):
    return create_app(config)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def make_client():
    """按自定义配置构造客户端的工厂，用于需要不同限流配额的用例。"""

    async def _make(config: GovernanceConfig):
        app = create_app(config)
        transport = ASGITransport(app=app)
        return app, AsyncClient(transport=transport, base_url="http://test")

    return _make


def headers(client_id: str, key: str) -> dict[str, str]:
    return {"X-Client-Id": client_id, "X-Idempotency-Key": key}
