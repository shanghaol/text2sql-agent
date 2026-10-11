# W3 基线评测报告：no_fewshot（2026-10-12 01:02:52）

## 运行配置
- 数据集：`eval\dataset\eval_v1.jsonl`（50 条，单轮）
- top_n=8 ｜ runs=3 ｜ 本次共 150 次题级运行
- 模型：deepseek-flash（temperature=0）｜ 快照锚点：2026-09-30
- 检索参数：rrf_k=60，BM25路=开，向量路=开，doc_types=schema+glossary（去 few-shot）（消融行，实际生效值，W6 §3.4）
- 语料：schema 6 / glossary 11 / qa_pair 11（共 28 片，索引自 data/rag_corpus 删除重建）

## 四指标汇总

### 执行准确率（SQL 级：比较重放结果集，不看 SQL 文本）
- overall：**10.0%**（SQL 判过 15 ｜ 文本判过 48（计入答案级，见下节）｜ fail 87 ｜ error 0，共 150 次）
- 各 run：r1=12.0% ｜ r2=8.0% ｜ r3=10.0%（三次结果不一致的题见 flaky 节，§3.2 不机械取众数）
| 分组 | SQL 判过 | 文本判过 | fail | error | SQL 级准确率 | 答案级准确率 |
| --- | --- | --- | --- | --- | --- | --- |
| single_table | 5 | 9 | 13 | 0 | 18.5% | 51.9% |
| multi_join | 2 | 8 | 32 | 0 | 4.8% | 23.8% |
| aggregation | 4 | 9 | 20 | 0 | 12.1% | 39.4% |
| time_window | 4 | 6 | 14 | 0 | 16.7% | 41.7% |
| glossary_trap | 0 | 16 | 8 | 0 | 0.0% | 66.7% |
| 分组 | SQL 判过 | 文本判过 | fail | error | SQL 级准确率 | 答案级准确率 |
| --- | --- | --- | --- | --- | --- | --- |
| 示例层（hit：EV-001~011 有同款示例，§1.3） | 3 | 13 | 17 | 0 | 9.1% | 48.5% |
| 近示例（near：泛化题，有同口径/同构 QA 可改窗口改维度套用，W6 §2.3） | 5 | 11 | 44 | 0 | 8.3% | 26.7% |
| 远示例（far：泛化题，须 schema+glossary 组合推理，W6 §2.3） | 7 | 24 | 26 | 0 | 12.3% | 54.4% |

### 回答准确率（答案级，含文本计算）
- overall：**42.0%**（pass_via：sql 15 + text 48，共 150 次）
- 差值说明：答案级 − SQL 级 = 32.0%（文本判过 48 题）= Agent 文本计算弥补单条 SQL 表达局限的部分，W6 消融时分别跟踪（§2.4 v2 / 声明⑤）

### Recall@8
- 整体逐片 Recall：**82.1%**（Σ|∩|=261 / Σ|expected|=318）
- 题级全命中率（expected 全部命中）：70.0%（105/150 题）
- 分 doc_type：schema 80.6% ｜ glossary 100.0% ｜ qa_pair 0.0%
- 分题型：single_table 100.0% ｜ multi_join 76.6% ｜ aggregation 88.9% ｜ time_window 100.0% ｜ glossary_trap 70.0%

### 延迟（wall-clock：search 前起、invoke 返回止，含全部工具往返）
- mean 5.8s ｜ median 4.9s ｜ P95 11.6s ｜ max 15.0s
- P95 口径：statistics.quantiles(n=20, method='inclusive')[18]（线性插值，§8-5 拍板）
- 分题型 P95：single_table 6.5s ｜ multi_join 13.4s ｜ aggregation 8.7s ｜ time_window 8.2s ｜ glossary_trap 7.2s
- 延迟绝对值受网络波动影响，仅相对可比（见声明③）

### token 成本（累加全部 AIMessage.usage_metadata，计费口径 §2.6）
- 单题均值：input 12080 ｜ output 857 ｜ total 12937（cache_read 10343 单列，不重复计）
- 总量：input 1811943 ｜ output 128544 ｜ total 1940487（cache_read 1551474）；人民币估算按当期牌价手填旁注

