# 离线评测方法与证据

本文件记录 `DEFAULT_FLAGS` 主要取值的本地消融证据。所有数字都由仓库脚本使用
100% 合成数据生成，可用同一 seed 复现；没有任何官方评测数据参与。

v1.0 的官方公开成绩（43.98，第 8 名）是一次独立的排行榜结果。本文的合成指标只用于
比较检索配置，不能与官方综合分换算，也不能替代新版本的官方评测。

## 1. 数据从哪来（以及它不是什么）

`aml_retriever/evaluation/dataset.py` 用固定 seed 生成合成语料：多用户、多会话、
带时间戳的消息流，并按查询类型构造 gold 与干扰项（distractor）。

| 查询类型 | 考察点 | gold 定义 |
| --- | --- | --- |
| `single_hop` | 单条消息即可命中 | 该消息 |
| `multi_session` | 证据跨会话 | 跨会话的多条消息 |
| `temporal` | 带时间限定的检索 | 时间窗内的消息 |
| `knowledge_update` | 同一事实被覆写，要拿**新值** | 新值消息；旧值列为 distractor |
| `absent` | 库里根本没有答案 | 空（不计 Recall/MRR，只看是否硬凑证据） |
| `governance_update_noise` | 真更新之后出现更晚的说明性提及 | 真更新为 gold；旧值与说明性提及为 distractor（仅 `suite=v11`） |
| `direct_preference` | 用户本人偏好 vs assistant 建议 / 第三人偏好 | 用户第一人称原始消息（仅 `suite=v11`） |

> **边界声明**：这是**自造的**代理任务（proxy task），用来做**相对比较**（A 档 vs B 档）。
> 它不能预测官方榜单绝对分数，也不等价于官方 LoCoMo 等数据集的难度分布。
> 任何"本地涨点"只能读作"在本合成集上涨点"。

## 2. 指标口径

- `Recall@k`：前 k 条结果覆盖到的 gold 消息数 / gold 总数，按查询取平均。
  结果既可能是原始消息也可能是聚合视图，只要其 `source_message_ids` 覆盖 gold 即算命中。
- `MRR`：首个命中任一 gold 的结果排名倒数；未命中记 0。
- `旧值泄漏@10`：前 10 条里出现「已被覆写旧值」的比例，**越低越好**。
  本系统不删除旧值（原始消息必须全量保留），只做降权与冲突标注，因此不为 0 属预期。
- 延迟：单进程串行 Search 的端到端墙钟时间，**不含 HTTP 开销**。

## 3. 主消融梯度（run_eval.py）

```bash
python3 scripts/run_eval.py --scale medium --difficulty mixed --seed 20260806 --top-k 100
```

每一档**重建索引**，因为 `views` 开关同时影响写入期的视图构建，复用索引会让结论失真。

| 档位 | 增量 | 结论 |
| --- | --- | --- |
| `L0_lexical_baseline` | FTS5 词法基线 | 起点 |
| `L1_plus_views` | 滑窗 / 会话片段视图 | MRR 明显上升（paraphrase 受益最大），代价是建库变慢 |
| `L2_plus_exact` | 精确子串 / 覆盖率 | 本合成集上与 L1 持平，**保留但标注为无独立收益** |
| `L3_plus_context` | 实体/数字/日期 + 时间与邻接重排 | 收益最大的一档，且大幅降低旧值泄漏 |
| `L4_plus_dedup` | 候选去重 | Recall@20 补到 1.0 |
| `L5_plus_weighted_rrf` | 加权 RRF（v1.0 默认 / v1.1 直接基线） | 小幅正增益，无召回代价 |
| `L6_temporal_intent_ctrl` | 时间意图放大（负对照） | 负增益，固化为关闭 |
| `L7_plus_vector` | 可选向量 | 依赖不可用时自动跳过（unknown） |
| `L8_supersession_ctrl` | 无保护成对覆写（4/1 安全对照） | 总 MRR 小涨但一个 seed 的 Recall@20 回退，不默认启用 |
| **`L9_guarded_supersession`** | 显式更新保护 + 保守 4/1 权重 | **v1.1 默认**；见附录 E |
| `L10_preference_ctrl` | 用户第一人称偏好证据软加权 | 代理集有效，但场景宽度不足，默认关闭 |

