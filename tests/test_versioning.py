"""版本场景：旧版可用但可观察地过时、不存在的版本可区分、版本与限流/幂等协同。"""

import pytest

from tests.conftest import headers

pytestmark = pytest.mark.asyncio


async def test_v1_still_works_but_marked_deprecated(client):
    """旧版兼容：v1 请求正常生效，但响应带弃用标记可被调用方观察。"""
    r = await client.post(
        "/api/v1/orders", json={"item": "legacy"}, headers=headers("v1c", "v1-key")
    )
    assert r.status_code == 201
    assert r.headers["Deprecation"] == "true"
    assert "Sunset" in r.headers
    assert "deprecated" in r.headers["X-API-Warn"]
    assert r.json()["order_id"].startswith("order-")  # 旧版响应结构保持原样


async def test_v2_is_current_and_not_deprecated(client):
    """当前版本：v2 正常生效且无弃用标记。"""
    r = await client.post(
        "/api/v2/orders", json={"item": "modern"}, headers=headers("v2c", "v2-key")
    )
    assert r.status_code == 201
    assert "Deprecation" not in r.headers
    assert r.json()["version"] == "v2"


async def test_unsupported_version_gives_distinguishable_error(client):
    """版本边界：请求不存在的版本返回可区分的错误并列出受支持版本。"""
    r = await client.post(
        "/api/v9/orders", json={"item": "x"}, headers=headers("v9c", "v9-key")
    )
    assert r.status_code == 404
    body = r.json()["error"]
    assert body["code"] == "unsupported_version"
    assert body["supported_versions"] == ["v1", "v2"]


async def test_unsupported_version_bypasses_neither_limiter_nor_business(client, app, config):
    """协同生效：不存在的版本不消耗限流额度、不触碰幂等存储、不产生业务效果。"""
    for i in range(10):
        r = await client.post(
            "/api/v9/orders", json={"item": f"x{i}"}, headers=headers("v9g", f"v9g-{i}")
        )
        assert r.status_code == 404
    assert app.state.order_service.effect_count == 0
    # 额度未被消耗：同一调用方在合法版本上仍有完整配额
    for i in range(config.rate_limit_capacity):
        ok = await client.post(
            "/api/v2/orders", json={"item": f"ok{i}"}, headers=headers("v9g", f"ok-{i}")
        )
        assert ok.status_code == 201


async def test_governance_applies_on_every_supported_version(client, app):
    """协同生效：v1 与 v2 都同样经过限流与幂等，谁也不绕过谁。"""
    # v1 上幂等重放
    first = await client.post(
        "/api/v1/orders", json={"item": "a"}, headers=headers("vg", "vg-key")
    )
    replay = await client.post(
        "/api/v1/orders", json={"item": "a"}, headers=headers("vg", "vg-key")
    )
    assert replay.json() == first.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    # v1 上同键不同内容同样冲突
    conflict = await client.post(
        "/api/v1/orders", json={"item": "b"}, headers=headers("vg", "vg-key")
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    # v1 与 v2 共享同一调用方限流额度（按调用方维度，不按版本维度）
    assert "X-RateLimit-Limit" in first.headers
    assert app.state.order_service.effect_count == 1
