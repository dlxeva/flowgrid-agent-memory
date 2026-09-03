# Add / Search API Contract

Reference sources checked on 2026-08-06:

- 官方 API 指南：<https://agentmemories.ai/api-guide>（章节 `05 ADD / SEARCH CONTRACT`、`06 ERROR HANDLING`）
- 官方公开评测代码：<https://github.com/AML-memory/agent-memory-leaderboard>（README「Disclosed production parameters」）
- 官方赛事页：<https://agentmemories.ai/competition/>

This document records the published wire contract implemented by the repository.
Upstream requirements can change, so integrations should compare it with the
current official documentation before running a new evaluation.

## 1. 传输与地址

| 项 | 官方要求 |
| --- | --- |
| 接口地址 | 由参赛方自行配置，**请求/响应格式固定，不随 URL 路径变化** |
| 可达性 | 必须能从评测环境访问；生产建议 HTTPS |
| 禁止 | URL 内含凭据；指向私有 / 回环 / 链路本地地址 |
| 写入语义 | **同步**：记忆写入完成后 Add 才返回 HTTP 200 |
| 正式评测 top_k | 100（平台按返回顺序最多读取 top_k 条） |
| 检索范围 | Add 与 Search 必须使用完全相同的 `user_id` |

## 2. 鉴权与健康检查

- 支持 `Token`、`Bearer`、`X-Api-Key`；`none` 仅用于公开 smoke。
- Health：**无需鉴权的 GET**，返回任意 2xx 即视为正常。
- 未单独配置 Health 地址时，正式任务检查与 Add **同源的 `/health`**。

## 3. POST Add

平台每个来源会话默认调用一次 Add；超过 **20 条消息或 2000 词**时在最近的完整消息或句子边界分段。

请求（只包含下列字段）：

```json
{
  "request_id": "eval:run_abc123:locomo_refined:conv-0:chunk-0",
  "messages": [{ "role": "user", "timestamp": 1704067200000, "content": "memory text" }],
  "user_id": "eval:run_abc123:locomo:conv-0",
  "session_id": "eval:run_abc123:sample:0"
}
```

| 字段 | 要求 |
| --- | --- |
| `request_id` | 必填。唯一标识；成功响应必须**原样返回** |
| `messages` | 必填。按原顺序；每条含 `role` 与非空 `content`；`timestamp` 可选，Unix **毫秒** |
| `user_id` | 必填。Search 唯一使用的检索范围标识 |
| `session_id` | 必填。标识来源会话，可用于组织记忆，**但不作为 Search 的筛选条件** |
| 不发送 | `metadata`、`app_id`、`agent_id`、`async_mode` |

响应（HTTP 200，写入完成且**立即可检索**后才能返回）：

```json
{ "success": true, "request_id": "…", "user_id": "…", "session_id": "…" }
```

- `success` 必须是布尔 `true`；三个 ID 必须与请求完全一致。
- 内部可异步处理，但接口必须等待完成再返回。
- **禁止**返回 HTTP 202 / task ID / 状态查询地址；无需 `memory_ids`。

## 4. POST Search

```json
{
  "query": "Which answer best matches the memory?",
  "options": ["A. First answer", "B. Second answer"],
  "user_id": "eval:run_abc123:locomo:conv-0",
  "top_k": 100
}
```

| 字段 | 要求 |
| --- | --- |
| `query` | 必填。按原文检索，**不得替换为最终答案**，不得使用金标 |
| `options` | 可选。选择题传入字符串数组；开放题不发送 |
| `user_id` | 必填。**只能**在该 user_id 范围内检索 |
| `top_k` | 必填。返回数量不得超过该值；正式外部评测固定 100 |
| 不发送 | `filters`、`rerank`、`keyword_search` |

响应（HTTP 200）：

```json
{ "data": [{ "id": "mem_1", "content": "remembered fact text", "score": 0.87, "created_at": "2026-07-01T12:00:00Z" }] }
```

| 字段 | 要求 |
| --- | --- |
| `data` | 必填数组。**不要加 `items` 包装层，也不要直接返回顶层数组**；无结果返回空数组 |
| `id` | 必填，非空字符串，稳定标识 |
| `content` | 必填，非空字符串。**直接提供给统一回答模型** |
| `score` | 可选数值，越大越相关 |
| `created_at` | 可选 |
| 其他字段 | 只读取上述字段，未声明字段被忽略 |

- 平台**保留接口返回的顺序**，最多读取 top_k 条 → 排序必须在返回前完成。

## 5. 错误处理

平台业务错误格式 `{"detail":{"reason":"..."}}`；字段校验失败用 HTTP 422 返回结构化明细。

| HTTP | 类型 | 平台行为 |
| --- | --- | --- |
| 400 / 422 | 格式错误 | 不自动重试 |
| 401 | 认证失败 | 不自动重试 |
| 403 | 访问被拒绝（含拒绝该 user_id） | 不自动重试 |
| 404 | 资源不存在 | 现行同步规范**不包含 Add Status 查询** |
| 409 | 状态冲突 | Add 有限重试；**Search 遇 409 不重试** |
| 408 / 425 | 暂时不可用 | Add / Search 均退避重试 |
| 429 | 限流 / 配额 | 有限重试 |
| 5xx | 临时异常 | 自动退避重试 |

