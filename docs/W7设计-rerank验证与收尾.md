# W7 设计——rerank 负结果修复验证与 W5 遗留收尾

> **草稿 · 待用户确认**（2026-11-03，设计子 Agent 拟定；§9 开放问题逐条给推荐项，用户拍板后转"已确认 · 可编码"）
> 上游依据：《W6设计-评测扩展与消融.md》（F 行协议、语料冻结纪律、rerank 实现与"线上暂不启用"决策）、《W6讲解-评测扩展与消融.md》（发现 3 的机制假说与"W7：按 doc_type 配额保留"的修法预告、W6 审查 P1"E 行 +4.6pp 未超运行方差"的措辞纪律）、《W5设计-安全加固与自举.md》（HITL 状态机、decisions_for 安全不变量）、W5 审查遗留（0caf490 审查通过无 P0/P1，P2/P3 见 §3，本文档逐条对照现行代码核实）
> 现状代码基线：`agent/rag/hybrid_retriever.py`（search 已参数化 enable_bm25/enable_dense/reranker/rerank_pool，W6-T2）、`agent/rag/rerank.py`（SiliconFlowReranker，fail-closed，W4 审查通过）、`agent/core/turn.py`（RetrievalOptions 收口，prepare_turn 三方共用）、`eval/run_eval.py`（--rerank 等消融 CLI 齐全，detail config 记录实际参数）、`eval/judge.py` v2.1（W3 审查 P2-1 排序键数值化已修，rejudge 零翻转——**确认无需再动**）、`agent/core/approval.py`（decisions_for"批 1 拒余"）、测试账本 pytest 423 / vitest 42 / mvn 47
> 生产现状：https://sql.shanghl.club 原生共存部署（deploy/deploy.sh rsync 三件套）；rerank 线上未启用（服务器 .env 无 SILICONFLOW_API_KEY，W6 §4.4 决策——**W7 维持，本里程碑无线上检索行为变更**）

---

## 0. 范围与现状衔接

### 0.1 W7 交付物 ↔ 本文档章节映射

| W7 交付物 | 章节 | 落地产物 |
| --- | --- | --- |
| rerank doc_type 配额的"假设→实现→实验→结论"完整闭环 | §1 + §2 | `agent/rag/quota.py` + hybrid_retriever 配额钩子 + `eval/quota_sweep.py` 离线标定 + `--quota` 实验行 + experiment-log W7 段 |
| W5 审查遗留 P2×3 收尾 | §3 | chat.js 覆写修复 + 审批卡降级文案、openApproval threadId 交叉校验、server.py 死变量清理 |
| W4 遗留可选项的决策（不铺新摊子） | §4 + §5 | ECharts 与 MCP Server 的成本/收益判断，给"做或不做"结论 |
| 简历口径更新（rerank 闭环的正/负两分支文案） | §2.3 + §7 T6 | 需求文档 §9 / 简历报告 的 rerank 条目定稿 |

### 0.2 W6 遗留清单（逐条核对进 W7 的处置）

| # | 遗留项 | 来源 | W7 处置 |
| --- | --- | --- | --- |
| 1 | rerank 负结果 −4.0pp 的修复验证（doc_type 配额） | W6 发现 3 + W6讲解 §3.3 修法预告 | **主线**（§1/§2） |
| 2 | W6 T4-①② 未实施：多 action 逐条审批 UI、openApproval threadId 交叉校验 | W6 设计 §6 T4（可选纳入项，W6 结束时未做——现行 server.py/approval.py/chat.js 仍为 W5 形态，已核实） | §3：①的**逐条 UI 不做**（降级文案替代，决策见 §3.1）、②做（§3.2） |
| 3 | server.py `_last_tool_name` 死变量 + 类 docstring 与实际行为不符 | W5 审查 P2 | §3.3，清理 + 纠偏 |
| 4 | chat.js 多 action 同名 tool 完成事件覆写指示行 sql | W5 审查 P2 | §3.1，修复 |
| 5 | P3：compare_digest 非 ASCII TypeError；刷新丢审批卡 5 分钟 409 | W5 审查 P3 | §3.4：前者 1 行顺手修；后者不做（UX 出口已有），仅核对 409 文案 |
| 6 | ECharts 图表（W4 设计遗留，标注顺延 W7） | W4 设计开头"明确不做"清单 | §4：给成本/收益判断，**默认不做进主线** |
| 7 | MCP Server（W4 设计 P2） | W4 设计开头"明确不做"清单 | §5：**不做**，给理由与最小形态备档 |
| 8 | W5 自举闭环的线上观察（absorb_feedback 至今未对线上样本跑过） | W5-T3 交付时线上无样本 | §9-4：可选 15 分钟只读观察，不 --apply |

**不在 W7 范围**（克制清单）：不做 k×quota 组合行（OAT 纪律）、不做多轮复跑（W6 F 行多轮本就未跑，FQ 对齐）、不新增汇总基建（3 行对比用既有 --compare + 人工核对清单）、不动判定器 v2.1、不扩语料、不改线上检索行为、ECharts/MCP 默认不做。

### 0.3 设计阶段再分析：W6 明细数据的 run 级复核（本设计的证据基础，零成本）

W6 的消融明细 JSON（`eval/reports/detail_baseline_50_20261012_001636.json`、`detail_rerank_bge_20261012_004947.json`，各 150 条 case）在 W7 设计阶段做了逐题复核。**这一步没有花一分钱**，但它改写了 W6 讲解对负结果的机制叙述的证据强度——预注册必须建立在修正后的证据上，否则 W7 实验验证的是一个与数据不符的假说。

**事实一：Recall 总量与 SQL 级彻底脱钩。** F 行 Recall@8 从 57.5% 升到 84.0%（+26.5pp），SQL 级反而 26.7%→22.7%（−4.0pp）；答案级持平（53.3%→54.0%，文本计算兜底，W3 发现 4 的再现）。"召回更多"与"答得更对"在本语料上无正相关——**片的类型构成比召回总量更接近因果**。

