"""幂等提交：重复请求单次生效、重放一致、同键不同内容明确拒绝、并发只执行一次。"""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

pytestmark = pytest.mark.idempotency
logger = logging.getLogger("tests.idempotency")


def _headers(key: str, caller: str = "idem-user", version: str | None = None) -> dict[str, str]:
    h = {"X-Client-Id": caller, "Idempotency-Key": key}
    if version:
        h["X-API-Version"] = version
    return h


def test_same_key_replays_first_response_with_single_effect(client):
    """同键重复两次：只创建一单，重放状态码/响应体与首次完全一致。"""
    logger.info("验证: 同键重放一致 + 副作用只发生一次")
    payload = {"product": "phone", "quantity": 1}
    r1 = client.post("/v2/orders", json=payload, headers=_headers("K-100", version="v2"))
    r2 = client.post("/v2/orders", json=payload, headers=_headers("K-100", version="v2"))
    assert r1.status_code == r2.status_code == 201
    assert r1.json() == r2.json()
    assert r1.json()["order"]["id"] == r2.json()["order"]["id"]
    assert r2.headers["Idempotency-Replayed"] == "true"
    assert "Idempotency-Replayed" not in r1.headers

    orders = client.get("/v2/orders", headers={"X-API-Version": "v2"}).json()["orders"]
    assert len(orders) == 1
    logger.info("两次请求后订单数=%d, 重放头=%s", len(orders),
                r2.headers.get("Idempotency-Replayed"))


def test_replay_matches_even_first_failure_semantics(client):
    """重放连首次的业务拒绝（422 校验失败）也要保持一致。"""
    logger.info("验证: 首次 422 的响应同样被缓存并原样重放")
    bad_payload = {"product": "", "quantity": -1}  # 两个字段都非法
    r1 = client.post("/orders", json=bad_payload, headers=_headers("K-422"))
    r2 = client.post("/orders", json=bad_payload, headers=_headers("K-422"))
    assert r1.status_code == r2.status_code == 422
    assert r1.json() == r2.json()
    assert r2.headers["Idempotency-Replayed"] == "true"

    orders = client.get("/v2/orders", headers={"X-API-Version": "v2"}).json()["orders"]
    assert orders == []  # 4xx 不产生订单，但结果被缓存


def test_same_key_different_payload_is_rejected(client):
    """同键 + 不同请求体必须 409 明确拒绝，既不新建也不当旧结果重放。"""
    logger.info("验证: 同键不同内容 -> 409 idempotency_conflict")
    base = _headers("K-200")
    r1 = client.post("/orders", json={"product": "a"}, headers=base)
    assert r1.status_code == 201

    conflict = client.post("/orders", json={"product": "TAMPERED"}, headers=base)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert conflict.json()["error"]["details"]["idempotency_key"] == "K-200"
    assert "Idempotency-Replayed" not in conflict.headers

    orders = client.get("/v2/orders", headers={"X-API-Version": "v2"}).json()["orders"]
    assert [o["product"] for o in orders] == ["a"]
    logger.info("冲突请求未产生副作用，409 与 429/400 可区分")


def test_same_key_different_path_or_version_is_conflict(client):
    """同键但指纹中的路径/版本不同，也必须按冲突处理。"""
    logger.info("验证: 同键跨版本/跨路径 -> 409")
    r1 = client.post("/v2/orders", json={"product": "a"},
                     headers=_headers("K-300", version="v2"))
    assert r1.status_code == 201

    # 同键改走 v1（响应结构都不同），不能当成同一提交
    r2 = client.post("/orders", json={"product": "a"}, headers=_headers("K-300"))
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "idempotency_conflict"

    # 查询串不同也算不同请求
    r3 = client.post(
        "/orders?source=ads", json={"product": "a"},
        headers=_headers("K-300", caller="other-caller"),  # 换调用方则独立
    )
    assert r3.status_code == 201  # 调用方维度隔离，不冲突


def test_idempotency_keys_are_scoped_per_caller(client):
    """不同调用方使用相同的键互不影响。"""
    logger.info("验证: 幂等键按调用方隔离")
    payload = {"product": "shared-key-item"}
    r1 = client.post("/orders", json=payload, headers=_headers("DUP", caller="user-A"))
    r2 = client.post("/orders", json=payload, headers=_headers("DUP", caller="user-B"))
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["order_id"] != r2.json()["order_id"]
    assert "Idempotency-Replayed" not in r2.headers


def test_concurrent_same_key_executes_exactly_once(gated_app):
    """同键并发：只有一个请求真正执行，其余等待并回放同一结果。"""
    logger.info("验证: 10 个并发同键请求, 业务只执行 1 次, 其余全部回放")

    async def scenario():
        state = gated_app.state.gated_state
        entered = gated_app.state.gated_entered
        release = gated_app.state.gated_release
        transport = httpx.ASGITransport(app=gated_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as ac:
            headers = {"X-Client-Id": "race", "Idempotency-Key": "CONC-1"}

            async def call():
                return await ac.post("/slow-job", json={"x": 1}, headers=headers)

            task = asyncio.create_task(call())
            await entered.wait()  # 等首个请求进入业务端点
            others_task = asyncio.gather(*[call() for _ in range(9)])
            # 给等待者一点时间登记到幂等存储；期间业务执行数必须保持 1
            await asyncio.sleep(0.05)
            assert state["runs"] == 1, state
            release.set()  # 放行首个请求 -> 等待者被唤醒并回放
            first = await task
            others = await others_task

            results = [first, *others]
            assert all(r.status_code == 200 for r in results)
            assert all(r.json()["runs"] == 1 for r in results)
            replays = [r for r in results
                       if r.headers.get("Idempotency-Replayed") == "true"]
            assert len(replays) == 9, len(replays)
            assert state["runs"] == 1
            return state["runs"], len(replays)

    runs, replays = asyncio.run(scenario())
    logger.info("业务实际执行=%d 次, 回放=%d 次", runs, replays)
