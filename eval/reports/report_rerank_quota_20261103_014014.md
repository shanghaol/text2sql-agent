# W3 基线评测报告：rerank_quota（2026-11-03 01:40:14）

## 运行配置
- 数据集：`eval\dataset\eval_v1.jsonl`（50 条，单轮）
- top_n=8 ｜ runs=3 ｜ 本次共 150 次题级运行
- 模型：deepseek-flash（temperature=0）｜ 快照锚点：2026-09-30
- 检索参数：rrf_k=60，BM25路=开，向量路=开，rerank=BAAI/bge-reranker-v2-m3（pool=16），quota=开（mode=cap，W7 离线标定）（消融行，实际生效值，W6 §3.4）
- 语料：schema 6 / glossary 11 / qa_pair 11（共 28 片，索引自 data/rag_corpus 删除重建）

## 四指标汇总

### 执行准确率（SQL 级：比较重放结果集，不看 SQL 文本）
- overall：**16.7%**（SQL 判过 25 ｜ 文本判过 52（计入答案级，见下节）｜ fail 73 ｜ error 0，共 150 次）
- 各 run：r1=18.0% ｜ r2=18.0% ｜ r3=14.0%（三次结果不一致的题见 flaky 节，§3.2 不机械取众数）
| 分组 | SQL 判过 | 文本判过 | fail | error | SQL 级准确率 | 答案级准确率 |
| --- | --- | --- | --- | --- | --- | --- |
| single_table | 8 | 9 | 10 | 0 | 29.6% | 63.0% |
| multi_join | 5 | 5 | 32 | 0 | 11.9% | 23.8% |
| aggregation | 9 | 9 | 15 | 0 | 27.3% | 54.5% |
| time_window | 3 | 6 | 15 | 0 | 12.5% | 37.5% |
| glossary_trap | 0 | 23 | 1 | 0 | 0.0% | 95.8% |
| 分组 | SQL 判过 | 文本判过 | fail | error | SQL 级准确率 | 答案级准确率 |
| --- | --- | --- | --- | --- | --- | --- |
| 示例层（hit：EV-001~011 有同款示例，§1.3） | 14 | 16 | 3 | 0 | 42.4% | 90.9% |
| 近示例（near：泛化题，有同口径/同构 QA 可改窗口改维度套用，W6 §2.3） | 2 | 12 | 46 | 0 | 3.3% | 23.3% |
| 远示例（far：泛化题，须 schema+glossary 组合推理，W6 §2.3） | 9 | 24 | 24 | 0 | 15.8% | 57.9% |

### 回答准确率（答案级，含文本计算）
- overall：**51.3%**（pass_via：sql 25 + text 52，共 150 次）
- 差值说明：答案级 − SQL 级 = 34.7%（文本判过 52 题）= Agent 文本计算弥补单条 SQL 表达局限的部分，W6 消融时分别跟踪（§2.4 v2 / 声明⑤）

### Recall@8
- 整体逐片 Recall：**84.9%**（Σ|∩|=270 / Σ|expected|=318）
- 题级全命中率（expected 全部命中）：74.0%（111/150 题）
- 分 doc_type：schema 83.9% ｜ glossary 91.7% ｜ qa_pair 100.0%
- 分题型：single_table 100.0% ｜ multi_join 83.0% ｜ aggregation 83.3% ｜ time_window 90.0% ｜ glossary_trap 80.0%

### 延迟（wall-clock：search 前起、invoke 返回止，含全部工具往返）
- mean 4.9s ｜ median 4.2s ｜ P95 9.8s ｜ max 17.7s
- P95 口径：statistics.quantiles(n=20, method='inclusive')[18]（线性插值，§8-5 拍板）
- 分题型 P95：single_table 5.7s ｜ multi_join 10.3s ｜ aggregation 8.8s ｜ time_window 6.8s ｜ glossary_trap 6.2s
- 延迟绝对值受网络波动影响，仅相对可比（见声明③）

