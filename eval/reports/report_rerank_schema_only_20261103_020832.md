# W3 基线评测报告：rerank_schema_only（2026-11-03 02:08:32）

## 运行配置
- 数据集：`eval\dataset\eval_v1.jsonl`（50 条，单轮）
- top_n=8 ｜ runs=3 ｜ 本次共 150 次题级运行
- 模型：deepseek-flash（temperature=0）｜ 快照锚点：2026-09-30
- 检索参数：rrf_k=60，BM25路=开，向量路=开，rerank=BAAI/bge-reranker-v2-m3（pool=16，scope=schema_only）（消融行，实际生效值，W6 §3.4）
- 语料：schema 6 / glossary 11 / qa_pair 11（共 28 片，索引自 data/rag_corpus 删除重建）

## 四指标汇总

### 执行准确率（SQL 级：比较重放结果集，不看 SQL 文本）
- overall：**20.0%**（SQL 判过 30 ｜ 文本判过 51（计入答案级，见下节）｜ fail 69 ｜ error 0，共 150 次）
- 各 run：r1=20.0% ｜ r2=22.0% ｜ r3=18.0%（三次结果不一致的题见 flaky 节，§3.2 不机械取众数）
| 分组 | SQL 判过 | 文本判过 | fail | error | SQL 级准确率 | 答案级准确率 |
| --- | --- | --- | --- | --- | --- | --- |
| single_table | 8 | 9 | 10 | 0 | 29.6% | 63.0% |
| multi_join | 5 | 6 | 31 | 0 | 11.9% | 26.2% |
| aggregation | 12 | 8 | 13 | 0 | 36.4% | 60.6% |
| time_window | 5 | 4 | 15 | 0 | 20.8% | 37.5% |
| glossary_trap | 0 | 24 | 0 | 0 | 0.0% | 100.0% |
| 分组 | SQL 判过 | 文本判过 | fail | error | SQL 级准确率 | 答案级准确率 |
| --- | --- | --- | --- | --- | --- | --- |
| 示例层（hit：EV-001~011 有同款示例，§1.3） | 13 | 16 | 4 | 0 | 39.4% | 87.9% |
| 近示例（near：泛化题，有同口径/同构 QA 可改窗口改维度套用，W6 §2.3） | 8 | 11 | 41 | 0 | 13.3% | 31.7% |
| 远示例（far：泛化题，须 schema+glossary 组合推理，W6 §2.3） | 9 | 24 | 24 | 0 | 15.8% | 57.9% |

### 回答准确率（答案级，含文本计算）
- overall：**54.0%**（pass_via：sql 30 + text 51，共 150 次）
- 差值说明：答案级 − SQL 级 = 34.0%（文本判过 51 题）= Agent 文本计算弥补单条 SQL 表达局限的部分，W6 消融时分别跟踪（§2.4 v2 / 声明⑤）

### Recall@8
- 整体逐片 Recall：**84.0%**（Σ|∩|=267 / Σ|expected|=318）
- 题级全命中率（expected 全部命中）：74.0%（111/150 题）
- 分 doc_type：schema 83.9% ｜ glossary 83.3% ｜ qa_pair 100.0%
- 分题型：single_table 100.0% ｜ multi_join 83.0% ｜ aggregation 77.8% ｜ time_window 90.0% ｜ glossary_trap 80.0%

### 延迟（wall-clock：search 前起、invoke 返回止，含全部工具往返）
- mean 4.9s ｜ median 4.2s ｜ P95 9.1s ｜ max 19.8s
- P95 口径：statistics.quantiles(n=20, method='inclusive')[18]（线性插值，§8-5 拍板）
- 分题型 P95：single_table 4.5s ｜ multi_join 14.2s ｜ aggregation 7.5s ｜ time_window 8.0s ｜ glossary_trap 6.3s
- 延迟绝对值受网络波动影响，仅相对可比（见声明③）