**事实二：注入构成的真实变化主项是 qa_pair，不是 glossary。** 150 次注入合计（平均每题）：基线 qa 4.4 片 / glossary 2.3 片 / schema 1.3 片；F 行 qa 2.7 片（−37.6%）/ glossary 2.2 片（−2%）/ schema 3.0 片（+130%）。glossary 的注入总量几乎没变——被挤掉的是**特定的期望口径片**（见事实三），而**被大量挤掉的是 few-shot 示例片**。W6 已证明 few-shot 是最大单项贡献者（G 行 −16.7pp，>3σ）。

**事实三：glossary 期望片回归的全部证据 = 1 道题。** 基线 36 个 glossary 期望实例缺 3 个（EV-045 的 `glossary:yoy_mom` ×3 runs）；F 行缺 6 个 = EV-045 ×3（两行都缺，非 rerank 回归）+ **EV-029 的 `glossary:net_sales` ×3（唯一 rerank 新增回归）**。83.3% vs 91.7% 的 −8.4pp 完全由这一道题贡献。

**事实四（最关键）：被挤掉 glossary 的 EV-029 在两行都没过。** EV-029（净销售额口径题，far 层）SQL 级基线 0/3、F 行 0/3——**基线注入了 net_sales 口径片它也没做对**。而 EV-045（yoy_mom 两行都缺片）反而 0→1 升分。把"修复 glossary 挤出"直接换算成 SQL 增量的预期是不成立的。

**事实五：SQL 掉分分散在期望片全命中的题上。** H→F 的 SQL 翻转：降 12 runs（EV-005 3→1、EV-012 2→0、EV-022 3→2、EV-025 2→0、EV-028 1→0、EV-037 2→0、EV-040 1→0，其中 3 道是 4-5 个 schema 片的 multi_join 题）、升 6 runs（EV-002/027/044/045/046）。**这 7 道掉分题在 F 行的期望片全部命中**（仅 EV-005 的 schema:orders 基线也缺）——它们的失败不是"缺片"，是**上下文构成变化对生成侧的扰动**（示例片少了 38%、schema 片多了 1.7 片/题、注入顺序被重排改变）。

**对 W6 叙述的修正声明（写进 experiment-log W7 段的开场）**：W6 讲解的"口径片与问题字面相似度天然低、top-8 预算内被 schema 片挤掉→口径题答错"是当时基于 Recall 分型的**机制假说**，其检索侧事实（glossary Recall 83.3% < 91.7%，挤出真实发生）成立，但其 SQL 因果链（"挤掉→答错"）在 run 级证据上不成立——唯一被挤掉的题有片时也没过。**W7 的假说据此升级为双通道版本（§1.1），配额修法同时覆盖两个通道**。这本身就是"负结果→假设→假设被自己的明细数据修正→再实验"的科学方法叙事，比照搬 W6 的修法预告更完整。

### 0.4 设计总原则（延续 W3-W6）

1. **先挖既有数据再花钱**：0.3 的再分析零成本；离线标定（§1.4）在 LLM 全量之前完成且零 LLM 成本。
2. **预注册纪律升级**：W6 的教训是"预注册了预期方向"（E 行意外正向因此可称发现）与"边际效应必须带方差口径"（审查 P1 降格 E 行）。W7 预注册三处升级：①预期建立在 0.3 修正后的假说上；②判据分级（点估计 + ±1σ 带双口径）；③"若不成立"的分支实验（C 行）在跑之前就定义好。
3. 每个决策带理由和被否备选；不确定的 API/行为进 §8 验证清单，禁止编造。
4. 同构红线：所有检索层新参数默认关闭，默认路径与 W6 现行逐字节等价（W4 §6.2 三题验收复跑）。

---

## 1. rerank 配额方案（假设 → 修法）

### 1.1 机制假说的修正版（W7 预注册的假说对象）

**H1（主假说，双通道）**：rerank 负结果的主因是**重排对注入"类型构成"的扰动**，分两通道：

- **通道①（示例稀释，主）**：qa_pair 示例片注入量 4.4→2.7 片/题（−37.6%）。依据：效应量与 W6 最大发现（few-shot −16.7pp）同族；掉分题含 hit 层题（EV-005，定义上依赖示例）；期望片全命中仍掉分（事实五）。
- **通道②（口径缺失，次）**：期望 glossary 片被挤出（EV-029 ×3 runs）。依据：检索侧回归真实；但 SQL 增量预期 ≈0（该题基线有片也没过，事实四）——修它的价值是**消除检索侧回归**（配额让 Recall 分型不再有短板），不是直接买 SQL 分。

**H1 的推论（可证伪）**：若配额把注入构成拉回基线形态（类型覆盖恢复、qa 重新在场），SQL 级应回到基线方差带（≥26.7%−1σ）；若构成恢复而 SQL 仍 <21.7%，H1 不成立，伤害另有来源（候选：重排本身打乱上下文顺序效应——由 C 行检验，§2.3）。

**H0（W6 讲解原版假说，作为被修正的对照记录在案）**："glossary 被挤出是主因"——按 0.3 事实三/四，它对 SQL 级的预测力已被既有数据否定为主因地位，降级为通道②。

### 1.2 三个修法候选的对比论证

| 候选 | 机制 | 结论 |
| --- | --- | --- |
| **A. doc_type 配额保留（主案）** | rerank 重排后按类型规则调整选片（§1.3 精确定义） | **采用**。排名空间（rank-space）规则：对分数尺度不敏感、可单测、可解释（"基线会注入的类型不许被整体挤掉"一句话讲清）、无新增连续超参 |
| B. rerank 分数加类型先验（glossary/qa 加 α 分） | score-space 连续偏置 | **否**。①relevance_score 分布随 query 漂移，固定 α 的实际效果逐题不可控（非平稳）；②无保证语义——加 α 后口径片仍可能全数落在 8 名外，要"保证"就得逐题调 α，退化为配额但语义更弱；③多一个需要扫描的超参，扫描成本与 A 相同换来更差的解释性与可测性；④分数改写还污染 rerank 分本身的明细语义（rrf_score 字段里混入偏置量） |
| C. 只对 schema 片 rerank、其余按 RRF 原序保留 | 注入选集完全不变（成员=RRF top-8），仅 schema 片内部排序优化 | **否（作为主修）**：Recall 三项分型与基线逐位相同（选集不变），W6 检索侧收益（schema 52.7→83.9%）全部放弃——它不"修复"负结果，是放弃 rerank 的选片价值。**但保留独特身份**：构成不变的纯生成侧对照行——H1 证伪时它是预注册的第二假设实验（§2.3），从"被否备选"反转为"分支实验" |