## 对照行对比表
（本次运行未提供 --compare 明细文件；对照行对比由 T3 汇主行与对照行两份 detail JSON 后生成。）

## 错答模式统计
| failure_mode | 次数 | 典型样本（case id） |
| --- | --- | --- |
| other | 85 | EV-002、EV-004、EV-007 |
| trap:month_shift_aug | 2 | EV-003 |

trap:<id> 与 valid_alt_gloss 分开统计（§4.3）：前者是真陷阱，后者是合法口径但答非所问；other 为错出新花样，人工看明细 JSON 归类后可回填登记表。

## flaky 题记录
| id | 各 run 判定 |
| --- | --- |
| EV-003 | fail → fail → pass |
| EV-014 | pass → fail → pass |
| EV-020 | pass → pass → fail |
| EV-022 | pass → fail → fail |
| EV-027 | fail → fail → pass |
| EV-040 | pass → pass → fail |
| EV-042 | fail → pass → pass |
| EV-044 | pass → fail → fail |

flaky 题不机械取众数：人工复盘明细 JSON（三次的 SQL 差在哪），结论记入 experiment-log（§3.2）。

## 多轮评测节
（本次未运行多轮评测；多轮题用 `--multiturn` 跑 eval_v1_multiturn.jsonl，独立报告节 §1.7/§2.7。）

