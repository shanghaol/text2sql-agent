# W3 基线评测报告：rerank_schema_only_v2（2026-11-03 02:24:36）

## 运行配置
- 数据集：`eval\dataset\eval_v1.jsonl`（50 条，单轮）
- top_n=8 ｜ runs=3 ｜ 本次共 150 次题级运行
- 模型：deepseek-flash（temperature=0）｜ 快照锚点：2026-09-30
- 检索参数：rrf_k=60，BM25路=开，向量路=开，rerank=BAAI/bge-reranker-v2-m3（pool=16，scope=schema_only）（消融行，实际生效值，W6 §3.4）
- 语料：schema 6 / glossary 11 / qa_pair 11（共 28 片，索引自 data/rag_corpus 删除重建）

## 四指标汇总

### 执行准确率（SQL 级：比较重放结果集，不看 SQL 文本）
- overall：**20.7%**（SQL 判过 31 ｜ 文本判过 47（计入答案级，见下节）｜ fail 72 ｜ error 0，共 150 次）
- 各 run：r1=20.0% ｜ r2=18.0% ｜ r3=24.0%（三次结果不一致的题见 flaky 节，§3.2 不机械取众数）
| 分组 | SQL 判过 | 文本判过 | fail | error | SQL 级准确率 | 答案级准确率 |
| --- | --- | --- | --- | --- | --- | --- |
| single_table | 9 | 9 | 9 | 0 | 33.3% | 66.7% |
| multi_join | 6 | 4 | 32 | 0 | 14.3% | 23.8% |
| aggregation | 8 | 9 | 16 | 0 | 24.2% | 51.5% |
| time_window | 5 | 5 | 14 | 0 | 20.8% | 41.7% |
| glossary_trap | 3 | 20 | 1 | 0 | 12.5% | 95.8% |
| 分组 | SQL 判过 | 文本判过 | fail | error | SQL 级准确率 | 答案级准确率 |
| --- | --- | --- | --- | --- | --- | --- |
| 示例层（hit：EV-001~011 有同款示例，§1.3） | 16 | 14 | 3 | 0 | 48.5% | 90.9% |
| 近示例（near：泛化题，有同口径/同构 QA 可改窗口改维度套用，W6 §2.3） | 7 | 10 | 43 | 0 | 11.7% | 28.3% |
| 远示例（far：泛化题，须 schema+glossary 组合推理，W6 §2.3） | 8 | 23 | 26 | 0 | 14.0% | 54.4% |

### 回答准确率（答案级，含文本计算）
- overall：**52.0%**（pass_via：sql 31 + text 47，共 150 次）
- 差值说明：答案级 − SQL 级 = 31.3%（文本判过 47 题）= Agent 文本计算弥补单条 SQL 表达局限的部分，W6 消融时分别跟踪（§2.4 v2 / 声明⑤）

### Recall@8
- 整体逐片 Recall：**57.5%**（Σ|∩|=183 / Σ|expected|=318）
- 题级全命中率（expected 全部命中）：38.0%（57/150 题）
- 分 doc_type：schema 52.7% ｜ glossary 91.7% ｜ qa_pair 100.0%
- 分题型：single_table 72.7% ｜ multi_join 44.7% ｜ aggregation 72.2% ｜ time_window 70.0% ｜ glossary_trap 60.0%

### 延迟（wall-clock：search 前起、invoke 返回止，含全部工具往返）
- mean 4.9s ｜ median 4.0s ｜ P95 10.9s ｜ max 21.3s
- P95 口径：statistics.quantiles(n=20, method='inclusive')[18]（线性插值，§8-5 拍板）
- 分题型 P95：single_table 4.6s ｜ multi_join 13.4s ｜ aggregation 8.5s ｜ time_window 7.0s ｜ glossary_trap 6.9s
- 延迟绝对值受网络波动影响，仅相对可比（见声明③）

