# FlowGrid Agent Memory v0.1 本地验收

## 结论

通用 Agent Memory 的首期本地代码已经完成。当前候选覆盖治理内核、候选流水线、
当前状态解析、证据检索、上下文编译、Python 门面、CLI、AML Add/Search、
Governed REST、MCP stdio、离线 wheel 和双门评测。

这是 v0.1 的本地开发验收证据。公开仓库、GitHub Release、部署与比赛提交是独立的
外部动作；本页也不证明托管服务、长期真实 Agent 收益或新的 AML 官方成绩。

## 产品要求矩阵

| 要求 | 当前证据 | 结论 |
| --- | --- | --- |
| 原始事件可追加、不可变、可追溯 | `messages` 保留原文，`raw_events` 提供不可变 locator；更新触发器和来源回指测试通过 | 通过 |
| 派生记忆与原始证据分层 | `memory_records` 只保存派生内容并引用 RawEvent；普通检索与 audit 分离 | 通过 |
| 完整治理状态语义 | candidate、confirmed、inferred、unknown、superseded、rejected、deleted 均有状态机和回归 | 通过 |
| 当前状态不会复活失效记忆 | 替代、拒绝、删除、冲突、时间有效性和 owner gate 测试通过 | 通过 |
| 来源、时间、权限和选择理由可核查 | governed search 与 ContextPack 保留公开 allowlist、opaque locator、状态依据和 `why_selected` | 通过 |
| 用户拥有确认权 | Agent、system、external、unknown 不能确认或治理当前真相；候选保持 unknown 并返回 owner gate | 通过 |
| 上下文预算与最小披露 | 字符预算按最终 canonical JSON 精确计算；token 路径要求宿主注入精确 counter；REST/MCP 不做估算 | 通过 |
| 隔离与隐私擦除 | 用户与 scope 授权、跨用户和跨 scope 拒绝、user-wide erase、并发读写回归通过 | 通过 |
| AML 兼容 | 旧 Add/Search 结构、同步写后读和历史基线保留；CLI 8/8、HTTP 31/31 | 通过 |
| 通用接口 | `FlowGridMemory`、本地 CLI、loopback REST 与 MCP stdio 共用治理内核 | 通过 |
| 可安装与可复现 | base wheel 无运行时依赖，MCP 仅为 extra；repo 外 fresh wheel 和官方 SDK stdio 通过 | 通过 |
| FLG 与 AML 双门 | product profile 的 core、local proxy 与 surface 全部通过；官方 artifact 单独保持 unverified | 通过 |
| 自然语言候选提取 | provider-neutral quote-anchored adapter；4 正例/4 负例的固定 DeepSeek 配置合成盲测 7/7 门通过 | 有界通过 |
| 真实 Agent 宿主 | Codex TUI 与 Hermes 各完成两次新会话的 governed-memory MCP continuation smoke | 有界通过 |
| Docker live image | Docker Desktop 29.6.2 完成真实 build、fail-closed、非 root、loopback REST 与 candidate-not-current smoke | 有界通过 |

## 接口与信任边界

- Python：`FlowGridMemory` 需要显式数据库路径，不暴露底层 DB 或通用 SQL。
- CLI：所有写入命令要求 `--db` 或 `--ephemeral`，doctor 对给定路径保持只读。
- REST：只绑定字面地址 `127.0.0.1`，身份与权限来自启动配置。异常协议、鉴权、
  scope、owner gate、erase 和关闭语义已通过独立复核。
- MCP：使用官方 Python SDK v2 与 stdio。只暴露 ingest、extract、current query 和
  context compile 四个工具。没有 transition、audit、raw、erase 或 admin 工具。
- SQLite：首次 schema bootstrap 在一个事务中完成。POSIX 主机上的协作进程还使用
  本地 advisory lock 串行化 preflight 与首次打开。

## 最终实跑证据

- MCP SDK 环境：共运行 375 项单测，373 项通过，2 项预期跳过。
- CLI self-check：8/8。
- AML Add/Search HTTP smoke：31/31。
- Governed REST：auth/REST 35/35，loopback smoke 14 项通过。
- MCP：官方 Client 与 repo 外 stdio subprocess 通过，4 个工具，0 个 resources、
  templates、prompts，stderr 无私密数据，子进程已回收。
- 迁移：32/32；八进程首次建库通过，三类真实进程退出故障完整回滚并可重试。
- product governance profile：pass，`failures=[]`，legacy floor 16/16。
- fresh wheel SHA-256：
  `3af2286a5c818762309d52fd3d8a58ffab9c336d07246bc4cccabf2a0954a890`。
- 独立 REST 与安装包/MCP 复核：P0、P1、P2 均为 0。
- `flg doctor --strict`：OK，无 pending patch、坏证据或索引漂移。
- Quote-anchored 合成盲测：4 个应提取项精确命中，4 个负例与虚构 token 未进入
  候选；来源均在输入批次，所有记录保持 candidate，全部 current 查询 abstain。
- Codex TUI 与 Hermes host smoke：各两次新会话、每次 6 个成功 MCP current 查询；
  替代、删除、拒绝、候选和缺失状态均按预期返回且无禁止值泄漏。
- Docker live：从当前 Dockerfile 构建 51,396,087-byte 镜像，基础镜像绑定 digest；
  默认无配置退出码 2，配置后以 UID/GID 10001 运行，健康、`no-store`、401、事件写入、
  candidate 提取及 candidate-not-current 六项运行检查通过，日志为空。临时容器和验证镜像已删除。

## 尚未被证明的范围

- 普通自然语言提取的生产质量。当前只有一个固定模型配置、一次小型英文合成盲测；
  quote anchor 不等于语义安全或解释正确。
- 真实 Agent runtime 的长期跨会话收益。Codex 与 Hermes 已通过短程合成 MCP smoke，
  尚未证明真实用户任务效果。
- 托管多租户、TLS、分布式授权、速率限制、静态加密和生产级公网边界。
- AML 下一公开周期的正式协议、提交结果或官方提分。
- GitHub Release、部署、比赛提交，以及外部用户对公开包的独立复现结果。
