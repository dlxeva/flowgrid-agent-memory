# FlowGrid Agent Memory

面向 AI Agent 的证据优先、受治理的本地通用记忆方案。

> **状态：** alpha。当前适合本地评估与集成试验；安全边界仍是单一可信宿主。

[English](README.md) · [安装](docs/INSTALL.md) ·
[受治理 REST](docs/REST_V1.md) · [MCP](docs/MCP.md) ·
[Owner 审核](docs/OWNER_REVIEW.md) · [本地安全](docs/LOCAL_SECURITY.md) ·
[数据生命周期](docs/DATA_LIFECYCLE.md) ·
[评测](docs/EVAL.md) · [本地验收](docs/ACCEPTANCE_V0_1.md)

FlowGrid Agent Memory 保存不可改写的原始证据，把提取结果与已确认事实分开，只解析
当前有效的受治理状态，再按授权和披露策略编译成供 Agent 使用的最小 ContextPack。
文本被存储或被模型提取，不代表它已经成为事实。

仓库仍保留作为代码底座的 AML Retriever v1.1 Add/Search 实现，但它现在是兼容层与
竞赛适配层，不是产品的信任或安全边界。

这个独立项目由 AML Retriever v1.1 的 commit
`cdae7dbd38d73eda33793b30017559bdfb75eff5` 演进而来。FlowGrid 的治理语义和
验证经验塑造了产品层；内部项目账本、运行轨迹、真实试验数据和比赛规划不进入公开包。

## 当前产品状态

- 产品版本：`0.1.0`
- AML Add/Search 适配层版本：`1.1.0`
- 基础运行环境：Python 3.11+、标准库、启用 FTS5 的 SQLite
- 已有接口：Python 门面、本地 CLI、带认证的 loopback REST v1，以及基于官方
  MCP SDK v2 的可选 stdio 适配器
- 当前支持边界：由一个可信宿主管理的本地部署，`user_id` 是该边界内的逻辑分区
- 当前不包含：托管多租户、公网服务或生产级远程安全边界

## FlowGrid 的核心记忆哲学

```text
不可变 RawEvent
      ↓ 显式提取
candidate / inferred 提案
      ↓ 用户、owner 或 policy 明确变更状态
confirmed 当前事实
      ↓ 可信 AccessContext + 披露策略
供 Agent 使用的最小 ContextPack
```

- **先来源，后表达**：派生记忆必须回指不可变原始事件，状态变化进入追加式审计链。
- **真值状态明确**：`candidate`、`inferred`、`unknown`、`confirmed`、
  `superseded`、`rejected`、`deleted` 不能混为一谈。
- **只解析当前状态**：被拒绝、被取代、已删除或仅为候选的正文不会重新混入普通上下文。
- **允许不知道**：没有可靠事实或存在冲突时，系统明确 abstain，并可要求 owner gate。
- **最小授权披露**：读取必须携带可信 `AccessContext`；audit 默认关闭，必须同时满足权限
  和获准用途。

## 安装与验证

在本地源码目录中：

```bash
python3 -m pip install .
flowgrid-memory --version
flowgrid-memory doctor --ephemeral
flowgrid-memory demo --ephemeral
```

产品 CLI 不会静默创建 `./aml.db`。`doctor` 与 `demo` 必须明确选择
`--db PATH` 或 `--ephemeral`；`review` 只接受已经存在的 `--db PATH`，路径拼错时
不会创建空库并误报“没有候选”。`doctor --db PATH` 只读检查：目标不存在或 schema
不兼容时只报告，不创建、不迁移。

离线构建 wheel 和在全新环境中验收的方法见 [docs/INSTALL.md](docs/INSTALL.md)。

## Python 门面

`FlowGridMemory` 是 CLI、REST 与 MCP 共用的稳定门面，与传输方式无关。数据库路径
必须显式给出，也不会向调用方暴露底层 DB 或通用 SQL 入口。

