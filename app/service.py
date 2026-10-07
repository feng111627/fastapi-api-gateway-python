"""订单业务逻辑：用于观察幂等是否只产生一次实际效果。"""

import asyncio
from dataclasses import dataclass, field


@dataclass
class OrderService:
    """内存订单服务，effect_count 记录真实生效次数。"""

    effect_count: int = 0
    orders: dict = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def create_order(self, payload: dict) -> dict:
        """创建订单（一次真实效果）。"""
        async with self._lock:
            self.effect_count += 1
            order_id = f"order-{self.effect_count}"
            order = {
                "order_id": order_id,
                "item": payload.get("item"),
                "quantity": payload.get("quantity", 1),
            }
            self.orders[order_id] = order
            return order
