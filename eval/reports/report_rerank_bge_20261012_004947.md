# W3 基线评测报告：rerank_bge（2026-10-12 00:49:47）

## 运行配置
- 数据集：`eval\dataset\eval_v1.jsonl`（50 条，单轮）
- top_n=8 ｜ runs=3 ｜ 本次共 150 次题级运行
- 模型：deepseek-flash（temperature=0）｜ 快照锚点：2026-09-30
- 检索参数：rrf_k=60，BM25路=开，向量路=开，rerank=BAAI/bge-reranker-v2-m3（pool=16）（消融行，实际生效值，W6 §3.4）
- 语料：schema 6 / glossary 11 / qa_pair 11（共 28 片，索引自 data/rag_corpus 删除重建）

## 四指标汇总

### 执行准确率（SQL 级：比较重放结果集，不看 SQL 文本）
- overall：**22.7%**（SQL 判过 34 ｜ 文本判过 47（计入答案级，见下节）｜ fail 69 ｜ error 0，共 150 次）
- 各 run：r1=20.0% ｜ r2=26.0% ｜ r3=22.0%（三次结果不一致的题见 flaky 节，§3.2 不机械取众数）
| 分组 | SQL 判过 | 文本判过 | fail | error | SQL 级准确率 | 答案级准确率 |
| --- | --- | --- | --- | --- | --- | --- |
| single_table | 8 | 9 | 10 | 0 | 29.6% | 63.0% |
| multi_join | 4 | 5 | 33 | 0 | 9.5% | 21.4% |
| aggregation | 11 | 9 | 13 | 0 | 33.3% | 60.6% |
| time_window | 9 | 5 | 10 | 0 | 37.5% | 58.3% |
| glossary_trap | 2 | 19 | 3 | 0 | 8.3% | 87.5% |
| 分组 | SQL 判过 | 文本判过 | fail | error | SQL 级准确率 | 答案级准确率 |
| --- | --- | --- | --- | --- | --- | --- |
| 示例层（hit：EV-001~011 有同款示例，§1.3） | 16 | 15 | 2 | 0 | 48.5% | 93.9% |
| 近示例（near：泛化题，有同口径/同构 QA 可改窗口改维度套用，W6 §2.3） | 8 | 8 | 44 | 0 | 13.3% | 26.7% |
| 远示例（far：泛化题，须 schema+glossary 组合推理，W6 §2.3） | 10 | 24 | 23 | 0 | 17.5% | 59.6% |

### 回答准确率（答案级，含文本计算）
- overall：**54.0%**（pass_via：sql 34 + text 47，共 150 次）
- 差值说明：答案级 − SQL 级 = 31.3%（文本判过 47 题）= Agent 文本计算弥补单条 SQL 表达局限的部分，W6 消融时分别跟踪（§2.4 v2 / 声明⑤）

### Recall@8
- 整体逐片 Recall：**84.0%**（Σ|∩|=267 / Σ|expected|=318）
- 题级全命中率（expected 全部命中）：74.0%（111/150 题）
- 分 doc_type：schema 83.9% ｜ glossary 83.3% ｜ qa_pair 100.0%
- 分题型：single_table 100.0% ｜ multi_join 83.0% ｜ aggregation 83.3% ｜ time_window 90.0% ｜ glossary_trap 75.0%

### 延迟（wall-clock：search 前起、invoke 返回止，含全部工具往返）
- mean 5.1s ｜ median 4.4s ｜ P95 10.1s ｜ max 24.8s
- P95 口径：statistics.quantiles(n=20, method='inclusive')[18]（线性插值，§8-5 拍板）
- 分题型 P95：single_table 5.4s ｜ multi_join 11.6s ｜ aggregation 7.7s ｜ time_window 6.6s ｜ glossary_trap 7.5s
- 延迟绝对值受网络波动影响，仅相对可比（见声明③）