## 逐题结果
| run | id | 判定 | pass_via | failure_mode | sql_source | 行数(金/模) | 延迟s | tokens(total) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| r1 | EV-001 | pass | sql | - | last | 15/15 | 3.147 | 10900 |
| r1 | EV-002 | fail | - | other | last | 5/5 | 7.416 | 12022 |
| r1 | EV-003 | fail | - | trap:month_shift_aug | last | 1/1 | 3.548 | 8382 |
| r1 | EV-004 | fail | - | other | last | 1/1 | 5.343 | 16474 |
| r1 | EV-005 | pass | text | - | last | 1/2 | 11.285 | 29928 |
| r1 | EV-006 | pass | text | - | last | 1/1 | 5.714 | 18718 |
| r1 | EV-007 | fail | - | other | last | 7/7 | 2.894 | 10461 |
| r1 | EV-008 | pass | text | - | last | 1/1 | 2.224 | 9963 |
| r1 | EV-009 | pass | text | - | last | 1/1 | 5.463 | 13917 |
| r1 | EV-010 | fail | - | other | last | 3/3 | 3.628 | 12629 |
| r1 | EV-011 | fail | - | other | last | 3/3 | 3.76 | 10676 |
| r1 | EV-012 | pass | text | - | last | 1/6 | 2.351 | 11652 |
| r1 | EV-013 | fail | - | other | last | 3/3 | 1.895 | 11699 |
| r1 | EV-014 | pass | sql | - | last | 6/6 | 2.622 | 13724 |
| r1 | EV-015 | fail | - | other | last | 5/5 | 4.322 | 14598 |
| r1 | EV-016 | fail | - | other | last | 20/1 | 7.205 | 16287 |
| r1 | EV-017 | fail | - | other | last | 8/1 | 6.477 | 12926 |
| r1 | EV-018 | fail | - | other | last | 20/1 | 5.59 | 15945 |
| r1 | EV-019 | fail | - | other | last | 5/5 | 4.129 | 10670 |
| r1 | EV-020 | pass | text | - | last | 1/1 | 11.781 | 15968 |
| r1 | EV-021 | fail | - | other | last | 5/1 | 4.622 | 7759 |
| r1 | EV-022 | pass | sql | - | last | 3/3 | 3.422 | 9081 |
| r1 | EV-023 | pass | text | - | last | 1/1 | 5.316 | 15818 |
| r1 | EV-024 | fail | - | other | last | 20/1 | 6.511 | 11533 |
| r1 | EV-025 | pass | text | - | last | 1/2 | 3.337 | 10543 |
| r1 | EV-026 | fail | - | other | last | 7/7 | 4.352 | 10919 |
| r1 | EV-027 | fail | - | other | last | 12/12 | 8.762 | 15438 |
| r1 | EV-028 | pass | text | - | last | 1/1 | 3.254 | 12305 |
| r1 | EV-029 | pass | text | - | last | 1/1 | 4.419 | 8695 |
| r1 | EV-030 | fail | - | other | last | 1/1 | 4.679 | 11256 |
| r1 | EV-031 | pass | sql | - | last | 8/8 | 2.368 | 11444 |
| r1 | EV-032 | pass | text | - | last | 1/8 | 4.83 | 9207 |
| r1 | EV-033 | fail | - | other | last | 1/1 | 4.329 | 15828 |
| r1 | EV-034 | fail | - | other | last | 8/1 | 10.576 | 12506 |
| r1 | EV-035 | fail | - | other | last | 3/1 | 12.51 | 21418 |
| r1 | EV-036 | fail | - | other | last | 3/3 | 3.716 | 10718 |
| r1 | EV-037 | pass | text | - | last | 1/1 | 7.835 | 21066 |
| r1 | EV-038 | fail | - | other | last | 20/20 | 9.16 | 17542 |
| r1 | EV-039 | fail | - | other | last | 1/8 | 10.395 | 15520 |
| r1 | EV-040 | pass | sql | - | last | 5/5 | 4.727 | 10196 |
| r1 | EV-041 | fail | - | other | last | 2/1 | 4.474 | 9888 |
| r1 | EV-042 | fail | - | other | last | 3/1 | 3.787 | 8732 |
| r1 | EV-043 | fail | - | other | last | 3/18 | 7.233 | 9716 |
| r1 | EV-044 | pass | sql | - | last | 8/8 | 9.989 | 12454 |
| r1 | EV-045 | pass | text | - | last | 1/2 | 4.686 | 9633 |
| r1 | EV-046 | fail | - | other | last | 7/7 | 4.937 | 9005 |
| r1 | EV-047 | fail | - | other | last | 4/1 | 6.346 | 11379 |
| r1 | EV-048 | pass | text | - | last | 1/1 | 7.85 | 11480 |
| r1 | EV-049 | pass | text | - | last | 1/3 | 7.247 | 10916 |
| r1 | EV-050 | pass | text | - | last | 1/1 | 4.97 | 8832 |
| r2 | EV-001 | pass | sql | - | last | 15/15 | 3.82 | 10844 |
| r2 | EV-002 | fail | - | other | last | 5/5 | 7.542 | 20991 |
| r2 | EV-003 | fail | - | trap:month_shift_aug | last | 1/1 | 3.119 | 8197 |
| r2 | EV-004 | fail | - | other | last | 1/1 | 4.836 | 10716 |
| r2 | EV-005 | pass | text | - | last | 1/1 | 6.614 | 15596 |
| r2 | EV-006 | pass | text | - | last | 1/1 | 8.752 | 25329 |
| r2 | EV-007 | fail | - | other | last | 7/7 | 4.39 | 10722 |
| r2 | EV-008 | pass | text | - | last | 1/1 | 4.022 | 10157 |
| r2 | EV-009 | pass | text | - | last | 1/1 | 5.953 | 13255 |
| r2 | EV-010 | fail | - | other | last | 3/3 | 3.71 | 12501 |
| r2 | EV-011 | fail | - | other | last | 3/1 | 5.115 | 10955 |
| r2 | EV-012 | pass | text | - | last | 1/6 | 3.528 | 11776 |
| r2 | EV-013 | fail | - | other | last | 3/3 | 3.27 | 11668 |
| r2 | EV-014 | fail | - | other | last | 6/1 | 4.262 | 14007 |
| r2 | EV-015 | fail | - | other | last | 5/5 | 6.827 | 14966 |
| r2 | EV-016 | fail | - | other | last | 20/1 | 7.068 | 16222 |
| r2 | EV-017 | fail | - | other | last | 8/1 | 6.42 | 12570 |
| r2 | EV-018 | fail | - | other | last | 20/1 | 6.482 | 15935 |
| r2 | EV-019 | fail | - | other | last | 5/5 | 4.314 | 10679 |
| r2 | EV-020 | pass | text | - | last | 1/8 | 9.953 | 15147 |
| r2 | EV-021 | fail | - | other | last | 5/5 | 3.628 | 7297 |
| r2 | EV-022 | fail | - | other | last | 3/3 | 4.938 | 9417 |
| r2 | EV-023 | pass | text | - | last | 1/1 | 4.062 | 10440 |
| r2 | EV-024 | fail | - | other | last | 20/20 | 7.757 | 11231 |
| r2 | EV-025 | pass | text | - | last | 1/2 | 3.888 | 10654 |
| r2 | EV-026 | fail | - | other | last | 7/7 | 4.111 | 10873 |
| r2 | EV-027 | fail | - | other | last | 12/12 | 8.484 | 10485 |
| r2 | EV-028 | pass | text | - | last | 1/1 | 4.479 | 12612 |
| r2 | EV-029 | pass | text | - | last | 1/1 | 3.784 | 8757 |
| r2 | EV-030 | fail | - | other | last | 1/12 | 6.975 | 11764 |
| r2 | EV-031 | pass | sql | - | last | 8/8 | 2.458 | 11469 |
| r2 | EV-032 | pass | text | - | last | 1/8 | 5.771 | 13866 |
| r2 | EV-033 | fail | - | other | last | 1/1 | 3.812 | 15638 |
| r2 | EV-034 | fail | - | other | last | 8/8 | 11.817 | 13243 |
| r2 | EV-035 | fail | - | other | last | 3/8 | 14.956 | 27570 |
| r2 | EV-036 | fail | - | other | last | 3/3 | 5.018 | 10942 |
| r2 | EV-037 | pass | text | - | last | 1/1 | 7.589 | 21077 |
| r2 | EV-038 | fail | - | other | last | 20/1 | 9.48 | 18097 |
| r2 | EV-039 | fail | - | other | last | 1/1 | 12.693 | 20142 |
| r2 | EV-040 | pass | sql | - | last | 5/5 | 5.171 | 10510 |
| r2 | EV-041 | fail | - | other | last | 2/1 | 4.421 | 9846 |
| r2 | EV-042 | pass | sql | - | last | 3/3 | 3.022 | 8379 |
| r2 | EV-043 | fail | - | other | last | 3/3 | 4.717 | 9189 |
| r2 | EV-044 | fail | - | other | last | 8/8 | 14.322 | 26540 |
| r2 | EV-045 | pass | text | - | last | 1/2 | 5.064 | 9879 |
| r2 | EV-046 | fail | - | other | last | 7/7 | 4.233 | 8989 |
| r2 | EV-047 | fail | - | other | last | 4/1 | 4.893 | 10961 |
| r2 | EV-048 | pass | text | - | last | 1/6 | 6.829 | 11116 |
| r2 | EV-049 | pass | text | - | last | 1/3 | 4.397 | 10432 |
| r2 | EV-050 | pass | text | - | last | 1/1 | 3.773 | 8419 |
| r3 | EV-001 | pass | sql | - | last | 15/15 | 4.321 | 11033 |
| r3 | EV-002 | fail | - | other | last | 5/5 | 8.722 | 16403 |
| r3 | EV-003 | pass | text | - | last | 1/1 | 3.306 | 8132 |
| r3 | EV-004 | fail | - | other | last | 1/1 | 5.565 | 16135 |
| r3 | EV-005 | pass | text | - | last | 1/1 | 6.921 | 15569 |
| r3 | EV-006 | pass | text | - | last | 1/1 | 5.149 | 12264 |
| r3 | EV-007 | fail | - | other | last | 7/7 | 3.952 | 10559 |
| r3 | EV-008 | pass | text | - | last | 1/1 | 3.209 | 9969 |
| r3 | EV-009 | pass | text | - | last | 1/1 | 6.697 | 13438 |
| r3 | EV-010 | fail | - | other | last | 3/5 | 4.087 | 12609 |
| r3 | EV-011 | fail | - | other | last | 3/3 | 4.172 | 10581 |
| r3 | EV-012 | pass | text | - | last | 1/6 | 3.534 | 11642 |
| r3 | EV-013 | fail | - | other | last | 3/3 | 2.665 | 11652 |
| r3 | EV-014 | pass | sql | - | last | 6/6 | 4.297 | 13724 |
| r3 | EV-015 | fail | - | other | last | 5/5 | 5.363 | 14547 |
| r3 | EV-016 | fail | - | other | last | 20/1 | 7.578 | 16328 |
| r3 | EV-017 | fail | - | other | last | 8/1 | 11.122 | 20013 |
| r3 | EV-018 | fail | - | other | last | 20/1 | 6.459 | 11076 |
| r3 | EV-019 | fail | - | other | last | 5/5 | 5.047 | 10678 |
| r3 | EV-020 | fail | - | other | last | 1/1 | 10.613 | 24585 |
| r3 | EV-021 | fail | - | other | last | 5/5 | 4.787 | 7662 |
| r3 | EV-022 | fail | - | other | last | 3/3 | 4.364 | 9363 |
| r3 | EV-023 | pass | text | - | last | 1/1 | 5.499 | 15853 |
| r3 | EV-024 | fail | - | other | last | 20/1 | 7.355 | 17056 |
| r3 | EV-025 | pass | text | - | last | 1/2 | 3.471 | 10605 |
| r3 | EV-026 | fail | - | other | last | 7/7 | 3.362 | 10760 |
| r3 | EV-027 | pass | sql | - | last | 12/12 | 6.459 | 9740 |
| r3 | EV-028 | pass | text | - | last | 1/1 | 3.915 | 12503 |
| r3 | EV-029 | pass | text | - | last | 1/1 | 3.989 | 8805 |
| r3 | EV-030 | fail | - | other | last | 1/1 | 4.41 | 10982 |
| r3 | EV-031 | pass | sql | - | last | 8/8 | 2.74 | 11493 |
| r3 | EV-032 | pass | text | - | last | 1/1 | 2.752 | 8448 |
| r3 | EV-033 | fail | - | other | last | 1/1 | 4.689 | 15841 |
| r3 | EV-034 | fail | - | other | last | 8/8 | 11.284 | 12784 |
| r3 | EV-035 | fail | - | other | last | 3/1 | 9.562 | 16250 |
| r3 | EV-036 | fail | - | other | last | 3/3 | 5.879 | 11177 |
| r3 | EV-037 | pass | text | - | last | 1/1 | 13.441 | 29342 |
| r3 | EV-038 | fail | - | other | last | 20/1 | 9.867 | 18057 |
| r3 | EV-039 | fail | - | other | last | 1/1 | 14.573 | 27963 |
| r3 | EV-040 | fail | - | other | last | 5/5 | 4.607 | 10280 |
| r3 | EV-041 | fail | - | other | last | 2/1 | 4.739 | 9967 |
| r3 | EV-042 | pass | sql | - | last | 3/3 | 3.069 | 8400 |
| r3 | EV-043 | fail | - | other | last | 3/18 | 7.277 | 14603 |
| r3 | EV-044 | fail | - | other | last | 8/8 | 7.811 | 11697 |
| r3 | EV-045 | pass | text | - | last | 1/2 | 5.499 | 9857 |
| r3 | EV-046 | fail | - | other | last | 7/7 | 4.787 | 9170 |
| r3 | EV-047 | fail | - | other | last | 4/1 | 5.33 | 11142 |
| r3 | EV-048 | pass | text | - | last | 1/1 | 3.406 | 10395 |
| r3 | EV-049 | pass | text | - | last | 1/1 | 6.086 | 10254 |
| r3 | EV-050 | pass | text | - | last | 1/1 | 5.016 | 8668 |

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