**A 的下位变体（离线扫描对象，§1.4）**：覆盖集锚定（RRF top-8 出现过的类型 vs 池 16 内出现过的类型）、floor 剂量（glossary/qa 各 1 vs qa 2）、cap-only（schema ≤6 不保底）。主案取**最小干预**版本（§1.3），变体只在离线层对比，LLM 全量只跑一个。

### 1.3 配额规则的精确定义（主案：RRF 锚定类型覆盖保底）

**语义**：rerank 允许重排**顺序**，不允许降低**类型覆盖**——相对基线（RRF top-8）而言。规则两段式：

```
输入：rerank_pool=16 的池（RRF 序）、池内片的 rerank 全序（rerank 分降序，
      同分按池内 index 升序决胜——与 rrf_fuse/rerank.py 决胜哲学一致）、top_n=8
1. 覆盖集 C = { doc_type(t) | 片 t ∈ RRF top-8 }   # 基线行为锚，通常 ≤3 类
2. 预留段：对每个 c ∈ C 且池内存在 c 类片：取 c 类 rerank 分最高的 1 片强制入选
   （池内无 c 类片则跳过并记日志——配额救不了池外的片，这是诚实边界）
3. 填充段：剩余槽位按 rerank 全序从池内其余片补满（跳过已入选片）
4. 输出按 rerank 分全局降序排列（保持确定性；rerank 分仍写在 rrf_score 字段位置）
```

- **为什么覆盖集锚 RRF top-8 而不是池 16**：最小干预语义——"只修复 rerank 造成的回归，不新增基线没有的注入策略"。EV-045 的 yoy_mom 基线就不在 top-8（两行都缺），池锚定会为它新增基线没有的注入——那是另一个实验（变体 V2），不混进主案。
- **为什么 floor=1**：需求侧事实（§1.4）——50 题里 glossary/qa 单题期望各 ≤1 片，floor=1 精确匹配需求；floor=2 是为 EV-029 类型内排序不利时的备用剂量（变体 V3），不是主案。
- **确定性**：全程只依赖 RRF 序 + rerank 序 + index 决胜，无随机、无分数阈值——同样输入必然同样输出，离线扫描与线上行为可逐位对账。
- **与需求分布的冲突检查（配额可行性的数学验证）**：floor 总和 2 ≤ top_n 8；schema 槽位下界 = 8−2 = 6 ≥ 单题最大 schema 期望 5 → **任何题的配额都不与需求冲突**。这个检查写进单测（用 50 题期望分布断言）。

### 1.4 配额标定方法（三步，前两步零 LLM 成本）

1. **需求侧（已完成，本设计实测）**：对 `eval/dataset/eval_v1.jsonl` 50 题统计 expected_chunks 的类型组合——schema-only 38 题、glossary+schema 11 题、glossary+qa 1 题；单题内 schema ≤5、glossary ≤1、qa ≤1。结论：glossary/qa 的保底需求是 1，schema 无需保底只需不设过低的 cap。
2. **供给侧（W7-T2 离线执行）**：对 50 题各跑一次 `search(rerank_pool=16, top_n=16)` 取**池内全序**（检索确定性与 rerank 无采样参数，单次即标定基准——§8-1 验证；50 次 rerank API 调用 ≈0.28M tokens）。先回答可行性问题：EV-029 的 net_sales 片是否在池 16 内？——在，配额才救得回；不在，glossary 判据改写（诚实降级为"该回归需扩池/改切分解决"）。
3. **规则扫描（零边际成本）**：池提取后，主案与变体（V2 池锚定 / V3 qa floor 2 / V4 cap-only / B 加分网格 / C）全部是**同一份池排序之上的确定性选片函数**——扫 6 个变体 = 0 次额外 API。输出每个变体的 Recall@8 三分型 + 逐题注入构成对比，选主案并**离线预演判据**（glossary ≥91.7%、schema ≥80% 在离线层即可判定是否可达），然后冻结预注册（§2.3）再进 LLM。

> 标定方法论要点（面试价值点）：**配额是重排结果之上的确定性选取规则，所以它的全部变体可以离线零成本穷举**——这与"调 LLM 侧参数必须全量跑"的成本结构完全不同。检索层实验先离线扫、LLM 层只验证胜者，是本设计省预算的核心手法。

### 1.5 代码改动点与参数组合关系

| 位置 | 改动 | 说明 |
| --- | --- | --- |
| 新增 `agent/rag/quota.py` | 纯函数：`coverage_types(rrf_top_ids, metas) -> set[str]`、`apply_coverage_quota(reranked, rrf_order, coverage, top_n) -> list[RetrievedChunk]` | 纯函数独立成模块——quota_sweep.py 与 search() 共用同一实现，杜绝"标定规则 ≠ 线上规则"的漂移；无 Chroma/jieba/rerank 依赖，单测裸机可跑 |
| `hybrid_retriever.search()` | 新参 `quota: bool = False`；rerank 钩子后追加：`if use_rerank and quota: 需另算 rrf top-8（rrf_fuse 取 top_n 一份）→ apply_coverage_quota` | **默认 False 路径逐字节等价**（同构红线）；quota=True 而 reranker=None 时抛 ValueError（配额无重排可调，参数配错早失败——与"双路全关"同款哲学） |
| `agent/core/turn.py` RetrievalOptions | 新字段 `quota: bool = False`，prepare_turn 透传 | None-opts 默认路径不变 |
| `eval/run_eval.py` | `--quota` 开关；与 `--no-fewshot` 互斥校验（**同给直接启动期报错**——few-shot 都被过滤了还保 qa 覆盖是语义矛盾）；detail config 记录 `quota: true/false`（实际参数如实记录，W6 §3.4 原则） | `effective_retrieval_config` 与 `_describe_retrieval` 同步 |
| `eval/quota_sweep.py`（新，评测侧工具） | 读 50 题 → 池提取（缓存 JSON，含 rerank 分）→ 变体扫描 → 输出 md 对比表 | 只进 eval/，不进 agent/ 运行时 |