```python
from aml_retriever import AccessContext, FlowGridMemory, PERMISSION_READ

access = AccessContext(
    principal_id="trusted-local-owner",
    authority="owner",
    scopes={"project": "demo"},
    permissions=frozenset({PERMISSION_READ}),
    purpose="agent context",
    allowed_users=frozenset({"user-1"}),
)

with FlowGridMemory(db_path=":memory:") as memory:
    # ingest_raw_events -> extract_candidates/propose_memory ->
    # explicit transition_memory -> query_current/compile_context
    pass
```

默认零依赖提取器只识别整条消息形式的严格 `@flowgrid.memory/v1` 指令。普通自然语言
会如实产生 0 个提案，不假装具备 NLU。宿主可以注入自己的提取器，但核心仍负责绑定
scope/authority、校验证据 span，并只持久化 candidate。

## 本地 CLI

```bash
# 临时库，结束后自动清理
flowgrid-memory demo --ephemeral

# 只读检查，不会创建或迁移该路径
flowgrid-memory doctor --db /absolute/path/to/memory.db

# 在明确指定的持久库中运行受治理 demo
flowgrid-memory demo --db /absolute/path/to/memory.db

# 人类 Owner 审核队列；该命令会有意打印候选正文和来源证据
flowgrid-memory review \
  --db /absolute/path/to/memory.db \
  --user user-1 \
  --actor owner@example \
  --scope project=alpha
```

在同一命令中增加 `--record`、`--decision confirm|reject` 与 `--reason`，即可执行
一次明确的状态变更。决策回执不会重复输出记忆与证据正文。完整边界见
[本地 Owner 审核](docs/OWNER_REVIEW.md)。

demo 不打印记忆正文、数据库路径或内部 traceback，只证明这条状态链：

`RawEvent → candidate → unknown/owner gate → owner-confirmed current → authorized ContextPack`

## 受治理的本地适配器

REST 只接受字面地址 `127.0.0.1`。启动时必须选择可信本地进程边界或 bearer 认证，
并显式提供配置文件：

```bash
flowgrid-memory-rest --config /absolute/path/to/product.json
```

可选 MCP 适配器通过 stdio 暴露四个非管理工具。它不提供状态变更、audit、原始证据、
隐私擦除或管理员工具：

```bash
python3 -m pip install 'flowgrid-agent-memory[mcp]'
flowgrid-memory-mcp \
  --db /absolute/path/to/memory.db \
  --principal-config /absolute/path/to/mcp-principal.json
```

两种适配器都从可信启动配置读取身份、authority、权限、用户与 scope。请求正文和工具
参数不能替换这些值。完整边界见 [Governed REST v1](docs/REST_V1.md) 与
[MCP v2 local stdio](docs/MCP.md)。

## AML 兼容适配层

原有 `aml_retriever.cli`、HTTP Add/Search、确定性检索器和官方契约测试继续保留，
用于 AML 兼容与比赛验证。它们保持同步 Add、写后立即可 Search、`user_id` 隔离和原有
官方响应结构，但不能被当作受治理产品的远程鉴权边界。

历史 AML v1.0 的 `FlowGrid_AML_Retriever` 在首期公开 academic/text 快照中为
43.98 分、第 8 名。当前产品与 v1.1 改动尚无新的官方分数；本地合成评测和治理评测
只属于开发证据，详见 [docs/EVAL.md](docs/EVAL.md)。

## 明确不声称的能力

- 零依赖路径具备普通自然语言提取能力；
- 已在隐藏官方数据上提分或取得新的 AML 官方成绩；
- 能识别获准记忆正文内部的语义 PII；
- 已具备托管多租户、分布式存储或生产级远程安全；
- 非 `ready` ContextPack 可以注入模型。调用方只能注入 `status=ready` 的 pack。

在持久库中放入真实用户数据前，请先阅读
[docs/LOCAL_SECURITY.md](docs/LOCAL_SECURITY.md)。

## 参与贡献与安全报告

提交变更前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题请按
[SECURITY.md](SECURITY.md) 私下报告；不要在公开 issue 中附加真实记忆库、凭据或
私密对话。
