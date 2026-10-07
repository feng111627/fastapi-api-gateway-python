"""并发限流场景：按调用方计数、不超算不漏算、拒绝可识别、恢复后放行。"""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

pytestmark = pytest.mark.ratelimit
logger = logging.getLogger("tests.ratelimit")

ORDERS_URL = "http://gw/orders"
PAYLOAD = {"product": "book", "quantity": 1}


def test_quota_headers_on_allowed_request(client):
    """放行响应应带标准 RateLimit-* 头，数值随消耗递减。"""
    logger.info("验证: 放行时返回 RateLimit-Limit/Remaining/Reset")
    headers = {"X-Client-Id": "alice"}
    r1 = client.post("/orders", json=PAYLOAD, headers=headers)
    assert r1.status_code == 201
    assert r1.headers["RateLimit-Limit"] == "5"
    assert r1.headers["RateLimit-Remaining"] == "4"

    r2 = client.post("/orders", json=PAYLOAD, headers=headers)
    assert r2.headers["RateLimit-Remaining"] == "3"


def test_concurrent_requests_cannot_overspend_quota(app):
    """20 个并发请求抢 5 个令牌，成功数必须恰好为 5，其余都是 429。"""
    logger.info("验证: 并发 20 请求, 配额 5, 成功严格 == 5")

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw"
        ) as ac:
            async def one():
                return await ac.post("/orders", json=PAYLOAD,
                                     headers={"X-Client-Id": "bob"})
            return await asyncio.gather(*[one() for _ in range(20)])

    responses = asyncio.run(scenario())
    statuses = sorted(r.status_code for r in responses)
    allowed = [s for s in statuses if s == 201]
    rejected = [s for s in statuses if s == 429]
    assert len(allowed) == 5
    assert len(rejected) == 15
    logger.info("实际放行=%d, 拒绝=%d", len(allowed), len(rejected))


def test_rejected_response_is_machine_readable(client):
    """超限拒绝要给出可区分原因和客户端可识别的限流信息。"""
    logger.info("验证: 429 + rate_limited 错误码 + Retry-After/限流头")
    headers = {"X-Client-Id": "carol"}
    for _ in range(5):
        assert client.post("/orders", json=PAYLOAD, headers=headers).status_code == 201

    r = client.post("/orders", json=PAYLOAD, headers=headers)
    assert r.status_code == 429
    body = r.json()
    assert body["error"]["code"] == "rate_limited"
    assert body["error"]["details"]["client_id"] == "carol"
    assert body["error"]["details"]["limit"] == 5
    assert int(body["error"]["details"]["retry_after_seconds"]) >= 1
    assert r.headers["RateLimit-Remaining"] == "0"
    assert int(r.headers["Retry-After"]) >= 1


def test_quota_refills_and_does_not_stay_blocked(client, clock):
    """时间推进、令牌补充后必须恢复放行，不能一直卡住。"""
    logger.info("验证: 打满 -> 时钟推进 1s -> 恰好恢复 1 个令牌")
    headers = {"X-Client-Id": "dave"}
    for _ in range(5):
        client.post("/orders", json=PAYLOAD, headers=headers)
    assert client.post("/orders", json=PAYLOAD, headers=headers).status_code == 429

    clock.advance(0.2)  # 5 tokens/s => 恢复 1 个令牌
    r = client.post("/orders", json=PAYLOAD, headers=headers)
    assert r.status_code == 201, r.text
    assert r.headers["RateLimit-Remaining"] == "0"

    clock.advance(0.4)  # 再恢复 2 个
    assert client.post("/orders", json=PAYLOAD, headers=headers).status_code == 201
    assert client.post("/orders", json=PAYLOAD, headers=headers).status_code == 201
    assert client.post("/orders", json=PAYLOAD, headers=headers).status_code == 429

    clock.advance(60.0)  # 空闲足够久，恢复到上限而不是无限累积
    r = client.post("/orders", json=PAYLOAD, headers=headers)
    assert r.status_code == 201
    assert r.headers["RateLimit-Remaining"] == "4"


def test_quotas_are_isolated_per_caller(client, clock):
    """不同调用方独立计数，互不影响。"""
    logger.info("验证: 调用方 A 打满不影响调用方 B")
    for _ in range(5):
        assert client.post("/orders", json=PAYLOAD,
                           headers={"X-Client-Id": "A"}).status_code == 201
    assert client.post("/orders", json=PAYLOAD,
                       headers={"X-Client-Id": "A"}).status_code == 429
    r = client.post("/orders", json=PAYLOAD, headers={"X-Client-Id": "B"})
    assert r.status_code == 201
    assert r.headers["RateLimit-Remaining"] == "4"