**与既有参数的组合关系**：quota × rerank_pool（池是配额的原料，16 不变——扩池是另一个变量）；quota × rrf_k（覆盖集锚的是 k=60 的 RRF top-8；k=10+quota 组合不做，§9-1）；quota × no_fewshot（互斥报错）；quota × enable_bm25/dense（正交，但 W7 全部双路默认开——不组合）。**线上不接线**：quota 与 reranker 同款，仅经 RetrievalOptions 可达（评测 runner 专属），CLI/FastAPI 零变化——W6 §4.4"评测侧"决策对配额同样生效，W7 不改线上行为。

---

## 2. 配额实验设计（W6 F 行协议复用）

### 2.1 行定义与可比性

全部行共享：数据集 50 条、判定 v2.1、temperature=0、快照锚点 2026-09-30、语料 28 片（schema 6 / glossary 11 / qa 11）、top_n=8、top_k_each=20、双路开、k=60。

| 行 | tag | 配置 | runs | 状态 |
| --- | --- | --- | --- | --- |
| H 基线 | `baseline_50` | 全默认 | 3 | **复用 W6**（detail 已在） |
| F（W6 负结果） | `rerank_bge` | --rerank（pool=16，无配额） | 3 | **复用 W6** |
| **FQ（主实验）** | `rerank_quota` | --rerank --quota（§1.3 主案） | 3 | 新跑 150 次 |
| C（分支实验） | `rerank_schema_only` | schema-only 重排（§1.2-C） | 3 | 条件触发（§2.3），预注册在先 |

**W6 两行复用的可比性论证**（为什么不重跑）：语料文件自 e4516dc（W2）后零变更（`git log -- data/rag_corpus` 实证 + 工作区干净），W6 的 corpus_commit=3b98c3a 与 W7 新 HEAD 的差异只来自 docs 提交——**可比性锚是语料内容而非 HEAD 哈希**。判定器 v2.1、数据集、模型、温度全部未变。复用省 300 次 LLM 运行；代价是汇总核对要用"语料片数相等 + `git diff e4516dc..HEAD -- data/rag_corpus` 为空"替代 corpus_commit 逐字相等（写进 T3 核对清单；summarize 若有 corpus_commit 断言则按此口径放行）。

### 2.2 判据（点估计 + 方差带双口径，W6 审查 P1 教训的直接落地）

W6 P1 教训：E 行 +4.6pp 逐 run 32/24/38%（H 为 24/26/30%），约 1σ 未超方差——被降格为"方向性观察"。W7 判据从设计起就分级：

| 判据 | 阈值 | 口径 |
| --- | --- | --- |
| J1 glossary Recall | **≥91.7%**（回到基线水平，即 EV-029 被救回） | 检索侧，离线可预演；qa 期望仅 3 实例不设判据（无统计意义），qa 的信号看注入量（J4） |
| J2 schema Recall | **≥80%**（保留 W6-F 增益的大部，83.9% 的 −4pp 容差） | 检索侧；上不封顶 |
| J3 SQL 级 | **≥26.7%**（点判据）；21.7%（−1σ）~26.7% 记"部分恢复（方向性）"；**<21.7% 判 H1 证伪** | 生成侧；σ 取 W6 逐 run 极差经验值 ≈5pp，报告必须附逐 run 值与 FQ vs H 的逐题翻转表 |
| J4 注入构成 | qa_pair ≥3.7 片/题（较 F 行 +1）、类型覆盖恢复（每题注入类型数 ≥ 基线） | 构成通道的直接观测，连接机制与结果 |
| J5 答案级 | 53-56% 带内（不设通过线，防文本计算兜底掩盖 J3） | 参考指标 |

**结论语义**：J1+J2+J3 全过 = "配额消除类型挤出、W6 负结果归因确认"（H1 成立）；J1/J2 过而 J3 部分恢复 = 方向性结论（措辞按 P1 纪律降格）；J3 证伪 = 第二假设触发（C 行）。**任何一档都不允许把 Δ<1σ 的差写成"优化"**。

### 2.3 预注册（跑之前冻结， experiment-log W7 段开头落盘）

**预期方向（基于 0.3 修正后假说，非 W6 原版）**：

- Recall 构成：glossary 83.3%→91.7% 或 100%（取决于 EV-029 net_sales 的类型内排序与池内存在性，离线先知）；schema 83.9%→80-84%（保底挤占 ≤2 槽，微降可接受）；整体 84.0%→82-86%。
- 注入构成：qa 2.7→≥3.7 片/题、glossary 2.2→2.5±、schema 3.0→2.6±（向基线形态回归但不完全复原——floor=1 不追 4.4 片的 qa 剂量）。
- **SQL 级：预期"恢复至基线带"（22.7%→26.7%±1σ），不预期正向超越**——W6 的 E 行（k=10）已以零挤出方式拿到 +4.6pp 方向性，rerank+配额的价值主张在召回上限（schema 83.9% vs E 的 64.5%），不在 SQL 超越基线。若 FQ SQL >31%（超 E 行点值），先查方差再谈发现（P1 纪律）。

**分支树（预注册，触发即执行不临场决定）**：