### token 成本（累加全部 AIMessage.usage_metadata，计费口径 §2.6）
- 单题均值：input 12411 ｜ output 789 ｜ total 13200（cache_read 10691 单列，不重复计）
- 总量：input 1861639 ｜ output 118323 ｜ total 1979962（cache_read 1603582）；人民币估算按当期牌价手填旁注

## 对照行对比表
（本次运行未提供 --compare 明细文件；对照行对比由 T3 汇主行与对照行两份 detail JSON 后生成。）

## 错答模式统计
| failure_mode | 次数 | 典型样本（case id） |
| --- | --- | --- |
| other | 69 | EV-007、EV-013、EV-015 |

trap:<id> 与 valid_alt_gloss 分开统计（§4.3）：前者是真陷阱，后者是合法口径但答非所问；other 为错出新花样，人工看明细 JSON 归类后可回填登记表。

## flaky 题记录
| id | 各 run 判定 |
| --- | --- |
| EV-007 | fail → fail → pass |
| EV-014 | pass → pass → fail |
| EV-021 | fail → pass → fail |
| EV-022 | pass → fail → pass |
| EV-042 | fail → pass → pass |
| EV-046 | fail → pass → fail |

flaky 题不机械取众数：人工复盘明细 JSON（三次的 SQL 差在哪），结论记入 experiment-log（§3.2）。

## 多轮评测节
（本次未运行多轮评测；多轮题用 `--multiturn` 跑 eval_v1_multiturn.jsonl，独立报告节 §1.7/§2.7。）