### token 成本（累加全部 AIMessage.usage_metadata，计费口径 §2.6）
- 单题均值：input 11680 ｜ output 738 ｜ total 12417（cache_read 9971 单列，不重复计）
- 总量：input 1751945 ｜ output 110642 ｜ total 1862587（cache_read 1495677）；人民币估算按当期牌价手填旁注

## 对照行对比表
（本次运行未提供 --compare 明细文件；对照行对比由 T3 汇主行与对照行两份 detail JSON 后生成。）

## 错答模式统计
| failure_mode | 次数 | 典型样本（case id） |
| --- | --- | --- |
| other | 72 | EV-007、EV-013、EV-015 |
| valid_alt_gloss | 1 | EV-030 |

trap:<id> 与 valid_alt_gloss 分开统计（§4.3）：前者是真陷阱，后者是合法口径但答非所问；other 为错出新花样，人工看明细 JSON 归类后可回填登记表。

## flaky 题记录
| id | 各 run 判定 |
| --- | --- |
| EV-014 | pass → pass → fail |
| EV-030 | pass → fail → pass |
| EV-039 | fail → pass → fail |
| EV-042 | pass → fail → pass |
| EV-044 | fail → pass → fail |

flaky 题不机械取众数：人工复盘明细 JSON（三次的 SQL 差在哪），结论记入 experiment-log（§3.2）。

## 多轮评测节
（本次未运行多轮评测；多轮题用 `--multiturn` 跑 eval_v1_multiturn.jsonl，独立报告节 §1.7/§2.7。）