```
离线闸门（LLM 之前）：
  net_sales 不在池 16 → glossary 判据 J1 改写为"池外回归另案"，主案仍可进 LLM（J1 缺席）
  主案离线 J1/J2 预演不过 → 换变体（V3 qa floor 2 → V2 池锚定），取离线最优冻结
LLM 之后：
  J1+J2+J3 全过        → H1 成立；C 行不跑；简历正向文案 A（§7 T6）
  J3 部分恢复（21.7-26.7%）→ 可选触发 C 行做归因补强；措辞降格"方向性"
  J3 证伪（<21.7%）     → C 行必跑（第二假设：重排顺序本身伤生成——C 构成不变，
                          若 C ≈基线则伤害在构成以外的重排效应，若 C 也负则 rerank 净负终稿）
  C 行也证伪           → "rerank 在 28 片语料上净负"为终稿结论；两轮负结果 + 机制排除
                          （构成/顺序双双排除）本身就是可交付的科学叙事；配额代码保留
                          （默认关，作为"验证过的修复尝试"存档）
```

**为何把 C 行的触发条件预注册**：W6 的经验是"跑完再看数据说话"会诱使人临时加跑"再试一版"——把第二假设的实验条件写死，既防 p-hacking 嫌疑，也保证预算上界（LLM 行 ≤2 个）。

### 2.4 成本估算（复用 W6 管线，预算内）

| 项 | 量 | 估算 |
| --- | --- | --- |
| 离线池提取 | 50 次 rerank API（pool 16，≈5.6k tokens/次） | ≈0.28M SF tokens，<¥0.5，分钟级 |
| 变体扫描 | 0 API（本地纯函数） | 秒级 |
| FQ 行 | 150 次 LLM（50×3）+ 150 次 rerank | ≈1.37M DeepSeek tokens（W6 实测 9.1k/题）+ ≈0.84M SF tokens；DeepSeek <¥5 + SF <¥1.5 |
| C 行（若触发） | 150 次 LLM + 150 次 rerank | 同上 |
| 挂机 | 150 次 × 30-90s ≈ 1.2-3.7h/行 | 1-2 晚（C 行触发则 2-3 晚或双终端并行） |

费用上界 ≈¥12（两行全跑），远在需求 §10 全程 ¥50 余量内。流程闸门沿用 W3 风险 4：FQ 行先 `--limit 3 --runs 1` 冒烟外推。rerank 失败语义沿用 fail-closed + `--rerun` 补跑（W6 已实跑验证）。

---

## 3. W5 审查遗留 P2/P3 收尾

> 三项全部对照现行代码核实（2026-11-03），修复不动安全不变量（"绝不执行未经用户过目的 SQL"）。

### 3.1 多 action 审批：**显式降级文案**（决策）+ 前端覆写修复（bug）

**决策：不做逐条审批 UI，做降级文案 + 覆写修复。**

- **现状**（代码核实）：一轮多条 SQL 时，middleware 一次收集全部 action_requests（`server.py` `_extract_interrupt_payload`），`pending_approval` 事件只带第一条 SQL（`action_requests[0]`，server.py 有 TODO 注释），登记 `action_count=len(actions)`；resume 由 `approval.decisions_for` 兜底为"approve=批第 1 条 + reject 其余"（`agent/core/approval.py:131`，安全不变量成立：绝不执行未过目 SQL）；前端 ApprovalCard 只展示这条 SQL。被拒 SQL 若仍需要，模型重新发起 → 链式新审批卡（W5 §8-⑰ 实测）。
- **真实缺陷（W5 审查 P2）**：`frontend/src/stores/chat.js` 的 tool 完成事件处理——`[...m.tools].reverse().find(t => t.name === d.name); if (target) target.sql = d.sql`。多 action 轮两条同名 `execute_readonly_sql` 完成事件**先后覆写同一条指示行的 sql**：气泡最终展示被拒的第 2 条 SQL，与审批卡/落库的第 1 条不一致。server 侧 docstring"同名工具只发一次"与实际（按槽位 index 各发一条）不符，是这个误适配的认识论根源（§3.3 一并纠偏）。
- **W7 做的三件事**（合计 ≈半天的量）：
  1. **chat.js 覆写修复**：完成事件只在 `target.sql == null`（还是"正在查询"态）时覆写；已完成的指示行不覆写，追加新行。效果：多 action 轮每个 SQL 一条可折叠指示行，与服务端逐条 tool 事件一一对齐，气泡展示的是真实执行序列。vitest +2 用例（双完成事件 → 两行；先完成后起始 → 不复活）。
  2. **降级文案**：`pending_approval` 事件加 `actionCount` 字段（server.py 1 行，从 `len(actions)` 取）；ApprovalCard 在 actionCount>1 时显示："本轮共 N 条 SQL：批准第 1 条、拒绝其余；被拒的 SQL 如仍需要，助手会重新发起并再次请求审批"。把既有兜底行为对用户透明化——不一致感来自"用户不知道批 1 拒余"，而不是兜底本身。vitest +1。
  3. Java 侧零改动预期：`ChatProxyService.handleData` 的 pending_approval 分支只读取 sql/tokens/latencyMs 字段，多余字段自然忽略；**原始帧是否逐字透传给前端需 §8-6 验证**（若 Java 重组 JSON 则需同步透传 actionCount）。
- **被否：逐条审批 UI（W6 T4-① 原规格：decisions 列表 + 逐条批准/拒绝）**。理由：①真实多 action 轮是低频路径（模型重试/复合查询才触发；W6 多轮评测的对话链也未以多 action 为主形态）；②三层协议改动（前端卡片重做 + Java ApprovalRequest/`decisions_for` 列表化 + 等长校验）≈2-3 天联调，挤占主线；③降级文案消除了用户侧不一致感后，逐条 UI 的边际收益是"能批准第 2 条"——而拒绝后链式新审批卡已覆盖该场景，只是多一次交互。**记 backlog**，若 W8 时间富余按 W6 设计 §6 T4-① 规格实施。

### 3.2 openApproval threadId 交叉校验

