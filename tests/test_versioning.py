"""版本治理：路径/头/默认三种指定方式、弃用可观察、未知版本可区分、三层联动。"""
from __future__ import annotations

import logging

import pytest

pytestmark = pytest.mark.versioning
logger = logging.getLogger("tests.versioning")


def test_path_version_prefix_is_supported(client):
    """/v2/... 与无版本都能命中同一组路由，响应结构随版本变化。"""
    logger.info("验证: 路径前缀版本 + 新旧响应结构")
    r1 = client.post("/orders", json={"product": "book"}, headers={"X-Client-Id": "u"})
    r2 = client.post("/v2/orders", json={"product": "book"}, headers={"X-Client-Id": "u"})
    assert r1.status_code == r2.status_code == 201
    body1, body2 = r1.json(), r2.json()
    assert set(body1) == {"order_id", "product", "qty"}
    assert set(body2) == {"api_version", "order", "location"}
    assert r1.headers["X-API-Version"] == "v1"
    assert r2.headers["X-API-Version"] == "v2"


def test_header_version_is_supported(client):
    """X-API-Version 头指定版本，不带路径前缀也可生效。"""
    logger.info("验证: X-API-Version 请求头")
    r = client.get("/orders", headers={"X-API-Version": "v2"})
    assert r.status_code == 200
    assert r.json()["api_version"] == "v2"
    assert r.headers["X-API-Version"] == "v2"


def test_default_version_is_v1_and_deprecated_but_works(client):
    """旧调用方式（无版本号）继续可用，但响应要显式发出过时信号。"""
    logger.info("验证: 默认版本=v1, 继续可用且 Deprecation/Sunset 可观察")
    r = client.post("/orders", json={"product": "legacy"}, headers={"X-Client-Id": "u"})
    assert r.status_code == 201  # 旧方式仍可用
    assert r.headers["X-API-Version"] == "v1"
    assert r.headers["X-API-Deprecated"] == "true"
    assert r.headers["Deprecation"] == "true"
    assert "Sunset" in r.headers  # 计划移除时间
    assert "successor-version" in r.headers["Link"]
    # v2 为当前版本，不带弃用信号
    fresh = client.get("/v2/orders")
    assert fresh.status_code == 200
    assert "X-API-Deprecated" not in fresh.headers
    assert "Deprecation" not in fresh.headers


def test_unknown_path_version_is_distinct_error(client):
    """请求不存在的版本：400 + version_not_supported，和 404 路由缺失明确区分。"""
    logger.info("验证: 未知路径版本 -> 400 version_not_supported (非 404)")
    r = client.get("/v3/orders")
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "version_not_supported"
    assert err["details"]["requested_version"] == "v3"
    assert err["details"]["source"] == "path"
    assert "v2" in err["details"]["supported_versions"]

    # 对照：存在的版本 + 不存在的资源路径 -> 404，错误类型不同
    not_found = client.get("/v2/does-not-exist")
    assert not_found.status_code == 404
    assert not_found.json().get("error", {}).get("code") != "version_not_supported"


def test_unknown_header_version_is_distinct_error(client):
    """头里写不存在或格式错误的版本，同样给可区分的错误。"""
    logger.info("验证: 未知/畸形头版本 -> 400 version_not_supported")
    r = client.post("/orders", json={}, headers={"X-API-Version": "v9"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "version_not_supported"
    assert r.json()["error"]["details"]["source"] == "header"

    r = client.post("/orders", json={}, headers={"X-API-Version": "banana"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "version_not_supported"
    assert "expected_format" in r.json()["error"]["details"]


def test_version_check_happens_before_rate_limit(client):
    """未知版本请求必须在限流之前就被拒绝：打满配额后它仍是版本错误而非 429。"""
    logger.info("验证: 版本判定先于限流，未知版本不会被 429 掩盖")
    headers = {"X-Client-Id": "vf"}
    for _ in range(5):
        client.post("/orders", json={"product": "x"}, headers=headers)
    assert client.post("/orders", json={"product": "x"},
                       headers=headers).status_code == 429
    r = client.post("/v9/orders", json={"product": "x"}, headers=headers)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "version_not_supported"
    assert "Retry-After" not in r.headers  # 没走到限流，自然没有限流头
    assert "RateLimit-Limit" not in r.headers


def test_version_check_happens_before_idempotency(client):
    """未知版本 + 幂等键：拒绝原因必须是版本错误，不能被幂等重放掩盖。"""
    logger.info("验证: 版本判定先于幂等")
    h = {"X-Client-Id": "vi", "Idempotency-Key": "VK-1"}
    ok = client.post("/v2/orders", json={"product": "a"},
                     headers={**h, "X-API-Version": "v2"})
    assert ok.status_code == 201
    bad = client.post("/v3/orders", json={"product": "a"},
                      headers={**h, "X-API-Version": "v3"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "version_not_supported"
    assert "Idempotency-Replayed" not in bad.headers


def test_rate_limit_and_idempotency_coexist(client):
    """同一条请求上限流头与幂等重放头同时正确出现，互不干扰。"""
    logger.info("验证: 限流信息与幂等重放共存")
    h = {"X-Client-Id": "coexist", "Idempotency-Key": "CO-1"}
    r1 = client.post("/v2/orders", json={"product": "a"}, headers=h)
    r2 = client.post("/v2/orders", json={"product": "a"}, headers=h)
    assert r1.status_code == r2.status_code == 201
    assert r2.headers["Idempotency-Replayed"] == "true"
    # 两次都过了限流链，都带有计数头；重放也消耗调用方令牌
    assert r1.headers["RateLimit-Remaining"] == "4"
    assert r2.headers["RateLimit-Remaining"] == "3"


def test_deprecated_version_is_logged(caplog, client):
    """弃用版本的使用必须在服务端日志中可观察。"""
    logger.info("验证: 访问 v1 会写 deprecated 警告日志")
    with caplog.at_level(logging.WARNING, logger="governance"):
        client.post("/orders", json={"product": "x"}, headers={"X-Client-Id": "log"})
    assert any("deprecated API version v1" in rec.message for rec in caplog.records)
