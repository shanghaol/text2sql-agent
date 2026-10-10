# W3 基线评测报告：baseline_full_v2（2026-10-10 16:16:33）

## 运行配置
- 数据集：`eval\dataset\eval_v1.jsonl`（30 条，单轮）
- top_n=8 ｜ runs=3 ｜ 本次共 90 次题级运行
- 模型：deepseek-flash（temperature=0）｜ 快照锚点：2026-09-30
- 检索参数：top_k_each=20，RRF k=60（与线上 hybrid_retriever 默认一致，§2.3 同构）

## 四指标汇总

### 执行准确率（SQL 级：比较重放结果集，不看 SQL 文本）
- overall：**41.1%**（SQL 判过 37 ｜ 文本判过 21（计入答案级，见下节）｜ fail 32 ｜ error 0，共 90 次）
- 各 run：r1=30.0% ｜ r2=43.3% ｜ r3=50.0%（三次结果不一致的题见 flaky 节，§3.2 不机械取众数）
| 分组 | SQL 判过 | 文本判过 | fail | error | SQL 级准确率 | 答案级准确率 |
| --- | --- | --- | --- | --- | --- | --- |
| single_table | 8 | 4 | 6 | 0 | 44.4% | 66.7% |
| multi_join | 7 | 1 | 13 | 0 | 33.3% | 38.1% |
| aggregation | 8 | 8 | 5 | 0 | 38.1% | 76.2% |
| time_window | 8 | 1 | 6 | 0 | 53.3% | 60.0% |
| glossary_trap | 6 | 7 | 2 | 0 | 40.0% | 86.7% |
| 分组 | SQL 判过 | 文本判过 | fail | error | SQL 级准确率 | 答案级准确率 |
| --- | --- | --- | --- | --- | --- | --- |
| 有同款示例（EV-001~011，§1.3） | 23 | 10 | 0 | 0 | 69.7% | 100.0% |
| 泛化题（EV-012 起） | 14 | 11 | 32 | 0 | 24.6% | 43.9% |

### 回答准确率（答案级，含文本计算）
- overall：**64.4%**（pass_via：sql 37 + text 21，共 90 次）
- 差值说明：答案级 − SQL 级 = 23.3%（文本判过 21 题）= Agent 文本计算弥补单条 SQL 表达局限的部分，W6 消融时分别跟踪（§2.4 v2 / 声明⑤）

### Recall@8
- 整体逐片 Recall：**57.6%**（Σ|∩|=102 / Σ|expected|=177）
- 题级全命中率（expected 全部命中）：40.0%（36/90 题）
- 分 doc_type：schema 51.9% ｜ glossary 100.0% ｜ qa_pair 100.0%
- 分题型：single_table 62.5% ｜ multi_join 47.8% ｜ aggregation 70.0% ｜ time_window 60.0% ｜ glossary_trap 61.5%

### 延迟（wall-clock：search 前起、invoke 返回止，含全部工具往返）
- mean 5.6s ｜ median 3.2s ｜ P95 7.4s ｜ max 164.0s
- P95 口径：statistics.quantiles(n=20, method='inclusive')[18]（线性插值，§8-5 拍板）
- 分题型 P95：single_table 28.9s ｜ multi_join 7.4s ｜ aggregation 6.4s ｜ time_window 6.5s ｜ glossary_trap 4.9s
- 延迟绝对值受网络波动影响，仅相对可比（见声明③）

### token 成本（累加全部 AIMessage.usage_metadata，计费口径 §2.6）
- 单题均值：input 8446 ｜ output 659 ｜ total 9105（cache_read 7947 单列，不重复计）
- 总量：input 760131 ｜ output 59345 ｜ total 819476（cache_read 715258）；人民币估算按当期牌价手填旁注

## 对照行对比表
| 题型 | 本行（baseline_full_v2） | 对照（no_rag_control_v2） | 差值（百分点） |
| --- | --- | --- | --- |
| overall（答案级） | 64.4% | 43.3% | +21.1 |
| overall（SQL 级） | 41.1% | 6.7% | +34.4 |
| single_table | 44.4% | 16.7% | +27.8 |
| multi_join | 33.3% | 0.0% | +33.3 |
| aggregation | 38.1% | 14.3% | +23.8 |
| time_window | 53.3% | 0.0% | +53.3 |
| glossary_trap | 40.0% | 0.0% | +40.0 |

## 错答模式统计
| failure_mode | 次数 | 典型样本（case id） |
| --- | --- | --- |
| other | 32 | EV-013、EV-015、EV-016 |

trap:<id> 与 valid_alt_gloss 分开统计（§4.3）：前者是真陷阱，后者是合法口径但答非所问；other 为错出新花样，人工看明细 JSON 归类后可回填登记表。

