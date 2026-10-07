"""幂等场景：重复提交只生效一次、重放结果一致、同键不同内容明确拒绝。"""

import asyncio

import pytest

from tests.conftest import headers

pytestmark = pytest.mark.asyncio


async def test_replay_returns_identical_result_with_single_effect(client, app):
    """重复提交：同一幂等键重放返回与首次一致的结果，且只产生一次实际效果。"""
    first = await client.post(
        "/api/v2/orders", json={"item": "book", "quantity": 2}, headers=headers("c1", "idem-1")
    )
    assert first.status_code == 201

    replay = await client.post(
        "/api/v2/orders", json={"item": "book", "quantity": 2}, headers=headers("c1", "idem-1")
    )
    assert replay.status_code == first.status_code
    assert replay.json() == first.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert app.state.order_service.effect_count == 1


async def test_concurrent_duplicates_collapse_to_one_effect(make_client):
    """并发重复：同一幂等键并发提交，只产生一次实际效果，响应完全一致。"""
    from app.config import GovernanceConfig

    app, client = await make_client(
        GovernanceConfig(rate_limit_capacity=100, rate_limit_refill_per_second=20.0)
    )
    async with client:
        async def one():
            return await client.post(
                "/api/v2/orders", json={"item": "pen"}, headers=headers("c2", "idem-dup")
            )

        responses = await asyncio.gather(*[one() for _ in range(10)])
    bodies = {r.content for r in responses}
    assert len(bodies) == 1  # 所有响应逐字节一致
    assert all(r.status_code == 201 for r in responses)
    assert app.state.order_service.effect_count == 1


async def test_same_key_with_different_body_is_rejected(client, app):
    """键冲突：同一幂等键配不同请求内容必须明确拒绝，不当新请求也不当旧请求。"""
    first = await client.post(
        "/api/v2/orders", json={"item": "book"}, headers=headers("c3", "idem-conflict")
    )
    assert first.status_code == 201

    conflict = await client.post(
        "/api/v2/orders", json={"item": "different"}, headers=headers("c3", "idem-conflict")
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    # 没有产生新的实际效果
    assert app.state.order_service.effect_count == 1


async def test_missing_idempotency_key_is_rejected(client):
    """缺少幂等键：明确拒绝并给出可区分错误码。"""
    r = await client.post(
        "/api/v2/orders", json={"item": "book"}, headers={"X-Client-Id": "c4"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "idempotency_key_required"


async def test_different_keys_are_independent(client, app):
    """不同幂等键互不影响，各自产生一次实际效果。"""
    for i in range(3):
        r = await client.post(
            "/api/v2/orders", json={"item": "x"}, headers=headers("c5", f"idem-ind-{i}")
        )
        assert r.status_code == 201
    assert app.state.order_service.effect_count == 3