评测产物默认写入已被 Git 忽略的 `eval_out/`。若需要长期保存不同版本的结果，建议为
每次运行指定独立的 `--out` 目录，并记录版本或提交号。

> `L2` 在本合成集上无独立增益是**已知事实而非笔误**：合成语料的词面重叠已被
> `L1` 的视图聚合吃掉大部分。它在真实语料上是否有效属 `unknown`。

## 4. 参数扫描（run_scan.py）

```bash
python3 scripts/run_scan.py --scan all --scale medium --difficulties plain,paraphrase,mixed
```

与主消融不同，扫描**共享同一索引**（全程 `views=True`），
因此扫描点之间的差异只来自被扫描的那个参数，不含索引噪声。

产物生成到 `eval_out/`；v1.1 的覆写扫描使用 `--scan supersession`，并可用
`--suite classic|v11` 切换代理集。

### 附录 A — 加权 RRF 的词法权重（`rrf_weight_lexical`）

medium / seed=20260806 / top_k=100，对照点 `rrf_off` 即 `L4`。

| 难度 | 取值 | MRR | ΔMRR | Recall@20 | Recall@100 |
| --- | --- | --- | --- | --- | --- |
| plain | rrf_off | 0.7553 | — | 1.0000 | 1.0000 |
| plain | **0.1（默认）** | 0.7613 | +0.0060 | **1.0000** | 1.0000 |
| plain | 0.25 | 0.7762 | +0.0209 | 1.0000 | 1.0000 |
| plain | 0.5 | 0.7966 | +0.0413 | 1.0000 | 1.0000 |
| plain | 1.0 | 0.8281 | +0.0728 | 1.0000 | 1.0000 |
| paraphrase | rrf_off | 0.5678 | — | 1.0000 | 1.0000 |
| paraphrase | **0.1（默认）** | 0.5687 | +0.0009 | **1.0000** | 1.0000 |
| paraphrase | 0.25 | 0.5769 | +0.0091 | 0.9948 | 1.0000 |
| paraphrase | 0.5 | 0.5819 | +0.0141 | 0.9688 | 1.0000 |
| paraphrase | 1.0 | 0.5770 | +0.0092 | 0.7708 | 1.0000 |
| mixed | rrf_off | 0.6576 | — | 1.0000 | 1.0000 |
| mixed | **0.1（默认）** | 0.6631 | +0.0055 | **1.0000** | 1.0000 |
| mixed | 0.25 | 0.6777 | +0.0201 | 1.0000 | 1.0000 |
| mixed | 0.5 | 0.6829 | +0.0253 | 0.9896 | 1.0000 |
| mixed | 1.0 | 0.6901 | +0.0325 | 0.8750 | 1.0000 |

**结论**：把 BM25 名次以更大权重融进来会**持续抬高 MRR，同时持续压低 Recall@20**
（`Recall@100` 始终为 1.0，说明 gold 没丢，只是被挤出前 20）。
`w_lex=0.1` 是扫描点中唯一 **Pareto 安全**的取值：三种难度 MRR 均为正增益，
Recall@20 全部保持 1.0000。故默认取 0.1。