## flaky 题记录
| id | 各 run 判定 |
| --- | --- |
| EV-019 | fail → pass → pass |
| EV-021 | fail → pass → pass |
| EV-022 | fail → pass → pass |
| EV-030 | fail → fail → pass |

flaky 题不机械取众数：人工复盘明细 JSON（三次的 SQL 差在哪），结论记入 experiment-log（§3.2）。

## 多轮评测节
（本次未运行多轮评测；多轮题用 `--multiturn` 跑 eval_v1_multiturn.jsonl，独立报告节 §1.7/§2.7。）

## 逐题结果
| run | id | 判定 | pass_via | failure_mode | sql_source | 行数(金/模) | 延迟s | tokens(total) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| r1 | EV-001 | pass | sql | - | last | 15/15 | 3.182 | 7612 |
| r1 | EV-002 | pass | sql | - | last | 5/5 | 3.143 | 9963 |
| r1 | EV-003 | pass | sql | - | last | 1/1 | 2.728 | 5371 |
| r1 | EV-004 | pass | text | - | last | 1/1 | 3.208 | 7252 |
| r1 | EV-005 | pass | text | - | last | 1/1 | 3.838 | 12258 |
| r1 | EV-006 | pass | sql | - | last | 1/1 | 2.371 | 6588 |
| r1 | EV-007 | pass | sql | - | last | 7/7 | 3.205 | 7075 |
| r1 | EV-008 | pass | text | - | last | 1/1 | 2.98 | 7198 |
| r1 | EV-009 | pass | text | - | last | 1/1 | 3.58 | 5767 |
| r1 | EV-010 | pass | sql | - | last | 3/3 | 2.557 | 10689 |
| r1 | EV-011 | pass | sql | - | last | 3/3 | 2.397 | 8263 |
| r1 | EV-012 | pass | text | - | last | 1/6 | 3.987 | 13333 |
| r1 | EV-013 | fail | - | other | last | 3/3 | 2.43 | 8461 |
| r1 | EV-014 | pass | sql | - | last | 6/6 | 3.225 | 7137 |
| r1 | EV-015 | fail | - | other | last | 5/5 | 5.019 | 8819 |
| r1 | EV-016 | fail | - | other | last | 20/1 | 6.21 | 11697 |
| r1 | EV-017 | fail | - | other | last | 8/1 | 5.629 | 13527 |
| r1 | EV-018 | fail | - | other | last | 20/1 | 5.064 | 12959 |
| r1 | EV-019 | fail | - | other | last | 5/5 | 3.242 | 8630 |
| r1 | EV-020 | fail | - | other | last | 1/8 | 7.406 | 16730 |
| r1 | EV-021 | fail | - | other | last | 5/5 | 2.869 | 7191 |
| r1 | EV-022 | fail | - | other | last | 3/3 | 3.079 | 9448 |
| r1 | EV-023 | pass | text | - | last | 1/1 | 4.21 | 9137 |
| r1 | EV-024 | fail | - | other | last | 20/1 | 6.417 | 12031 |
| r1 | EV-025 | pass | text | - | last | 1/2 | 2.248 | 6925 |
| r1 | EV-026 | fail | - | other | last | 7/7 | 3.149 | 7486 |
| r1 | EV-027 | fail | - | other | last | 12/72 | 4.384 | 6440 |
| r1 | EV-028 | pass | sql | - | last | 1/1 | 2.58 | 6942 |
| r1 | EV-029 | pass | text | - | last | 1/1 | 2.623 | 5480 |
| r1 | EV-030 | fail | - | other | last | 1/1 | 4.785 | 7980 |
| r2 | EV-001 | pass | sql | - | last | 15/15 | 3.458 | 7720 |
| r2 | EV-002 | pass | sql | - | last | 5/5 | 2.679 | 9914 |
| r2 | EV-003 | pass | sql | - | last | 1/1 | 1.805 | 5231 |
| r2 | EV-004 | pass | text | - | last | 1/1 | 3.352 | 7232 |
| r2 | EV-005 | pass | sql | - | last | 1/1 | 3.216 | 8113 |
| r2 | EV-006 | pass | text | - | last | 1/1 | 3.203 | 9936 |
| r2 | EV-007 | pass | sql | - | last | 7/7 | 2.859 | 7052 |
| r2 | EV-008 | pass | text | - | last | 1/1 | 2.155 | 7187 |
| r2 | EV-009 | pass | text | - | last | 1/1 | 2.716 | 5650 |
| r2 | EV-010 | pass | sql | - | last | 3/3 | 2.313 | 10747 |
| r2 | EV-011 | pass | sql | - | last | 3/3 | 2.674 | 8398 |
| r2 | EV-012 | pass | text | - | last | 1/6 | 3.11 | 9047 |
| r2 | EV-013 | fail | - | other | last | 3/3 | 3.469 | 8447 |
| r2 | EV-014 | pass | sql | - | last | 6/6 | 2.301 | 6950 |
| r2 | EV-015 | fail | - | other | last | 5/5 | 4.981 | 8595 |
| r2 | EV-016 | fail | - | other | last | 20/1 | 6.285 | 11862 |
| r2 | EV-017 | fail | - | other | last | 8/8 | 3.501 | 8427 |
| r2 | EV-018 | fail | - | other | last | 20/1 | 5.125 | 13031 |
| r2 | EV-019 | pass | sql | - | last | 5/5 | 2.753 | 8707 |
| r2 | EV-020 | fail | - | other | last | 1/1 | 6.392 | 16209 |
| r2 | EV-021 | pass | sql | - | last | 5/5 | 3.004 | 7072 |
| r2 | EV-022 | pass | sql | - | last | 3/3 | 3.032 | 9212 |
| r2 | EV-023 | pass | text | - | last | 1/1 | 4.204 | 13303 |
| r2 | EV-024 | fail | - | other | last | 20/1 | 5.52 | 11877 |
| r2 | EV-025 | pass | text | - | last | 1/2 | 2.818 | 6979 |
| r2 | EV-026 | fail | - | other | last | 7/7 | 3.061 | 7492 |
| r2 | EV-027 | fail | - | other | last | 12/12 | 7.306 | 10007 |
| r2 | EV-028 | pass | sql | - | last | 1/1 | 2.74 | 6749 |
| r2 | EV-029 | pass | sql | - | last | 1/1 | 4.319 | 8859 |
| r2 | EV-030 | fail | - | other | last | 1/1 | 4.365 | 8021 |
| r3 | EV-001 | pass | sql | - | last | 15/15 | 2.967 | 7493 |
| r3 | EV-002 | pass | sql | - | last | 5/5 | 3.12 | 9997 |
| r3 | EV-003 | pass | sql | - | last | 1/1 | 2.045 | 5357 |
| r3 | EV-004 | pass | sql | - | last | 1/1 | 3.049 | 7061 |
| r3 | EV-005 | pass | sql | - | last | 1/1 | 3.056 | 8140 |
| r3 | EV-006 | pass | text | - | last | 1/1 | 164.012 | 10425 |
| r3 | EV-007 | pass | sql | - | last | 7/7 | 3.053 | 7174 |
| r3 | EV-008 | pass | sql | - | last | 1/1 | 2.526 | 7169 |
| r3 | EV-009 | pass | text | - | last | 1/1 | 4.138 | 8428 |
| r3 | EV-010 | pass | sql | - | last | 3/3 | 2.608 | 10735 |
| r3 | EV-011 | pass | sql | - | last | 3/3 | 2.242 | 8328 |
| r3 | EV-012 | pass | sql | - | last | 1/1 | 2.931 | 8652 |
| r3 | EV-013 | fail | - | other | last | 3/3 | 2.345 | 8434 |
| r3 | EV-014 | pass | sql | - | last | 6/6 | 3.768 | 7478 |
| r3 | EV-015 | fail | - | other | last | 5/5 | 3.9 | 8519 |
| r3 | EV-016 | fail | - | other | last | 20/1 | 4.922 | 11565 |
| r3 | EV-017 | fail | - | other | last | 8/1 | 4.953 | 8888 |
| r3 | EV-018 | fail | - | other | last | 20/20 | 7.404 | 18470 |
| r3 | EV-019 | pass | sql | - | last | 5/5 | 3.238 | 8604 |
| r3 | EV-020 | fail | - | other | last | 1/1 | 12.6 | 24226 |
| r3 | EV-021 | pass | sql | - | last | 5/5 | 2.83 | 7064 |
| r3 | EV-022 | pass | sql | - | last | 3/3 | 3.338 | 9330 |
| r3 | EV-023 | pass | text | - | last | 1/1 | 3.138 | 8634 |
| r3 | EV-024 | fail | - | other | last | 20/1 | 9.787 | 17573 |
| r3 | EV-025 | pass | sql | - | last | 1/1 | 3.247 | 10612 |
| r3 | EV-026 | fail | - | other | last | 7/7 | 3.288 | 7409 |
| r3 | EV-027 | fail | - | other | last | 12/12 | 6.195 | 6579 |
| r3 | EV-028 | pass | text | - | last | 1/1 | 3.1 | 6825 |
| r3 | EV-029 | pass | text | - | last | 1/1 | 2.833 | 5682 |
| r3 | EV-030 | pass | text | - | last | 1/1 | 5.324 | 8210 |

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

