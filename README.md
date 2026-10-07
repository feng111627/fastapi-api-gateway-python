# FastAPI 请求治理网关（限流 / 幂等 / 版本）

在普通 FastAPI 业务之外加了一层面向外部的“请求治理”中间件，让同一批接口在
**并发、重复提交、版本切换**下都能得到稳定一致、可解释的结果。三层能力由同一个
纯 ASGI 中间件按固定顺序执行，层与层之间**不能互相绕过**：

```
请求 → 1. 版本判定 → 2. 调用方限流 → 3. 幂等控制 → 业务路由
```

- 未知版本在最前面就被拒绝，不会被后续的 429 / 幂等重放掩盖；
- 限流先于幂等：拿不到令牌的请求不会登记幂等键，配额语义清晰；
- 同一请求上的版本信号、限流计数、幂等重放三类响应头同时存在、互不覆盖。

## 目录结构

```
app/
  config.py       # 配额、恢复速率、版本集合等配置
  errors.py       # 统一错误信封与可区分错误码
  versioning.py   # 版本解析（路径前缀 / X-API-Version 头 / 默认版本）
  ratelimit.py    # 按调用方维度的并发安全令牌桶
  idempotency.py  # 幂等键存储（去重 / 指纹冲突 / 重放 / 并发占位）
  middleware.py   # 串联三层的 ASGI 治理中间件 + 响应头
  routers/orders.py  # 示例业务：订单（v1/v2 两种响应结构，同一份路由）
  main.py         # 应用组装（支持注入时钟/存储，便于测试）
tests/            # 三类场景 20 个用例，运行日志带业务标签与逐条结果
main.py           # uvicorn 入口
```

## 快速开始

```bash
uv sync
uv run uvicorn main:app --reload

# 当前版本 v2
curl -s -X POST http://127.0.0.1:8000/v2/orders \
  -H 'Content-Type: application/json' -H 'X-Client-Id: demo' \
  -d '{"product":"book","quantity":2}'
# 旧方式（无版本号 = 默认 v1，仍可用但带弃用信号）
curl -s -X POST http://127.0.0.1:8000/orders \
  -H 'Content-Type: application/json' -H 'X-Client-Id: demo' \
  -d '{"product":"book","quantity":2}'
```

运行测试（日志会逐条打印用例所属业务与 PASS/FAIL）：

```bash
uv run pytest                      # 全部
uv run pytest -m ratelimit         # 只看并发限流
uv run pytest -m idempotency       # 只看重复提交
uv run pytest -m versioning        # 只看版本边界与联动
```

## 1. 限流：按调用方维度的令牌桶

### 配额与恢复规则

配置见 `app/config.py`：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `rate_capacity` | `5` | 每个调用方的令牌桶容量（突发上限） |
| `rate_refill_per_second` | `5.0` | 每秒自动恢复的令牌数 |

- 调用方身份取 `X-Client-Id` 请求头；缺省时回退 TCP 对端地址，再缺省为
  `anonymous`。**每个调用方一个独立桶**，互不影响。
- 采用**惰性补充**：只在请求到达时按“距上次更新的时间差”补令牌，不依赖后台
  定时器。因此额度恢复后**下一个请求立即放行，不会一直卡住**；服务空闲也不耗资源。
- 补充上限为桶容量，空闲再久也不会无限累积。
- “补充 + 扣减”在同一把 `asyncio.Lock` 内完成，**并发请求下配额不会被算超，
  也不会漏算**（测试用 20 个协程抢 5 个令牌，成功数严格等于 5）。
- 限流先于幂等执行，被限流的请求不占用幂等键。

### 拒绝时客户端能识别的信息

HTTP `429 Too Many Requests`，统一错误信封：

```json
{
  "error": {
    "code": "rate_limited",
    "message": "rate limit exceeded for caller",
    "details": {"client_id": "carol", "limit": 5, "retry_after_seconds": 1}
  }
}
```

响应头同时给出（放行响应上也有，客户端可提前感知余量）：

| 头 | 含义 |
| --- | --- |
| `RateLimit-Limit` | 桶容量 |
| `RateLimit-Remaining` | 本次处理后剩余整数令牌 |
| `RateLimit-Reset` | 恢复到满额所需秒数（满额时为 0） |
| `Retry-After` | 仅 429 时出现，建议等待秒数（向上取整，≥1） |

> 多实例部署时需把 `RateLimiter` 换成 Redis/共享存储上的原子令牌桶，接口不变。

## 2. 幂等提交

### 判定方式

- 写请求（`POST/PUT/PATCH/DELETE`）携带 `Idempotency-Key` 头即启用；不带则按普通请求处理。
- 幂等作用域为 **(调用方, 幂等键)**：不同调用方使用相同键互不影响。
- 请求指纹 = SHA-256(`方法 ∥ 版本 ∥ 路径 ∥ 查询串 ∥ 请求体`)。
- 判定结果：
  - **首次**：执行业务，缓存最终响应（状态码 + 安全响应头 + 响应体）；
  - **同键同指纹重放**：不再次执行业务，原样返回首次的状态码与响应体，并带
    `Idempotency-Replayed: true`；
  - **同键不同指纹**：返回 `409 idempotency_conflict`，**明确拒绝**，既不会当成
    新请求处理，也不会回放旧结果。请求体、查询串、路径、版本任一不同都会冲突；
  - **同键并发**：只有获得执行权的那一个请求进入业务，其余请求等待其完成后回放
    同一结果（测试用 10 个并发请求验证：业务只执行 1 次，9 次回放）；
  - 首次执行抛异常或返回 5xx 时**不写缓存**，允许客户端用同一键安全重试；
  - 条目默认保留 1 小时（`idempotency_ttl_seconds`），过期后同键视为新请求。