> **更正记录（2026-08-06）**：本文件此前（以及 `config.py` 注释）曾声称
> "等权融合会把精调特征分抹平（mixed MRR 0.6576 → 0.6325）"。
> 该说法**未能被本次可复现扫描重现**——实测等权（w=1.0）时 mixed MRR 是 **0.6901（上升）**，
> 真正的代价在 Recall@20（1.0 → 0.8750）。已按实测数据更正结论与代码注释。
> 保留 0.1 作为默认的**理由随之改变**：不是"防止 MRR 被抹平"，而是"不牺牲 Recall@20"。
>
> 若下游更看重排序质量而非前 20 条召回，`0.25` 在本合成集上是更激进但仍无 mixed 召回损失的选择。
> **在官方数据上哪个更优 = `unknown`**（无授权数据，不做猜测）。

### 附录 B — 时间意图放大（`temporal_intent`）

| 难度 | 取值 | 总 MRR | 总 Recall@20 | `temporal` 类 MRR |
| --- | --- | --- | --- | --- |
| plain | off（默认） | 0.7613 | 1.0000 | 1.0000 |
| plain | on | 0.7623 | 1.0000 | 1.0000 |
| paraphrase | off（默认） | 0.5687 | 1.0000 | 0.0771 |
| paraphrase | on | 0.5591 | 0.9896 | 0.0809 |
| mixed | off（默认） | 0.6631 | 1.0000 | 0.5392 |
| mixed | on | 0.6561 | 0.9922 | 0.5407 |

**结论**：即便在它**专门针对**的 `temporal` 查询类上，增益也只有 +0.0015 ~ +0.0038；
而整体 MRR 与 Recall@20 双双下降。收益来自「相对新近度」本身，不是在其上再加意图放大。
固化为 `temporal_intent = False`。

> `paraphrase` 难度下 `temporal` 类 MRR 只有 0.077，是**本系统当前最弱的一环**：
> 查询被改写后，纯词法 + 确定性特征几乎抓不到时间锚点。这正是向量检索最可能补上的地方；
> 默认零依赖路径尚未验证该分支，因此保留为 `unknown`。

### 附录 C — 跨 seed 稳定性（3 seed）

单 seed 的结论可能只是随机性。用 `--seeds 20260806,20260807,20260808` 在
`medium` 规模上各跑一遍完整梯度，取 mean / min / max：

| difficulty | 档位 | Recall@20 | MRR | 旧值泄漏@10 | p95 (ms) |
| --- | --- | --- | --- | --- | --- |
| plain | `L5`（v1.0 默认） | 0.9974 (0.9922–1.0000) | 0.7776 (0.7613–0.7951) | 0.7986 | 16.84 |
| paraphrase | `L5`（v1.0 默认） | 0.9948 (0.9896–1.0000) | 0.5719 (0.5629–0.5841) | 0.1806 | 11.68 |
| mixed | `L5`（v1.0 默认） | 0.9948 (0.9870–1.0000) | 0.6728 (0.6631–0.6791) | 0.5035 | 14.37 |

**v1.0 结论**：当时线上默认档位的 Recall@20 在 3 个难度 × 3 个 seed 上都 ≥0.9870，
MRR 的跨 seed 波动带宽 ≤0.034，说明主结论不是单 seed 偶然。
逐 seed 原始数字可由第 6 节命令生成到 `eval_out/*_per_seed.csv`。

> **反例（必须如实记录）**：加权 RRF（L4→L5）在 `medium` 上是正收益
> （plain +0.013、paraphrase +0.003、mixed +0.006），但在 `small` 规模的 `paraphrase` 上
> **反而掉 0.10**（0.7719 → 0.6710）。即「`rrf_weight_lexical=0.1` 是 Pareto 安全点」
> 这一结论**只在 medium 及以上规模成立**，不能外推到小规模。官方数据规模未知 → 属 `unknown`。

### 附录 D — v1.0 无保护覆写检测（历史 18/6 负对照）

针对附录 B 暴露的 `temporal × paraphrase` 短板做的**零依赖**尝试：检测同一用户内
「同话题的旧值 → 新值」成对覆写（结构性内容冗余 + 时间戳先后），局部抬高新值、轻降旧值。
仅在查询含时间意图时触发（否则会误伤模板化的近重复干扰项）。