- **现状**（`ChatProxyService.openApproval` 核实）：校验了 threadId 格式（validateThreadId）、原行归属（original.userId == userId → 404）、原行状态（status==4 → 409），**唯独没有校验 `req.threadId()` 与 `original.getThreadId()` 一致**。同用户可以把 A 会话的审批请求配上 B 会话的 threadId 发进来——resume 会打到 B 会话的 LangGraph 线程（同用户自伤面，但破坏会话一致性，且 resume 失败的报错难排查）。
- **修复（1 行 + 1 测试）**：原行加载后追加：`if (!req.threadId().equalsIgnoreCase(original.getThreadId())) throw new BizException(ErrorCode.APPROVAL_NOT_FOUND);`。用忽略大小写比较（UUID 十六进制大小写无语义差异；两侧都过了 validateThreadId 的 UUID 解析）。错误码复用 APPROVAL_NOT_FOUND 409——与"行不在待审批态"同码，不泄露该行是否存在于其他会话（越权面一致性）。
- mvn +1：错 threadId → 409；正确 threadId → 放行。

### 3.3 server.py 死变量与 docstring 纠偏

- `_last_tool_name`（`agent/server.py:157` 初始化、`:192` 赋值、**无任何读取点**——grep 全仓核实）：删除两处。它记录的"去重防刷屏"意图从未实现，实际去重语义是 `_pending_tools` 按槽位 index 各发一次起始事件。
- 类 docstring 纠偏：`StreamEventMapper` 的"tool_call_chunks 非空且工具名变化 → tool（同一轮里同名工具只发一次）"改为实际行为："每个工具调用槽（index）起始各发一次 tool 事件；多 action 同名工具各发一条——前端按槽位逐条适配（chat.js 起始事件的同名合并仅用于'正在查询'指示行，完成事件逐条落行）"。docstring 撒谎比死变量更有害——它误导了前端的适配假设（§3.1 缺陷的认识论根源）。
- **范围纪律**：`agent/main.py:119` 的 `last_tool_name` 是活代码（CLI 打印去重），其"同名去重"的记录层瑕疵 W2 已定调（"CLI 看趋势，inspect_chain 看真相"）——**不顺手改**，只在 docstring 纠偏时加一行交叉引用。

### 3.4 P3 两项的处置

1. **compare_digest 非 ASCII TypeError（修，1 行）**：`agent/server.py:104` `secrets.compare_digest(provided, INTERNAL_TOKEN)`——两个 str 参数含非 ASCII 时抛 TypeError → 本应 401 的坏头变 500。改为 bytes 比较：`secrets.compare_digest((provided or "").encode("utf-8"), INTERNAL_TOKEN.encode("utf-8"))`（bytes 版对任意内容恒可比较，时序侧信道防护语义不变）。pytest +1（非 ASCII 头 → False 而非异常）。
2. **页面刷新丢审批卡 → 5 分钟 409（不做）**：UX 出口已有（历史页可查全文；重新提问会因 find_by_thread 409 短路……**核对项**：确认 409 的错误文案是否已指路"等待或稍后重试/历史页"，若未指路则一行文案改进，不做恢复机制——审批态在 Python 进程内存（InMemorySaver 同款限制），跨重启持久化是 W4 已知限制的延续，12 月前不加摊子。

---

## 4. 前端小改进：ECharts 图表（成本/收益判断，默认不做进主线）

**现状（代码核实）**：查询结果只以两种形态到达前端——工具指示行的 SQL 文本、模型在 delta 文本里的叙述；ToolMessage 的结果原文被 mapper **显式过滤**（W5 修复的防泄漏设计），done 事件只带 tokens/latencyMs/generatedSql。结果数据在 Python 侧是"给人读的表格文本"（`execute_readonly_sql` 返回 markdown 表格、最多 20 行截断），**没有任何结构化行/列到达前端**。

**最小实现的成本拆解**（若做）：

1. Python：结构化结果出口——要么改 `execute_readonly_sql` 返回结构化载荷（波及模型侧 prompt 与评测口径，**不可取**），要么在 mapper 结算槽位时附带解析（表格文本反向解析脆弱）或在工具层并行暴露 rows/columns（新事件 `result`，mapper 透传）——≈0.5-1 天 + SSE 契约扩展；
2. Java：新事件类型的转发兼容（现有 handleData 对未知事件的处理路径需验证，§8-7）——0.5 天含测试；
3. 前端：图表组件 + 类型启发（数值列/类别列识别 → 柱/线/饼）+ 空结果/截断/单行边界——1-1.5 天；
4. 联调与部署冒烟：0.5 天。**合计 ≈2.5-3.5 天**，相当于 W7 主线的 60%。

**收益**：演示门面（"智能数据分析助手"的可视化观感）；面试中属"加分项"而非"区分项"——面试官对"负结果→归因→修复→验证"闭环的追问深度远高于对图表的追问，且 W4 的 P95 7.4s 已是指标短板，图表不改善它。

**决策：W7 不做。** 触发条件（满足才重估）：T1-T4 在周内提前完成、且不挤压 W8 简历终稿/模拟面试的时间。届时做**最小版**：仅柱状图、仅"首列类别 + 其余数值列 ≤2"的启发式、无图表类型切换。**被否**：现在排进 W7（违克制原则——W4 遗留清单的"顺延 W7"是候选标注不是承诺，W6 已用行动证明遗留项按价值重估）；SVG 手画图表替代 ECharts（省依赖但可访问性/交互全无，收益更低）。

---

## 5. MCP Server（做/不做判断：**不做**）

**提议形态**：把 NL2SQL 封装为 MCP server（stdio）供 ZCode 等 MCP 客户端调用——tools：`list_tables`（schema 片来源）、`run_readonly_sql`（复用 sql_guard + 只读账号）。本地 stdio、无网络暴露，代码量 50-100 行 + 单测 ≈1-1.5 天。

**不做的三条理由**：

