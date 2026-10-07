"""公共 fixture 与测试日志。

每个测试使用全新的应用/限流器/幂等存储/订单存储，互不污染。
日志里会打印每个用例的归属业务（marker）、用例名与通过/失败结果，
便于从运行输出直接看出覆盖范围与各用例情况。
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
from starlette.testclient import TestClient

from app.config import GovernanceConfig
from app.main import create_app
from app.routers.orders import reset_store

logging.getLogger("httpx").setLevel(logging.WARNING)


MARKER_LABELS = {
    "ratelimit": "业务:并发限流",
    "idempotency": "业务:幂等提交",
    "versioning": "业务:版本治理",
}


class FakeClock:
    """单调时钟替身：限流补充的时间完全由测试掌控。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def app(clock):
    reset_store()
    return create_app(monotonic=clock)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def gated_app(clock):
    """带一个可阻塞的测试端点 /slow-job，用于制造同键并发窗口。

    限流配额放大到 100，确保该用例考察的是幂等并发去重，
    而不是被前置限流拦截（限流在幂等之前，属另一用例的覆盖范围）。
    """
    reset_store()
    state = {"runs": 0}
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_job():
        state["runs"] += 1
        entered.set()
        await release.wait()
        return {"executed": True, "runs": state["runs"]}

    cfg = GovernanceConfig(rate_capacity=100, rate_refill_per_second=100.0)
    app = create_app(
        cfg=cfg,
        monotonic=clock,
        extra_routes=[{"path": "/slow-job", "endpoint": slow_job, "methods": ["POST"]}],
    )
    app.state.gated_state = state
    app.state.gated_entered = entered
    app.state.gated_release = release
    return app


@pytest.fixture
def gated_client(gated_app):
    with TestClient(gated_app) as c:
        yield c


def pytest_runtest_logstart(nodeid, location):
    markers = [m for m in MARKER_LABELS if nodeid.startswith(f"tests/test_{m}.py")]
    label = MARKER_LABELS.get(markers[0], "其他") if markers else "其他"
    import logging

    logging.getLogger("tests").info("==== 开始用例 [%s] %s", label, nodeid)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call":
        import logging

        logging.getLogger("tests").info(
            ">>>> 用例结果 %s :: %s",
            "PASS" if report.passed else "FAIL",
            item.nodeid,
        )