`medium` / 3 seed，L5（默认）vs 当时使用 18/6 权重的 L8（开启 supersession）：

| difficulty | 指标 | L5 默认 | L8 候选 | 差值 |
| --- | --- | --- | --- | --- |
| paraphrase | `temporal\|paraphrase` MRR | 0.0923 (0.0771–0.1020) | **0.2042 (0.1910–0.2152)** | **+0.112** |
| paraphrase | 总 Recall@20 | 0.9948 | 0.9957 | +0.0009 |
| paraphrase | 总旧值泄漏@10 | 0.1806 | 0.1319 | −0.049（更好） |
| paraphrase | **总 MRR** | 0.5719 | **0.5326** | **−0.039（更差）** |
| mixed | `temporal\|paraphrase` MRR | 0.0929 (0.0784–0.1042) | **0.1985 (0.1946–0.2053)** | **+0.106** |
| mixed | 总 Recall@20 | 0.9948 | 0.9939 | −0.0009 |
| mixed | 总旧值泄漏@10 | 0.5035 | 0.4653 | −0.038（更好） |
| mixed | **总 MRR** | 0.6728 | **0.6419** | **−0.031（更差）** |

目标格 Recall@20 在两种难度下都是 1.0000 → 1.0000，无回退。

**历史结论：不默认启用无保护版本。** 目标格上的增益是真实且跨 seed 稳定的（3 个 seed 的取值区间
与 L5 完全不重叠，paraphrase 上 0.191–0.215 vs 0.077–0.102），泄漏也变好；
但**整体 MRR 在 2 个难度 × 3 个 seed 上一致下降**，未满足
「不引入 R@20 回退 **且** 多数 seed 上总 MRR 上升」的启用门槛，
故 v1.0 固化为 `DEFAULT_FLAGS["supersession"] = False`。v1.1 没有推翻“无保护版本不可默认”的结论：
当前 `L8_supersession_ctrl` 用保守 4/1 权重作为安全对照，默认启用的是附录 E 的**受保护组合**。

失败根因（已定位，不做 fixture 特调）：整体 MRR 的损失集中在 `knowledge_update` 类。
语料中存在「同一项目在更晚时间点的另一条无关提及」，纯时间戳先后会把这条无关的更晚消息
误判为对 gold 的覆写并抬上去。要区分它们只能依赖显式更新措辞（"最新口径/已上调/作废"），
当时没有通用保护与独立代理集，故未启用；v1.1 的后续处理见附录 E。

> 也试过用 IDF 加权的内容包含度来降低模板共有词的影响，实测**更差**
> （目标格增益从 +0.11 掉到 +0.06，且 `knowledge_update` 的损失没有改善），已回退。
>
> 以上全部是**纯合成数据上的观测（observed）**。在官方数据上是否同号、量级如何，属 `unknown`。

### 附录 E — v1.1 显式更新保护与偏好来源实验

v1.1 将两个问题拆开评测：

1. `L9_guarded_supersession`：较新消息除话题重合与时间更晚外，还必须带通用更新语义；
   数值/日期更新不会干扰不询问数值状态的查询。仍只做软重排，旧证据完整保留。
2. `L10_preference_ctrl`：只在偏好类查询中，软加权 `role=user` 且为第一人称的直接偏好陈述；
   assistant 建议和第三人偏好不删除、不屏蔽。

`classic` / medium / mixed / seeds 20260806–20260808 / top_k=100：

| 档位 | Recall@20 mean (min–max) | Recall@100 | MRR mean (min–max) | temporal MRR | knowledge_update MRR |
| --- | --- | --- | --- | --- | --- |
| `L5` v1.0 基线 | 0.9948 (0.9870–1.0000) | 1.0000 | 0.6728 (0.6631–0.6791) | 0.5464 | 0.9017 |
| `L8` 无保护 4/1 对照 | 0.9939 (0.9844–1.0000) | 1.0000 | 0.6780 (0.6680–0.6834) | 0.5891 | 0.8869 |
| **`L9` 受保护 4/1** | **0.9948 (0.9870–1.0000)** | **1.0000** | **0.6948 (0.6854–0.7004)** | **0.6343** | **0.9017** |