1. **与本项目最强的安全叙事冲突**：MCP 工具调用没有审批卡位——`execute_readonly_sql` 在主产品里是 HITL 审批后执行，包成 MCP tool 后变成客户端直调。复用 sql_guard + 只读账号能守住"只读"，但守不住"人工过目"——为简历加一个关键词，稀释掉四层防线 + HITL 的完整故事，得不偿失。（要加审批就得在 MCP 层再造一套 pending/resume，那是把 W5 的 Java 代理重写一遍。）
2. **无新增工程深度**：MCP 封装是协议适配层，项目的难点（混合检索/消融/HITL/自举）它一个都不碰；面试里"我实现过 MCP server"讲不过"我用消融证明了一个优化是负的、然后修好了它"。
3. **时间线**：12 月投递，W7 主线（§1-§3）+ W8（简历终稿/模拟面试）已满；2C2G 服务器也不需要再多一个常驻进程。

**备档（若 W8 后时间真富余）**：本地 stdio 版、只读、tools 仅 list_tables + run_readonly_sql（强制走 sql_guard）、README 注明"实验性质、与 HITL 主线无关"。不部署服务器、不进简历主线。**被否**：SSE/HTTP 远程 MCP 版（网络暴露面 + 鉴权设计 = 新的安全审查周期）。

---

## 6. 风险与应对

| # | 风险 | 影响 | 应对与回退 |
| --- | --- | --- | --- |
| 1 | **配额救不回 EV-029**：net_sales 片不在池 16，或在池但类型内排序不利（floor=1 抢到别的 glossary 片） | J1 达不成，glossary 判据落空 | 离线闸门先行（§2.3）——池外则 J1 诚实改写；类型内排序不利则 V3（qa floor 2 / glossary floor 2）变体离线顶上；最坏情况 J1 缺席进 LLM（J3 是主判据） |
| 2 | **FQ 的 SQL 仍低于基线−1σ**（H1 证伪） | 修法无效，rerank 终稿结论为净负 | 预注册分支：C 行必跑做机制排除；两轮负结果 + 双机制排除（构成/顺序）仍是完整可交付叙事——本里程碑的价值在设计时就定义为"完整闭环"而非"必须转正" |
| 3 | **rerank API 不稳/限流**（150+ 次调用窗口） | FQ 行 error 题，补跑拖期 | 沿用 W6 实跑经验（fail-closed + 内部重试 1 次 + --rerun）；离线池提取失败可重试且缓存 JSON 断点续扫 |
| 4 | **语料/环境漂移破坏三行可比** | Δ 不可归因 | 冻结纪律复用（窗口内不 --apply、不提交语料、索引不动）；W6 两行复用的可比性核对清单（§2.1）在 T3 首项执行 |
| 5 | **同构红线破坏**（quota 默认路径改动了现行行为） | 线上行为意外变化 | quota 默认 False 逐字节等价 + W4 §6.2 三题验收复跑 + quota 与 reranker 的耦合断言（quota 无 reranker 即抛） |
| 6 | **方差误读复发**（FQ 恢复到 26.7% 附近时，±3pp 的波动被当成"配额有效/无效"） | 结论措辞失当 | 判据双口径（§2.2）预注册；逐 run 值 + 翻转表强制入报告；"部分恢复"档的措辞模板预先写好 |
| 7 | **Java/前端小修的回归**（chat.js 覆写修复影响单 action 正常轮） | 线上对话展示回归 | vitest 覆盖单 action（覆写路径不变）+ 多 action（追加行）双形态；部署后线上冒烟含一轮双 SQL 审批 |

---

## 7. 工单拆分

> 顺序：**T1 → T2 → T3**（主线串行：实现 → 离线标定/预注册 → 实验）；**T4 与主线并行**（互不依赖）；T5 由 T3 结果触发；T6 收尾。总计 ≈4.5-5 天，1 周内完成。

| 工单 | 内容 | 完成标准（验收即测试） |
| --- | --- | --- |
| **T1 配额实现** | `agent/rag/quota.py`（两段式纯函数 + 覆盖集计算 + 需求分布断言单测）；search() 新参 quota（默认关）+ RetrievalOptions/prepare_turn 透传 + run_eval `--quota`（与 --no-fewshot 互斥报错）+ detail config 如实记录 | quota 纯函数单测全绿（构造池序/覆盖集 → 断言输出与决胜；50 题期望分布的冲突检查断言）；mock reranker 的 search 集成测试；默认路径 W4 §6.2 三题验收逐字节通过；ruff 通过 |
| **T2 离线标定 + 预注册** | `eval/quota_sweep.py`：50 题池提取（缓存 JSON）→ 主案 + 变体扫描 → md 对比表；EV-029/EV-045 池内存在性结论；按 §2.3 冻结预注册（预期 + 分支树 + 判据）写进 experiment-log W7 段开头 | 池提取 50/50 成功且 rerank 分缓存落盘；扫描表覆盖 ≥4 变体；离线预演 J1/J2 有明确结论；预注册文本落盘（此后不得改动——改动须新增附注而非改写） |
| **T3 FQ 行全量 + 结论** | 冒烟（--limit 3 --runs 1）→ `--rerank --quota --runs 3` 全量 → 与 H/F 三行汇总（--compare + 人工核对 W6 复用可比性清单）→ 判据五项逐项判定 → experiment-log W7 段（含逐 run 值 + 翻转表 + 分支走向） | 150 次 error=0（或已 --rerun 补齐）；语料冻结核对（片数 + git diff 为空）；判据表 + 结论分支落盘；detail config 含 quota=true |
| **T4 W5 遗留收尾（与 T1-T3 并行）** | §3 全部：chat.js 覆写修复 + 审批卡降级文案（含 server pending_approval 加 actionCount）+ openApproval threadId 校验 + 死变量/docstring 纠偏 + compare_digest bytes + 409 文案核对 | pytest/vitest/mvn 新增用例全绿（账本 423/42/47 相应增长）；多 action 手测：诱导两条 SQL → 卡片显示共 N 条文案 → 双指示行分别展示真实 SQL 序列；错 threadId 409；非 ASCII 内部 token 头 401 非 500；单 action 回归无损 |
| **T5 分支行（条件触发）** | C 行（`rerank_schema_only`，J3 证伪必跑 / 部分恢复可选）或 V3 变体复跑，按预注册分支执行 | 行为与判据同 T3 口径；触发决策记录在 experiment-log（引用预注册条款，不临场新增假设） |
| **T6 收尾** | 部署一次（deploy.sh 三件套——§3 的三处修复上线）+ 线上冒烟（多 action 审批文案、审批回归、正常对话回归）；简历 rerank 条目按分支定稿（正向："+doc_type 配额重排消除类型挤出，SQL 恢复基线且保留 schema 召回 +31pp"；负向："两轮消融负结果 + 机制排除"）；W7 讲解文档（短版：再分析修正叙事 + 离线标定方法 + 闭环结论） | 线上 8 步冒烟通过；简历文案与 experiment-log 结论一致（禁止提前写"修复成功"）；教学文档含"预注册被自家数据修正"一节 |

