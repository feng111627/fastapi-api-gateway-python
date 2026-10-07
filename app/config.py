"""运行期治理配置（配额、窗口、版本集合等）。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GovernanceConfig:
    # 限流：令牌桶参数
    rate_capacity: int = 5
    rate_refill_per_second: float = 5.0
    # 幂等结果保留秒数
    idempotency_ttl_seconds: float = 3600.0
    # 版本
    supported_versions: tuple[str, ...] = ("v1", "v2")
    default_version: str = "v1"
    deprecated_versions: tuple[str, ...] = ("v1",)
    latest_version: str = "v2"


config = GovernanceConfig()