参数扫描还显示：`14/4` 与 `18/6` 可把 MRR 进一步抬到 0.7040，但 Recall@20
在一个 seed 上从 0.9870 降到 0.9844。v1.1 选择 `4/1`，因为它是三个 seed 上
**MRR 全部提升且 Recall@20 完全不退**的保守点；不为多拿约 0.009 MRR 交换召回。
逐 seed 原始扫描可使用第 6 节命令生成 JSON 与同名 CSV。

`v11` 代理集是在 classic 基础上每用户追加两类合成题。三 seed 聚合中：

- L9 相比 L5：总体 MRR 0.5477 → 0.5645，Recall@100 保持 1.0000；
- L10（在 L9 上再开偏好加权）：总体 MRR 0.5645 → 0.6340；
- `direct_preference` MRR 从 0.5834 升到 1.0000；
- 保守 4/1 权重尚未改善 `governance_update_noise` 的首位排序（0.4827 持平），
  更大权重虽能改善，但会触发上面的 Recall@20 代价。

因此只将 L9 升为 v1.1 默认。L10 保持默认关闭：它证明了“用户本人直接证据”这一方向
值得继续，但当前代理题仍由规则设计者构造，尚不足以证明对更广泛表达、引用转述和偏好变更都安全。

> 以上仍然只是**本地合成代理证据**。v1.0 的官方公开榜成绩与 v1.1 本地消融必须分开陈述；
> v1.1 尚未提交官方复评，也不能用这里的 MRR 推算官方综合分。

## 5. 向量分支（`vector`）

`config.vector_backend_available()` 只**探测** `numpy` / `sentence_transformers` / `faiss`
是否可导入，不安装依赖，也不下载模型。依赖不可用时，`L7_plus_vector` 自动跳过；
`DEFAULT_FLAGS["vector"] = False`。

该可选分支没有进入默认配置，其收益仍为 **unknown**。

## 6. 复现清单

```bash
cd aml-retriever
python3 -m unittest discover -s tests            # 全量测试
python3 scripts/smoke_api.py                     # HTTP 契约 smoke（自起临时服务）
python3 scripts/run_eval.py  --scale medium --difficulty mixed  --seed 20260806 --top-k 100
python3 scripts/run_eval.py  --scale medium --difficulty plain  --seed 20260806 --top-k 100
python3 scripts/run_scan.py  --scan all --scale medium --difficulties plain,paraphrase,mixed
python3 scripts/run_scan.py  --scan supersession --scale medium --suite classic --difficulties mixed
python3 scripts/run_eval.py  --scale medium --difficulty mixed --suite v11 \
    --seeds 20260806,20260807,20260808 --top-k 100

# 跨 seed 稳定性（附录 C / D 的数字来源），每条约 5 分钟
for d in plain paraphrase mixed; do
  python3 scripts/run_eval.py --scale medium --difficulty "$d" \
      --seeds 20260806,20260807,20260808 --top-k 100
done

# 只想换报告模板、不想重算指标时
python3 scripts/run_eval.py --from-json eval_out/ablation_medium_paraphrase_multiseed.json
```

同一 seed 必然复现同一份数据与同一组指标（延迟数除外，受机器负载影响）。
跨 seed 产物为 `eval_out/ablation_<scale>_<difficulty>_multiseed{,_per_seed}.csv`
与 `REPORT_<scale>_<difficulty>_multiseed.md`；**产物只含指标数字，不落语料原文**，
临时索引库在 `run_ladder_seeds` 的 `finally` 里 `shutil.rmtree` 清除。