## 逐题结果
| run | id | 判定 | pass_via | failure_mode | sql_source | 行数(金/模) | 延迟s | tokens(total) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| r1 | EV-001 | pass | sql | - | last | 15/15 | 3.509 | 10927 |
| r1 | EV-002 | pass | sql | - | last | 5/5 | 2.904 | 11930 |
| r1 | EV-003 | pass | text | - | last | 1/1 | 3.12 | 7957 |
| r1 | EV-004 | pass | text | - | last | 1/1 | 3.934 | 10358 |
| r1 | EV-005 | pass | text | - | last | 1/1 | 5.239 | 15152 |
| r1 | EV-006 | pass | text | - | last | 1/1 | 4.977 | 15393 |
| r1 | EV-007 | fail | - | other | last | 7/7 | 5.039 | 9252 |
| r1 | EV-008 | pass | text | - | last | 1/1 | 3.935 | 15098 |
| r1 | EV-009 | pass | text | - | last | 1/1 | 3.741 | 8364 |
| r1 | EV-010 | pass | sql | - | last | 3/3 | 3.074 | 13574 |
| r1 | EV-011 | pass | sql | - | last | 3/3 | 3.009 | 12111 |
| r1 | EV-012 | pass | text | - | last | 1/6 | 3.751 | 17480 |
| r1 | EV-013 | fail | - | other | last | 3/3 | 3.086 | 11707 |
| r1 | EV-014 | pass | sql | - | last | 6/6 | 2.954 | 13610 |
| r1 | EV-015 | fail | - | other | last | 5/5 | 5.801 | 13560 |
| r1 | EV-016 | fail | - | other | last | 20/0 | 5.666 | 14125 |
| r1 | EV-017 | fail | - | other | last | 8/1 | 20.94 | 44479 |
| r1 | EV-018 | fail | - | other | last | 20/1 | 6.444 | 15984 |
| r1 | EV-019 | fail | - | other | last | 5/5 | 5.135 | 10829 |
| r1 | EV-020 | fail | - | other | last | 1/3 | 8.915 | 15735 |
| r1 | EV-021 | fail | - | other | last | 5/5 | 4.765 | 11038 |
| r1 | EV-022 | pass | sql | - | last | 3/3 | 3.292 | 9199 |
| r1 | EV-023 | pass | text | - | last | 1/1 | 5.157 | 16054 |
| r1 | EV-024 | fail | - | other | last | 20/1 | 6.154 | 11924 |
| r1 | EV-025 | pass | text | - | last | 1/2 | 3.071 | 10667 |
| r1 | EV-026 | fail | - | other | last | 7/7 | 3.416 | 10831 |
| r1 | EV-027 | pass | sql | - | last | 12/12 | 5.774 | 11033 |
| r1 | EV-028 | pass | text | - | last | 1/1 | 3.225 | 12259 |
| r1 | EV-029 | pass | text | - | last | 1/1 | 7.679 | 20256 |
| r1 | EV-030 | fail | - | other | last | 1/1 | 5.347 | 11384 |
| r1 | EV-031 | pass | sql | - | last | 8/8 | 2.847 | 11559 |
| r1 | EV-032 | pass | text | - | last | 1/8 | 3.359 | 8842 |
| r1 | EV-033 | fail | - | other | last | 1/5 | 2.331 | 10328 |
| r1 | EV-034 | fail | - | other | last | 8/8 | 10.74 | 18949 |
| r1 | EV-035 | fail | - | other | last | 3/8 | 11.628 | 21001 |
| r1 | EV-036 | fail | - | other | last | 3/1 | 4.765 | 11284 |
| r1 | EV-037 | pass | text | - | last | 1/1 | 6.116 | 20945 |
| r1 | EV-038 | fail | - | other | last | 20/1 | 11.167 | 32860 |
| r1 | EV-039 | fail | - | other | last | 1/8 | 24.778 | 34269 |
| r1 | EV-040 | fail | - | other | last | 5/1 | 5.002 | 12622 |
| r1 | EV-041 | fail | - | other | last | 2/1 | 3.683 | 8577 |
| r1 | EV-042 | fail | - | other | last | 3/3 | 2.401 | 9821 |
| r1 | EV-043 | fail | - | other | last | 3/3 | 5.661 | 11067 |
| r1 | EV-044 | pass | sql | - | last | 8/8 | 5.599 | 12688 |
| r1 | EV-045 | pass | sql | - | last | 1/1 | 4.737 | 14357 |
| r1 | EV-046 | fail | - | other | last | 7/7 | 3.777 | 8890 |
| r1 | EV-047 | fail | - | other | last | 4/1 | 6.679 | 11268 |
| r1 | EV-048 | pass | text | - | last | 1/1 | 3.886 | 8912 |
| r1 | EV-049 | pass | text | - | last | 1/1 | 3.96 | 10131 |
| r1 | EV-050 | pass | text | - | last | 1/1 | 4.853 | 8794 |
| r2 | EV-001 | pass | sql | - | last | 15/15 | 4.191 | 11160 |
| r2 | EV-002 | pass | sql | - | last | 5/5 | 3.216 | 11905 |
| r2 | EV-003 | pass | sql | - | last | 1/1 | 2.686 | 7895 |
| r2 | EV-004 | pass | text | - | last | 1/1 | 4.798 | 10488 |
| r2 | EV-005 | pass | sql | - | last | 1/1 | 3.988 | 9809 |
| r2 | EV-006 | pass | text | - | last | 1/1 | 4.156 | 10234 |
| r2 | EV-007 | fail | - | other | last | 7/7 | 4.458 | 8999 |
| r2 | EV-008 | pass | text | - | last | 1/1 | 4.625 | 15022 |
| r2 | EV-009 | pass | text | - | last | 1/1 | 5.12 | 12488 |
| r2 | EV-010 | pass | sql | - | last | 3/3 | 3.522 | 13624 |
| r2 | EV-011 | pass | sql | - | last | 3/3 | 3.147 | 12112 |
| r2 | EV-012 | pass | text | - | last | 1/6 | 3.587 | 11846 |
| r2 | EV-013 | fail | - | other | last | 3/3 | 2.941 | 11704 |
| r2 | EV-014 | pass | sql | - | last | 6/6 | 3.75 | 13701 |
| r2 | EV-015 | fail | - | other | last | 5/5 | 5.128 | 13379 |
| r2 | EV-016 | fail | - | other | last | 20/1 | 6.211 | 14234 |
| r2 | EV-017 | fail | - | other | last | 8/8 | 9.015 | 20271 |
| r2 | EV-018 | fail | - | other | last | 20/1 | 6.394 | 15956 |
| r2 | EV-019 | fail | - | other | last | 5/5 | 4.066 | 10497 |
| r2 | EV-020 | fail | - | other | last | 1/8 | 11.608 | 21436 |
| r2 | EV-021 | pass | sql | - | last | 5/5 | 3.607 | 7143 |
| r2 | EV-022 | fail | - | other | last | 3/3 | 4.564 | 9529 |
| r2 | EV-023 | pass | text | - | last | 1/1 | 5.253 | 15891 |
| r2 | EV-024 | fail | - | other | last | 20/1 | 7.862 | 17333 |
| r2 | EV-025 | pass | text | - | last | 1/2 | 4.287 | 10760 |
| r2 | EV-026 | fail | - | other | last | 7/7 | 3.779 | 10799 |
| r2 | EV-027 | pass | sql | - | last | 12/12 | 6.444 | 11383 |
| r2 | EV-028 | pass | text | - | last | 1/1 | 4.161 | 12387 |
| r2 | EV-029 | pass | text | - | last | 1/1 | 6.605 | 14365 |
| r2 | EV-030 | fail | - | other | last | 1/1 | 5.133 | 11156 |
| r2 | EV-031 | pass | sql | - | last | 8/8 | 2.708 | 11514 |
| r2 | EV-032 | pass | text | - | last | 1/8 | 5.529 | 13581 |
| r2 | EV-033 | fail | - | other | last | 1/1 | 2.775 | 10356 |
| r2 | EV-034 | fail | - | other | last | 8/8 | 9.476 | 12632 |
| r2 | EV-035 | fail | - | other | last | 3/1 | 7.192 | 13414 |
| r2 | EV-036 | fail | - | other | last | 3/3 | 3.681 | 10946 |
| r2 | EV-037 | pass | text | - | last | 1/1 | 7.19 | 20982 |
| r2 | EV-038 | fail | - | other | last | 20/1 | 7.718 | 25690 |
| r2 | EV-039 | fail | - | other | last | 1/8 | 9.48 | 20884 |
| r2 | EV-040 | fail | - | other | last | 5/5 | 5.112 | 12227 |
| r2 | EV-041 | fail | - | other | last | 2/2 | 3.437 | 8381 |
| r2 | EV-042 | pass | sql | - | last | 3/3 | 3.689 | 9973 |
| r2 | EV-043 | fail | - | other | last | 3/3 | 4.632 | 10873 |
| r2 | EV-044 | pass | sql | - | last | 8/8 | 5.586 | 12545 |
| r2 | EV-045 | pass | text | - | last | 1/2 | 3.858 | 9423 |
| r2 | EV-046 | pass | sql | - | last | 7/7 | 3.11 | 8753 |
| r2 | EV-047 | fail | - | other | last | 4/1 | 6.122 | 11437 |
| r2 | EV-048 | pass | text | - | last | 1/6 | 4.299 | 13410 |
| r2 | EV-049 | pass | text | - | last | 1/1 | 3.321 | 10127 |
| r2 | EV-050 | pass | text | - | last | 1/1 | 5.364 | 9157 |
| r3 | EV-001 | pass | sql | - | last | 15/15 | 3.864 | 11132 |
| r3 | EV-002 | pass | sql | - | last | 5/5 | 3.377 | 11961 |
| r3 | EV-003 | pass | sql | - | last | 1/1 | 2.407 | 7796 |
| r3 | EV-004 | pass | text | - | last | 1/1 | 6.092 | 16295 |
| r3 | EV-005 | pass | text | - | last | 1/1 | 4.905 | 15332 |
| r3 | EV-006 | pass | text | - | last | 1/1 | 4.331 | 15091 |
| r3 | EV-007 | pass | sql | - | last | 7/7 | 3.191 | 8918 |
| r3 | EV-008 | pass | text | - | last | 1/1 | 2.856 | 10014 |
| r3 | EV-009 | pass | text | - | last | 1/1 | 3.148 | 8310 |
| r3 | EV-010 | pass | sql | - | last | 3/3 | 2.736 | 13536 |
| r3 | EV-011 | pass | sql | - | last | 3/3 | 2.941 | 12121 |
| r3 | EV-012 | pass | text | - | last | 1/6 | 4.184 | 17610 |
| r3 | EV-013 | fail | - | other | last | 3/3 | 2.254 | 11707 |
| r3 | EV-014 | fail | - | other | last | 6/1 | 3.18 | 13778 |
| r3 | EV-015 | fail | - | other | last | 5/5 | 4.425 | 13041 |
| r3 | EV-016 | fail | - | other | last | 20/1 | 5.37 | 14270 |
| r3 | EV-017 | fail | - | other | last | 8/2 | 6.953 | 19378 |
| r3 | EV-018 | fail | - | other | last | 20/1 | 5.056 | 15861 |
| r3 | EV-019 | fail | - | other | last | 5/5 | 3.447 | 10536 |
| r3 | EV-020 | fail | - | other | last | 1/1 | 9.123 | 15453 |
| r3 | EV-021 | fail | - | other | last | 5/5 | 4.189 | 7577 |
| r3 | EV-022 | pass | sql | - | last | 3/3 | 3.392 | 9225 |
| r3 | EV-023 | pass | text | - | last | 1/1 | 5.695 | 11022 |
| r3 | EV-024 | fail | - | other | last | 20/1 | 8.405 | 17847 |
| r3 | EV-025 | pass | text | - | last | 1/2 | 3.45 | 10858 |
| r3 | EV-026 | fail | - | other | last | 7/7 | 3.143 | 10806 |
| r3 | EV-027 | pass | sql | - | last | 12/12 | 5.025 | 11082 |
| r3 | EV-028 | pass | text | - | last | 1/1 | 2.872 | 12233 |
| r3 | EV-029 | pass | text | - | last | 1/1 | 9.428 | 21259 |
| r3 | EV-030 | fail | - | other | last | 1/1 | 4.827 | 11352 |
| r3 | EV-031 | pass | sql | - | last | 8/8 | 1.714 | 11479 |
| r3 | EV-032 | pass | text | - | last | 1/1 | 2.398 | 8425 |
| r3 | EV-033 | fail | - | other | last | 1/3 | 2.22 | 10416 |
| r3 | EV-034 | fail | - | other | last | 8/8 | 6.651 | 12012 |
| r3 | EV-035 | fail | - | other | last | 3/1 | 11.198 | 21268 |
| r3 | EV-036 | fail | - | other | last | 3/1 | 4.379 | 11308 |
| r3 | EV-037 | pass | text | - | last | 1/1 | 5.145 | 14146 |
| r3 | EV-038 | fail | - | other | last | 20/1 | 7.872 | 25971 |
| r3 | EV-039 | fail | - | other | last | 1/8 | 10.625 | 26426 |
| r3 | EV-040 | fail | - | other | last | 5/1 | 5.989 | 12808 |
| r3 | EV-041 | fail | - | other | last | 2/2 | 3.166 | 8457 |
| r3 | EV-042 | pass | sql | - | last | 3/3 | 3.893 | 10291 |
| r3 | EV-043 | fail | - | other | last | 3/3 | 4.361 | 10710 |
| r3 | EV-044 | pass | sql | - | last | 8/8 | 7.555 | 13702 |
| r3 | EV-045 | pass | text | - | last | 1/2 | 2.851 | 9263 |
| r3 | EV-046 | fail | - | other | last | 7/7 | 3.064 | 8900 |
| r3 | EV-047 | fail | - | other | last | 4/8 | 6.771 | 16962 |
| r3 | EV-048 | pass | text | - | last | 1/6 | 4.226 | 9505 |
| r3 | EV-049 | pass | text | - | last | 1/1 | 3.003 | 10087 |
| r3 | EV-050 | pass | text | - | last | 1/1 | 4.657 | 9180 |

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