### token 成本（累加全部 AIMessage.usage_metadata，计费口径 §2.6）
- 单题均值：input 9362 ｜ output 783 ｜ total 10145（cache_read 8298 单列，不重复计）
- 总量：input 1404340 ｜ output 117381 ｜ total 1521721（cache_read 1244668）；人民币估算按当期牌价手填旁注

## 对照行对比表
（本次运行未提供 --compare 明细文件；对照行对比由 T3 汇主行与对照行两份 detail JSON 后生成。）

## 错答模式统计
| failure_mode | 次数 | 典型样本（case id） |
| --- | --- | --- |
| other | 72 | EV-002、EV-007、EV-013 |

trap:<id> 与 valid_alt_gloss 分开统计（§4.3）：前者是真陷阱，后者是合法口径但答非所问；other 为错出新花样，人工看明细 JSON 归类后可回填登记表。

## flaky 题记录
| id | 各 run 判定 |
| --- | --- |
| EV-002 | fail → pass → pass |
| EV-007 | fail → fail → pass |
| EV-022 | pass → fail → pass |
| EV-030 | fail → pass → pass |
| EV-037 | fail → pass → pass |
| EV-042 | pass → pass → fail |
| EV-044 | pass → fail → fail |

flaky 题不机械取众数：人工复盘明细 JSON（三次的 SQL 差在哪），结论记入 experiment-log（§3.2）。

## 多轮评测节
（本次未运行多轮评测；多轮题用 `--multiturn` 跑 eval_v1_multiturn.jsonl，独立报告节 §1.7/§2.7。）