## 逐题结果
| run | id | 判定 | pass_via | failure_mode | sql_source | 行数(金/模) | 延迟s | tokens(total) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| r1 | EV-001 | pass | sql | - | last | 15/15 | 4.417 | 10637 |
| r1 | EV-002 | pass | sql | - | last | 5/5 | 4.422 | 11932 |
| r1 | EV-003 | pass | text | - | last | 1/1 | 3.115 | 7155 |
| r1 | EV-004 | pass | text | - | last | 1/1 | 5.826 | 10832 |
| r1 | EV-005 | pass | sql | - | last | 1/1 | 3.337 | 9480 |
| r1 | EV-006 | pass | text | - | last | 1/1 | 3.947 | 11697 |
| r1 | EV-007 | fail | - | other | last | 7/7 | 3.897 | 8268 |
| r1 | EV-008 | pass | text | - | last | 1/1 | 2.706 | 9547 |
| r1 | EV-009 | pass | text | - | last | 1/1 | 2.941 | 7585 |
| r1 | EV-010 | pass | sql | - | last | 3/3 | 2.519 | 13524 |
| r1 | EV-011 | pass | sql | - | last | 3/3 | 3.019 | 12132 |
| r1 | EV-012 | pass | text | - | last | 1/6 | 4.399 | 17509 |
| r1 | EV-013 | fail | - | other | last | 3/3 | 2.753 | 11643 |
| r1 | EV-014 | pass | sql | - | last | 6/6 | 2.954 | 13610 |
| r1 | EV-015 | fail | - | other | last | 5/5 | 4.764 | 13410 |
| r1 | EV-016 | fail | - | other | last | 20/1 | 5.58 | 13139 |
| r1 | EV-017 | fail | - | other | last | 8/1 | 9.677 | 20124 |
| r1 | EV-018 | fail | - | other | last | 20/1 | 6.173 | 15855 |
| r1 | EV-019 | fail | - | other | last | 5/5 | 3.808 | 10252 |
| r1 | EV-020 | fail | - | other | last | 1/1 | 12.036 | 15477 |
| r1 | EV-021 | fail | - | other | last | 5/5 | 4.199 | 6468 |
| r1 | EV-022 | pass | sql | - | last | 3/3 | 3.567 | 8454 |
| r1 | EV-023 | pass | text | - | last | 1/1 | 2.586 | 9989 |
| r1 | EV-024 | fail | - | other | last | 20/1 | 11.21 | 18615 |
| r1 | EV-025 | pass | text | - | last | 1/2 | 3.025 | 10301 |
| r1 | EV-026 | fail | - | other | last | 7/7 | 3.438 | 10497 |
| r1 | EV-027 | fail | - | other | last | 12/12 | 7.836 | 16684 |
| r1 | EV-028 | pass | text | - | last | 1/1 | 3.132 | 12313 |
| r1 | EV-029 | pass | text | - | last | 1/1 | 5.043 | 8437 |
| r1 | EV-030 | pass | text | - | last | 1/1 | 6.279 | 13409 |
| r1 | EV-031 | pass | sql | - | last | 8/8 | 2.405 | 11620 |
| r1 | EV-032 | pass | text | - | last | 1/8 | 4.121 | 8469 |
| r1 | EV-033 | fail | - | other | last | 1/3 | 2.586 | 10029 |
| r1 | EV-034 | fail | - | other | last | 8/8 | 5.952 | 11355 |
| r1 | EV-035 | fail | - | other | last | 3/1 | 6.422 | 12891 |
| r1 | EV-036 | fail | - | other | last | 3/1 | 4.739 | 10855 |
| r1 | EV-037 | pass | text | - | last | 1/1 | 9.979 | 22481 |
| r1 | EV-038 | fail | - | other | last | 20/1 | 7.581 | 25884 |
| r1 | EV-039 | fail | - | other | last | 1/1 | 9.707 | 23098 |
| r1 | EV-040 | fail | - | other | last | 5/5 | 5.312 | 12480 |
| r1 | EV-041 | fail | - | other | last | 2/2 | 4.189 | 7901 |
| r1 | EV-042 | pass | sql | - | last | 3/3 | 3.087 | 9594 |
| r1 | EV-043 | fail | - | other | last | 3/3 | 8.082 | 16733 |
| r1 | EV-044 | fail | - | other | last | 8/8 | 7.492 | 13143 |
| r1 | EV-045 | pass | text | - | last | 1/2 | 3.58 | 8700 |
| r1 | EV-046 | fail | - | other | last | 7/7 | 3.054 | 8206 |
| r1 | EV-047 | fail | - | other | last | 4/1 | 6.474 | 19260 |
| r1 | EV-048 | pass | text | - | last | 1/1 | 3.824 | 8103 |
| r1 | EV-049 | pass | text | - | last | 1/1 | 5.706 | 15539 |
| r1 | EV-050 | pass | text | - | last | 1/1 | 5.599 | 8484 |
| r2 | EV-001 | pass | sql | - | last | 15/15 | 3.754 | 10755 |
| r2 | EV-002 | pass | sql | - | last | 5/5 | 3.7 | 11967 |
| r2 | EV-003 | pass | text | - | last | 1/2 | 4.66 | 8000 |
| r2 | EV-004 | pass | text | - | last | 1/1 | 5.158 | 15440 |
| r2 | EV-005 | pass | sql | - | last | 1/1 | 2.398 | 9371 |
| r2 | EV-006 | pass | text | - | last | 1/1 | 17.728 | 31461 |
| r2 | EV-007 | fail | - | other | last | 7/7 | 3.513 | 8209 |
| r2 | EV-008 | pass | text | - | last | 1/1 | 2.536 | 9615 |
| r2 | EV-009 | pass | text | - | last | 1/1 | 4.801 | 11494 |
| r2 | EV-010 | pass | sql | - | last | 3/3 | 2.461 | 13538 |
| r2 | EV-011 | pass | sql | - | last | 3/3 | 2.864 | 12078 |
| r2 | EV-012 | pass | text | - | last | 1/20 | 3.983 | 12042 |
| r2 | EV-013 | fail | - | other | last | 3/3 | 2.426 | 11686 |
| r2 | EV-014 | pass | sql | - | last | 6/6 | 2.989 | 13643 |
| r2 | EV-015 | fail | - | other | last | 5/5 | 4.174 | 13201 |
| r2 | EV-016 | fail | - | other | last | 20/1 | 7.022 | 13425 |
| r2 | EV-017 | fail | - | other | last | 8/1 | 6.913 | 19358 |
| r2 | EV-018 | fail | - | other | last | 20/1 | 5.297 | 15625 |
| r2 | EV-019 | fail | - | other | last | 5/5 | 3.899 | 10246 |
| r2 | EV-020 | fail | - | other | last | 1/1 | 7.412 | 8865 |
| r2 | EV-021 | fail | - | other | last | 5/5 | 2.978 | 6167 |
| r2 | EV-022 | pass | sql | - | last | 3/3 | 3.394 | 8421 |
| r2 | EV-023 | pass | text | - | last | 1/1 | 4.835 | 10615 |
| r2 | EV-024 | fail | - | other | last | 20/1 | 6.15 | 11321 |
| r2 | EV-025 | pass | text | - | last | 1/1 | 3.69 | 15721 |
| r2 | EV-026 | fail | - | other | last | 7/7 | 3.211 | 10534 |
| r2 | EV-027 | fail | - | other | last | 12/12 | 6.869 | 11373 |
| r2 | EV-028 | pass | text | - | last | 1/1 | 2.286 | 12211 |
| r2 | EV-029 | pass | text | - | last | 1/1 | 4.023 | 8047 |
| r2 | EV-030 | fail | - | valid_alt_gloss | last | 1/1 | 4.801 | 13015 |
| r2 | EV-031 | pass | sql | - | last | 8/8 | 1.95 | 11575 |
| r2 | EV-032 | pass | text | - | last | 1/8 | 4.188 | 12135 |
| r2 | EV-033 | fail | - | other | last | 1/1 | 2.311 | 9895 |
| r2 | EV-034 | fail | - | other | last | 8/8 | 8.666 | 11995 |
| r2 | EV-035 | fail | - | other | last | 3/1 | 10.487 | 20362 |
| r2 | EV-036 | fail | - | other | last | 3/3 | 3.598 | 10602 |
| r2 | EV-037 | pass | text | - | last | 1/1 | 4.998 | 13934 |
| r2 | EV-038 | fail | - | other | last | 20/1 | 8.261 | 19307 |
| r2 | EV-039 | pass | text | - | last | 1/1 | 9.697 | 18692 |
| r2 | EV-040 | fail | - | other | last | 5/5 | 4.652 | 12151 |
| r2 | EV-041 | fail | - | other | last | 2/2 | 3.821 | 7705 |
| r2 | EV-042 | fail | - | other | last | 3/3 | 3.365 | 9878 |
| r2 | EV-043 | fail | - | other | last | 3/3 | 5.394 | 15207 |
| r2 | EV-044 | pass | sql | - | last | 8/8 | 8.181 | 19596 |
| r2 | EV-045 | pass | text | - | last | 1/2 | 3.425 | 8581 |
| r2 | EV-046 | fail | - | other | last | 7/7 | 3.65 | 8222 |
| r2 | EV-047 | fail | - | other | last | 4/1 | 5.281 | 12612 |
| r2 | EV-048 | pass | text | - | last | 1/1 | 4.528 | 8320 |
| r2 | EV-049 | pass | text | - | last | 1/6 | 5.234 | 10267 |
| r2 | EV-050 | pass | text | - | last | 1/1 | 3.5 | 7829 |
| r3 | EV-001 | pass | sql | - | last | 15/15 | 3.445 | 10640 |
| r3 | EV-002 | pass | sql | - | last | 5/5 | 3.118 | 11921 |
| r3 | EV-003 | pass | text | - | last | 1/1 | 2.433 | 7252 |
| r3 | EV-004 | pass | text | - | last | 1/1 | 5.001 | 10671 |
| r3 | EV-005 | pass | text | - | last | 1/1 | 3.772 | 14450 |
| r3 | EV-006 | pass | text | - | last | 1/1 | 6.044 | 18097 |
| r3 | EV-007 | fail | - | other | last | 7/7 | 4.214 | 8314 |
| r3 | EV-008 | pass | text | - | last | 1/1 | 2.447 | 9590 |
| r3 | EV-009 | pass | text | - | last | 1/1 | 3.483 | 7716 |
| r3 | EV-010 | pass | sql | - | last | 3/3 | 3.408 | 13588 |
| r3 | EV-011 | pass | sql | - | last | 3/3 | 3.231 | 12132 |
| r3 | EV-012 | pass | text | - | last | 1/6 | 3.627 | 17714 |
| r3 | EV-013 | fail | - | other | last | 3/3 | 2.986 | 11672 |
| r3 | EV-014 | fail | - | other | last | 6/1 | 3.685 | 13927 |
| r3 | EV-015 | fail | - | other | last | 5/5 | 4.77 | 13106 |
| r3 | EV-016 | fail | - | other | last | 20/1 | 5.777 | 13169 |
| r3 | EV-017 | fail | - | other | last | 8/1 | 6.755 | 19018 |
| r3 | EV-018 | fail | - | other | last | 20/1 | 7.147 | 15811 |
| r3 | EV-019 | fail | - | other | last | 5/5 | 3.614 | 10147 |
| r3 | EV-020 | fail | - | other | last | 1/1 | 5.361 | 8436 |
| r3 | EV-021 | fail | - | other | last | 5/5 | 3.202 | 6234 |
| r3 | EV-022 | pass | sql | - | last | 3/3 | 3.229 | 8316 |
| r3 | EV-023 | pass | text | - | last | 1/1 | 4.753 | 10485 |
| r3 | EV-024 | fail | - | other | last | 20/1 | 9.831 | 24034 |
| r3 | EV-025 | pass | text | - | last | 1/2 | 2.793 | 10225 |
| r3 | EV-026 | fail | - | other | last | 7/7 | 4.011 | 10593 |
| r3 | EV-027 | fail | - | other | last | 12/12 | 5.391 | 10939 |
| r3 | EV-028 | pass | text | - | last | 1/1 | 2.755 | 12288 |
| r3 | EV-029 | pass | text | - | last | 1/1 | 3.991 | 8154 |
| r3 | EV-030 | pass | text | - | last | 1/1 | 5.328 | 13009 |
| r3 | EV-031 | pass | sql | - | last | 8/8 | 1.823 | 11578 |
| r3 | EV-032 | pass | text | - | last | 1/8 | 4.516 | 12246 |
| r3 | EV-033 | fail | - | other | last | 1/5 | 3.847 | 10062 |
| r3 | EV-034 | fail | - | other | last | 8/8 | 10.096 | 12558 |
| r3 | EV-035 | fail | - | other | last | 3/1 | 5.975 | 12839 |
| r3 | EV-036 | fail | - | other | last | 3/1 | 4.756 | 10974 |
| r3 | EV-037 | pass | text | - | last | 1/1 | 8.049 | 14783 |
| r3 | EV-038 | fail | - | other | last | 20/1 | 9.026 | 31395 |
| r3 | EV-039 | fail | - | other | last | 1/1 | 10.343 | 24230 |
| r3 | EV-040 | fail | - | other | last | 5/5 | 4.325 | 11963 |
| r3 | EV-041 | fail | - | other | last | 2/1 | 3.492 | 7677 |
| r3 | EV-042 | pass | sql | - | last | 3/3 | 3.23 | 9574 |
| r3 | EV-043 | fail | - | other | last | 3/3 | 6.93 | 16022 |
| r3 | EV-044 | fail | - | other | last | 8/8 | 5.764 | 12628 |
| r3 | EV-045 | pass | text | - | last | 1/6 | 4.558 | 9129 |
| r3 | EV-046 | fail | - | other | last | 7/7 | 3.012 | 8137 |
| r3 | EV-047 | fail | - | other | last | 4/1 | 6.712 | 13532 |
| r3 | EV-048 | pass | text | - | last | 1/1 | 3.345 | 7934 |
| r3 | EV-049 | pass | text | - | last | 1/1 | 6.337 | 15627 |
| r3 | EV-050 | pass | text | - | last | 1/1 | 4.005 | 7942 |

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