---

## 8. 编码前验证清单（禁止边写边猜）

| # | 验证项 | 验证方式 |
| --- | --- | --- |
| 1 | **检索确定性假设**：同 query 两次 search（含 rerank API）的池序是否逐位一致（离线标定的单次提取是否可信） | 50 题中抽 5 题各跑 2 次池提取比对；不一致则池提取改为 3 次取众数序并在 T2 注明 |
| 2 | **池内存在性**：EV-029 的 `glossary:net_sales`、EV-045 的 `glossary:yoy_mom` 在各自池 16 内是否存在 | T2 池提取缓存直接查；结论写进预注册 |
| 3 | **rerank 分数无采样参数**：SiliconFlow /rerank 是否有 temperature 类参数（有则标定口径需固定） | 复核 API 文档 + 冒烟两次同输入比对分数 |
| 4 | **quota 与 W4 同构验收**：默认路径三题 injected_chunks 与 W5 明细逐元素相等 | 复跑 W4 §6.2 验收 |
| 5 | **互斥校验行为**：--quota --no-fewshot 同给时启动期报错文案正确指向语义冲突 | 单测 |
| 6 | **Java 对 pending_approval 附加字段（actionCount）的透传**：Java 重组 JSON 还是原样转发 | 读 handleData 转发段 + 本地联调抓帧；重组则 Java 同步加字段 |
| 7 | **Java 对未知 SSE 事件（若做 result 事件——仅 ECharts 触发时）**：不破坏现有解析 | 同上；不触发则跳过 |
| 8 | **chat.js 修复的双形态**：单 action 轮（覆写路径原样）与多 action 轮（追加行）的 vitest | 组件测试两用例 |
| 9 | **openApproval 校验的大小写边界**：大写 UUID threadId + 小写落库值 | mvn 单测 |
| 10 | **compare_digest bytes 语义**：等值/不等值/非 ASCII/空头四态 | pytest 四态 |
| 11 | **409 文案现状**：审批占用时新提问的报错文案是否已指路出口 | 手测 + 截图记录；缺则一行补 |
| 12 | **W6 复用行的明细完整性**：baseline_50 / rerank_bge 两份 detail 的 records 字段可支撑逐题翻转表再生成 | T3 汇总时实跑核对脚本 |

---

## 9. 开放问题（逐条给推荐项）

1. **E 行（k=10）+ FQ 的组合行跑不跑？**
   **推荐：W7 不跑**（OAT 纪律：组合行无法归因；E 本身是方向性观察）。若 FQ 判据全过且 W8 想"最佳配置定稿"，再跑一行 `--rrf-k 10 --rerank --quota --runs 3`（半晚）——那时它是"配置定稿验证"，不是归因实验。被否：现在跑（两个未定效应叠加，跑出来解释不了）。
2. **C 行何时触发？**
   **推荐：按预注册分支机械执行**——J3 证伪必跑、J3 部分恢复可选、J3 通过不跑。不提前跑（省 150 次）；不因"好奇"加跑（p-hacking 嫌疑 + 预算纪律）。
3. **FQ 判据全过后，rerank 要不要上线上？**
   **推荐：仍不上**。W6 §4.4 的三条理由（BYOK 原则不开洞 / 2C2G 延迟预算 / 收益未证）中前两条不变，第三条变为"收益已证但限于评测口径"——线上启用需要 BYOK 多 provider 改造或本地 ONNX 路径（模型 ~1GB，2C2G 内存风险真实），都是独立决策。W7 的产出是**可写进简历的完整闭环 + 归因结论**，不是线上开关。被否：判据一过就上线（把评测侧最优解等同于部署决策）。
4. **absorb_feedback 要不要顺带跑一次 dry-run 观察？**
   **推荐：做一次只读观察（15 分钟级，不 --apply）**。线上 internal 接口被 nginx deny（W5 审查修复），观察路径 = ssh 到服务器 `curl 127.0.0.1:8081/api/internal/feedback/samples?limit=200`（X-Internal-Token）拉线上样本清单 → 本地 `absorb_feedback --dry-run` 消费（或直接人工过目样本质量）。产出：真实点赞样本的数量与过门槛率，记 experiment-log——若为零样本，"自举闭环暂无原料"本身是真实数据点；若有样本，W8 决定是否正式收录（**必须避开 T3 实验窗口**，冻结纪律优先）。被否：直接 --apply 收录（污染冻结窗口 + 点赞≠正确的人工闸不可省）。
5. **预注册被自家数据修正（0.3）要不要在简历/面试里显式讲？**
   **推荐：显式讲，作为 W7 的第一叙事**。"负结果 → 归因假说 → 用既有明细复核假说 → 发现原机制叙述证据不足 → 升级为双通道假说 → 离线标定 → 预注册 → 实验"比"负结果 → 修复 → 变好了"高一个量级——它展示的是对**自己的归因也保持怀疑**的纪律。落点：W7 讲解文档独立一节 + 面试口述稿。

---

*设计完，未改动任何代码。0.3 的再分析结论（五事实 + H1 双通道假说）如与用户对 W6 叙述的理解有出入，先对齐再拍板 §9。待用户对 §9 五个开放问题拍板（推荐项可整包接受）后，按 §7 工单顺序派发编码。*