### token 成本（累加全部 AIMessage.usage_metadata，计费口径 §2.6）
- 单题均值：input 12231 ｜ output 745 ｜ total 12976（cache_read 11002 单列，不重复计）
- 总量：input 1834587 ｜ output 111772 ｜ total 1946359（cache_read 1650296）；人民币估算按当期牌价手填旁注

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
| EV-002 | pass → pass → fail |
| EV-021 | fail → pass → fail |
| EV-031 | fail → pass → pass |
| EV-039 | fail → pass → pass |
| EV-040 | fail → fail → pass |
| EV-044 | pass → fail → fail |

flaky 题不机械取众数：人工复盘明细 JSON（三次的 SQL 差在哪），结论记入 experiment-log（§3.2）。

## 多轮评测节
（本次未运行多轮评测；多轮题用 `--multiturn` 跑 eval_v1_multiturn.jsonl，独立报告节 §1.7/§2.7。）

## 逐题结果
| run | id | 判定 | pass_via | failure_mode | sql_source | 行数(金/模) | 延迟s | tokens(total) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| r1 | EV-001 | pass | sql | - | last | 15/15 | 3.772 | 10721 |
| r1 | EV-002 | pass | sql | - | last | 5/5 | 3.627 | 11872 |
| r1 | EV-003 | pass | text | - | last | 1/1 | 2.637 | 8020 |
| r1 | EV-004 | pass | text | - | last | 1/1 | 3.676 | 10427 |
| r1 | EV-005 | pass | sql | - | last | 1/1 | 3.006 | 9723 |
| r1 | EV-006 | pass | text | - | last | 1/1 | 3.503 | 17432 |
| r1 | EV-007 | fail | - | other | last | 7/7 | 3.087 | 8802 |
| r1 | EV-008 | pass | text | - | last | 1/1 | 3.945 | 15060 |
| r1 | EV-009 | pass | text | - | last | 1/1 | 3.293 | 8235 |
| r1 | EV-010 | pass | sql | - | last | 3/3 | 2.833 | 13441 |
| r1 | EV-011 | pass | sql | - | last | 3/3 | 4.146 | 12191 |
| r1 | EV-012 | pass | text | - | last | 1/6 | 3.722 | 19856 |
| r1 | EV-013 | fail | - | other | last | 3/3 | 2.714 | 11764 |
| r1 | EV-014 | pass | sql | - | last | 6/6 | 3.843 | 13704 |
| r1 | EV-015 | fail | - | other | last | 5/5 | 5.325 | 13398 |
| r1 | EV-016 | fail | - | other | last | 20/1 | 6.618 | 14375 |
| r1 | EV-017 | fail | - | other | last | 8/1 | 6.617 | 12766 |
| r1 | EV-018 | fail | - | other | last | 20/1 | 5.451 | 15827 |
| r1 | EV-019 | fail | - | other | last | 5/5 | 3.713 | 10631 |
| r1 | EV-020 | fail | - | other | last | 1/1 | 14.274 | 12523 |
| r1 | EV-021 | fail | - | other | last | 5/5 | 3.327 | 7047 |
| r1 | EV-022 | pass | sql | - | last | 3/3 | 3.827 | 9279 |
| r1 | EV-023 | pass | text | - | last | 1/1 | 4.892 | 10903 |
| r1 | EV-024 | fail | - | other | last | 20/20 | 7.373 | 12319 |
| r1 | EV-025 | pass | sql | - | last | 1/1 | 3.534 | 16081 |
| r1 | EV-026 | fail | - | other | last | 7/7 | 3.042 | 10897 |
| r1 | EV-027 | fail | - | other | last | 12/12 | 7.003 | 17971 |
| r1 | EV-028 | pass | text | - | last | 1/1 | 2.785 | 12199 |
| r1 | EV-029 | pass | text | - | last | 1/1 | 3.681 | 9082 |
| r1 | EV-030 | pass | text | - | last | 1/1 | 6.402 | 20193 |
| r1 | EV-031 | fail | - | other | last | 8/8 | 2.367 | 11695 |
| r1 | EV-032 | pass | text | - | last | 1/8 | 4.105 | 13434 |
| r1 | EV-033 | fail | - | other | last | 1/1 | 2.277 | 10285 |
| r1 | EV-034 | fail | - | other | last | 8/1 | 9.941 | 12673 |
| r1 | EV-035 | fail | - | other | last | 3/1 | 7.883 | 13569 |
| r1 | EV-036 | fail | - | other | last | 3/1 | 5.383 | 11242 |
| r1 | EV-037 | pass | text | - | last | 1/1 | 5.059 | 13722 |
| r1 | EV-038 | fail | - | other | last | 20/20 | 6.007 | 12699 |
| r1 | EV-039 | fail | - | other | last | 1/1 | 11.406 | 25385 |
| r1 | EV-040 | fail | - | other | last | 5/1 | 19.796 | 20117 |
| r1 | EV-041 | fail | - | other | last | 2/1 | 5.168 | 8724 |
| r1 | EV-042 | pass | sql | - | last | 3/3 | 3.295 | 9847 |
| r1 | EV-043 | fail | - | other | last | 3/3 | 3.786 | 10410 |
| r1 | EV-044 | pass | sql | - | last | 8/8 | 6.549 | 13092 |
| r1 | EV-045 | pass | text | - | last | 1/2 | 3.228 | 9038 |
| r1 | EV-046 | fail | - | other | last | 7/7 | 3.404 | 8848 |
| r1 | EV-047 | fail | - | other | last | 4/1 | 8.112 | 19676 |
| r1 | EV-048 | pass | text | - | last | 1/1 | 3.97 | 9190 |
| r1 | EV-049 | pass | text | - | last | 1/1 | 4.701 | 15446 |
| r1 | EV-050 | pass | text | - | last | 1/1 | 6.438 | 9218 |
| r2 | EV-001 | pass | sql | - | last | 15/15 | 4.44 | 10802 |
| r2 | EV-002 | pass | sql | - | last | 5/5 | 3.328 | 11849 |
| r2 | EV-003 | pass | text | - | last | 1/1 | 2.938 | 7948 |
| r2 | EV-004 | pass | text | - | last | 1/1 | 4.675 | 15861 |
| r2 | EV-005 | pass | text | - | last | 1/1 | 4.165 | 14874 |
| r2 | EV-006 | pass | text | - | last | 1/1 | 4.306 | 17360 |
| r2 | EV-007 | fail | - | other | last | 7/7 | 4.194 | 9060 |
| r2 | EV-008 | pass | text | - | last | 1/1 | 2.048 | 9897 |
| r2 | EV-009 | pass | text | - | last | 1/1 | 3.118 | 8194 |
| r2 | EV-010 | pass | sql | - | last | 3/3 | 2.585 | 13440 |
| r2 | EV-011 | pass | sql | - | last | 3/3 | 2.863 | 12008 |
| r2 | EV-012 | pass | text | - | last | 1/6 | 4.042 | 20128 |
| r2 | EV-013 | fail | - | other | last | 3/3 | 2.169 | 11767 |
| r2 | EV-014 | pass | sql | - | last | 6/6 | 3.104 | 13586 |
| r2 | EV-015 | fail | - | other | last | 5/5 | 4.538 | 13178 |
| r2 | EV-016 | fail | - | other | last | 20/1 | 5.878 | 14490 |
| r2 | EV-017 | fail | - | other | last | 8/8 | 7.762 | 19687 |
| r2 | EV-018 | fail | - | other | last | 20/1 | 6.921 | 21743 |
| r2 | EV-019 | fail | - | other | last | 5/5 | 3.81 | 10549 |
| r2 | EV-020 | fail | - | other | last | 1/1 | 7.053 | 9986 |
| r2 | EV-021 | pass | sql | - | last | 5/5 | 3.927 | 7046 |
| r2 | EV-022 | pass | sql | - | last | 3/3 | 3.054 | 9144 |
| r2 | EV-023 | pass | text | - | last | 1/1 | 5.443 | 16102 |
| r2 | EV-024 | fail | - | other | last | 20/20 | 7.834 | 12071 |
| r2 | EV-025 | pass | text | - | last | 1/2 | 2.67 | 10666 |
| r2 | EV-026 | fail | - | other | last | 7/7 | 2.924 | 10911 |
| r2 | EV-027 | fail | - | other | last | 12/12 | 7.288 | 12045 |
| r2 | EV-028 | pass | sql | - | last | 1/1 | 2.408 | 12116 |
| r2 | EV-029 | pass | text | - | last | 1/1 | 4.402 | 13418 |
| r2 | EV-030 | pass | text | - | last | 1/1 | 6.044 | 13290 |
| r2 | EV-031 | pass | sql | - | last | 8/8 | 2.105 | 11556 |
| r2 | EV-032 | pass | text | - | last | 1/1 | 2.835 | 8449 |
| r2 | EV-033 | fail | - | other | last | 1/5 | 2.388 | 10485 |
| r2 | EV-034 | fail | - | other | last | 8/8 | 8.885 | 12774 |
| r2 | EV-035 | fail | - | other | last | 3/3 | 6.355 | 12790 |
| r2 | EV-036 | fail | - | other | last | 3/1 | 4.968 | 11223 |
| r2 | EV-037 | pass | text | - | last | 1/1 | 3.903 | 13558 |
| r2 | EV-038 | fail | - | other | last | 20/1 | 9.906 | 20771 |
| r2 | EV-039 | pass | text | - | last | 1/1 | 14.992 | 26746 |
| r2 | EV-040 | fail | - | other | last | 5/5 | 6.169 | 18762 |
| r2 | EV-041 | fail | - | other | last | 2/1 | 3.132 | 8465 |
| r2 | EV-042 | pass | sql | - | last | 3/3 | 3.061 | 9871 |
| r2 | EV-043 | fail | - | other | last | 3/3 | 6.042 | 10953 |
| r2 | EV-044 | fail | - | other | last | 8/8 | 7.504 | 13656 |
| r2 | EV-045 | pass | sql | - | last | 1/1 | 4.053 | 9496 |
| r2 | EV-046 | fail | - | other | last | 7/7 | 3.102 | 8759 |
| r2 | EV-047 | fail | - | other | last | 4/1 | 7.569 | 19210 |
| r2 | EV-048 | pass | text | - | last | 1/1 | 4.504 | 9118 |
| r2 | EV-049 | pass | text | - | last | 1/1 | 4.717 | 10582 |
| r2 | EV-050 | pass | text | - | last | 1/1 | 5.859 | 13750 |
| r3 | EV-001 | pass | sql | - | last | 15/15 | 3.768 | 10947 |
| r3 | EV-002 | fail | - | other | last | 5/1 | 4.747 | 17991 |
| r3 | EV-003 | pass | text | - | last | 1/1 | 2.382 | 7960 |
| r3 | EV-004 | pass | text | - | last | 1/1 | 3.08 | 10327 |
| r3 | EV-005 | pass | sql | - | last | 1/1 | 2.904 | 9761 |
| r3 | EV-006 | pass | text | - | last | 1/1 | 2.94 | 11578 |
| r3 | EV-007 | fail | - | other | last | 7/7 | 3.994 | 8975 |
| r3 | EV-008 | pass | text | - | last | 1/1 | 2.747 | 9937 |
| r3 | EV-009 | pass | text | - | last | 1/1 | 4.431 | 8456 |
| r3 | EV-010 | pass | sql | - | last | 3/3 | 3.47 | 13548 |
| r3 | EV-011 | pass | sql | - | last | 3/3 | 2.247 | 11970 |
| r3 | EV-012 | pass | text | - | last | 1/6 | 4.225 | 20007 |
| r3 | EV-013 | fail | - | other | last | 3/3 | 2.546 | 11772 |
| r3 | EV-014 | pass | sql | - | last | 6/6 | 3.349 | 13727 |
| r3 | EV-015 | fail | - | other | last | 5/5 | 4.489 | 12985 |
| r3 | EV-016 | fail | - | other | last | 20/1 | 6.396 | 18838 |
| r3 | EV-017 | fail | - | other | last | 8/1 | 7.347 | 19170 |
| r3 | EV-018 | fail | - | other | last | 20/1 | 6.908 | 16199 |
| r3 | EV-019 | fail | - | other | last | 5/5 | 4.042 | 10649 |
| r3 | EV-020 | fail | - | other | last | 1/1 | 5.582 | 9369 |
| r3 | EV-021 | fail | - | other | last | 5/5 | 4.213 | 7124 |
| r3 | EV-022 | pass | sql | - | last | 3/3 | 3.196 | 9191 |
| r3 | EV-023 | pass | text | - | last | 1/1 | 2.392 | 10242 |
| r3 | EV-024 | fail | - | other | last | 20/1 | 7.425 | 23087 |
| r3 | EV-025 | pass | text | - | last | 1/2 | 3.269 | 10764 |
| r3 | EV-026 | fail | - | other | last | 7/7 | 2.918 | 10886 |
| r3 | EV-027 | fail | - | other | last | 12/5 | 8.461 | 23525 |
| r3 | EV-028 | pass | text | - | last | 1/1 | 3.151 | 12368 |
| r3 | EV-029 | pass | text | - | last | 1/1 | 4.53 | 8882 |
| r3 | EV-030 | pass | text | - | last | 1/1 | 4.772 | 13183 |
| r3 | EV-031 | pass | sql | - | last | 8/8 | 2.182 | 11582 |
| r3 | EV-032 | pass | text | - | last | 1/1 | 3.013 | 8459 |
| r3 | EV-033 | fail | - | other | last | 1/5 | 1.768 | 10396 |
| r3 | EV-034 | fail | - | other | last | 8/8 | 8.514 | 12408 |
| r3 | EV-035 | fail | - | other | last | 3/1 | 9.248 | 20300 |
| r3 | EV-036 | fail | - | other | last | 3/1 | 4.453 | 11000 |
| r3 | EV-037 | pass | text | - | last | 1/1 | 7.194 | 27645 |
| r3 | EV-038 | fail | - | other | last | 20/1 | 6.426 | 18915 |
| r3 | EV-039 | pass | text | - | last | 1/1 | 12.058 | 25898 |
| r3 | EV-040 | pass | sql | - | last | 5/5 | 4.337 | 12263 |
| r3 | EV-041 | fail | - | other | last | 2/2 | 4.316 | 12881 |
| r3 | EV-042 | pass | sql | - | last | 3/3 | 2.928 | 9883 |
| r3 | EV-043 | fail | - | other | last | 3/3 | 5.59 | 15563 |
| r3 | EV-044 | fail | - | other | last | 8/8 | 6.853 | 12676 |
| r3 | EV-045 | pass | text | - | last | 1/1 | 4.341 | 14300 |
| r3 | EV-046 | fail | - | other | last | 7/7 | 3.371 | 8820 |
| r3 | EV-047 | fail | - | other | last | 4/1 | 6.863 | 12952 |
| r3 | EV-048 | pass | text | - | last | 1/6 | 5.016 | 13323 |
| r3 | EV-049 | pass | text | - | last | 1/3 | 4.702 | 15484 |
| r3 | EV-050 | pass | text | - | last | 1/1 | 4.823 | 8954 |

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

