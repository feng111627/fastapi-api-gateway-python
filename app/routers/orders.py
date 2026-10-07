"""示例业务：订单创建/查询。

路由只注册一份（/orders），版本前缀由治理中间件在进入路由前剥离，
具体命中的版本放在 ``request.scope["api_version"]``。

为体现"旧调用方式仍可用但结构过时"：v1 与无版本（默认 v1）返回旧的扁平结构，
v2 返回带版本标识与服务端生成编号的新结构；无论哪个版本，创建动作都由同一个
内存计数器产生副作用，便于在测试里验证"重复提交只产生一次实际效果"。
"""
from __future__ import annotations

import threading

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

router = APIRouter(tags=["orders"])


class OrderIn(BaseModel):
    product: str = Field(min_length=1)
    quantity: int = Field(default=1, ge=1)


_ORDER_LOCK = threading.Lock()
_ORDER_SEQ = 0
_ORDERS: list[dict] = []


def _create_order_record(payload: OrderIn) -> dict:
    """真正产生副作用的唯一入口（加锁，保证编号不重复）。"""
    global _ORDER_SEQ
    with _ORDER_LOCK:
        _ORDER_SEQ += 1
        record = {"id": _ORDER_SEQ, "product": payload.product, "quantity": payload.quantity}
        _ORDERS.append(record)
    return record


@router.post("/orders", status_code=201)
async def create_order(payload: OrderIn, request: Request):
    version = request.scope.get("api_version", "v1")
    record = _create_order_record(payload)
    if version == "v2":
        return {
            "api_version": "v2",
            "order": record,
            "location": f"/v2/orders/{record['id']}",
        }
    # v1（含无版本默认流量）：旧的扁平结构
    return {
        "order_id": record["id"],
        "product": record["product"],
        "qty": record["quantity"],
    }


@router.get("/orders")
async def list_orders(request: Request):
    version = request.scope.get("api_version", "v1")
    if version == "v2":
        return {"api_version": "v2", "orders": list(_ORDERS)}
    return {"orders": [{"order_id": o["id"], "product": o["product"], "qty": o["quantity"]}
                       for o in _ORDERS]}


def reset_store() -> None:
    """清空订单（测试用）。"""
    global _ORDER_SEQ
    with _ORDER_LOCK:
        _ORDER_SEQ = 0
        _ORDERS.clear()
