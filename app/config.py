"""治理相关配置。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class GovernanceConfig:
    """限流与幂等的可调参数。"""

    rate_limit_capacity: int = 5
    rate_limit_refill_per_second: float = 1.0
    idempotency_header: str = "X-Idempotency-Key"
    client_id_header: str = "X-Client-Id"