## 逐题结果
| run | id | 判定 | pass_via | failure_mode | sql_source | 行数(金/模) | 延迟s | tokens(total) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| r1 | EV-001 | pass | sql | - | last | 15/15 | 3.518 | 7504 |
| r1 | EV-002 | fail | - | other | last | 5/1 | 4.818 | 15089 |
| r1 | EV-003 | pass | sql | - | last | 1/1 | 1.958 | 5276 |
| r1 | EV-004 | pass | text | - | last | 1/1 | 4.516 | 7580 |
| r1 | EV-005 | pass | text | - | last | 1/1 | 4.113 | 12229 |
| r1 | EV-006 | pass | text | - | last | 1/1 | 3.221 | 9844 |
| r1 | EV-007 | fail | - | other | last | 7/7 | 4.162 | 7429 |
| r1 | EV-008 | pass | text | - | last | 1/1 | 2.141 | 7215 |
| r1 | EV-009 | pass | text | - | last | 1/1 | 4.563 | 8607 |
| r1 | EV-010 | pass | sql | - | last | 3/3 | 2.902 | 10730 |
| r1 | EV-011 | pass | sql | - | last | 3/3 | 2.657 | 8285 |
| r1 | EV-012 | pass | text | - | last | 1/6 | 3.878 | 13207 |
| r1 | EV-013 | fail | - | other | last | 3/3 | 2.696 | 8463 |
| r1 | EV-014 | pass | sql | - | last | 6/6 | 3.207 | 7190 |
| r1 | EV-015 | fail | - | other | last | 5/5 | 4.569 | 8404 |
| r1 | EV-016 | fail | - | other | last | 20/0 | 6.679 | 12029 |
| r1 | EV-017 | fail | - | other | last | 8/1 | 21.305 | 31810 |
| r1 | EV-018 | fail | - | other | last | 20/1 | 4.918 | 12761 |
| r1 | EV-019 | fail | - | other | last | 5/5 | 5.844 | 13340 |
| r1 | EV-020 | fail | - | other | last | 1/1 | 6.254 | 16338 |
| r1 | EV-021 | fail | - | other | last | 5/5 | 5.948 | 11412 |
| r1 | EV-022 | pass | sql | - | last | 3/3 | 3.624 | 9156 |
| r1 | EV-023 | pass | text | - | last | 1/1 | 2.73 | 8610 |
| r1 | EV-024 | fail | - | other | last | 20/1 | 6.754 | 12226 |
| r1 | EV-025 | pass | text | - | last | 1/2 | 2.403 | 6899 |
| r1 | EV-026 | fail | - | other | last | 7/7 | 3.105 | 7366 |
| r1 | EV-027 | fail | - | other | last | 12/12 | 7.036 | 10273 |
| r1 | EV-028 | pass | text | - | last | 1/1 | 2.216 | 6762 |
| r1 | EV-029 | pass | text | - | last | 1/1 | 4.089 | 5916 |
| r1 | EV-030 | fail | - | other | last | 1/1 | 4.033 | 7720 |
| r1 | EV-031 | pass | sql | - | last | 8/8 | 2.571 | 8233 |
| r1 | EV-032 | pass | text | - | last | 1/8 | 3.992 | 13181 |
| r1 | EV-033 | fail | - | other | last | 1/3 | 2.296 | 7039 |
| r1 | EV-034 | fail | - | other | last | 8/1 | 11.65 | 14728 |
| r1 | EV-035 | fail | - | other | last | 3/8 | 13.385 | 19837 |
| r1 | EV-036 | fail | - | other | last | 3/1 | 4.234 | 7640 |
| r1 | EV-037 | fail | - | other | last | 1/1 | 9.253 | 16316 |
| r1 | EV-038 | fail | - | other | last | 20/0 | 12.762 | 14167 |
| r1 | EV-039 | fail | - | other | last | 1/1 | 11.153 | 18905 |
| r1 | EV-040 | pass | sql | - | last | 5/5 | 3.705 | 8666 |
| r1 | EV-041 | fail | - | other | last | 2/1 | 4.212 | 7169 |
| r1 | EV-042 | pass | sql | - | last | 3/3 | 3.372 | 8523 |
| r1 | EV-043 | fail | - | other | last | 3/1 | 4.723 | 10728 |
| r1 | EV-044 | pass | sql | - | last | 8/8 | 8.719 | 9015 |
| r1 | EV-045 | pass | text | - | last | 1/1 | 3.482 | 9423 |
| r1 | EV-046 | fail | - | other | last | 7/7 | 3.257 | 7174 |
| r1 | EV-047 | fail | - | other | last | 4/1 | 6.948 | 14964 |
| r1 | EV-048 | pass | text | - | last | 1/1 | 5.045 | 11427 |
| r1 | EV-049 | pass | text | - | last | 1/1 | 3.706 | 7083 |
| r1 | EV-050 | pass | text | - | last | 1/1 | 7.365 | 10250 |
| r2 | EV-001 | pass | sql | - | last | 15/15 | 3.439 | 7511 |
| r2 | EV-002 | pass | sql | - | last | 5/5 | 3.267 | 9857 |
| r2 | EV-003 | pass | sql | - | last | 1/1 | 2.343 | 5224 |
| r2 | EV-004 | pass | text | - | last | 1/1 | 5.823 | 8178 |
| r2 | EV-005 | pass | text | - | last | 1/1 | 4.374 | 12516 |
| r2 | EV-006 | pass | text | - | last | 1/1 | 3.222 | 9889 |
| r2 | EV-007 | fail | - | other | last | 7/7 | 3.127 | 7183 |
| r2 | EV-008 | pass | text | - | last | 1/1 | 1.97 | 7156 |
| r2 | EV-009 | pass | text | - | last | 1/1 | 4.02 | 5852 |
| r2 | EV-010 | pass | sql | - | last | 3/3 | 2.29 | 10702 |
| r2 | EV-011 | pass | sql | - | last | 3/3 | 2.744 | 8313 |
| r2 | EV-012 | pass | text | - | last | 1/6 | 3.52 | 13190 |
| r2 | EV-013 | fail | - | other | last | 3/3 | 2.202 | 8458 |
| r2 | EV-014 | pass | sql | - | last | 6/6 | 5.428 | 15001 |
| r2 | EV-015 | fail | - | other | last | 5/5 | 4.531 | 8546 |
| r2 | EV-016 | fail | - | other | last | 20/1 | 7.045 | 11782 |
| r2 | EV-017 | fail | - | other | last | 8/1 | 6.203 | 9003 |
| r2 | EV-018 | fail | - | other | last | 20/1 | 5.571 | 9112 |
| r2 | EV-019 | fail | - | other | last | 5/5 | 4.74 | 8682 |
| r2 | EV-020 | fail | - | other | last | 1/1 | 6.983 | 16177 |
| r2 | EV-021 | fail | - | other | last | 5/5 | 2.757 | 7057 |
| r2 | EV-022 | fail | - | other | last | 3/3 | 3.532 | 9354 |
| r2 | EV-023 | pass | text | - | last | 1/1 | 3.366 | 8720 |
| r2 | EV-024 | fail | - | other | last | 20/20 | 4.548 | 7755 |
| r2 | EV-025 | pass | text | - | last | 1/2 | 3.197 | 7065 |
| r2 | EV-026 | fail | - | other | last | 7/7 | 3.77 | 7396 |
| r2 | EV-027 | fail | - | other | last | 12/12 | 5.868 | 9178 |
| r2 | EV-028 | pass | text | - | last | 1/1 | 3.014 | 6877 |
| r2 | EV-029 | pass | text | - | last | 1/1 | 3.789 | 5860 |
| r2 | EV-030 | pass | text | - | last | 1/1 | 6.915 | 12544 |
| r2 | EV-031 | pass | sql | - | last | 8/8 | 2.277 | 8250 |
| r2 | EV-032 | pass | text | - | last | 1/8 | 3.966 | 13156 |
| r2 | EV-033 | fail | - | other | last | 1/1 | 2.076 | 6898 |
| r2 | EV-034 | fail | - | other | last | 8/1 | 12.259 | 17954 |
| r2 | EV-035 | fail | - | other | last | 3/3 | 5.075 | 7754 |
| r2 | EV-036 | fail | - | other | last | 3/3 | 3.219 | 7217 |
| r2 | EV-037 | pass | text | - | last | 1/1 | 5.155 | 14748 |
| r2 | EV-038 | fail | - | other | last | 20/0 | 8.782 | 13247 |
| r2 | EV-039 | fail | - | other | last | 1/1 | 13.51 | 24338 |
| r2 | EV-040 | pass | sql | - | last | 5/5 | 6.534 | 13942 |
| r2 | EV-041 | fail | - | other | last | 2/2 | 2.935 | 6865 |
| r2 | EV-042 | pass | sql | - | last | 3/3 | 2.775 | 8420 |
| r2 | EV-043 | fail | - | other | last | 3/3 | 3.534 | 6927 |
| r2 | EV-044 | fail | - | other | last | 8/8 | 5.784 | 9332 |
| r2 | EV-045 | pass | text | - | last | 1/1 | 3.32 | 9452 |
| r2 | EV-046 | fail | - | other | last | 7/7 | 2.858 | 7051 |
| r2 | EV-047 | fail | - | other | last | 4/1 | 5.413 | 9593 |
| r2 | EV-048 | pass | text | - | last | 1/1 | 4.213 | 7418 |
| r2 | EV-049 | pass | text | - | last | 1/3 | 6.57 | 15356 |
| r2 | EV-050 | pass | text | - | last | 1/1 | 4.409 | 6075 |
| r3 | EV-001 | pass | sql | - | last | 15/15 | 2.611 | 7473 |
| r3 | EV-002 | pass | sql | - | last | 5/5 | 3.374 | 9845 |
| r3 | EV-003 | pass | sql | - | last | 1/1 | 2.617 | 5278 |
| r3 | EV-004 | pass | text | - | last | 1/1 | 6.611 | 8190 |
| r3 | EV-005 | pass | sql | - | last | 1/1 | 3.354 | 8001 |
| r3 | EV-006 | pass | text | - | last | 1/1 | 3.126 | 9762 |
| r3 | EV-007 | pass | sql | - | last | 7/7 | 3.168 | 7068 |
| r3 | EV-008 | pass | text | - | last | 1/1 | 3.616 | 10801 |
| r3 | EV-009 | pass | text | - | last | 1/1 | 4.336 | 5839 |
| r3 | EV-010 | pass | sql | - | last | 3/3 | 2.662 | 10710 |
| r3 | EV-011 | pass | sql | - | last | 3/3 | 2.5 | 8268 |
| r3 | EV-012 | pass | text | - | last | 1/6 | 3.496 | 13168 |
| r3 | EV-013 | fail | - | other | last | 3/3 | 2.767 | 8481 |
| r3 | EV-014 | pass | sql | - | last | 6/6 | 2.974 | 7071 |
| r3 | EV-015 | fail | - | other | last | 5/5 | 4.489 | 8338 |
| r3 | EV-016 | fail | - | other | last | 20/20 | 7.222 | 8619 |
| r3 | EV-017 | fail | - | other | last | 8/1 | 10.273 | 19014 |
| r3 | EV-018 | fail | - | other | last | 20/1 | 5.429 | 12796 |
| r3 | EV-019 | fail | - | other | last | 5/5 | 4.253 | 8694 |
| r3 | EV-020 | fail | - | other | last | 1/1 | 9.386 | 20673 |
| r3 | EV-021 | fail | - | other | last | 5/5 | 2.887 | 7083 |
| r3 | EV-022 | pass | sql | - | last | 3/3 | 3.782 | 9207 |
| r3 | EV-023 | pass | text | - | last | 1/1 | 6.271 | 14444 |
| r3 | EV-024 | fail | - | other | last | 20/1 | 8.376 | 12818 |
| r3 | EV-025 | pass | text | - | last | 1/2 | 3.472 | 7054 |
| r3 | EV-026 | fail | - | other | last | 7/7 | 2.918 | 7413 |
| r3 | EV-027 | fail | - | other | last | 12/12 | 10.539 | 11795 |
| r3 | EV-028 | pass | text | - | last | 1/1 | 2.376 | 6751 |
| r3 | EV-029 | pass | text | - | last | 1/1 | 3.072 | 5718 |
| r3 | EV-030 | pass | text | - | last | 1/1 | 5.792 | 8489 |
| r3 | EV-031 | pass | sql | - | last | 8/8 | 1.914 | 8241 |
| r3 | EV-032 | pass | text | - | last | 1/8 | 3.86 | 9100 |
| r3 | EV-033 | fail | - | other | last | 1/1 | 2.067 | 6928 |
| r3 | EV-034 | fail | - | other | last | 8/1 | 12.542 | 23390 |
| r3 | EV-035 | fail | - | other | last | 3/3 | 3.657 | 7642 |
| r3 | EV-036 | fail | - | other | last | 3/1 | 5.709 | 11427 |
| r3 | EV-037 | pass | text | - | last | 1/1 | 5.833 | 10569 |
| r3 | EV-038 | fail | - | other | last | 20/20 | 5.139 | 8124 |
| r3 | EV-039 | fail | - | other | last | 1/1 | 8.657 | 17797 |
| r3 | EV-040 | pass | sql | - | last | 5/5 | 4.456 | 8656 |
| r3 | EV-041 | fail | - | other | last | 2/2 | 4.629 | 10691 |
| r3 | EV-042 | fail | - | other | last | 3/1 | 3.456 | 8760 |
| r3 | EV-043 | fail | - | other | last | 3/1 | 6.842 | 11971 |
| r3 | EV-044 | fail | - | other | last | 8/8 | 9.446 | 19837 |
| r3 | EV-045 | pass | sql | - | last | 1/1 | 3.357 | 9400 |
| r3 | EV-046 | fail | - | other | last | 7/7 | 3.315 | 7122 |
| r3 | EV-047 | fail | - | other | last | 4/1 | 4.99 | 9856 |
| r3 | EV-048 | pass | text | - | last | 1/1 | 5.034 | 7794 |
| r3 | EV-049 | pass | text | - | last | 1/7 | 3.47 | 7206 |
| r3 | EV-050 | pass | text | - | last | 1/1 | 4.658 | 6080 |

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