冲突响应示例：

```json
{
  "error": {
    "code": "idempotency_conflict",
    "message": "idempotency key was reused with a different request payload",
    "details": {"idempotency_key": "K-200", "client_id": "alice"}
  }
}
```

使用示例：

```bash
for i in 1 2; do
  curl -s -X POST http://127.0.0.1:8000/v2/orders \
    -H 'Content-Type: application/json' -H 'X-Client-Id: demo' \
    -H 'Idempotency-Key: order-2026-10-08-001' \
    -d '{"product":"book","quantity":1}'
done   # 两次返回完全一致（含同一订单号），只创建一笔订单
```

## 3. 版本治理

### 版本指定方式与兼容范围

| 方式 | 示例 | 说明 |
| --- | --- | --- |
| 路径前缀（优先） | `/v2/orders` | 中间件进入路由前剥离前缀，路由只注册一份 |
| 请求头 | `X-API-Version: v2` | 不带路径前缀时生效 |
| 都不带 | `/orders` | 回落默认版本 **v1** |

当前支持版本：`v1`、`v2`（见 `app/config.py` 的 `supported_versions`）。

- **v1（兼容）**：默认版本，旧调用方式继续可用。响应带可观察的过时信号：
  `X-API-Version: v1`、`X-API-Deprecated: true`、`Deprecation: true`、
  `Sunset: <RFC 1123 时间>`、`Link: </v2>; rel="successor-version"`；
  服务端同时打 `WARNING` 日志，便于统计存量旧版流量。v1 返回旧的扁平结构
  （`order_id/qty`）。
- **v2（当前）**：返回新结构（`api_version/order/location`），无弃用头。
- **不存在的版本**：返回 `400 version_not_supported`（而不是路由层面的 404），
  错误体给出请求的版本、来源（`path`/`header`）、支持版本列表与最新版本；
  头值格式非法（如 `banana`）会额外给出期望格式 `v<number>`。

```json
{
  "error": {
    "code": "version_not_supported",
    "message": "the requested API version is not supported",
    "details": {
      "requested_version": "v3",
      "source": "path",
      "supported_versions": ["v1", "v2"],
      "latest_version": "v2"
    }
  }
}
```

### 版本与限流 / 幂等如何一起生效

执行顺序固定为 **版本 → 限流 → 幂等**，因此：

- 未知版本请求即使在配额打满时也返回 `version_not_supported` 而不是 429，
  且不会出现限流头（对应测试 `test_version_check_happens_before_rate_limit`）；
- 未知版本 + 已存在的幂等键，返回版本错误而不是旧结果重放
  （`test_version_check_happens_before_idempotency`）；
- 幂等指纹包含版本号，同一个键跨版本（v1↔v2）提交会被判为冲突；
- 正常请求同时携带三类头：`X-API-Version`、`RateLimit-*`、
  `Idempotency-Replayed`（重放时）。

### 如何验证

```bash
# 1) v1 可用但过时（观察 Deprecation/Sunset/X-API-Deprecated 头）
curl -i -X POST http://127.0.0.1:8000/orders \
  -H 'Content-Type: application/json' -H 'X-Client-Id: demo' \
  -d '{"product":"book"}' | grep -Ei 'HTTP/|X-API|Deprecation|Sunset|Link'

# 2) v2 当前版本
curl -i http://127.0.0.1:8000/v2/orders -H 'X-Client-Id: demo' | grep X-API

# 3) 不存在的版本 -> 400 version_not_supported（区别于 404）
curl -i http://127.0.0.1:8000/v3/orders

# 4) 自动测试覆盖版本边界与三层联动
uv run pytest -m versioning -v
```

## 错误信封与错误码一览

所有治理层拒绝均为 `{"error": {"code", "message", "details"}}`：

| code | HTTP | 触发条件 |
| --- | --- | --- |
| `version_not_supported` | 400 | 请求了未知/非法 API 版本 |
| `rate_limited` | 429 | 调用方令牌耗尽 |
| `idempotency_conflict` | 409 | 同幂等键携带不同请求内容（含跨版本/跨路径） |

## 测试与日志

- `tests/test_ratelimit.py`：配额头、**并发不超算不漏算**、429 可识别、
  按时恢复不卡死、调用方隔离（5 例）。
- `tests/test_idempotency.py`：重放一致（含 422 响应）、同键不同内容 409、
  跨版本/路径冲突、调用方隔离、**并发 10 请求只执行 1 次**（6 例）。
- `tests/test_versioning.py`：路径/头/默认三种版本、弃用信号、未知版本区分、
  版本先于限流/幂等、限流与幂等共存、弃用日志（9 例）。

运行时日志示例（每个用例都能看到业务归属和逐条结果）：

```
[INFO] tests: ==== 开始用例 [业务:并发限流] tests/test_ratelimit.py::...
[INFO] tests: >>>> 用例结果 PASS :: tests/test_ratelimit.py::...
```

## 备注

- 限流器与幂等存储当前为进程内实现，适合单实例演示；水平扩展时替换为带原子
  CAS/Lua 的外部存储即可，中间件与业务代码无需改动。
- `/health`、`/docs`、`/openapi.json` 等辅助端点只做版本门控，不参与限流/幂等。
