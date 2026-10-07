"""限流场景：并发不超放、按调用方隔离、超限可区分、额度恢复后放行。"""

import asyncio

import pytest

from tests.conftest import headers

pytestmark = pytest.mark.asyncio


async def test_concurrent_requests_never_exceed_quota(client, config):
    """并发限流：N 倍并发同时打进来，放行数恰好等于配额，不超算不漏算。"""
    total = config.rate_limit_capacity * 5

    async def one(i: int):
        return await client.post(
            "/api/v2/orders",
            json={"item": f"item-{i}"},
            headers=headers("burst-client", f"burst-{i}"),
        )

    responses = await asyncio.gather(*[one(i) for i in range(total)])
    allowed = [r for r in responses if r.status_code == 201]
    rejected = [r for r in responses if r.status_code == 429]
    assert len(allowed) == config.rate_limit_capacity
    assert len(rejected) == total - config.rate_limit_capacity
    # 放行的订单互不重复：没有漏算导致同一额度被用两次
    ids = {r.json()["order"]["order_id"] for r in allowed}
    assert len(ids) == len(allowed)


async def test_rejected_response_carries_rate_limit_details(client, config):
    """超限拒绝：429 + 可区分错误码 + 客户端可识别的限流头。"""
    for i in range(config.rate_limit_capacity):
        r = await client.post(
            "/api/v2/orders", json={"item": f"x{i}"}, headers=headers("limited", f"rl-{i}")
        )
        assert r.status_code == 201

    r = await client.post(
        "/api/v2/orders", json={"item": "over"}, headers=headers("limited", "rl-over")
    )
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limit_exceeded"
    assert r.headers["X-RateLimit-Limit"] == str(config.rate_limit_capacity)
    assert r.headers["X-RateLimit-Remaining"] == "0"
    assert "X-RateLimit-Reset" in r.headers
    assert int(r.headers["Retry-After"]) >= 1


async def test_quota_is_per_caller(client, config):
    """按调用方维度：一个调用方超限不影响其他调用方。"""
    for i in range(config.rate_limit_capacity):
        await client.post(
            "/api/v2/orders", json={"item": f"a{i}"}, headers=headers("hog", f"hog-{i}")
        )
    blocked = await client.post(
        "/api/v2/orders", json={"item": "a"}, headers=headers("hog", "hog-blocked")
    )
    assert blocked.status_code == 429

    other = await client.post(
        "/api/v2/orders", json={"item": "b"}, headers=headers("other", "other-1")
    )
    assert other.status_code == 201


async def test_quota_recovers_after_refill(client, config):
    """额度恢复：超限被拒后等待令牌回补，能正常放行而不是一直卡住。"""
    for i in range(config.rate_limit_capacity):
        await client.post(
            "/api/v2/orders", json={"item": f"r{i}"}, headers=headers("recover", f"rec-{i}")
        )
    blocked = await client.post(
        "/api/v2/orders", json={"item": "r"}, headers=headers("recover", "rec-blocked")
    )
    assert blocked.status_code == 429

    await asyncio.sleep(2.0 / config.rate_limit_refill_per_second)
    recovered = await client.post(
        "/api/v2/orders", json={"item": "r"}, headers=headers("recover", "rec-after")
    )
    assert recovered.status_code == 201
    assert recovered.headers["X-RateLimit-Remaining"] >= "0"