- 自动重试范围：Add = {408,409,425,429,500,502,503,504}；Search = 同上但**不含 409**。
- **格式错误立即终止**：即使 HTTP 200，只要 Add 未返回 `success=true` / 三个 ID 不对，或 Search 未返回 `data` 数组 / 某条缺 `id`/`content`，当前阶段立即失败。

## 5.1 本实现的校验矩阵（2026-08-07 收紧）

原则：**严进宽容**——官方声明为必填的字段一律强校验且不做隐式转换；
官方未声明的字段一律忽略而不报错。理由是评测端"格式错误立即终止"，
静默转换（例如把 `"100"` 当成 100、把 `10.5` 截成 10）会让线上用了一个
我们自己编出来的值却毫无提示，比直接 422 危险得多。

| 字段 | 判定 | 拒绝样例 | 状态码 |
| --- | --- | --- | --- |
| `request_id` / `user_id` / `session_id` | 必填，非空字符串 | 缺失、`""`、`"  "`、非字符串 | 422 |
| `messages` | 必填，非空数组，元素为对象 | `[]`、`{}`、`[1]` | 422 |
| `messages[].role` | **必填**，非空字符串（取值不限于 user/assistant） | 缺失、`""`、`"  "`、`None`、`1`、`True` | 422 |
| `messages[].content` | 必填，非空字符串 | `""`、`"  "`、非字符串 | 422 |
| `messages[].timestamp` | 可选；给了必须是整数毫秒 | `"1704067200000"`、`1704067200000.7`、`True`、`NaN`、`inf` | 422 |
| `messages[].timestamp`（无损浮点） | 接受 | `1704067200000.0` → `1704067200000` | 200 |
| `query` | 必填，非空字符串 | 缺失、`""` | 422 |
| `top_k` | **必填**，必须是真整数 `int` | 缺失、`None`、`"100"`、`100.0`、`10.5`、`True`、`[100]` | 422 |
| `top_k` 越界 | `< 0` 拒绝；`> top_k_max` **钳制不报错** | `-1` → 422；`10**6` → 钳到上限 | 422 / 200 |
| `options` | 可选，字符串数组 | `[1,2]`、`"A"` | 422 |
| 未声明的额外字段 | 顶层与 `messages[]` 内**一律忽略** | `metadata` / `app_id` / `rerank` / `name` | 200 |

几点需要说明清楚的判断：

- **`role` 从"可选"改为"必填"**：官方请求样例里 `role` 与 `content` 同级出现，
  且文字表述为"每条含 `role` 与非空 `content`"。此前实现把它当可选（缺失时补 `""`），
  属于比官方更宽松，会掩盖上游拼装错误。现按官方字面收紧。
- **`top_k` 不再有服务端默认值**：官方把 `top_k` 列为必填、正式评测固定 100。
  此前缺失时会回落到 `top_k_default=10`，一旦评测端漏传，我们会安静地只返回 10 条，
  排查起来极难。现在缺失即 422，让问题在第一时间暴露。
- **`top_k` 超上限只钳制不拒绝**：这是与上一条相反方向的取舍。评测端固定传 100，
  而 `top_k_max` 默认也是 100；万一对方传更大值，返回少一些结果远好过整批失败。
- **小数一律拒绝而非截断**：`10.5 → 10` 和 `1704067200000.7 → 1704067200000` 都属于
  静默丢失信息。无损的整数浮点（`100.0`）在 `timestamp` 上接受，在 `top_k` 上仍拒绝，
  因为 `top_k` 的类型在官方样例里是明确的整数字面量，没有放宽的理由。

### Add 幂等语义（官方未声明，本实现显式选择）

幂等键为 **(request_id, user_id)**：

- 同键重复提交 → 返回 200 与相同的三个 ID，**不重复落库**。
- 同键但 `messages` 内容不同 → **首次写入生效（first write wins）**，
  第二次的正文既不覆盖也不追加，也不报 409。
  理由：官方把 `request_id` 定位为一次写请求的标识，重复到达最可能是网络重试；
  静默追加会在重试时污染记忆库并让召回指标虚高。
- 换 `user_id` 后同 `request_id` → 各自独立落库（幂等键是二元组）。

上述行为由 `tests/test_api_contract.py::TestAddContract` 中
`test_same_request_id_with_different_payload_is_first_write_wins` 与
`test_same_request_id_different_user_is_not_idempotent` 锁定。
若官方后续澄清应当以最后一次为准，只需改 `retriever._add_locked` 的分支，契约层无需变动。

## 6. 官方披露的运行参数（GitHub README）

| 项 | 值 |
| --- | --- |
| Full 任务超时 | 72 小时；检索 `top_k` = 100 |
| Add | 默认 **64 global workers**、单数据集 48 记录硬上限、**20 条消息分块**、1200s HTTP 超时 |
| Search | **32 workers**、1200s HTTP 超时、**6 次请求尝试**、最多 3 次失败记录重排、连续 5 次可重试 5xx 后自适应降并发 |
| 评测页可配 | Max add concurrency 16–64、Search concurrency 16–256、Top K |

## 7. 数据与隐私义务（官方原文要点）

- 评测数据及派生副本**只能用于完成当前任务**，不得用于模型训练、微调、产品分析、数据集重建或对外传播。
- 仅向必要人员开放，**避免记录不必要的请求正文**，任务完成后 **30 天内删除**；延长保留需事先书面同意。
- 禁止跨 `user_id` 返回记忆；`session_id` 只用于组织来源会话。
