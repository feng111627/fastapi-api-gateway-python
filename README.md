# Governed Orders API

基于 FastAPI 的对外服务，在业务接口前加了一层请求治理：**按调用方限流、幂等提交、接口版本区分**。
三类能力在同一条管线上按固定顺序生效：**版本路由 → 限流 → 幂等 → 业务**，互不绕过。

## 运行

```bash
uv sync
uv run python main.py        # 启动服务
uv run pytest                # 运行全部测试（-v 输出各业务场景与用例通过情况）
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/orders` | 旧版下单（已过时，仍可用） |
| POST | `/api/v2/orders` | 当前版下单 |

请求头：

- `X-Client-Id`：调用方标识，限流按此维度计数；缺省为 `anonymous`。
- `X-Idempotency-Key`：幂等键，下单接口必填。

错误响应统一为 `{"error": {"code": ..., "message": ...}}`，错误码可区分：
`rate_limit_exceeded` / `idempotency_key_required` / `idempotency_conflict` /
`invalid_json` / `invalid_order` / `unsupported_version`。

## 限流：配额与恢复规则

- 算法：按调用方（`X-Client-Id`）独立令牌桶。默认容量 5 个令牌，按 1 个/秒匀速回补
  （见 `app/config.py` 的 `GovernanceConfig`，可用 `create_app(GovernanceConfig(...))` 调整）。
- 判定在同一把异步锁内完成「按经过时间回补 → 扣减」，并发请求不会被算超（放行数不超过配额）
  也不会漏算（每个放行请求恰好消耗一个令牌）。
- 超限响应：`429` + `code=rate_limit_exceeded`，并带客户端可识别的限流头：
  - `X-RateLimit-Limit`：配额上限；`X-RateLimit-Remaining`：剩余额度；
    `X-RateLimit-Reset`：预计回满时间；`Retry-After`：建议重试秒数。
- 恢复：令牌随时间自动回补，额度恢复后请求正常放行，不会一直卡住
  （测试 `test_quota_recovers_after_refill` 验证）。
- 不同调用方互不影响（`test_quota_is_per_caller`）；不存在的版本不消耗额度
  （`test_unsupported_version_bypasses_neither_limiter_nor_business`）。

## 幂等：判定方式

- 下单必须携带 `X-Idempotency-Key`，缺失返回 `400 idempotency_key_required`。
- 指纹 = 请求体原始字节的 SHA-256。判定规则：
  - **键不存在** → 正常执行，记录「指纹 + 响应」，只产生一次实际效果；
  - **键存在且指纹相同** → 重放：返回与首次完全一致的状态码与响应体，
    并带 `Idempotency-Replayed: true`；并发在途的重复请求会等待首个请求完成后再重放；
  - **键存在但指纹不同** → 拒绝：`409 idempotency_conflict`，
    既不当成新请求执行，也不返回旧请求的结果。
- 验证：重放前后 `OrderService.effect_count` 不变（只生效一次），
  见 `tests/test_idempotency.py`。

## 版本：兼容范围与验证方法

- 受支持版本：`v1`、`v2`（`app/versioning.py` 的 `SUPPORTED_VERSIONS`）。
- `v1` 已过时但**仍可用**：响应带 `Deprecation: true`、`Sunset` 与 `X-API-Warn` 头，
  调用方可据此观察到弃用；OpenAPI 中 v1 路由标记 `deprecated`。v1 响应结构保持旧版原样。
- `v2` 为当前版本，无弃用标记。
- 请求不存在的版本（如 `/api/v9/...`）：`404` + `code=unsupported_version`，
  响应体列出 `supported_versions`；此类请求在版本路由阶段即被拒绝，
  不消耗限流额度、不触碰幂等存储、不产生业务效果。
- 限流与幂等对所有受支持版本同样生效（额度按调用方维度共享，不按版本拆分）。
- 验证：运行 `uv run pytest tests/test_versioning.py -v`，
  覆盖旧版可用且可观察、当前版本、不存在版本、版本与限流/幂等协同四类边界。

## 测试覆盖

```
tests/test_rate_limit.py   并发限流不超放、超限信息、按调用方隔离、额度恢复
tests/test_idempotency.py  重放一致且单次生效、并发重复收敛、键冲突拒绝、缺键拒绝
tests/test_versioning.py   旧版兼容与弃用标记、不存在版本、版本与限流/幂等协同
```

`uv run pytest` 的详细日志（`addopts = "-v"`）逐条列出各业务场景用例及通过情况。