## 7. 治理与 AML 双门评测

`run_governance_eval.py` 是独立于 `run_eval.py` 的三层硬门。它不会把安全不变量和
MRR／Recall 平均成一个“总分”：

1. `core_invariant`：`FLG.raw_immutable`、authority gate、traceability，以及
   D1/D3/E1/E2/H1/H2。它们都是不可补偿硬门；任何终态或越权泄漏、unknown／owner gate／
   来源链丢失、预算越界都会直接失败。
2. `local_e2e_proxy`：A2/B2/B3/G1/G3/G5 的纯合成本地代理，以及从精确 clean commit
   `cdae7dbd38d73eda33793b30017559bdfb75eff5` 重跑并冻结的 v1.1 small/mixed
   legacy floors。B2 是真实三段来源链召回代理；G1 只证明规则证据可召回，不证明回答模型
   完成了领域推理；G5 通过本机临时服务锁定 AML Add/Search HTTP routing、鉴权、序列化与 wire shape。
3. `official_aml`：没有可独立核验且绑定精确代码 artifact 的官方产物时固定为
   `unverified`，并令 `official_claim_allowed=false`。这不是本地测试异常，也不能由本地绿灯抵消。

标准 `governance-v1` 不只锁 capability 到 operator／fixture 的名字，还锁定整份 reviewed manifest
的 canonical SHA-256：`714b2ded85115df473ff51a5318c1b8116b5354951e802a283b3189aa4ff445a`。
改变任何 fixture 内容都会得到整体失败的 `custom_manifest_non_attestable` artifact；它不能产出
`evaluation_mode=standard`、标准套件绿灯或 official claim。

快速本地门：

```bash
python3 scripts/run_governance_eval.py --profile quick --output eval_out/governance-quick.json
```

产品面门：

```bash
python3 scripts/run_governance_eval.py --profile product --output eval_out/governance-product.json
```

`product` 额外要求 governed REST 与 MCP surface。缺失时命令返回非零，artifact 中会明确列出
surface failure；已经通过的 core/local 探针仍保留各自证据，但不能据此称产品面已完成。

结果 JSON 包含 manifest SHA-256、代码 revision 与 dirty 状态、逐 surface／capability verdict、
分层 metrics、legacy baseline comparison、failures 与 claim boundary。fixture 使用稳定 alias；
随机 UUID、墙钟和临时路径不会进入可比较产物。仓库内 reviewed legacy golden 来自精确 commit 的
clean `git archive` snapshot，并由固定 baseline／commit／tree／dataset hash 校验；不冻结延迟、
执行路径或生成时间。`assert_clean_baseline_source` 只检查调用方当前 checkout 的 revision 与 dirty
状态，是 freeze workflow 的 provenance precondition，不负责创建 clean snapshot、运行评测或写入
golden，也不应被描述成不可绕过的完整 freeze 命令。

## 8. Quote-anchored 自然语言提取盲测

自然语言提取是宿主适配能力，不进入零依赖 core，也不改变 AML Add/Search。固定模型配置的
合成盲测命令如下：

```bash
python3 scripts/evaluate_natural_language_extractor.py
```

脚本生成 nonce 标记的 4 个正例和 4 个负例，通过 Hermes one-shot 调用显式指定的
provider/model，然后在本地验证：key 与规范化内容精确命中、负例及虚构 token 不进入候选、
每个来源属于输入批次、所有落库状态都是 `candidate`，且普通 current 查询全部 abstain。
provider 原始 JSON、usage 与总结报告写入已忽略的 `eval_out/`。

这项门只证明一次固定配置在小型英文合成集上的结构化行为。它不证明生产自然语言质量、
语义敏感信息识别、跨模型稳定性、真实用户收益或 AML 官方成绩。真实数据是否允许发给模型、
超时取消、成本与速率限制仍由宿主负责。
