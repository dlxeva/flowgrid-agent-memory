# SDK 扩展候选接口

本页描述待发布的可选扩展，不表示它们已进入正式Release。使用前核对实际包的接口；功能开关不是用户授权，调用成功也不代表适合生产。

## 信任与权限

宿主必须从真实认证和最新权限来源提供身份、用户、项目、用途及撤销状态。不能把模型或请求JSON中的`authority`、`owner`、`approved`变成授权。消息的`role=user`只是来源分类，不证明真人身份。原始证据、候选、有效期解释和已确认记忆分别处理。

## 只读打开已有数据库

`FlowGridMemory.open_read_only(db_path=..., config=...)`只接受已存在的绝对文件路径。缺失、空、损坏、未来版本或需要迁移的数据库拒绝；不初始化、迁移或生成schema锁。池中每个连接使用SQLite只读模式，写方法报`PermissionError`。

```python
from aml_retriever import FlowGridMemory

def read_current(database_path, user_id, project, trusted_access_context):
    # All arguments are fixed by the trusted host, not selected by model JSON.
    if not hasattr(FlowGridMemory, "open_read_only"):
        raise RuntimeError("This package does not provide the candidate interface")
    with FlowGridMemory.open_read_only(db_path=database_path) as memory:
        return memory.compile_context(
            user_id=user_id,
            scope={"project": project},
            access_context=trusted_access_context,
            memory_key="decision.plan",
            max_chars=4096,
        )
```

WAL协调文件仍可能创建或更新；这不是源目录零写入、冻结快照或法证访问保证。活跃writer删除的数据会在后续读取反映。非ready上下文交给宿主控制流，不由模型从历史补成当前行动；ready也不授予执行或确认权限。合法原始证据问答有独立用途，不因当前决定未知而全部禁用。

## 条件式数值证据

`compose_numeric_evidence`默认关闭，使用可信principal的read/audit/evidence权限、允许的审计用途及确切来源范围。它从实际数据库重新取原文，核对span、scope和单位后做有界计算。

结果仅表明“这些已授权的来源切片按指定操作计算成立”。主体、断言、单位意义、指代和重复事实仍需审阅。`authorization_verified`是本次host-provided principal的权限检查，不是认证签名或长期许可；来源可能在读取后撤销，发送、返回及实际使用前还要复查。

## 精确来源删除

`erase_source`是可信宿主的Owner操作，必须绑定真实actor、原因和确切user/source目标。不得将模型输出的Owner字段直接传入。候选和原始文本里的“我批准”不构成确认或删除授权。

删除会去掉对应原文、相关聚合视图和依赖记忆，保留不会复活前驱的状态。现有提取回执没有完整输入manifest，因此会保守失效该用户的全部提取回执；无关origin仍约束旧幂等键。SDK调用方应显式处理这一影响，不承诺只删除单一回执。

这不是磁盘页、WAL、备份或供应商副本的安全擦除。扩展没有自动新增REST/MCP/AML公网删除路由；生产接线仍需独立权限和生命周期方案。

## 宿主提取与审核

CompletionRunner默认关闭，transport由宿主注入。尝试计数包含失败，但它不是金额上限、跨进程预留、费用对账或发送许可。涉及外部调用时，宿主另行落实数据范围、同意、持久预算、超时和保留约束。

日期/数值纯函数、约束器及显式factory没有统一enabled开关；不能笼统宣称所有模块默认关闭。durability prompt是假设，精确引文不保证长期意义正确。接受的提议继续是candidate；有效期`supported`只支持该解释，不能自动把记忆变confirmed。

HostReviewEventResolver/ValidityReviewHost默认关闭。它们要求可信回调认证原真人和当前调用者、查最新撤销状态并参与writer序列化。typed元数据不是签名。注入Python runner/callback必须由可信宿主选择；schema校验不是恶意Python插件沙盒。

LocalReviewDemo只用于新的私密合成沙盒，会建立模拟Owner和固定合成用户。它不能提供真实项目认证、真人体验或批准证明；不能包装成默认生产授权后端。

## 上线前仍需验证

真实认证/撤销、所有writer协调、外部费用与保留、供应商兼容、完整文档与发布清单、公开固定版本、真实语义及任务收益。局部测试和合成演示不替代这些验证；AML适配层要求另行核对，不与核心治理权限混合。
