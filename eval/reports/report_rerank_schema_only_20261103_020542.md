# W3 基线评测报告：rerank_schema_only（2026-11-03 02:05:42）

## 运行配置
- 数据集：`eval\dataset\eval_v1.jsonl`（50 条，单轮）
- top_n=8 ｜ runs=3 ｜ 本次共 150 次题级运行
- 模型：deepseek-flash（temperature=0）｜ 快照锚点：2026-09-30
- 检索参数：rrf_k=60，BM25路=开，向量路=开，rerank=BAAI/bge-reranker-v2-m3（pool=16，scope=schema_only）（消融行，实际生效值，W6 §3.4）
- 语料：schema 6 / glossary 11 / qa_pair 11（共 28 片，索引自 data/rag_corpus 删除重建）

## 四指标汇总

### 执行准确率（SQL 级：比较重放结果集，不看 SQL 文本）
- overall：**0.0%**（SQL 判过 0 ｜ 文本判过 0（计入答案级，见下节）｜ fail 0 ｜ error 150，共 150 次）
- 各 run：r1=0.0% ｜ r2=0.0% ｜ r3=0.0%（三次结果不一致的题见 flaky 节，§3.2 不机械取众数）
| 分组 | SQL 判过 | 文本判过 | fail | error | SQL 级准确率 | 答案级准确率 |
| --- | --- | --- | --- | --- | --- | --- |
| single_table | 0 | 0 | 0 | 27 | 0.0% | 0.0% |
| multi_join | 0 | 0 | 0 | 42 | 0.0% | 0.0% |
| aggregation | 0 | 0 | 0 | 33 | 0.0% | 0.0% |
| time_window | 0 | 0 | 0 | 24 | 0.0% | 0.0% |
| glossary_trap | 0 | 0 | 0 | 24 | 0.0% | 0.0% |
| 分组 | SQL 判过 | 文本判过 | fail | error | SQL 级准确率 | 答案级准确率 |
| --- | --- | --- | --- | --- | --- | --- |
| 示例层（hit：EV-001~011 有同款示例，§1.3） | 0 | 0 | 0 | 33 | 0.0% | 0.0% |
| 近示例（near：泛化题，有同口径/同构 QA 可改窗口改维度套用，W6 §2.3） | 0 | 0 | 0 | 60 | 0.0% | 0.0% |
| 远示例（far：泛化题，须 schema+glossary 组合推理，W6 §2.3） | 0 | 0 | 0 | 57 | 0.0% | 0.0% |

### 回答准确率（答案级，含文本计算）
- overall：**0.0%**（pass_via：sql 0 + text 0，共 150 次）
- 差值说明：答案级 − SQL 级 = 0.0%（文本判过 0 题）= Agent 文本计算弥补单条 SQL 表达局限的部分，W6 消融时分别跟踪（§2.4 v2 / 声明⑤）

### Recall@8
- 整体逐片 Recall：**0.0%**（Σ|∩|=0 / Σ|expected|=318）
- 题级全命中率（expected 全部命中）：0.0%（0/150 题）
- 分 doc_type：schema 0.0% ｜ glossary 0.0% ｜ qa_pair 0.0%
- 分题型：single_table 0.0% ｜ multi_join 0.0% ｜ aggregation 0.0% ｜ time_window 0.0% ｜ glossary_trap 0.0%

### 延迟（wall-clock：search 前起、invoke 返回止，含全部工具往返）
- 无可用延迟样本（全部题级异常）

### token 成本（累加全部 AIMessage.usage_metadata，计费口径 §2.6）
- 单题均值：input 0 ｜ output 0 ｜ total 0（cache_read 0 单列，不重复计）
- 总量：input 0 ｜ output 0 ｜ total 0（cache_read 0）；人民币估算按当期牌价手填旁注

## 对照行对比表
（本次运行未提供 --compare 明细文件；对照行对比由 T3 汇主行与对照行两份 detail JSON 后生成。）

## 错答模式统计
| failure_mode | 次数 | 典型样本（case id） |
| --- | --- | --- |
| error:invoke_exception | 150 | EV-001、EV-002、EV-003 |

trap:<id> 与 valid_alt_gloss 分开统计（§4.3）：前者是真陷阱，后者是合法口径但答非所问；other 为错出新花样，人工看明细 JSON 归类后可回填登记表。

## flaky 题记录
（无 （runs>1 时才可能出现翻转））

## 多轮评测节
（本次未运行多轮评测；多轮题用 `--multiturn` 跑 eval_v1_multiturn.jsonl，独立报告节 §1.7/§2.7。）

## 逐题结果
| run | id | 判定 | pass_via | failure_mode | sql_source | 行数(金/模) | 延迟s | tokens(total) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| r1 | EV-001 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-002 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-003 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-004 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-005 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-006 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-007 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-008 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-009 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-010 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-011 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-012 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-013 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-014 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-015 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-016 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-017 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-018 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-019 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-020 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-021 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-022 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-023 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-024 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-025 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-026 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-027 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-028 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-029 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-030 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-031 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-032 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-033 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-034 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-035 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-036 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-037 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-038 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-039 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-040 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-041 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-042 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-043 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-044 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-045 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-046 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-047 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-048 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-049 | error | - | invoke_exception | - | -/- | - | 0 |
| r1 | EV-050 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-001 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-002 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-003 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-004 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-005 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-006 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-007 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-008 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-009 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-010 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-011 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-012 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-013 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-014 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-015 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-016 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-017 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-018 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-019 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-020 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-021 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-022 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-023 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-024 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-025 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-026 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-027 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-028 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-029 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-030 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-031 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-032 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-033 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-034 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-035 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-036 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-037 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-038 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-039 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-040 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-041 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-042 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-043 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-044 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-045 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-046 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-047 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-048 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-049 | error | - | invoke_exception | - | -/- | - | 0 |
| r2 | EV-050 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-001 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-002 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-003 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-004 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-005 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-006 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-007 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-008 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-009 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-010 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-011 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-012 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-013 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-014 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-015 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-016 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-017 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-018 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-019 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-020 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-021 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-022 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-023 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-024 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-025 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-026 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-027 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-028 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-029 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-030 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-031 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-032 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-033 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-034 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-035 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-036 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-037 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-038 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-039 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-040 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-041 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-042 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-043 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-044 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-045 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-046 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-047 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-048 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-049 | error | - | invoke_exception | - | -/- | - | 0 |
| r3 | EV-050 | error | - | invoke_exception | - | -/- | - | 0 |

## 声明（固定五条，W3 设计 §3.3）
1. 本报告是**系统级数字**：评测对象为"混合检索注入 + Agent"完整链路，含 few-shot 自举
   （qa_pair 片参与检索，11 条评测题原题在示例库中），不得表述为"模型的 NL2SQL 能力"（§1.3）。
2. temperature=0 仍存在服务端非确定性；主行（runs>1）为多次运行结果，W6 定稿前会复测（§3.2）。
3. 延迟绝对值受评测时网络波动影响，仅横向（题型间）与纵向（W6 前后对比）相对可比（§2.6）。
4. token 成本按当次实测累计（逐条 AIMessage.usage_metadata 累加；cache_read 单列，
   已含在 input 内不重复计）；人民币估算按当期牌价手填旁注，不写死单价（§2.6）。
5. 双层指标分列：SQL 级执行准确率只认重放结果集一致（pass_via=sql），答案级回答准确率
   额外计入最终回答文本计算命中金标的题（pass_via=text）；两者差值 = Agent 文本计算弥补
   SQL 单查询表达局限的部分，W6 消融时分别跟踪（§2.4 判定规则 v2）。

